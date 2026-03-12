#!/usr/bin/env python3
"""
CD RECORDER — UI em ANSI puro, sem curses.
Funciona em qualquer TTY Linux headless.
RMS via parec+sox (sem sounddevice/numpy).
Devices detectados automaticamente via pactl.
"""
import sys, os, time, signal, subprocess, threading, re, tty, termios, select

# ── Dispositivos (detectados automaticamente) ─────────────
CD_DEV = "/dev/sr0"

def detectar_devices():
    """Detecta sink analógico e source line-in via pactl."""
    try:
        out = subprocess.check_output(["pactl", "list", "short", "sinks"],
                                      text=True, stderr=subprocess.DEVNULL)
        sink = None
        for linha in out.splitlines():
            if "analog" in linha and "hdmi" not in linha.lower():
                sink = linha.split()[1]
                break
        if not sink:
            # fallback: primeiro sink que não seja HDMI
            for linha in out.splitlines():
                if "hdmi" not in linha.lower():
                    sink = linha.split()[1]
                    break

        out2 = subprocess.check_output(["pactl", "list", "short", "sources"],
                                       text=True, stderr=subprocess.DEVNULL)
        source = None
        for linha in out2.splitlines():
            name = linha.split()[1] if len(linha.split()) > 1 else ""
            if "input" in name and "analog" in name:
                source = name
                break

        return sink, source
    except Exception:
        return None, None

# Detecta na inicialização — pode ser sobrescrito no main
_sink, _source = detectar_devices()
SINK_ANALOG   = _sink   or "alsa_output.pci-0000_00_1b.0.analog-stereo"
SOURCE_LINEIN = _source or "alsa_input.pci-0000_00_1b.0.analog-stereo"
SOURCE_DESKTOP = SINK_ANALOG + ".monitor"

PULSE_SOURCE = SOURCE_LINEIN
input_modo   = "LINEIN"

# ── Thresholds SYNC ───────────────────────────────────────
SILENCE_THRESHOLD = 0.005
SILENCE_DURATION  = 2.0
SOUND_THRESHOLD   = 0.01
SOUND_DURATION    = 0.5

# ── Estado global ─────────────────────────────────────────
proc               = None
faixa              = 0
estado             = "PRONTO"
sync_ativo         = False
ultimo_rms         = 0.0
log_msgs           = []
blink              = False
running            = True
bandeja_aberta     = False
finalizar_pendente = False

# ── ANSI ──────────────────────────────────────────────────
ESC = "\033"
def ansi(*c): return f"{ESC}[{';'.join(str(x) for x in c)}m"
def goto(r,c): return f"{ESC}[{r};{c}H"
def cls():
    sys.stdout.write(f"{ESC}[2J{ESC}[H")
    sys.stdout.flush()
def hide_cursor(): sys.stdout.write(f"{ESC}[?25l"); sys.stdout.flush()
def show_cursor(): sys.stdout.write(f"{ESC}[?25h"); sys.stdout.flush()

R  = ansi(0);  B  = ansi(1);  DM = ansi(2);  RV = ansi(7)
RED = ansi(31); GREEN = ansi(32); YELLOW = ansi(33)
CYAN = ansi(36); WHITE = ansi(37); MAGENTA = ansi(35)

# ── Utilitários ───────────────────────────────────────────
def term_size():
    try:    return os.get_terminal_size()
    except: return os.terminal_size((80, 24))

def log(msg):
    ts = time.strftime("%H:%M:%S")
    log_msgs.append(f"{ts}  {msg}")
    if len(log_msgs) > 5: log_msgs.pop(0)

# ── Input mode ────────────────────────────────────────────
def toggle_input():
    global PULSE_SOURCE, input_modo
    if input_modo == "LINEIN":
        input_modo   = "DESKTOP"
        PULSE_SOURCE = SOURCE_DESKTOP
    else:
        input_modo   = "LINEIN"
        PULSE_SOURCE = SOURCE_LINEIN
    log(f"Input: {input_modo}")

# ── Gravação ──────────────────────────────────────────────
def iniciar_gravacao(auto=False):
    global proc, faixa, estado
    if estado in ("GRAVANDO", "AGUARDANDO", "FINALIZANDO", "CONCLUIDO"): return
    faixa += 1
    estado = "AGUARDANDO"
    log(f"{'AUTO' if auto else 'MANUAL'} -- faixa {faixa:02d} aguardando...")

    def _iniciar():
        global proc, estado
        time.sleep(3)
        if estado != "AGUARDANDO": return
        cmd = (f"parec --device={PULSE_SOURCE} --format=s16le --rate=44100 --channels=2"
               f" | wodim dev={CD_DEV} speed=1 -tao -audio -swab -")
        p = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        proc = p
        with open("/tmp/recorder.pid", "w") as f:
            f.write(str(os.getpgid(p.pid)))
        for linha in p.stderr:
            txt = linha.decode(errors="ignore").lower()
            if "starting" in txt or "write" in txt or "tao" in txt:
                estado = "GRAVANDO"
                log(f"Faixa {faixa:02d} gravando")
                break
        if estado == "AGUARDANDO":
            estado = "GRAVANDO"

    threading.Thread(target=_iniciar, daemon=True).start()

def pausar(auto=False):
    global proc, estado
    if estado not in ("GRAVANDO", "AGUARDANDO"): return
    estado = "PAUSADO"
    log(f"{'AUTO' if auto else 'MANUAL'} -- faixa {faixa:02d} fechando...")
    _proc = proc
    def _matar():
        global proc
        if _proc:
            try: os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
            except: pass
        proc = None
        subprocess.run(["pkill", "-TERM", "parec"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            r = subprocess.run(["pgrep", "wodim"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode != 0: break
            time.sleep(0.1)
        try: os.remove("/tmp/recorder.pid")
        except: pass
        log(f"Faixa {faixa:02d} fechada")
    threading.Thread(target=_matar, daemon=True).start()

def finalizar():
    global proc, estado, finalizar_pendente
    if not finalizar_pendente:
        finalizar_pendente = True
        log("Confirme: pressione 3 novamente!")
        return
    finalizar_pendente = False
    if estado in ("GRAVANDO", "AGUARDANDO"):
        pausar()
        time.sleep(1)
    estado = "FINALIZANDO"
    log("Finalizando disco...")
    if proc:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except: pass
        proc = None
    def _fix():
        global estado
        subprocess.run(["wodim", f"dev={CD_DEV}", "-fix"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        estado = "CONCLUIDO"
        log("Disco finalizado!")
    threading.Thread(target=_fix, daemon=True).start()

def toggle_sync():
    global sync_ativo
    sync_ativo = not sync_ativo
    log(f"SYNC {'ATIVADO' if sync_ativo else 'DESATIVADO'}")

def toggle_bandeja():
    global bandeja_aberta
    if estado == "GRAVANDO":
        log("ERRO: pause antes de ejetar!")
        return
    if bandeja_aberta:
        log("Recolhendo bandeja...")
        subprocess.run(["eject", "-t", CD_DEV],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bandeja_aberta = False
    else:
        log("Ejetando disco...")
        subprocess.run(["eject", CD_DEV],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bandeja_aberta = True

# ── Monitor RMS via parec + sox ───────────────────────────
def monitor_rms():
    global ultimo_rms
    silencio_desde = som_desde = None

    while running:
        try:
            cmd = (f"parec --device={PULSE_SOURCE} --format=s16le --rate=44100 --channels=2"
                   f" | sox -t raw -r 44100 -c 2 -b 16 -e signed - -n trim 0 0.4 stat 2>&1")
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=3)
            rms = 0.0
            for linha in (r.stdout + r.stderr).splitlines():
                if re.search(r"rms\s+amplitude", linha, re.IGNORECASE):
                    m = re.search(r"[\d.]+\s*$", linha)
                    if m:
                        rms = float(m.group().strip())
                        break
            ultimo_rms = rms

            if sync_ativo:
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
            else:
                silencio_desde = som_desde = None

        except Exception:
            time.sleep(1)

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
    W    = cols - 2

    L = []

    def border(content_plain, content_colored=None):
        if content_colored is None: content_colored = content_plain
        pad  = max(0, (W - len(content_plain)) // 2)
        rpad = W - len(content_plain) - pad
        return DM + "║" + R + " " * pad + content_colored + " " * rpad + DM + "║" + R

    def sep_line():
        return DM + "╠" + "═" * W + "╣" + R

    L.append(DM + "╔" + "═" * W + "╗" + R)

    # titulo + input + sync
    title       = " CD RECORDER "
    input_plain = f" {input_modo} "
    sync_plain  = "SYNC ON " if sync_ativo else "SYNC OFF"
    input_col   = B + YELLOW + input_plain + R if input_modo == "LINEIN" else DM + input_plain + R
    sync_col    = B + GREEN + sync_plain + R if sync_ativo else DM + sync_plain + R
    title_col   = B + CYAN + title + R
    gap = W - len(title) - len(input_plain) - len(sync_plain) - 3
    L.append(DM + "║" + R + title_col + " " * max(1, gap) + input_col + " " + sync_col + " " + DM + "║" + R)

    L.append(sep_line())

    # estado
    estado_map = {
        "PRONTO":      ("PRONTO",         WHITE,  False),
        "AGUARDANDO":  ("GRAVANDO",        RED,    True),
        "GRAVANDO":    ("GRAVANDO",        RED,    True),
        "PAUSADO":     ("PAUSADO",         YELLOW, True),
        "FINALIZANDO": ("FINALIZANDO...",  YELLOW, False),
        "CONCLUIDO":   ("CONCLUIDO",       GREEN,  False),
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
    vu_fill = int(min(ultimo_rms / 0.3, 1.0) * vu_w)
    vu_bar  = "█" * vu_fill + "░" * (vu_w - vu_fill)
    vcor    = GREEN if ultimo_rms < 0.1 else YELLOW if ultimo_rms < 0.2 else RED
    vu_plain   = f"RMS {vu_bar}"
    vu_colored = DM + "RMS " + R + vcor + vu_bar + R
    L.append(border(vu_plain, vu_colored))

    L.append(border(""))

    # log (4 linhas)
    for i in range(4):
        msg = log_msgs[i] if i < len(log_msgs) else ""
        msg = msg[:W-2]
        L.append(DM + "║  " + R + DM + msg.ljust(W-3) + R + DM + "║" + R)

    # preenche ate menu
    used     = len(L)
    menu_pos = rows - 4
    for _ in range(max(0, menu_pos - used - 1)):
        L.append(border(""))

    L.append(sep_line())

    # menu
    linha1 = [
        (" 1 GRAVAR ",    RED),
        (" 2 PAUSAR ",    YELLOW),
        (" 3 FINALIZAR ", CYAN),
    ]
    linha2 = [
        (" 4 INPUT ",   YELLOW),
        (" 5 SYNC ",    MAGENTA),
        (" 0 SAIR ",    WHITE),
    ]

    def render_menu_line(itens):
        ms = ""; mp = ""
        for txt, cor in itens:
            if len(mp) + len(txt) + 2 > W: break
            ms += B + RV + cor + txt + R + "  "
            mp += txt + "  "
        pad = max(0, (W - len(mp)) // 2)
        return DM + "║" + R + " " * pad + ms + " " * max(0, W - len(mp) - pad) + DM + "║" + R

    L.append(render_menu_line(linha1))
    L.append(render_menu_line(linha2))
    L.append(DM + "╚" + "═" * W + "╝" + R)

    sys.stdout.write(goto(1, 1) + "\n".join(L[:rows]))
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
    global running, SINK_ANALOG, SOURCE_LINEIN, SOURCE_DESKTOP, PULSE_SOURCE
    time.sleep(2)
    os.environ.setdefault("TERM", "linux")
    os.environ.setdefault("PULSE_RUNTIME_PATH", f"/run/user/{os.getuid()}/pulse")
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")

    # Re-detecta devices após PulseAudio estar pronto
    sink, source = detectar_devices()
    if sink:
        SINK_ANALOG    = sink
        SOURCE_DESKTOP = sink + ".monitor"
    if source:
        SOURCE_LINEIN = source
        PULSE_SOURCE  = source

    os.environ["PULSE_SOURCE"] = PULSE_SOURCE

    # Acorda devices
    subprocess.run(["pactl", "suspend-source", SOURCE_LINEIN, "0"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pactl", "suspend-sink", SINK_ANALOG, "0"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    hide_cursor()
    cls()
    log(f"Line-in: {SOURCE_LINEIN}")
    log("Pronto para gravar!")

    threading.Thread(target=blink_loop,  daemon=True).start()
    threading.Thread(target=monitor_rms, daemon=True).start()

    ACOES = {
        '1': lambda: iniciar_gravacao(False),
        '2': lambda: pausar(False),
        '3': finalizar,
        '4': toggle_input,
        '5': toggle_sync,
    }

    try:
        global finalizar_pendente
        while True:
            draw_ui()
            key = get_key()
            if key in ('0', '\x03'): break
            if key and key in ACOES:
                if estado not in ("FINALIZANDO", "CONCLUIDO"):
                    if key != '3' and finalizar_pendente:
                        finalizar_pendente = False
                        log("Finalização cancelada")
                    ACOES[key]()
    finally:
        running = False
        show_cursor()
        cls()
        if proc:
            try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except: pass
        try:
            pid = int(open("/tmp/recorder.pid").read().strip())
            os.killpg(pid, signal.SIGTERM)
            os.remove("/tmp/recorder.pid")
        except: pass
        print("Ate logo.")
        os._exit(0)

if __name__ == "__main__":
    main()
