# 🎙️ CD Recorder — Gravador de CD em Linux Headless

Clone funcional de gravador de CD dedicado (inspirado no Sony RCD-W500C), rodando em Ubuntu Server headless com TUI em ANSI puro. Grava áudio do LINE IN diretamente em CD-R via `wodim`, sem interface gráfica, sem curses, sem dependências pesadas.

---

## Como funciona

O sinal de áudio entra pela entrada LINE IN da placa de som, é capturado pelo PulseAudio via `parec` e jogado diretamente no `wodim`, que grava o stream PCM no CD em tempo real. Cada faixa é uma sessão TAO (Track At Once). O disco só é finalizado quando você manda — enquanto aberto, é possível gravar múltiplas faixas.

```
LINE IN → parec → wodim → CD-R
```

O VU meter lê o RMS do mesmo source via `sox` em paralelo, em ciclos de 0.4s, sem interferir na gravação.

---

## Requisitos

### Hardware
- PC com placa de som integrada com entrada LINE IN
- Gravadora de CD **SATA** (gravadoras USB são problemáticas com `wodim`)
- Cabo P2 ligado à fonte de áudio (aparelho de som, mixer, etc.)

### Software
```bash
sudo apt install wodim alsa-utils pulseaudio pulseaudio-utils sox python3 -y
```

> `sounddevice` e `numpy` **não são necessários** — o RMS é calculado via `parec + sox`.

---

## Instalação

```bash
sudo cp cd-recorder.py /usr/local/bin/recorder.py
sudo chmod +x /usr/local/bin/recorder.py
```

### Auto-login no TTY1 (opcional, para uso headless)

```bash
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d/
sudo nano /etc/systemd/system/getty@tty1.service.d/autologin.conf
```

```ini
[Service]
ExecStart=
ExecStart=-/sbin/agetty --noissue --autologin SEU_USUARIO %I $TERM
Type=idle
```

### `~/.bash_profile`

```bash
export PULSE_RUNTIME_PATH=/run/user/1000/pulse

# Restaura mixer: tudo no max, mic zerado, input em Line
amixer -c 1 scontrols | grep -o "'[^']*'" | xargs -I{} amixer -c 1 sset {} 100% 2>/dev/null
amixer -c 1 sset 'Front Mic' 0% 2>/dev/null
amixer -c 1 sset 'Rear Mic' 0% 2>/dev/null
amixer -c 1 sset 'Line Boost' 0% 2>/dev/null
amixer -c 1 sset 'Capture' 50%
amixer -c 1 sset 'Input Source' 'Line'

sleep 3

if [ "$(tty)" = "/dev/tty1" ]; then
    sleep 1
    TERM=linux python3 /usr/local/bin/recorder.py
fi
```

### `~/.config/pulse/default.pa`

```bash
mkdir -p ~/.config/pulse
echo '.include /etc/pulse/default.pa
set-default-sink alsa_output.pci-0000_0a_00.4.analog-stereo' > ~/.config/pulse/default.pa
```

> Substitua o device pelo seu — use `pactl list short sinks` para descobrir.

### Desabilitar suspend do PulseAudio

Em `/etc/pulse/default.pa`, comente a linha:

```
#load-module module-suspend-on-idle
```

---

## Detecção automática de devices

O script detecta automaticamente o sink analógico e o source LINE IN via `pactl` na inicialização — sem necessidade de hardcodar device names. Funciona em qualquer PC com placa de som integrada, desde que o PulseAudio esteja rodando.

Se preferir fixar manualmente, edite no topo do script:

```python
CD_DEV        = "/dev/sr0"
SOURCE_LINEIN  = "alsa_input.pci-XXXX_XX_XX.X.analog-stereo"
SINK_ANALOG    = "alsa_output.pci-XXXX_XX_XX.X.analog-stereo"
```

---

## Interface

```
╔══════════════════════════════════════════════════╗
║  CD RECORDER                    LINEIN  SYNC OFF ║
╠══════════════════════════════════════════════════╣
║                    GRAVANDO                      ║  ← pisca vermelho
║                                                  ║
║                    FAIXA  02                     ║
║                                                  ║
║  RMS ████████████████░░░░░░░░░░░░░░░░░░░░░░░░░  ║  ← VU meter
║                                                  ║
║  13:42:01  Faixa 02 gravando                     ║
║  13:41:58  Faixa 01 fechada                      ║
╠══════════════════════════════════════════════════╣
║         1 GRAVAR    2 PAUSAR    3 FINALIZAR      ║
║            4 INPUT    5 SYNC    0 SAIR           ║
╚══════════════════════════════════════════════════╝
```

### Teclas

| Tecla | Ação |
|-------|------|
| `1` | Inicia gravação de nova faixa |
| `2` | Fecha a faixa atual (pausa) |
| `3` | Finaliza o disco (duplo toque para confirmar) |
| `4` | Alterna input: LINE IN / DESKTOP (monitor PulseAudio) |
| `5` | Liga/desliga SYNC REC |
| `0` / `Ctrl+C` | Sai do programa |

---

## Fluxo de gravação

```
1. Insere CD-R virgem
2. Pressiona 1 → GRAVANDO pisca
3. Dá play na fonte de áudio
4. Pressiona 2 → fecha a faixa, wodim grava silêncio de separação
5. Repete para cada faixa
6. Pressiona 3 → 3 novamente → disco finalizado
```

> Durante o AGUARDANDO (3s antes de abrir o pipe), o wodim inicializa e calibra o laser. O áudio é bufferizado — nada é perdido.

---

## SYNC REC

Com SYNC ativo (tecla `5`), o script monitora o RMS continuamente:

- **Em PAUSADO:** se detectar som acima do threshold por 0.5s → inicia gravação automaticamente
- **Em GRAVANDO:** se detectar silêncio por 2s → fecha a faixa automaticamente

Útil para gravar faces de LP ou fitas sem precisar apertar teclas.

Thresholds configuráveis no topo do script:

```python
SILENCE_THRESHOLD = 0.005
SILENCE_DURATION  = 2.0
SOUND_THRESHOLD   = 0.01
SOUND_DURATION    = 0.5
```

---

## VU Meter

O RMS é amostrado a cada 0.4s via:

```bash
parec --device={SOURCE_LINEIN} ... | sox -t raw ... -n trim 0 0.4 stat
```

Cores do medidor:

- 🟢 Verde — sinal normal (RMS < 0.1)
- 🟡 Amarelo — sinal alto (RMS 0.1–0.2)
- 🔴 Vermelho — saturando (RMS > 0.2)

Se o VU meter aparecer sempre vermelho com volume normal, reduza o Capture no alsamixer:

```bash
amixer -c 1 sset 'Capture' 50%
```

---

## Notas técnicas

- Gravadoras USB **não funcionam** de forma confiável com `wodim` — use SATA
- O `module-suspend-on-idle` do PulseAudio deve estar desabilitado, caso contrário o device suspende e o RMS vai a zero
- O script usa `os._exit(0)` no encerramento para evitar hang causado por threads de subprocesso
- Em sistemas com múltiplas placas de som, o card analógico pode ser `-c 1` em vez de `-c 0` no `amixer`
- CD-R gravado em TAO pode não ser lido por leitoras USB baratas — leitoras SATA leem normalmente

---

## Dependências

| Pacote | Uso |
|--------|-----|
| `wodim` | Gravação do CD |
| `parec` (pulseaudio-utils) | Captura de áudio do PulseAudio |
| `sox` | Análise RMS do stream de áudio |
| `alsa-utils` | Configuração do mixer (amixer) |
| `python3` | Runtime do script |

---

## Licença

MIT
