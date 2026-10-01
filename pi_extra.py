"""Account agents, the identification take, and the voice-lane room.

assistant.py imports this after its own names exist. Path constants are read
from that module on each call, so a test can point them at a temporary folder.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np

def _host():
    """The module that owns the path constants and STOP.

    Started as a program, that module is __main__. Imported by a test, it is
    assistant. Importing assistant from here while the program is __main__
    would load a second copy.
    """
    import sys

    main = sys.modules.get("__main__")
    main_file = getattr(main, "__file__", None) if main is not None else None
    if main_file and Path(main_file).name == "assistant.py":
        return main
    loaded = sys.modules.get("assistant")
    if loaded is not None:
        return loaded
    import assistant
    return assistant


A = _host()
log = A.log
ROOT = A.ROOT

_CHAT_MODES = {"auto", "fast", "expert", "heavy", "build"}


def _skip_agent_stem(stem: str) -> bool:
    """Personas, roles, and chat modes are not agents. A bundled agent keeps its name."""
    key = stem.casefold()
    if key in _CHAT_MODES:
        return True
    personas = Path.home() / ".grok" / "bundled" / "skills" / "shared" / "personas"
    if (personas / f"{key}.md").is_file():
        return True
    roles = Path.home() / ".grok" / "bundled" / "roles"
    if (roles / f"{key}.toml").is_file() and not (A.BUNDLED_AGENTS / f"{key}.md").is_file():
        return True
    return False


def agent_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", name)[:80].strip(".-")
    if not cleaned:
        return "agente.md"
    if cleaned.endswith(".md"):
        return cleaned[:80]
    return cleaned[:77] + ".md"


def agent_sources() -> list[tuple[str, Path, str]]:
    """Bundled, then the account copy, then local files. The later name wins."""
    found: dict[str, tuple[str, Path, str]] = {}
    for directory, source in (
        (A.BUNDLED_AGENTS, "bundled"),
        (A.ACCOUNT_AGENTS, "account"),
        (A.AGENTS_DIR, "local"),
    ):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if _skip_agent_stem(path.stem):
                continue
            found[path.stem.casefold()] = (path.stem, path, source)
    return list(found.values())


def resolve_agent(name: str) -> tuple[str, Path] | None:
    key = (name or "").casefold()
    if not key:
        return None
    for stem, path, _source in agent_sources():
        if stem.casefold() == key:
            return stem, path
    return None


def agent_markdown(name: str) -> Path | None:
    found = resolve_agent(name)
    if found is None:
        return None
    return found[1]


def list_agent_names() -> list[str]:
    return sorted((stem for stem, _path, _source in agent_sources()), key=str.casefold)


def agent_list_line() -> str:
    rows = agent_sources()
    if not rows:
        return "No hay agentes."
    names = sorted((stem for stem, _path, _source in rows), key=str.casefold)
    line = "Tengo " + ", ".join(names) + "."
    if all(source != "local" for _stem, _path, source in rows):
        line += " Esos salen de la cuenta."
    return line


def remember_agent_id(name: str) -> str:
    """A uuid created on this Pi. The account is not asked for another id."""
    key = name.casefold()
    data: dict = {}
    try:
        stored = json.loads(A.AGENT_STATE.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            data = stored
    except (OSError, json.JSONDecodeError):
        data = {}
    ids = data.get("ids")
    if not isinstance(ids, dict):
        ids = {}
    current = ids.get(key)
    if not isinstance(current, str) or not current:
        current = str(uuid.uuid4())
        ids[key] = current
        data["ids"] = ids
        A.AGENT_STATE.parent.mkdir(parents=True, exist_ok=True)
        A.AGENT_STATE.write_text(json.dumps(data) + "\n", encoding="utf-8")
        os.chmod(A.AGENT_STATE, 0o600)
    return current


def _auth_session() -> tuple[str, str] | None:
    path = Path.home() / ".grok" / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entries: list[dict] = []
    if isinstance(data, dict):
        if data.get("key") and data.get("user_id"):
            entries.append(data)
        for value in data.values():
            if isinstance(value, dict) and value.get("key") and value.get("user_id"):
                entries.append(value)
    if not entries:
        return None
    token = str(entries[0].get("key") or "")
    user = str(entries[0].get("user_id") or "")
    if not token or not user:
        return None
    return token, user


def _get_json(url: str, token: str, user: str) -> object | None:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "GrokAssistant",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-userid": user,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read()
    except (OSError, urllib.error.URLError, TimeoutError):
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _bundle_agents(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    agents = payload.get("agents")
    if not isinstance(agents, dict):
        return {}
    found: dict[str, str] = {}
    for name, body in agents.items():
        if not isinstance(name, str) or not isinstance(body, str) or not body.strip():
            continue
        text = body if body.endswith("\n") else body + "\n"
        found[name.strip()] = text
    return found


def _customization_markdown(item: dict) -> tuple[str, str] | None:
    name = ""
    for key in ("name", "title", "displayName", "id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    if not name:
        return None
    text = ""
    for key in ("instructions", "systemPrompt", "prompt", "description"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    return name, f"---\nname: {name}\n---\n\n{text}\n"


def _custom_agents(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("agentCustomizations")
    if not isinstance(rows, list):
        return {}
    found: dict[str, str] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        parsed = _customization_markdown(item)
        if parsed is None:
            continue
        name, body = parsed
        found[name] = body
    return found


def _replace_account_agents(files: dict[str, str]) -> None:
    A.ACCOUNT_AGENTS.mkdir(parents=True, exist_ok=True)
    os.chmod(A.ACCOUNT_AGENTS, 0o700)
    kept: set[str] = set()
    for filename, content in files.items():
        dest = A.ACCOUNT_AGENTS / filename
        tmp = A.ACCOUNT_AGENTS / f"{filename}.tmp"
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
        kept.add(filename)
    for old in A.ACCOUNT_AGENTS.glob("*.md"):
        if old.name not in kept:
            old.unlink()


def refresh_account_agents() -> None:
    """Copy account agents. A failure or an empty body leaves the previous copy in place."""
    session = _auth_session()
    if session is None:
        return
    token, user = session
    bundle = _get_json("https://cli-chat-proxy.grok.com/v1/subagents/bundle", token, user)
    customs = _get_json("https://grok.com/rest/user-settings", token, user)
    if bundle is None and customs is None:
        log.info("agentes de la cuenta: sin respuesta")
        return
    merged: dict[str, str] = {}
    if bundle is not None:
        merged.update(_bundle_agents(bundle))
    if customs is not None:
        merged.update(_custom_agents(customs))
    files: dict[str, str] = {}
    for name, body in merged.items():
        if _skip_agent_stem(name):
            continue
        if not body.strip():
            continue
        text = body if body.endswith("\n") else body + "\n"
        files[agent_filename(name)] = text
    if not files:
        log.info("agentes de la cuenta: vacíos")
        return
    try:
        _replace_account_agents(files)
    except OSError:
        log.info("agentes de la cuenta: no pude guardar la copia")
        return
    log.info("agentes de la cuenta: %d", len(files))


def start_account_agent_fetch() -> None:
    threading.Thread(target=refresh_account_agents, name="account-agents", daemon=True).start()


class MusicPlayer:
    """Audio-only playback from a YouTube search. One song at a time."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.sock = Path("/tmp/grok-music.sock")
        self.title = ""
        self.user_paused = False
        self._for_voice = False

    def playing(self) -> bool:
        alive = self.proc is not None and self.proc.poll() is None
        if not alive and (self.user_paused or self._for_voice):
            self.user_paused = False
            self._for_voice = False
        return alive

    def _ipc(self, command: list) -> bool:
        if not self.sock.exists():
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.4)
                sock.connect(str(self.sock))
                sock.sendall(json.dumps({"command": command}).encode() + b"\n")
        except OSError:
            return False
        return True

    def pause_for_voice(self) -> bool:
        # A song the user already paused must not start again after we speak.
        if not self.playing() or self.user_paused:
            return False
        self._for_voice = True
        return self._ipc(["set_property", "pause", True])

    def resume(self) -> None:
        if self._for_voice and self.playing() and not self.user_paused:
            self._ipc(["set_property", "pause", False])
        self._for_voice = False

    def user_pause(self) -> None:
        self.user_paused = True
        self._ipc(["set_property", "pause", True])
        log.info("música en pausa")

    def user_resume(self) -> None:
        self.user_paused = False
        if self.playing():
            self._ipc(["set_property", "pause", False])
        log.info("música sigue")

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        self.title = ""
        self.user_paused = False
        self._for_voice = False
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()

    def play(self, query: str, device: str) -> str:
        ytdlp = ROOT / ".venv" / "bin" / "yt-dlp"
        if not ytdlp.is_file():
            found = shutil.which("yt-dlp")
            ytdlp = Path(found) if found else ytdlp
        if not ytdlp.is_file() or not shutil.which("mpv"):
            return ""
        self.stop()
        try:
            found = subprocess.run(
                [
                    str(ytdlp),
                    "--no-playlist",
                    "--no-warnings",
                    "-f",
                    "bestaudio/best",
                    "--print",
                    "%(title)s",
                    "--print",
                    "url",
                    f"ytsearch1:{query}",
                ],
                capture_output=True,
                text=True,
                timeout=40,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("búsqueda de música: %s", exc)
            return ""
        lines = [line.strip() for line in (found.stdout or "").splitlines() if line.strip()]
        if found.returncode != 0 or len(lines) < 2:
            log.warning("búsqueda de música: %s", (found.stderr or "").strip()[:300])
            return ""
        title, url = lines[0], lines[1]
        self.sock.unlink(missing_ok=True)
        audio_device = device if device.startswith("alsa/") else f"alsa/{device}"
        self.proc = subprocess.Popen(
            [
                "mpv",
                "--no-video",
                "--really-quiet",
                "--no-terminal",
                f"--audio-device={audio_device}",
                f"--input-ipc-server={self.sock}",
                "--ytdl=no",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.title = title
        log.info("música: %s", title)
        return title


music = MusicPlayer()


ENROLL_VOICE_RMS = 0.004
ENROLL_MIN_VOICE = 0.8
ENROLL_SILENCE = 1.0
ENROLL_MAX_VOICE = 8.0
ENROLL_MAX_WAIT = 12.0  # name and yes/no only. A phrase waits for seguir or salir.


def _discard_queued(mic: Mic) -> None:
    """Drop audio already sitting in the capture buffer. Do not wait for new samples."""
    mic.buf = b""
    if mic.proc.poll() is not None or mic.proc.stdout is None:
        return
    fd = mic.proc.stdout.fileno()
    while True:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            return
        if not chunk:
            return


def _pause_and_clear(mic: Mic, seconds: float = 0.1) -> None:
    time.sleep(seconds)
    _discard_queued(mic)


def _tone_path(freq: float, duration: float) -> Path:
    samples = np.arange(int(A.RATE * duration)) / A.RATE
    attack = min(0.008, duration / 5)
    release = min(0.02, duration / 4)
    envelope = np.ones_like(samples)
    if attack > 0:
        envelope = np.minimum(envelope, np.clip(samples / attack, 0.0, 1.0))
    if release > 0:
        envelope = np.minimum(envelope, np.clip((duration - samples) / release, 0.0, 1.0))
    tone = np.sin(2 * np.pi * freq * samples) * envelope * 0.25
    path = Path(tempfile.mkstemp(suffix=".wav")[1])
    A.write_wav(path, tone.astype(np.float32), A.RATE)
    return path


def _play_tone(freq: float, duration: float, device: str, block: bool) -> None:
    path = _tone_path(freq, duration)

    def _run() -> None:
        try:
            A.play_on(path, device)
        finally:
            path.unlink(missing_ok=True)

    if block:
        _run()
        return
    threading.Thread(target=_run, daemon=True).start()


def _enroll_take(
    speech: Speech,
    mic: Mic,
    display: HeardDisplay,
    prompt: str,
) -> tuple[str, np.ndarray | None]:
    """One identification take. Energy closes it. The words are only shown and logged."""
    stream = speech.new_stream()
    voice: list[np.ndarray] = []
    voice_s = 0.0
    quiet_s = 0.0
    text = ""
    step = A.CHUNK_FRAMES / A.RATE
    deadline = time.monotonic() + ENROLL_MAX_WAIT
    display.update(f"DI: {prompt}   OI:", display.level)
    while not A.STOP and time.monotonic() < deadline:
        if not display.listening:
            return "", None
        block = mic.read_frames()
        if block is None:
            break
        level = A.rms(block)
        heard = (speech.feed_spanish(stream, block) or "").strip()
        if heard:
            text = heard
        display.update(f"DI: {prompt}   OI: {text}", level)
        loud = level > ENROLL_VOICE_RMS
        if loud:
            voice.append(np.array(block, copy=True))
            voice_s += step
            quiet_s = 0.0
        elif voice_s > 0:
            quiet_s += step
        if voice_s >= ENROLL_MIN_VOICE and quiet_s >= ENROLL_SILENCE:
            break
        if voice_s >= ENROLL_MAX_VOICE:
            break
    final = (speech.finish_spanish(stream) or text).strip()
    display.update(f"DI: {prompt}   OI: {final}", display.level)
    if voice_s < ENROLL_MIN_VOICE or not voice:
        return final, None
    return final, np.concatenate(voice)


def _enroll_listen(
    speech: Speech,
    mic: Mic,
    display: HeardDisplay,
    speaker: str,
    prompt: str,
) -> tuple[str, np.ndarray | None]:
    """Name and yes/no on the capture device that is already open."""
    if not mic.ensure_open():
        return "", None
    _pause_and_clear(mic, 0.1)
    _play_tone(880, 0.14, speaker, block=False)
    heard, audio = _enroll_take(speech, mic, display, prompt)
    _discard_queued(mic)
    _play_tone(494, 0.22, speaker, block=True)
    return heard, audio


def enroll_decision(text: str) -> str:
    """seguir, salir, or nothing. One short word, one edit still counts."""
    words = [
        word
        for word in A._folded_words(text).split()
        if word not in {"por", "favor", "vale", "ok"}
    ]
    if len(words) != 1:
        return ""
    word = words[0]
    best = ""
    best_distance = 2
    for target, kind in (("salir", "salir"), ("siguiente", "seguir"), ("seguir", "seguir")):
        distance = A._edit_distance(word, target)
        if distance < best_distance:
            best = kind
            best_distance = distance
    return best


def split_phrase_decision(
    heard: str, audio: np.ndarray | None
) -> tuple[str, np.ndarray | None, str]:
    """A lone seguir or salir is the decision. It is not phrase audio."""
    decision = enroll_decision(heard)
    if decision:
        return "", None, decision
    return heard, audio, ""


def enroll_followup(decision: str, has_samples: bool, has_vector: bool, text: str) -> str:
    """empty, keep, miss, or salir. An empty seguir is not a failure."""
    if decision == "salir":
        return "salir"
    if decision != "seguir":
        return "wait"
    if not has_samples and not text.strip():
        return "empty"
    if has_samples and has_vector:
        return "keep"
    return "miss"


def _capture_until_pause(
    speech: Speech,
    mic: Mic,
    display: HeardDisplay,
    prompt: str,
    keep_audio: bool,
) -> tuple[str, np.ndarray | None, bool]:
    """Read the open mic until speech stops, or until 8 seconds of voice.

    The pause freezes the buffer. It does not choose the next phrase.
    There is no 12 second give-up. False means the microphone closed.
    """
    stream = speech.new_stream()
    voice: list[np.ndarray] = []
    voice_s = 0.0
    quiet_s = 0.0
    text = ""
    step = A.CHUNK_FRAMES / A.RATE
    display.update(f"DI: {prompt}   OI:", display.level)
    while not A.STOP:
        if not display.listening:
            return "", None, False
        block = mic.read_frames()
        if block is None:
            return text, None, False
        level = A.rms(block)
        heard = (speech.feed_spanish(stream, block) or "").strip()
        if heard:
            text = heard
        display.update(f"DI: {prompt}   OI: {text}", level)
        loud = level > ENROLL_VOICE_RMS
        if loud and voice_s < ENROLL_MAX_VOICE:
            if keep_audio:
                voice.append(np.array(block, copy=True))
            voice_s += step
            quiet_s = 0.0
        elif voice_s > 0:
            quiet_s += step
        if voice_s >= ENROLL_MAX_VOICE:
            break
        if voice_s > 0 and quiet_s >= ENROLL_SILENCE:
            break
    final = (speech.finish_spanish(stream) or text).strip()
    display.update(f"DI: {prompt}   OI: {final}", display.level)
    if not keep_audio or voice_s < ENROLL_MIN_VOICE or not voice:
        return final, None, True
    return final, np.concatenate(voice), True


def _enroll_phrase(
    speech: Speech,
    mic: Mic,
    display: HeardDisplay,
    prompt: str,
) -> tuple[str, np.ndarray | None, bool]:
    """The phrase wav. Silence or 8 seconds only freezes it."""
    return _capture_until_pause(speech, mic, display, prompt, True)


def _enroll_choice(
    speech: Speech,
    mic: Mic,
    display: HeardDisplay,
    prompt: str,
) -> str:
    """A later short utterance: seguir, siguiente, or salir. Not part of the wav."""
    while not A.STOP and display.listening:
        heard, _audio, alive = _capture_until_pause(speech, mic, display, prompt, False)
        if not alive:
            return ""
        decision = enroll_decision(heard)
        if decision:
            return decision
    return ""


def enroll_result(has_samples: bool, has_vector: bool, text: str) -> str:
    """keep, retry, or silence. A success is samples plus a CampPlus vector."""
    if has_samples and has_vector:
        return "keep"
    if text.strip():
        return "retry"
    return "silence"


def log_enroll_take(text: str, has_samples: bool, label: str) -> None:
    heard = text.strip()
    if heard:
        log.info("huella: %s · %s", heard, label)
    if has_samples and not heard:
        log.info("huella: sonido guardado · %s", label)
    if not has_samples:
        log.info("huella: silencio · %s", label)


def is_presence_phrase(text: str) -> bool:
    """me escuchas, me oyes, and estás ahí stay with the selected engine."""
    folded = A._folded_words(text)
    if not folded:
        return False
    core = folded
    for filler in (" grok", "grok ", " oye ", " por favor"):
        core = core.replace(filler, " ")
    core = re.sub(r"\s+", " ", core).strip()
    return core in {"me escuchas", "me oyes", "estas ahi", "esta ahi"}


def _one_token(text: str) -> bool:
    return len(text.strip().split()) == 1


def _real_phrase(text: str) -> bool:
    parts = text.strip().split()
    if len(parts) >= 2:
        return True
    return bool(parts) and bool(re.search(r"[a-zñ]", A.fold_text(parts[0])))


def second_reading(speech: Speech, text: str, audio: np.ndarray | None) -> str:
    """Whisper reread so an English name is not lost. Test mode does not call this.

    A one-token reread, such as 1.0, does not replace a real phrase.
    """
    original = (text or "").strip()
    if not getattr(speech, "reread_label", ""):
        return original
    if audio is None or is_presence_phrase(original):
        return original
    second = speech.reread_text(audio).strip()
    if not second:
        return original
    if _one_token(second) and _real_phrase(original):
        log.info("relectura de una ficha, dejo: %s", original)
        return original
    if _phrase_order(original) and not _phrase_order(second):
        log.info("relectura no borra la orden: %s", original)
        return original
    if second != original:
        log.info("relectura: %s", second)
    return second


def _phrase_order(text: str) -> str:
    """A wake, a comando, a song, a goodbye, or a yes or no. Empty for ordinary talk."""
    if not (text or "").strip():
        return ""
    if A.opens_talk(text):
        return "wake"
    if A.command_body(text) is not None:
        return "comando"
    if A.music_command(text):
        return "music"
    if A.is_goodbye(text) or A.is_nothing_needed(text):
        return "close"
    if A.confirms(text) is not None:
        return "yesno"
    return ""


class _VoiceLane:
    def __init__(self, speech: Speech, embedding: np.ndarray | None, label: str, saved_index: int) -> None:
        self.embedding = embedding
        self.label = label
        self.saved_index = saved_index
        self.stream = speech.new_stream()
        self.audio: list[np.ndarray] = []
        self.text = ""
        self.quiet = 0.0
        self.alive = False
        self.last_heard = 0.0


class VoiceRoom:
    """One transcript per voice print. Side talk never joins another person's words."""

    def __init__(self, speech: Speech, cfg: dict) -> None:
        self.speech = speech
        self.silence_s = float(cfg["silence_seconds"])
        self.max_samples = int(float(cfg["max_utterance_seconds"]) * A.RATE)
        self.lanes: list[_VoiceLane] = []
        self.probe: list[np.ndarray] = []
        self.probe_n = 0
        self.active: _VoiceLane | None = None
        self.serial = 1

    def reset(self) -> None:
        self.lanes.clear()
        self.probe.clear()
        self.probe_n = 0
        self.active = None

    def feed(self, block: np.ndarray, loud: bool) -> list[tuple]:
        events: list[tuple] = []
        frame = np.array(block, copy=True)
        step = frame.size / A.RATE
        now = time.monotonic()
        for lane in list(self.lanes):
            if lane is self.active or not lane.last_heard:
                continue
            if now - lane.last_heard >= 10:
                log.info("descarto voz temporal: %s", lane.label)
                self.lanes.remove(lane)
                if lane is self.active:
                    self.active = None
        # The recognizer needs the whole phrase, quiet parts included.
        # Feeding only the loud peaks left "comando apaga…" as empty text.
        if self.active is None and loud:
            self.active = self._make(None, self._anon_label(), -1)
        if self.active is not None and (loud or self.active.quiet < self.silence_s):
            self._hear(self.active, frame)
            if loud:
                self.active.quiet = 0.0
                self.probe.append(frame)
                self.probe_n += int(frame.size)
                if self.probe_n >= int(0.8 * A.RATE):
                    self._identify()
            else:
                self.active.quiet += step
            for lane in self.lanes:
                if lane is not self.active:
                    lane.quiet += step
        else:
            for lane in self.lanes:
                lane.quiet += step
            self.probe.clear()
            self.probe_n = 0
        for lane in list(self.lanes):
            samples = sum(int(chunk.size) for chunk in lane.audio)
            if not lane.alive:
                continue
            if lane.quiet < self.silence_s and samples < self.max_samples:
                continue
            text = (self.speech.finish_spanish(lane.stream) or lane.text or "").strip()
            audio = np.concatenate(lane.audio) if lane.audio else None
            label, embedding, saved = lane.label, lane.embedding, lane.saved_index
            lane.alive = False
            lane.text = ""
            lane.audio = []
            lane.stream = self.speech.new_stream()
            lane.quiet = 0.0
            if lane is self.active:
                self.active = None
            if text:
                events.append(("final", label, text, audio, embedding, saved))
        parts = [f"{lane.label}: {lane.text}" for lane in self.lanes if lane.text]
        events.append(("partial", " | ".join(parts)))
        return events

    def _identify(self) -> None:
        if not self.probe or self.active is None:
            self.probe.clear()
            self.probe_n = 0
            return
        audio = np.concatenate(self.probe)
        self.probe.clear()
        self.probe_n = 0
        book = A.speakers()
        emb = book.embed(audio)
        if emb is None:
            return
        if book.self_embedding is not None and book.self_embedding.shape == emb.shape:
            if book._similar(emb, book.self_embedding) >= 0.55:
                log.info("mi voz, no la mezclo")
                self._drop(self.active)
                self.active = None
                return
        name, index, known = "", -1, 0.0
        best_person = None
        for person_index, person in enumerate(book.people):
            score = book.best_score(emb, person)
            if score > known:
                known, name, index, best_person = score, str(person["name"]), person_index, person
        other_lane = None
        other_score = 0.0
        for lane in self.lanes:
            if lane is self.active or lane.embedding is None or lane.embedding.shape != emb.shape:
                continue
            score = book._similar(emb, lane.embedding)
            if score > other_score:
                other_lane, other_score = lane, score
        changed = other_lane is not None and other_score >= 0.48 and other_score >= known
        if changed and other_lane is not None:
            log.info("cambia la voz: %s -> %s %.2f", self.active.label, other_lane.label, other_score)
            self.active.quiet = self.silence_s
            self.active = other_lane
            other_lane.embedding = 0.75 * other_lane.embedding + 0.25 * emb
            return
        if best_person is not None and known >= book.gate(best_person) and name:
            if self.active.label != name:
                log.info("voz conocida: %s %.2f", name, known)
            self.active.label = name
            self.active.saved_index = index
        if self.active.embedding is None:
            self.active.embedding = emb
        elif self.active.embedding.shape == emb.shape:
            self.active.embedding = 0.75 * self.active.embedding + 0.25 * emb

    def _drop(self, lane: _VoiceLane) -> None:
        lane.alive = False
        lane.text = ""
        lane.audio = []
        lane.stream = self.speech.new_stream()
        lane.quiet = 0.0

    def _assign(self, audio: np.ndarray) -> _VoiceLane | None:
        book = A.speakers()
        emb = book.embed(audio)
        if emb is None:
            return self.active
        if book.self_embedding is not None and book.self_embedding.shape == emb.shape:
            if book._similar(emb, book.self_embedding) >= 0.50:
                log.info("mi voz, no la mezclo")
                return None
        best_i, best_s, best_name = -1, 0.0, ""
        best_person = None
        for index, person in enumerate(book.people):
            score = book.best_score(emb, person)
            if score > best_s:
                best_s, best_i, best_name = score, index, str(person["name"])
                best_person = person
        if best_person is not None and best_s >= book.gate(best_person) and best_name:
            for lane in self.lanes:
                if lane.saved_index == best_i:
                    if lane.embedding is None or getattr(lane.embedding, "shape", None) != emb.shape:
                        lane.embedding = emb
                    else:
                        lane.embedding = 0.7 * lane.embedding + 0.3 * emb
                    return lane
            log.info("voz conocida: %s %.2f", best_name, best_s)
            return self._make(emb, best_name, best_i)
        best: _VoiceLane | None = None
        score = 0.0
        for lane in self.lanes:
            if lane.embedding is None or lane.embedding.shape != emb.shape:
                continue
            here = book._similar(emb, lane.embedding)
            if here > score:
                best, score = lane, here
        if best is not None and score >= 0.48:
            best.embedding = 0.75 * best.embedding + 0.25 * emb
            return best
        label = self._anon_label()
        log.info("voz nueva: %s", label)
        return self._make(emb, label, -1)

    def _anon_label(self) -> str:
        used = {lane.label for lane in self.lanes}
        number = 1
        while f"voz {number}" in used:
            number += 1
        return f"voz {number}"

    def _make(self, embedding: np.ndarray, label: str, saved_index: int) -> _VoiceLane:
        if len(self.lanes) >= 4:
            quiet = [lane for lane in self.lanes if lane is not self.active and not lane.alive]
            if quiet:
                self.lanes.remove(max(quiet, key=lambda lane: lane.quiet))
        lane = _VoiceLane(self.speech, embedding, label, saved_index)
        self.lanes.append(lane)
        return lane

    def _hear(self, lane: _VoiceLane, frame: np.ndarray) -> None:
        lane.alive = True
        lane.last_heard = time.monotonic()
        lane.audio.append(frame)
        total = sum(int(chunk.size) for chunk in lane.audio)
        if total > self.max_samples:
            lane.audio = lane.audio[-8:]
        text = (self.speech.feed_spanish(lane.stream, frame) or "").strip()
        if text:
            lane.text = text
