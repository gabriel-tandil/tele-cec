"""Android: la pantalla de un celular o tablet en la TV, con scrcpy (por USB o por Wi‑Fi, vía adb).

scrcpy muestra la pantalla del celular en una ventana a pantalla completa y, desde Android 11, también manda el
sonido. Las flechas, OK y los números se le escriben a esa ventana (wtype; scrcpy 3+ usa SDL3, que en labwc es
una app Wayland) y scrcpy se los pasa al celular como un teclado. Atrás, Inicio, Recientes y las teclas
multimedia van directo al celular con `adb shell input keyevent`, por un shell de adb que queda abierto (así no
se arranca adb por cada tecla).

Por Wi‑Fi (Android 11+, «Depuración inalámbrica»): el celular se vincula una vez desde el menú, con un código QR
que muestra el panel o con el código de 6 dígitos que muestra el celular. El adb de Debian no trae descubrimiento
mDNS, así que los servicios que anuncia el celular (_adb-tls-pairing y _adb-tls-connect) se buscan con
avahi-browse, y tele-cec hace `adb pair` y `adb connect`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..system import keyboard
from ..system.procs import LOG_DIR, find_pids, graphical_env
from .base import ProcActivity

log = logging.getLogger("actividades")

# Teclas de Android (KeyEvent.KEYCODE_*) que van por adb.
KEYCODES = {
    "home": 3, "back": 4, "apps": 187, "menu": 82, "notifications": 83, "wakeup": 224, "power": 26,
    "play_pause": 85, "play": 126, "pause": 127, "stop": 86, "next": 87, "previous": 88,
    "rewind": 89, "fast_forward": 90, "volume_up": 24, "volume_down": 25, "mute": 164,
}

# Lo que contesta `adb devices` y qué significa para el menú.
STATE_TEXT = {
    "unauthorized": "Aceptá «Permitir depuración USB» en el celular",
    "offline": "El celular no responde: desenchufalo y volvé a enchufarlo",
    "no permissions": "Sin permiso para usar el USB (ver README → Android)",
    "authorizing": "Autorizando… mirá el celular",
    "connecting": "Conectando…",
}
NO_DEVICE = "Sin celular · OK para conectarlo (USB o Wi‑Fi)"


PAIRING = "_adb-tls-pairing._tcp"   # el celular lo anuncia mientras muestra el código o espera el QR
CONNECT = "_adb-tls-connect._tcp"   # y este mientras la depuración inalámbrica está activa


@dataclass
class Service:
    """Un servicio que anuncia un celular en la red (mDNS)."""
    name: str
    ip: str
    port: int

    @property
    def addr(self) -> str:
        return f"{self.ip}:{self.port}"


def _unescape(text: str) -> str:
    """avahi-browse escapa los caracteres raros como \\DDD (decimal)."""
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), text)


def parse_avahi(text: str, kind: str) -> list[Service]:
    """Salida de `avahi-browse -rpt <tipo>` → servicios resueltos (IPv4; uno por nombre)."""
    out: dict[str, Service] = {}
    for line in text.splitlines():
        f = line.split(";")
        if len(f) < 9 or f[0] != "=" or f[2] != "IPv4" or f[4] != kind:
            continue
        try:
            out.setdefault(_unescape(f[3]), Service(_unescape(f[3]), f[7], int(f[8])))
        except ValueError:
            continue
    return list(out.values())


def qr_payload(name: str, password: str) -> str:
    """Lo que lee el celular en «Vincular dispositivo con código QR» (el mismo formato que Android Studio)."""
    return f"WIFI:T:ADB;S:{name};P:{password};;"


@dataclass
class Device:
    serial: str
    state: str           # "device" = listo; "unauthorized", "offline", "no permissions"…
    model: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def wifi(self) -> bool:
        return bool(re.match(r"^[\w.-]+:\d+$", self.serial)) or "._adb-tls-connect._tcp" in self.serial

    def label(self) -> str:
        return f"{self.nice_name()} · {'Wi‑Fi' if self.wifi else 'USB'}"

    def nice_name(self) -> str:
        return self.model.replace("_", " ") or self.serial


def parse_devices(text: str) -> list[Device]:
    """Salida de `adb devices -l` → dispositivos."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*", "adb server")):
            continue
        parts = line.split()
        serial = parts[0]
        rest = parts[1:]
        # El estado puede ser de dos palabras: «no permissions (…)».
        if rest[:2] == ["no", "permissions"]:
            state = "no permissions"
        else:
            state = rest[0] if rest else "?"
        model = next((p.split(":", 1)[1] for p in rest if p.startswith("model:")), "")
        out.append(Device(serial, state, model))
    return out


class AndroidActivity(ProcActivity):
    """La pantalla de un celular Android en la TV (scrcpy)."""

    id = "android"
    name = "Android"
    icon = "android.svg"
    context = "android"
    procs = {"scrcpy"}
    start_timeout = 15.0
    TITLE = "tele-cec android"

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.cfg = cfg
        self.name = str(cfg.get("name") or self.name)
        self.icon = str(cfg.get("icon") or self.icon)
        self.binary_cfg = str(cfg.get("binary") or "")
        self.adb_cfg = str(cfg.get("adb") or "")
        # "" = el que esté conectado; "192.168.0.50:5555" = ese celular por Wi‑Fi (se conecta solo con adb connect).
        self.serial_cfg = str(cfg.get("serial") or "").strip()
        self.serial = ""            # el celular que se está mostrando
        self.fail_text = ""         # por qué no se pudo abrir (lo muestra el menú)
        self.wayland = True         # cómo se le mandan las teclas a la ventana de scrcpy (wtype o xdotool)
        self._shell: subprocess.Popen | None = None
        self._shell_lock = threading.Lock()
        self._last: list[Device] = []
        self.notif_open = False     # la cortina de notificaciones (amarillo la abre y la cierra)
        self.ready = False          # según el último source(): hay un celular listo (si no, OK abre «conectar»)
        self.want_serial = ""       # el celular elegido en «Android · conectar» (para el próximo open)

    # ---------------------------------------------------------------- programas
    def binary(self) -> str | None:
        return shutil.which(os.path.expanduser(self.binary_cfg or "scrcpy"))

    def adb(self) -> str | None:
        return shutil.which(os.path.expanduser(self.adb_cfg or os.environ.get("ADB", "") or "adb"))

    def installed(self) -> bool:
        return self.binary() is not None and self.adb() is not None

    def _adb_run(self, args: list[str], timeout: float = 5.0) -> tuple[int, str]:
        adb = self.adb()
        if not adb:
            return 127, "falta adb"
        try:
            p = subprocess.run([adb, *args], capture_output=True, text=True, timeout=timeout,  # noqa: S603
                               env=graphical_env(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 124, f"adb {' '.join(args)}: no contestó en {timeout:.0f} s"
        except OSError as exc:
            return 127, str(exc)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def devices(self) -> list[Device]:
        rc, out = self._adb_run(["devices", "-l"], 8)
        if rc != 0:
            log.warning("Android: adb devices: %s", out.strip()[:200])
            return []
        self._last = parse_devices(out)
        return self._last

    def _target(self) -> str:
        """El celular por Wi‑Fi configurado, con el puerto (5555 si no se puso)."""
        s = self.serial_cfg
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", s):
            return s + ":5555"
        return s

    def _pick(self, devs: list[Device]) -> Device | None:
        target = self._target()
        if target:
            return next((d for d in devs if d.serial == target), None)
        ready = [d for d in devs if d.ready]
        if ready:
            return next((d for d in ready if not d.wifi), ready[0])  # con varios: primero el del cable
        return devs[0] if devs else None

    def find_device(self, connect: bool = False) -> Device | None:
        """El celular a mostrar. Con connect, si está configurado uno por Wi‑Fi y no aparece, lo conecta."""
        dev = self._pick(self.devices())
        target = self._target()
        if connect and target and ":" in target and (dev is None or not dev.ready):
            rc, out = self._adb_run(["connect", target], 10)
            log.info("Android: adb connect %s: %s", target, out.strip()[:200] or rc)
            for _ in range(6):  # recién conectado puede figurar un instante como «offline» o «connecting»
                dev = self._pick(self.devices())
                if dev and dev.ready:
                    break
                time.sleep(0.5)
        return dev

    # ---------------------------------------------------------------- Wi‑Fi: descubrir, vincular y conectar
    @staticmethod
    def discovery_available() -> bool:
        return shutil.which("avahi-browse") is not None

    def browse(self, kind: str, timeout: float = 5.0) -> list[Service]:
        """Los celulares que anuncian ese servicio en la red (avahi-browse; tarda un par de segundos)."""
        if not self.discovery_available():
            return []
        try:
            p = subprocess.run(["avahi-browse", "-rpt", kind], capture_output=True, text=True,  # noqa: S603,S607
                               timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return parse_avahi(out, kind)
        except OSError as exc:
            log.warning("Android: avahi-browse: %s", exc)
            return []
        return parse_avahi(p.stdout or "", kind)

    def pair(self, addr: str, code: str) -> tuple[bool, str]:
        rc, out = self._adb_run(["pair", addr, code], 20)
        text = out.strip().splitlines()[-1] if out.strip() else str(rc)
        log.info("Android: adb pair %s: %s", addr, text)
        return rc == 0 and "Successfully paired" in out, text

    def connect(self, addr: str) -> bool:
        rc, out = self._adb_run(["connect", addr], 10)
        log.info("Android: adb connect %s: %s", addr, out.strip()[:200] or rc)
        return rc == 0 and ("connected to" in out) and "failed" not in out.lower()

    def auto_connect(self, services: list[Service] | None = None) -> list[Device]:
        """Conecta los celulares vinculados que anuncian la depuración inalámbrica (el adb de Debian no lo hace
        solo). Los no vinculados rechazan la conexión: no pasa nada."""
        services = self.browse(CONNECT) if services is None else services
        have = {d.serial for d in self.devices() if d.ready}
        for svc in services:
            if svc.addr not in have:
                self.connect(svc.addr)
        return [d for d in self.devices() if d.ready]

    @staticmethod
    def new_pairing() -> tuple[str, str]:
        """Nombre y contraseña para un QR de vinculación."""
        return f"tele-cec-{secrets.token_hex(3)}", f"{secrets.randbelow(10 ** 6):06d}{secrets.token_hex(2)}"

    @staticmethod
    def write_qr(path: str, payload: str) -> bool:
        if not shutil.which("qrencode"):
            log.warning("Android: falta qrencode (sudo apt install qrencode): no puedo mostrar el QR")
            return False
        try:
            subprocess.run(["qrencode", "-t", "SVG", "-m", "2", "-o", path, payload], check=True,  # noqa: S603,S607
                           capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("Android: qrencode: %s", exc)
            return False
        return True

    # ---------------------------------------------------------------- menú
    def source(self) -> tuple[str, bool]:
        """Siempre se puede elegir: sin celular listo, OK abre «Android · conectar» (vincular por Wi‑Fi…)."""
        dev = self._pick(self.devices())
        target = self._target()
        self.ready = bool(dev and dev.ready) or bool(target and ":" in target)
        if dev and dev.ready:
            return dev.label(), True
        if target and ":" in target:
            # Por Wi‑Fi: se intenta conectar al elegirlo (adb connect tarda si el celular no está).
            return f"Wi‑Fi: {target} (se conecta al abrir)", True
        if dev:
            return STATE_TEXT.get(dev.state, f"Celular: {dev.state}"), True
        return NO_DEVICE, True

    def unavailable_text(self) -> str:
        dev = self._pick(self._last)
        if dev and not dev.ready:
            return STATE_TEXT.get(dev.state, f"Celular: {dev.state}")
        return "Conectá el celular por USB y activá la depuración USB (ver Ayuda)"

    # ---------------------------------------------------------------- abrir y cerrar
    def env(self) -> dict[str, str]:
        env = graphical_env()
        driver = str(self.cfg.get("video_driver") or "").strip()
        if not driver and env.get("WAYLAND_DISPLAY"):
            driver = "wayland"  # app Wayland nativa: las teclas van con wtype
        if driver:
            env["SDL_VIDEO_DRIVER"] = driver      # SDL3 (scrcpy 3+)
            env["SDL_VIDEODRIVER"] = driver       # SDL2 (scrcpy viejo)
        self.wayland = driver != "x11" and bool(env.get("WAYLAND_DISPLAY"))
        if self.adb_cfg:
            env["ADB"] = self.adb() or self.adb_cfg
        return env

    def args(self, binary: str, serial: str) -> list[str]:
        c = self.cfg
        args = [binary, f"--serial={serial}", f"--window-title={self.TITLE}", "--no-terminal-title"]
        if c.get("fullscreen", True):
            args.append("--fullscreen")
        if int(c.get("max_size", 0) or 0) > 0:
            args.append(f"--max-size={int(c['max_size'])}")
        if int(c.get("max_fps", 0) or 0) > 0:
            args.append(f"--max-fps={int(c['max_fps'])}")
        if c.get("video_bit_rate"):
            args.append(f"--video-bit-rate={c['video_bit_rate']}")
        if not c.get("audio", True):
            args.append("--no-audio")
        elif c.get("audio_dup", False):
            args.append("--audio-dup")  # Android 13+: el sonido sigue saliendo también por el celular
        if c.get("stay_awake", True):
            args.append("--stay-awake")
        if c.get("turn_screen_off", False):
            args.append("--turn-screen-off")
        if c.get("power_off_on_close", False):
            args.append("--power-off-on-close")
        args.append("--disable-screensaver")
        extra = c.get("extra_args") or []
        args += extra.split() if isinstance(extra, str) else [str(a) for a in extra]
        return args

    def pids(self, fresh: bool = False) -> list[int]:
        """scrcpy (no el adb que lanza, ni el systemd-run que lo envuelve)."""
        return find_pids(lambda argv: os.path.basename(argv[0]) == "scrcpy", fresh)

    def _log_tail(self) -> str:
        try:
            return (LOG_DIR / "android.log").read_text(errors="replace")[-4000:]
        except OSError:
            return ""

    def _error_from_log(self) -> str:
        """Un motivo entendible a partir de lo que escribió scrcpy al fallar."""
        text = self._log_tail()
        low = text.lower()
        if "unauthorized" in low:
            return STATE_TEXT["unauthorized"]
        if "could not find any adb device" in low or "no device" in low or "not found" in low:
            return "No encontré el celular: ¿está conectado y con la depuración USB activada?"
        if "injecting input events requires" in low or "inject_input_events" in low:
            return "Activá «Depuración USB (ajustes de seguridad)» en el celular (Xiaomi)"
        errors = [ln.split("ERROR:", 1)[1].strip() for ln in text.splitlines() if "ERROR:" in ln]
        return f"scrcpy: {errors[-1][:90]}" if errors else ""

    async def open(self, serial: str = "") -> bool:
        serial, self.want_serial = serial or self.want_serial, ""
        self.fail_text = ""
        self.notif_open = False
        binary = self.binary()
        if not binary or not self.adb():
            self.fail_text = "Falta scrcpy o adb (ver README → Android)"
            log.error("Android: %s", self.fail_text)
            return False
        if serial:
            dev = next((d for d in await asyncio.to_thread(self.devices) if d.serial == serial), None)
        else:
            dev = await asyncio.to_thread(self.find_device, True)
            if (not dev or not dev.ready) and self.discovery_available():
                ready = await asyncio.to_thread(self.auto_connect)  # un celular vinculado por Wi‑Fi
                dev = self._pick(ready) if ready else dev
        if not dev or not dev.ready:
            self.fail_text = (STATE_TEXT.get(dev.state, f"Celular: {dev.state}") if dev
                              else f"No encontré el celular en {self._target()}" if self._target()
                              else "Conectá el celular por USB y activá la depuración USB (ver Ayuda)")
            log.error("Android: %s", self.fail_text)
            return False
        self.serial = dev.serial
        log.info("Android: muestro %s", dev.label())
        env = self.env()
        if not await self.spawn(self.args(binary, dev.serial), env=env):
            self.fail_text = self._error_from_log() or "scrcpy no arrancó"
            return False
        # Que aparezca la imagen antes de sacar «Abriendo Android…» (o que falle: entonces se dice por qué).
        deadline = time.monotonic() + float(self.cfg.get("open_wait_s", 15))
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            if not self.pids(fresh=True):
                self.fail_text = self._error_from_log() or "scrcpy se cerró al abrir"
                log.error("Android: %s", self.fail_text)
                return False
            if "Texture:" in self._log_tail():
                break
        return True

    async def close(self) -> None:
        await self.terminate()
        await asyncio.to_thread(self._shell_close)

    async def pause(self) -> None:
        await self.keyevent("pause")

    # ---------------------------------------------------------------- teclas
    async def key(self, name: str) -> bool:
        """Una tecla a la ventana de scrcpy (que se la pasa al celular): flechas, Return, números…"""
        if self.wayland:
            return await keyboard.wtype_key(name)
        return await keyboard.xdotool_key(name)

    async def keyevent(self, what: str | int) -> bool:
        """Una tecla de Android (KEYCODE_*) directo al celular, por el shell de adb que queda abierto."""
        code = KEYCODES.get(str(what), what)
        try:
            code = int(code)
        except (TypeError, ValueError):
            log.warning("Android: tecla %r desconocida", what)
            return False
        return await asyncio.to_thread(self._shell_send, f"input keyevent {code}")

    async def shell(self, command: str) -> bool:
        return await asyncio.to_thread(self._shell_send, command)

    def _shell_send(self, command: str) -> bool:
        with self._shell_lock:
            for attempt in (1, 2):
                sh = self._shell
                if sh is None or sh.poll() is not None:
                    sh = self._shell = self._shell_start()
                    if sh is None:
                        return False
                try:
                    assert sh.stdin is not None
                    sh.stdin.write((command + "\n").encode())
                    sh.stdin.flush()
                    return True
                except (OSError, ValueError) as exc:
                    log.debug("Android: shell de adb cerrado (%s); lo reabro", exc)
                    self._shell = None
                    if attempt == 2:
                        log.warning("Android: no pude mandar «%s» al celular", command)
        return False

    def _shell_start(self) -> subprocess.Popen | None:
        adb = self.adb()
        if not adb:
            return None
        if not self.serial:
            dev = self.find_device()
            if not dev or not dev.ready:
                log.warning("Android: no hay celular conectado para mandarle teclas")
                return None
            self.serial = dev.serial
        try:
            return subprocess.Popen([adb, "-s", self.serial, "shell"], stdin=subprocess.PIPE,  # noqa: S603
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=graphical_env(),
                                    start_new_session=True)
        except OSError as exc:
            log.warning("Android: no pude abrir adb shell: %s", exc)
            return None

    def _shell_close(self) -> None:
        with self._shell_lock:
            sh, self._shell = self._shell, None
        if sh and sh.poll() is None:
            with contextlib.suppress(OSError, ValueError):
                assert sh.stdin is not None
                sh.stdin.write(b"exit\n")
                sh.stdin.close()
            try:
                sh.wait(2)
            except subprocess.TimeoutExpired:
                sh.kill()
