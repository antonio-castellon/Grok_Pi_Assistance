# Grok voice assistant — desktop clone

Build the same voice assistant as a tray application for Windows and Linux. It is not a Raspberry Pi, not a framebuffer, and not a touchscreen. Keep the conversation rules, the command API, and the session model. Change only the shell: a tray icon, an information window, and a debug transcript.

Spoken language is Spanish. Short spoken answers. No markdown, lists, emoji, or code in speech.

## What the user hears and sees

The tray icon is the whole application. Left click opens the information window. Right click opens the menu. A separate debug window shows the text that was heard and the text that was answered. Closing a window does not quit the app. Quit is a menu item.

Tray menu:

- Pause listening / Resume listening
- Information
- Debug
- Recognizer (the speech-to-text engine)
- Voice (the spoken voice)
- Model (the Grok model and the reasoning effort)
- Sessions
- Administrator password
- Quit

Information window, always readable, updated in place:

- Listening, paused, searching, or in a conversation
- Active Grok model and reasoning effort (`low` or `high`)
- Active voice name and number
- Active recognizer name
- Active session name, and whether it is the shared one
- Volume
- Last heard line and last spoken line

Debug window:

- A running log, newest at the bottom
- Each finished utterance: time, raw recognizer text, what the interpreter decided (`ignorar`, `conversacion`, or `comando` plus the strict line), and whether the user confirmed
- Each Grok answer, as text, before it is spoken
- Errors from the recognizer, the interpreter, and Grok, in one line each
- A button to clear the view. Clearing the view does not delete sessions

## Flow

While waiting, no conversation is open:

```
microphone
    |
local speech-to-text          (streaming if the engine can, otherwise when the phrase ends)
    |
hola grok, or "estás ahí grok"? yes --> say "Hola." or "Sí, aquí estoy." and wait
    |                              (and the close mishearings)
    |                              do not search, and do not send those words
no
    |
starts with "comando"? ------ no --> drop it. It is not a command
    |
the words after "comando"
    |
exact local command? -------- yes --> do it now
    |
Grok interpreter, only to repair that command
    |
    +-- a known order -------> ask "Has dicho: <orden>. ¿Sí o no?"
    +-- anything else -------> "No conozco ese comando."
                               This does not open a conversation

When a real question is sent to the cloud:

    status BUSCANDO
    say one short waiting line
    listener stays off
    Grok answers (web search allowed)
    status back to CONVERSACIÓN
    speak the answer
    listener on again
```

Audio is never streamed to Grok. Only a finished phrase is sent, and only when the rules above say so.

When several people talk, each voice print keeps its own transcript. A single microphone cannot split two voices that speak at the same instant; it splits them when one stops and the other starts.

Until `identifica mi voz` has been completed, no voice is locked. A wake or a line that starts with `comando` can come from whoever is speaking. Once that command has saved a name, only that person's prints are heard. Wakes, commands, a yes or no, and the open conversation all have to match the closest of those prints. Every other person is ignored. Only that same voice can run `identifica mi voz` again. Before any name is locked, an open conversation is limited to the voice that opened it.

## Voice footprint

The speaker model is 3D-Speaker CampPlus (`3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`). One person can have up to 12 prints. A new phrase matches the closest print, not a single average.

`comando identifica mi voz` is spoken. The screen is optional.

1. It asks for the name first and waits.
2. Two words in a row, such as Jose Antonio, are one person.
3. It repeats the name aloud.
4. If that voice or that name is already saved, it says this is the same person and asks whether to repeat the identification. A yes replaces the old prints. It does not create a second person.
5. If the name is not saved, it says so and asks whether to store it as another person. A no means “Di otro nombre.”
6. Only a yes keeps the name.
7. It then speaks four phrases, three times each, and waits after every one: `hola grok`, `estás ahí`, `qué hora es`, `pon una canción`. The second and third time it says “Otra vez” and the phrase.
8. The bottom line shows `DI:` the prompt and `OI:` what was heard. The words do not have to match.
9. `salir` cancels.
10. The prints are saved under the confirmed name. `speakers.json` field `locked` becomes that name.

A label such as `voz 78` is a temporary lane in memory, not a saved person. An unidentified lane is deleted after 10 seconds without use. It is not written to disk.

After at least one person has finished `identifica mi voz`, only those enrolled voices are heard: wake, commands, yes or no, and the open conversation. A print counts when it is within 0.30 of the closest saved take, or the lane is already named as that person. That name check also applies to a live wake line, before the phrase has finished. Anyone else is ignored. The exception is `comando identifica mi voz`: any person may say it, so a new voice can be enrolled and the system can be started. Until the first enrollment, no voice is locked.

In administrator mode, `lista las personas` speaks the names. `borra` plus the name asks sí or no, then deletes that person and their prints. Deleting the locked name clears the lock.

The listener is off while the assistant is speaking, including the short waiting line and the real answer. On the Pi the microphone loop does not read during playback, so the answer is not captured as a new phrase. Do not add a second filter that throws lines away because they sound like the assistant. When the spoken reply has finished, listening turns on again.

While the user is already in a conversation there is no six-word limit. A phrase that starts with `comando` is an order and runs immediately when it matches. Asking for a song also runs, even without that word. Anything else goes to Grok as spoken. If Grok answers with a line `COMANDO: ...`, that order is run and the line is not read aloud.

The conversation Grok may answer with a single line `COMANDO: ...`. That line is a strict command. Run it. Do not speak the line `COMANDO:` itself.

## States

- **Waiting.** The mic is on. Ordinary talk is not a conversation. Only `hola grok` starts one, and only from the enrolled voice once a voice has been identified. Every other order starts with the word `comando`. A phrase that does not is ignored. Another person's voice is ignored after enrollment. The six-word check runs when the phrase has finished, so a half-heard line can still be corrected by the recognizer. A finished phrase longer than six words is ignored and is not sent to the cloud, unless it starts with a real wake (`hola grok`, `ok grok`, `despierta grok`, including the close mishearings) or it is a song request. Words spoken in the same breath as `hola grok` are not sent anywhere. The next phrase, after `Hola.`, is the one that is heard. `pon la canción` plus the title may be longer than six words, up to sixteen, and runs at once. Any other command that is not attached to a wake must be six words or fewer. While sí or no is pending, the next phrase is only that answer: it is not dropped for length and it is not sent to the cloud. Sí runs the command. No, or any other phrase, says `Vale.` and does not run it.
- **Conversation.** Grok answers. There is no six-word limit. It stays open until goodbye or 60 seconds with no new phrase. Time spent answering does not count. After enrollment, only the enrolled voice is answered. Before enrollment, only the voice that opened the talk is answered.
- **Searching.** A question has been sent to the cloud. The status is BUSCANDO. The listener is off. This ends when the answer starts to be spoken.
- **Paused.** The tray action or a future hotkey closes the mic. No recognition, no Grok. The icon shows paused.
- **Test.** Say `prueba` or `test`. The status becomes PRUEBA. The debug window and the information window show only what the recognizer wrote. No commands, no Grok, no wake. `salir` leaves the test and says "Salgo de la prueba."
- **Admin.** Required when the request changes the computer itself or needs administrator rights (install, files outside the app, services, sudo). It is also required to list the identified people (`lista las personas`) and to delete one (`borra NOMBRE`, then sí or no). Deleting the enrolled voice clears the lock. The public commands do not need it: volume, voice, recognizer, sessions, music, the spoken shutdown confirmation, and help. Entering admin asks for the password. It lasts 5 minutes, then a spoken line returns to the normal conversation without closing it.

## Wake and goodbye

The wake is `hola grok` (including close mishearings such as `hola grop` and `pola grove`), a short `hola` on its own, or `¿estás ahí, Grok?` / `Grok, ¿estás ahí?`. The reply is `Hola.` or `Sí, aquí estoy.` Nothing is searched and nothing from that phrase is sent to the cloud. The assistant then waits for the next thing the user says.

If a stored voice print matches, greet by the time since that person last spoke:

- under 1 hour: `Dime.`
- from 1 hour to under 5 hours: `Hola de nuevo, <nombre>.`
- 5 hours or more, or no earlier time: `Hola, <nombre>.` plus one short rotating extra

Ask `Hola, ¿cómo te llamas?` only the first time a new print appears. Prints do not expire. Do not keep a print of the assistant's own voice to reject lines. Listening is simply off until its reply has finished playing.

Goodbye is immediate and local, before any Grok call:

- `cierra conversación` (also `cerrar`, `acaba`, `termina`, `salir`, `corta` together with `conversación`) → say `Adiós.` and close, when the whole phrase is at most eight words. A long sentence that only mentions those words stays in the talk. Outside, the six-word gate applies first.
- `gracias`, `muchas gracias`, or just `vale` → close. `gracias` is answered with `De nada.` and `vale` with `Vale.` These do not need the word `comando`. A yes/no that is already waiting still treats `vale` as yes.
- `vale ya está` → say `Adiós.` and close, when the phrase is six words or fewer
- `adiós`, `hasta luego`, `chao`, `cuando quieras seguimos`, and the other short closers (`ya está`, `nada más`, `basta`, `déjalo`) → say `Adiós.` and close. These are at most eight words.
- `no necesito nada` and the other exact short "nothing else" phrases → say `Vale.` and close

`cierra conversación` is not `cerrar sesión`. Closing the conversation does not switch the Grok session. `cerrar sesión` does switch back to the shared one, and if the talk is open it ends. The screen returns to waiting. A phrase that contains `gracias` is answered with `De nada.` even if it also closes the talk.

After a real question with a long answer, ask once: `¿Quieres que te lo cuente con más detalle?` Sí repeats the same question with reasoning effort `high` and does not ask again. That second cloud wait uses the same searching status and another waiting line. No says `Vale.` and stays in the conversation. Do not offer this after a short closer, after an error, or when no question was asked.

## Waiting while the cloud answers

A cloud answer takes several seconds. Do not leave the screen on a bare "Interpretando".

- While a loose phrase is still being classified, the status text is `Interpretando … buscando en la nube`. Do not speak a waiting line yet. The result may be "ignore" or a command confirmation.
- When a real question is sent to Grok, switch the status to BUSCANDO and show `Buscando en la nube`. Speak one short filler line, then wait for the answer, then speak the answer. The microphone stays off from the start of the filler until the answer has finished playing.
- Use a long list of fillers, one per cloud wait, in order, and do not repeat one until the list is finished. Keep each line short enough to say in one breath. Mix plain lines (`Entiendo, déjame que busque`) with light jokes (`Te lo busco mientras me ato los zapatos`, `Huy, espera que lo miro`). On the Pi the list is `waits-es.txt`, 166 Spanish lines, index in the app config. Ship that list, or one at least as long, with the desktop app.
- Do not speak a filler for an exact local command, for sí or no, or for goodbye. Those stay immediate.

## Lines said at startup

Each time the app starts, speak one short hello and nothing else. No instructions, no command list. The line starts with `Hola,` and is one breath long. Mix light jokes with the feeling of just having woken up (`Hola, el que madruga encuentra el café frío. Yo lo encontré.`).

Use a long list, one line per start, in order, and do not repeat one until the list is finished. On the Pi the list is `hellos-es.txt`, 180 Spanish lines, and the next index is `hello-index` in the app config. Ship that list, or one at least as long, with the desktop app. These are not the waiting fillers. Startup uses `hellos-es.txt`. The cloud wait uses `waits-es.txt`.

Silence of about 3.5 seconds ends one phrase. A phrase may last up to 45 seconds. 60 seconds with no new phrase ends the conversation.

## Local match is one character loose

A local command does not need the perfect string. Match the command words with the usual alternates (`apaga` / `apagar`, `otro` / `siguiente`, `hola grok` / `hola grop` / `pola grove`) and still accept the phrase when one character is inserted, deleted, or substituted. `otro reconocedor` and `otro reconocedot` are the same command. Two wrong characters are not. This is only the local fast path. If it still does not match, Grok may reinterpret it, and that result waits for sí or no.

On the Pi the spoken admin key, when one is set, uses that same one-character rule. A single digit does not match. The key is not in the repository. It lives only in the machine file `~/.config/grok-assistant/config.json`. An empty key means administrator mode does not open. The desktop app does not speak that key and does not ship it: the tray password is checked against the salted hash, as typed.

## Administrator password

The tray menu has **Administrator password**. The user sets it there. Save only a salted hash in the application folder, never the password itself. There is no spoken default key and no password in the source.

When a request must change the machine or needs administrator or sudo rights, do not do it in the normal conversation. Say that it needs administrator access and ask for the password. On success, run that request with rights, then ask if they want to leave admin. Yes returns to the normal conversation. The conversation stays open either way. After 5 minutes, leave admin the same way.

Volume, voice, recognizer, sessions, music, help, listing agents, opening an agent, and the sí or no before shutdown stay available without that password. Creating an agent asks for that password first.

## Agents and sessions

A session stays the private local chat. An agent is a Grok agent you open on purpose. Do not rename sessions to agents.

- The phrase must contain `agente` or `agentes`. It does not match `cierra conversación`. Inside a conversation the phrase is at most eight words. Outside, the six-word gate applies first.
- `listar agentes` and `abrir agente NOMBRE` (`hablar`, `cargar`, `iniciar`) do not need the password. Opening one says its name and the following questions go to that agent, with web search, until `cerrar agente`. The talk stays open; do not also say `Dime.`
- `crear agente NOMBRE` asks for the administrator password, then creates the agent. It does not open that agent. On the desktop app that password is the hashed tray password. On the Pi it is the spoken admin key. `no` cancels.
- Each agent can remember dates, places, and lists, and can search the internet. That memory lives with the Grok account, so it is reachable from another machine. Local sessions do not move there.
- `cerrar agente` returns to the normal assistant. `cierra conversación` still ends the talk and does not delete the agent.
- On screen the command list starts with how to start and how to end a talk, then one line per group. The group name and its commands are on that same line. Actions that take a name are in parentheses, and NOMBRE stays outside:

```
iniciar: "hola grok" o "¿estás ahí?"   acabar: "gracias" o "vale"
COMANDOS (iniciar con palabra "comando")
SESION ( abrir ¦ crear ¦ borrar ) NOMBRE , listar , cerrar
AGENTE ( abrir ¦ crear ) NOMBRE , listar , cerrar
modo administrador
subir volumen
bajar volumen
voz NÚMERO
otra voz
otro reconocedor
pon la canción X
para la música
apaga el dispositivo
prueba
identifica mi voz
```

The first line is how the talk starts and ends. The next line is the command header, and it is the only place that says the orders start with `comando`. The lines under it do not repeat that word. SESION and AGENTE stay in capitals, on the same line as their commands. `quiero cerrar sesión de …` still closes the session, but only when the whole phrase is six words or fewer and it starts with `comando`. The verbs of a session order may sit anywhere after that word, as long as it says `sesión` and does not say `agente`. `listar` and `cerrar` do not take NOMBRE. The desktop help and the information window use this same list.

## Commands

A local match, including one wrong character, runs at once. A phrase that does not match becomes one of these strict lines only after Grok says so and the user answers sí.

| Strict line | What it does |
|---|---|
| `subir volumen` / `bajar volumen` | step the output volume and say the new percent |
| `otra voz` | next voice |
| `voz N` | voice number N, 1-based, as shown in the menu |
| `pon cancion TITULO` | play that song, audio only. The title may make the phrase longer than six words, up to sixteen. It still runs locally and is not sent to the cloud |
| `identifica mi voz` | enroll the speaker. It starts with the name. Two words such as Jose Antonio are one person. If the voice or the name is already saved, it says so and asks whether to repeat that same person’s identification. If the name is new, it says it is not saved and asks whether to store it as another person. A no means “Di otro nombre.” Only a yes keeps the name. The same name replaces that person’s prints and does not create a second person. Then it speaks four short phrases (`hola grok`, `estás ahí`, `qué hora es`, `pon una canción`), three times each, and waits after every one for the repeat. The screen is optional: `DI:` is the prompt and `OI:` is what was heard. Each take is its own print, up to 12. A later phrase matches the closest print. After this, only enrolled voices are heard. Anyone else is ignored, except that any person may say `comando identifica mi voz` to enroll. `salir` cancels |
| `lista las personas` | administrator only. Speaks the names of the identified people |
| `borra PERSONA` | administrator only. Asks sí or no, then deletes that person and their prints. If that person was the only voice being heard, the lock is cleared |
| `pausa musica` / `seguir musica` / `para la musica` | pause, resume, stop |
| `otro reconocedor` | next speech-to-text engine |
| `reconocedor kroko` / `whisper` / `base` / `canary` | select that engine if it is installed |
| `listar sesiones` | speak which sessions exist |
| `crear sesion NOMBRE` | ask sí or no, then create. Named sessions do not expire |
| `abrir sesion NOMBRE` | switch to it |
| `cerrar sesion` | back to the shared session and, if a conversation is open, leave it. The screen returns to waiting. It does not keep talking on the shared session. Also «quiero cerrar sesión de …», if the whole phrase is at most six words |
| `listar agentes` | say the Grok agents that exist. No password |
| `abrir agente NOMBRE` | talk to that agent from here. Also `hablar`, `cargar`, `iniciar`. No password. Later turns use that agent until `cerrar agente` |
| `crear agente NOMBRE` | ask for the administrator password, then create it. Not for everyone |
| `cerrar agente` | leave the agent and return to the normal assistant. Does not end the talk |
| `borrar sesion NOMBRE` | ask sí or no, then delete. No admin key |
| `apagar` | ask sí or no, then shut down the computer |
| `ayuda` | speak the short command list. It does not have to be on screen |
| `prueba` | enter test mode |

The shared session is one conversation, deleted and recreated after 24 hours. Named sessions stay until deleted.

While music is playing, listening is off so the mic does not hear the song. Pausing or stopping the music turns listening back on. The tray shows pause and stop while a song is loaded.

## Models and voices

Default model is `grok-4.7`. Reasoning effort starts at `low`. `high` is only the "más detalle" repeat. The model menu lists the models the local `grok` command can actually run, and the current one is marked. Changing model applies to the next turn. Do not invent a local Grok: the model call needs the network. A small local language model was tried on a Pi and was too slow; do not add one unless a later measurement says it answers in under 3 seconds.

Normal turns may use web search. Do not claim there is no internet. The interpreter turn is classification only: no tools, no web search, one JSON object `accion`, `orden`, `texto`.

Voices are local text-to-speech, Spanish, one loaded at a time. On the Pi they are Piper: Dave, two Sharvard, Carlfm, Glados, Miro, Ald, Claude, Daniela. On a PC, use the same set if the engines exist; otherwise ship at least a few Spanish voices and the same menu behavior (`otra voz`, `voz N`). Speech pauses music, then resumes it, and does not resume a song the user already paused.

Recognizers are local. Prefer a streaming engine so words appear while they are spoken. An end-of-utterance engine (Whisper, Canary) shows text only after the pause. The debug window must show that text and keep it. The Pi engines were Kroko (streaming), Whisper tiny, Whisper base, and Canary. Use those if they run on the PC; if not, expose whatever local engines install cleanly and keep the same command names for the ones that exist.

English proper names are the weak spot of a Spanish-only recognizer: it writes the name the way it sounds. On this Pi, Whisper and Canary did not fix that reliably, and they damaged short Spanish commands. A multilingual streaming model (Nemotron 3.5, 560 ms) spelled a few famous names better and still missed others, and on the Pi it ran at about three to five times slower than realtime, so it is not the live engine. A faster PC may use it. Hotword lists do not invent a spelling the audio never suggested.

## Do not copy from the Pi

The Pi draws on the framebuffer the operating system assigned (`fb0`) and plays and records on the ALSA default devices. It does not configure HDMI, a panel, or a sound card, and it does not ship a password or a token. The desktop password is the one set in the tray, stored only as a hash. Shutdown may call the operating system's normal shutdown after the spoken sí or no.

The Pi program is the private repository `antonio-castellon/Grok_Pi_Assistance`. `install-pi.sh` in that tree is the clean Raspberry Pi OS installer. It is not the desktop tray installer. Do not copy `speakers.json`, a sudo password, or an API token into the repository. The script downloads the models.

## Done when

- The tray icon starts with the session and survives closing the windows.
- Pause stops recognition until resume.
- Information shows model, voice, recognizer, session, and listening state.
- Debug shows heard text and answered text.
- Sessions can be listed, created, opened, closed back to the shared one, and deleted, with sí or no where the table says so.
- Every order except `hola grok` is ignored unless the phrase starts with `comando`.
- A garbled command that only Grok recovered is not executed until the user says sí.
- Listening is off for the whole spoken reply, then on again. There is no echo filter.
- A cloud question shows BUSCANDO, speaks one rotating short filler from the waiting list, then the answer. The mic stays off through both.
- Startup speaks the next line from the hello list, not from the waiting list.
- Machine changes ask for the administrator password from the tray. The file in the app folder is a hash.
- `cierra conversación` ends the talk. `cerrar sesión` returns to the shared session and, if a conversation is open, ends that too. The screen goes back to waiting.
- Outside a conversation, a finished phrase of more than six words is ignored unless it starts with a real wake or it is `pon la canción` plus a title (up to sixteen words). Inside a conversation there is no six-word limit.
- The on-screen list leads with how to start and end the talk, then the line `COMANDOS (iniciar con palabra "comando")`, then the command list.
