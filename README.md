# CD RECORDER — Setup Guide

## Dependências

```bash
sudo apt install wodim alsa-utils pulseaudio pulseaudio-utils cdparanoia eject vlc libportaudio2 python3-pip -y
pip install sounddevice numpy --break-system-packages
```

## Autologin TTY1

```bash
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d/
sudo nano /etc/systemd/system/getty@tty1.service.d/autologin.conf
```

```ini
[Service]
ExecStart=
ExecStart=-/sbin/agetty --noissue --autologin administrador %I $TERM
Type=idle
```

## ~/.bash_profile

```bash
amixer -c 0 scontrols | grep -o "'[^']*'" | xargs -I{} amixer -c 0 sset {} unmute 2>/dev/null
amixer -c 0 sset 'Input Source' 'Line'
sleep 3
if [ "$(tty)" = "/dev/tty1" ]; then
    sleep 1
    TERM=linux python3 /usr/local/bin/recorder.py
fi
```

## PulseAudio — Default Sink

```bash
mkdir -p ~/.config/pulse
echo '.include /etc/pulse/default.pa
set-default-sink alsa_output.pci-0000_00_1b.0.analog-stereo' > ~/.config/pulse/default.pa
```

> ⚠️ NÃO adicionar set-source-port — causa conflito no line-in

## ALSA — Salvar configurações

```bash
# No alsamixer: F4 Capture → Input Source → Line → Space para selecionar
# Desmutar todos os canais
alsamixer
sudo alsactl store
```

## Fonte do TTY (framebuffer only)

```bash
sudo dpkg-reconfigure console-setup
# UTF-8 → Guess Optimal → Terminus Bold → 12x24
```

> Só afeta TTY físico (HDMI). SSH e Desktop não são afetados.

## Hardware

- Gravação: `/dev/sr0` (USB CD drive — disco em branco)
- Fonte CD: `/dev/sr1` (USB CD drive — CD original)
- Input áudio: Line-in azul (traseiro)
- Output áudio: P2 verde (traseiro)
- Placa: HDA Intel PCH VIA VT1705CF

## Observações

- Drive GH24NSB0: ~80s de aquecimento por faixa (OPC) — normal, wodim faz buffer
- Disco não finalizado: TOC ilegível — comportamento normal do drive
- Input Source volta para Front Mic após reiniciar PulseAudio — corrigido pelo bash_profile
- Índice dos devices ALSA muda a cada boot — código busca por nome (VT1705CF)
