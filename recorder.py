#!/usr/bin/env python3
"""
CD RECORDER — UI em ANSI puro, sem curses.
Funciona em qualquer TTY Linux headless.
"""
import sys, os, time, signal, subprocess, threading, re, tty, termios

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
proc       = None
faixa      = 0
estado     = "PRONTO"
sync_ativo = False
ultimo_rms = 0.0
modo_idx   = 0
log_msgs   = []
blink      = False
running    = True

MODOS = ["DESKTOP", "LINEIN", "CDROM"]

# ── ANSI helpers ──────────────────────────────────────────
ESC = "\033"
def ansi(*codes):      return f"{ESC}[{';'.join(str(c) for c in codes)}m"
def goto(r, c):        return f"{ESC}[{r};{c}H"
def clear():           return f"{ESC}[2J"
def hide_cursor():     sys.stdout.write(f"{ESC}[?25l"); sys.stdout.flush()
def show_cursor():     sys.stdout.write(f"{ESC}[?25h"); sys.stdout.flush()
def cls():             sys.stdout.write(clear() + goto(1,1)); sys.stdout.flush()

RESET   = ansi(0)
BOLD    = ansi(1)
DIM     = ansi(2)
REVERSE = ansi(7)

# cores foreground
BLACK   = ansi(30)
RED     = ansi(31)
GREEN   = ansi(32)
YELLOW  = ansi(33)
BLUE    = ansi(34)
MAGENTA = ansi(35)
CYAN    = ansi(36)
WHITE   = ansi(37)

# ── Tamanho do terminal ───────────────────────────────────
def term_size():
    try:
        rows, cols = os.get_terminal_size()
    except:
        rows, cols = 24, 80
    return rows, cols

# ── Log ───────────────────────────────────────────────────
def log(msg):
    ts = time.strftime("%H:%M:%S")
    log_msgs.append(f"{ts}  {msg}")
    if len(log_msgs) > 5:
        log_msgs.pop(0)

# ── Modo de input ─────────────────────────────────────────
def modo_atual():
    if os.path.exists(CTRL_CDROM):
        return "CDROM"
    if os.path.exists(CTRL_LINEIN):
        return "LINEIN"
    return MODOS[modo_idx]

def audio_cmd_capture():
    modo = modo_atual()
    if modo == "CDROM":
        return "cdparanoia -d /dev/sr1 -B - 2>/dev/null"
    elif modo == "LINEIN":
        return f"arecord -D {LINEIN_DEV} -f cd -t raw"
    else:
        return (f"parec --device={SINK_MONITOR} "
                f"--format=s16le --rate=44100 --channels=2")

def audio_cmd_rms():
    modo = modo_atual()
    if modo == "LINEIN":
        return f"arecord -D {LINEIN_DEV} -f cd -t raw --latency-time=500000"
    return (f"parec --device={SINK_MONITOR} "
            f"--format=s16le --rate=44100 --channels=2 --latency-msec=500")

def toggle_modo():
    global modo_idx
    if os.path.exists(CTRL_CDROM) or os.path.exists(CTRL_LINEIN):
        log("Control file ativo — rm /tmp/cdrom ou /tmp/linein")
        return
    modo_idx = (modo_idx + 1) % len(MODOS)
    log(f"Input → {MODOS[modo_idx]}")

# ── Ações de gravação ─────────────────────────────────────
def iniciar_gravacao(auto=False):
    global proc, faixa, estado
    if estado in ("FINALIZANDO", "CONCLUIDO"):
        return
    faixa += 1
    estado = "GRAVANDO"
    cap = audio_cmd_capture()
    cmd = f"{cap} | wodim dev={CD_DEV} -tao -audio -swab -"
    proc = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"{'AUTO' if auto else 'MANUAL'} — faixa {faixa:02d} [{modo_atual()}]")

def pausar(auto=False):
    global proc, estado
    if proc:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        proc = None
    estado = "PAUSADO"
    log(f"{'AUTO' if auto else 'MANUAL'} — faixa {faixa:02d} fechada")

def continuar():
    iniciar_gravacao(auto=False)

def finalizar():
    global proc, estado
    estado = "FINALIZANDO"
    log("Finalizando disco...")
    if proc:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        proc = None
    subprocess.run(["wodim", f"dev={CD_DEV}", "-fix"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    estado = "CONCLUIDO"
    log("✔ Disco finalizado!")

def toggle_sync():
    global sync_ativo
    sync_ativo = not sync_ativo
    log(f"SYNC REC {'ATIVADO' if sync_ativo else 'DESATIVADO'}")

# ── Monitor RMS ───────────────────────────────────────────
def monitor_rms():
    global ultimo_rms
    silencio_desde = som_desde = None
    while running:
        if not sync_ativo:
            silencio_desde = som_desde = None
            time.sleep(0.3)
            continue
        try:
            cap = audio_cmd_rms()
            r = subprocess.run(
                f"{cap} | sox -t raw -r 44100 -c 2 -b 16 -e signed - "
                f"-n trim 0 0.4 stat 2>&1",
                shell=True, capture_output=True, text=True, timeout=2)
            rms = 0.0
            for linha in (r.stdout + r.stderr).splitlines():
                if "RMS amplitude" in linha:
                    m = re.search(r"[\d.]+$", linha)
                    if m:
                        rms = float(m.group())
                        break
            ultimo_rms = rms
            agora = time.time()
            if estado == "GRAVANDO":
                if rms < SILENCE_THRESHOLD:
                    if silencio_desde is None: silencio_desde = agora
                    elif agora - silencio_desde >= SILENCE_DURATION:
                        pausar(auto=True); silencio_desde = som_desde = None
                else:
                    silencio_desde = None
            elif estado == "PAUSADO":
                if rms > SOUND_THRESHOLD:
                    if som_desde is None: som_desde = agora
                    elif agora - som_desde >= SOUND_DURATION:
                        iniciar_gravacao(auto=True); som_desde = silencio_desde = None
                else:
                    som_desde = None
        except Exception:
            time.sleep(0.5)

# ── Blink ─────────────────────────────────────────────────
def blink_loop():
    global blink
    while running:
        blink = not blink
        time.sleep(0.55)

# ── Desenho ANSI ──────────────────────────────────────────
def draw_line(r, c, text, *attrs):
    style = "".join(attrs)
    sys.stdout.write(goto(r, c) + style + text + RESET)

def center(text, width):
    pad = max(0, (width - len(text)) // 2)
    return " " * pad + text

def draw_ui():
    rows, cols = term_size()
    mid = cols // 2
    out = []

    # limpa tela
    out.append(clear())

    # ── borda superior ──
    out.append(goto(1, 1) + DIM + "╔" + "═" * (cols-2) + "╗" + RESET)

    # ── título ──
    title = "▓▓  CD RECORDER  ▓▓"
    out.append(goto(2, 1) + DIM + "║" + RESET)
    out.append(goto(2, mid - len(title)//2) + BOLD + CYAN + title + RESET)
    out.append(goto(2, cols) + DIM + "║" + RESET)

    # ── SYNC badge (canto direito linha 2) ──
    if sync_ativo:
        badge = BOLD + GREEN + " SYNC ON " + RESET
        badge_len = 9
    else:
        badge = DIM + " SYNC OFF " + RESET
        badge_len = 10
    out.append(goto(2, cols - badge_len - 2) + badge)

    # ── separador ──
    out.append(goto(3, 1) + DIM + "╠" + "═" * (cols-2) + "╣" + RESET)

    # ── modo de input ──
    modo = modo_atual()
    ctrl = ""
    if os.path.exists(CTRL_CDROM):   ctrl = " [/tmp/cdrom]"
    elif os.path.exists(CTRL_LINEIN): ctrl = " [/tmp/linein]"
    modo_cor = {
        "DESKTOP": CYAN, "LINEIN": YELLOW, "CDROM": MAGENTA
    }.get(modo, WHITE)
    modo_str = f"INPUT: {modo}{ctrl}"
    out.append(goto(4, 1) + DIM + "║" + RESET)
    out.append(goto(4, mid - len(modo_str)//2) + BOLD + modo_cor + modo_str + RESET)
    out.append(goto(4, cols) + DIM + "║" + RESET)

    # ── separador ──
    out.append(goto(5, 1) + DIM + "╠" + "═" * (cols-2) + "╣" + RESET)

    # ── estado principal ──
    estado_cfg = {
        "PRONTO":      ("●  PRONTO",       WHITE,  False),
        "GRAVANDO":    ("⏺  GRAVANDO",      RED,    True),
        "PAUSADO":     ("⏸  PAUSADO",       YELLOW, True),
        "FINALIZANDO": ("⏳  FINALIZANDO…", YELLOW, False),
        "CONCLUIDO":   ("✔  CONCLUIDO",     GREEN,  False),
    }
    label, cor, pisca = estado_cfg.get(estado, ("?", WHITE, False))

    estado_row = rows // 2 - 2
    out.append(goto(estado_row, 1) + DIM + "║" + RESET)
    out.append(goto(estado_row, cols) + DIM + "║" + RESET)
    if (not pisca) or blink:
        s = center(label, cols - 2)
        out.append(goto(estado_row, 2) + BOLD + cor + s + RESET)

    # ── faixa ──
    faixa_row = estado_row + 2
    out.append(goto(faixa_row, 1) + DIM + "║" + RESET)
    out.append(goto(faixa_row, cols) + DIM + "║" + RESET)
    if faixa > 0:
        fs = f"FAIXA  {faixa:02d}"
        out.append(goto(faixa_row, mid - len(fs)//2) + BOLD + WHITE + fs + RESET)

    # ── VU meter ──
    vu_row = faixa_row + 2
    vu_w   = min(40, cols - 12)
    vu_fill = int(min(ultimo_rms / 0.1, 1.0) * vu_w)
    vu_bar  = "█" * vu_fill + "░" * (vu_w - vu_fill)
    vu_cor  = GREEN if ultimo_rms < 0.07 else YELLOW if ultimo_rms < 0.09 else RED
    out.append(goto(vu_row, 1) + DIM + "║" + RESET)
    out.append(goto(vu_row, cols) + DIM + "║" + RESET)
    out.append(goto(vu_row, mid - vu_w//2 - 4) + DIM + "RMS " + RESET)
    out.append(goto(vu_row, mid - vu_w//2) + vu_cor + vu_bar + RESET)

    # ── log de eventos ──
    log_start = vu_row + 2
    for i in range(5):
        r = log_start + i
        if r >= rows - 4:
            break
        out.append(goto(r, 1) + DIM + "║" + RESET)
        out.append(goto(r, cols) + DIM + "║" + RESET)
        if i < len(log_msgs):
            msg = log_msgs[-(len(log_msgs) - i)]  if i < len(log_msgs) else ""
            # pega do mais antigo para o mais novo
            msg = log_msgs[i] if i < len(log_msgs) else ""
            out.append(goto(r, 4) + DIM + msg[:cols-6] + RESET)

    # ── preenche linhas do meio com bordas ──
    for r in range(6, rows - 3):
        if r not in (estado_row, faixa_row, vu_row) and \
           r not in range(log_start, log_start + 5):
            out.append(goto(r, 1)    + DIM + "║" + RESET)
            out.append(goto(r, cols) + DIM + "║" + RESET)

    # ── separador menu ──
    out.append(goto(rows-3, 1) + DIM + "╠" + "═" * (cols-2) + "╣" + RESET)

    # ── menu ──
    modo_cor2 = {
        "DESKTOP": CYAN, "LINEIN": YELLOW, "CDROM": MAGENTA
    }.get(modo, WHITE)
    itens = [
        (" 2 GRAVAR ",    RED,     True),
        (" 3 PAUSAR ",    YELLOW,  True),
        (" 4 CONTINUAR ", GREEN,   True),
        (" 5 FINALIZAR ", CYAN,    True),
        (" 6 SYNC ",      MAGENTA, True),
        (" 7 INPUT ",     modo_cor2, True),
        (" 0 SAIR ",      WHITE,   True),
    ]
    out.append(goto(rows-2, 1) + DIM + "║" + RESET)
    out.append(goto(rows-2, cols) + DIM + "║" + RESET)
    x = 3
    for texto, cor, rev in itens:
        if x + len(texto) >= cols - 2:
            break
        style = BOLD + cor + REVERSE if rev else BOLD + cor
        out.append(goto(rows-2, x) + style + texto + RESET)
        x += len(texto) + 2

    # ── borda inferior ──
    out.append(goto(rows-1, 1) + DIM + "╚" + "═" * (cols-2) + "╝" + RESET)
    # ── posiciona cursor fora da view ──
    out.append(goto(rows, 1))

    sys.stdout.write("".join(out))
    sys.stdout.flush()

# ── Leitura de tecla raw ──────────────────────────────────
def get_key_nonblock():
    """Retorna tecla pressionada ou None, sem bloquear."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # timeout via select
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            return sys.stdin.read(1)
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ── Main ──────────────────────────────────────────────────
def main():
    global running

    # aguarda TTY estabilizar
    time.sleep(2)
    os.environ.setdefault("TERM", "linux")

    hide_cursor()
    cls()

    # inicia threads
    threading.Thread(target=blink_loop,  daemon=True).start()
    threading.Thread(target=monitor_rms, daemon=True).start()

    ACOES = {
        '2': lambda: iniciar_gravacao(False),
        '3': lambda: pausar(False),
        '4': continuar,
        '5': finalizar,
        '6': toggle_sync,
        '7': toggle_modo,
    }

    try:
        while True:
            draw_ui()
            key = get_key_nonblock()

            if key == '0' or key == '\x03':  # 0 ou Ctrl+C
                break
            if key and key in ACOES:
                if estado not in ("FINALIZANDO", "CONCLUIDO"):
                    ACOES[key]()

    finally:
        running = False
        show_cursor()
        cls()
        if proc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except:
                pass
        print("Até logo.")

if __name__ == "__main__":
    main()
