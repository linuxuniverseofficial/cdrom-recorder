#!/usr/bin/env python3
"""
CD RECORDER — UI em ANSI puro, sem curses.
Funciona em qualquer TTY Linux headless.
"""
import sys, os, time, signal, subprocess, threading, re, tty, termios, select

# ── Dispositivos ──────────────────────────────────────────
CD_DEV       = "/dev/sr0"
LINEIN_DEV   = "hw:1,0"
SINK_MONITOR = "$(pactl get-default-sink).monitor"

# ── Control files ─────────────────────────────────────────
CTRL_CDROM  = "/tmp/cdrom"
CTRL_LINEIN = "/tmp/linein"

# ── Thresholds ────────────────────────────────────────────
SILENCE_THRESHOLD = 0.002
SILENCE_DURATION  = 2.0
SOUND_THRESHOLD   = 0.005
SOUND_DURATION    = 0.5

# ── Estado global ─────────────────────────────────────────
proc           = None
faixa          = 0
estado         = "PRONTO"
sync_ativo     = False
ultimo_rms     = 0.0
modo_idx       = 0
log_msgs       = []
blink          = False
running        = True
bandeja_aberta = False

MODOS = ["DESKTOP", "LINEIN", "CDROM"]

# ── ANSI ─────────────────────────────────────────────────
ESC = "\033"
def ansi(*c): return f"{ESC}[{';'.join(str(x) for x in c)}m"
def goto(r,c): return f"{ESC}[{r};{c}H"
def cls():
    sys.stdout.write(f"{ESC}[2J{ESC}[H")
    sys.stdout.flush()
def hide_cursor(): sys.stdout.write(f"{ESC}[?25l"); sys.stdout.flush()
def show_cursor(): sys.stdout.write(f"{ESC}[?25h"); sys.stdout.flush()

R  = ansi(0)       # reset
B  = ansi(1)       # bold
DM = ansi(2)       # dim
RV = ansi(7)       # reverse

RED  = ansi(31); GREEN = ansi(32); YELLOW = ansi(33)
CYAN = ansi(36); WHITE = ansi(37); MAGENTA = ansi(35)

# ── Utilitários ───────────────────────────────────────────
def term_size():
    try:    return os.get_terminal_size()
    except: return os.terminal_size((80, 24))

def ctr(s, w):
    """Centraliza string s numa largura w (conta chars visíveis = len(s))."""
    pad = max(0, (w - len(s)) // 2)
    return " " * pad + s + " " * (w - len(s) - pad)

def log(msg):
    ts = time.strftime("%H:%M:%S")
    log_msgs.append(f"{ts}  {msg}")
    if len(log_msgs) > 5: log_msgs.pop(0)

# ── Modo de input ─────────────────────────────────────────
def modo_atual():
    if os.path.exists(CTRL_CDROM):  return "CDROM"
    if os.path.exists(CTRL_LINEIN): return "LINEIN"
    return MODOS[modo_idx]

def audio_cmd_capture():
    m = modo_atual()
    if m == "CDROM":   return "cdparanoia -d /dev/sr1 -B - 2>/dev/null"
    if m == "LINEIN":  return f"arecord -D {LINEIN_DEV} -f cd -t raw"
    return f"parec --device={SINK_MONITOR} --format=s16le --rate=44100 --channels=2"

def audio_cmd_rms():
    m = modo_atual()
    if m == "LINEIN": return f"arecord -D {LINEIN_DEV} -f cd -t raw --latency-time=500000"
    return f"parec --device={SINK_MONITOR} --format=s16le --rate=44100 --channels=2 --latency-msec=500"

def toggle_modo():
    global modo_idx
    if os.path.exists(CTRL_CDROM) or os.path.exists(CTRL_LINEIN):
        log("Control file ativo — rm /tmp/cdrom ou /tmp/linein"); return
    modo_idx = (modo_idx + 1) % len(MODOS)
    log(f"Input -> {MODOS[modo_idx]}")

# ── Acoes de gravacao ─────────────────────────────────────
def iniciar_gravacao(auto=False):
    global proc, faixa, estado
    if estado in ("FINALIZANDO", "CONCLUIDO"): return
    faixa += 1; estado = "GRAVANDO"
    cmd = f"{audio_cmd_capture()} | wodim dev={CD_DEV} -tao -audio -swab -"
    proc = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"{'AUTO' if auto else 'MANUAL'} -- faixa {faixa:02d} [{modo_atual()}]")

def pausar(auto=False):
    global proc, estado
    if proc:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except: pass
        proc = None
    estado = "PAUSADO"
    log(f"{'AUTO' if auto else 'MANUAL'} -- faixa {faixa:02d} fechada")

def continuar():     iniciar_gravacao(auto=False)

def finalizar():
    global proc, estado
    estado = "FINALIZANDO"; log("Finalizando disco...")
    if proc:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except: pass
        proc = None
    subprocess.run(["wodim", f"dev={CD_DEV}", "-fix"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    estado = "CONCLUIDO"; log("Disco finalizado!")

def toggle_sync():
    global sync_ativo
    sync_ativo = not sync_ativo
    log(f"SYNC REC {'ATIVADO' if sync_ativo else 'DESATIVADO'}")

def toggle_bandeja():
    global bandeja_aberta
    if bandeja_aberta:
        log("Recolhendo bandeja...")
        subprocess.run(["eject", "-t", CD_DEV], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bandeja_aberta = False
    else:
        log("Ejetando disco...")
        subprocess.run(["eject", CD_DEV], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bandeja_aberta = True

# -- Play/Pause do CD fonte (sr1) --------------------------
proc_play = None

def toggle_play():
    global proc_play
    if proc_play and proc_play.poll() is None:
        try: os.killpg(os.getpgid(proc_play.pid), signal.SIGTERM)
        except: pass
        proc_play = None
        log("CD fonte pausado")
    else:
        cmd = "cvlc --no-video cdda:///dev/sr1 2>/dev/null"
        proc_play = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("CD fonte tocando...")

# ── Monitor RMS ───────────────────────────────────────────
def monitor_rms():
    global ultimo_rms
    silencio_desde = som_desde = None
    while running:
        if not sync_ativo:
            silencio_desde = som_desde = None
            time.sleep(0.3); continue
        try:
            r = subprocess.run(
                f"{audio_cmd_rms()} | sox -t raw -r 44100 -c 2 -b 16 -e signed - -n trim 0 0.4 stat 2>&1",
                shell=True, capture_output=True, text=True, timeout=2)
            rms = 0.0
            for linha in (r.stdout + r.stderr).splitlines():
                if "RMS amplitude" in linha:
                    m = re.search(r"[\d.]+$", linha)
                    if m: rms = float(m.group()); break
            ultimo_rms = rms
            agora = time.time()
            if estado == "GRAVANDO":
                if rms < SILENCE_THRESHOLD:
                    if silencio_desde is None: silencio_desde = agora
                    elif agora - silencio_desde >= SILENCE_DURATION:
                        pausar(auto=True); silencio_desde = som_desde = None
                else: silencio_desde = None
            elif estado == "PAUSADO":
                if rms > SOUND_THRESHOLD:
                    if som_desde is None: som_desde = agora
                    elif agora - som_desde >= SOUND_DURATION:
                        iniciar_gravacao(auto=True); som_desde = silencio_desde = None
                else: som_desde = None
        except: time.sleep(0.5)

# ── Blink ─────────────────────────────────────────────────
def blink_loop():
    global blink
    while running:
        blink = not blink
        time.sleep(0.55)

# ── UI ────────────────────────────────────────────────────
def draw_ui():
    sz   = term_size()
    rows = sz.lines
    cols = sz.columns
    W    = cols - 2   # largura interna

    modo = modo_atual()
    mcor = {"DESKTOP": CYAN, "LINEIN": YELLOW, "CDROM": MAGENTA}.get(modo, WHITE)

    # cada elemento e uma linha — imprime de cima pra baixo
    L = []  # lista de linhas prontas (sem \n, sem escape de borda embutido)

    def border(content_plain, content_colored=None):
        """
        Monta uma linha: bordo + conteúdo centralizado + bordo.
        content_plain  = texto sem escapes (para medir largura)
        content_colored = texto com escapes (para exibir); se None usa content_plain
        """
        if content_colored is None: content_colored = content_plain
        pad = max(0, (W - len(content_plain)) // 2)
        rpad = W - len(content_plain) - pad
        return DM + "║" + R + " " * pad + content_colored + " " * rpad + DM + "║" + R

    def sep_line():
        return DM + "╠" + "═" * W + "╣" + R

    # topo
    L.append(DM + "╔" + "═" * W + "╗" + R)

    # titulo + sync na mesma linha
    title = "    CD RECORDER    "
    sync_plain = "SYNC ON " if sync_ativo else "SYNC OFF"
    sync_col   = B + GREEN + sync_plain + R if sync_ativo else DM + sync_plain + R
    title_col  = B + CYAN + title + R
    gap = W - len(title) - len(sync_plain) - 2
    L.append(DM + "║" + R + title_col + " " * max(1, gap) + sync_col + " " + DM + "║" + R)

    L.append(sep_line())

    # modo input
    ctrl = ""
    if os.path.exists(CTRL_CDROM):    ctrl = " [/tmp/cdrom]"
    elif os.path.exists(CTRL_LINEIN): ctrl = " [/tmp/linein]"
    input_plain   = f"INPUT: {modo}{ctrl}"
    input_colored = B + mcor + input_plain + R
    L.append(border(input_plain, input_colored))

    L.append(sep_line())

    # estado
    estado_map = {
        "PRONTO":      ("PRONTO",        WHITE,  False),
        "GRAVANDO":    ("GRAVANDO",       RED,    True),
        "PAUSADO":     ("PAUSADO",        YELLOW, True),
        "FINALIZANDO": ("FINALIZANDO...", YELLOW, False),
        "CONCLUIDO":   ("CONCLUIDO",      GREEN,  False),
    }
    elabel, ecor, episca = estado_map.get(estado, ("?", WHITE, False))
    eshow = elabel if (not episca or blink) else ""
    L.append(border(elabel, B + ecor + eshow + R))

    L.append(border(""))

    # faixa
    fstr = f"FAIXA  {faixa:02d}" if faixa > 0 else ""
    L.append(border(fstr, B + WHITE + fstr + R))

    L.append(border(""))

    # VU meter
    vu_w    = min(36, W - 8)
    vu_fill = int(min(ultimo_rms / 0.1, 1.0) * vu_w)
    vu_bar  = "█" * vu_fill + "░" * (vu_w - vu_fill)
    vcor    = GREEN if ultimo_rms < 0.07 else YELLOW if ultimo_rms < 0.09 else RED
    vu_plain   = f"RMS {vu_bar}"
    vu_colored = DM + "RMS " + R + vcor + vu_bar + R
    L.append(border(vu_plain, vu_colored))

    L.append(border(""))

    # log (4 linhas)
    for i in range(4):
        msg = log_msgs[i] if i < len(log_msgs) else ""
        msg = msg[:W-2]
        L.append(DM + "║  " + R + DM + msg.ljust(W-3) + R + DM + "║" + R)

    # preenche linhas vazias ate o menu
    used     = len(L)
    menu_pos = rows - 3   # 1 sep + 1 menu + 1 fundo
    empties  = menu_pos - used - 1
    for _ in range(max(0, empties)):
        L.append(border(""))

    # separador menu
    L.append(sep_line())

    # menu
    itens = [
        (" 2 GRAVAR ",    RED),
        (" 3 PAUSAR ",    YELLOW),
        (" 4 CONTINUAR ", GREEN),
        (" 5 FINALIZAR ", CYAN),
        (" 6 SYNC ",      MAGENTA),
        (" 7 INPUT ",     mcor),
        (" 8 BANDEJA ",   WHITE),
        (" 9 PLAY/PAUSE ", GREEN),
        (" 0 SAIR ",      WHITE),
    ]
    menu_str   = ""
    menu_plain = ""
    for txt, cor in itens:
        if len(menu_plain) + len(txt) + 2 > W: break
        menu_str   += B + RV + cor + txt + R + "  "
        menu_plain += txt + "  "
    pad = max(0, (W - len(menu_plain)) // 2)
    L.append(DM + "║" + R + " " * pad + menu_str + " " * max(0, W - len(menu_plain) - pad) + DM + "║" + R)

    # fundo
    L.append(DM + "╚" + "═" * W + "╝" + R)

    # manda tudo de uma vez, a partir da linha 1 col 1
    output = goto(1, 1) + "\n".join(L[:rows])
    sys.stdout.write(output)
    sys.stdout.flush()

# ── Tecla ─────────────────────────────────────────────────
def get_key():
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        rr, _, _ = select.select([sys.stdin], [], [], 0.05)
        if rr: return sys.stdin.read(1)
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ── Main ──────────────────────────────────────────────────
def main():
    global running
    time.sleep(2)
    os.environ.setdefault("TERM", "linux")
    hide_cursor()
    cls()

    threading.Thread(target=blink_loop,  daemon=True).start()
    threading.Thread(target=monitor_rms, daemon=True).start()

    ACOES = {
        '2': lambda: iniciar_gravacao(False),
        '3': lambda: pausar(False),
        '4': continuar,
        '5': finalizar,
        '6': toggle_sync,
        '7': toggle_modo,
        '8': toggle_bandeja,
        '9': toggle_play,
    }

    try:
        while True:
            draw_ui()
            key = get_key()
            if key in ('0', '\x03'): break
            if key and key in ACOES:
                if estado not in ("FINALIZANDO", "CONCLUIDO"):
                    ACOES[key]()
    finally:
        running = False
        show_cursor()
        cls()
        if proc:
            try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except: pass
        print("Ate logo.")

if __name__ == "__main__":
    main()
