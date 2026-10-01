#!/usr/bin/env python3
"""Boot screen that starts the voice assistant only when the internet works.

If the Pi already has a connection, this program starts grok-assistant and
exits. If not, it stays on the small touchscreen: pick a network, type the
password, and it tries again. It does not load the assistant in this process.
"""

from __future__ import annotations

import gzip
import mmap
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import threading
import time

FONT = "/usr/share/consolefonts/Lat15-TerminusBold16.psf.gz"
BG = (8, 16, 32)
FG = (236, 242, 248)
MUTED = (150, 176, 196)
ACCENT = (120, 210, 255)
OK = (120, 220, 150)
WARN = (255, 196, 80)
KEY = (24, 42, 64)
KEY_ON = (48, 86, 120)
SUDO = Path.home() / ".config" / "grok-assistant" / "bin" / "sudo"
ROWS = (
    "1234567890",
    "qwertyuiop",
    "asdfghjklñ",
    "zxcvbnm.-_",
)


def load_psf(path: str) -> tuple[int, int, dict[int, bytes]]:
    raw = gzip.open(path, "rb").read() if path.endswith(".gz") else open(path, "rb").read()
    glyphs: dict[int, bytes] = {}
    if raw[:2] == b"\x36\x04":
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
        return width, height, glyphs
    raise ValueError(f"unsupported font {path}")


def panel_fb() -> str:
    """The framebuffer the operating system assigned. fb0 is that console."""
    if Path("/sys/class/graphics/fb0").is_dir():
        return "fb0"
    found = sorted(Path("/sys/class/graphics").glob("fb[0-9]*"))
    return found[0].name if found else "fb0"


def _axis_limits(fd: int, code: int) -> tuple[int, int]:
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


def split_nmcli(line: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    escape = False
    for char in line:
        if escape:
            buf.append(char)
            escape = False
        elif char == "\\":
            escape = True
        elif char == ":":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    parts.append("".join(buf))
    return parts


def internet_up() -> bool:
    try:
        result = subprocess.run(
            [
                "curl",
                "-fsS",
                "-o",
                "/dev/null",
                "--max-time",
                "4",
                "-w",
                "%{http_code}",
                "http://connectivitycheck.gstatic.com/generate_204",
            ],
            capture_output=True,
            text=True,
            timeout=6,
        )
        if (result.stdout or "").strip() == "204":
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", "1.1.1.1"],
            capture_output=True,
            timeout=4,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def scan_networks() -> list[dict]:
    if SUDO.is_file():
        subprocess.run([str(SUDO), "nmcli", "dev", "wifi", "rescan"], capture_output=True, timeout=20)
    else:
        subprocess.run(["nmcli", "dev", "wifi", "rescan"], capture_output=True, timeout=20)
    result = subprocess.run(
        ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "no"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    found: dict[str, dict] = {}
    for line in (result.stdout or "").splitlines():
        parts = split_nmcli(line)
        if len(parts) < 3:
            continue
        ssid, signal, security = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not ssid:
            continue
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        locked = security not in {"", "--"}
        previous = found.get(ssid)
        if previous is None or strength > previous["signal"]:
            found[ssid] = {"ssid": ssid, "signal": strength, "locked": locked, "security": security}
    return sorted(found.values(), key=lambda item: item["signal"], reverse=True)


def connect_wifi(ssid: str, password: str) -> tuple[bool, str]:
    command = [str(SUDO) if SUDO.is_file() else "nmcli", "nmcli", "dev", "wifi", "connect", ssid, "ifname", "wlan0"]
    if not SUDO.is_file():
        command = ["nmcli", "dev", "wifi", "connect", ssid, "ifname", "wlan0"]
    if password:
        command.extend(["password", password])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=50)
    except subprocess.TimeoutExpired:
        return False, "Tardó demasiado."
    text = ((result.stderr or "") + " " + (result.stdout or "")).strip().replace("\n", " ")
    if result.returncode == 0:
        return True, text
    lowered = text.lower()
    if "secrets were required" in lowered or "password" in lowered or "clave" in lowered:
        return False, "La clave no vale."
    return False, text[:120] or "No pude unirme a esa red."


def launch_assistant() -> None:
    print("internet ok, arranco grok-assistant", flush=True)
    command = [str(SUDO), "systemctl", "start", "grok-assistant"] if SUDO.is_file() else ["systemctl", "start", "grok-assistant"]
    subprocess.run(command, timeout=40)
    os._exit(0)


class Panel:
    def __init__(self) -> None:
        self.fb_name = panel_fb()
        size = open(f"/sys/class/graphics/{self.fb_name}/virtual_size", encoding="ascii").read().strip()
        self.width, self.height = (int(part) for part in size.split(","))
        self.stride = int(open(f"/sys/class/graphics/{self.fb_name}/stride", encoding="ascii").read().strip())
        self.font_w, self.font_h, self.glyphs = load_psf(FONT)
        fd = os.open(f"/dev/{self.fb_name}", os.O_RDWR)
        self.mem = mmap.mmap(fd, self.stride * self.height, mmap.MAP_SHARED, mmap.PROT_WRITE)
        self.rotation = 0
        self.touch_x = (0, 1)
        self.touch_y = (0, 1)
        self.buttons: list[tuple[int, int, int, int, str]] = []
        self._tap: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self._stop = False
        self._quiet_console()
        path = touch_device()
        if path:
            threading.Thread(target=self._touch_loop, args=(path,), daemon=True).start()

    def close(self) -> None:
        self._stop = True

    def _quiet_console(self) -> None:
        blink = "/sys/class/graphics/fbcon/cursor_blink"
        try:
            open(blink, "w", encoding="ascii").write("0\n")
        except OSError:
            if SUDO.is_file():
                subprocess.run([str(SUDO), "tee", blink], input="0\n", text=True, capture_output=True)

    def _pack(self, color: tuple[int, int, int]) -> bytes:
        red, green, blue = color
        value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
        return value.to_bytes(2, "little")

    def fill(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        pixel = self._pack(color)
        for row in range(max(0, y), min(self.height, y + h)):
            begin = row * self.stride + max(0, x) * 2
            end = row * self.stride + min(self.width, x + w) * 2
            if end > begin:
                self.mem[begin:end] = pixel * ((end - begin) // 2)

    def _pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = y * self.stride + x * 2
            self.mem[offset : offset + 2] = self._pack(color)

    def text(self, x: int, y: int, text: str, color: tuple[int, int, int], max_x: int | None = None) -> None:
        limit = self.width - 2 if max_x is None else max_x
        cursor = x
        for char in text:
            if cursor + self.font_w > limit:
                break
            glyph = self.glyphs.get(ord(char)) or self.glyphs.get(ord("?"), b"")
            row_bytes = (self.font_w + 7) // 8
            for row in range(self.font_h):
                bits = glyph[row * row_bytes : (row + 1) * row_bytes]
                for col in range(self.font_w):
                    byte = bits[col // 8] if col // 8 < len(bits) else 0
                    if byte & (0x80 >> (col % 8)):
                        self._pixel(cursor + col, y + row, color)
            cursor += self.font_w

    def clear(self) -> None:
        self.buttons = []
        self.fill(0, 0, self.width, self.height, BG)

    def button(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        label: str,
        key: str,
        color: tuple[int, int, int] = KEY,
        center: bool = True,
    ) -> None:
        self.fill(x, y, w, h, color)
        width = len(label) * self.font_w
        text_x = x + max(4, (w - width) // 2) if center else x + 6
        self.text(text_x, y + max(0, (h - self.font_h) // 2), label, FG, x + w - 4)
        self.buttons.append((x, y, x + w, y + h, key))

    def take_tap(self) -> str | None:
        with self._lock:
            tap = self._tap
            self._tap = None
        if tap is None:
            return None
        x, y = tap
        for x0, y0, x1, y1, key in self.buttons:
            if x0 <= x < x1 and y0 <= y < y1:
                return key
        return ""

    def _map(self, raw_x: int, raw_y: int) -> tuple[int, int]:
        def axis(raw: int, lo: int, hi: int, size: int) -> int:
            span = hi - lo
            if span == 0:
                return 0
            pos = min(1.0, max(0.0, (raw - lo) / span))
            return int(pos * (size - 1))

        x_lo, x_hi = self.touch_x
        y_lo, y_hi = self.touch_y
        return axis(raw_x, x_lo, x_hi, self.width), axis(raw_y, y_lo, y_hi, self.height)

    def _touch_loop(self, path: str) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            print(f"táctil no disponible: {exc}", flush=True)
            return
        event = struct.Struct("qqHHi")
        self.touch_x = _axis_limits(fd, 0)
        self.touch_y = _axis_limits(fd, 1)
        raw_x = raw_y = 0
        have = False
        pressed = False
        last = 0.0
        while not self._stop:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(fd, event.size * 32)
            except BlockingIOError:
                continue
            if not data:
                continue
            for _sec, _usec, kind, code, value in (event.unpack_from(data, offset) for offset in range(0, len(data) - event.size + 1, event.size)):
                if kind == 3 and code == 0:
                    raw_x = value
                    have = True
                elif kind == 3 and code == 1:
                    raw_y = value
                    have = True
                elif kind == 1 and code in (330, 272) and have and value == 1 and not pressed:
                    pressed = True
                    now = time.monotonic()
                    if now - last > 0.25:
                        last = now
                        point = self._map(raw_x, raw_y)
                        with self._lock:
                            self._tap = point
                elif kind == 1 and code in (330, 272) and value == 0:
                    pressed = False
                elif kind == 3 and code == 24 and have:
                    down = value > 20
                    if down and not pressed:
                        pressed = True
                        now = time.monotonic()
                        if now - last > 0.25:
                            last = now
                            point = self._map(raw_x, raw_y)
                            with self._lock:
                                self._tap = point
                    elif not down:
                        pressed = False


def paint_message(panel: Panel, title: str, detail: str) -> None:
    panel.clear()
    panel.text(8, 80, title, ACCENT)
    panel.text(8, 110, detail, FG)


def paint_list(panel: Panel, networks: list[dict], page: int, note: str) -> None:
    panel.clear()
    panel.text(8, 6, "Elige una wifi", ACCENT)
    panel.text(8, 26, note[:58], MUTED)
    per_page = 6
    start = page * per_page
    shown = networks[start : start + per_page]
    y = 48
    for index, item in enumerate(shown):
        lock = "clave" if item["locked"] else "abierta"
        label = f"{item['ssid'][:22]}  {item['signal']}%  {lock}"
        panel.button(6, y, panel.width - 12, 30, label, f"net:{start + index}", center=False)
        y += 34
    panel.button(6, 286, 150, 28, "Buscar", "scan", KEY_ON)
    if page > 0:
        panel.button(164, 286, 100, 28, "Antes", "prev")
    if start + per_page < len(networks):
        panel.button(272, 286, 200, 28, "Más", "next")


def paint_password(panel: Panel, ssid: str, secret: str, upper: bool, message: str) -> None:
    panel.clear()
    panel.text(8, 4, "Clave de la wifi", ACCENT)
    panel.text(8, 22, ssid[:40], FG)
    panel.fill(6, 42, panel.width - 12, 24, (16, 28, 48))
    shown = secret if len(secret) <= 48 else secret[-48:]
    panel.text(10, 46, shown or "(toca las letras)", WARN if secret else MUTED, panel.width - 10)
    if message:
        panel.text(8, 68, message[:56], WARN)
    key_y = 88
    key_h = 28
    gap = 3
    for row_index, row in enumerate(ROWS):
        count = len(row)
        width = (panel.width - 8 - gap * (count - 1)) // count
        for col, char in enumerate(row):
            label = char.upper() if upper and char.isalpha() else char
            x = 4 + col * (width + gap)
            panel.button(x, key_y, width, key_h, label, f"key:{label}")
        key_y += key_h + gap
    panel.button(4, key_y, 90, key_h, "MAYÚS" if not upper else "MAYÚS*", "shift", KEY_ON if upper else KEY)
    panel.button(98, key_y, 200, key_h, "espacio", "key: ")
    panel.button(302, key_y, 174, key_h, "borrar", "back")
    key_y += key_h + gap
    panel.button(4, key_y, 160, key_h, "Volver", "back-list")
    panel.button(170, key_y, panel.width - 174, key_h, "Conectar", "join", (20, 72, 48))


def main() -> int:
    force_menu = "--menu" in sys.argv
    force_keys = "--keys" in sys.argv
    panel = Panel()
    print(f"pantalla {panel.fb_name} {panel.width}x{panel.height}", flush=True)
    if not force_menu and not force_keys:
        paint_message(panel, "Comprobando internet…", "Si ya hay red, arranco solo.")
        deadline = time.monotonic() + 16
        while time.monotonic() < deadline:
            if internet_up():
                paint_message(panel, "Hay internet.", "Arranco el asistente.")
                time.sleep(0.6)
                launch_assistant()
            time.sleep(2)
    networks: list[dict] = []
    page = 0
    chosen: dict | None = None
    secret = ""
    upper = False
    note = "Buscando redes…"
    message = ""
    mode = "keys" if force_keys else "list"
    if mode == "list":
        paint_message(panel, "Buscando redes…", "")
        try:
            networks = scan_networks()
            note = f"{len(networks)} redes" if networks else "No veo ninguna red."
        except (OSError, subprocess.TimeoutExpired) as exc:
            note = "No pude buscar."
            print(f"scan failed: {exc}", flush=True)
        paint_list(panel, networks, page, note)
    else:
        chosen = {"ssid": "Makakos", "locked": True, "signal": 60, "security": "WPA2"}
        paint_password(panel, chosen["ssid"], secret, upper, message)
    next_check = time.monotonic() + 4
    while True:
        if not force_menu and not force_keys and time.monotonic() >= next_check and mode == "list":
            next_check = time.monotonic() + 4
            if internet_up():
                paint_message(panel, "Hay internet.", "Arranco el asistente.")
                time.sleep(0.6)
                launch_assistant()
        key = panel.take_tap()
        if not key:
            time.sleep(0.05)
            continue
        if mode == "list" and key == "scan":
            paint_message(panel, "Buscando redes…", "")
            try:
                networks = scan_networks()
                page = 0
                note = f"{len(networks)} redes" if networks else "No veo ninguna red."
            except (OSError, subprocess.TimeoutExpired):
                note = "No pude buscar."
            paint_list(panel, networks, page, note)
        elif mode == "list" and key == "prev" and page > 0:
            page -= 1
            paint_list(panel, networks, page, note)
        elif mode == "list" and key == "next":
            page += 1
            paint_list(panel, networks, page, note)
        elif mode == "list" and key.startswith("net:"):
            index = int(key.split(":", 1)[1])
            if index >= len(networks):
                continue
            chosen = networks[index]
            if not chosen["locked"]:
                paint_message(panel, "Conectando…", chosen["ssid"][:40])
                ok, detail = connect_wifi(chosen["ssid"], "")
                if ok and _wait_online(panel, chosen["ssid"]):
                    launch_assistant()
                note = detail or "Unida, pero sin internet."
                mode = "list"
                paint_list(panel, networks, page, note[:58])
            else:
                secret = ""
                upper = False
                message = ""
                mode = "keys"
                paint_password(panel, chosen["ssid"], secret, upper, message)
        elif mode == "keys" and key == "shift":
            upper = not upper
            paint_password(panel, chosen["ssid"], secret, upper, message)
        elif mode == "keys" and key == "back":
            secret = secret[:-1]
            message = ""
            paint_password(panel, chosen["ssid"], secret, upper, message)
        elif mode == "keys" and key.startswith("key:"):
            secret += key.split(":", 1)[1]
            if len(secret) > 64:
                secret = secret[:64]
            message = ""
            paint_password(panel, chosen["ssid"], secret, upper, message)
        elif mode == "keys" and key == "back-list":
            mode = "list"
            paint_list(panel, networks, page, note)
        elif mode == "keys" and key == "join" and chosen is not None:
            if chosen["locked"] and not secret:
                message = "Escribe la clave."
                paint_password(panel, chosen["ssid"], secret, upper, message)
                continue
            if force_keys:
                message = "Modo de prueba. No conecto."
                paint_password(panel, chosen["ssid"], secret, upper, message)
                continue
            paint_message(panel, "Conectando…", chosen["ssid"][:40])
            ok, detail = connect_wifi(chosen["ssid"], secret)
            if ok and _wait_online(panel, chosen["ssid"]):
                launch_assistant()
            message = "La clave no vale." if "clave" in detail.lower() else "Sin internet con esa red."
            mode = "keys"
            paint_password(panel, chosen["ssid"], secret, upper, message)


def _wait_online(panel: Panel, ssid: str) -> bool:
    paint_message(panel, "Comprobando internet…", ssid[:40])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if internet_up():
            paint_message(panel, "Hay internet.", "Arranco el asistente.")
            time.sleep(0.6)
            return True
        time.sleep(2)
    return False


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
