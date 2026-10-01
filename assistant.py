#!/usr/bin/env python3
"""Voice assistant for this Raspberry Pi.

Wake word on the microphone, speech to text, a Grok reply, then spoken audio.
The 3.5 mm jack is speakers only. A USB microphone is required for listening.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timedelta, timezone
import gzip
import itertools
import json
import mmap
import os
import shutil
import socket
import struct
import threading
import unicodedata
import uuid
import logging
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from urllib.parse import quote

import numpy as np

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
KWS_DIR = MODELS / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
ASR_DIR = MODELS / "sherpa-onnx-moonshine-tiny-en-int8"
TTS_DIR = MODELS / "vits-piper-en_US-amy-low"
ES_TTS_DIR = MODELS / "vits-piper-es_ES-davefx-medium-int8"
SESSIONS_PATH = Path.home() / ".config" / "grok-assistant" / "sessions.json"
ROUTER_PATH = Path.home() / ".config" / "grok-assistant" / "router-session"
VOICE_INDEX = Path.home() / ".config" / "grok-assistant" / "voice-index"
VOLUME_PATH = Path.home() / ".config" / "grok-assistant" / "volume"
SPEAKERS_PATH = Path.home() / ".config" / "grok-assistant" / "speakers.json"
RAW_DIR = Path.home() / ".config" / "grok-assistant" / "raw"
SPEAKER_MODEL = MODELS / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
VOICE_SESSIONS = Path.home() / ".grok" / "sessions" / quote(str(ROOT), safe="")
SHARED_NAME = "compartida"
SESSION_TTL = timedelta(hours=24)
ES_DIR = MODELS / "sherpa-onnx-streaming-zipformer-es-kroko-2025-08-06"
USER_CONFIG = Path.home() / ".config" / "grok-assistant" / "config.json"
RATE = 16000
CHUNK_FRAMES = 1600  # 100 ms



SYSTEM_PROMPT = (
    "You are Grok, a voice assistant on a Raspberry Pi in the room with the user. "
    "They just spoke a request after a wake phrase. "
    "Reply in plain spoken English, usually one to three short sentences. "
    "No markdown, no bullet lists, no emojis, no code, no stage directions. "
    "If you are unsure, say so briefly. "
    "When the question needs current or outside facts, search the web and answer from what you find. "
    "Do not say you have no internet. "
    "Do not claim you can see the room or control devices."
)
SYSTEM_PROMPT_ES = (
    "Eres Grok, un asistente de voz en una Raspberry Pi, en la misma habitación que el usuario. "
    "La conversación sigue abierta hasta que diga comando ok gracias, "
    "comando vale ya está, o comando cierra conversación. "
    "Una orden de voz empieza por la palabra comando. "
    "Si no empieza así, no es una orden: responde con normalidad y no escribas COMANDO:. "
    "Responde en español hablado, en una o dos frases cortas. "
    "No saludes de nuevo en cada respuesta. "
    "Sin markdown, sin listas, sin emojis y sin código. "
    "Si la pregunta necesita datos de ahora o de fuera, busca en internet y responde con lo que encuentres. "
    "No digas que no tienes internet. "
    "Si no estás seguro, dilo en una frase. "
    "No digas que puedes ver la habitación ni controlar aparatos. "
    "Puedes ejecutar, sin clave, volumen, voces, sesiones y apagado. "
    "Si el usuario pide uno de esos, responde una sola línea y nada más, con esta forma: "
    "COMANDO: subir volumen. COMANDO: bajar volumen. COMANDO: otra voz. COMANDO: voz 4. "
    "COMANDO: listar sesiones. COMANDO: crear sesion NOMBRE. COMANDO: abrir sesion NOMBRE. "
    "COMANDO: cerrar sesion. COMANDO: borrar sesion NOMBRE. "
    "COMANDO: listar agentes. COMANDO: abrir agente NOMBRE. COMANDO: crear agente NOMBRE. "
    "COMANDO: cerrar agente. COMANDO: apagar. "
    "COMANDO: pon cancion NOMBRE. COMANDO: pausa musica. COMANDO: seguir musica. "
    "COMANDO: para la musica. COMANDO: otro reconocedor. "
    "COMANDO: reconocedor kroko. COMANDO: reconocedor whisper. "
    "COMANDO: reconocedor base. COMANDO: reconocedor small. COMANDO: reconocedor canary. "
    "COMANDO: personalidad. COMANDO: personalidad vega. "
    "COMANDO: nombre. COMANDO: nombre Miguel. "
    "No pidas clave para esos."
)

# Headless Grok can hang if a tool asks for approval. Keep the tool list empty
# of anything that touches the machine. "nope" is intentionally not a real tool.
DISALLOWED_TOOLS = ",".join(
    [
        "read_file",
        "search_replace",
        "grep",
        "list_dir",
        "run_terminal_command",
        "run_terminal_cmd",
        "web_search",
        "web_fetch",
        "todo_write",
        "spawn_subagent",
        "memory_search",
        "image_gen",
        "image_edit",
        "Agent",
    ]
)

log = logging.getLogger("grok-assistant")
STOP = False


def handle_stop(signum, _frame):
    global STOP
    STOP = True
    log.info("stopping (%s)", signum)


def defaults() -> dict:
    return json.loads((ROOT / "config.json").read_text())


def load_config() -> dict:
    cfg = defaults()
    if USER_CONFIG.is_file():
        override = json.loads(USER_CONFIG.read_text())
        cfg.update(override)
    cfg["playback_device"] = str(cfg["playback_device"])
    cfg["capture_device"] = str(cfg["capture_device"])
    cfg["assistant_name"] = apply_assistant_name(str(cfg.get("assistant_name") or "Grok"))
    return cfg


def save_user_config(updates: dict) -> None:
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if USER_CONFIG.is_file():
        current = json.loads(USER_CONFIG.read_text())
    current.update(updates)
    USER_CONFIG.write_text(json.dumps(current, indent=2) + "\n")


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    x = samples.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x) + 1e-12))


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or samples.size == 0:
        return samples.astype(np.float32)
    n = max(1, int(round(samples.shape[0] * dst_rate / src_rate)))
    x_old = np.linspace(0.0, 1.0, num=samples.shape[0], endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def play_wav(path: Path, device: str) -> bool:
    result = subprocess.run(
        ["aplay", "-D", device, "-q", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        log.error("playback failed on %s: %s", device, err)
        return False
    return True


def play_on(path: Path, device: str) -> bool:
    return play_wav(path, device)


def list_alsa_devices(program: str) -> list[tuple[str, str]]:
    result = subprocess.run([program, "-l"], capture_output=True, text=True)
    found = []
    for line in (result.stdout + result.stderr).splitlines():
        match = re.search(r"card \d+: (\S+) .*device (\d+):", line)
        if match:
            name = match.group(1)
            found.append((name, f"plughw:CARD={name},DEV={match.group(2)}"))
    return found


def list_capture_devices() -> list[str]:
    return [device for _name, device in list_alsa_devices("arecord")]


def resolve_capture(cfg: dict) -> str | None:
    """The capture device the OS exposes. auto is the ALSA default."""
    if cfg["capture_device"] != "auto":
        return cfg["capture_device"]
    if not list_capture_devices():
        return None
    return "default"


def resolve_playback(cfg: dict) -> str:
    """The playback device the OS exposes. auto is the ALSA default."""
    choice = str(cfg.get("playback_device") or "auto")
    if choice != "auto":
        return choice
    return "default"


def _spawn_arecord(device: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "arecord",
            "-D",
            device,
            "-f",
            "S16_LE",
            "-r",
            str(RATE),
            "-c",
            "1",
            "-t",
            "raw",
            "-q",
            "--buffer-time",
            "200000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class Mic:
    def __init__(self, device: str):
        self.device = device
        self.proc = _spawn_arecord(device)
        self.buf = b""

    def ensure_open(self) -> bool:
        """Open this capture device once if it is not already running, and leave it open."""
        if self.proc.poll() is None:
            return True
        self.close()
        self.buf = b""
        self.proc = _spawn_arecord(self.device)
        return self.proc.poll() is None

    def read_frames(self, frames: int = CHUNK_FRAMES, timeout: float = 2.0) -> np.ndarray | None:
        need = frames * 2
        deadline = time.monotonic() + timeout
        assert self.proc.stdout is not None
        while len(self.buf) < need:
            if self.proc.poll() is not None:
                err = b""
                if self.proc.stderr:
                    err = self.proc.stderr.read() or b""
                log.error("microphone closed: %s", err.decode(errors="replace").strip())
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(self.proc.stdout.fileno(), 8192)
            if not chunk:
                return None
            self.buf += chunk
        raw, self.buf = self.buf[:need], self.buf[need:]
        return np.frombuffer(raw, dtype=np.int16)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class NoiseFloor:
    def __init__(self) -> None:
        self.values: list[float] = []

    def observe(self, value: float) -> None:
        self.values.append(value)
        if len(self.values) > 80:
            self.values.pop(0)

    def threshold(self, minimum: float) -> float:
        if len(self.values) < 8:
            return minimum
        ordered = sorted(self.values)
        median = ordered[len(ordered) // 2]
        return max(minimum, median * 2.8)


def prefer_onnx(directory: Path, prefix: str) -> Path:
    int8 = sorted(directory.glob(f"{prefix}*.int8.onnx"))
    if int8:
        return int8[0]
    full = [p for p in sorted(directory.glob(f"{prefix}*.onnx")) if ".int8." not in p.name]
    if not full:
        raise FileNotFoundError(f"no {prefix} model in {directory}")
    return full[0]


def build_keywords(cfg: dict) -> Path:
    from sherpa_onnx import text2token

    phrases = [str(p).strip() for p in cfg["wake_phrases"] if str(p).strip()]
    encoded = text2token(
        phrases,
        tokens=str(KWS_DIR / "tokens.txt"),
        tokens_type="bpe",
        bpe_model=str(KWS_DIR / "bpe.model"),
    )
    boost = float(cfg["wake_boost"])
    threshold = float(cfg["wake_threshold"])
    lines = []
    for tokens, _phrase in zip(encoded, phrases):
        # Every line is a pronunciation of the same wake phrase, "Hey Grok".
        lines.append(" ".join(tokens) + f" :{boost} #{threshold} @HEY_GROK")
    path = KWS_DIR / "hey_grok_keywords.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("wake phrases: %s", ", ".join(phrases))
    log.info("keyword tokens written to %s", path)
    return path


def load_psf(path: str) -> tuple[int, int, dict[int, bytes]]:
    raw = gzip.open(path, "rb").read() if path.endswith(".gz") else open(path, "rb").read()
    glyphs: dict[int, bytes] = {}
    if raw[:2] == b"\x36\x04":
        # PSF1. Glyph order is not Unicode: ñ and the accents live in the
        # 16-bit table that follows the bitmaps (0xFFFF ends each glyph).
        mode = raw[2]
        char_size = raw[3]
        height = char_size
        width = 8
        count = 512 if mode & 0x01 else 256
        data = raw[4 : 4 + count * char_size]
        bitmaps = [data[index * char_size : (index + 1) * char_size] for index in range(count)]
        for index, bitmap in enumerate(bitmaps):
            if index < 128:
                glyphs[index] = bitmap
        if mode & 0x02:
            table = raw[4 + count * char_size :]
            pos = 0
            for index in range(count):
                while pos + 1 < len(table):
                    value = int.from_bytes(table[pos : pos + 2], "little")
                    pos += 2
                    if value == 0xFFFF:
                        break
                    if value == 0xFFFE:
                        while pos + 1 < len(table):
                            seq = int.from_bytes(table[pos : pos + 2], "little")
                            pos += 2
                            if seq == 0xFFFF:
                                break
                        continue
                    glyphs[value] = bitmaps[index]
        else:
            for index, bitmap in enumerate(bitmaps):
                glyphs[index] = bitmap
        return width, height, glyphs
    if raw[:4] != b"\x72\xb5\x4a\x86":
        raise ValueError(f"unsupported font {path}")
    header_size, _flags, count, char_size, height, width = struct_unpack(raw[8:32])
    bitmaps = raw[header_size : header_size + count * char_size]
    table = raw[header_size + count * char_size :]
    pos = 0
    for index in range(count):
        glyphs.setdefault(index, bitmaps[index * char_size : (index + 1) * char_size])
        while pos < len(table):
            byte = table[pos]
            pos += 1
            if byte == 0xFF:
                break
            if byte == 0xFE:
                continue
            pos -= 1
            char, used = decode_utf8(table, pos)
            pos += used
            glyphs[char] = bitmaps[index * char_size : (index + 1) * char_size]
    return width, height, glyphs


def struct_unpack(blob: bytes) -> tuple[int, int, int, int, int, int]:
    import struct

    return struct.unpack("<6I", blob)


def decode_utf8(blob: bytes, pos: int) -> tuple[int, int]:
    byte = blob[pos]
    if byte < 0x80:
        return byte, 1
    if byte < 0xE0:
        return ((byte & 0x1F) << 6) | (blob[pos + 1] & 0x3F), 2
    return ((byte & 0x0F) << 12) | ((blob[pos + 1] & 0x3F) << 6) | (blob[pos + 2] & 0x3F), 3


GROK_LOGO = (
    "  ____            _    ",
    " / ___|_ __ ___ | | __",
    "| |  _| '__/ _ \\| |/ /",
    "| |_| | | | (_) |   < ",
    " \\____|_|  \\___/|_|\\_\\",
)


def os_status_line() -> str:
    pretty = "Linux"
    try:
        for raw in open("/etc/os-release", encoding="utf-8"):
            if raw.startswith("PRETTY_NAME="):
                pretty = raw.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    info = os.uname()
    release = info.release.split("+", 1)[0]
    return f"{pretty} · {release} {info.machine}"


def panel_fb() -> str:
    """The framebuffer the operating system assigned. fb0 is that console."""
    if Path("/sys/class/graphics/fb0").is_dir():
        return "fb0"
    found = sorted(Path("/sys/class/graphics").glob("fb[0-9]*"))
    return found[0].name if found else "fb0"


def _axis_limits(fd: int, code: int) -> tuple[int, int]:
    """Minimum and maximum the kernel reports for one touch axis."""
    import fcntl

    try:
        raw = fcntl.ioctl(fd, 0x80184540 + code, b"\0" * 24)
    except OSError:
        return 0, 1
    _value, lo, hi, _fuzz, _flat, _resolution = struct.unpack("6i", raw[:24])
    if hi <= lo:
        return 0, 1
    return lo, hi


def touch_device() -> str | None:
    import fcntl

    for node in sorted(Path("/dev/input").glob("event*")):
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            name = fcntl.ioctl(fd, 0x81104506, b"\0" * 256)
        except OSError:
            name = b""
        finally:
            os.close(fd)
        label = name.split(b"\0", 1)[0]
        if b"touchscreen" in label.lower():
            return str(node)
    return None


class HeardDisplay:
    """Show the Grok mark, the model, and the live microphone text."""

    FONT = "/usr/share/consolefonts/Lat15-TerminusBold16.psf.gz"
    LOGO_FONT = "/usr/share/consolefonts/Lat15-TerminusBold22x11.psf.gz"
    SMALL_FONT = "/usr/share/consolefonts/Lat15-Terminus12x6.psf.gz"

    def __init__(self, model: str) -> None:
        self.model = model
        self.mem = None
        self.width = 320
        self.height = 480
        self.stride = 640
        self.fb_name = panel_fb()
        try:
            size = open(f"/sys/class/graphics/{self.fb_name}/virtual_size", encoding="ascii").read().strip()
            self.width, self.height = (int(part) for part in size.split(","))
            self.stride = int(open(f"/sys/class/graphics/{self.fb_name}/stride", encoding="ascii").read().strip())
        except (OSError, ValueError):
            pass
        self.font_w = 8
        self.font_h = 16
        self.glyphs: dict[int, bytes] = {}
        self.small_w = 6
        self.small_h = 12
        self.small_glyphs: dict[int, bytes] = {}
        self.logo_w = 8
        self.logo_h = 16
        self.logo_glyphs: dict[int, bytes] = {}
        self.split_y = 0
        self.bar_y = 0
        self._static_ready = False
        self._static_listening = True
        self._dyn_text: str | None = None
        self._speech = None
        self.volume_line = "Volumen —"
        self.volume_row_y = -1
        self._stats_shown = ""
        self._stats_at = 0.0
        self._cpu_prev: tuple[int, int] | None = None
        self.voice_line = "Voz —"
        self.voice_row_y = -1
        self._wifi_shown = ""
        self._wifi_at = 0.0
        self.asr_name = "Kroko"
        self.os_line = os_status_line()
        self.listening = True
        self.in_conversation = False
        self.in_test = False
        self.searching = False
        self._music_mode = ""
        self._music_blocked = False
        self._listen_restore = True
        self._music_rects: tuple[tuple[str, int, int, int, int], ...] = ()
        self.logo_rect = (0, 0, 160, 80)
        self.shown = "(silencio)"
        self.level = 0.0
        self.draw_lock = threading.Lock()
        self._announce: str | None = None
        self._ann_lock = threading.Lock()
        self._last_tap = 0.0
        self.rotation = 0
        self.touch_x = (0, 1)
        self.touch_y = (0, 1)
        self.last_text = ""
        self.last_draw = 0.0
        try:
            self.font_w, self.font_h, self.glyphs = load_psf(self.FONT)
            self.small_w, self.small_h, self.small_glyphs = load_psf(self.SMALL_FONT)
            try:
                self.logo_w, self.logo_h, self.logo_glyphs = load_psf(self.LOGO_FONT)
            except (OSError, ValueError):
                self.logo_w, self.logo_h, self.logo_glyphs = self.font_w, self.font_h, self.glyphs
            fd = os.open(f"/dev/{self.fb_name}", os.O_RDWR)
            self.mem = mmap.mmap(fd, self.stride * self.height, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            log.info("pantalla %s %sx%s", self.fb_name, self.width, self.height)
        except OSError as exc:
            log.warning("live screen text unavailable: %s", exc)
        print("\n".join(GROK_LOGO), flush=True)
        print(f"modelo {self.model}", flush=True)
        log.info("modelo %s", self.model)
        self._quiet_console()
        self._draw("(silencio)", 0.0)
        self._start_touch()

    def _quiet_console(self) -> None:
        """The console cursor blinks on this panel and punches holes in the help."""
        blink = "/sys/class/graphics/fbcon/cursor_blink"
        try:
            open(blink, "w", encoding="ascii").write("0\n")
        except OSError:
            wrapper = Path.home() / ".config" / "grok-assistant" / "bin" / "sudo"
            if wrapper.is_file():
                subprocess.run(
                    [str(wrapper), "tee", blink],
                    input="0\n",
                    text=True,
                    capture_output=True,
                )
        try:
            open("/dev/tty1", "w", encoding="ascii").write("\033[?25l")
        except OSError:
            pass

    def bind(self, speech: Speech) -> None:
        self._speech = speech
        self.note_voice(speech)
        self.note_volume()
        self._draw(self.shown, self.level)

    def note_voice(self, speech: Speech) -> None:
        specs = getattr(speech, "voice_specs", None) or []
        total = max(1, len(specs))
        number = int(getattr(speech, "voice_index", 0)) + 1
        name = getattr(speech, "voice_label", "") or "sin voz"
        line = f"Voz {number}/{total}  {name}"
        if line != self.voice_line:
            self.voice_line = line
            self._static_ready = False

    def note_volume(self) -> None:
        value = current_playback()
        line = "Volumen —" if value is None else f"Volumen {value}%"
        if line != self.volume_line:
            self.volume_line = line
            self._static_ready = False

    def set_conversation(self, active: bool) -> None:
        if active == self.in_conversation:
            return
        self.in_conversation = active
        self._dyn_text = None
        self._draw(self.shown, self.level)

    def set_test(self, active: bool) -> None:
        if active == self.in_test:
            return
        self.in_test = active
        self._dyn_text = None
        self._draw(self.shown, self.level)

    def set_searching(self, active: bool) -> None:
        if active == self.searching:
            return
        self.searching = active
        self._dyn_text = None
        self._draw(self.shown, self.level)

    def take_announce(self) -> str | None:
        with self._ann_lock:
            message = self._announce
            self._announce = None
            return message

    def update(self, text: str, level: float) -> None:
        shown = text.strip() or "(silencio)"
        if (
            self.in_test
            and self.asr_name
            and shown not in {"(silencio)", "(en pausa)", "escuchando…"}
            and not shown.endswith("\n" + self.asr_name)
        ):
            shown = shown + "\n" + self.asr_name
        now = time.monotonic()
        changed = shown != self.last_text
        if changed:
            print(f"ESCUCHO: {shown}", flush=True)
            log.info("ESCUCHO: %s", shown)
            self.last_text = shown
        self.shown = shown
        self.level = level
        if self._speech is not None:
            self.note_voice(self._speech)
        music_changed = self._sync_music()
        if music_changed:
            self._static_ready = False
        if changed or music_changed or now - self.last_draw >= 0.25 or not self._static_ready:
            self._draw(shown, level)
            self.last_draw = now

    def show_commands(self, entries: list[tuple[str, str]]) -> None:
        print("AYUDA", flush=True)
        for command, note in entries:
            print(f"{command}  {note}", flush=True)
        self._draw(self.shown, self.level)

    def _logo_lines(self) -> list[str]:
        return [line.rstrip(" ") for line in GROK_LOGO]

    def _split_x(self) -> int:
        """Left edge of the command column. The logo sits in what remains."""
        logo_px = max(len(line) for line in self._logo_lines()) * self.logo_w
        command_px = 228
        left = self.width - command_px
        if left < logo_px + 6:
            left = min(self.width - 120, logo_px + 6)
        return max(self.logo_w * 8, left)

    def _draw(self, text: str, level: float) -> None:
        if self.mem is None:
            return
        self.shown = text
        self.level = level
        with self.draw_lock:
            # The command list is static. Repainting it on every mic sample
            # is what made the right column flicker.
            if not self._static_ready or self._static_listening != self.listening:
                self._paint_static()
                self._static_ready = True
                self._static_listening = self.listening
                self._dyn_text = None
            if text != self._dyn_text or self.bar_y <= 0:
                self._paint_dynamic(text, level)
                self._dyn_text = text
            else:
                self._paint_bar(level)
            self._paint_stats()
            self._paint_wifi()

    def _paint_static(self) -> None:
        bg = (8, 16, 32)
        self._fill(0, 0, self.width, self.height, bg)
        lines = self._logo_lines()
        logo_px_w = max(len(line) for line in lines) * self.logo_w
        # Pause and stop sit in the top-left corner while a song is loaded.
        left_pad = 96 if self._music_mode else 2
        area_w = self.width - left_pad
        x = left_pad + max(2, (area_w - logo_px_w) // 2)
        y = 2
        color = (120, 210, 255) if self.listening else (70, 88, 104)
        top = y
        for line in lines:
            self._text_in(x, y, line, color, self.logo_glyphs, self.logo_w, self.logo_h, self.width - 2)
            y += self.logo_h
        self.logo_rect = (
            max(0, x - 16),
            max(0, top - 8),
            min(self.width, x + logo_px_w + 16),
            min(self.height, y + 18),
        )
        y += 6
        self.volume_row_y = y
        self._text(4, y, self.volume_line, (255, 196, 80), self.width - 4)
        self._stats_shown = ""
        self._paint_stats()
        y += self.font_h
        self.voice_row_y = y
        self._text(4, y, self.voice_line, (255, 255, 255), self.width - 4)
        self._wifi_shown = ""
        self._paint_wifi()
        y += self.font_h
        self._text(4, y, self.os_line, (170, 196, 214), self.width - 4)
        y += self.font_h + 3
        reserve = self.font_h + self.small_h + 22
        cmd_bottom = self._paint_command_grid(y, self.height - reserve)
        self.split_y = min(self.height - reserve, cmd_bottom + 2)
        self._fill(0, max(0, self.split_y - 1), self.width, 1, (36, 58, 88))
        self._paint_music_buttons()

    def _paint_dynamic(self, text: str, level: float) -> None:
        bg = (8, 16, 32)
        y = self.split_y + 6
        self._fill(0, self.split_y, self.width, self.height - self.split_y, bg)
        self.bar_y = y
        self._paint_bar(level)
        y += 12
        if not self.listening:
            label, label_color = "PAUSA", (255, 150, 70)
        elif self.in_test:
            label, label_color = "PRUEBA", (120, 210, 255)
        elif self.searching:
            label, label_color = "BUSCANDO", (120, 210, 255)
        elif self.in_conversation:
            label, label_color = "CONVERSACIÓN", (120, 220, 150)
        else:
            label, label_color = "ESPERANDO", (230, 190, 80)
        self._text_in(4, y, label, label_color, self.small_glyphs, self.small_w, self.small_h, self.width - 4)
        model_color = (255, 196, 80) if self.listening else (140, 120, 70)
        model_x = self.width - 4 - len(self.model) * self.font_w
        self._text(model_x, y - 2, self.model, model_color, self.width - 2)
        y += self.small_h + 4
        cols = max(8, (self.width - 8) // self.font_w)
        room = max(1, (self.height - y - 2) // self.font_h)
        for line in self._wrap(text, cols)[:room]:
            self._text(4, y, line, (255, 255, 255), self.width - 4)
            y += self.font_h

    def _read_stats(self) -> str:
        mem = cpu = disk = "—"
        try:
            info = {}
            for line in open("/proc/meminfo", encoding="ascii"):
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])
            total = info.get("MemTotal") or 0
            avail = info.get("MemAvailable") or 0
            if total:
                mem = f"{round((total - avail) * 100 / total)}%"
        except (OSError, ValueError, IndexError):
            pass
        try:
            parts = open("/proc/stat", encoding="ascii").readline().split()
            numbers = [int(part) for part in parts[1:]]
            idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
            total_cpu = sum(numbers)
            if self._cpu_prev is not None:
                prev_idle, prev_total = self._cpu_prev
                delta = total_cpu - prev_total
                if delta > 0:
                    cpu = f"{round((delta - (idle - prev_idle)) * 100 / delta)}%"
            self._cpu_prev = (idle, total_cpu)
        except (OSError, ValueError, IndexError):
            pass
        try:
            usage = os.statvfs("/")
            blocks = usage.f_blocks or 0
            if blocks:
                disk = f"{round((blocks - usage.f_bavail) * 100 / blocks)}%"
        except OSError:
            pass
        temp = "—"
        try:
            for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
                if (zone / "type").read_text(encoding="ascii").strip() != "cpu-thermal":
                    continue
                milli = int((zone / "temp").read_text(encoding="ascii").strip())
                temp = f"{round(milli / 1000)}°"
                break
        except (OSError, ValueError):
            pass
        return f"MEM {mem}  CPU {cpu}  DISCO {disk}  TEMP {temp}"

    def _paint_stats(self) -> None:
        if self.mem is None or self.volume_row_y < 0:
            return
        now = time.monotonic()
        if self._stats_shown and now - self._stats_at < 2:
            return
        stats = self._read_stats()
        self._stats_at = now
        if stats == self._stats_shown:
            return
        self._stats_shown = stats
        self._paint_volume_tail()

    def set_asr_name(self, label: str) -> None:
        self.asr_name = label
        self._paint_volume_tail()

    def _paint_volume_tail(self) -> None:
        """Recognizer name sits between the volume and the memory figures."""
        if self.mem is None or self.volume_row_y < 0:
            return
        stats = self._stats_shown or ""
        x0 = 4 + len(self.volume_line) * self.font_w + 8
        self._fill(x0, self.volume_row_y, self.width - 4 - x0, self.font_h, (8, 16, 32))
        stats_w = len(stats) * self.small_w
        stats_x = self.width - 4 - stats_w
        if stats_x < x0:
            stats_x = x0
        label = self.asr_name or ""
        room = stats_x - x0 - 8
        if label and room >= self.small_w:
            shown = label[: max(1, room // self.small_w)]
            self._text_in(
                x0,
                self.volume_row_y + 2,
                shown,
                (186, 214, 170),
                self.small_glyphs,
                self.small_w,
                self.small_h,
                stats_x - 4,
            )
        if stats:
            self._text_in(
                stats_x,
                self.volume_row_y + 2,
                stats,
                (150, 196, 214),
                self.small_glyphs,
                self.small_w,
                self.small_h,
                self.width - 2,
            )

    def _read_wifi(self) -> tuple[str, bool]:
        if not os.path.isdir("/sys/class/net/wlan0"):
            return "Wifi ausente", False
        out = ""
        try:
            out = subprocess.run(
                ["iw", "dev", "wlan0", "link"],
                capture_output=True,
                text=True,
                timeout=1.5,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        ssid = ""
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("SSID:"):
                ssid = stripped.split(":", 1)[1].strip()
                break
        if ssid:
            address = self._wlan_ipv4()
            label = f"{ssid} {address}".strip() if address else ssid
            return self._fit_wifi(f"Wifi {label}", label, address), True
        try:
            oper = open("/sys/class/net/wlan0/operstate", encoding="ascii").read().strip()
        except OSError:
            oper = ""
        if oper == "down":
            return "Wifi apagada", False
        return "Wifi sin red", False

    def _wlan_ipv4(self) -> str:
        try:
            out = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "dev", "wlan0"],
                capture_output=True,
                text=True,
                timeout=1.5,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                return parts[parts.index("inet") + 1].split("/", 1)[0]
        return ""

    def _fit_wifi(self, full: str, short: str, address: str) -> str:
        """Keep the address on screen when the network name is long."""
        tail = self.width - 8 - (4 + len(self.voice_line) * self.font_w + 8)
        budget = max(8, tail // max(1, self.small_w))
        if len(full) <= budget:
            return full
        if len(short) <= budget:
            return short
        if address and len(address) < budget:
            room = budget - len(address) - 1
            name = short[: room].rstrip() if room > 0 else ""
            return f"{name} {address}".strip()
        return short[:budget]

    def _paint_wifi(self) -> None:
        if self.mem is None or self.voice_row_y < 0:
            return
        now = time.monotonic()
        if self._wifi_shown and now - self._wifi_at < 2:
            return
        text, connected = self._read_wifi()
        self._wifi_at = now
        if text == self._wifi_shown:
            return
        self._wifi_shown = text
        x0 = 4 + len(self.voice_line) * self.font_w + 8
        self._fill(x0, self.voice_row_y, self.width - 4 - x0, self.font_h, (8, 16, 32))
        width = len(text) * self.small_w
        x = max(x0, self.width - 4 - width)
        color = (120, 220, 150) if connected else (255, 170, 90)
        self._text_in(
            x,
            self.voice_row_y + 2,
            text,
            color,
            self.small_glyphs,
            self.small_w,
            self.small_h,
            self.width - 2,
        )

    def _paint_bar(self, level: float) -> None:
        if self.bar_y <= 0:
            return
        width = self.width - 8
        self._fill(4, self.bar_y, width, 5, (18, 32, 48))
        span = int(min(level / 0.15, 1.0) * width)
        color = (80, 220, 120) if self.listening else (150, 70, 60)
        if span > 0:
            self._fill(4, self.bar_y, span, 5, color)

    def _paint_command_grid(self, y: int, max_y: int) -> int:
        entries = help_lines()
        rest = []
        for command, note in entries:
            if note in {"talk", "header"}:
                color = (180, 220, 255) if note == "header" else (255, 255, 255)
                self._text_in(4, y, command, color, self.small_glyphs, self.small_w, self.small_h, self.width - 4)
                y += self.small_h + (2 if note == "talk" else 1)
                continue
            if note == "group":
                head, _, tail = command.partition(" ")
                self._text_in(4, y, head, (180, 220, 255), self.small_glyphs, self.small_w, self.small_h, self.width - 4)
                self._text_in(
                    4 + (len(head) + 1) * self.small_w,
                    y,
                    tail,
                    (255, 255, 255),
                    self.small_glyphs,
                    self.small_w,
                    self.small_h,
                    self.width - 4,
                )
                y += self.small_h
                continue
            if note in {"header", "wide"} or "¦" in command:
                color = (180, 220, 255) if note == "header" else (255, 255, 255)
                self._text_in(4, y, command, color, self.small_glyphs, self.small_w, self.small_h, self.width - 4)
                y += self.small_h
                continue
            rest.append((command, note))
        entries = rest
        usable = max_y - y
        rows_for_two = (len(entries) + 1) // 2
        cols = 2 if rows_for_two * self.small_h <= usable else 3
        gap = 4
        col_w = (self.width - 8 - gap * (cols - 1)) // cols
        xs = tuple(4 + i * (col_w + gap) for i in range(cols))
        row_h = self.small_h
        index = 0
        while index < len(entries) and y + self.small_h <= max_y:
            for origin in xs:
                if index >= len(entries):
                    break
                command, note = entries[index]
                index += 1
                limit = origin + col_w
                cmd_w = len(command) * self.small_w
                self._text_in(origin, y, command, (255, 255, 255), self.small_glyphs, self.small_w, self.small_h, limit)
                if cmd_w + 6 + len(note) * self.small_w <= col_w:
                    self._text_in(
                        origin + cmd_w + 6,
                        y,
                        note,
                        (150, 170, 190),
                        self.small_glyphs,
                        self.small_w,
                        self.small_h,
                        limit,
                    )
            y += row_h
        return y

    def _wrap(self, text: str, cols: int) -> list[str]:
        pieces = text.split("\n")
        lines: list[str] = []
        for piece in pieces:
            lines.extend(self._wrap_piece(piece, cols))
        return lines or [""]

    def _wrap_piece(self, text: str, cols: int) -> list[str]:
        words = text.split()
        if not words:
            return [""]
        lines: list[str] = []
        current = ""
        for word in words:
            while len(word) > cols:
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:cols])
                word = word[cols:]
            if not word:
                continue
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= cols:
                current += " " + word
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    def _text(self, x: int, y: int, text: str, color: tuple[int, int, int], max_x: int | None = None) -> None:
        self._text_in(x, y, text, color, self.glyphs, self.font_w, self.font_h, max_x)

    def _text_in(
        self,
        x: int,
        y: int,
        text: str,
        color: tuple[int, int, int],
        glyphs: dict[int, bytes],
        font_w: int,
        font_h: int,
        max_x: int | None = None,
    ) -> None:
        limit = self.width if max_x is None else max_x
        cursor = x
        for char in text:
            if cursor + font_w > limit:
                break
            glyph = glyphs.get(ord(char)) or glyphs.get(ord("?"), b"")
            self._glyph(cursor, y, glyph, color, font_w, font_h)
            cursor += font_w

    def _glyph(
        self,
        x: int,
        y: int,
        glyph: bytes,
        color: tuple[int, int, int],
        font_w: int | None = None,
        font_h: int | None = None,
    ) -> None:
        font_w = self.font_w if font_w is None else font_w
        font_h = self.font_h if font_h is None else font_h
        row_bytes = (font_w + 7) // 8
        for row in range(font_h):
            if y + row >= self.height:
                break
            start = row * row_bytes
            bits = glyph[start : start + row_bytes]
            for col in range(font_w):
                byte = bits[col // 8] if col // 8 < len(bits) else 0
                if byte & (0x80 >> (col % 8)):
                    self._pixel(x + col, y + row, color)

    def _fill(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        pixel = self._pack(color)
        for row in range(max(0, y), min(self.height, y + h)):
            begin = row * self.stride + max(0, x) * 2
            end = row * self.stride + min(self.width, x + w) * 2
            self.mem[begin:end] = pixel * ((end - begin) // 2)

    def _pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if x < 0 or y < 0 or x >= self.width or y >= self.height:
            return
        offset = y * self.stride + x * 2
        self.mem[offset : offset + 2] = self._pack(color)

    def _stroke(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        if w < 2 or h < 2:
            return
        self._fill(x, y, w, 1, color)
        self._fill(x, y + h - 1, w, 1, color)
        self._fill(x, y, 1, h, color)
        self._fill(x + w - 1, y, 1, h, color)

    def _pack(self, color: tuple[int, int, int]) -> bytes:
        red, green, blue = color
        value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
        return value.to_bytes(2, "little")

    def _map_touch(self, raw_x: int, raw_y: int) -> tuple[int, int]:
        """Map a touch into the framebuffer using the ranges the kernel reports."""

        def axis(raw: int, lo: int, hi: int, size: int) -> int:
            span = hi - lo
            if span == 0:
                return 0
            pos = (raw - lo) / span
            pos = min(1.0, max(0.0, pos))
            return int(pos * (size - 1))

        x_lo, x_hi = self.touch_x
        y_lo, y_hi = self.touch_y
        return axis(raw_x, x_lo, x_hi, self.width), axis(raw_y, y_lo, y_hi, self.height)

    def _sync_music(self) -> bool:
        """Shut the mic while a song is audible. Open it again when paused or stopped."""
        playing = music.playing()
        if playing and not music.user_paused:
            mode = "play"
        elif playing:
            mode = "paused"
        else:
            mode = ""
        changed = mode != self._music_mode
        self._music_mode = mode
        if mode == "play":
            if not self._music_blocked:
                self._listen_restore = self.listening
                self._music_blocked = True
                self.listening = False
                log.info("música sonando, micrófono cerrado")
                changed = True
        elif self._music_blocked:
            self._music_blocked = False
            self.listening = self._listen_restore
            log.info("música en pausa o parada, micrófono %s", "abierto" if self.listening else "sigue cerrado")
            changed = True
        return changed

    def _paint_music_buttons(self) -> None:
        if self._music_mode not in {"play", "paused"}:
            self._music_rects = ()
            return
        pause_label = "PAUSA" if self._music_mode == "play" else "SEGUIR"
        pause_color = (120, 72, 24) if self._music_mode == "play" else (20, 72, 48)
        boxes = (
            (pause_label, "pause" if self._music_mode == "play" else "resume", pause_color, 2),
            ("STOP", "stop", (92, 32, 36), 30),
        )
        rects = []
        for label, key, color, top in boxes:
            x, w, h = 2, 88, 24
            self._fill(x, top, w, h, color)
            text_x = x + max(4, (w - len(label) * self.small_w) // 2)
            self._text_in(
                text_x,
                top + 6,
                label,
                (255, 255, 255),
                self.small_glyphs,
                self.small_w,
                self.small_h,
                x + w - 2,
            )
            rects.append((key, max(0, x - 8), max(0, top - 6), min(self.width, x + w + 12), top + h + 8))
        self._music_rects = tuple(rects)

    def _consider_tap(self, x: int, y: int, raw_x: int, raw_y: int) -> None:
        now = time.monotonic()
        if now - self._last_tap < 0.5:
            return
        for key, x0, y0, x1, y1 in self._music_rects:
            if x0 <= x < x1 and y0 <= y < y1:
                self._last_tap = now
                log.info("toque música %s", key)
                if key == "pause":
                    music.user_pause()
                elif key == "resume":
                    music.user_resume()
                elif key == "stop":
                    music.stop()
                self._sync_music()
                self._static_ready = False
                self._draw(self.shown, self.level)
                return
        x0, y0, x1, y1 = self.logo_rect
        inside = x0 <= x < x1 and y0 <= y < y1
        log.info("toque x=%s y=%s raw=%s,%s logo=%s", x, y, raw_x, raw_y, "sí" if inside else "no")
        if not inside or self._music_mode == "play":
            return
        self._last_tap = now
        if not self._music_blocked:
            self.listening = not self.listening
            self._listen_restore = self.listening
        message = "Pausa." if not self.listening else "Escucho."
        with self._ann_lock:
            self._announce = message
        log.info("escucha %s", "en pausa" if not self.listening else "activa")
        self._draw(self.shown, self.level)

    def _start_touch(self) -> None:
        path = touch_device()
        if not path:
            log.warning("no hay pantalla táctil")
            return
        threading.Thread(target=self._touch_loop, args=(path,), name="touch", daemon=True).start()

    def _touch_loop(self, path: str) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            log.warning("táctil no disponible: %s", exc)
            return
        self.touch_x = _axis_limits(fd, 0)
        self.touch_y = _axis_limits(fd, 1)
        log.info("táctil %s ejes %s %s", path, self.touch_x, self.touch_y)
        event = struct.Struct("qqHHi")
        raw_x = raw_y = 0
        have_point = False
        pressed = False
        while not STOP:
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if not ready:
                    continue
                try:
                    data = os.read(fd, event.size * 32)
                except BlockingIOError:
                    continue
                if not data:
                    continue
            except Exception as exc:
                log.warning("táctil: %s", exc)
                time.sleep(0.5)
                continue
            try:
                events = [event.unpack_from(data, offset) for offset in range(0, len(data) - event.size + 1, event.size)]
            except struct.error as exc:
                log.warning("táctil: %s", exc)
                continue
            for _sec, _usec, kind, code, value in events:
                if kind == 3 and code == 0:
                    raw_x = value
                    have_point = True
                elif kind == 3 and code == 1:
                    raw_y = value
                    have_point = True
                elif kind == 3 and code == 24 and have_point:
                    down = value > 20
                    if down and not pressed:
                        pressed = True
                        x, y = self._map_touch(raw_x, raw_y)
                        self._consider_tap(x, y, raw_x, raw_y)
                    elif not down:
                        pressed = False
                elif kind == 1 and code in (330, 272) and have_point:
                    if value == 1 and not pressed:
                        pressed = True
                        x, y = self._map_touch(raw_x, raw_y)
                        self._consider_tap(x, y, raw_x, raw_y)
                    elif value == 0:
                        pressed = False
        os.close(fd)


# The recognizer rarely writes "grok". These are the words it uses instead.
GROK_NAME = (
    r"(?:gro[cfgk]\w*|grog\w*|gorf\w*|"
    r"dro[cfgk]\w*|drog\w*|"
    r"cro[cfgk]\w*|croc\w*|crock\w*|"
    r"bro[cfgk]\w*|brock\w*|"
    r"tro[cfgk]\w*|trog\w*)"
)
# "despierta" often arrives as "es cierta" or "de cierta".
WAKE_VERB = r"(?:despiert\w*|desper\w*|despierte|de cierta|es cierta|de cierra|es cierra)"
WAKE_VERB_STRICT = r"(?:despiert\w*|desper\w*|despierte|de cierta|de cierra)"


def fold_text(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def is_despierta_grok(text: str) -> bool:
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded:
        return False
    has_name = re.search(rf"\b(?:{GROK_NAME}|rock|group)\b", folded) is not None
    has_strict = re.search(rf"\b{WAKE_VERB_STRICT}\b", folded) is not None
    has_loose = re.search(r"\b(?:es cierta|es cierra)\b", folded) is not None
    if has_name and (has_strict or has_loose):
        return True
    # "Grok" is not a Spanish word, so the recognizer often drops it.
    # The loose mishear "es cierta" needs the name, or any short noise wakes it.
    return bool(has_strict and len(folded.split()) <= 5)


def _folded_words(text: str) -> str:
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


# How the user calls the assistant. "Grok" is the default. A Spanish name is
# easier for the Spanish ear than that English word.
ASSISTANT_NAME = "Grok"
_NAME_STOP = {
    "comando", "comandos", "salir", "cancela", "cancelar", "si", "no", "vale",
    "gracias", "adios", "hola", "ola", "ok", "ayuda", "prueba", "personalidad",
    "volumen", "musica", "cancion", "agente", "sesion", "administrador",
    "identifica", "voz", "nombre",
}


def clean_assistant_name(text: str) -> str:
    """A short call-name, such as Miguel. Empty when it is not a name."""
    raw = (text or "").strip().strip(".,;:!?¿¡\"'")
    parts = [part.strip(".,;:!?¿¡\"'") for part in raw.split() if part.strip(".,;:!?¿¡\"'")]
    folded = [_folded_words(part) for part in parts]
    if folded and folded[0] in {"hola", "ola", "jola"}:
        parts = parts[1:]
        folded = folded[1:]
    if not parts or len(parts) > 2 or any(not word for word in folded):
        return ""
    if any(len(word) < 2 or len(word) > 16 or word in _NAME_STOP for word in folded):
        return ""
    if any(not re.fullmatch(r"[a-zñ]+", word) for word in folded):
        return ""
    if folded == ["grok"]:
        return "Grok"
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def apply_assistant_name(text: str) -> str:
    """Remember the call-name. A blank or unusable value stays Grok."""
    global ASSISTANT_NAME
    cleaned = clean_assistant_name(text) or "Grok"
    ASSISTANT_NAME = cleaned
    return cleaned


def assistant_call() -> str:
    """The wake the footprint asks for. Default is hola grok."""
    if _folded_words(ASSISTANT_NAME) == "grok":
        return "hola grok"
    return "hola " + ASSISTANT_NAME


def _custom_name_words() -> list[str]:
    words = _folded_words(ASSISTANT_NAME).split()
    if not words or words == ["grok"]:
        return []
    return words


def _span_close(heard: list[str], wanted: list[str]) -> bool:
    if len(heard) != len(wanted):
        return False
    return all(_edit_distance(left, right) <= 1 for left, right in zip(heard, wanted))


def _custom_hello(folded: str) -> bool:
    wanted = _custom_name_words()
    if not wanted or not folded:
        return False
    words = folded.split()
    hellos = {"hola", "ola", "jola", "pola", "bola"}
    size = len(wanted)
    for index, word in enumerate(words):
        if word in hellos and _span_close(words[index + 1:index + 1 + size], wanted):
            return True
    return False


def _custom_there(folded: str) -> bool:
    wanted = _custom_name_words()
    if not wanted or not folded:
        return False
    if not re.search(r"\b(?:estas|esta)\s+(?:ahi|alli|hay|ai|ay)\b", folded):
        return False
    words = folded.split()
    size = len(wanted)
    return any(_span_close(words[index:index + size], wanted) for index in range(len(words) - size + 1))


# "hola grok" often arrives as "hola grop", "pola grove" or one word "holagro".
HELLO_GROK = (
    rf"(?:(?:hola|ola|jola|pola|bola)\s+(?:{GROK_NAME}|grop\w*|agro\w*|gro\b|grove\w*|grov\w*|group\w*|crove\w*|holagro\w*|holagrok\w*)"
    r"|holagro\w*|holagrok\w*|holaagro\w*|olagro\w*|polagro\w*)"
)


_COMANDO_WORD = {"comando", "comandos", "comado", "komando", "comand"}


def find_hola_grok(text: str) -> bool:
    """True if 'hola grok' or 'hola' plus the chosen name appears."""
    folded = _folded_words(text)
    if folded and re.search(HELLO_GROK, folded):
        return True
    return _custom_hello(folded)


def find_grok_there(text: str) -> bool:
    """'Grok, ¿estás ahí?' or the same shape with the chosen name."""
    folded = _folded_words(text)
    if not folded:
        return False
    if _custom_there(folded):
        return True
    there = r"(?:estas|esta)\s+(?:ahi|alli|hay|ai|ay)"
    if re.search(rf"(?:{GROK_NAME}).{{0,30}}{there}", folded):
        return True
    return bool(re.search(rf"{there}.{{0,30}}(?:{GROK_NAME})", folded))


def opens_talk(text: str) -> bool:
    """A short greeting opens the talk, even if 'grok' was not heard."""
    if find_hola_grok(text) or find_grok_there(text):
        return True
    folded = _folded_words(text)
    words = folded.split()
    if not words or len(words) > 4:
        return False
    if words[0] in {"hola", "ola", "jola", "pola", "bola"}:
        return True
    if re.search(r"\b(?:estas|esta|estoy)\b", folded) and re.search(
        r"\b(?:ahi|alli|hay|ai|ay)\b", folded
    ):
        return True
    return bool(re.search(GROK_NAME, folded))


def wake_reply(text: str) -> str:
    folded = _folded_words(text)
    there = bool(
        re.search(r"\b(?:estas|esta|estoy)\b", folded)
        and re.search(r"\b(?:ahi|alli|hay|ai|ay)\b", folded)
    )
    if (find_grok_there(text) or there) and not find_hola_grok(text):
        return "Sí, aquí estoy."
    return "Hola."


def split_hola_grok(text: str) -> str | None:
    """The words after 'hola grok'. None when this is not that wake."""
    folded = _folded_words(text)
    if not folded:
        return None
    match = re.search(rf"(?:{HELLO_GROK})(?:\s+|$)", folded)
    if not match:
        return None
    return folded[match.end() :].strip()


def command_body(text: str) -> str | None:
    """The words after a leading 'comando'. None if this is not a command.

    Every order starts with that word. 'hola grok' is the only phrase that
    opens a conversation without it.
    """
    raw = (text or "").strip()
    words = _folded_words(raw).split()
    if not words or words[0] not in _COMANDO_WORD:
        return None
    return " ".join(raw.split()[1:]).strip(" .,;:!?¿¡")


def split_ok_grok(text: str) -> str | None:
    """If the phrase starts with 'ok grok' or 'hola grok', return the rest."""
    folded = _folded_words(text)
    match = re.match(
        rf"(?:ok|okay|oc|vale|{HELLO_GROK})\s*",
        folded,
    )
    if not match:
        return None
    if not re.match(rf"(?:ok|okay|oc|vale)\s+{GROK_NAME}\b", folded) and not re.match(HELLO_GROK, folded):
        return None
    return folded[match.end() :].strip()


def is_admin_request(text: str) -> bool:
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if "admin" not in folded and "administrador" not in folded:
        return False
    return re.search(r"\b(haz|has|as|modo|activa|activar|quiero|pon|entra)\b", folded) is not None


def grok_needs_admin(answer: str) -> bool:
    """True when Grok says the task cannot be done from the normal conversation."""
    folded = fold_text(answer)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded)
    if "desde aqui" in folded:
        return True
    return re.search(r"no puedo (hacerlo|hacer eso|hacer esto)", folded) is not None


# While the key is expected, only digits matter. These are the ways the
# recognizer writes the numbers, including the usual one-letter mistakes.
_DIGIT_WORDS = (
    ("quinientos", "500"),
    ("quince", "15"),
    ("catorce", "14"),
    ("dieciseis", "16"),
    ("trece", "13"),
    ("doce", "12"),
    ("once", "11"),
    ("cuatro", "4"),
    ("quatro", "4"),
    ("siete", "7"),
    ("nueve", "9"),
    ("cinco", "5"),
    ("sinco", "5"),
    ("cinko", "5"),
    ("zinco", "5"),
    ("sinko", "5"),
    ("seis", "6"),
    ("ocho", "8"),
    ("tres", "3"),
    ("cero", "0"),
    ("zero", "0"),
    ("sero", "0"),
    ("diez", "10"),
    ("cien", "100"),
    ("uno", "1"),
    ("umo", "1"),
    ("uma", "1"),
    ("hun", "1"),
    ("dos", "2"),
    ("tos", "2"),
    ("mil", "1000"),
    ("un", "1"),
)


def heard_digits(text: str) -> str:
    """Turn a spoken attempt at a number into digits. 'umo cinco' becomes '15'."""
    folded = fold_text(text)
    folded = re.sub(r"[^a-z0-9ñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    parts: list[str] = []
    for token in folded.split():
        if token.isdigit():
            parts.append(token)
            continue
        rest = token
        found = False
        while rest:
            hit = next((pair for pair in _DIGIT_WORDS if rest.startswith(pair[0])), None)
            if hit is None:
                break
            parts.append(hit[1])
            rest = rest[len(hit[0]) :]
            found = True
        if not found:
            parts.extend(ch for ch in token if ch.isdigit())
    if not parts:
        return ""
    if any(int(part) >= 100 for part in parts):
        return str(sum(int(part) for part in parts))
    return "".join(parts)


def _near_digits(heard: str, key: str) -> bool:
    if not heard or not key:
        return False
    if key in heard or (heard in key and len(heard) >= len(key) - 1):
        return True
    if abs(len(heard) - len(key)) > 1:
        return False
    previous = list(range(len(key) + 1))
    for i, left in enumerate(heard, 1):
        current = [i]
        for j, right in enumerate(key, 1):
            current.append(min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1] <= 1


def is_passphrase(text: str, cfg: dict) -> bool:
    """True if the configured admin key is heard, allowing misheard digits."""
    key = re.sub(r"[^a-z0-9]", "", fold_text(str(cfg.get("admin_key") or "")))
    if not key:
        return False
    if key.isdigit():
        return _near_digits(heard_digits(text), key)
    folded = fold_text(text)
    folded = re.sub(r"[^a-z0-9\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    return key in folded.split()


def is_stt_test(text: str) -> bool:
    folded = _folded_words(text)
    if not folded or len(folded.split()) > 4:
        return False
    return folded in {"prueba", "test", "modo prueba", "modo test", "comando prueba", "comando test"}


def is_leave_test(text: str) -> bool:
    body = command_body(text)
    folded = _folded_words(body if body is not None else text)
    if not folded or len(folded.split()) > 4:
        return False
    return folded in {"salir", "salir del test", "salir de la prueba", "sal de la prueba", "sal del test"}


def is_goodbye(text: str) -> bool:
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded:
        return False
    # "cierra conversación" ends the talk. It is not "cerrar sesión".
    # A long remark that only mentions the words is not a goodbye: closing
    # it made the next sentence open a new conversation.
    if len(folded.split()) <= 8 and re.search(r"\bconversacion\b", folded) and re.search(
        r"\b(cierra|cerrar|acaba|acabar|termina|terminar|sal|salir|corta|cortar)\b", folded
    ):
        return True
    if len(folded.split()) > 8:
        return False
    words = folded.split()
    # A lone "gracias" or "vale" ends the talk. A couple of fillers may sit with it.
    if len(words) <= 3 and any(word in {"vale", "gracias"} for word in words):
        extra = [word for word in words if word not in {"vale", "gracias", "muchas", "grok", "eh", "pues", "ok", "okay"}]
        if not extra:
            return True
    if re.fullmatch(r"(ok|okay|vale|listo)(\s+grok)?\s+gracias", folded):
        return True
    if "vale ya esta" in folded and len(folded.split()) <= 6:
        return True
    if re.search(r"\b(adios|hasta luego|hasta pronto|hasta ahora|nos vemos|chao|chau|ciao)\b", folded):
        return True
    return folded in {
        "eso es todo",
        "ya esta",
        "ya esta bien",
        "se acabo",
        "cuando quieras",
        "cuando quieras seguimos",
        "nada mas",
        "basta",
        "dejalo",
        "lo dejamos",
        "terminamos",
    }


def goodbye_reply(text: str) -> str:
    folded = fold_text(text)
    if "gracias" in folded:
        return "De nada."
    if re.search(r"\bvale\b", folded):
        return "Vale."
    return "Adiós."


def wants_more_detail(phrase: str, answer: str) -> bool:
    """Only after a real question with a real answer, not after a short closer."""
    if is_goodbye(phrase) or is_goodbye(answer) or is_nothing_needed(phrase):
        return False
    if is_own_words(phrase) or is_own_words(answer):
        return False
    phrase_words = re.sub(r"[^a-zñ\s]", " ", fold_text(phrase)).split()
    answer_words = re.sub(r"[^a-zñ\s]", " ", fold_text(answer)).split()
    if len(phrase_words) < 4 or len(answer_words) < 12:
        return False
    return bool(
        "?" in phrase
        or re.search(r"\b(que|cual|como|cuando|donde|quien|por que|cuanto|explic|cuent)\b", fold_text(phrase))
    )


def is_nothing_needed(text: str) -> bool:
    """True when the reply to 'qué necesitas' is that they need nothing."""
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded:
        return False
    patterns = (
        r"nada",
        r"nada mas",
        r"nada gracias",
        r"no nada",
        r"no necesito nada",
        r"no necesito nada mas",
        r"ya no necesito nada",
        r"no quiero nada",
        r"no quiero nada mas",
        r"no me hace falta nada",
        r"no hace falta",
        r"no hace falta nada",
    )
    return any(re.fullmatch(pattern, folded) for pattern in patterns)


def is_real_phrase(text: str) -> bool:
    folded = fold_text(text)
    words = folded.split()
    letters = re.sub(r"[^a-zñ]", "", folded)
    if len(letters) < 3 or not words:
        return False
    noise = {"de", "la", "el", "y", "a", "en", "un", "se", "lo", "que"}
    return not (len(words) == 1 and words[0] in noise)


def _strip_custom_wake(raw: str) -> str | None:
    """Words after 'hola NAME' when NAME is the chosen call-name. None otherwise."""
    wanted = _custom_name_words()
    if not wanted:
        return None
    folded = _folded_words(raw).split()
    hellos = {"hola", "ola", "jola", "pola", "bola"}
    size = len(wanted)
    if len(folded) < 1 + size or folded[0] not in hellos:
        return None
    if not _span_close(folded[1:1 + size], wanted):
        return None
    original = raw.split()
    if len(original) < 1 + size:
        return ""
    return " ".join(original[1 + size:]).strip(" .,;:!?¿¡")


def raw_after_wake(text: str) -> str:
    """The words after the wake phrase, taken from the recognizer text."""
    raw = text.strip()
    custom = _strip_custom_wake(raw)
    if custom is not None:
        return custom
    wake_name = r"gro[cfgk]\w*|grog\w*|gorf\w*|dro[cfgk]\w*|drog\w*|cro[cfgk]\w*|croc\w*|crock\w*|bro[cfgk]\w*|brock\w*|tro[cfgk]\w*|trog\w*|rock|group"
    patterns = (
        rf"(?i)^(?:ok|okay|oc|vale)\b[\s,]*\b(?:{wake_name})\b[\s,.:;!?¿¡]*",
        rf"(?i)^(?:hola|ola|jola)\b[\s,]*\b(?:{wake_name}|grop\w*|agro\w*|gro|holagro\w*|holagrok\w*)\b[\s,.:;!?¿¡]*",
        r"(?i)^(?:holagro\w*|holagrok\w*|holaagro\w*|olagro\w*)\b[\s,.:;!?¿¡]*",
        rf"(?i)^(?:despiert\w*|desper\w*|de cierta|es cierta|de cierra|es cierra)\b[\s,]*\b(?:{wake_name})\b[\s,.:;!?¿¡]*",
        rf"(?i)^(?:despiert\w*|desper\w*|de cierta|es cierta|de cierra|es cierra)\b[\s,.:;!?¿¡]*",
    )
    for pattern in patterns:
        match = re.match(pattern, raw)
        if match and match.end() > 0:
            return raw[match.end() :].strip(" .,;:!?¿¡")
    return strip_despierta(raw)


def strip_despierta(text: str) -> str:
    folded = fold_text(text)
    folded = re.sub(rf"\b{WAKE_VERB}\b", " ", folded, count=1)
    folded = re.sub(rf"\b(?:{GROK_NAME}|rock|group)\b", " ", folded, count=1)
    return re.sub(r"\s+", " ", folded).strip(" .,;:!?¿¡")


class Speech:
    def __init__(self, cfg: dict):
        import sherpa_onnx

        threads = int(cfg["num_threads"])
        self.language = str(cfg.get("language") or "en")
        self.kws = None
        self.asr = None
        self.tts = None
        self.spanish = None
        self.reread = None
        self.reread_label = ""
        self._reread_live = False
        self._reread_id = ""
        self._sherpa = sherpa_onnx
        self.speed = float(cfg["tts_speed"])
        if self.language == "es":
            self._init_spanish(cfg, threads)
            return
        keywords = build_keywords(cfg)
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(KWS_DIR / "tokens.txt"),
            encoder=str(prefer_onnx(KWS_DIR, "encoder")),
            decoder=str(prefer_onnx(KWS_DIR, "decoder")),
            joiner=str(prefer_onnx(KWS_DIR, "joiner")),
            keywords_file=str(keywords),
            num_threads=min(2, threads),
            provider="cpu",
            keywords_score=float(cfg["wake_boost"]),
            keywords_threshold=float(cfg["wake_threshold"]),
            num_trailing_blanks=1,
            max_active_paths=8,
        )
        self.asr = sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=str(ASR_DIR / "preprocess.onnx"),
            encoder=str(ASR_DIR / "encode.int8.onnx"),
            uncached_decoder=str(ASR_DIR / "uncached_decode.int8.onnx"),
            cached_decoder=str(ASR_DIR / "cached_decode.int8.onnx"),
            tokens=str(ASR_DIR / "tokens.txt"),
            num_threads=threads,
            debug=False,
        )
        model = TTS_DIR / "en_US-amy-low.onnx"
        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model),
                    tokens=str(TTS_DIR / "tokens.txt"),
                    data_dir=str(TTS_DIR / "espeak-ng-data"),
                ),
                num_threads=min(2, threads),
                provider="cpu",
                debug=False,
            ),
            max_num_sentences=1,
        )
        if not tts_config.validate():
            raise RuntimeError("text-to-speech model config is invalid")
        self.tts = sherpa_onnx.OfflineTts(tts_config)

    def _init_spanish(self, cfg: dict, threads: int) -> None:
        if not ES_DIR.is_dir():
            raise SystemExit(f"Spanish speech model is missing: {ES_DIR}")
        self._threads = threads
        self.asr_kind = "streaming"
        self.asr_id = "kroko"
        self.asr_label = "Kroko"
        self.spanish = None
        self.offline = None
        self._load_spanish_voice(threads)
        saved = self._normalize_asr_id(self._saved_asr_id())
        try:
            score_missing_engines(self)
        except Exception:
            log.exception("no pude puntuar los motores")
        self._select_asr(choose_live_asr(saved))
        log_listen_motor(self)
        self.log_reread_line()

    def _load_spanish_voice(self, threads: int) -> None:
        self.voice_specs = [
            (ES_TTS_DIR, "Dave, España", 0),
            (MODELS / "vits-piper-es_ES-sharvard-medium-int8", "Sharvard, España, hablante 0", 0),
            (MODELS / "vits-piper-es_ES-sharvard-medium-int8", "Sharvard, España, hablante 1", 1),
            (MODELS / "vits-piper-es_ES-carlfm-x_low-int8", "Carlfm, España", 0),
            (MODELS / "vits-piper-es_ES-glados-medium-int8", "Glados, España", 0),
            (MODELS / "vits-piper-es_ES-miro-high-int8", "Miro, España", 0),
            (MODELS / "vits-piper-es_MX-ald-medium-int8", "Ald, México", 0),
            (MODELS / "vits-piper-es_MX-claude-high-int8", "Claude, México", 0),
            (MODELS / "vits-piper-es_AR-daniela-high-int8", "Daniela, Argentina", 0),
        ]
        self.voice_specs = [item for item in self.voice_specs if item[0].is_dir()]
        self.voice_cache: dict[str, object] = {}
        self.voice_index = 0
        self.voice_threads = threads
        start = 0
        try:
            start = int(VOICE_INDEX.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            start = 0
        if self.voice_specs:
            start %= len(self.voice_specs)
        if not self.voice_specs or not self._use_voice(start):
            log.warning("Spanish Piper voice missing, using espeak")
            return
        log.info("voz activa: %s", self.voice_label)

    def _use_voice(self, index: int) -> bool:
        directory, label, sid = self.voice_specs[index]
        key = str(directory)
        if key not in self.voice_cache:
            voice = self._open_piper(directory, self.voice_threads)
            if voice is None:
                return False
            self.voice_cache[key] = voice
        self.voice_index = index
        self.tts = self.voice_cache[key]
        self.voice_label = label
        self.voice_sid = sid
        return True

    def _open_piper(self, directory: Path, threads: int):
        model = next(directory.glob("*.onnx"), None)
        tokens = directory / "tokens.txt"
        data_dir = directory / "espeak-ng-data"
        if not data_dir.is_dir():
            data_dir = TTS_DIR / "espeak-ng-data"
        if model is None or not tokens.is_file() or not data_dir.is_dir():
            return None
        tts_config = self._sherpa.OfflineTtsConfig(
            model=self._sherpa.OfflineTtsModelConfig(
                vits=self._sherpa.OfflineTtsVitsModelConfig(
                    model=str(model),
                    tokens=str(tokens),
                    data_dir=str(data_dir),
                ),
                num_threads=min(2, threads),
                provider="cpu",
                debug=False,
            ),
            max_num_sentences=1,
        )
        if not tts_config.validate():
            return None
        return self._sherpa.OfflineTts(tts_config)

    def _save_voice_index(self) -> None:
        VOICE_INDEX.parent.mkdir(parents=True, exist_ok=True)
        VOICE_INDEX.write_text(f"{self.voice_index}\n", encoding="utf-8")

    def next_voice(self) -> str:
        specs = getattr(self, "voice_specs", [])
        if len(specs) < 2:
            return ""
        for step in range(1, len(specs) + 1):
            index = (self.voice_index + step) % len(specs)
            if self._use_voice(index):
                self._save_voice_index()
                log.info("voz: %s", self.voice_label)
                return self.voice_label
        return ""

    def use_voice_number(self, number: int) -> str:
        """Select the voice by the number shown on screen, starting at 1."""
        specs = getattr(self, "voice_specs", [])
        if number < 1 or number > len(specs):
            return ""
        if not self._use_voice(number - 1):
            return ""
        self._save_voice_index()
        log.info("voz %s: %s", number, self.voice_label)
        return self.voice_label

    def _saved_asr_id(self) -> str:
        path = Path.home() / ".config" / "grok-assistant" / "asr-index"
        try:
            return path.read_text(encoding="utf-8").strip() or "kroko"
        except OSError:
            return "kroko"

    def _normalize_asr_id(self, asr_id: str) -> str:
        return {"whisper-tiny": "whisper", "whisper-base": "base"}.get(asr_id or "", asr_id or "kroko")

    def _save_asr_id(self, asr_id: str) -> None:
        path = Path.home() / ".config" / "grok-assistant" / "asr-index"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(asr_id + "\n", encoding="utf-8")

    def _release_asr(self) -> None:
        import gc

        self.spanish = None
        self.offline = None
        gc.collect()

    def _select_asr(self, asr_id: str) -> str:
        choices = available_asrs()
        chosen = next((item for item in choices if item["id"] == asr_id), None)
        if chosen is None:
            chosen = next((item for item in choices if item["id"] == "kroko"), choices[0])
        self._release_asr()
        try:
            self._open_asr(chosen)
        except Exception:
            log.exception("no pude cargar el reconocedor %s", chosen["id"])
            if chosen["id"] != "kroko":
                return self._select_asr("kroko")
            raise
        self.asr_kind = chosen["kind"]
        self.asr_id = chosen["id"]
        self.asr_label = chosen["label"]
        self._save_asr_id(chosen["id"])
        log.info("reconocedor activo: %s", self.asr_label)
        self._ensure_reread()
        return self.asr_label

    def _load_chosen(self, chosen: dict) -> None:
        """Open one engine for scoring. Does not write asr-index."""
        self._release_asr()
        self._open_asr(chosen)
        self.asr_kind = chosen["kind"]
        self.asr_id = chosen["id"]
        self.asr_label = chosen["label"]

    def _open_asr(self, chosen: dict) -> None:
        kind = chosen["kind"]
        threads = self._threads
        if kind == "streaming":
            hotwords = ES_DIR / "hotwords.txt"
            hotwords.write_text("▁de s pi er ta ▁g ro k\n▁de s pi er ta ▁g ro c k\n", encoding="utf-8")
            self.spanish = self._sherpa.OnlineRecognizer.from_transducer(
                tokens=str(ES_DIR / "tokens.txt"),
                encoder=str(ES_DIR / "encoder.onnx"),
                decoder=str(ES_DIR / "decoder.onnx"),
                joiner=str(ES_DIR / "joiner.onnx"),
                num_threads=threads,
                sample_rate=RATE,
                decoding_method="modified_beam_search",
                max_active_paths=8,
                hotwords_file=str(hotwords),
                hotwords_score=3.0,
                provider="cpu",
                enable_endpoint_detection=True,
                rule2_min_trailing_silence=0.8,
            )
            return
        if kind == "whisper":
            encoder, decoder, tokens = _whisper_files(chosen["dir"], chosen["prefix"])
            self.offline = self._sherpa.OfflineRecognizer.from_whisper(
                encoder=str(encoder),
                decoder=str(decoder),
                tokens=str(tokens),
                language="es",
                task="transcribe",
                num_threads=threads,
                provider="cpu",
            )
            return
        directory = chosen["dir"]
        encoder = next(directory.glob("encoder*.onnx"))
        decoder = next(directory.glob("decoder*.onnx"))
        self.offline = self._sherpa.OfflineRecognizer.from_nemo_canary(
            encoder=str(encoder),
            decoder=str(decoder),
            tokens=str(directory / "tokens.txt"),
            src_lang="es",
            tgt_lang="es",
            num_threads=threads,
            provider="cpu",
        )

    def cycle_asr(self) -> str:
        choices = available_asrs()
        ids = [item["id"] for item in choices]
        index = ids.index(self.asr_id) if self.asr_id in ids else -1
        return self._select_asr(ids[(index + 1) % len(ids)])

    def new_stream(self):
        if self.asr_kind == "streaming":
            return self.spanish.create_stream()
        return {"chunks": [], "text": ""}

    def reset_stream(self, stream) -> None:
        if self.asr_kind == "streaming":
            self.spanish.reset(stream)
            return
        stream["chunks"] = []
        stream["text"] = ""

    def buffered(self, stream) -> bool:
        return self.asr_kind != "streaming" and bool(stream.get("chunks"))

    def keep_audio(self, stream, loud: bool, quiet: float) -> bool:
        """Offline models only keep the words, plus a short tail. Not the long pause."""
        if self.asr_kind == "streaming":
            return True
        if loud:
            return True
        return self.buffered(stream) and quiet < 0.6

    def partial_spanish(self, stream) -> str:
        if self.asr_kind == "streaming":
            return self.spanish.get_result(stream) or ""
        return stream.get("text") or ""

    def finish_spanish(self, stream) -> str:
        if self.asr_kind == "streaming":
            return (self.spanish.get_result(stream) or "").strip()
        chunks = stream.get("chunks") or []
        if not chunks:
            return (stream.get("text") or "").strip()
        audio = np.concatenate(chunks).astype(np.float32) / 32768.0
        if audio.size < int(0.25 * RATE):
            stream["chunks"] = []
            return ""
        off = self.offline.create_stream()
        off.accept_waveform(RATE, audio)
        self.offline.decode_stream(off)
        text = (off.result.text or "").strip()
        stream["text"] = text
        stream["chunks"] = []
        log.info("reconocedor %s: %s", self.asr_label, text)
        return text

    def feed_spanish(self, stream, samples: np.ndarray) -> str:
        if self.asr_kind != "streaming":
            stream["chunks"].append(np.array(samples, copy=True))
            return stream.get("text") or ""
        stream.accept_waveform(RATE, samples.astype(np.float32) / 32768.0)
        while self.spanish.is_ready(stream):
            self.spanish.decode_stream(stream)
        return self.spanish.get_result(stream)

    def hear_keyword(self, stream, samples: np.ndarray) -> str:
        stream.accept_waveform(RATE, samples.astype(np.float32) / 32768.0)
        heard = ""
        while self.kws.is_ready(stream):
            self.kws.decode_stream(stream)
            result = self.kws.get_result(stream)
            text = result if isinstance(result, str) else str(getattr(result, "keyword", result))
            if is_hey_grok(text):
                heard = text
                self.kws.reset_stream(stream)
            elif text:
                log.info("ignored partial wake match: %s", text)
                self.kws.reset_stream(stream)
        return heard

    def transcribe(self, samples: np.ndarray, sample_rate: int = RATE) -> str:
        audio = np.asarray(samples, dtype=np.float32)
        if audio.size and float(np.max(np.abs(audio))) > 2.0:
            audio = audio / 32768.0
        audio = resample(audio, sample_rate, RATE)
        if audio.size == 0:
            return ""
        stream = self.asr.create_stream()
        stream.accept_waveform(RATE, audio)
        self.asr.decode_stream(stream)
        return (stream.result.text or "").strip()

    def transcribe_wav(self, path: Path) -> str:
        """Read one enrollment wav with the engine that is loaded now."""
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            frames = handle.readframes(handle.getnframes())
        if not frames:
            return ""
        pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1)
        if rate != RATE:
            pcm = resample(pcm, rate, RATE)
        if pcm.size == 0:
            return ""
        if self.asr_kind == "streaming" and self.spanish is not None:
            stream = self.spanish.create_stream()
            stream.accept_waveform(RATE, pcm)
            stream.accept_waveform(RATE, np.zeros(int(0.4 * RATE), dtype=np.float32))
            stream.input_finished()
            while self.spanish.is_ready(stream):
                self.spanish.decode_stream(stream)
            return (self.spanish.get_result(stream) or "").strip()
        if self.offline is None:
            return ""
        off = self.offline.create_stream()
        off.accept_waveform(RATE, pcm)
        self.offline.decode_stream(off)
        return (off.result.text or "").strip()

    def _reread_choice(self) -> dict | None:
        """Whisper base, or Whisper pequeño when base is not installed."""
        ready = {item["id"]: item for item in available_asrs()}
        return ready.get("base") or ready.get("whisper")

    def _release_reread(self) -> None:
        if self.reread is None and not self._reread_live:
            return
        self.reread = None
        self._reread_live = False
        self._reread_id = ""
        import gc

        gc.collect()

    def _ensure_reread(self) -> None:
        """Keep one Whisper model for the second reading, apart from the live engine when it differs."""
        chosen = self._reread_choice()
        if chosen is None:
            self._release_reread()
            self.reread_label = ""
            return
        self.reread_label = str(chosen["label"])
        if self.asr_id == chosen["id"] and self.offline is not None:
            if self.reread is not None:
                self.reread = None
                import gc

                gc.collect()
            self._reread_live = True
            self._reread_id = chosen["id"]
            return
        if self.reread is not None and self._reread_id == chosen["id"]:
            self._reread_live = False
            return
        self._release_reread()
        self.reread_label = str(chosen["label"])
        try:
            encoder, decoder, tokens = _whisper_files(chosen["dir"], chosen["prefix"])
            self.reread = self._sherpa.OfflineRecognizer.from_whisper(
                encoder=str(encoder),
                decoder=str(decoder),
                tokens=str(tokens),
                language="es",
                task="transcribe",
                num_threads=self._threads,
                provider="cpu",
            )
        except Exception:
            log.exception("no pude cargar la relectura")
            self.reread = None
            self.reread_label = ""
            self._reread_id = ""
            self._reread_live = False
            return
        self._reread_id = chosen["id"]
        self._reread_live = False

    def log_reread_line(self) -> None:
        if not self.reread_label:
            return
        log.info(
            "fuera de la prueba, releo cada frase con %s para guardar los nombres en inglés",
            self.reread_label,
        )

    def reread_text(self, samples: np.ndarray | None) -> str:
        engine = self.reread
        if engine is None and self._reread_live:
            engine = self.offline
        if engine is None or samples is None:
            return ""
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return ""
        if float(np.max(np.abs(audio))) > 2.0:
            audio = audio / 32768.0
        if audio.size < int(0.2 * RATE):
            return ""
        try:
            off = engine.create_stream()
            off.accept_waveform(RATE, audio)
            engine.decode_stream(off)
            return (off.result.text or "").strip()
        except Exception:
            log.exception("relectura")
            return ""

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        sid = getattr(self, "voice_sid", 0)
        engine = self.tts
        try:
            gen = self._sherpa.GenerationConfig()
            gen.sid = sid
            gen.speed = self.speed
            gen.silence_scale = 0.2
            audio = engine.generate(text, gen)
        except TypeError:
            audio = engine.generate(text, sid=sid, speed=self.speed)
        samples = np.array(audio.samples, dtype=np.float32)
        return samples, int(audio.sample_rate)


def speakable(text: str) -> str:
    cleaned = re.sub(r"```.*?```", " ", text, flags=re.S)
    cleaned = re.sub(r"[#*_`>|\[\]]", " ", cleaned)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 700:
        cleaned = cleaned[:700].rsplit(" ", 1)[0] + ". I'll stop there."
    return cleaned


def asr_catalog() -> list[dict]:
    """Fixed local engines. Audio stays on the Pi. small is never auto-selected."""
    return [
        {"id": "kroko", "label": "Kroko", "kind": "streaming"},
        {
            "id": "whisper",
            "label": "Whisper pequeño",
            "kind": "whisper",
            "dir": MODELS / "sherpa-onnx-whisper-tiny",
            "prefix": "tiny",
        },
        {
            "id": "base",
            "label": "Whisper base",
            "kind": "whisper",
            "dir": MODELS / "sherpa-onnx-whisper-base",
            "prefix": "base",
        },
        {
            "id": "small",
            "label": "Whisper small",
            "kind": "whisper",
            "dir": MODELS / "sherpa-onnx-whisper-small",
            "prefix": "small",
        },
        {
            "id": "canary",
            "label": "Canary",
            "kind": "canary",
            "dir": MODELS / "sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8",
        },
    ]


def available_asrs() -> list[dict]:
    """Recognizers that are actually on disk and can be tried."""
    ready = []
    for spec in asr_catalog():
        if spec["kind"] == "streaming":
            ready.append(spec)
            continue
        directory = spec["dir"]
        if spec["kind"] == "whisper":
            encoder, decoder, tokens = _whisper_files(directory, spec["prefix"])
            if encoder.is_file() and decoder.is_file() and tokens.is_file():
                ready.append(spec)
            continue
        if directory.is_dir() and any(directory.glob("encoder*.onnx")) and any(directory.glob("decoder*.onnx")) and (directory / "tokens.txt").is_file():
            ready.append(spec)
    return ready


def _whisper_files(directory: Path, prefix: str) -> tuple[Path, Path, Path]:
    encoder = directory / f"{prefix}-encoder.int8.onnx"
    if not encoder.is_file():
        encoder = directory / f"{prefix}-encoder.onnx"
    decoder = directory / f"{prefix}-decoder.int8.onnx"
    if not decoder.is_file():
        decoder = directory / f"{prefix}-decoder.onnx"
    return encoder, decoder, directory / f"{prefix}-tokens.txt"


def asr_choice(text: str) -> str | None:
    """'next' or an engine id when the phrase asks to change the recognizer."""
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded or "reconoce" not in folded or len(folded.split()) > 10:
        return None
    named = (
        ("whisper small", "small"),
        ("whisper base", "base"),
        ("whisper pequeno", "whisper"),
        ("whisper pequenyo", "whisper"),
        ("whisper tiny", "whisper"),
        ("small", "small"),
        ("whisper", "whisper"),
        ("wisper", "whisper"),
        ("uisper", "whisper"),
        ("visper", "whisper"),
        ("guisper", "whisper"),
        ("pequeno", "whisper"),
        ("pequenyo", "whisper"),
        ("tiny", "whisper"),
        ("base", "base"),
        ("canario", "canary"),
        ("canari", "canary"),
        ("canary", "canary"),
        ("kroko", "kroko"),
        ("croco", "kroko"),
    )
    for alias, asr_id in named:
        if re.search(rf"\b{re.escape(alias)}\b", folded):
            return asr_id
    if re.search(r"\b(otro|siguiente|cambia|cambiar)\b", folded):
        return "next"
    return None


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    prev = list(range(len(right) + 1))
    for i, ca in enumerate(left, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(right, 1):
            value = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb))
            cur.append(value)
            if value < row_min:
                row_min = value
        if row_min > 1 and abs(len(left) - len(right)) > 1:
            return row_min
        prev = cur
    return prev[-1]


def phrase_hit(expected: str, heard: str) -> bool:
    """True when the heard phrase matches the stored one.

    Three words or fewer must all appear, in order. Four or more may miss one.
    One inserted, deleted, or changed character still counts. Extra words may be skipped.
    """
    exp = _folded_words(expected).split()
    got = _folded_words(heard).split()
    if not exp:
        return False
    allow = 1 if len(exp) >= 4 else 0
    n, m = len(exp), len(got)
    missing = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        missing[i][0] = i
        for j in range(1, m + 1):
            best = missing[i][j - 1]
            best = min(best, missing[i - 1][j] + 1)
            if _edit_distance(exp[i - 1], got[j - 1]) <= 1:
                best = min(best, missing[i - 1][j - 1])
            missing[i][j] = best
    return missing[n][m] <= allow


def combined_percentages() -> dict[str, int]:
    """One percentage per engine: hits of every person over totals, rounded."""
    hits: dict[str, int] = {}
    totals: dict[str, int] = {}
    for person in speakers().people:
        scores = person.get("scores") or {}
        if not isinstance(scores, dict):
            continue
        for engine_id, row in scores.items():
            if not isinstance(row, dict):
                continue
            total = int(row.get("total") or 0)
            if total <= 0:
                continue
            hits[engine_id] = hits.get(engine_id, 0) + int(row.get("hits") or 0)
            totals[engine_id] = totals.get(engine_id, 0) + total
    return {engine_id: int(round(100.0 * hits[engine_id] / totals[engine_id])) for engine_id in totals}


def choose_live_asr(saved_id: str) -> str:
    """Highest combined score among installed engines, except Whisper small.

    A tie keeps the engine already in use. No scores means Kroko.
    """
    ready = [item["id"] for item in available_asrs()]
    eligible = [engine_id for engine_id in ready if engine_id != "small"] or ready
    perc = combined_percentages()
    ranked = [(engine_id, perc[engine_id]) for engine_id in eligible if engine_id in perc]
    if not ranked:
        return "kroko" if "kroko" in ready else eligible[0]
    best = max(score for _, score in ranked)
    tied = [engine_id for engine_id, score in ranked if score == best]
    if saved_id in tied:
        return saved_id
    return tied[0]


def log_listen_motor(speech: Speech) -> None:
    perc = combined_percentages()
    if not perc:
        log.info("motor escucha: Kroko. huellas combinadas: aún no hay porcentajes.")
        return
    score = perc.get(speech.asr_id)
    if score is None:
        log.info("motor escucha: %s. huellas combinadas.", speech.asr_label)
        return
    log.info("motor escucha: %s (%s%%). huellas combinadas.", speech.asr_label, score)


def score_missing_engines(speech: Speech) -> None:
    """Transcribe saved wavs with each installed engine that has no score yet.

    One model at a time, so a small Pi is not asked to hold every engine.
    """
    book = speakers()
    speech._release_reread()
    if not any(person.get("raw") for person in book.people):
        return
    installed = {item["id"]: item for item in available_asrs()}
    for spec in asr_catalog():
        chosen = installed.get(spec["id"])
        if chosen is None:
            continue
        pending = []
        for person in book.people:
            raw = person.get("raw") or []
            if not raw:
                continue
            scores = person.get("scores") if isinstance(person.get("scores"), dict) else {}
            if spec["id"] not in scores:
                pending.append(person)
        if not pending:
            continue
        log.info("puntuando %s", spec["label"])
        try:
            speech._load_chosen(chosen)
        except Exception:
            log.exception("no puntúo %s", spec["id"])
            speech._release_asr()
            continue
        for person in pending:
            hits = 0
            total = 0
            for item in person.get("raw") or []:
                if not isinstance(item, dict):
                    continue
                path = RAW_DIR / str(item.get("file") or "")
                if not path.is_file():
                    continue
                total += 1
                heard = speech.transcribe_wav(path)
                if phrase_hit(str(item.get("phrase") or ""), heard):
                    hits += 1
                log.info("puntuación %s «%s»: %s", spec["id"], item.get("phrase") or "", heard)
            person.setdefault("scores", {})
            person["scores"][spec["id"]] = {"hits": hits, "total": total}
        book._save()
        speech._release_asr()


def _hello_lines(language: str) -> tuple[str, ...]:
    """Short boot lines, one per restart, from a file so the list can grow."""
    name = "hellos-es.txt" if language == "es" else "hellos-en.txt"
    fallback = (
        ("Hola, ya estoy de vuelta.",)
        if language == "es"
        else ("Hello, I am back.",)
    )
    try:
        raw = (ROOT / name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return fallback
    lines = tuple(line.strip() for line in raw if line.strip() and not line.startswith("#"))
    return lines or fallback


def boot_hello(language: str) -> str:
    """A different short hello each time the assistant starts."""
    lines = _hello_lines(language)
    path = Path.home() / ".config" / "grok-assistant" / "hello-index"
    try:
        index = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        index = 0
    line = lines[index % len(lines)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{index + 1}\n", encoding="utf-8")
    return line


_last_said = ""
_spoken_at = 0.0


def note_spoken(text: str) -> None:
    global _last_said, _spoken_at
    _last_said = text
    _spoken_at = time.monotonic()


def is_own_words(text: str) -> bool:
    """True when the recognizer is repeating what this assistant just said."""
    if not _last_said or not text or time.monotonic() - _spoken_at > 20:
        return False
    said = re.sub(r"[^a-zñ\s]", " ", fold_text(_last_said))
    heard = re.sub(r"[^a-zñ\s]", " ", fold_text(text))
    said_words = [word for word in said.split() if len(word) > 2]
    heard_words = [word for word in heard.split() if len(word) > 2]
    if not said_words or not heard_words:
        return False
    if heard in said or said in heard:
        return True
    common = sum(1 for word in heard_words if word in said_words)
    return common / len(heard_words) >= 0.6


def next_wait_line(language: str) -> str:
    """One short spoken filler while a cloud answer is on its way."""
    name = "waits-es.txt" if language == "es" else "waits-en.txt"
    fallback = "Un momento, lo miro." if language == "es" else "One moment, I'll look."
    try:
        raw = (ROOT / name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return fallback
    lines = [line.strip() for line in raw if line.strip() and not line.startswith("#")]
    if not lines:
        return fallback
    path = Path.home() / ".config" / "grok-assistant" / "wait-index"
    try:
        index = int(path.read_text(encoding="ascii").strip() or "0")
    except (OSError, ValueError):
        index = 0
    line = lines[index % len(lines)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(index + 1), encoding="ascii")
    return line


def begin_cloud_wait(speech: Speech, speaker: str, display: HeardDisplay) -> None:
    """Show that the question went to the cloud, and say a short filler."""
    display.set_searching(True)
    display.update("Buscando en la nube", display.level)
    speak(speech, next_wait_line(speech.language), speaker)


def speak(speech: Speech, text: str, device: str) -> bool:
    spoken = speakable(text)
    if not spoken:
        return False
    log.info("speaking: %s", spoken)
    note_spoken(spoken)
    paused = music.pause_for_voice()
    try:
        return _speak_now(speech, spoken, device)
    finally:
        if paused:
            music.resume()


def _speak_now(speech: Speech, spoken: str, device: str) -> bool:
    if speech.language == "es" and speech.tts is None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = Path(handle.name)
        try:
            result = subprocess.run(
                ["espeak-ng", "-v", "es", "-s", "145", "-w", str(path), spoken],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log.error("espeak failed: %s", (result.stderr or "").strip())
                return False
            return play_on(path, device)
        finally:
            path.unlink(missing_ok=True)
    samples, sample_rate = speech.synthesize(spoken)
    if samples.size == 0:
        log.error("text to speech produced no audio")
        return False
    speakers().remember_self(samples, sample_rate)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        path = Path(handle.name)
    try:
        write_wav(path, samples, sample_rate)
        return play_on(path, device)
    finally:
        path.unlink(missing_ok=True)


def service_touch(display: HeardDisplay, speech: Speech, speaker: str) -> None:
    message = display.take_announce()
    if message:
        speak(speech, message, speaker)


def current_grok_model(cfg: dict) -> str:
    chosen = str(cfg.get("grok_model") or "").strip()
    if chosen:
        return chosen
    try:
        result = subprocess.run(
            ["grok", "models"],
            capture_output=True,
            text=True,
            timeout=20,
            env=grok_env(),
        )
        for line in (result.stdout or "").splitlines():
            if "Default model:" in line:
                name = line.split(":", 1)[1].strip()
                if name:
                    return name
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "grok-4.7"


def grok_env() -> dict:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GROK_"):
            env.pop(key, None)
    env["HOME"] = str(Path.home())
    env["PATH"] = str(Path.home() / ".grok" / "bin") + os.pathsep + env.get("PATH", "")
    return env


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_sessions() -> dict:
    blank = {"active": SHARED_NAME, "shared": {}, "named": {}}
    try:
        data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = blank
    data.setdefault("active", SHARED_NAME)
    data.setdefault("shared", {})
    data.setdefault("named", {})
    return data


def save_sessions(data: dict) -> None:
    SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(SESSIONS_PATH, 0o600)


def session_label(name: str) -> str:
    return name.replace("-", " ")


def clean_session_name(text: str) -> str:
    folded = fold_text(text)
    folded = re.sub(r"[^a-z0-9\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded.replace(" ", "-")


def delete_grok_session(session_id: str) -> None:
    if not session_id:
        return
    subprocess.run(
        ["grok", "sessions", "delete", session_id],
        capture_output=True,
        text=True,
        timeout=30,
        env=grok_env(),
    )
    folder = VOICE_SESSIONS / session_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def fresh_entry() -> dict:
    return {"id": str(uuid.uuid4()), "started": iso_now(), "live": False}


def shared_expired(entry: dict) -> bool:
    started = parse_iso(str(entry.get("started") or ""))
    if not entry.get("id") or started is None:
        return True
    return now_utc() - started >= SESSION_TTL


def ensure_shared(data: dict) -> dict:
    shared = data.setdefault("shared", {})
    if shared_expired(shared):
        delete_grok_session(str(shared.get("id") or ""))
        shared.clear()
        shared.update(fresh_entry())
        save_sessions(data)
    return shared


def active_entry(data: dict) -> tuple[dict, str]:
    active = str(data.get("active") or SHARED_NAME)
    named = data.setdefault("named", {})
    if active != SHARED_NAME and active in named:
        return named[active], active
    data["active"] = SHARED_NAME
    return ensure_shared(data), SHARED_NAME


def known_session_ids(data: dict) -> set[str]:
    ids = set()
    shared_id = str(data.get("shared", {}).get("id") or "")
    if shared_id:
        ids.add(shared_id)
    try:
        router = json.loads(ROUTER_PATH.read_text(encoding="utf-8"))
        router_id = str(router.get("id") or "")
        if router_id:
            ids.add(router_id)
    except (OSError, json.JSONDecodeError):
        pass
    for entry in data.get("named", {}).values():
        sid = str(entry.get("id") or "")
        if sid:
            ids.add(sid)
    return ids


def cleanup_orphan_sessions() -> None:
    data = load_sessions()
    keep = known_session_ids(data)
    if not VOICE_SESSIONS.is_dir():
        return
    for folder in VOICE_SESSIONS.iterdir():
        if folder.is_dir() and folder.name not in keep:
            delete_grok_session(folder.name)


def bind_session(cmd: list[str]) -> dict:
    data = load_sessions()
    entry, name = active_entry(data)
    cmd.extend(session_cli_args(entry))
    log.info("sesión %s (%s)", session_label(name), "continúa" if entry.get("live") else "se estrena")
    return entry


def commit_session(entry: dict, raw: str) -> str:
    answer, session = answer_and_session(grok_payloads(raw or ""))
    remember_session(entry, session)
    return answer


def session_cli_args(entry: dict) -> list[str]:
    if entry.get("live"):
        return ["--resume", str(entry["id"])]
    return ["--session-id", str(entry["id"])]


def remember_session(entry: dict, returned_id: str) -> None:
    data = load_sessions()
    if returned_id:
        entry["id"] = returned_id
    entry["live"] = True
    active, _name = active_entry(data)
    active.clear()
    active.update(entry)
    save_sessions(data)


def describe_sessions() -> str:
    data = load_sessions()
    ensure_shared(data)
    save_sessions(data)
    active = str(data.get("active") or SHARED_NAME)
    started = parse_iso(str(data["shared"].get("started") or ""))
    hours = 24
    if started is not None:
        left = SESSION_TTL - (now_utc() - started)
        hours = max(1, int(left.total_seconds() // 3600))
    parts = [f"La sesión compartida está {'activa' if active == SHARED_NAME else 'en espera'} y se reinicia en unas {hours} horas"]
    names = sorted(data.get("named", {}))
    if not names:
        parts.append("No hay otras sesiones")
    else:
        spoken = ", ".join(session_label(name) for name in names)
        parts.append("Sesiones guardadas sin caducidad: " + spoken)
        if active in names:
            parts.append("Ahora estás en " + session_label(active))
    return ". ".join(parts) + "."


AGENTS_DIR = Path.home() / ".grok" / "agents"
BUNDLED_AGENTS = Path.home() / ".grok" / "bundled" / "agents"
ACCOUNT_AGENTS = Path.home() / ".config" / "grok-assistant" / "account-agents"
AGENT_STATE = Path.home() / ".config" / "grok-assistant" / "agent-state.json"
ACTIVE_AGENT = Path.home() / ".config" / "grok-assistant" / "active-agent"


def active_agent() -> str:
    try:
        return ACTIVE_AGENT.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_active_agent(name: str) -> None:
    ACTIVE_AGENT.parent.mkdir(parents=True, exist_ok=True)
    if name:
        ACTIVE_AGENT.write_text(name + "\n", encoding="utf-8")
    elif ACTIVE_AGENT.exists():
        ACTIVE_AGENT.unlink()


def write_agent(name: str) -> None:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    label = name.replace("-", " ")
    text = (
        f"---\nname: {name}\n"
        f"description: Agente de voz {label}. Recuerda fechas, sitios y listas, y busca en internet.\n"
        "prompt_mode: full\nmodel: inherit\npermission_mode: default\n---\n\n"
        f"Eres el agente «{label}» del asistente de voz.\n"
        "Puedes recordar y guardar lo que te pidan: fechas, eventos, dónde están las cosas y listas.\n"
        "Cuando te pidan recordar algo, confírmalo en una frase corta.\n"
        "Si la pregunta necesita datos de ahora o de fuera, busca en internet y responde con lo que encuentres.\n"
        "Responde en español hablado, en una o dos frases cortas.\n"
        "Sin markdown, sin listas, sin emojis y sin código.\n"
    )
    (AGENTS_DIR / f"{name}.md").write_text(text, encoding="utf-8")


def agent_tail(folded: str) -> str:
    match = re.search(r"\bagente\b\s+(.+)$", folded)
    if not match:
        return ""
    tail = re.sub(r"^(?:el|la|un|una|de|del)\s+", "", match.group(1).strip())
    return clean_session_name(tail)


def agent_command(text: str) -> tuple[str, str] | None:
    """List, open, create, or leave a Grok agent. None if this is not about agents."""
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded or not re.search(r"\bagentes?\b", folded) or len(folded.split()) > 8:
        return None
    if re.search(r"\b(listar|lista|listame|liste)\b", folded) or folded in {"agentes", "los agentes"}:
        return ("list", "")
    if re.search(r"\b(cerrar|cierra|quita|quitar)\b", folded):
        return ("close", "")
    if re.search(r"\b(crear|crea|nuevo|nueva)\b", folded):
        return ("create", agent_tail(folded))
    if re.search(r"\b(hablar|habla|cargar|carga|abrir|abre|iniciar|inicia)\b", folded):
        return ("open", agent_tail(folded))
    return None


def session_name_from(folded: str) -> str:
    match = re.search(r"\bsesion(?:es)?\b\s+(.+)$", folded)
    if not match:
        return ""
    tail = match.group(1).strip()
    tail = re.sub(r"^(?:(?:el|la|un|una|de|del|mi)\s+)+", "", tail)
    return clean_session_name(tail)


def session_command(text: str) -> tuple[str, str] | None:
    """Session orders, also inside a longer phrase such as 'quiero cerrar sesion de casa'."""
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded or "sesion" not in folded or re.search(r"\bagente\b", folded):
        return None
    name = session_name_from(folded)
    if re.search(r"\b(borrar|borra|elimina|eliminar)\b", folded):
        return ("delete", name)
    if re.search(r"\b(crear|crea|nueva|nuevo)\b", folded):
        return ("create", name)
    if re.search(r"\b(abrir|abre|abreme)\b", folded):
        return ("open", name)
    if re.search(r"\b(listar|lista|listame|liste)\b", folded):
        return ("list", "")
    if re.search(r"\b(cerrar|cierra)\b", folded):
        return ("close", "")
    return None


def canonicalize(phrase: str, pending: str) -> str:
    """Ask Grok to map a loose spoken phrase onto a fixed command."""
    if not phrase.strip():
        return phrase
    hint = {
        "create": "El usuario debe confirmar un nombre. Si acepta, responde si. Si rechaza, responde no.",
        "delete": "El usuario debe decir la clave. No la interpretes. Responde PREGUNTA.",
        "clave": "Está diciendo la clave. No la interpretes. Responde PREGUNTA.",
    }.get(pending, "Si no es una orden, responde PREGUNTA.")
    instruction = (
        "Eres un clasificador de voz. No hagas la tarea. Responde una sola línea.\n"
        f"{hint}\n"
        "Si es una orden, reescríbela en una forma canónica de esta lista, "
        "aunque el usuario use otras palabras:\n"
        "ayuda\n"
        "modo administrador\n"
        "otra voz\n"
        "voz <numero>\n"
        "subir volumen\n"
        "bajar volumen\n"
        "otro reconocedor\n"
        "reconocedor kroko\n"
        "reconocedor whisper\n"
        "reconocedor base\n"
        "reconocedor small\n"
        "reconocedor canary\n"
        "personalidad\n"
        "personalidad <nombre>\n"
        "nombre\n"
        "nombre <nombre>\n"
        "identifica mi voz\n"
        "listar sesiones\n"
        "cerrar sesion\n"
        "ok gracias\n"
        "crear sesion <nombre>\n"
        "abrir sesion <nombre>\n"
        "borrar sesion <nombre>\n"
        "si\n"
        "no\n"
        "El nombre va en minúsculas, sin acentos, con espacios.\n"
        "Ejemplos: abre admin, conecta como administrador, haz de admin, modo admin -> modo administrador. "
        "lista ayuda, ayuda en comandos, ver los comandos, dame la ayuda, "
        "ahora dame la ayuda, enséñame la ayuda, me puedes dar la ayuda -> ayuda. "
        "cierra la sesión, vuelve a la normal -> cerrar sesion. "
        "vale gracias, eso es todo -> ok gracias. "
        "Estas órdenes no necesitan modo admin. Haz exactamente lo pedido.\n"
        "Si nombra un número de voz, responde voz y ese número. "
        "No lo conviertas en otra voz.\n"
        "activar la voz número cuatro, pon la voz 4, quiero la voz cuatro -> voz 4. "
        "activa la voz dos -> voz 2. "
        "siguiente voz, otra voz, cambia a la siguiente -> otra voz. "
        "más alto, sube el volumen, sube el sonido -> subir volumen. "
        "más bajo, baja el volumen, baja el sonido -> bajar volumen. "
        "otro reconocedor, siguiente reconocedor -> otro reconocedor. "
        "reconocedor whisper small -> reconocedor small. "
        "reconocedor whisper pequeño -> reconocedor whisper. "
        "pon la personalidad vega, personalidad vega -> personalidad vega. "
        "qué personalidad tienes -> personalidad. "
        "te llamas miguel, llamarte miguel, tu nombre es miguel -> nombre miguel. "
        "cómo te llamas, cuál es tu nombre -> nombre. "
        "identifica mi voz, graba mi voz -> identifica mi voz. "
        "explícame los comandos, qué puedo decir, pon ejemplos -> ayuda. "
        "quiero una sesión que se llame casa de campo -> crear sesion casa de campo.\n"
        "Si es una pregunta o un encargo normal, responde exactamente PREGUNTA.\n"
        f"Frase: {phrase}"
    )
    try:
        text = router_ask(instruction)
    except (OSError, TimeoutError, RuntimeError) as exc:
        log.info("clasificador no disponible: %s", exc)
        return phrase
    line = fold_text(text).strip()
    if not line or line == "pregunta" or line.startswith("pregunta"):
        return phrase
    log.info("entendido: %s", line)
    return line


INTERPRET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "accion": {"type": "string", "enum": ["ignorar", "conversacion", "comando"]},
        "orden": {"type": "string"},
        "texto": {"type": "string"},
    },
    "required": ["accion", "orden", "texto"],
}


INTERPRET_SYSTEM = (
    "Clasifica una frase de voz. Responde solo JSON con accion, orden y texto. "
    "accion es ignorar, conversacion o comando. "
    "ignorar: no habla con el asistente. orden y texto vacíos. "
    "conversacion: pregunta o charla. texto es la pregunta. orden vacío. "
    "comando: quiere una acción. orden es una sola de estas, texto vacío: "
    "subir volumen, bajar volumen, otra voz, voz N, pon cancion TITULO, "
    "pausa musica, seguir musica, para la musica, otro reconocedor, "
    "reconocedor kroko, reconocedor whisper, reconocedor base, reconocedor small, reconocedor canary, "
    "personalidad, personalidad NOMBRE, nombre, nombre NOMBRE, identifica mi voz, "
    "listar sesiones, crear sesion NOMBRE, abrir sesion NOMBRE, cerrar sesion, "
    "borrar sesion NOMBRE, listar agentes, abrir agente NOMBRE, crear agente NOMBRE, "
    "cerrar agente, apagar, ayuda, prueba. "
    "Si el reconocedor escribió mal, pero se parece a un comando, devuelve ese comando. "
    "Ejemplos: otros recobetos -> otro reconocedor. "
    "otro reconoce dor -> otro reconocedor. "
    "ponme Queen -> pon cancion Queen. "
    "con la cancion de Queen -> pon cancion Queen. "
    "Si oye con en vez de pon, sigue siendo poner la cancion. "
    "más alto -> subir volumen. "
    "cierra conversacion no es cerrar sesion. "
    "qué tiempo hace -> conversacion. charla de la casa -> ignorar."
)


def spoken_command(orden: str) -> str:
    text = orden.strip()
    text = re.sub(r"(?i)^pon cancion\b", "pon la canción", text)
    return (
        text.replace("sesion", "sesión")
        .replace("musica", "música")
        .replace("cancion", "canción")
    )


def _local_guess(phrase: str) -> tuple[str, str] | None:
    """Skip Grok only for echoes, tiny noises, and clear questions.

    Anything else, including a garbled line, is sent to Grok. It may be a
    misheard command such as "otros recobetos".
    """
    folded = fold_text(phrase)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded or is_own_words(phrase):
        return "ignorar", ""
    letters = re.sub(r"[^a-zñ]", "", folded)
    if len(folded.split()) == 1 and len(letters) < 4:
        return "ignorar", ""
    # A question word used to open a conversation with no wake. After
    # "cierra conversación" the next short sentence did that again.
    return None


def interpret(phrase: str) -> tuple[str, str]:
    """Map a loose phrase to the assistant API.

    Returns ("ignorar", ""), ("conversacion", question), or ("comando", strict line).
    Clear questions and room chatter stay on the Pi. Only a possible command
    asks Grok, and that call does use the network: a local model on this Pi
    was slower than the cloud one.
    """
    guessed = _local_guess(phrase)
    if guessed is not None:
        log.info("intérprete local: %s %s", guessed[0], guessed[1])
        return guessed
    instruction = INTERPRET_SYSTEM + "\nFrase: " + phrase
    try:
        raw = router_ask(instruction, schema=INTERPRET_SCHEMA, timeout=20)
    except (OSError, TimeoutError, RuntimeError) as exc:
        log.info("intérprete no disponible: %s", exc)
        return "ignorar", ""
    kind, payload_text = _parse_action(raw)
    log.info("intérprete: %s %s", kind, payload_text)
    return kind, payload_text


def _parse_action(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = re.sub(r"^json", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        action = str(data.get("accion") or "").strip().lower()
        if action == "comando":
            return "comando", str(data.get("orden") or "").strip()
        if action == "conversacion":
            return "conversacion", str(data.get("texto") or "").strip()
        return "ignorar", ""
    line = text.splitlines()[0].strip() if text else ""
    folded = fold_text(line)
    if folded.startswith("comando:"):
        return "comando", line.split(":", 1)[1].strip()
    if folded.startswith("conversacion"):
        return "conversacion", line.split(":", 1)[1].strip() if ":" in line else ""
    return "ignorar", ""


def router_ask(instruction: str, schema: dict | None = None, timeout: float = 45) -> str:
    data = {"id": str(uuid.uuid4()), "live": False}
    try:
        stored = json.loads(ROUTER_PATH.read_text(encoding="utf-8"))
        if stored.get("id"):
            data = stored
    except (OSError, json.JSONDecodeError):
        pass
    cmd = [
        "grok",
        "-p",
        instruction,
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--no-subagents",
        "--no-plan",
        "--disable-web-search",
        "--verbatim",
        "--no-alt-screen",
        "--reasoning-effort",
        "low",
        "--cwd",
        str(ROOT),
        "--tools",
        "nope",
        "--disallowed-tools",
        DISALLOWED_TOOLS,
    ]
    if schema:
        cmd.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])
    if data.get("live"):
        cmd.extend(["--resume", str(data["id"])])
    else:
        cmd.extend(["--session-id", str(data["id"])])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=grok_env(),
        cwd=str(ROOT),
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise TimeoutError("classifier timeout")
    if proc.returncode != 0:
        raise RuntimeError((err or out or "classifier failed").strip().splitlines()[-1])
    answer, session = answer_and_session(grok_payloads(out or ""))
    if session:
        data["id"] = session
    data["live"] = True
    ROUTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROUTER_PATH.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.chmod(ROUTER_PATH, 0o600)
    return (answer or "").strip().splitlines()[0] if answer else ""


def _default_mixer() -> tuple[str, int, int] | None:
    """Playback control on the OS default card: name, current value, maximum."""
    result = subprocess.run(["amixer", "scontents"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    preferred = ("Master", "PCM", "Speaker", "Headphone", "Playback")
    found: dict[str, tuple[int, int]] = {}
    for block in result.stdout.split("Simple mixer control "):
        if "'" not in block or "Playback" not in block:
            continue
        name = block.split("'", 2)[1]
        limit = re.search(r"Limits: Playback (\d+) - (\d+)", block)
        level = re.search(r"Playback (\d+) \[", block)
        if limit is None or level is None:
            continue
        found[name] = (int(level.group(1)), int(limit.group(2)))
    for name in preferred:
        if name in found:
            current, maximum = found[name]
            return name, current, maximum
    if not found:
        return None
    name, (current, maximum) = next(iter(found.items()))
    return name, current, maximum


def current_playback() -> int | None:
    """Speaker volume as a percent of the OS default mixer."""
    info = _default_mixer()
    if info is None:
        return None
    _name, current, maximum = info
    if maximum <= 0:
        return None
    return round(current * 100 / maximum)


def set_playback(percent: int) -> int | None:
    info = _default_mixer()
    if info is None:
        return None
    name, _current, _maximum = info
    percent = max(0, min(100, int(percent)))
    result = subprocess.run(
        ["amixer", "sset", name, f"{percent}%"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.warning("no pude fijar el volumen: %s", (result.stderr or "").strip())
        return None
    VOLUME_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOLUME_PATH.write_text(f"{percent}\n", encoding="utf-8")
    return percent


def apply_saved_volume() -> None:
    try:
        value = int(VOLUME_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if value > 100:
        value = round(value * 100 / 255)
    set_playback(value)


def change_volume(direction: str) -> str:
    current = current_playback()
    if current is None:
        return "No pude cambiar el volumen."
    step = 8
    target = current + step if direction == "up" else current - step
    target = max(0, min(100, target))
    if target == current:
        return "El volumen ya está al máximo." if direction == "up" else "El volumen ya está al mínimo."
    if set_playback(target) is None:
        return "No pude cambiar el volumen."
    log.info("volumen %s -> %s", current, target)
    return f"Volumen al {target} por ciento."


def volume_command(text: str) -> str | None:
    """'up' or 'down' when the phrase asks to change the speaker volume."""
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    folded = folded.replace("bolumen", "volumen")
    if not folded or len(folded.split()) > 6:
        return None
    if re.search(r"\b(que|como|cuando|donde)\b", folded):
        return None
    up = re.search(r"\b(sube\w*|subi\w*|aument\w*|alza\w*|mas alto|mas fuerte)\b", folded) is not None
    down = re.search(r"\b(baja\w*|bajalo|barj\w*|disminu\w*|mas bajo|mas flojo|mas suave|menos)\b", folded) is not None
    topic = re.search(r"\b(volumen|sonido|altavoz|voz)\b", folded) is not None
    if up and down:
        return None
    if re.search(r"\bmas alto\b|\bmas fuerte\b", folded):
        return "up"
    if re.search(r"\bmas bajo\b|\bmas flojo\b|\bmas suave\b", folded):
        return "down"
    if topic and up:
        return "up"
    if topic and down:
        return "down"
    if re.fullmatch(r"(?:sube|subir|subelo|baja|bajar|bajalo)(?: el| la)? (?:volumen|sonido)", folded):
        return "up" if folded.startswith("sub") else "down"
    return None


VOICE_NUMBER_WORDS = {
    "uno": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "primera": 1,
    "primero": 1,
    "segunda": 2,
    "segundo": 2,
    "tercera": 3,
    "tercero": 3,
    "cuarta": 4,
    "cuarto": 4,
    "quinta": 5,
    "quinto": 5,
    "sexta": 6,
    "sexto": 6,
    "septima": 7,
    "septimo": 7,
    "octava": 8,
    "octavo": 8,
    "novena": 9,
    "noveno": 9,
}


def _voice_word(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return VOICE_NUMBER_WORDS.get(token)


def voice_number(text: str) -> int | None:
    """The voice number the person asked for, counting from 1. None if they did not name one."""
    folded = fold_text(text)
    folded = re.sub(r"[^a-z0-9ñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded or "voz" not in folded.split():
        return None
    match = re.search(
        r"\bvoz\b(?:\s+(?:la|el|numero|num|n|de))*\s+([a-z0-9]+)\b",
        folded,
    )
    if not match:
        return None
    return _voice_word(match.group(1))


def is_voice_change(text: str) -> bool:
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded in {
        "otra voz",
        "cambiar voz",
        "cambia la voz",
        "siguiente voz",
        "otra voz por favor",
    }


def is_help(text: str) -> bool:
    """True when the phrase asks for the command screen.

    The recognizer rarely says a bare "ayuda". It says "dame la ayuda",
    "ahora dame la ayuda", "enséñame la ayuda", often after a mangled wake.
    """
    folded = strip_despierta(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded:
        return False
    if re.search(r"\b(ayudarme|ayuda a|ayuda con|ayuda para|ayudar a|ayudar con)\b", folded):
        return False
    if re.fullmatch(r"(?:por favor\s+)?(?:ayuda|ayudame|ayudar|alluda|oyuda|help|comandos)(?:\s+por favor)?", folded):
        return True
    if re.fullmatch(r"(?:ayuda|alluda|oyuda)(?:\s+en)?(?:\s+los)?\s+comandos", folded):
        return True
    if re.fullmatch(r"que comandos(?:\s+hay|\s+tienes|\s+existen)?", folded):
        return True
    words = folded.split()
    fillers = {"la", "el", "los", "las", "de", "en", "por", "favor", "me", "una", "un", "ahora"}
    topic = {"ayuda", "ayudame", "ayudar", "alluda", "oyuda", "comandos", "help"}
    content = [word for word in words if word not in fillers]
    if content and all(word in topic for word in content):
        return True
    has_topic = any(word in topic for word in words)
    has_ask = re.search(
        r"\b(dame|da|dar|deja|ensen\w*|muestra\w*|lista\w*|listar|ver|quiero|puedes|puede|ahora|di|dime|explica\w*|cuenta\w*|ejemplo\w*)\b",
        folded,
    )
    if has_topic and has_ask and len(words) <= 10:
        return True
    # "lista la ayuda" often arrives as "le instala ayuda".
    return bool(
        re.search(r"\binstala\w*\b", folded)
        and re.search(r"\b(ayuda|comandos)\b", folded)
        and len(words) <= 4
    )


def help_lines() -> list[tuple[str, str]]:
    call = assistant_call()
    return [
        (f"iniciar: \"{call}\" o \"¿estás ahí?\"   acabar: \"gracias\" o \"vale\"", "talk"),
        ("COMANDOS (iniciar con palabra \"comando\")", "header"),
        ("SESION ( abrir ¦ crear ¦ borrar ) NOMBRE , listar , cerrar", "group"),
        ("AGENTE ( abrir ¦ crear ) NOMBRE , listar , cerrar", "group"),
        ("modo administrador", "lista personas, borra NOMBRE"),
        ("subir volumen", "más alto"),
        ("bajar volumen", "más bajo"),
        ("voz NÚMERO", "esa voz, por ejemplo la 4"),
        ("otra voz", "pasa a la siguiente"),
        ("otro reconocedor", "Kroko, Whisper, base, small o Canary"),
        ("personalidad", "alex, vega, nico…"),
        ("nombre NOMBRE", "la primera frase de la huella"),
        ("pon la canción X", "suena solo el audio"),
        ("para la música", "corta la canción"),
        ("apaga el dispositivo", "pide sí y lo apaga"),
        ("prueba", "escribe lo que oye; salir"),
        ("identifica mi voz", "16 frases, una vez"),
    ]


def help_speech() -> str:
    call = assistant_call()
    call = call[:1].upper() + call[1:]
    return (
        f"{call} abre la conversación. Adiós o gracias la cierra. "
        "Las demás órdenes empiezan por la palabra comando. "
        "Comando otra voz. Comando subir volumen. Comando pon la canción y el título. "
        "Comando apaga el dispositivo, y si dices sí, se apaga. "
        "Comando modo administrador, y después la clave, solo para tareas del sistema. "
        "Comando otro reconocedor cambia entre Kroko, Whisper, base, small y Canary. "
        "Comando personalidad elige una de las ocho. "
        "Comando nombre, y un nombre, cambia cómo se le llama. "
        "Comando identifica mi voz graba dieciséis frases, una sola vez, después del pitido."
    )


def music_command(text: str) -> tuple[str, str] | None:
    """('stop', '') or ('play', query) when the phrase asks for a song.

    A title may push the phrase past six words. Sixteen is the cap.
    Play wins over "para" so "pon la canción para bailar" is not a stop.
    """
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ0-9\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded or len(folded.split()) > 16:
        return None
    match = re.search(
        r"\b(?:ponme|pon(?:e|ga|gas|er|me)?|reproduce\w*|reproducir|toca|tocar|escuchar|escucha)\s+"
        r"(?:(?:la|el|una|un|esa|ese)\s+)?(?:cancion|tema|musica)\s+(.+)",
        folded,
    )
    if not match:
        match = re.search(
            r"\b(?:ponme|reproduce\w*|reproducir|toca|tocar|escuchar|escucha)\s+(.+)",
            folded,
        )
    if match:
        query = re.sub(r"^(?:de)\s+", "", match.group(1)).strip()
        query = re.sub(r"^(?:(?:la|el|una|un)\s+)?(?:cancion|tema|musica)\s+", "", query).strip()
        if len(query) >= 2:
            return ("play", query)
    if re.search(r"\b(pausa|pausar)\b", folded) and re.search(r"\b(musica|cancion|tema)\b", folded):
        return ("pause", "")
    if re.search(r"\b(sigue|seguir|continua|continuar|reanuda|reanudar)\b", folded) and re.search(
        r"\b(musica|cancion|tema)\b", folded
    ):
        return ("resume", "")
    if re.search(r"\b(para|parar|quita|quitar|deten\w*|stop)\b", folded) and re.search(
        r"\b(musica|cancion|tema)\b", folded
    ):
        return ("stop", "")
    return None


ENROLL_LINES = (
    "hola grok",
    "estás ahí",
    "qué hora es",
    "pon una canción",
    "sube el volumen",
    "baja el volumen",
    "para la música",
    "buenos días",
    "hasta luego",
    "qué día es hoy",
    "me escuchas",
    "gracias",
    "abre la sesión",
    "cuenta hasta tres",
    "cómo estás",
    "dime la hora",
)


def enroll_phrases() -> tuple[str, ...]:
    """The sixteen phrases. The first one is hola plus the chosen call-name."""
    return (assistant_call(), *ENROLL_LINES[1:])


def assistant_name_command(text: str) -> tuple[str, str] | None:
    """('show', '') or ('set', spoken name). The footprint then starts with that name."""
    folded = _folded_words(text)
    words = folded.split()
    if not words or len(words) > 6:
        return None
    if words[0] == "nombre" or (len(words) >= 2 and words[0] == "tu" and words[1] == "nombre"):
        rest = words[2:] if words[0] == "tu" else words[1:]
        rest = [word for word in rest if word not in {"es", "de", "del", "se", "llama", "te", "un", "el"}]
        if not rest or rest[0] in {"cual", "que", "como", "actual"}:
            return ("show", "")
        return ("set", " ".join(rest))
    if re.search(r"\b(llamas|llamarte|llamate)\b", folded):
        rest = re.sub(r"^.*\b(?:llamas|llamarte|llamate)\b", "", folded).strip()
        rest = re.sub(r"^(?:te|a|ahora)\s+", "", rest).strip()
        if not rest:
            return ("show", "")
        return ("set", rest)
    return None


def people_command(text: str) -> tuple[str, str] | None:
    """('list', '') or ('delete', saved name) for identified people."""
    folded = _folded_words(text)
    words = folded.split()
    if not folded or len(words) > 8:
        return None
    mentions_people = re.search(r"\b(persona|personas|identificad\w*)\b", folded)
    if re.search(r"\b(lista|listar|listame|liste|quienes|quien)\b", folded) and mentions_people:
        return ("list", "")
    match = re.search(
        r"\b(?:borra|borrar|elimina|eliminar|quita|quitar)\b(?:\s+(?:a|al|la|el|persona))?\s+(.+)$",
        folded,
    )
    if not match:
        return None
    wanted = re.sub(r"^(?:a|al|la|el|persona)\s+", "", match.group(1).strip())
    for person in speakers().people:
        saved = str(person.get("name") or "")
        if fold_text(saved) == wanted:
            return ("delete", saved)
    if mentions_people:
        return ("delete", wanted)
    return None


def is_voice_enroll(text: str) -> bool:
    folded = _folded_words(text)
    words = folded.split()
    return bool(words) and len(words) <= 6 and "identifica" in words and "voz" in words


def _enroll_stopped(text: str) -> bool:
    return is_leave_test(text) or _folded_words(text) in {"salir", "cancela", "cancelar"}


def _saved_name(name: str) -> str:
    wanted = fold_text(name)
    for person in speakers().people:
        saved = str(person.get("name") or "")
        if fold_text(saved) == wanted:
            return saved
    return ""


def _cosine(one: np.ndarray, other: np.ndarray) -> float:
    vector = np.asarray(one, dtype=np.float32)
    sample = np.asarray(other, dtype=np.float32)
    return float(np.dot(vector, sample) / (np.linalg.norm(vector) * np.linalg.norm(sample) + 1e-8))


def largest_voice_group(vectors: list[np.ndarray]) -> list[int]:
    """Indexes of the largest set where every pair has cosine >= 0.55.

    Needs at least 12. The first group of that size is kept.
    """
    count = len(vectors)
    if count < 12:
        return []
    for size in range(count, 11, -1):
        for combo in itertools.combinations(range(count), size):
            if all(_cosine(vectors[i], vectors[j]) >= 0.55 for i, j in itertools.combinations(combo, 2)):
                return list(combo)
    return []


def name_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", fold_text(name)).strip("-")
    return slug or "persona"


def _enroll_choose_name(
    speech: Speech,
    mic: Mic,
    floor: NoiseFloor,
    cfg: dict,
    display: HeardDisplay,
    speaker: str,
    speaker_embedding: np.ndarray | None,
) -> str:
    """Ask the name. A saved voice or a saved name is replaced. A new name is another person."""
    candidate = ""
    name_embedding = None
    ask_name = True
    for _attempt in range(4):
        if not candidate:
            speak(speech, "¿Cómo te llamas?" if ask_name else "Di otro nombre.", speaker)
            ask_name = False
            heard, audio = _enroll_listen(speech, mic, display, speaker, "cómo te llamas")
            if _enroll_stopped(heard):
                return ""
            candidate = person_name(heard)
            if not candidate:
                speak(speech, "No he oído el nombre.", speaker)
                continue
            captured = speakers().capture(audio)
            name_embedding = captured[0] if captured is not None else None
        voice_name = ""
        for emb in (name_embedding, speaker_embedding):
            if emb is None:
                continue
            known, _index = speakers().match(emb)
            if known:
                voice_name = known
                break
        saved = _saved_name(candidate)
        if voice_name or saved:
            nombre = voice_name or saved
            speak(speech, f"Esta voz ya la tengo como {nombre}. ¿Repito la identificación?", speaker)
        else:
            nombre = ""
            speak(speech, f"No tengo a {candidate}. ¿Lo guardo como otra persona?", speaker)
        answer, answer_audio = _enroll_listen(speech, mic, display, speaker, "sí o no")
        if _enroll_stopped(answer):
            return ""
        if confirms(answer) is True:
            return nombre or candidate
        if confirms(answer) is False:
            candidate = ""
            name_embedding = None
            continue
        other = person_name(answer)
        if other:
            candidate = other
            captured = speakers().capture(answer_audio)
            if captured is not None:
                name_embedding = captured[0]
            continue
        speak(speech, "Di sí o no.", speaker)
    return ""


def run_voice_enrollment(
    speech: Speech,
    mic: Mic,
    floor: NoiseFloor,
    cfg: dict,
    display: HeardDisplay,
    speaker: str,
    speaker_embedding: np.ndarray | None = None,
) -> None:
    """Sixteen phrases, once. One CampPlus print, then a score for each installed engine."""
    book = speakers()
    if book.extractor is None:
        speak(speech, "No tengo el modelo de voces.", speaker)
        return
    name = _enroll_choose_name(speech, mic, floor, cfg, display, speaker, speaker_embedding)
    if not name:
        speak(speech, "Cancelo la identificación.", speaker)
        return
    taken: list[tuple[str, np.ndarray, np.ndarray]] = []
    failures = 0
    phrases = enroll_phrases()
    total = len(phrases)
    speak(
        speech,
        (
            f"Grabaré {total} frases una sola vez. Guardo el sonido en crudo, sin comprobar las palabras. "
            "El sonido vale para todos los motores. Habla después del pitido, y espera el segundo pitido."
        ),
        speaker,
    )
    index = 0
    while index < total:
        prompt = phrases[index]
        speak(speech, f"{index + 1} de {total}. {prompt}", speaker)
        heard, audio = _enroll_listen(speech, mic, display, speaker, prompt)
        if _enroll_stopped(heard):
            speak(speech, "Cancelo la identificación.", speaker)
            return
        has_samples = audio is not None and int(getattr(audio, "size", 0)) > 0
        captured = book.capture(audio) if has_samples else None
        log_enroll_take(heard, has_samples, speech.asr_label)
        outcome = enroll_result(has_samples, captured is not None, heard)
        if outcome == "keep" and captured is not None:
            failures = 0
            vector, wave_audio = captured
            taken.append((prompt, wave_audio, vector))
            index += 1
            continue
        failures += 1
        if failures >= 3:
            if outcome == "retry":
                speak(speech, f"He oído: {heard.strip()}. No he cogido la huella. Lo dejo.", speaker)
            else:
                speak(speech, "No oigo el micrófono. Lo dejo.", speaker)
            return
        if outcome == "retry":
            speak(speech, f"He oído: {heard.strip()}. No he cogido la huella. Repite.", speaker)
    group = largest_voice_group([item[2] for item in taken])
    if len(group) < 12:
        speak(speech, "Estas tomas no son una sola voz. No guardo a otra persona.", speaker)
        return
    kept = [taken[index] for index in group]
    book.save_enrollment(name, kept)
    speak(
        speech,
        f"Listo, {name}. El sonido queda guardado y vale para todos los motores. Valoro cada uno.",
        speaker,
    )
    if len(kept) < total:
        speak(speech, f"Guardo {len(kept)} de {total}.", speaker)
    try:
        score_missing_engines(speech)
        speech._select_asr(choose_live_asr(speech.asr_id))
        log_listen_motor(speech)
    except Exception:
        log.exception("no pude puntuar tras identificar")
        if speech.spanish is None and speech.offline is None:
            speech._select_asr("kroko")
        else:
            speech._ensure_reread()


def is_shutdown(text: str) -> bool:
    """True when the phrase asks to power off this device, not a lamp or a TV."""
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not folded or len(folded.split()) > 8:
        return False
    if re.search(r"\b(luz|luces|lampara|tele|television|musica|radio|alarma)\b", folded):
        return False
    if re.fullmatch(r"(?:por favor )?(?:apaga\w*|apagar|shutdown|power off|poweroff)", folded):
        return True
    return bool(
        re.search(r"\b(apaga\w*|shutdown|power ?off|cierra\w*|apagar)\b", folded)
        and re.search(r"\b(dispositivo|equipo|sistema|ordenador|aparato|raspberry|maquina|pc|pi)\b", folded)
    )


def shutdown_device() -> bool:
    sudo = Path.home() / ".config" / "grok-assistant" / "bin" / "sudo"
    log.info("apagando el dispositivo")
    try:
        result = subprocess.run(
            [str(sudo), "shutdown", "-h", "now"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("apagado: %s", exc)
        return False
    if result.returncode != 0:
        log.error("apagado: %s", (result.stderr or result.stdout or "").strip()[:300])
        return False
    return True


def person_name(text: str) -> str:
    """A short name from 'me llamo Ana' or just 'Ana'. Empty if it is not a name."""
    raw = text.strip().strip(".,;:!?¿¡")
    if not raw or is_goodbye(raw) or is_shutdown(raw) or voice_number(raw) or volume_command(raw):
        return ""
    folded = re.sub(r"[^a-zñ\s]", " ", fold_text(raw))
    folded = re.sub(r"\s+", " ", folded).strip()
    words = raw.split()
    fwords = folded.split()
    if len(words) != len(fwords):
        words = fwords
    drop = 0
    joined = " ".join(fwords)
    for prefix in ("me llamo ", "me llama ", "yo me llamo ", "yo me llama ", "mi nombre es ", "yo soy ", "soy "):
        if joined.startswith(prefix):
            drop = len(prefix.split())
            break
    chosen = [word.strip(".,;:!?¿¡") for word in words[drop:] if word.strip(".,;:!?¿¡")]
    if not chosen or len(chosen) > 3:
        return ""
    return " ".join(word[:1].upper() + word[1:] for word in chosen)


class SpeakerBook:
    """Local CampPlus prints. One print works for every listening engine. Nothing leaves the Pi."""

    def __init__(self) -> None:
        self.people: list[dict] = []
        self.self_embedding: np.ndarray | None = None
        self.locked = ""
        self.extractor = None
        self._load()
        if SPEAKER_MODEL.is_file():
            import sherpa_onnx

            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(SPEAKER_MODEL),
                num_threads=2,
                debug=False,
                provider="cpu",
            )
            self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            log.info("voces guardadas: %d", len(self.people))
        else:
            log.info("sin modelo de voces; no identifico personas")

    def _normalize_person(self, item: dict, name: str) -> dict:
        """Old list-of-vectors stays as prints.legacy. Loading does not write the file."""
        prints = item.get("prints")
        store: dict = {}
        if isinstance(prints, dict):
            store = prints
        elif isinstance(prints, list) and prints:
            store = {"legacy": prints}
        elif isinstance(item.get("embedding"), list) and item.get("embedding"):
            store = {"legacy": [item["embedding"]]}
        last = item.get("last", 0)
        if isinstance(last, bool) or not isinstance(last, (int, float)):
            last = 0
        seen = parse_iso(str(item.get("last_seen") or ""))
        if seen is not None:
            last = seen.timestamp()
        raw = item.get("raw") if isinstance(item.get("raw"), list) else []
        scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
        return {
            "name": name,
            "prints": store,
            "raw": raw,
            "scores": scores,
            "last": float(last or 0),
            "greet_count": int(item.get("greet_count") or 0),
        }

    def _load(self) -> None:
        try:
            data = json.loads(SPEAKERS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        people = data.get("people")
        loaded: list[dict] = []
        if isinstance(people, list):
            for item in people:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                if not (item.get("embedding") or item.get("prints")):
                    continue
                loaded.append(self._normalize_person(item, str(item["name"])))
        elif isinstance(people, dict):
            for name, item in people.items():
                if isinstance(item, dict) and name:
                    loaded.append(self._normalize_person(item, str(name)))
        self.people = loaded
        own = data.get("self")
        if isinstance(own, list) and own:
            self.self_embedding = np.asarray(own, dtype=np.float32)
        locked = data.get("locked")
        if isinstance(locked, str):
            self.locked = locked

    def _save(self) -> None:
        SPEAKERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        people: dict = {}
        for person in self.people:
            name = str(person.get("name") or "")
            if not name:
                continue
            people[name] = {
                "prints": person.get("prints") if isinstance(person.get("prints"), dict) else {},
                "raw": person.get("raw") if isinstance(person.get("raw"), list) else [],
                "scores": person.get("scores") if isinstance(person.get("scores"), dict) else {},
                "last": person.get("last") or 0,
                "greet_count": int(person.get("greet_count") or 0),
            }
        payload: dict = {"people": people}
        if self.locked:
            payload["locked"] = self.locked
        if self.self_embedding is not None:
            payload["self"] = [round(float(value), 5) for value in self.self_embedding]
        SPEAKERS_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(SPEAKERS_PATH, 0o600)

    def _wave(self, samples: np.ndarray, sample_rate: int) -> np.ndarray | None:
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return None
        if float(np.max(np.abs(audio))) > 2.0:
            audio = audio / 32768.0
        if sample_rate != RATE:
            audio = resample(audio, sample_rate, RATE)
        if audio.size < int(RATE * 0.6):
            return None
        return audio

    def _embed_wave(self, audio: np.ndarray) -> np.ndarray | None:
        if self.extractor is None:
            return None
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=RATE, waveform=audio)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            return None
        return np.array(self.extractor.compute(stream), dtype=np.float32)

    def remember_self(self, samples: np.ndarray, sample_rate: int) -> None:
        audio = self._wave(samples, sample_rate)
        if audio is None:
            return
        fresh = self._embed_wave(audio)
        if fresh is None:
            return
        if self.self_embedding is not None and self.self_embedding.shape == fresh.shape:
            fresh = 0.65 * self.self_embedding + 0.35 * fresh
        self.self_embedding = fresh
        self._save()
        log.info("huella propia actualizada")

    def _similar(self, embedding: np.ndarray, other: np.ndarray) -> float:
        vector = np.asarray(embedding, dtype=np.float32)
        return float(np.dot(vector, other) / (np.linalg.norm(vector) * np.linalg.norm(other) + 1e-8))

    def is_self(self, samples: np.ndarray | None, sample_rate: int = RATE) -> bool:
        if self.self_embedding is None or samples is None:
            return False
        audio = self._wave(samples, sample_rate)
        if audio is None:
            return False
        fresh = self._embed_wave(audio)
        if fresh is None or fresh.shape != self.self_embedding.shape:
            return False
        score = self._similar(fresh, self.self_embedding)
        log.info("voz propia: %.2f", score)
        return score >= 0.62

    def embed(self, samples: np.ndarray | None) -> np.ndarray | None:
        audio = self._wave(samples if samples is not None else np.array([]), RATE)
        if audio is None:
            return None
        return self._embed_wave(audio)

    def capture(self, samples: np.ndarray | None) -> tuple[np.ndarray, np.ndarray] | None:
        """CampPlus vector plus the 16 kHz float audio, or nothing if the take is empty."""
        audio = self._wave(samples if samples is not None else np.array([]), RATE)
        if audio is None:
            return None
        fresh = self._embed_wave(audio)
        if fresh is None:
            return None
        return fresh, audio

    def _vector_list(self, raw: object) -> list[np.ndarray]:
        vectors: list[np.ndarray] = []
        if not isinstance(raw, list):
            return vectors
        for item in raw:
            if isinstance(item, list) and item:
                vectors.append(np.asarray(item, dtype=np.float32))
        return vectors

    def _prints(self, person: dict) -> list[np.ndarray]:
        raw = person.get("prints")
        if isinstance(raw, dict):
            camp = self._vector_list(raw.get("campplus"))
            if camp:
                return camp
            vectors: list[np.ndarray] = []
            for value in raw.values():
                vectors.extend(self._vector_list(value))
            return vectors
        vectors = self._vector_list(raw)
        if not vectors and person.get("embedding"):
            vectors.append(np.asarray(person["embedding"], dtype=np.float32))
        return vectors

    def gate(self, person: dict) -> float:
        """CampPlus uses 0.55. A print from before that change stays at 0.30."""
        prints = person.get("prints")
        if isinstance(prints, dict) and self._vector_list(prints.get("campplus")):
            return 0.55
        return 0.30

    def best_score(self, embedding: np.ndarray, person: dict) -> float:
        best = 0.0
        vector = np.asarray(embedding, dtype=np.float32)
        for other in self._prints(person):
            if other.shape != vector.shape:
                continue
            best = max(best, self._similar(vector, other))
        return best

    def match(self, embedding: np.ndarray) -> tuple[str, int]:
        """Closest name that clears that person's own threshold."""
        best_name = ""
        best = 0.0
        passed_name = ""
        passed = 0.0
        passed_index = -1
        vector = np.asarray(embedding, dtype=np.float32)
        for index, person in enumerate(self.people):
            score = self.best_score(vector, person)
            if score > best:
                best = score
                best_name = str(person["name"])
            if score >= self.gate(person) and score > passed:
                passed = score
                passed_name = str(person["name"])
                passed_index = index
        log.info("voz parecida: %s %.2f", best_name or "(nadie)", best)
        if passed_index < 0:
            return "", -1
        return passed_name, passed_index

    def since(self, index: int) -> timedelta | None:
        if index < 0 or index >= len(self.people):
            return None
        last = self.people[index].get("last")
        if isinstance(last, (int, float)) and not isinstance(last, bool) and float(last) > 0:
            when = datetime.fromtimestamp(float(last), timezone.utc)
            return now_utc() - when
        when = parse_iso(str(self.people[index].get("last_seen") or ""))
        if when is None:
            return None
        return now_utc() - when

    def mark(self, index: int) -> None:
        if index < 0 or index >= len(self.people):
            return
        self.people[index]["last"] = time.time()
        self._save()

    def next_extra(self, index: int) -> str:
        extras = (
            "¿Qué se te ofrece?",
            "Te escucho.",
            "Cuéntame.",
            "¿En qué andas?",
            "Aquí estoy.",
        )
        count = int(self.people[index].get("greet_count") or 0)
        extra = extras[count % len(extras)]
        self.people[index]["greet_count"] = count + 1
        self._save()
        return extra

    def add(self, name: str, embedding: np.ndarray) -> int:
        return self.add_prints(name, [embedding])

    def _index_of(self, name: str) -> int:
        wanted = fold_text(name)
        return next(
            (i for i, person in enumerate(self.people) if fold_text(str(person.get("name") or "")) == wanted),
            -1,
        )

    def _person_slug(self, person: dict) -> str:
        raw = person.get("raw") or []
        if raw and isinstance(raw[0], dict) and "/" in str(raw[0].get("file") or ""):
            return str(raw[0]["file"]).split("/", 1)[0]
        return ""

    def _free_slug(self, name: str) -> str:
        used = {self._person_slug(person) for person in self.people}
        used.discard("")
        candidate = name_slug(name)
        number = 2
        while candidate in used or (RAW_DIR / candidate).exists():
            candidate = f"{name_slug(name)}-{number}"
            number += 1
        return candidate

    def _drop_raw(self, person: dict) -> None:
        slug = self._person_slug(person)
        if slug:
            shutil.rmtree(RAW_DIR / slug, ignore_errors=True)

    def save_enrollment(self, name: str, takes: list[tuple[str, np.ndarray, np.ndarray]]) -> int:
        """Keep one voice group. Raw wavs sit beside speakers.json. Scores start empty."""
        index = self._index_of(name)
        if index < 0:
            person = {"name": name, "prints": {}, "raw": [], "scores": {}, "last": 0, "greet_count": 0}
            self.people.append(person)
            index = len(self.people) - 1
            slug = self._free_slug(name)
            log.info("persona nueva: %s", name)
        else:
            person = self.people[index]
            name = str(person.get("name") or name)
            slug = self._person_slug(person) or self._free_slug(name)
            self._drop_raw(person)
            log.info("reidentifico: %s", name)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(RAW_DIR, 0o700)
        folder = RAW_DIR / slug
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        raw = []
        packed = []
        for number, (phrase, audio, vector) in enumerate(takes):
            rel = f"{slug}/{number:02d}.wav"
            write_wav(RAW_DIR / rel, audio, RATE)
            os.chmod(RAW_DIR / rel, 0o600)
            raw.append({"phrase": phrase, "file": rel})
            packed.append([round(float(value), 5) for value in vector])
        person["name"] = name
        person["prints"] = {"campplus": packed}
        person["raw"] = raw
        person["scores"] = {}
        self.locked = name
        self._save()
        log.info("huellas de %s: %d", name, len(packed))
        return index

    def add_prints(self, name: str, embeddings: list[np.ndarray], replace: bool = False) -> int:
        """Store CampPlus vectors when a caller has no wavs. Enrollment uses save_enrollment."""
        index = self._index_of(name)
        if index < 0:
            self.people.append(
                {"name": name, "prints": {}, "raw": [], "scores": {}, "last": time.time(), "greet_count": 0}
            )
            index = len(self.people) - 1
        person = self.people[index]
        prints = person.get("prints") if isinstance(person.get("prints"), dict) else {}
        stored = [] if replace else self._vector_list(prints.get("campplus"))
        for embedding in embeddings:
            stored.append(np.asarray(embedding, dtype=np.float32))
        prints = dict(prints)
        prints["campplus"] = [[round(float(value), 5) for value in embedding] for embedding in stored]
        person["prints"] = prints
        person["last"] = time.time()
        self._save()
        return index

    def remove_person(self, name: str) -> bool:
        wanted = fold_text(name)
        removed = [person for person in self.people if fold_text(str(person.get("name") or "")) == wanted]
        kept = [person for person in self.people if fold_text(str(person.get("name") or "")) != wanted]
        if len(kept) == len(self.people):
            return False
        for person in removed:
            self._drop_raw(person)
        self.people = kept
        if fold_text(self.locked) == wanted:
            self.locked = ""
        self._save()
        log.info("persona borrada: %s", name)
        return True

    def set_lock(self, name: str) -> None:
        self.locked = name
        self._save()
        log.info("solo escucho a %s", name)

    def _locked_person(self) -> dict | None:
        if not self.locked:
            return None
        for person in self.people:
            if str(person.get("name") or "") == self.locked:
                return person
        return None

    def identified_people(self) -> list[dict]:
        found = []
        for person in self.people:
            if person.get("identified"):
                found.append(person)
            elif self.locked and str(person.get("name") or "") == self.locked:
                found.append(person)
        return found

    def accepts(self, embedding: np.ndarray | None) -> bool:
        """True when the door is open, or this print is the locked voice.

        CampPlus matches at 0.55. A legacy print, with no campplus vectors, stays at 0.30
        so the person already enrolled is not locked out.
        """
        if self.extractor is None or not self.locked:
            return True
        person = self._locked_person()
        if person is None:
            return True
        if embedding is None:
            return False
        vector = np.asarray(embedding, dtype=np.float32)
        return self.best_score(vector, person) >= self.gate(person)


_speakers: SpeakerBook | None = None


def speakers() -> SpeakerBook:
    global _speakers
    if _speakers is None:
        _speakers = SpeakerBook()
    return _speakers


def allowed_voice(
    embedding: np.ndarray | None,
    audio: np.ndarray | None = None,
    label: str = "",
) -> bool:
    """After a voice is locked, only that print is heard. A missing model leaves the door open."""
    del label
    book = speakers()
    if book.extractor is None or not book.locked:
        return True
    emb = embedding
    if emb is None and audio is not None:
        emb = book.embed(audio)
    ok = book.accepts(emb)
    if not ok and emb is not None:
        person = book._locked_person()
        score = book.best_score(emb, person) if person is not None else 0.0
        log.info("voz no identificada, no la atiendo %.2f", score)
    elif not ok:
        log.info("no es la voz guardada (%s)", book.locked)
    return ok


def enroll_open(embedding: np.ndarray | None, audio: np.ndarray | None = None) -> bool:
    """identifica mi voz is open until a voice is locked. After that, only that voice."""
    book = speakers()
    if book.extractor is None or not book.locked:
        return True
    return allowed_voice(embedding, audio)


def same_voice(one: np.ndarray | None, other: np.ndarray | None) -> bool:
    """True when two prints are close enough to be the same person."""
    if one is None or other is None or one.shape != other.shape:
        return False
    return speakers()._similar(one, other) >= 0.48


def voice_addresses_grok(text: str) -> bool:
    """A phrase aimed at the assistant, not at the people nearby."""
    if opens_talk(text):
        return True
    if command_body(text) is not None:
        return True
    if is_goodbye(text) or is_nothing_needed(text):
        return True
    return False


def opening_greeting(book: SpeakerBook, index: int, name: str) -> str:
    """How to open, from how long this voice has been away."""
    elapsed = book.since(index)
    if elapsed is not None and elapsed < timedelta(hours=1):
        return "Dime."
    if elapsed is not None and elapsed < timedelta(hours=5):
        return f"Hola de nuevo, {name}."
    return f"Hola, {name}. {book.next_extra(index)}"


def confirms(text: str) -> bool | None:
    folded = fold_text(text)
    folded = re.sub(r"[^a-zñ\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    words = folded.split()
    if words and len(words) <= 4 and words[0] in {"si", "vale", "ok", "confirmo", "correcto"}:
        return True
    if words and len(words) <= 4 and words[0] in {"no", "cancelar", "cancela"}:
        return False
    if folded in {"si", "correcto", "confirmo", "vale", "ok", "de acuerdo", "exacto", "esa es", "asi es"}:
        return True
    if folded in {"no", "incorrecto", "mal", "cancelar", "cancela", "no es"}:
        return False
    return None


def apply_session_command(kind: str, name: str) -> str:
    data = load_sessions()
    named = data.setdefault("named", {})
    if kind == "list":
        return describe_sessions()
    if kind == "close":
        data["active"] = SHARED_NAME
        save_sessions(data)
        return "Vuelta a la sesión compartida."
    if kind == "open":
        if name not in named:
            return "No tengo una sesión con ese nombre."
        data["active"] = name
        save_sessions(data)
        return f"Abierta la sesión {session_label(name)}. Esa no caduca."
    if kind == "delete":
        return ""
    if kind == "create":
        return ""
    return ""


def run_grok_watched(cmd: list[str], env: dict, timeout: int, label: str) -> tuple[str, str, int]:
    """Run one Grok call. If it stays silent or dies, kill it and start it once more."""
    last_out, last_err, last_code = "", "", 1
    for attempt in (1, 2):
        started = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(ROOT),
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("watchdog: %s no respondió en %ss, lo reinicio", label, timeout)
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            last_out, last_err, last_code = "", "timeout", 1
            continue
        last_out, last_err, last_code = out or "", err or "", int(proc.returncode or 0)
        log.info("%s volvió en %.1fs (exit %s, intento %s)", label, time.monotonic() - started, last_code, attempt)
        if err.strip():
            log.info("%s stderr: %s", label, err.strip()[:500])
        if last_code == 0 and last_out.strip():
            return last_out, last_err, last_code
        log.warning("watchdog: %s no contestó (exit %s), lo reinicio", label, last_code)
    return last_out, last_err, last_code


TONE_HINTS = {
    "auto": "Elige el tono según la frase y mantén la misma persona. Serio si importa, pedagógico si están aprendiendo, relajado si es charla.",
    "professional": "Tono profesional: frases limpias, poco argot, humor escaso.",
    "serious": "Tono serio: calma, precisión, sin chistes, di la duda si la hay.",
    "technical": "Tono técnico: el término correcto, la contrapartida y un ejemplo solo si ayuda.",
    "pedagogical": "Tono pedagógico: primero la idea, luego un ejemplo. Nadie es torpe por preguntar.",
    "relaxed": "Tono relajado: frases cortas y un poco de coloquial.",
    "playful": "Tono juguetón: más ritmo, sin convertir cada frase en un chiste.",
    "direct": "Tono directo: la conclusión primero.",
    "empathetic": "Tono empático: reconoce la situación sin frase terapéutica y sigue siendo útil.",
    "creative": "Tono creativo: propone alternativas y conexiones.",
}

CULTURE_HINTS = {
    "spain_neutral": "Español de España, actual y sin región marcada. Tú, y un vale o un ojo cuando encaja.",
    "spain_madrid_urban": "Más directo y con algo de energía urbana. Sin caricatura ni argot forzado.",
    "spain_andalusian_warmth": "Calidez y comparación expresiva del sur, sin imitar acento ni escribir fonética.",
    "spain_catalonia_bilingual_context": "Español preciso. No inventes catalanismos.",
    "latam_neutral": "Español latinoamericano amplio. Ustedes, sin muletillas de España.",
    "mexico_urban": "Calidez mexicana urbana, con vocabulario local solo si el sabor cultural es alto.",
    "argentina_rioplatense": "Ritmo rioplatense y voseo solo si el sabor cultural lo permite. Sin estereotipo.",
    "english_uk": "Ironía seca y subestimación, dichas en español.",
    "english_us": "Directo y práctico, sin entusiasmo artificial, dicho en español.",
}


def load_personalities() -> list[dict]:
    try:
        data = json.loads((ROOT / "personalities.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("personalities") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def find_personality(spoken: str) -> dict | None:
    wanted = _folded_words(spoken)
    if not wanted:
        return None
    for row in load_personalities():
        if wanted == _folded_words(str(row.get("id") or "")):
            return row
        if wanted == _folded_words(str(row.get("name") or "")):
            return row
    return None


def active_personality(cfg: dict) -> dict | None:
    pid = str(cfg.get("personality") or "").strip()
    if not pid:
        return None
    for row in load_personalities():
        if str(row.get("id") or "") == pid:
            return row
    return None


def personality_block(cfg: dict) -> str:
    """Spanish persona text appended to the spoken answer. Empty means the usual assistant."""
    person = active_personality(cfg)
    if person is None:
        return ""
    tone = str(person.get("tone") or "auto")
    culture = str(person.get("culture") or "spain_neutral")
    return (
        f"Persona: {person.get('name')}, {person.get('label')}. {person.get('meaning')}\n"
        f"Tono: {tone}. {TONE_HINTS.get(tone, TONE_HINTS['auto'])}\n"
        f"Cultura: {culture}. {CULTURE_HINTS.get(culture, '')}\n"
        "Rasgos de 0 a 100. Son techos, no una obligación de usarlos siempre: "
        f"intensidad {person.get('intensity')}, humor {person.get('humor')}, "
        f"calidez {person.get('warmth')}, franqueza {person.get('directness')}, "
        f"curiosidad {person.get('curiosity')}, expresividad {person.get('expressiveness')}, "
        f"lenguaje de calle {person.get('street_language')}, sabor cultural {person.get('culture_intensity')}.\n"
        f"Verbosidad {person.get('verbosity')}. Formalidad {person.get('formality')}.\n"
        "Comportamiento, en palabras libres:\n"
        f"{person.get('behavior')}\n"
        "La corrección gana a la persona. No digas qué persona ni qué tono estás usando. "
        "No fuerces un chiste ni una curiosidad. Si el asunto es serio o duele, baja el humor. "
        "Sigue cabiendo en una o dos frases habladas."
    )


def personality_command(text: str) -> tuple[str, str] | None:
    """('show', '') or ('set', spoken id or name). Only the eight people in the file."""
    folded = _folded_words(text)
    words = folded.split()
    if not folded or "personalidad" not in words or len(words) > 6:
        return None
    rest = re.sub(r"^.*\bpersonalidad\b", "", folded).strip()
    rest = re.sub(r"^(?:de|del|la|el|un|una)\s+", "", rest).strip()
    if not rest or rest in {"cual", "que", "activa", "actual", "hay", "tienes"}:
        return ("show", "")
    return ("set", rest)


def ask_grok(
    text: str,
    cfg: dict,
    history: list[tuple[str, str]] | None = None,
    speaker_name: str = "",
    effort: str = "",
) -> str:
    prompt = SYSTEM_PROMPT_ES if str(cfg.get("language") or "en") == "es" else SYSTEM_PROMPT
    if effort == "high":
        prompt += (
            " El usuario pidió más detalle sobre lo mismo. "
            "Responde en español hablado, en varias frases cortas, con lo importante. "
            "Sin listas ni markdown."
        )
        text = f"Explícame con más detalle: {text}"
    if speaker_name:
        prompt += (
            f" La persona que habla se llama {speaker_name}. "
            "Puedes usar su nombre, sin repetirlo en cada frase."
        )
    block = personality_block(cfg)
    if block:
        prompt += "\n" + block
    if history:
        turns = []
        for role, said in history[-8:]:
            who = "Usuario" if role == "user" else "Grok"
            turns.append(f"{who}: {said}")
        text = "Conversación hasta ahora:\n" + "\n".join(turns) + f"\nUsuario: {text}"
    cmd = [
        "grok",
        "-p",
        text,
        "--output-format",
        "json",
        "--max-turns",
        "4",
        "--no-subagents",
        "--no-plan",
        "--always-approve",
        "--verbatim",
        "--no-alt-screen",
        "--reasoning-effort",
        effort or str(cfg.get("reasoning_effort") or "low"),
        "--cwd",
        str(ROOT),
        "--system-prompt-override",
        prompt,
        "--tools",
        "web_search,web_fetch",
        "--disallowed-tools",
        ",".join(
            name
            for name in DISALLOWED_TOOLS.split(",")
            if name not in {"web_search", "web_fetch"}
        ),
    ]
    model = str(cfg.get("grok_model") or "").strip()
    if model:
        cmd.extend(["-m", model])
    agent = active_agent()
    markdown = agent_markdown(agent) if agent else None
    if markdown is not None:
        cmd.extend(["--agent", str(markdown)])
    entry = bind_session(cmd)
    out, err, code = run_grok_watched(cmd, grok_env(), int(cfg["grok_timeout_seconds"]), "Grok")
    if code != 0:
        detail = (err or out or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {code}"
        raise RuntimeError(tail)
    answer = commit_session(entry, out or "")
    if not answer and (out or "").strip() and not (out or "").strip().startswith("{"):
        answer = out.strip()
    if not answer:
        raise RuntimeError("Grok returned an empty answer")
    return answer


def grok_payloads(raw: str) -> list[dict]:
    payloads = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(raw):
        while index < len(raw) and raw[index] not in "{[":
            index += 1
        if index >= len(raw):
            break
        try:
            obj, end = decoder.raw_decode(raw, index)
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(obj, dict):
            payloads.append(obj)
        index = end
    return payloads


def answer_and_session(payloads: list[dict]) -> tuple[str, str]:
    answer = ""
    session = ""
    for obj in payloads:
        sid = obj.get("sessionId") or obj.get("session_id") or ""
        if sid:
            session = str(sid)
        for key in ("text", "result"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                answer = value.strip()
        message = obj.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                answer = content.strip()
            elif isinstance(content, list):
                parts = [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                joined = " ".join(part.strip() for part in parts if part and part.strip())
                if joined:
                    answer = joined
    return answer, session


def load_admin_session() -> str:
    try:
        session_id = ADMIN_SESSION.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return session_id


def save_admin_session(session_id: str) -> None:
    if not session_id:
        return
    ADMIN_SESSION.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_SESSION.write_text(session_id + "\n", encoding="utf-8")
    os.chmod(ADMIN_SESSION, 0o600)


def ask_grok_build(text: str, cfg: dict, session_id: str | None = None) -> tuple[str, str]:
    """Send a spoken task to the one shared Grok Build session."""
    try:
        answer, session = ask_grok_build_once(text, cfg, session_id)
    except RuntimeError:
        if not session_id:
            raise
        log.info("la sesión guardada no se pudo reanudar; abro otra y la dejo como compartida")
        answer, session = ask_grok_build_once(text, cfg, None)
    if session:
        save_admin_session(session)
    return answer, session


def ask_grok_build_once(text: str, cfg: dict, session_id: str | None = None) -> tuple[str, str]:
    """Send a spoken task straight to the Grok Build CLI, tools included."""
    cmd = [
        "grok",
        "-p",
        text,
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--max-turns",
        "12",
        "--no-alt-screen",
        "--reasoning-effort",
        str(cfg.get("reasoning_effort") or "low"),
        "--cwd",
        str(ROOT),
        "--system-prompt-override",
        "The user is speaking a task on a Raspberry Pi. Do the task with your tools. "
        "If a command needs administrator rights, run sudo. It will not ask for a password. "
        "When you finish, reply in short spoken Spanish, one or two sentences, "
        "saying what you did. No markdown.",
    ]
    model = str(cfg.get("grok_model") or "").strip()
    if model:
        cmd.extend(["-m", model])
    entry = bind_session(cmd)
    log.info("tarea al Grok Build CLI")
    env = grok_env()
    sudo_bin = Path.home() / ".config" / "grok-assistant" / "bin"
    if (sudo_bin / "sudo").is_file():
        env["PATH"] = str(sudo_bin) + os.pathsep + env.get("PATH", "")
        env["SUDO_ASKPASS"] = str(Path.home() / ".config" / "grok-assistant" / "sudo-askpass")
    timeout = max(int(cfg.get("grok_timeout_seconds") or 120), 300)
    out, err, code = run_grok_watched(cmd, env, timeout, "Grok Build")
    answer, session = answer_and_session(grok_payloads(out or ""))
    blob = f"{out or ''}\n{err or ''}"
    if code != 0:
        if code < 0 and "grok-assistant" in blob and "restart" in blob:
            log.info("Grok Build reinició el asistente")
            if session or entry.get("id"):
                remember_session(entry, session)
            return answer or "Me estoy reiniciando.", session or str(entry.get("id") or "")
        detail = (err or out or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {code}"
        raise RuntimeError(tail)
    if not answer and (out or "").strip() and not (out or "").strip().startswith("{"):
        answer = out.strip()
    if not answer:
        raise RuntimeError("Grok returned an empty answer")
    remember_session(entry, session)
    return answer, session or str(entry.get("id") or "")


def drop_wake_phrase(text: str) -> str:
    cleaned = re.sub(
        rf"^(hey|hay|he|hi|okay|ok)\s+{GROK_NAME}\b[,.\s]*",
        "",
        text,
        count=1,
        flags=re.I,
    )
    return cleaned.strip()


def record_command(
    mic: Mic,
    floor: NoiseFloor,
    cfg: dict,
    preamble: np.ndarray | None = None,
) -> np.ndarray | None:
    frames: list[np.ndarray] = []
    heard = False
    silence = 0.0
    speech = 0.0
    chunk_s = CHUNK_FRAMES / RATE
    started = time.monotonic()
    if preamble is not None and preamble.size:
        frames.append(preamble)
        if rms(preamble) >= floor.threshold(float(cfg["speech_rms_min"])):
            heard = True
            speech += preamble.size / RATE
    while not STOP:
        block = mic.read_frames()
        if block is None:
            break
        level = rms(block)
        frames.append(block)
        elapsed = time.monotonic() - started
        if level >= floor.threshold(float(cfg["speech_rms_min"])):
            heard = True
            speech += chunk_s
            silence = 0.0
        elif heard:
            silence += chunk_s
        if heard and silence >= float(cfg["silence_seconds"]) and speech >= float(cfg["min_speech_seconds"]):
            break
        if not heard and elapsed >= float(cfg["no_speech_timeout_seconds"]):
            return None
        if elapsed >= float(cfg["max_utterance_seconds"]):
            break
    if not frames or not heard:
        return None
    return np.concatenate(frames).astype(np.float32) / 32768.0


def make_beep() -> Path:
    duration = 0.12
    t = np.arange(int(RATE * duration)) / RATE
    envelope = np.minimum(t / 0.01, 1.0) * np.minimum((duration - t) / 0.02, 1.0)
    tone = np.sin(2 * np.pi * 880 * t) * envelope * 0.25
    path = Path(tempfile.mkstemp(suffix=".wav")[1])
    write_wav(path, tone.astype(np.float32), RATE)
    return path


def show_devices(cfg: dict) -> int:
    print("Playback in use:   ", resolve_playback(cfg))
    print("Playback setting:  ", cfg["playback_device"])
    print("Capture in use:    ", resolve_capture(cfg) or "none")
    caps = list_capture_devices()
    if caps:
        print("Microphones the OS lists:")
        for device in caps:
            print("  ", device)
    else:
        print("Microphones: none")
    return 0


def cmd_say(speech: Speech, cfg: dict, text: str) -> int:
    ok = speak(speech, text, resolve_playback(cfg))
    return 0 if ok else 1


def cmd_ask(speech: Speech, cfg: dict, text: str) -> int:
    try:
        answer = ask_grok(text, cfg)
    except (TimeoutError, RuntimeError) as exc:
        log.error("%s", exc)
        speak(speech, "I could not reach Grok.", resolve_playback(cfg))
        return 1
    print(answer)
    speak(speech, answer, resolve_playback(cfg))
    return 0


def is_hey_grok(result: str) -> bool:
    """True only for the full wake phrase, never for the name alone."""
    token = (result or "").strip().upper()
    return "HEY_GROK" in token


def spot_in_audio(speech: Speech, samples: np.ndarray, sample_rate: int) -> str:
    audio = resample(samples, sample_rate, RATE)
    tail = np.zeros(int(0.7 * RATE), dtype=np.float32)
    stream = speech.kws.create_stream()
    stream.accept_waveform(RATE, audio)
    stream.accept_waveform(RATE, tail)
    stream.input_finished()
    heard = ""
    while speech.kws.is_ready(stream):
        speech.kws.decode_stream(stream)
        result = speech.kws.get_result(stream)
        text = result if isinstance(result, str) else str(getattr(result, "keyword", result))
        if is_hey_grok(text):
            heard = text
            speech.kws.reset_stream(stream)
        elif text:
            speech.kws.reset_stream(stream)
    return heard


def self_test(speech: Speech, cfg: dict, with_grok: bool) -> int:
    if speech.language == "es":
        sample = ES_DIR / "test_wavs" / "0.wav"
        with wave.open(str(sample)) as handle:
            audio = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
            rate = handle.getframerate()
        if getattr(speech, "asr_id", "") != "kroko":
            speech._select_asr("kroko")
        stream = speech.new_stream()
        speech.feed_spanish(stream, audio)
        speech.feed_spanish(stream, np.zeros(int(0.4 * rate), dtype=np.int16))
        text = speech.finish_spanish(stream)
        print("spanish model heard:", text)
        if "pais" not in fold_text(text):
            print("FAIL: Spanish model did not transcribe the sample")
            return 1
        print("Wake phrase is: despierta grok")
        return 0
    print("Checking that only the full phrase Hey Grok wakes the assistant...")
    wake_audio, wake_rate = speech.synthesize("hey grok")
    spotted = spot_in_audio(speech, wake_audio, wake_rate)
    print("hey grok heard:", spotted or "(nothing)")
    bare = spot_in_audio(speech, *speech.synthesize("grok"))
    print("grok alone heard:", bare or "(nothing)")
    other = spot_in_audio(speech, *speech.synthesize("hi grok"))
    print("hi grok heard:", other or "(nothing)")
    print("Synthesizing a question and checking speech to text...")
    question = "What is two plus two?"
    q_audio, q_rate = speech.synthesize(question)
    heard = speech.transcribe(q_audio, q_rate)
    print("speech to text heard:", heard or "(nothing)")
    failed = 0
    if not spotted:
        print("FAIL: Hey Grok was not detected")
        failed = 1
    if bare or other:
        print("FAIL: a phrase other than Hey Grok woke the assistant")
        failed = 1
    if "two" not in heard.lower() and "2" not in heard:
        print("FAIL: transcription did not contain the question")
        failed = 1
    if with_grok:
        print("Asking Grok one short question...")
        try:
            answer = ask_grok("What is two plus two? Answer with one short sentence.", cfg)
        except (TimeoutError, RuntimeError) as exc:
            print("FAIL: Grok:", exc)
            return 1
        print("Grok:", answer)
        speak(speech, answer, resolve_playback(cfg))
    if failed:
        return failed
    print("Software pipeline is working.")
    caps = list_capture_devices()
    if not caps:
        print("No microphone is plugged in, so the live wake loop cannot start yet.")
    return 0


def command_line(answer: str) -> str:
    """A local command Grok asked to run, or empty."""
    for line in answer.splitlines():
        raw = line.strip()
        if fold_text(raw).startswith("comando:"):
            return raw.split(":", 1)[1].strip()
    return ""


def apply_waiting_command(
    speech: Speech,
    speaker: str,
    cfg: dict,
    display: HeardDisplay,
    text: str,
    state: dict,
    allow_admin: bool = True,
    admin_active: bool = False,
) -> str:
    """Handle a phrase heard while no conversation is open.

    Returns "admin" when the passphrase opens an admin conversation,
    "used" when a local command was done, or "" when the phrase is not one.
    Nothing here is sent to the conversation session.
    """
    if is_stt_test(text):
        display.set_test(True)
        speak(speech, "Modo prueba. Di salir para terminar.", speaker)
        return "test"
    if is_voice_enroll(text):
        return "enroll"
    if state.get("off"):
        answer = confirms(text)
        if answer is True:
            state["off"] = False
            speak(speech, "Apagando.", speaker)
            if not shutdown_device():
                speak(speech, "No pude apagar.", speaker)
            return "used"
        if answer is False:
            state["off"] = False
            speak(speech, "Sigo encendido.", speaker)
            return "used"
        speak(speech, "Di sí para apagar, o no para seguir.", speaker)
        return "used"
    if state.get("agent"):
        if confirms(text) is False:
            state["agent"] = ""
            speak(speech, "No creo el agente.", speaker)
            return "used"
        if is_passphrase(text, cfg):
            name = str(state.get("agent") or "")
            state["agent"] = ""
            write_agent(name)
            speak(speech, f"Agente {name} creado.", speaker)
            return "used"
        speak(speech, "No he oído la clave. Repítela.", speaker)
        return "used"
    if state.get("key"):
        if is_passphrase(text, cfg):
            state["key"] = False
            speak(speech, "Modo admin activo.", speaker)
            return "admin"
        speak(speech, "No he oído la clave. Repítela.", speaker)
        return "used"
    if state.get("create"):
        answer = confirms(text)
        spoken = session_label(state["create"])
        if answer is True:
            data = load_sessions()
            data["named"][state["create"]] = fresh_entry()
            data["active"] = state["create"]
            save_sessions(data)
            state["create"] = ""
            speak(speech, f"Sesión {spoken} creada y abierta. No caduca.", speaker)
        elif answer is False:
            state["create"] = ""
            speak(speech, "No creo esa sesión.", speaker)
        else:
            speak(speech, f"He oído {spoken}. Di sí si es correcto, o no para cancelar.", speaker)
        return "used"
    if state.get("forget"):
        spoken = str(state["forget"])
        answer = confirms(text)
        if answer is True:
            removed_lock = fold_text(speakers().locked) == fold_text(spoken)
            speakers().remove_person(spoken)
            state["forget"] = ""
            if removed_lock:
                speak(speech, f"Borrado {spoken}. Ya no hay una voz exclusiva.", speaker)
            else:
                speak(speech, f"Borrado {spoken}.", speaker)
        elif answer is False:
            state["forget"] = ""
            speak(speech, "No borro a nadie.", speaker)
        else:
            speak(speech, "Di sí para borrar, o no para seguir.", speaker)
        return "used"
    if state.get("delete"):
        spoken = session_label(state["delete"])
        answer = confirms(text)
        if answer is True:
            data = load_sessions()
            entry = data.get("named", {}).pop(state["delete"], None)
            if entry:
                delete_grok_session(str(entry.get("id") or ""))
            if data.get("active") == state["delete"]:
                data["active"] = SHARED_NAME
            save_sessions(data)
            state["delete"] = ""
            speak(speech, f"Sesión {spoken} borrada.", speaker)
        elif answer is False:
            state["delete"] = ""
            speak(speech, "No borro nada.", speaker)
        else:
            speak(speech, "Di sí para borrar, o no para seguir.", speaker)
        return "used"
    number = voice_number(text)
    if number:
        label = speech.use_voice_number(number)
        total = len(getattr(speech, "voice_specs", []) or [])
        if not label:
            speak(speech, f"No tengo la voz {number}. Hay {total}.", speaker)
        else:
            speak(speech, f"Voz {number}. {label}.", speaker)
        display.note_voice(speech)
        return "used"
    if is_voice_change(text):
        label = speech.next_voice()
        if not label:
            speak(speech, "Solo tengo una voz.", speaker)
        else:
            shown = int(getattr(speech, "voice_index", 0)) + 1
            speak(speech, f"Voz {shown}. {label}.", speaker)
        display.note_voice(speech)
        return "used"
    named = assistant_name_command(text)
    if named:
        kind, wanted = named
        if kind == "show":
            speak(speech, f"Me llamo {ASSISTANT_NAME}. Para empezar se dice {assistant_call()}.", speaker)
        else:
            cleaned = clean_assistant_name(wanted)
            if not cleaned:
                speak(speech, "Ese nombre no vale. Di un nombre corto.", speaker)
            else:
                cfg["assistant_name"] = apply_assistant_name(cleaned)
                save_user_config({"assistant_name": cfg["assistant_name"]})
                speak(speech, f"A partir de ahora se dice {assistant_call()}.", speaker)
                display._static_ready = False
                display._draw(display.shown, display.level)
        return "used"
    persona = personality_command(text)
    if persona:
        kind, wanted = persona
        if kind == "show":
            person = active_personality(cfg)
            if person is None:
                speak(speech, "Sin personalidad.", speaker)
            else:
                speak(speech, f"Personalidad {person.get('name')}.", speaker)
        else:
            found = find_personality(wanted)
            if found is None:
                speak(speech, "No tengo esa personalidad.", speaker)
            else:
                cfg["personality"] = str(found.get("id") or "")
                save_user_config({"personality": cfg["personality"]})
                speak(speech, f"Personalidad {found.get('name')}.", speaker)
        return "used"
    picked = asr_choice(text)
    if picked:
        if picked != "next" and picked not in {item["id"] for item in available_asrs()}:
            missing = next((item["label"] for item in asr_catalog() if item["id"] == picked), picked)
            speak(speech, f"{missing} no está. Sigo con {speech.asr_label}.", speaker)
            return "used"
        label = speech.cycle_asr() if picked == "next" else speech._select_asr(picked)
        display.set_asr_name(speech.asr_label)
        names = ", ".join(item["label"] for item in available_asrs())
        speak(speech, f"Reconocedor {label}. Hay {names}.", speaker)
        return "used"
    song = music_command(text)
    if song:
        action, query = song
        if action == "stop":
            music.stop()
            speak(speech, "Música parada.", speaker)
            return "used"
        if action == "pause":
            music.user_pause()
            speak(speech, "Música en pausa.", speaker)
            return "used"
        if action == "resume":
            music.user_resume()
            speak(speech, "Sigue la música.", speaker)
            return "used"
        title = music.play(query, speaker)
        if not title:
            speak(speech, "No encontré esa canción.", speaker)
        else:
            speak(speech, f"Pongo {title}.", speaker)
        return "used"
    direction = volume_command(text)
    if direction:
        log.info("volumen: %s", direction)
        speak(speech, change_volume(direction), speaker)
        display.note_volume()
        return "used"
    if is_help(text):
        log.info("ayuda en pantalla: %s", text)
        display.show_commands(help_lines())
        speak(speech, help_speech(), speaker)
        return "used"
    agent = agent_command(text)
    if agent:
        kind, name = agent
        if kind == "list":
            speak(speech, agent_list_line(), speaker)
        elif kind == "close":
            if not active_agent():
                speak(speech, "No hay ningún agente abierto.", speaker)
            else:
                set_active_agent("")
                speak(speech, "Vuelta al asistente.", speaker)
        elif kind == "create":
            if not name:
                speak(speech, "No he oído el nombre del agente.", speaker)
            elif resolve_agent(name):
                speak(speech, f"El agente {name} ya existe.", speaker)
            else:
                state["agent"] = name
                speak(speech, f"Para crear el agente {name} di la clave.", speaker)
        else:
            found = resolve_agent(name) if name else None
            if not name:
                speak(speech, "No he oído el nombre del agente.", speaker)
            elif found is None:
                speak(speech, "No tengo ese agente.", speaker)
            else:
                stem, _path = found
                remember_agent_id(stem)
                set_active_agent(stem)
                speak(speech, f"Agente {stem}.", speaker)
                return "agent"
        return "used"
    command = session_command(text)
    if command:
        kind, name = command
        if kind == "create":
            if not name:
                speak(speech, "No he oído el nombre de la sesión.", speaker)
            else:
                state["create"] = name
                speak(speech, f"He oído {session_label(name)}. Di sí si es correcto, o no para cancelar.", speaker)
        elif kind == "delete":
            data = load_sessions()
            if name not in data.get("named", {}):
                speak(speech, "No tengo una sesión con ese nombre.", speaker)
            else:
                state["delete"] = name
                speak(speech, f"¿Borro la sesión {session_label(name)}? Di sí o no.", speaker)
        else:
            if kind == "close" and display.in_conversation:
                apply_session_command(kind, name)
                speak(speech, "Sesión cerrada.", speaker)
                return "wait"
            speak(speech, apply_session_command(kind, name), speaker)
        return "used"
    if is_shutdown(text):
        state["off"] = True
        speak(speech, "¿Apago el dispositivo? Di sí o no.", speaker)
        return "used"
    listed = people_command(text)
    if listed:
        if not admin_active:
            speak(speech, "Para eso necesito el modo administrador.", speaker)
            return "used"
        kind, person_name_heard = listed
        if kind == "list":
            names = [str(person.get("name") or "") for person in speakers().people if person.get("name")]
            if not names:
                speak(speech, "No hay personas identificadas.", speaker)
            else:
                speak(speech, "Personas identificadas: " + ", ".join(names) + ".", speaker)
        else:
            if not person_name_heard or not any(
                fold_text(str(person.get("name") or "")) == fold_text(person_name_heard)
                for person in speakers().people
            ):
                speak(speech, "No tengo esa persona.", speaker)
            else:
                state["forget"] = person_name_heard
                speak(speech, f"¿Borro a {person_name_heard}? Di sí o no.", speaker)
        return "used"
    if allow_admin and is_admin_request(text):
        state["key"] = True
        log.info("piden modo admin")
        speak(speech, "Di la clave.", speaker)
        return "used"
    return ""


def listen_spanish(
    speech: Speech,
    mic: Mic,
    floor: NoiseFloor,
    speaker: str,
    beep: Path,
    cfg: dict,
    display: HeardDisplay,
) -> None:
    stream = speech.new_stream()
    quiet = 0.0
    utterance: list[np.ndarray] = []
    pending = ""; utterance.clear()
    paused_clear = False
    waiting = {"key": False, "create": "", "delete": "", "off": False}
    room = VoiceRoom(speech, cfg) if speakers().extractor is not None else None
    asked: dict = {"emb": None}
    voice_audio: deque[np.ndarray] = deque(maxlen=40)
    sticky = ""
    chunk_s = CHUNK_FRAMES / RATE
    silence_s = float(cfg["silence_seconds"])

    def _from_voices(block: np.ndarray, loud: bool, level: float) -> None:
        nonlocal stream, room, sticky, pending, quiet
        events = room.feed(block, loud)
        partial = ""
        finals = []
        for event in events:
            if event[0] == "partial":
                partial = event[1]
            else:
                finals.append(event)
        if partial:
            sticky = partial
            display.update(partial, level)
        elif loud:
            display.update(sticky or "escuchando…", level)
        elif not finals:
            display.update(sticky or "(silencio)", level)
        if waiting.get("off") or waiting.get("confirm"):
            for lane in list(room.lanes):
                words = _folded_words(lane.text).split() if lane.text else []
                if not lane.text or len(words) > 2 or confirms(lane.text) is None:
                    continue
                audio = np.concatenate(lane.audio) if lane.audio else None
                finals.insert(0, ("final", lane.label, lane.text, audio, lane.embedding, lane.saved_index))
                room.reset()
                break
        for lane in list(room.lanes):
            if not lane.text:
                continue
            audio = np.concatenate(lane.audio) if lane.audio else None
            heard_text = lane.text if display.in_test else second_reading(speech, lane.text, audio)
            if not opens_talk(heard_text):
                continue
            heard = command_body(heard_text) or heard_text
            if is_voice_enroll(heard):
                if not enroll_open(lane.embedding, audio):
                    continue
            elif not allowed_voice(lane.embedding, audio, lane.label):
                continue
            log.info("hola, sin búsqueda: %s", heard_text)
            if not display.listening:
                return
            speak(speech, wake_reply(heard_text), speaker)
            run_conversation(
                speech, mic, stream, floor, speaker, cfg, display, "",
                admin=False, samples=audio, skip_hello=True, owner=lane.embedding,
            )
            stream = speech.new_stream()
            room = VoiceRoom(speech, cfg)
            return
        for _kind, label, text, audio, embedding, _saved in finals:
            if not display.in_test:
                text = second_reading(speech, text, audio)
            order_text = command_body(text)
            if order_text is None:
                order_text = text
            if is_voice_enroll(order_text):
                if not enroll_open(embedding, audio):
                    sticky = f"{label}: {text}"
                    display.update(sticky, level)
                    continue
            elif not allowed_voice(embedding, audio, label):
                sticky = f"{label}: {text}"
                display.update(sticky, level)
                continue
            if display.in_test:
                line = f"{label}: {text}"
                sticky = line
                display.update(line, level)
                if is_leave_test(text):
                    log.info("fin de la prueba: %s", text)
                    display.set_test(False)
                    speak(speech, "Salgo de la prueba.", speaker)
                    room = VoiceRoom(speech, cfg)
                continue
            pending_answer = bool(
                waiting.get("off") or waiting.get("create") or waiting.get("delete")
                or waiting.get("key") or waiting.get("agent") or waiting.get("confirm")
            )
            if pending_answer and asked["emb"] is not None and embedding is not None and not same_voice(embedding, asked["emb"]):
                log.info("huella distinta en la respuesta, la acepto igual (%s): %s", label, text)
            if not pending_answer and not voice_addresses_grok(text):
                log.info("no es para mí (%s): %s", label, text)
                sticky = f"{label}: {text}"
                display.update(sticky, level)
                continue
            log.info("para grok (%s): %s", label, text)
            asked["emb"] = embedding
            sticky = f"{label}: {text}"
            display.update(sticky, level)
            shown = text
            if opens_talk(shown):
                log.info("hola, sin búsqueda: %s", shown)
                if not display.listening:
                    continue
                speak(speech, wake_reply(shown), speaker)
                run_conversation(
                    speech, mic, stream, floor, speaker, cfg, display, "",
                    admin=False, samples=audio, skip_hello=True, owner=embedding,
                )
                stream = speech.new_stream()
                room = VoiceRoom(speech, cfg)
                continue
            # A line that starts with "comando" is the user, even if the
            # print is close to the assistant. "con" for "pon" still goes to Grok.
            if command_body(shown) is None and (is_own_words(shown) or speakers().is_self(audio)):
                log.info("eco, lo suelto: %s", shown)
                continue
            if waiting.get("confirm"):
                orden = str(waiting.pop("confirm"))
                choice = confirms(shown)
                if choice is True:
                    log.info("confirmado: %s", orden)
                    action = apply_waiting_command(speech, speaker, cfg, display, orden, waiting)
                    if action == "agent":
                        run_conversation(
                            speech, mic, stream, floor, speaker, cfg, display, "",
                            admin=False, skip_hello=True, owner=embedding,
                        )
                    elif action == "admin":
                        run_conversation(
                            speech, mic, stream, floor, speaker, cfg, display, "",
                            admin=True, samples=audio, owner=embedding,
                        )
                    elif action != "used":
                        speak(speech, "No he podido hacer ese comando.", speaker)
                else:
                    waiting["confirm"] = ""
                    speak(speech, "Vale.", speaker)
                stream = speech.new_stream()
                continue
            body = command_body(shown)
            if not pending_answer and body is None and (is_goodbye(shown) or is_nothing_needed(shown)):
                speak(
                    speech,
                    "Vale." if is_nothing_needed(shown) and not is_goodbye(shown) else goodbye_reply(shown),
                    speaker,
                )
                continue
            if body is not None and not body:
                speak(speech, "No he oído el comando.", speaker)
                continue
            order = body if body is not None else shown
            if body is not None and len(_folded_words(order).split()) > 16:
                speak(speech, "El comando es demasiado largo.", speaker)
                continue
            if is_goodbye(order) or is_nothing_needed(order):
                speak(
                    speech,
                    "Vale." if is_nothing_needed(order) and not is_goodbye(order) else goodbye_reply(order),
                    speaker,
                )
                continue
            asr_before = speech.asr_id
            action = apply_waiting_command(speech, speaker, cfg, display, order, waiting)
            if not action and body is not None:
                display.update("Interpretando … buscando en la nube", level)
                kind, payload = interpret(order)
                if kind == "comando" and payload and not is_goodbye(order):
                    waiting["confirm"] = payload
                    spoken = spoken_command(payload)
                    log.info("confirmo comando: %s", payload)
                    speak(speech, f"Has dicho: {spoken}. ¿Sí o no?", speaker)
                    action = "used"
                else:
                    speak(speech, "No conozco ese comando.", speaker)
                    action = "used"
            if action == "enroll":
                run_voice_enrollment(speech, mic, floor, cfg, display, speaker, embedding)
                room = VoiceRoom(speech, cfg)
            if action == "agent":
                run_conversation(
                    speech, mic, stream, floor, speaker, cfg, display, "",
                    admin=False, skip_hello=True, owner=embedding,
                )
            if action == "admin":
                run_conversation(
                    speech, mic, stream, floor, speaker, cfg, display, "",
                    admin=True, samples=audio, owner=embedding,
                )
            stream = speech.new_stream()
            if speech.asr_id != asr_before or action == "enroll":
                room = VoiceRoom(speech, cfg)

    while not STOP:
        block = mic.read_frames()
        if block is None:
            log.warning("microphone read failed, reopening")
            return
        level = rms(block)
        floor.observe(level)
        voice_audio.append(np.array(block, copy=True))
        service_touch(display, speech, speaker)
        if not display.listening:
            if not paused_clear:
                speech.reset_stream(stream)
                if room is not None:
                    room.reset()
                pending = ""; utterance.clear()
                quiet = 0.0
                paused_clear = True
            held = display.shown if display.shown not in {"", "(silencio)"} else "(en pausa)"
            display.update(held, level)
            continue
        paused_clear = False
        loud = level >= floor.threshold(float(cfg["speech_rms_min"]))
        if room is not None:
            _from_voices(block, loud, level)
            continue
        if loud and quiet >= silence_s:
            utterance.clear()
        if loud or (utterance and quiet < silence_s):
            utterance.append(np.array(block, copy=True))
        if speech.keep_audio(stream, loud, quiet):
            text = (speech.feed_spanish(stream, block) or "").strip()
        else:
            text = (speech.partial_spanish(stream) or "").strip()
        if text and text != pending:
            quiet = 0.0
        if text:
            pending = text
        if loud:
            sticky = ""
            quiet = 0.0
        else:
            quiet += chunk_s
        over_cap = False
        if speech.buffered(stream):
            frames = sum(int(chunk.size) for chunk in stream["chunks"])
            over_cap = frames >= float(cfg["max_utterance_seconds"]) * RATE
        if speech.buffered(stream) and not loud and (quiet >= silence_s or over_cap):
            final = (speech.finish_spanish(stream) or "").strip()
            if final:
                pending = final
                text = final
        shown = (text or pending).strip()
        if shown and not loud:
            sticky = shown
        display.update(shown or sticky or "(silencio)", level)
        if display.in_test:
            # Only the recognizer text. No commands and no Grok until "salir".
            if shown and not loud and quiet >= silence_s and is_leave_test(shown):
                log.info("fin de la prueba: %s", shown)
                display.set_test(False)
                speak(speech, "Salgo de la prueba.", speaker)
                speech.reset_stream(stream)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                sticky = ""
            elif shown and not loud and quiet >= silence_s:
                speech.reset_stream(stream)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
            continue
        if shown and command_body(shown) is None and is_own_words(shown):
            log.info("eco, lo suelto: %s", shown)
            speech.reset_stream(stream)
            quiet = 0.0
            pending = ""; utterance.clear()
            continue
        if opens_talk(shown):
            # The wake only opens the talk. Do not search, and do not send
            # the words that came with it.
            log.info("hola, sin búsqueda: %s", shown)
            samples = np.concatenate(list(voice_audio)) if voice_audio else None
            if not display.listening:
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                continue
            speak(speech, wake_reply(shown), speaker)
            run_conversation(
                speech, mic, stream, floor, speaker, cfg, display, "", admin=False, samples=samples, skip_hello=True
            )
            stream = speech.new_stream()
            quiet = 0.0
            pending = ""; utterance.clear()
            continue
        # Exact commands run here, once the phrase has ended. Cutting at six
        # words while the person is still talking wiped "pon la canción" plus
        # the title before the recognizer could finish it. A finished phrase
        # longer than six words is dropped below, unless it is a song request.
        if shown and not loud and quiet >= silence_s:
            heard_samples = np.concatenate(utterance) if utterance else None
            if not display.in_test:
                shown = second_reading(speech, shown, heard_samples)
                pending = shown
                text = shown
            if command_body(shown) is None and speakers().is_self(heard_samples):
                log.info("eco de mi voz, lo suelto: %s", shown)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                continue
            if waiting.get("confirm"):
                orden = str(waiting.pop("confirm"))
                choice = confirms(shown)
                if choice is True:
                    log.info("confirmado: %s", orden)
                    display.update(orden, level)
                    action = apply_waiting_command(speech, speaker, cfg, display, orden, waiting)
                    if action == "agent":
                        run_conversation(
                            speech, mic, stream, floor, speaker, cfg, display, "", admin=False, skip_hello=True
                        )
                    elif action == "admin":
                        samples = np.concatenate(list(voice_audio)) if voice_audio else None
                        run_conversation(
                            speech, mic, stream, floor, speaker, cfg, display, "", admin=True, samples=samples
                        )
                    elif action != "used":
                        speak(speech, "No he podido hacer ese comando.", speaker)
                elif choice is False:
                    speak(speech, "Vale.", speaker)
                else:
                    waiting["confirm"] = ""
                    speak(speech, "Vale.", speaker)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                continue
            # A command starts with "comando". Anything else stays out of
            # the cloud, unless a yes/no or a key is already expected.
            pending_answer = bool(
                waiting.get("off") or waiting.get("create") or waiting.get("delete")
                or waiting.get("key") or waiting.get("agent")
            )
            body = command_body(shown)
            if not pending_answer and body is None and (is_goodbye(shown) or is_nothing_needed(shown)):
                log.info("cierre oído fuera de conversación: %s", shown)
                speak(
                    speech,
                    "Vale." if is_nothing_needed(shown) and not is_goodbye(shown) else goodbye_reply(shown),
                    speaker,
                )
                speech.reset_stream(stream)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                continue
            if not pending_answer and body is None:
                log.info("sin comando, lo dejo: %s", shown)
                speech.reset_stream(stream)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                continue
            if body is not None and not body:
                speak(speech, "No he oído el comando.", speaker)
                speech.reset_stream(stream)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                continue
            order = body if body is not None else shown
            if body is not None and len(_folded_words(order).split()) > 16:
                log.info("comando demasiado largo: %s", order)
                speak(speech, "El comando es demasiado largo.", speaker)
                speech.reset_stream(stream)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                continue
            if is_goodbye(order) or is_nothing_needed(order):
                log.info("cierre oído fuera de conversación: %s", order)
                speak(speech, "Vale." if is_nothing_needed(order) and not is_goodbye(order) else goodbye_reply(order), speaker)
                speech.reset_stream(stream)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                continue
            action = apply_waiting_command(speech, speaker, cfg, display, order, waiting)
            if action == "enroll":
                run_voice_enrollment(speech, mic, floor, cfg, display, speaker)
                speech.reset_stream(stream)
                stream = speech.new_stream()
                quiet = 0.0
                pending = ""; utterance.clear()
                continue
            if not action and body is not None:
                # The word comando was there, but the rest is not an exact order.
                # Grok may repair it. It does not open a conversation.
                display.update("Interpretando … buscando en la nube", level)
                kind, payload = interpret(order)
                if kind == "comando" and payload and not is_goodbye(order):
                    waiting["confirm"] = payload
                    spoken = spoken_command(payload)
                    log.info("confirmo comando: %s", payload)
                    display.update(spoken, level)
                    speak(speech, f"Has dicho: {spoken}. ¿Sí o no?", speaker)
                    action = "used"
                else:
                    log.info("no conozco ese comando: %s", order)
                    speak(speech, "No conozco ese comando.", speaker)
                    action = "used"
            if action == "agent":
                run_conversation(
                    speech, mic, stream, floor, speaker, cfg, display, "", admin=False, skip_hello=True
                )
            if action == "admin":
                samples = np.concatenate(voice_audio) if voice_audio else None
                run_conversation(
                    speech, mic, stream, floor, speaker, cfg, display, "", admin=True, samples=samples
                )
            # The recognizer may have been replaced. Start a stream of the new kind.
            stream = speech.new_stream()
            quiet = 0.0
            pending = ""; utterance.clear()
            continue
        if not shown and quiet >= silence_s and speech.asr_kind == "streaming":
            speech.reset_stream(stream)
            quiet = 0.0
            pending = ""; utterance.clear()


def run_conversation(
    speech: Speech,
    mic: Mic,
    stream,
    floor: NoiseFloor,
    speaker: str,
    cfg: dict,
    display: HeardDisplay,
    opening: str,
    admin: bool = False,
    samples: np.ndarray | None = None,
    skip_hello: bool = False,
    owner: np.ndarray | None = None,
) -> None:
    idle = float(cfg.get("idle_seconds") or 60)
    log.info(
        "conversación abierta%s, cierro con 'comando ok gracias' o %ss en silencio",
        " en admin" if admin else "",
        int(idle),
    )
    admin_session = ""
    phrase = (opening or "").strip()
    local = {"key": False, "create": "", "delete": "", "forget": "", "off": False, "detail": "", "offer": "", "task": ""}
    admin_until = time.monotonic() + 300 if admin else 0.0
    display.set_conversation(True)
    if owner is None and samples is not None:
        owner = speakers().embed(samples)
    speaker_name = ""
    speaker_index = -1
    embedding = speakers().embed(samples)
    if embedding is not None and speakers().is_self(samples):
        log.info("la voz de apertura es la mía, no la guardo")
        embedding = None
    if embedding is not None:
        speaker_name, speaker_index = speakers().match(embedding)
    if skip_hello:
        pass
    elif speaker_name and not phrase:
        speak(speech, opening_greeting(speakers(), speaker_index, speaker_name), speaker)
    elif embedding is not None and not speaker_name:
        speak(speech, "Hola, ¿cómo te llamas?", speaker)
        heard_name = wait_for_phrase(speech, mic, floor, cfg, display, idle, owner)
        if not display.listening or not heard_name:
            display.set_conversation(False)
            return
        if is_nothing_needed(heard_name):
            speak(speech, "Vale.", speaker)
            display.set_conversation(False)
            return
        if is_goodbye(heard_name):
            speak(speech, goodbye_reply(heard_name), speaker)
            display.set_conversation(False)
            return
        name = person_name(heard_name)
        if name:
            speaker_name = name
            speak(speech, f"Hola, {name}. Para que te reconozca siempre, di comando identifica mi voz.", speaker)
        if not phrase:
            speak(speech, "Dime.", speaker)
    elif not phrase:
        speak(speech, "Dime.", speaker)
    try:
        while not STOP:
            service_touch(display, speech, speaker)
            if not display.listening:
                log.info("conversación en pausa")
                return
            busy = bool(local["off"] or local["create"] or local["delete"] or local.get("forget"))
            if phrase and is_own_words(phrase):
                log.info("ignoro mi eco en la conversación: %s", phrase)
                phrase = ""
            if phrase and local.get("confirm") and not busy:
                orden = str(local.pop("confirm"))
                choice = confirms(phrase)
                if choice is True:
                    log.info("confirmado: %s", orden)
                    action = apply_waiting_command(
                        speech, speaker, cfg, display, orden, local, allow_admin=False, admin_active=admin
                    )
                    if action == "test":
                        return
                    if action == "wait":
                        return
                    if action not in {"used", "agent"}:
                        speak(speech, "No he podido hacer ese comando.", speaker)
                else:
                    speak(speech, "Vale.", speaker)
                phrase = ""
            if phrase and local["detail"] and not busy:
                more = confirms(phrase)
                question = local["detail"]
                if more is True:
                    local["detail"] = ""
                    log.info("más detalle: %s", question)
                    try:
                        begin_cloud_wait(speech, speaker, display)
                        longer = ask_grok(question, cfg, speaker_name=speaker_name, effort="high")
                    except TimeoutError:
                        longer = "Tardé demasiado con el detalle."
                    except RuntimeError as exc:
                        log.error("grok failed: %s", exc)
                        longer = "No pude ampliarlo."
                    display.set_searching(False)
                    speak(speech, longer, speaker)
                    phrase = ""
                elif more is False:
                    local["detail"] = ""
                    speak(speech, "Vale.", speaker)
                    phrase = ""
                else:
                    local["detail"] = ""

            if admin and admin_until and time.monotonic() >= admin_until:
                admin = False
                admin_until = 0.0
                local["offer"] = ""
                speak(speech, "Se acabó el modo admin. Sigo en la conversación normal.", speaker)
            if phrase and local.get("offer") == "admin" and not busy:
                choice = confirms(phrase)
                if choice is True:
                    local["offer"] = "key"
                    speak(speech, "Di la clave.", speaker)
                elif choice is False:
                    local["offer"] = ""
                    local["task"] = ""
                    speak(speech, "Sigo sin admin.", speaker)
                else:
                    local["offer"] = ""
                    local["task"] = ""
                if choice is not None:
                    phrase = ""
            elif phrase and local.get("offer") == "key" and not busy:
                if is_passphrase(phrase, cfg):
                    task = local.get("task") or ""
                    local["offer"] = ""
                    local["task"] = ""
                    admin = True
                    admin_until = time.monotonic() + 300
                    speak(speech, "Modo admin activo.", speaker)
                    if task:
                        log.info("tarea admin: %s", task)
                        try:
                            answer, admin_session = ask_grok_build(task, cfg, admin_session or None)
                        except TimeoutError:
                            answer = "Tardé demasiado. Repite la tarea."
                        except RuntimeError as exc:
                            log.error("grok failed: %s", exc)
                            answer = "No pude hablar con Grok."
                        speak(speech, answer, speaker)
                    local["offer"] = "exit"
                    speak(speech, "¿Quieres salir del modo admin?", speaker)
                elif confirms(phrase) is False:
                    local["offer"] = ""
                    local["task"] = ""
                    speak(speech, "Sigo sin admin.", speaker)
                else:
                    speak(speech, "No he oído la clave. Repítela.", speaker)
                phrase = ""
            elif phrase and local.get("offer") == "exit" and not busy:
                choice = confirms(phrase)
                if choice is True:
                    admin = False
                    admin_until = 0.0
                    local["offer"] = ""
                    speak(speech, "Sigo en la conversación normal.", speaker)
                    phrase = ""
                elif choice is False:
                    local["offer"] = ""
                    speak(speech, "Sigo en admin.", speaker)
                    phrase = ""
                else:
                    local["offer"] = ""
            if phrase and busy:
                action = apply_waiting_command(
                    speech, speaker, cfg, display, phrase, local, allow_admin=False, admin_active=admin
                )
                if action == "test":
                    return
                if action == "wait":
                    return
                phrase = ""
            elif phrase and not busy and command_body(phrase) is None and (
                is_goodbye(phrase) or is_nothing_needed(phrase)
            ):
                log.info("conversación cerrada: %s", phrase)
                speak(
                    speech,
                    "Vale." if is_nothing_needed(phrase) and not is_goodbye(phrase) else goodbye_reply(phrase),
                    speaker,
                )
                return
            elif phrase:
                body = command_body(phrase)
                action = ""
                if body is None and music_command(phrase):
                    log.info("canción en la conversación: %s", phrase)
                    action = apply_waiting_command(
                        speech, speaker, cfg, display, phrase, local, allow_admin=False, admin_active=admin
                    )
                if body is not None and is_voice_enroll(body):
                    run_voice_enrollment(speech, mic, floor, cfg, display, speaker, owner)
                    phrase = ""
                    action = "used"
                elif body is not None:
                    log.info("comando: %s", body or "(vacío)")
                    if not body:
                        speak(speech, "No he oído el comando.", speaker)
                        phrase = ""
                        action = "used"
                    elif is_goodbye(body):
                        log.info("conversación cerrada: %s", body)
                        speak(speech, goodbye_reply(body), speaker)
                        return
                    elif is_nothing_needed(body):
                        log.info("conversación cerrada: no necesita nada")
                        speak(speech, "Vale.", speaker)
                        return
                    else:
                        action = apply_waiting_command(
                            speech, speaker, cfg, display, body, local, allow_admin=False, admin_active=admin
                        )
                        if action not in {"used", "agent", "test", "wait"}:
                            display.update("Interpretando … buscando en la nube", display.level)
                            kind, payload = interpret(body)
                            if kind == "comando" and payload and not is_goodbye(body):
                                spoken = spoken_command(payload)
                                log.info("confirmo comando: %s", payload)
                                local["confirm"] = payload
                                display.update(spoken, display.level)
                                speak(speech, f"Has dicho: {spoken}. ¿Sí o no?", speaker)
                                action = "used"
                            else:
                                log.info("no conozco ese comando: %s", body)
                                speak(speech, "No conozco ese comando.", speaker)
                                action = "used"
                if action == "test":
                    return
                if action == "wait":
                    return
                if action == "agent":
                    phrase = ""
                elif action == "used":
                    phrase = ""
                else:
                    log.info("a grok: %s", phrase)
                    begin_cloud_wait(speech, speaker, display)
                    try:
                        if admin:
                            answer, admin_session = ask_grok_build(phrase, cfg, admin_session or None)
                        else:
                            answer = ask_grok(phrase, cfg, speaker_name=speaker_name)
                    except TimeoutError:
                        answer = "Tardé demasiado. Repite la tarea." if admin else "Tardé demasiado. Repite la pregunta."
                    except RuntimeError as exc:
                        log.error("grok failed: %s", exc)
                        answer = "No pude hablar con Grok."
                    display.set_searching(False)
                    requested = command_line(answer)
                    if requested:
                        log.info("grok pide comando: %s", requested)
                        done = apply_waiting_command(
                            speech, speaker, cfg, display, requested, local, allow_admin=False, admin_active=admin
                        )
                        if done == "wait":
                            return
                        if done not in {"used", "agent"}:
                            speak(speech, "No he podido hacer ese comando.", speaker)
                    else:
                        speak(speech, answer, speaker)
                        if (
                            not admin
                            and phrase
                            and grok_needs_admin(answer)
                        ):
                            local["task"] = phrase
                            local["offer"] = "admin"
                            speak(
                                speech,
                                "Necesito pasar a ser administrador para poder hacer eso. ¿Quieres pasar a modo admin?",
                                speaker,
                            )
                        elif not admin and phrase and wants_more_detail(phrase, answer):
                            local["detail"] = phrase
                            speak(speech, "¿Quieres que te lo cuente con más detalle?", speaker)
                    time.sleep(0.3)
                    phrase = ""
            phrase = wait_for_phrase(speech, mic, floor, cfg, display, idle, owner)
            if not display.listening or STOP:
                return
            if phrase is None:
                log.info("conversación cerrada: %ss sin frases", int(idle))
                speak(speech, "Hasta luego.", speaker)
                return
    finally:
        if speaker_index >= 0:
            speakers().mark(speaker_index)
        display.set_conversation(False)


def _owner_line(room: VoiceRoom, owner: np.ndarray) -> str:
    parts = []
    for lane in room.lanes:
        if lane.text and lane.embedding is not None and (
            (speakers().locked and speakers().accepts(lane.embedding))
            or (not speakers().locked and same_voice(lane.embedding, owner))
        ):
            parts.append(f"{lane.label}: {lane.text}")
    return " | ".join(parts)


def _wait_owner(
    speech: Speech,
    mic: Mic,
    floor: NoiseFloor,
    cfg: dict,
    display: HeardDisplay,
    timeout: float,
    owner: np.ndarray,
) -> str | None:
    """While a talk is open, only the voice that opened it is heard."""
    owner = np.array(owner, dtype=np.float32, copy=True)
    room = VoiceRoom(speech, cfg)
    deadline = time.monotonic() + timeout
    sticky = ""
    while not STOP and time.monotonic() < deadline:
        if not display.listening:
            return None
        block = mic.read_frames()
        if block is None:
            return None
        level = rms(block)
        floor.observe(level)
        loud = level >= floor.threshold(float(cfg["speech_rms_min"]))
        for event in room.feed(block, loud):
            if event[0] == "partial":
                mine = _owner_line(room, owner)
                if mine:
                    sticky = mine
                    display.update(mine, level)
                elif room.active is not None and (
                    room.active.embedding is None or same_voice(room.active.embedding, owner)
                ) and loud:
                    display.update(sticky or "escuchando…", level)
                else:
                    display.update(sticky or "(silencio)", level)
                continue
            _kind, label, text, audio, embedding, _saved = event
            heard_order = command_body(text) or text
            if is_voice_enroll(heard_order):
                mine = enroll_open(embedding, audio)
            elif speakers().locked:
                mine = embedding is not None and speakers().accepts(embedding)
            else:
                mine = same_voice(embedding, owner)
            if not mine:
                score = 0.0
                if embedding is not None and owner is not None and embedding.shape == owner.shape:
                    score = speakers()._similar(embedding, owner)
                log.info("no es para mí (%s) %.2f: %s", label, score, text)
                continue
            if command_body(text) is None and (is_own_words(text) or speakers().is_self(audio)):
                log.info("era mi voz, sigo esperando: %s", text)
                continue
            if (
                not speakers().locked
                and embedding is not None
                and owner.shape == embedding.shape
            ):
                owner[:] = (0.85 * owner + 0.15 * embedding).astype(np.float32)
            if not display.in_test:
                text = second_reading(speech, text, audio)
            line = f"{label}: {text}"
            sticky = line
            display.update(line, level)
            log.info("para grok (%s): %s", label, text)
            return text
    return None


def wait_for_phrase(
    speech: Speech,
    mic: Mic,
    floor: NoiseFloor,
    cfg: dict,
    display: HeardDisplay,
    timeout: float,
    owner: np.ndarray | None = None,
) -> str | None:
    """Listen until a real phrase arrives, or until timeout with none."""
    if speakers().extractor is not None and (owner is not None or speakers().locked):
        if owner is None:
            owner = np.zeros(1, dtype=np.float32)
        return _wait_owner(speech, mic, floor, cfg, display, timeout, owner)
    stream = speech.new_stream()
    deadline = time.monotonic() + timeout
    chunk_s = CHUNK_FRAMES / RATE
    last = ""
    silence = 0.0
    speech_time = 0.0
    utterance_started: float | None = None
    heard_audio: list[np.ndarray] = []
    while not STOP and time.monotonic() < deadline:
        if not display.listening:
            return None
        block = mic.read_frames()
        if block is None:
            return None
        level = rms(block)
        loud = level >= floor.threshold(float(cfg["speech_rms_min"]))
        if speech.keep_audio(stream, loud, silence):
            text = (speech.feed_spanish(stream, block) or "").strip()
        else:
            text = (speech.partial_spanish(stream) or "").strip()
        display.update(text or "(silencio)", level)
        if text and is_own_words(text):
            log.info("eco en la conversación, lo suelto: %s", text)
            speech.reset_stream(stream)
            stream = speech.new_stream()
            last = ""
            silence = 0.0
            speech_time = 0.0
            heard_audio.clear()
            utterance_started = None
            continue
        if text and text != last:
            last = text
            silence = 0.0
        elif text:
            last = text
        if loud or (heard_audio and silence < 0.6):
            heard_audio.append(np.array(block, copy=True))
        if loud:
            if utterance_started is None:
                utterance_started = time.monotonic()
            speech_time += chunk_s
            silence = 0.0
        else:
            silence += chunk_s
        # The open conversation gets this string unchanged.
        heard = last.strip()
        if not loud and silence >= float(cfg["silence_seconds"]) and (heard or speech.buffered(stream)):
            heard = (speech.finish_spanish(stream) or heard).strip()
            if heard:
                log.info("frase completa, %d palabras: %s", len(heard.split()), heard)
                speech.reset_stream(stream)
                samples = np.concatenate(heard_audio) if heard_audio else None
                if is_own_words(heard) or speakers().is_self(samples):
                    log.info("era mi voz, sigo esperando: %s", heard)
                    stream = speech.new_stream()
                    last = ""
                    silence = 0.0
                    speech_time = 0.0
                    heard_audio.clear()
                    utterance_started = None
                    continue
                if not display.in_test:
                    heard = second_reading(speech, heard, samples)
                return heard
        if utterance_started is not None and time.monotonic() - utterance_started >= float(cfg["max_utterance_seconds"]) and not loud:
            log.info("corte por tiempo máximo, %d palabras: %s", len(heard.split()), heard)
            speech.reset_stream(stream)
            return heard or None
        if silence >= 3.0 and not heard:
            speech.reset_stream(stream)
            stream = speech.new_stream()
            last = ""
            silence = 0.0
            speech_time = 0.0
    return None


def finish_spanish_request(
    speech: Speech,
    mic: Mic,
    stream,
    floor: NoiseFloor,
    cfg: dict,
    display: HeardDisplay,
    already_started: bool = False,
    audio: list[np.ndarray] | None = None,
) -> str:
    silence = 0.0
    previous = ""
    speech_time = float(cfg["min_speech_seconds"]) if already_started else 0.0
    chunk_s = CHUNK_FRAMES / RATE
    started = time.monotonic()
    while not STOP:
        if not display.listening:
            break
        block = mic.read_frames()
        if block is None:
            break
        if audio is not None:
            audio.append(np.array(block, copy=True))
        level = rms(block)
        loud_now = level >= floor.threshold(float(cfg["speech_rms_min"]))
        if speech.keep_audio(stream, loud_now, silence):
            speech.feed_spanish(stream, block)
        heard_now = (speech.partial_spanish(stream) or "").strip()
        display.update(heard_now, level)
        if heard_now and heard_now != previous:
            silence = 0.0
        previous = heard_now
        if level >= floor.threshold(float(cfg["speech_rms_min"])):
            speech_time += chunk_s
            silence = 0.0
        else:
            silence += chunk_s
        elapsed = time.monotonic() - started
        if speech_time >= float(cfg["min_speech_seconds"]) and silence >= float(cfg["silence_seconds"]):
            break
        if speech_time == 0 and elapsed >= float(cfg["no_speech_timeout_seconds"]):
            break
        if elapsed >= float(cfg["max_utterance_seconds"]) and silence >= 0.4:
            log.info("corte por tiempo máximo en la frase inicial")
            break
    text = speech.finish_spanish(stream)
    log.info("frase inicial, %d palabras: %s", len(text.split()), text)
    speech.reset_stream(stream)
    return text


def run_loop(speech: Speech, cfg: dict) -> int:
    beep = make_beep()
    display = HeardDisplay(current_grok_model(cfg))
    display.bind(speech)
    display.set_asr_name(speech.asr_label)
    cleanup_orphan_sessions()
    if speech.language == "es":
        start_account_agent_fetch()
    announced_missing = False
    announced_ready = False
    try:
        while not STOP:
            device = resolve_capture(cfg)
            speaker = resolve_playback(cfg)
            if not device:
                if not announced_missing:
                    log.warning("No capture device. The operating system is not exposing a microphone.")
                    missing = (
                        "No hay micrófono. El sistema no tiene una entrada de sonido."
                        if speech.language == "es"
                        else "There is no microphone. The operating system has no capture device."
                    )
                    speak(
                        speech,
                        missing,
                        speaker,
                    )
                    announced_missing = True
                time.sleep(5)
                continue
            if not announced_ready:
                apply_saved_volume()
                log.info("listening on %s", device)
                ready = boot_hello(speech.language)
                speak(
                    speech,
                    ready,
                    speaker,
                )
                announced_ready = True
                time.sleep(0.6)
            mic = Mic(device)
            floor = NoiseFloor()
            if speech.language == "es":
                try:
                    listen_spanish(speech, mic, floor, speaker, beep, cfg, display)
                finally:
                    mic.close()
                continue
            stream = speech.kws.create_stream()
            recent: deque[np.ndarray] = deque(maxlen=15)
            try:
                while not STOP:
                    block = mic.read_frames()
                    if block is None:
                        log.warning("microphone read failed, reopening")
                        break
                    level = rms(block)
                    floor.observe(level)
                    recent.append(block.copy())
                    keyword = speech.hear_keyword(stream, block)
                    if not keyword:
                        continue
                    log.info("wake word: %s", keyword)
                    preamble = np.concatenate(list(recent)) if recent else None
                    play_on(beep, speaker)
                    command = record_command(mic, floor, cfg, preamble)
                    if command is None:
                        log.info("no speech after the wake word")
                        continue
                    heard = drop_wake_phrase(speech.transcribe(command))
                    log.info("heard: %s", heard or "(empty)")
                    if len(heard) < 2:
                        speak(speech, "I didn't catch that.", speaker)
                        time.sleep(0.4)
                        stream = speech.kws.create_stream()
                        continue
                    try:
                        answer = ask_grok(heard, cfg)
                    except TimeoutError:
                        answer = "Grok took too long. Please try again."
                    except RuntimeError as exc:
                        log.error("grok failed: %s", exc)
                        answer = "I could not reach Grok."
                    speak(speech, answer, speaker)
                    time.sleep(0.4)
                    stream = speech.kws.create_stream()
            finally:
                mic.close()
    finally:
        beep.unlink(missing_ok=True)
    return 0


def load_speech(cfg: dict) -> Speech:
    missing = [
        str(path)
        for path in (KWS_DIR, ASR_DIR, TTS_DIR)
        if not path.is_dir()
    ]
    if missing:
        raise SystemExit(
            "Speech models are missing:\n  "
            + "\n  ".join(missing)
            + f"\nRun {ROOT / 'setup.sh'}"
        )
    log.info("loading speech models")
    speech = Speech(cfg)
    log.info("models ready")
    return speech


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Grok voice assistant")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="listen for the wake word (default)")
    say = sub.add_parser("say", help="speak a sentence on the configured output")
    say.add_argument("text")
    ask = sub.add_parser("ask", help="send one sentence to Grok and speak the reply")
    ask.add_argument("text")
    sub.add_parser("devices", help="show playback and microphone devices")
    test = sub.add_parser("self-test", help="check wake word, transcription, and Grok")
    test.add_argument("--no-grok", action="store_true", help="skip the live Grok call")
    args = parser.parse_args(argv)
    command = args.command or "run"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    cfg = load_config()

    if command == "devices":
        return show_devices(cfg)

    speech = load_speech(cfg)
    if command == "say":
        return cmd_say(speech, cfg, args.text)
    if command == "ask":
        return cmd_ask(speech, cfg, args.text)
    if command == "self-test":
        return self_test(speech, cfg, with_grok=not args.no_grok)
    return run_loop(speech, cfg)



import pi_extra

_skip_agent_stem = pi_extra._skip_agent_stem
agent_filename = pi_extra.agent_filename
agent_sources = pi_extra.agent_sources
resolve_agent = pi_extra.resolve_agent
agent_markdown = pi_extra.agent_markdown
list_agent_names = pi_extra.list_agent_names
agent_list_line = pi_extra.agent_list_line
remember_agent_id = pi_extra.remember_agent_id
_auth_session = pi_extra._auth_session
_get_json = pi_extra._get_json
_bundle_agents = pi_extra._bundle_agents
_customization_markdown = pi_extra._customization_markdown
_custom_agents = pi_extra._custom_agents
_replace_account_agents = pi_extra._replace_account_agents
refresh_account_agents = pi_extra.refresh_account_agents
start_account_agent_fetch = pi_extra.start_account_agent_fetch
MusicPlayer = pi_extra.MusicPlayer
music = pi_extra.music
_discard_queued = pi_extra._discard_queued
_pause_and_clear = pi_extra._pause_and_clear
_tone_path = pi_extra._tone_path
_play_tone = pi_extra._play_tone
_enroll_take = pi_extra._enroll_take
_enroll_listen = pi_extra._enroll_listen
enroll_result = pi_extra.enroll_result
log_enroll_take = pi_extra.log_enroll_take
is_presence_phrase = pi_extra.is_presence_phrase
_one_token = pi_extra._one_token
_real_phrase = pi_extra._real_phrase
second_reading = pi_extra.second_reading
_phrase_order = pi_extra._phrase_order
_VoiceLane = pi_extra._VoiceLane
VoiceRoom = pi_extra.VoiceRoom

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
