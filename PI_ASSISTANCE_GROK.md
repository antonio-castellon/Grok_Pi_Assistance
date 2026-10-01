# Grok voice assistant — Raspberry Pi

This file is how to build the machine that is running now. The desktop tray app is a separate repository, [antonio-castellon/grok_assistant](https://github.com/antonio-castellon/grok_assistant). Do not follow that one for a Pi.

The live tree is `/home/antonio/grok-assistant`. The user is `antonio`. Spoken language is Spanish. Answers are short. Speech has no markdown, lists, emoji, or code.

The same tree is the private repository [antonio-castellon/Grok_Pi_Assistance](https://github.com/antonio-castellon/Grok_Pi_Assistance).

## Clean install

On a clean Raspberry Pi OS, clone that repository and run `./install-pi.sh` as the normal user, not as root. The script installs the packages, the virtualenv, the speech models, the Grok CLI binary when it is missing, and the systemd units for the user who runs it. It does not change HDMI, the screen, or the sound devices. Sign in to the Grok CLI on that machine. Do not put a token in the repository.

`setup.sh` only refreshes the virtualenv and the models. `install-pi.sh` is the full install and it calls `setup.sh`.

No password and no token belong in the repository. The spoken administrator key, if this machine has one, stays only in `~/.config/grok-assistant/config.json` on the device. Voice prints in `speakers.json` stay on the device. Model files are downloaded into `models/` and are not committed. Nemotron is not downloaded. Paths inside `assistant.py` follow the user who owns the tree.

## Hardware

The application does not configure HDMI, a panel, or a sound card. It uses what the operating system already delivers.

- Picture: framebuffer `fb0`.
- Sound out and sound in: the ALSA default devices (`playback_device` and `capture_device` are `auto`).
- Touch: only if the kernel already exposes an input device whose name contains “touchscreen”. The coordinates are the axis limits that kernel reports, drawn onto `fb0`.
- The on-screen temperature is `cpu-thermal` only. Do not show a touch-controller reading as a temperature.

## Operating system

Debian GNU/Linux 13 (trixie), kernel `6.18` Raspberry Pi `v8`, userland aarch64.

Packages beyond the base image:

- `python3` and `python3-venv` (the venv is Python 3.13)
- `alsa-utils`
- `mpv` (system package, used for music)
- `network-manager`
- `curl`

The Pi user must be in the groups `audio`, `video`, and `input`. The Wi-Fi boot service also needs `netdev`.

## Screen and sound

Raspberry Pi OS owns `/boot/firmware/config.txt`, HDMI, the panel, and the ALSA defaults. The assistant does not write that file, does not choose a sound card by name, and does not set mixer paths. Volume commands change the default playback control the OS already exposes (`Master`, `PCM`, `Speaker`, `Headphone`, or `Playback`), in percent.

## Tree

```
/home/antonio/grok-assistant/
  assistant.py          the running program
  wifi-boot.py          waits for the network, then starts the assistant
  config.json
  requirements.txt
  hellos-es.txt         180 boot lines, spoken in order
  hellos-en.txt
  waits-es.txt          166 short lines spoken while a cloud answer is in flight
  waits-en.txt
  grok-assistant.service
  wifi-boot.service
  install-pi.sh         full install on a clean Raspberry Pi OS
  setup.sh              virtualenv and speech models
  .venv/                sherpa-onnx 1.13.8, numpy, sentencepiece, pypinyin, yt-dlp
  models/               see below
```

`setup.sh` creates the virtualenv and downloads every model in the table below, plus the English keyword model, Moonshine tiny, and Piper Amy. The Spanish program does not use those English files unless `config.json` language is `en`. `install-pi.sh` calls `setup.sh` after the packages. It does not install a screen overlay or a sound overlay.

Grok CLI on this machine is `/home/antonio/.grok/bin/grok`, version `1.0.46`. `install-pi.sh` installs the current CLI from `https://x.ai/cli/install.sh` when `~/.grok/bin/grok` is missing. It must be logged in as the user who runs the assistant. There is no on-device Grok model. A network namespace without DNS cannot answer. The model name used by the assistant is `grok-4.7`. Reasoning effort is `low`, and `high` only when the user asks for more detail.

## Models that the listener actually loads

Put them under `models/` with these directory names.

| Role | Directory | Active now |
|---|---|---|
| Speech to text, streaming Spanish | `sherpa-onnx-streaming-zipformer-es-kroko-2025-08-06` | yes, id `kroko` |
| Speech to text, end of phrase | `sherpa-onnx-whisper-tiny` | selectable, “Whisper pequeño” |
| Speech to text, end of phrase | `sherpa-onnx-whisper-base` | selectable, “Whisper base” |
| Speech to text, end of phrase, en/es/de/fr | `sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8` | selectable, “Canary” |
| Voice prints | `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` | yes |
| Spoken voice 1 | `vits-piper-es_ES-davefx-medium-int8` | yes, “Dave, España” |
| Voices 2 and 3 | `vits-piper-es_ES-sharvard-medium-int8` | two speaker ids, 0 and 1 |
| Voice 4 | `vits-piper-es_ES-carlfm-x_low-int8` | |
| Voice 5 | `vits-piper-es_ES-glados-medium-int8` | |
| Voice 6 | `vits-piper-es_ES-miro-high-int8` | |
| Voice 7 | `vits-piper-es_MX-ald-medium-int8` | |
| Voice 8 | `vits-piper-es_MX-claude-high-int8` | |
| Voice 9 | `vits-piper-es_AR-daniela-high-int8` | |

Archives, from the sherpa-onnx `asr-models` and `tts-models` release assets:

- `sherpa-onnx-streaming-zipformer-es-kroko-2025-08-06.tar.bz2`
- `sherpa-onnx-whisper-tiny.tar.bz2`
- `sherpa-onnx-whisper-base.tar.bz2`
- `sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8.tar.bz2`
- `vits-piper-es_ES-davefx-medium-int8.tar.bz2` and the other Piper archives named like their directories

`sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11` is on this disk and is not in the recognizer menu. On this Pi its real-time factor was about 3 to 5, so it is not the live engine. Do not select it.

Threads for the recognizer are `num_threads` in `config.json`, currently 3. Piper loading uses at most 2.

## Voice footprint

This is part of the running Pi, not an optional extra.

- Model file: `models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`.
- Store: `/home/antonio/.config/grok-assistant/speakers.json`, mode 600.
- Each person has up to 12 prints. A phrase matches the closest one.
- `locked` in that file is the only name the assistant hears after enrollment. Until `identifica mi voz` has been completed once, `locked` is empty.

`comando identifica mi voz` is done by ear. The panel also shows `DI:` and `OI:` on the bottom line.

1. “Di tu nombre.” It waits.
2. Two words in a row, such as Jose Antonio, are one person.
3. It speaks the name it heard.
4. Same saved voice or same saved name: “Es la misma persona. ¿Repito su identificación?” A yes replaces that person’s prints. It does not create another person.
5. New name: “No está guardado. ¿Lo guardo como otra persona?” A no means “Di otro nombre.”
6. Only a yes keeps the name. That name is written to `locked`.
7. It speaks four phrases, three times each, and waits for the repeat after every one: `hola grok`, `estás ahí`, `qué hora es`, `pon una canción`. Repeats start with “Otra vez.”
8. `salir` cancels. Fewer than 3 good takes are not saved.

A screen label such as `voz 78` is not a stored person. It is a temporary lane. This machine has one enrolled name in `speakers.json`: Antonio, with 12 prints, and `locked` is Antonio. An unidentified lane is deleted after 10 seconds without use and is not saved.

After at least one person has finished `identifica mi voz`, only enrolled voices are heard: wake, commands, yes or no, and the open conversation. A print counts when it is within 0.30 of Antonio’s closest saved take, or the lane is already named Antonio. That name check also applies to a live wake line, before the phrase has finished. A lower score is ignored. The process does not quit when that happens. Anyone else is ignored. The exception is `comando identifica mi voz`: any person may say it, otherwise a new voice could never be enrolled. Until the first enrollment, no voice is locked.

In administrator mode only: `lista las personas` speaks the names. `borra` plus the name asks sí or no and deletes that person and their prints. If the deleted name is `locked`, the lock is cleared.

## config.json

```
language                     es
admin_key                    empty in the repository
playback_device              auto
capture_device               auto
silence_seconds              3.5
max_utterance_seconds        45
idle_seconds                 60
min_speech_seconds           0.35
speech_rms_min               0.004
grok_timeout_seconds         120
tts_speed                    1.0
num_threads                  3
reasoning_effort             low
```

`admin_key` in the repository is empty. A spoken administrator key, when one is used, is written only on the machine in `~/.config/grok-assistant/config.json`. That file is not part of the repository. One wrong digit still matches. With an empty key, administrator mode does not open. Do not put a sudo password or an API token in the repository or in `config.json`.

`auto` means the ALSA default playback device and the ALSA default capture device, the ones the operating system already selected.

## State files

All under `/home/antonio/.config/grok-assistant/`:

| File | Meaning now |
|---|---|
| `asr-index` | `kroko` |
| `voice-index` | `0` (Dave, España) |
| `hello-index` | next boot line in `hellos-es.txt` |
| `wait-index` | next waiting line in `waits-es.txt` |
| `volume` | Playback level in percent, 0–100, on the OS default mixer |
| `speakers.json` | mode 600. People, up to 12 prints each, the assistant’s own print, and `locked`, the only name that may be heard after enrollment |
| `active-agent` | path of the Grok agent in use, if any |
| `sessions` | local sessions, mode 600 |

Named sessions do not expire. The shared session lasts 24 hours and then a new id is created. Do not delete `~/.grok/sessions/` for the coding session. Cleanup of orphan voice sessions must stay inside the voice working directory.

Agents are markdown files in `~/.grok/agents/<name>.md`. Creating one asks for the spoken admin key. Opening one passes `--agent` to the Grok CLI. `cerrar agente` leaves the agent and stays in the talk. `cierra conversación` ends the talk and does not change the session.

## Boot order

1. The operating system brings up HDMI or the panel, and the default sound devices, before this application starts. Nothing in this project does that.
2. `wifi-boot.service` is enabled. It runs `/usr/bin/python3 wifi-boot.py` as the Pi user. It does not import sherpa. It waits about 16 seconds. If the network is up (`curl` to a generate-204 check, then ping `1.1.1.1`), it starts `grok-assistant.service` and exits. If not, it draws on `fb0` and connects with `nmcli`.
3. `grok-assistant.service` is installed with `Restart=always` and `RestartSec=5`, but it is disabled at boot. Wi-Fi boot starts it. The process is `assistant.py run`.

Restart the assistant with the operating system's sudo:

```
sudo systemctl restart grok-assistant
```

Do not `pkill` a pattern that matches the command line of the shell you are in. The first listening line in the journal appears about 18 seconds after the restart, once the models are loaded.

## Screen

`panel_fb()` is `fb0`, the framebuffer the operating system assigned to the console. The size and stride come from that framebuffer. The application does not look for a named panel and does not apply its own rotation.

Touch uses the minimum and maximum the kernel reports for X and Y, mapped onto that framebuffer. Music pause and stop are the top-left buttons of whatever picture the OS is showing.

The picture is: logo, then a row with volume, the recognizer name, and right-aligned `MEM`, `CPU`, `DISCO`, and `TEMP`. Temperature is only `cpu-thermal`, shown as `53°`. Then the voice row and, on the right, Wi-Fi status from `iw dev wlan0 link` and `ip -4 addr`. Then the command list. The heard line is at the bottom.

Spanish glyphs come from the PSF unicode table. A Latin-15 identity map draws the wrong accents. Do not replace that loader.

While music is audible the microphone is off. The buttons are `PAUSA` / `SEGUIR` and `STOP`. Pausing or stopping turns the microphone back on. A logo tap must not turn the microphone on while the song is still audible. Ducking for a spoken reply must not resume a song the user already paused.

## What the running program does

The rules of the talk below are what this Pi runs. They are implemented in `assistant.py`, not in a tray app. The desktop tray app is the separate repository `grok_assistant`.

Wake, with no cloud call and no search: `hola grok` (and close mishearings such as `hola grok` cut short, `hola grop`, `pola grove`), a short `hola` on its own, or `¿estás ahí, Grok?` / `Grok, ¿estás ahí?`. The answer is `Hola.` or `Sí, aquí estoy.` The words that came with the wake are not sent anywhere. The assistant then waits.

Every other order starts with the word `comando`. The screen says that once, on the `COMANDOS` line. The lines under it do not repeat the word. If the rest of the phrase is not an exact local command, it goes to the Grok interpreter. A repaired command is spoken back as `Has dicho: …. ¿Sí o no?` before it runs. `sí` or `vale` confirms. A clear `sí` or `no` is taken as soon as it is heard, even if the voice print’s label changed between the question and the answer.

`comando apaga el dispositivo` asks `¿Apago el dispositivo? Di sí o no.` Only `sí` runs `shutdown -h now` through the sudo wrapper.

In administrator mode, `lista las personas` speaks the identified names, and `borra` plus the name asks sí or no before deleting that person and their prints. If the deleted name is `locked`, the lock is cleared and any voice can speak again until the next enrollment. These two orders do nothing without administrator mode.

`comando identifica mi voz` enrolls the speaker by voice. It starts with the name. Two words in a row, such as Jose Antonio, are one person. If this voice or that name is already saved, it says it is the same person and asks whether to repeat the identification. If the name is not saved, it says so and asks whether to store it as another person. A no means “Di otro nombre.” Only a yes keeps the name. The same name replaces those prints and does not create another person. After the yes, it speaks four phrases (`hola grok`, `estás ahí`, `qué hora es`, `pon una canción`), three times each, and waits after every one for the repeat. The screen also shows `DI:` and `OI:`, but looking at it is not required. Each take is its own print, up to 12 under the confirmed name. Later speech matches the closest print. When it finishes, that name is the only voice the assistant will hear: wake, commands, confirmations, and the open conversation. Every other print is ignored, except that any person may say `comando identifica mi voz` to enroll a new voice. `salir` cancels. Until this command has been completed once, no voice is locked. An unidentified lane such as `voz 78` is temporary and is deleted after 10 seconds without use. It is not stored.

After `identifica mi voz`, the saved name in `speakers.json` under `locked` is the only voice that is heard. That covers the wake, every `comando`, the yes or no after a question, and the whole conversation. Any other print is ignored, including someone else saying `hola grok`. Only that voice can run `identifica mi voz` again. Until the command has been completed once, `locked` is empty and a wake or a `comando` can come from whoever is speaking. In that unlocked state, an open conversation still follows only the voice that opened it.

A single microphone cannot split two people who speak at the same instant. `gracias` or `vale` alone ends the talk, and only if that phrase is the locked voice. `gracias` is answered `De nada.` and `vale` is answered `Vale.`

A song request inside the conversation plays even without the word `comando`. If Grok answers with a line `COMANDO: …`, that order is run and the line is not read aloud. Music is YouTube audio only, via `yt-dlp` in the venv and system `mpv`, socket `/tmp/grok-music.sock`.

Outside a conversation, a finished phrase longer than six words is ignored unless it is a wake or `comando pon la canción` plus a title (up to sixteen words). The six-word check runs when the phrase has finished, not on a half-heard line. Inside a conversation there is no six-word limit.

Normal questions use Grok with `web_search` and `web_fetch`, at most 4 turns. While that call is in flight the status is `BUSCANDO`, the bottom line says `Buscando en la nube`, one line from `waits-es.txt` is spoken, and the microphone stays off until the real answer has been spoken.

The first boot line comes from `hellos-es.txt`, in order, one per start.

## Check that the copy matches

- `systemctl is-enabled wifi-boot` prints `enabled`. `grok-assistant` prints `disabled` and is inactive until Wi-Fi boot or a manual start. There is no service in this project that configures the screen or the sound card.
- `arecord -L` shows a default capture device when the operating system has a microphone.
- The journal line `listening on default` appears when the OS has a capture device, then `reconocedor activo: Kroko` and `voz activa: Dave, España`.
- The panel shows the command list, `TEMP` from the CPU, and the bottom line changes from `(silencio)` to `escuchando…` when someone speaks.
- `hola` gets `Hola.` and does not call the network.
- `comando identifica mi voz` asks whether the voice is the person already saved or another name. The same name replaces that person’s prints. A new confirmed name is stored as another person and becomes the only voice that is heard. Then it speaks each phrase, shows `DI:` / `OI:`, writes the prints into `speakers.json`, and sets `locked` to the confirmed name. After that, another voice saying `hola` is ignored.
