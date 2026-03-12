#!/usr/bin/env python3
"""
CD RECORDER — UI em ANSI puro, sem curses.
Funciona em qualquer TTY Linux headless.
Captura: LINE IN fixo via parec.
"""
import sys, os, time, signal, subprocess, threading, tty, termios, select

# ── Dispositivos ──────────────────────────────────────────
CD_DEV       = "/dev/sr0"
PULSE_SOURCE = "alsa_input.pci-0000_00_1b.0.analog-stereo"

# ── Estado global ─────────────────────────────────────────
proc               = None
faixa              = 0
estado             = "PRONTO"
log_msgs           = []
blink              = False
running            = True
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

R  = ansi(0)
B  = ansi(1)
DM = ansi(2)
RV = ansi(7)

RED    = ansi(31); GREEN  = ansi(32); YELLOW = ansi(33)
CYAN   = ansi(36); WHITE  = ansi(37)

# ── Utilitários ───────────────────────────────────────────
def term_size():
    try:    return os.get_terminal_size()
    except: return os.terminal_size((80, 24))

def log(msg):
    ts = time.strftime("%H:%M:%S")
    log_msgs.append(f"{ts}  {msg}")
    if len(log_msgs) > 5: log_msgs.pop(0)

# ── Gravação ──────────────────────────────────────────────

def iniciar_gravacao():
    global proc, faixa, estado
    if estado in ("GRAVANDO", "AGUARDANDO", "FINALIZANDO", "CONCLUIDO"): return
    faixa += 1
    estado = "AGUARDANDO"
    log(f"Faixa {faixa:02d} aguardando...")

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

def pausar():
    global proc, estado
    if estado not in ("GRAVANDO", "AGUARDANDO"): return
    estado = "PAUSADO"
    log(f"Faixa {faixa:02d} fechando...")
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

# ── Blink ─────────────────────────────────────────────────def blink_loop():
    global blink
    while running:
        blink = not blink
        time.sleep(0.55)

# ── UI ────────────────────────────────────────────────────
def aguardando_str():
    return "GRAVANDO"

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

    # titulo
    title = " CD RECORDER "
    gap   = W - len(title) - 2
    L.append(DM + "║" + R + B + CYAN + title + R + " " * max(1, gap) + " " + DM + "║" + R)

    L.append(sep_line())

    # estado
    estado_map = {
        "PRONTO":      ("PRONTO",         WHITE,  False),
        "AGUARDANDO":  (aguardando_str(), RED,    True),
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
    L.append(border(""))

    # log
    for i in range(4):
        msg = log_msgs[i] if i < len(log_msgs) else ""
        msg = msg[:W-2]
        L.append(DM + "║  " + R + DM + msg.ljust(W-3) + R + DM + "║" + R)

    # preenche ate menu
    used = len(L)
    menu_pos = rows - 3
    for _ in range(max(0, menu_pos - used - 1)):
        L.append(border(""))

    L.append(sep_line())

    # menu — só uma linha
    itens = [
        (" 1 GRAVAR ",    RED),
        (" 2 PAUSAR ",    YELLOW),
        (" 3 FINALIZAR ", CYAN),
    ]
    ms = ""; mp = ""
    for txt, cor in itens:
        ms += B + RV + cor + txt + R + "  "
        mp += txt + "  "
    pad = max(0, (W - len(mp)) // 2)
    L.append(DM + "║" + R + " " * pad + ms + " " * max(0, W - len(mp) - pad) + DM + "║" + R)

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
    global running
    time.sleep(2)
    os.environ.setdefault("TERM", "linux")
    os.environ.setdefault("PULSE_RUNTIME_PATH", f"/run/user/{os.getuid()}/pulse")
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    os.environ["PULSE_SOURCE"] = PULSE_SOURCE
    hide_cursor()
    cls()
    log("Pronto para gravar!")

    threading.Thread(target=blink_loop, daemon=True).start()

    ACOES = {
        '1': iniciar_gravacao,
        '2': pausar,
        '3': finalizar,
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
