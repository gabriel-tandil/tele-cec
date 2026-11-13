"""Joysticks (gamepads): cada botón hace lo mismo que una tecla del control remoto.

Lee los eventos del kernel (/dev/input/eventN) sin bibliotecas aparte: alcanza con que el usuario del servicio
esté en el grupo «input» (en Raspberry Pi OS lo está) o tenga acceso por la sesión gráfica. Los joysticks se
detectan solos al conectarlos (USB, el receptor inalámbrico de Xbox 360 o Bluetooth) y se sueltan al
desconectarlos.

Las teclas entran a la misma cola que las del control (con su «soltar»), así que valen en todos lados: menú,
apps, ayuda, y también despiertan a la Pi del reposo. Mantener una dirección la repite, como el control.

Nombres de los botones en [gamepad.buttons] (los de un joystick de Xbox; en otros, el que está en ese lugar):
  A B X Y · LB RB · LT RT (gatillos) · BACK START GUIDE (el botón del medio) · LS RS (apretar las palancas)
  DPAD_UP/DOWN/LEFT/RIGHT (la cruz y la palanca izquierda) · RS_UP/DOWN/LEFT/RIGHT (la palanca derecha)

Los de [gamepad] hold_buttons avisan además cuando se los mantiene apretados ("BACK:HOLD"): en los juegos,
mantener Back sale del juego (un toque corto es la moneda de MAME).
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import struct
from dataclasses import dataclass, field
from typing import Any, Callable

from ..keys import parse_key

log = logging.getLogger("joystick")

DEVICES_FILE = "/proc/bus/input/devices"
SCAN_EVERY = 3.0

EV_KEY, EV_ABS = 0x01, 0x03
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_GAS, ABS_BRAKE, ABS_HAT0X, ABS_HAT0Y = 0x09, 0x0A, 0x10, 0x11
BTN_JOYSTICK, BTN_GAMEPAD = 0x120, 0x130

# Código del kernel → nombre del botón (los de un gamepad estándar: xpad, hid-generic, Bluetooth).
BUTTONS = {
    0x130: "A", 0x131: "B", 0x133: "X", 0x134: "Y",
    0x136: "LB", 0x137: "RB", 0x138: "LT", 0x139: "RT",
    0x13A: "BACK", 0x13B: "START", 0x13C: "GUIDE", 0x13D: "LS", 0x13E: "RS",
    # La cruz como botones (receptor inalámbrico de Xbox 360 con dpad_to_buttons, mandos de 8BitDo…).
    0x220: "DPAD_UP", 0x221: "DPAD_DOWN", 0x222: "DPAD_LEFT", 0x223: "DPAD_RIGHT",
    0x2C2: "DPAD_UP", 0x2C3: "DPAD_DOWN", 0x2C0: "DPAD_LEFT", 0x2C1: "DPAD_RIGHT",
}

DEFAULT_BUTTONS = {
    "A": "SELECT", "B": "EXIT", "X": "PAUSE_PLAY_FUNCTION", "Y": "F4_YELLOW",
    "LB": "BACKWARD", "RB": "FORWARD", "LT": "REWIND", "RT": "FAST_FORWARD",
    "BACK": "F1_BLUE", "START": "CONTENTS_MENU", "GUIDE": "CONTENTS_MENU",
    "LS": "F2_RED", "RS": "F3_GREEN",
    "DPAD_UP": "UP", "DPAD_DOWN": "DOWN", "DPAD_LEFT": "LEFT", "DPAD_RIGHT": "RIGHT",
    "RS_UP": "VOLUME_UP", "RS_DOWN": "VOLUME_DOWN", "RS_LEFT": "", "RS_RIGHT": "",
}
# Cómo se llaman en la ayuda, en este orden.
BUTTON_LABELS = [("A", "A"), ("B", "B"), ("X", "X"), ("Y", "Y"), ("LB", "LB"), ("RB", "RB"),
                 ("LT", "LT (gatillo)"), ("RT", "RT (gatillo)"), ("START", "Start"), ("GUIDE", "Botón del medio"),
                 ("BACK", "Back"), ("LS", "Palanca izq. (apretar)"), ("RS", "Palanca der. (apretar)")]
REPEATING = {"DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "RS_UP", "RS_DOWN", "RS_LEFT", "RS_RIGHT"}

_LONG = struct.calcsize("l")
EVENT = struct.Struct("llHHi")  # struct input_event: timeval (2 long), type, code, value


def _eviocgabs(axis: int) -> int:
    return (2 << 30) | (24 << 16) | (ord("E") << 8) | (0x40 + axis)


def _bits(words: str) -> int:
    """Una máscara de /proc/bus/input/devices («B: KEY=…», palabras de un long, la más alta primero)."""
    try:
        return int("".join(w.zfill(_LONG * 2) for w in words.split()) or "0", 16)
    except ValueError:
        return 0


def find_joysticks(text: str | None = None) -> dict[str, str]:
    """{"/dev/input/eventN": nombre} de los joysticks conectados."""
    if text is None:
        try:
            with open(DEVICES_FILE, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return {}
    found: dict[str, str] = {}
    for block in text.split("\n\n"):
        name, handlers, keys = "", [], 0
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line[8:].strip().strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line[12:].split()
            elif line.startswith("B: KEY="):
                keys = _bits(line[7:])
        event = next((h for h in handlers if h.startswith("event")), "")
        is_pad = any(h.startswith("js") for h in handlers) or bool(keys >> BTN_GAMEPAD & 1) or bool(keys >> BTN_JOYSTICK & 1)
        if event and is_pad:
            found[f"/dev/input/{event}"] = name or event
    return found


@dataclass
class Axis:
    lo: float = -32768
    hi: float = 32767

    def norm(self, v: int) -> float:
        """-1..1 (palancas) a partir del rango que informa el kernel."""
        half = (self.hi - self.lo) / 2 or 1
        return max(-1.0, min(1.0, (v - (self.lo + half)) / half))

    def pull(self, v: int) -> float:
        """0..1 (gatillos)."""
        return max(0.0, min(1.0, (v - self.lo) / ((self.hi - self.lo) or 1)))


@dataclass
class Pad:
    path: str
    name: str
    fd: int
    axes: dict[int, Axis] = field(default_factory=dict)
    values: dict[int, int] = field(default_factory=dict)
    held: dict[str, bool] = field(default_factory=dict)   # dirección/gatillo → apretado
    triggers_on_z: bool = True    # ABS_Z/ABS_RZ son gatillos (Xbox) o la palanca derecha (otros gamepads)
    buf: bytes = b""


def button_keys(cfg: dict[str, Any] | None) -> dict[str, str]:
    """Botón → nombre de la tecla del control (los de [gamepad.buttons] sobre los de fábrica; "" = nada)."""
    cfg = cfg or {}
    return {**DEFAULT_BUTTONS, **{str(k).upper(): str(v or "") for k, v in (cfg.get("buttons") or {}).items()}}


class Gamepads:
    def __init__(self, cfg: dict[str, Any] | None, on_key: Callable[[int, str], None], on_release: Callable[[], None]):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        names = button_keys(cfg)
        self.codes: dict[str, int] = {}
        for btn, key in names.items():
            if not key:
                continue
            code = parse_key(key)
            if code is None:
                log.warning("[gamepad.buttons] %s = %r: tecla desconocida", btn, key)
            else:
                self.codes[btn] = code
        self.deadzone = float(cfg.get("deadzone", 0.55))
        self.repeat_delay = float(cfg.get("repeat_delay_ms", 400)) / 1000
        self.repeat_every = float(cfg.get("repeat_ms", 130)) / 1000
        # Botones que además avisan si se los mantiene apretados (p. ej. Back: salir del juego).
        self.hold_buttons = {str(b).upper() for b in cfg.get("hold_buttons", ["BACK"]) or []}
        self.hold_time = float(cfg.get("hold_ms", 1000)) / 1000
        self._holds: dict[str, asyncio.Task] = {}
        self.on_key = on_key
        self.on_release = on_release
        self.pads: dict[str, Pad] = {}
        self._repeat: asyncio.Task | None = None
        self._repeating = ""
        self._denied: set[str] = set()

    # ------------------------------------------------------------ conexión
    async def run(self) -> None:
        if not self.enabled:
            return
        loop = asyncio.get_running_loop()
        while True:
            found = await asyncio.to_thread(find_joysticks)
            for path, name in found.items():
                if path not in self.pads:
                    self._open(loop, path, name)
            for path in [p for p in self.pads if p not in found]:
                self._close(loop, path, "se desconectó")
            self._denied &= set(found)
            await asyncio.sleep(SCAN_EVERY)

    def _open(self, loop: asyncio.AbstractEventLoop, path: str, name: str) -> None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            if path not in self._denied:
                self._denied.add(path)
                log.warning("Joystick %s (%s): no lo puedo leer: %s%s", name, path, exc.strerror,
                            " (agregá tu usuario al grupo input: sudo adduser $USER input)"
                            if exc.errno == errno.EACCES else "")
            return
        pad = Pad(path, name, fd)
        for axis in (ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_GAS, ABS_BRAKE, ABS_HAT0X, ABS_HAT0Y):
            try:
                info = struct.unpack("6i", fcntl.ioctl(fd, _eviocgabs(axis), bytes(24)))
            except OSError:
                continue
            pad.axes[axis] = Axis(info[1], info[2])
            pad.values[axis] = info[0]
        # Con ABS_RX (palanca derecha aparte), Z y RZ son los gatillos (Xbox); si no, son la palanca derecha.
        pad.triggers_on_z = ABS_RX in pad.axes
        self.pads[path] = pad
        loop.add_reader(fd, self._readable, path)
        log.info("Joystick conectado: %s (%s)", name, path)

    def _close(self, loop: asyncio.AbstractEventLoop, path: str, why: str) -> None:
        pad = self.pads.pop(path, None)
        if pad is None:
            return
        try:
            loop.remove_reader(pad.fd)
        except (ValueError, OSError):
            pass
        try:
            os.close(pad.fd)
        except OSError:
            pass
        if any(pad.held.values()):
            self._stop_repeat()
            self.on_release()
        log.info("Joystick %s: %s", pad.name, why)

    def _readable(self, path: str) -> None:
        pad = self.pads.get(path)
        if pad is None:
            return
        try:
            data = os.read(pad.fd, EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError as exc:
            self._close(asyncio.get_running_loop(), path, f"se desconectó ({exc.strerror})")
            return
        if not data:
            return
        pad.buf += data
        n = len(pad.buf) // EVENT.size * EVENT.size
        chunk, pad.buf = pad.buf[:n], pad.buf[n:]
        for _s, _us, etype, code, value in EVENT.iter_unpack(chunk):
            self.handle(pad, etype, code, value)

    # ------------------------------------------------------------ eventos
    def handle(self, pad: Pad, etype: int, code: int, value: int) -> None:
        if etype == EV_KEY:
            btn = BUTTONS.get(code)
            if btn and value in (0, 1):
                self._set(pad, btn, value == 1)
        elif etype == EV_ABS and code in pad.axes:
            pad.values[code] = value
            self._axes(pad)

    def _axes(self, pad: Pad) -> None:
        dz = self.deadzone

        def stick(ax: int, ay: int) -> tuple[float, float]:
            x = pad.axes[ax].norm(pad.values.get(ax, 0)) if ax in pad.axes else 0.0
            y = pad.axes[ay].norm(pad.values.get(ay, 0)) if ay in pad.axes else 0.0
            return x, y

        def direction(x: float, y: float, prefix: str) -> None:
            want = ""
            if max(abs(x), abs(y)) >= dz:
                want = (("RIGHT" if x > 0 else "LEFT") if abs(x) > abs(y) else ("DOWN" if y > 0 else "UP"))
            for d in ("UP", "DOWN", "LEFT", "RIGHT"):
                held = pad.held.get(f"{prefix}{d}@axis", False)
                # Un poco de histéresis: se suelta recién bastante cerca del centro.
                if held and d != want and max(abs(x), abs(y)) < dz * 0.6:
                    self._set(pad, f"{prefix}{d}", False, src="axis")
                elif held and want and d != want:
                    self._set(pad, f"{prefix}{d}", False, src="axis")
            if want and not pad.held.get(f"{prefix}{want}@axis"):
                self._set(pad, f"{prefix}{want}", True, src="axis")

        hx = pad.values.get(ABS_HAT0X, 0) if ABS_HAT0X in pad.axes else 0
        hy = pad.values.get(ABS_HAT0Y, 0) if ABS_HAT0Y in pad.axes else 0
        for d, on in (("LEFT", hx < 0), ("RIGHT", hx > 0), ("UP", hy < 0), ("DOWN", hy > 0)):
            if pad.held.get(f"DPAD_{d}@hat", False) != on:
                self._set(pad, f"DPAD_{d}", on, src="hat")
        direction(*stick(ABS_X, ABS_Y), "DPAD_")
        if pad.triggers_on_z:
            direction(*stick(ABS_RX, ABS_RY), "RS_")
            triggers = ((ABS_Z, "LT"), (ABS_RZ, "RT"), (ABS_BRAKE, "LT"), (ABS_GAS, "RT"))
        else:
            direction(*stick(ABS_Z, ABS_RZ), "RS_")
            triggers = ((ABS_BRAKE, "LT"), (ABS_GAS, "RT"))
        for ax, btn in triggers:
            if ax in pad.axes:
                pull = pad.axes[ax].pull(pad.values.get(ax, 0))
                held = pad.held.get(f"{btn}@axis", False)
                if not held and pull >= 0.6:
                    self._set(pad, btn, True, src="axis")
                elif held and pull < 0.3:
                    self._set(pad, btn, False, src="axis")

    def _set(self, pad: Pad, btn: str, down: bool, src: str = "btn") -> None:
        key = f"{btn}@{src}"
        if pad.held.get(key, False) == down:
            return
        pad.held[key] = down
        if btn in self.hold_buttons and src == "btn":
            self._hold(pad, btn, down)
        code = self.codes.get(btn)
        if code is None:
            if down and btn in ("BACK", "START") and any(pad.held.get(f"{o}@btn") for o in ("BACK", "START") if o != btn):
                self.on_key(self.codes.get("START") or self.codes.get("GUIDE") or 0, "BACK+START")
            return
        if down:
            self._stop_repeat()
            self.on_key(code, btn)
            if btn in ("BACK", "START") and any(pad.held.get(f"{o}@btn") for o in ("BACK", "START") if o != btn):
                self.on_key(code, "BACK+START")  # los dos juntos: el menú en los juegos (ver on_gamepad_key)
            if btn in REPEATING:
                self._repeating = key
                self._repeat = asyncio.get_running_loop().create_task(self._repeat_key(code))
        else:
            if self._repeating == key:
                self._stop_repeat()
            self.on_release()

    def _hold(self, pad: Pad, btn: str, down: bool) -> None:
        """Mantenerlo apretado hold_ms avisa «<BOTÓN>:HOLD» (una vez por pulsación)."""
        ident = f"{pad.path}:{btn}"
        task = self._holds.pop(ident, None)
        if task and not task.done():
            task.cancel()
        if down:
            async def fire() -> None:
                await asyncio.sleep(self.hold_time)
                self.on_key(self.codes.get(btn) or 0, f"{btn}:HOLD")
            self._holds[ident] = asyncio.get_running_loop().create_task(fire())

    def _stop_repeat(self) -> None:
        if self._repeat and not self._repeat.done():
            self._repeat.cancel()
        self._repeat, self._repeating = None, ""

    async def _repeat_key(self, code: int) -> None:
        await asyncio.sleep(self.repeat_delay)
        while True:
            self.on_key(code, self._repeating.partition("@")[0])  # sin «soltar» en el medio: igual que la auto-repetición del control
            await asyncio.sleep(self.repeat_every)

    def connected(self) -> list[str]:
        return [p.name for p in self.pads.values()]
