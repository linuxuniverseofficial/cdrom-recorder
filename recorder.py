#!/usr/bin/env python3
import curses
import subprocess
import signal
import os
import time
import threading
import re

# ── Dispositivos ──────────────────────────────────────────
CD_DEV       = "/dev/sr0"       # gravadora
LINEIN_DEV   = "hw:1,0"         # placa de som RCA/line-in
SINK_MONITOR = "$(pactl get-default-sink).monitor"  # loopback desktop

# ── Control files ─────────────────────────────────────────
# Crie um desses arquivos externamente para forçar o modo:
#   touch /tmp/cdrom    → captura do CD-ROM (sr1)
#   touch /tmp/linein   → captura do line-in analógico
#   (nenhum)            → captura do desktop (loopback)
CTRL_CDROM  = "/tmp/cdrom"
CTRL_LINEIN = "/tmp/linein"

# ── Modos de input ────────────────────────────────────────
# DESKTOP → loopback PulseAudio (browser, player, etc)
# LINEIN  → placa de som analógica RCA
# CDROM   → leitura direta do segundo drive (sr1)
MODOS = ["DESKTOP", "LINEIN", "CDROM"]

# ── Thresholds de silêncio ────────────────────────────────
SILENCE_THRESHOLD = 0.002
SILENCE_DURATION  = 2.0
SOUND_THRESHOLD   = 0.005
SOUND_DURATION    = 0.5

# ── Estado global ─────────────────────────────────────────
proc       = None
faixa      = 0
estado     = "PRONTO"
blink      = False
sync_ativo = False
ultimo_rms = 0.0
log_msgs   = []
modo_idx   = 0          # índice em MODOS

def log(msg):
    ts = time.strftime("%H:%M:%S")
    log_msgs.append(f"{ts}  {msg}")
    if len(log_msgs) > 6:
        log_msgs.pop(0)

# ── Resolução do modo atual ───────────────────────────────
def modo_atual():
    """
    Prioridade:
      1. Control file /tmp/cdrom  → CDROM
      2. Control file /tmp/linein → LINEIN
      3. modo_idx (toggle pelo teclado)
    """
    if os.path.exists(CTRL_CDROM):
        return "CDROM"
    if os.path.exists(CTRL_LINEIN):
        return "LINEIN"
    return MODOS[modo_idx]

def audio_cmd_capture():
    """Retorna o comando de captura de áudio conforme o modo."""
    modo = modo_atual()
    if modo == "CDROM":
        # lê áudio digital direto do segundo drive
        return f"cdparanoia -d /dev/sr1 -B - 2>/dev/null"
    elif modo == "LINEIN":
        return (
            f"arecord -D {LINEIN_DEV} -f cd -t raw"
        )
    else:  # DESKTOP
        return (
            f"parec --device={SINK_MONITOR} "
            f"--format=s16le --rate=44100 --channels=2"
        )

def audio_cmd_monitor_rms():
    """Retorna o comando de captura para análise RMS (sox)."""
    modo = modo_atual()
    if modo == "LINEIN":
        return (
            f"arecord -D {LINEIN_DEV} -f cd -t raw --latency-time=500000"
        )
    else:  # DESKTOP ou CDROM — usa loopback para monitorar
        return (
            f"parec --device={SINK_MONITOR} "
            f"--format=s16le --rate=44100 --channels=2 --latency-msec=500"
        )

def toggle_modo():
    global modo_idx
    # só avança pelo toggle se não houver control file ativo
    if os.path.exists(CTRL_CDROM) or os.path.exists(CTRL_LINEIN):
        log("Control file ativo — remova /tmp/cdrom ou /tmp/linein primeiro")
        return
    modo_idx = (modo_idx + 1) % len(MODOS)
    log(f"Input → {MODOS[modo_idx]}")

# ── Gravação ──────────────────────────────────────────────
def iniciar_gravacao(auto=False):
    global proc, faixa, estado
    if estado in ("FINALIZANDO", "CONCLUIDO"):
        return
    faixa += 1
    estado = "GRAVANDO"
    cap = audio_cmd_capture()
    cmd = f"{cap} | cdrecord dev={CD_DEV} -tao -audio -swab -"
    proc = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
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
    subprocess.run(
        ["cdrecord", f"dev={CD_DEV}", "-fix"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    estado = "CONCLUIDO"
    log("✔ Disco finalizado!")

def toggle_sync():
    global sync_ativo
    sync_ativo = not sync_ativo
    log(f"SYNC REC {'ATIVADO' if sync_ativo else 'DESATIVADO'}")

# ── Monitor de RMS via sox ────────────────────────────────
def monitor_rms():
    global ultimo_rms, sync_ativo
    silencio_desde = None
    som_desde      = None

    while True:
        if not sync_ativo:
            time.sleep(0.3)
            silencio_desde = None
            som_desde      = None
            continue
        try:
            cap = audio_cmd_monitor_rms()
            result = subprocess.run(
                f"{cap} | sox -t raw -r 44100 -c 2 -b 16 -e signed - "
                f"-n trim 0 0.4 stat 2>&1",
                shell=True, capture_output=True, text=True, timeout=2
            )
            output = result.stdout + result.stderr
            rms = 0.0
            for linha in output.splitlines():
                if "RMS amplitude" in linha:
                    m = re.search(r"[\d.]+$", linha)
                    if m:
                        rms = float(m.group())
                        break
            ultimo_rms = rms
            agora = time.time()

            if estado == "GRAVANDO":
                if rms < SILENCE_THRESHOLD:
                    if silencio_desde is None:
                        silencio_desde = agora
                    elif agora - silencio_desde >= SILENCE_DURATION:
                        pausar(auto=True)
                        silencio_desde = None
                        som_desde      = None
                else:
                    silencio_desde = None

            elif estado == "PAUSADO":
                if rms > SOUND_THRESHOLD:
                    if som_desde is None:
                        som_desde = agora
                    elif agora - som_desde >= SOUND_DURATION:
                        iniciar_gravacao(auto=True)
                        som_desde      = None
                        silencio_desde = None
                else:
                    som_desde = None

        except Exception:
            time.sleep(0.5)

# ── Threads ───────────────────────────────────────────────
def blink_loop():
    global blink
    while True:
        blink = not blink
        time.sleep(0.55)

threading.Thread(target=blink_loop,  daemon=True).start()
threading.Thread(target=monitor_rms, daemon=True).start()

# ── Cores ─────────────────────────────────────────────────
C_RED     = 1
C_GREEN   = 2
C_YELLOW  = 3
C_WHITE   = 4
C_DIM     = 5
C_CYAN    = 6
C_MAGENTA = 7

# ── UI ────────────────────────────────────────────────────
def draw(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_RED,     curses.COLOR_RED,     -1)
    curses.init_pair(C_GREEN,   curses.COLOR_GREEN,   -1)
    curses.init_pair(C_YELLOW,  curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_WHITE,   curses.COLOR_WHITE,   -1)
    curses.init_pair(C_DIM,     curses.COLOR_BLACK,   -1)
    curses.init_pair(C_CYAN,    curses.COLOR_CYAN,    -1)
    curses.init_pair(C_MAGENTA, curses.COLOR_MAGENTA, -1)

    ACOES = {
        '2': lambda: iniciar_gravacao(False),
        '3': lambda: pausar(False),
        '4': continuar,
        '5': finalizar,
        '6': toggle_sync,
        '7': toggle_modo,
    }

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        mid  = w // 2

        # ── Borda ──
        stdscr.attron(curses.color_pair(C_DIM) | curses.A_DIM)
        stdscr.border()
        stdscr.attroff(curses.color_pair(C_DIM) | curses.A_DIM)

        # ── Título ──
        title = "▓▓  CD RECORDER  ▓▓"
        stdscr.attron(curses.color_pair(C_CYAN) | curses.A_BOLD)
        stdscr.addstr(2, mid - len(title)//2, title)
        stdscr.attroff(curses.color_pair(C_CYAN) | curses.A_BOLD)

        # ── SYNC badge ──
        sync_label = " SYNC " if sync_ativo else " SYNC OFF "
        sync_cor   = C_GREEN if sync_ativo else C_DIM
        stdscr.attron(curses.color_pair(sync_cor) | curses.A_REVERSE)
        stdscr.addstr(2, w - len(sync_label) - 3, sync_label)
        stdscr.attroff(curses.color_pair(sync_cor) | curses.A_REVERSE)

        # ── Separador ──
        stdscr.attron(curses.color_pair(C_DIM) | curses.A_DIM)
        stdscr.addstr(4, 2, "─" * (w - 4))
        stdscr.attroff(curses.color_pair(C_DIM) | curses.A_DIM)

        # ── Modo de input (linha abaixo do separador) ──
        modo = modo_atual()
        ctrl = ""
        if os.path.exists(CTRL_CDROM):
            ctrl = " [/tmp/cdrom]"
        elif os.path.exists(CTRL_LINEIN):
            ctrl = " [/tmp/linein]"
        modo_cor = {
            "DESKTOP": C_CYAN,
            "LINEIN":  C_YELLOW,
            "CDROM":   C_MAGENTA,
        }.get(modo, C_WHITE)
        modo_str = f"INPUT: {modo}{ctrl}"
        stdscr.attron(curses.color_pair(modo_cor) | curses.A_BOLD)
        stdscr.addstr(5, mid - len(modo_str)//2, modo_str)
        stdscr.attroff(curses.color_pair(modo_cor) | curses.A_BOLD)

        # ── Estado principal ──
        estado_cfg = {
            "PRONTO":      ("●  PRONTO",       C_WHITE,  False),
            "GRAVANDO":    ("⏺  GRAVANDO",      C_RED,    True),
            "PAUSADO":     ("⏸  PAUSADO",       C_YELLOW, True),
            "FINALIZANDO": ("⏳  FINALIZANDO…", C_YELLOW, False),
            "CONCLUIDO":   ("✔  CONCLUIDO",     C_GREEN,  False),
        }
        label, cor, pisca = estado_cfg.get(estado, ("?", C_WHITE, False))
        if (not pisca) or blink:
            stdscr.attron(curses.color_pair(cor) | curses.A_BOLD)
            stdscr.addstr(h//2 - 3, mid - len(label)//2, label)
            stdscr.attroff(curses.color_pair(cor) | curses.A_BOLD)

        # ── Faixa ──
        if faixa > 0:
            fs = f"FAIXA  {faixa:02d}"
            stdscr.attron(curses.color_pair(C_WHITE) | curses.A_BOLD)
            stdscr.addstr(h//2, mid - len(fs)//2, fs)
            stdscr.attroff(curses.color_pair(C_WHITE) | curses.A_BOLD)

        # ── VU meter ──
        vu_w    = min(40, w - 10)
        vu_fill = int(min(ultimo_rms / 0.1, 1.0) * vu_w)
        vu_bar  = "█" * vu_fill + "░" * (vu_w - vu_fill)
        vu_cor  = C_GREEN if ultimo_rms < 0.07 else C_YELLOW if ultimo_rms < 0.09 else C_RED
        vu_y    = h//2 + 2
        stdscr.attron(curses.color_pair(C_DIM))
        stdscr.addstr(vu_y, mid - vu_w//2 - 4, "RMS ")
        stdscr.attroff(curses.color_pair(C_DIM))
        stdscr.attron(curses.color_pair(vu_cor))
        stdscr.addstr(vu_y, mid - vu_w//2, vu_bar)
        stdscr.attroff(curses.color_pair(vu_cor))

        # ── Log de eventos ──
        log_y = h//2 + 4
        stdscr.attron(curses.color_pair(C_DIM) | curses.A_DIM)
        for i, msg in enumerate(log_msgs[-4:]):
            if log_y + i < h - 5:
                stdscr.addstr(log_y + i, 4, msg[:w - 8])
        stdscr.attroff(curses.color_pair(C_DIM) | curses.A_DIM)

        # ── Menu ──
        menu_y = h - 4
        stdscr.attron(curses.color_pair(C_DIM) | curses.A_DIM)
        stdscr.addstr(menu_y - 1, 2, "─" * (w - 4))
        stdscr.attroff(curses.color_pair(C_DIM) | curses.A_DIM)

        itens = [
            (" 2 GRAVAR ",    C_RED),
            (" 3 PAUSAR ",    C_YELLOW),
            (" 4 CONTINUAR ", C_GREEN),
            (" 5 FINALIZAR ", C_CYAN),
            (" 6 SYNC ",      C_MAGENTA),
            (" 7 INPUT ",     modo_cor),
            (" 0 SAIR ",      C_WHITE),
        ]
        x = 3
        for texto, cor in itens:
            if x + len(texto) >= w - 3:
                break
            stdscr.attron(curses.color_pair(cor) | curses.A_REVERSE)
            stdscr.addstr(menu_y, x, texto)
            stdscr.attroff(curses.color_pair(cor) | curses.A_REVERSE)
            x += len(texto) + 2

        stdscr.refresh()

        # ── Input ──
        try:
            key = stdscr.getkey()
        except:
            key = None

        if key == '0':
            if proc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except:
                    pass
            break
        if key in ACOES and estado not in ("FINALIZANDO", "CONCLUIDO"):
            ACOES[key]()

        time.sleep(0.05)

# ── Entry point ───────────────────────────────────────────
if __name__ == "__main__":
    try:
        curses.wrapper(draw)
    except KeyboardInterrupt:
        if proc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except:
                pass
    print("\nAté logo.")
