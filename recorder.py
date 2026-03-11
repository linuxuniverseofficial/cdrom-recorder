#!/usr/bin/env python3
"""
CD RECORDER — UI em ANSI puro, sem curses.
Funciona em qualquer TTY Linux headless.
RMS via sounddevice + PULSE_SOURCE (monitor loopback).
"""
import sys, os, time, signal, subprocess, threading, re, tty, termios, select
import numpy as np

# ── Dispositivos ──────────────────────────────────────────
CD_DEV       = "/dev/sr0"
PULSE_SOURCE = "alsa_output.pci-0000_00_1b.0.analog-stereo.monitor"

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
blink          = False
running        = True
bandeja_aberta = False

# ── Leitura do TOC do disco ───────────────────────────────
def ler_faixas_disco():
    try:
        r = subprocess.run(
            ["wodim", f"dev={CD_DEV}", "-toc"],
            capture_output=True, text=True, timeout=15
        )
        output = r.stdout + r.stderr
        faixas = 0
        for linha in output.splitlines():
            m = re.search("track:\\s*(\\d+)", linha, re.IGNORECASE)
            if m:
                n = int(m.group(1))
                if n < 170:
                    faixas = max(faixas, n)
        return faixas
    except Exception:
        return 0

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

# ── Audio source ──────────────────────────────────────────
def audio_cmd_capture():
    return "parec --device=alsa_output.pci-0000_00_1b.0.analog-stereo.monitor --format=s16le --rate=44100 --channels=2"

# ── Acoes de gravacao ─────────────────────────────────────
def iniciar_gravacao(auto=False):
    global proc, faixa, estado, aguardando_desde
    if estado in ("FINALIZANDO", "CONCLUIDO"): return
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
    estado = "PAUSADO"
    log(f"{'AUTO' if auto else 'MANUAL'} -- faixa {faixa:02d} fechando...")
    def _matar():
        if proc:
            try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except: pass
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

def continuar(): iniciar_gravacao(auto=False)

def finalizar():
    global proc, estado
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

    os.environ["PULSE_SOURCE"] = PULSE_SOURCE

    silencio_desde = som_desde = None
    buf = []

    def callback(indata, frames, t, status):
        buf.append(float(np.sqrt(np.mean(indata**2))))

    try:
        with sd.InputStream(device='pulse', channels=2, samplerate=44100,
                            blocksize=4096, callback=callback):
            while running:
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

    # titulo + sync
    title      = " CD RECORDER "
    sync_plain = "SYNC ON " if sync_ativo else "SYNC OFF"
    sync_col   = B + GREEN + sync_plain + R if sync_ativo else DM + sync_plain + R
    title_col  = B + CYAN + title + R
    gap = W - len(title) - len(sync_plain) - 2
    L.append(DM + "║" + R + title_col + " " * max(1, gap) + sync_col + " " + DM + "║" + R)

    L.append(sep_line())

    # estado
    estado_map = {
        "PRONTO":      ("PRONTO",        WHITE,  False),
        "AGUARDANDO":  (aguardando_str(), YELLOW, True),
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
        (" 2 GRAVAR ",    RED),
        (" 3 PAUSAR ",    YELLOW),
        (" 4 CONTINUAR ", GREEN),
        (" 5 FINALIZAR ", CYAN),
        (" 0 SAIR ",      WHITE),
    ]
    linha2 = [
        (" 9 PLAY/PAUSE ", GREEN),
        (" + PROXIMA ",    CYAN),
        (" - ANTERIOR ",   CYAN),
        (" 6 SYNC ",       MAGENTA),
        (" , MIXER ",      WHITE),
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

    log("Lendo disco...")
    faixas_existentes = ler_faixas_disco()
    if faixas_existentes > 0:
        faixa = faixas_existentes
        estado = "PAUSADO"
        log(f"Disco tem {faixas_existentes} faixa(s) — continuando")
    else:
        log("Disco vazio — pronto para gravar")

    threading.Thread(target=blink_loop,  daemon=True).start()
    threading.Thread(target=monitor_rms, daemon=True).start()

    ACOES = {
        '2': lambda: iniciar_gravacao(False),
        '3': lambda: pausar(False),
        '4': continuar,
        '5': finalizar,
        '6': toggle_sync,
        '8': toggle_bandeja,
        '9': toggle_play,
        '+': proxima_faixa,
        '-': faixa_anterior,
        ',': abrir_alsamixer,
    }

    try:
        while True:
            draw_ui()
            key = get_key()
            if key in ('0', '\x03', '\r'): break
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
        try:
            pid = int(open("/tmp/recorder.pid").read().strip())
            os.killpg(pid, signal.SIGTERM)
            os.remove("/tmp/recorder.pid")
        except: pass
        if proc_play and proc_play.poll() is None:
            try: os.killpg(os.getpgid(proc_play.pid), signal.SIGTERM)
            except: pass
        print("Ate logo.")

if __name__ == "__main__":
    main()
