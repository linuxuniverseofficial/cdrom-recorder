#!/usr/bin/env python3
"""
CD RECORDER — UI em ANSI puro, sem curses.
Funciona em qualquer TTY Linux headless.
RMS via sounddevice + PULSE_SOURCE (monitor loopback).
"""
import sys, os, time, signal, subprocess, threading, re, tty, termios, select
import numpy as np

# ── Dispositivos ──────────────────────────────────────────
CD_DEV        = "/dev/sr0"
SOURCE_DESKTOP = "alsa_output.pci-0000_00_1b.0.analog-stereo.monitor"
SOURCE_LINEIN  = "alsa_input.pci-0000_00_1b.0.analog-stereo"
PULSE_SOURCE   = SOURCE_LINEIN  # default
input_modo     = "LINEIN"

# ── Thresholds SYNC ───────────────────────────────────────
SILENCE_THRESHOLD = 0.005
SILENCE_DURATION  = 2.0
SOUND_THRESHOLD   = 0.01
SOUND_DURATION    = 0.5

# ── Estado global ─────────────────────────────────────────
proc           = None
faixa          = 0
estado         = "PRONTO"
sync_ativo     = False
ultimo_rms     = 0.0
log_msgs       = []
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

R  = ansi(0)
B  = ansi(1)
DM = ansi(2)
RV = ansi(7)

RED     = ansi(31); GREEN   = ansi(32); YELLOW = ansi(33)
CYAN    = ansi(36); WHITE   = ansi(37); MAGENTA = ansi(35)

# ── Utilitários ───────────────────────────────────────────
def term_size():
    try:    return os.get_terminal_size()
    except: return os.terminal_size((80, 24))

def log(msg):
    ts = time.strftime("%H:%M:%S")
    log_msgs.append(f"{ts}  {msg}")
    if len(log_msgs) > 5: log_msgs.pop(0)

# ── Input mode ───────────────────────────────────────────
def toggle_input():
    global PULSE_SOURCE, input_modo
    if input_modo == "DESKTOP":
        input_modo   = "LINEIN"
        PULSE_SOURCE = SOURCE_LINEIN
    else:
        input_modo   = "DESKTOP"
        PULSE_SOURCE = SOURCE_DESKTOP
    os.environ["PULSE_SOURCE"] = PULSE_SOURCE
    log(f"Input: {input_modo}")

# ── Audio source ──────────────────────────────────────────
def audio_cmd_capture():
    return f"parec --device={PULSE_SOURCE} --format=s16le --rate=44100 --channels=2"

# ── Acoes de gravacao ─────────────────────────────────────
def iniciar_gravacao(auto=False):
    global proc, faixa, estado, aguardando_desde
    if estado in ("GRAVANDO", "AGUARDANDO", "FINALIZANDO", "CONCLUIDO"): return
    faixa += 1
    estado = "AGUARDANDO"
    aguardando_desde = time.time()
    log(f"{'AUTO' if auto else 'MANUAL'} -- faixa {faixa:02d} aguardando...")

    def _iniciar():
        global proc, estado, aguardando_desde
        time.sleep(3)
        if estado != "AGUARDANDO": return
        cmd = f"{audio_cmd_capture()} | wodim dev={CD_DEV} speed=1 -tao -audio -swab -"
        p = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        proc = p
        with open("/tmp/recorder.pid", "w") as f:
            f.write(str(os.getpgid(p.pid)))
        for linha in p.stderr:
            txt = linha.decode(errors="ignore").lower()
            if "starting" in txt or "write" in txt or "tao" in txt:
                estado = "GRAVANDO"
                aguardando_desde = None
                log(f"Faixa {faixa:02d} gravando — play em 3s...")
                _play_faixa(faixa_src)
                break
        if estado == "AGUARDANDO":
            estado = "GRAVANDO"
            aguardando_desde = None
            _play_faixa(faixa_src)

    threading.Thread(target=_iniciar, daemon=True).start()

def pausar(auto=False):
    global proc, estado
    if estado not in ("GRAVANDO", "AGUARDANDO"): return
    estado = "PAUSADO"
    log(f"{'AUTO' if auto else 'MANUAL'} -- faixa {faixa:02d} fechando...")
    _proc = proc  # captura referencia local antes de zerar
    def _matar():
        global proc
        if _proc:
            try: os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
            except: pass
        proc = None  # zera imediatamente
        subprocess.run(["pkill", "-TERM", "parec"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            result = subprocess.run(["pgrep", "wodim"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode != 0:
                break
            time.sleep(0.1)
        try: os.remove("/tmp/recorder.pid")
        except: pass
        log(f"Faixa {faixa:02d} fechada")
    threading.Thread(target=_matar, daemon=True).start()


def finalizar():
    global proc, estado, finalizar_pendente
    if not finalizar_pendente:
        finalizar_pendente = True
        log("Confirme: pressione 3 novamente para finalizar!")
        return
    finalizar_pendente = False
    if estado == "GRAVANDO" or estado == "AGUARDANDO":
        pausar()
        time.sleep(1)
    estado = "FINALIZANDO"; log("Finalizando disco...")
    if proc:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except: pass
        proc = None
    def _fix():
        global estado
        subprocess.run(["wodim", f"dev={CD_DEV}", "-fix"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        estado = "CONCLUIDO"; log("Disco finalizado!")
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
        subprocess.run(["eject", "-t", CD_DEV], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bandeja_aberta = False
    else:
        log("Ejetando disco...")
        subprocess.run(["eject", CD_DEV], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bandeja_aberta = True

# ── Play/Pause/Faixa do CD fonte (sr1) ───────────────────
proc_play = None
faixa_src = 1

def _matar_cvlc():
    global proc_play
    if proc_play and proc_play.poll() is None:
        try: os.killpg(os.getpgid(proc_play.pid), signal.SIGTERM)
        except: pass
        time.sleep(0.3)
        try: os.killpg(os.getpgid(proc_play.pid), signal.SIGKILL)
        except: pass
    subprocess.run(["pkill", "-9", "vlc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc_play = None

def _play_faixa(n):
    global proc_play, faixa_src
    if proc_play and proc_play.poll() is None:
        try: os.killpg(os.getpgid(proc_play.pid), signal.SIGTERM)
        except: pass
        proc_play = None
    faixa_src = max(1, n)
    cmd = f"sleep 3 && cvlc --no-video --cdda-track={faixa_src} --play-and-stop cdda:///dev/sr1 2>/dev/null"
    proc_play = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"CD faixa {faixa_src:02d} em 3s...")

def toggle_play():
    if proc_play and proc_play.poll() is None:
        _matar_cvlc()
        log("CD fonte pausado")
    else:
        _play_faixa(faixa_src)

def proxima_faixa():  _play_faixa(faixa_src + 1)
def faixa_anterior(): _play_faixa(max(1, faixa_src - 1))

def abrir_alsamixer():
    import pty, select as sel
    show_cursor()
    master, slave = pty.openpty()
    p = subprocess.Popen(["alsamixer"], stdin=slave, stdout=sys.stdout, stderr=sys.stderr)
    os.close(slave)
    fd = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while p.poll() is None:
            r, _, _ = sel.select([fd], [], [], 0.05)
            if r:
                ch = os.read(fd, 1)
                if ch == b'*':
                    os.write(master, b'\x1b')
                else:
                    os.write(master, ch)
    except Exception:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        try: os.close(master)
        except: pass
        try: p.terminate()
        except: pass
    hide_cursor()
    cls()

# ── Monitor RMS via sounddevice ───────────────────────────
def monitor_rms():
    global ultimo_rms
    import sounddevice as sd

    silencio_desde = som_desde = None
    buf = []
    ultimo_source = None

    def callback(indata, frames, t, status):
        buf.append(float(np.sqrt(np.mean(indata**2))))

    def _find_device():
        """Busca device de input por nome — índice muda a cada boot."""
        for i, d in enumerate(sd.query_devices()):
            if d['max_input_channels'] >= 2 and 'VT1705CF' in d['name']:
                return i
        return None

    while running:
        try:
            # reabre stream se source mudou
            if PULSE_SOURCE != ultimo_source:
                ultimo_source = PULSE_SOURCE
                os.environ["PULSE_SOURCE"] = PULSE_SOURCE

            dev = _find_device()
            if dev is None:
                log("RMS: device nao encontrado, tentando em 3s...")
                time.sleep(3)
                continue

            with sd.InputStream(device=dev, channels=2, samplerate=44100,
                                blocksize=4096, callback=callback):
                while running and PULSE_SOURCE == ultimo_source:
                    time.sleep(0.3)
                    if buf:
                        rms = max(buf)
                        buf.clear()
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
        except Exception as e:
            log(f"RMS erro: {e}")
            time.sleep(3)

# ── Blink ─────────────────────────────────────────────────
def blink_loop():
    global blink
    while running:
        blink = not blink
        time.sleep(0.55)

# ── UI ────────────────────────────────────────────────────
aguardando_desde = None

def aguardando_str():
    if aguardando_desde is None: return "AGUARDANDO..."
    secs = int(time.time() - aguardando_desde)
    return f"AGUARDANDO  {secs:02d}s"

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

    # topo
    L.append(DM + "╔" + "═" * W + "╗" + R)

    # titulo + input + sync
    title      = " CD RECORDER "
    input_plain = f" {input_modo} "
    input_col   = B + YELLOW + input_plain + R if input_modo == "LINEIN" else DM + input_plain + R
    sync_plain  = "SYNC ON " if sync_ativo else "SYNC OFF"
    sync_col    = B + GREEN + sync_plain + R if sync_ativo else DM + sync_plain + R
    title_col   = B + CYAN + title + R
    gap = W - len(title) - len(input_plain) - len(sync_plain) - 3
    L.append(DM + "║" + R + title_col + " " * max(1, gap) + input_col + " " + sync_col + " " + DM + "║" + R)

    L.append(sep_line())

    # estado
    estado_map = {
        "PRONTO":      ("PRONTO",        WHITE,  False),
        "AGUARDANDO":  ("GRAVANDO",        RED,    True),
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

    # preenche ate o menu
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
        (" 4 INPUT ",      YELLOW),
        (" 5 PLAY/PAUSE ", GREEN),
        (" 6 SYNC ",       MAGENTA),
        (" 7 MIXER ",      WHITE),
        (" + PROXIMA ",    CYAN),
        (" - ANTERIOR ",   CYAN),
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
    global running, faixa, estado
    time.sleep(2)
    os.environ.setdefault("TERM", "linux")
    os.environ.setdefault("PULSE_RUNTIME_PATH", f"/run/user/{os.getuid()}/pulse")
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    os.environ["PULSE_SOURCE"] = PULSE_SOURCE
    hide_cursor()
    cls()

    log("Pronto para gravar!")

    threading.Thread(target=blink_loop,  daemon=True).start()
    threading.Thread(target=monitor_rms, daemon=True).start()

    ACOES = {
        '1': lambda: iniciar_gravacao(False),
        '2': lambda: pausar(False),
        '3': finalizar,
        '4': toggle_input,
        '5': toggle_play,
        '6': toggle_sync,
        '7': abrir_alsamixer,
        '+': proxima_faixa,
        '-': faixa_anterior,
    }

    try:
        global finalizar_pendente
        while True:
            draw_ui()
            key = get_key()
            if key in ('0', '\x03', '\r'): break
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
        if proc_play and proc_play.poll() is None:
            try: os.killpg(os.getpgid(proc_play.pid), signal.SIGTERM)
            except: pass
        print("Ate logo.")
        os._exit(0)

if __name__ == "__main__":
    main()
