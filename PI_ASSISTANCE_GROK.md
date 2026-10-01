# Grok voice assistant — Raspberry Pi

This file is how to build the machine that is running now. The desktop tray app is a separate repository, [antonio-castellon/grok_assistant](https://github.com/antonio-castellon/grok_assistant). Do not follow that one for a Pi.

The live tree is `/home/antonio/grok-assistant`. The user is `antonio`. Spoken language is Spanish. Answers are short. Speech has no markdown, lists, emoji, or code.

The same tree is the repository [antonio-castellon/Grok_Pi_Assistance](https://github.com/antonio-castellon/Grok_Pi_Assistance). How to use the running assistant, with a picture of its screen, is in [GUIDE.md](GUIDE.md).

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
  pi_extra.py           account agents, the identification take, and the voice-lane room
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
| Speech to text, while the person is speaking | `sherpa-onnx-streaming-zipformer-es-kroko-2025-08-06` | yes, id `kroko`, spoken name Kroko |
| Speech to text, at silence | `sherpa-onnx-whisper-tiny` | id `whisper`, “Whisper pequeño” |
| Speech to text, at silence | `sherpa-onnx-whisper-base` | id `base`, “Whisper base” |
| Speech to text, at silence | `sherpa-onnx-whisper-small` | id `small`, “Whisper small”. Scored, and chosen only by voice. Not the automatic live engine |
| Speech to text, at silence, en/es/de/fr | `sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8` | id `canary`, “Canary” |
| Voice prints | `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` | yes. One CampPlus print, whatever engine is listening |
| Spoken voice 1 | `vits-piper-es_ES-davefx-medium-int8` | “Dave, España” |
| Voices 2 and 3 | `vits-piper-es_ES-sharvard-medium-int8` | “Sharvard, España, hablante 0” and “hablante 1” |
| Voice 4 | `vits-piper-es_ES-carlfm-x_low-int8` | “Carlfm, España” |
| Voice 5 | `vits-piper-es_ES-glados-medium-int8` | “Glados, España” |
| Voice 6 | `vits-piper-es_ES-miro-high-int8` | “Miro, España” |
| Voice 7 | `vits-piper-es_MX-ald-medium-int8` | “Ald, México” |
| Voice 8 | `vits-piper-es_MX-claude-high-int8` | “Claude, México” |
| Voice 9 | `vits-piper-es_AR-daniela-high-int8` | “Daniela, Argentina” |

Archives, from the sherpa-onnx `asr-models` and `tts-models` release assets:

- `sherpa-onnx-streaming-zipformer-es-kroko-2025-08-06.tar.bz2`
- `sherpa-onnx-whisper-tiny.tar.bz2`
- `sherpa-onnx-whisper-base.tar.bz2`
- `sherpa-onnx-whisper-small.tar.bz2` (about 610 MB compressed)
- `sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8.tar.bz2`
- `vits-piper-es_ES-davefx-medium-int8.tar.bz2` and the other Piper archives named like their directories

`sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11` is on this disk and is not in the recognizer menu. On this Pi its real-time factor was about 3 to 5, so it is not the live engine. Do not select it.

Kroko is opened with sherpa’s streaming transducer (encoder, decoder, joiner). No `model_type` is passed. Threads stay `num_threads` from `config.json`, currently 3. Piper loading uses at most 2. Whisper small uses the same filenames as tiny and base, with the prefix `small`. If both a normal `.onnx` and an `int8` file exist, the `int8` file is used.

`comando otro reconocedor` cycles the next installed engine. `comando reconocedor kroko`, `whisper`, `base`, `small`, or `canary` selects that one. Longer names are matched first, so “whisper small” does not select Whisper pequeño. If that folder is missing, the assistant says the engine is not there and keeps the current one.

At startup, if saved recordings have scores, the installed engine with the highest combined percentage becomes the live one. Whisper small is left out of that automatic choice. A tie keeps the engine already in use (`asr-index`). If there are no percentages, the live engine is Kroko. A spoken choice lasts until the next startup or the next full scoring. The percentage is a journal line, not speech and not a number on the screen:

- `motor escucha: Whisper base (91%). huellas combinadas.`
- `motor escucha: Kroko. huellas combinadas: aún no hay porcentajes.`

An older `asr-index` value `whisper-tiny` is read as `whisper`, and `whisper-base` as `base`.

## Voice footprint

This is part of the running Pi, not an optional extra.

- Model file: `models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`. sherpa-onnx 1.13.8 builds it with `SpeakerEmbeddingExtractorConfig(model=path, num_threads=1, debug=False, provider="cpu")` and then `SpeakerEmbeddingExtractor(config)`. `SpeakerEmbeddingExtractor(model=..., num_threads=..., debug=..., provider=...)` raises `TypeError`. That error must not be turned into an empty print. Each take is `create_stream()`, `accept_waveform(16000, audio)`, `input_finished()`, and `compute()` when `is_ready` is true. A vector is 192 floats. Audio shorter than 0.5 seconds can return no vector.
- Store: `/home/antonio/.config/grok-assistant/speakers.json`, mode 600. `people` is a dict keyed by name. `locked` is a sibling of `people`, not a field inside one person.
- Raw audio, not inside the JSON: `/home/antonio/.config/grok-assistant/raw/<slug>/00.wav` and the following files. Each wav is 16 kHz, 16-bit, mono. The slug is the name in lowercase with spaces turned into hyphens. If that folder is already someone else’s, the next one is `nombre-2`. Recording the same person again deletes their previous audio and reuses their slug.
- Each of the sixteen phrases is one raw wav and one CampPlus vector, so the takes can be checked as one person. The print is the whole take, not the first word. There is no separate vector per engine. The same vectors identify the person no matter which listening engine is active.
- A new person looks like this. `scores` starts empty and fills when each installed engine is scored. `last` is the last time that person spoke, as a Unix time. `greet_count` keeps the rotating extra line in the greeting.

```json
{
  "locked": "Antonio",
  "people": {
    "Antonio": {
      "prints": {"campplus": [[0.01, 0.02]]},
      "raw": [{"phrase": "hola grok", "file": "antonio/00.wav"}],
      "scores": {"kroko": {"hits": 14, "total": 16}},
      "last": 0,
      "greet_count": 0
    }
  }
}
```

A print from before this change may still be a list of vectors and may have no `raw`. On load it is kept as `prints.legacy` and is still recognized. It is not rewritten until something else saves the file, and it is not recorded again by itself. It cannot be scored until that person records once with the sixteen phrases.

`comando identifica mi voz` is done by ear. During the sixteen phrases, `salir` is a separate word after the phrase audio has been frozen. It drops every clip from that session and saves nothing. The panel also shows `DI:` and `OI:` on the bottom line. The heard words do not have to match the phrase. The audio and the CampPlus vector are what is stored. The microphone is whatever capture device the operating system is already using.

1. “¿Cómo te llamas?” It waits.
2. Two words in a row, such as Jose Antonio, are one person.
3. If that voice or that name is already saved: “Esta voz ya la tengo como NOMBRE. ¿Repito la identificación?” Yes replaces that person and does not create another. No returns to “Di otro nombre.”
4. If the name is new: “No tengo a NOMBRE. ¿Lo guardo como otra persona?” Yes stores it. No asks for another name.
5. It then speaks sixteen phrases, once each. The first spoken line is “Seguir pasa a la frase siguiente. Salir tira esta grabación.” It does not mention a window. A beep follows, then “1 de 16.” and the phrase. With the call-name Miguel, that phrase is still `hola Miguel`. Each later line is “N de 16. FRASE” after the low beep.
6. The phrases, in order: `hola` plus the assistant’s name, then `estás ahí`, `qué hora es`, `pon una canción`, `sube el volumen`, `baja el volumen`, `para la música`, `buenos días`, `hasta luego`, `qué día es hoy`, `me escuchas`, `gracias`, `abre la sesión`, `cuenta hasta tres`, `cómo estás`, `dime la hora`. The name defaults to Grok, so the first phrase is `hola grok`. `comando nombre Miguel` stores `assistant_name` in the machine config and the first phrase becomes `hola Miguel`. `hola grok` still opens a talk. A Spanish name is used because the Spanish recognizer has more trouble with the English word Grok. `comando nombre` speaks the current call. A name that is not one or two short words is refused and nothing changes.
7. Each take uses the capture device that is already open. It does not open a second input. Before the first phrase, an 880 Hz beep of 140 ms plays, then the assistant says “1 de 16.” and the phrase. The microphone stays open on that phrase. A kept `seguir` plays the 494 Hz beep of 220 ms, and only then comes the next “N de 16. FRASE”. Both tones go out through the playback device the operating system is already using. Normal conversation does not beep, and its silence time stays the configured 3.5 seconds. The name question and the yes or no still use the older close: the high beep starts that short take, and the low beep ends it.
8. The phrase buffer closes on energy, not on the words, and closing it does not advance. A block counts as voice when its RMS is above 0.004. The kept audio needs 0.8 seconds of voice. One second of silence after that voice freezes the buffer. Eight seconds of voice freezes it too, even while the person is still speaking. That cut does not move to the next phrase, and the old 12 second give-up is gone. The microphone then waits, with no deadline, for a separate short utterance. `seguir` or `siguiente` keeps the frozen audio. `salir` stops. One changed letter still counts. Anything else is ignored and the wait continues. Those two words are not written into the wav. The bottom line may show `DI:` and `OI:` while the words grow. Those words do not accept or reject the take. The file in `raw/` stores the phrase that was requested.
9. `seguir` with samples plus one 192-float CampPlus vector keeps the take even when the text is empty or is a different phrase. The live words are display only. The low beep plays, and the spoken prompt moves from “1 de 16” to “2 de 16”. `seguir` when nothing was heard stays on the same phrase, says “No he oído esta frase. Sigo en la misma.”, and does not count as a failure. The log line is `huella: nada, sigo en la misma · ETIQUETA`. Empty text with samples is logged as `huella: sonido guardado · ETIQUETA`. Any non-empty text is logged as `huella: TEXTO · ETIQUETA`. A real miss is no audio, or audio with no vector. Text with samples but no vector has no `huella: silencio` line, because the audio did arrive. It is spoken back: “He oído: TEXTO. No he cogido la huella. Repite.” and the same phrase is asked again. Audio shorter than 0.5 seconds can be that miss. A model file that is present and large enough is not announced as missing. Three real misses in a row stop. With text: “He oído: TEXTO. No he cogido la huella. Lo dejo.” Without text: “No oigo el micrófono. Lo dejo.” A kept take sets the failure count back to zero. `salir` drops every clip from this session, writes no wav, and says “Salgo. No guardo los audios de esta huella.” After the sixteenth `seguir`, the kept wavs are saved and every installed engine is scored. The stored print is replaced only when at least 12 vectors are one voice at cosine 0.55.
10. Afterward the takes must be one voice. Two takes match when their cosine is at least 0.55. The largest group in which every pair passes is kept. It must contain at least 12 takes. Anything outside that group is dropped. If no group reaches 12, nothing is saved and the person who was already stored stays as they were: “Estas tomas no son una sola voz. No guardo a otra persona.”
11. When the group is good, only those takes are saved, `locked` is set to that name, and each installed engine is scored from the wavs. “Listo, NOMBRE. El sonido queda guardado y vale para todos los motores. Valoro cada uno.” If some takes were dropped: “Guardo N de 16.”

Scoring does not ask the person to speak again. One engine is loaded at a time, each wav is transcribed, and the model is unloaded. A phrase of three words or fewer is a hit only when every word is present, in order. Accents are not required. One inserted, deleted, or substituted character still counts. A phrase of four or more words may miss one word. Extra heard words may be skipped. `hits` and `total` are the kept takes. An engine’s percentage is the sum of hits across every person divided by the sum of totals, rounded to an integer.

A screen label such as `voz 78` is not a stored person. It is a temporary lane and is deleted after 10 seconds without use.

Recognition compares the voice that just spoke with the print, not with the engine that transcribed. CampPlus matches at 0.55: the closest name above that threshold wins. Below it, the speaker is unknown. A legacy print that has no `campplus` vectors is still accepted at 0.30, so the person enrolled before this change is not locked out. A lane name alone does not open the door.

With the CampPlus file loaded: if `locked` is empty, `comando identifica mi voz` is accepted and hearing stays open. Once `locked` is a name, only that voice can wake, give a command, answer yes or no, continue the conversation, or identify again. Someone else saying `hola grok` does nothing. Other saved people stay in the file and are not heard until they are locked. If the CampPlus file is missing, the door stays open and enrollment says the voice model is missing. A file that is present is not described as missing.

In administrator mode only: `lista las personas` speaks the names. `borra` plus the name asks sí or no and deletes that person, their vectors, and their `raw/` folder. If the deleted name is `locked`, the lock is cleared. An empty administrator key means administrator mode does not open.

## config.json

```
language                     es
admin_key                    empty in the repository
personality                  empty, or one id: alex, vega, nico, lucia, marcos, ines, bruno, carmen
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

`personality` empty is the usual assistant. `comando personalidad` says the active name, or “Sin personalidad.” `comando personalidad vega` accepts the id or the name from `personalities.json` and saves it with the machine config. An unknown name gets “No tengo esa personalidad.” and nothing changes. The eight texts are fixed. A ninth personality cannot be created by voice. The chosen person’s block is appended only to the spoken-answer prompt, after the rules Grok already has. The interpreter does not receive it. The trait numbers are ceilings. The reply stays one or two spoken sentences. Correctness wins. The assistant does not say which person or tone is in use.

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
| `speakers.json` | mode 600. People as a dict, CampPlus prints or legacy prints, scores, `last`, `greet_count`, and `locked` |
| `active-agent` | name of the Grok agent in use, if any. The CLI receives the markdown path |
| `agent-state.json` | local uuid for each opened agent. The account is not asked for an id |
| `account-agents/` | copy of the account’s agents, refreshed in the background at startup |
| `sessions` | local sessions, mode 600 |
| `raw/<slug>/*.wav` | enrollment audio for scoring. Not inside `speakers.json`. Not in the repository |

Named sessions do not expire. The shared session lasts 24 hours and then a new id is created. Do not delete `~/.grok/sessions/` for the coding session. Cleanup of orphan voice sessions must stay inside the voice working directory.

`listar agentes` speaks the bare names from three places. The same name, ignoring case, keeps the later one. First `~/.grok/bundled/agents/*.md` (on this account `explore`, `plan`, and `general-purpose`). Then the copy in `account-agents/`. Then `~/.grok/agents/*.md`, which wins. The spoken line is “Tengo explore, general-purpose, plan.” When every name comes from the bundle or the account copy, it adds “Esos salen de la cuenta.” When there are none: “No hay agentes.” Personas, roles, and the chat modes Auto, Fast, Expert, Heavy, and Build are not agents.

Creating an agent still writes a markdown file in `~/.grok/agents`, asks for the spoken admin key, and does not open it. Opening one passes `--agent` and the markdown path to the Grok CLI. The id stored on the Pi is a uuid created here. `cerrar agente` returns to the normal assistant and does not delete the file. A session stays the local notebook.

At startup, after the hello is not delayed, a background read copies the account agents. It asks `https://cli-chat-proxy.grok.com/v1/subagents/bundle` for the `agents` object and `https://grok.com/rest/user-settings` for `agentCustomizations`. A customization with the same name replaces the bundle entry. The token stays in `~/.grok/auth.json` and is not written to the log or this file. A failure or an empty body leaves the previous copy and the bundled files untouched. A response that contains agents replaces the copy: each file is written as `.md.tmp` and then renamed, and copy files that are no longer returned are deleted. The filename keeps letters, digits, dot, hyphen, and underscore, at most 80 characters. If nothing remains, the file is `agente.md`.

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

The picture is: logo, then a row with volume, the recognizer name, and right-aligned `MEM`, `CPU`, `DISCO`, and `TEMP`. Temperature is only `cpu-thermal`, shown as `53°`. Then the voice row and, on the right, Wi-Fi status from `iw dev wlan0 link` and `ip -4 addr`. Then the operating-system row. Its text is the pretty name from `/etc/os-release`, the kernel release up to the first `+`, and the machine. At the end of that same row, in the small font, is `build` plus seven characters. Those characters are the published `main` commit when `assistant.py` and `pi_extra.py` match that commit. Otherwise they are a hash of the two files on this disk, so the screen still shows the copy that is running. The journal line is `build` and those seven characters. Then the command list. The heard line is at the bottom.

Spanish glyphs come from the PSF unicode table. A Latin-15 identity map draws the wrong accents. Do not replace that loader.

While music is audible the microphone is off. The buttons are `PAUSA` / `SEGUIR` and `STOP`. Pausing or stopping turns the microphone back on. A logo tap must not turn the microphone on while the song is still audible. Ducking for a spoken reply must not resume a song the user already paused.

## What the running program does

The rules of the talk below are what this Pi runs. They are implemented in `assistant.py` and `pi_extra.py`, not in a tray app. The desktop tray app is the separate repository `grok_assistant`.

Wake, with no cloud call and no search: `hola grok` (and close mishearings such as `hola grok` cut short, `hola grop`, `pola grove`), `hola` plus the name set with `comando nombre` (one changed letter still counts), a short `hola` on its own, or `¿estás ahí?` with Grok or with that name. The answer is `Hola.` or `Sí, aquí estoy.` The words that came with the wake are not sent anywhere. The assistant then waits.

Every other order starts with the word `comando`. The screen says that once, on the `COMANDOS` line. The lines under it do not repeat the word. If the rest of the phrase is not an exact local command, it goes to the Grok interpreter. A repaired command is spoken back as `Has dicho: …. ¿Sí o no?` before it runs. `sí` or `vale` confirms. A clear `sí` or `no` is taken as soon as it is heard, even if the voice print’s label changed between the question and the answer.

`comando apaga el dispositivo` asks `¿Apago el dispositivo? Di sí o no.` Only `sí` runs `shutdown -h now` through the sudo wrapper.

In administrator mode, `lista las personas` speaks the saved names, and `borra` plus the name asks sí or no before deleting that person, their prints, and their `raw/` folder. If the deleted name is `locked`, the lock is cleared and any voice can speak again until the next enrollment. These two orders do nothing without administrator mode.

`comando identifica mi voz` enrolls one person, once. The dialogue, the sixteen phrases, the 0.55 group of at least 12, and the engine scores are in Voice footprint above. The screen shows `DI:` and `OI:`, and looking at it is not required. After a phrase is frozen, `seguir` keeps it and `salir` drops the session without writing a wav. When it finishes, that name is `locked` and is the only voice the assistant will hear.

After `identifica mi voz`, the name in `locked` is the only voice that is heard. That covers the wake, every `comando`, the yes or no after a question, identifying again, and the whole conversation. Any other print is ignored, including someone else saying `hola grok`. Until `locked` is set, a wake or a `comando` can come from whoever is speaking. In that unlocked state, an open conversation still follows only the voice that opened it. A print stored before this change, with no CampPlus vectors, still matches at 0.30 so that person can speak and can record again. A new CampPlus print matches at 0.55.

`comando personalidad` says the current personality, or “Sin personalidad.” `comando personalidad` plus an id or a name from `personalities.json` switches to that person. The spoken answer keeps the persona. The command interpreter does not.

A single microphone cannot split two people who speak at the same instant. `gracias` or `vale` alone ends the talk, and only if that phrase is the locked voice. `gracias` is answered `De nada.` and `vale` is answered `Vale.`

A song request inside the conversation plays even without the word `comando`. If Grok answers with a line `COMANDO: …`, that order is run and the line is not read aloud. Music is YouTube audio only, via `yt-dlp` in the venv and system `mpv`, socket `/tmp/grok-music.sock`.

Outside a conversation, a finished phrase longer than six words is ignored unless it is a wake or `comando pon la canción` plus a title (up to sixteen words). The six-word check runs when the phrase has finished, not on a half-heard line. Inside a conversation there is no six-word limit.

Normal questions use Grok with `web_search` and `web_fetch`, at most 4 turns. While that call is in flight the status is `BUSCANDO`, the bottom line says `Buscando en la nube`, one line from `waits-es.txt` is spoken, and the microphone stays off until the real answer has been spoken.

The first boot line comes from `hellos-es.txt`, in order, one per start.

Outside test mode, a finished phrase is read a second time with Whisper base when that model is installed, otherwise with Whisper pequeño. The point is an English name that the Spanish ear dropped. The startup log, not spoken, is `fuera de la prueba, releo cada frase con ETIQUETA para guardar los nombres en inglés`. A reread that is a single token, such as `1.0`, does not replace a real phrase. `me escuchas`, `me oyes`, and `estás ahí` are not read again. A reread that would drop a wake, a `comando`, a song request, a goodbye, or a yes or no is ignored, so the order the selected engine already heard still runs. In test mode the phrase stays the selected engine’s transcript, and that engine’s name is written under the heard line. Nothing in this second reading leaves the Pi.

## Check that the copy matches

- `systemctl is-enabled wifi-boot` prints `enabled`. `grok-assistant` prints `disabled` and is inactive until Wi-Fi boot or a manual start. There is no service in this project that configures the screen or the sound card.
- `arecord -L` shows a default capture device when the operating system has a microphone.
- The journal line `listening on default` appears when the OS has a capture device, then `reconocedor activo: Kroko`, `voz activa: Dave, España`, and `motor escucha: Kroko. huellas combinadas: aún no hay porcentajes.` until a sixteen-phrase recording has been scored. With Whisper base installed, the journal also has `fuera de la prueba, releo cada frase con Whisper base para guardar los nombres en inglés`.
- The panel shows the command list, including personalidad and the sixteen-phrase enrollment line, `TEMP` from the CPU, and the bottom line changes from `(silencio)` to `escuchando…` when someone speaks.
- `hola` gets `Hola.` and does not call the network.
- `comando identifica mi voz` asks “¿Cómo te llamas?”, confirms a saved voice or a new name, then says that `seguir` goes to the next phrase and `salir` throws this recording away. One second of silence freezes the phrase and leaves the microphone on it. `seguir` keeps it. An empty `seguir` stays on the same phrase and is not one of the three misses. `salir` says “Salgo. No guardo los audios de esta huella.” and writes no wav. Three real misses, no audio or no voice print, still stop. After the sixteenth `seguir`, it keeps a group of at least 12 that match at cosine 0.55. It writes CampPlus prints and wavs, sets `locked`, and scores the installed engines. After that, another voice saying `hola` is ignored. Only the locked voice can identify again.
