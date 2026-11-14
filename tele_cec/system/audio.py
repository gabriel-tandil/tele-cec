"""Salida de sonido: por dónde sale el audio (HDMI de la TV, Bluetooth, analógica, USB) y cambiarla.

Raspberry Pi OS usa PipeWire con WirePlumber:
- `pw-dump` (JSON) lista las salidas («sinks») y cuál es la predeterminada.
- `wpctl set-default <id>` la cambia; WirePlumber pasa ahí lo que esté sonando.
- Los dispositivos Bluetooth emparejados pero desconectados no tienen salida todavía: se listan con
  `bluetoothctl` y al elegirlos se conectan.
- Los parlantes y auriculares Bluetooth cercanos que todavía no están emparejados se buscan mientras se ve la
  lista (BluetoothScan); al elegir uno se empareja, se marca de confianza y se conecta (y se cierra el aviso
  que abre el panel del escritorio, ver desktop.py).

Las funciones sin async se llaman en un hilo aparte (asyncio.to_thread): corren comandos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass

from . import desktop
from .procs import graphical_env, run_cmd

log = logging.getLogger("sonido")

SINK = "@DEFAULT_AUDIO_SINK@"  # el volumen y el silencio son los de la salida actual

BT_AUDIO_UUID = "0000110b"  # «Audio Sink»: el dispositivo reproduce audio (parlante, auriculares)
# Íconos que BlueZ deduce de la clase del dispositivo: parlante o equipo de audio, auriculares con y sin micrófono.
BT_AUDIO_ICONS = {"audio-card", "audio-headset", "audio-headphones"}
# Clase de dispositivo Bluetooth: categoría mayor 4 = audio/video; menores que reproducen sonido (auricular, manos
# libres, parlante, auriculares, audio portátil, del auto, equipo de música).
BT_AUDIO_MINORS = {1, 2, 5, 6, 7, 8, 10}


@dataclass
class Output:
    name: str                 # cómo se muestra en el menú
    kind: str                 # "hdmi", "bluetooth", "analog", "usb" u "other"
    sink_id: int | None = None  # id de PipeWire (None: Bluetooth emparejado sin conectar)
    node_name: str = ""
    mac: str = ""             # Bluetooth
    default: bool = False     # la salida actual
    paired: bool = True       # Bluetooth: False = encontrado cerca, todavía sin emparejar

    @property
    def detail(self) -> str:
        """Línea secundaria del menú."""
        kind = {"hdmi": "HDMI", "bluetooth": "Bluetooth", "analog": "Analógica", "usb": "USB"}.get(self.kind, "")
        if self.kind == "bluetooth" and not self.paired:
            return "Bluetooth · nuevo (OK lo empareja y conecta)"
        if self.kind == "bluetooth" and self.sink_id is None:
            return "Bluetooth · desconectado (OK lo conecta)"
        return kind


def available() -> bool:
    return bool(shutil.which("pw-dump") and shutil.which("wpctl"))


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=graphical_env(), check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s: %s", cmd[0], exc)
        return ""
    return p.stdout if p.returncode == 0 else ""


def _pw_dump() -> list[dict]:
    try:
        data = json.loads(_run(["pw-dump"]) or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _sinks(dump: list[dict]) -> tuple[list[dict], str]:
    """(nodos de salida, node.name de la predeterminada)."""
    sinks, default = [], ""
    for o in dump:
        if o.get("type") == "PipeWire:Interface:Node":
            info = o.get("info") or {}
            if (info.get("props") or {}).get("media.class") == "Audio/Sink":
                sinks.append(o)
        elif o.get("type") == "PipeWire:Interface:Metadata" and (o.get("props") or {}).get("metadata.name") == "default":
            for m in o.get("metadata") or []:
                if m.get("key") == "default.audio.sink" and isinstance(m.get("value"), dict):
                    default = str(m["value"].get("name") or "")
    return sinks, default


def _kind(props: dict) -> str:
    name = str(props.get("node.name") or "")
    if props.get("device.api") == "bluez5" or name.startswith("bluez_output"):
        return "bluetooth"
    if "hdmi" in name.lower() or "hdmi" in str(props.get("alsa.card_name") or "").lower():
        return "hdmi"
    if ".usb-" in name or props.get("device.bus") == "usb":
        return "usb"
    if "analog" in name.lower() or "headphones" in name.lower():
        return "analog"
    return "other"


def _bt_devices(which: str = "") -> list[tuple[str, str]]:
    """(MAC, nombre) de `bluetoothctl devices [Paired]`: los emparejados o todos los que conoce BlueZ."""
    if not shutil.which("bluetoothctl"):
        return []
    out = []
    for line in _run(["bluetoothctl", "devices", *([which] if which else [])]).splitlines():
        m = re.match(r"Device ([0-9A-Fa-f:]{17}) (.*)", line.strip())
        if m:
            out.append((m.group(1).upper(), m.group(2).strip()))
    return out


def bt_info(mac: str) -> dict:
    """Lo que interesa de `bluetoothctl info`: nombre, ícono, clase, UUIDs, emparejado, conectado."""
    text = _run(["bluetoothctl", "info", mac])
    info: dict = {"uuids": [], "found": bool(text.strip())}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(": ")
        if not sep:
            continue
        if key == "UUID":
            info["uuids"].append(value.lower())
        elif key in ("Name", "Alias", "Icon", "Class", "Paired", "Connected"):
            info[key.lower()] = value.strip()
    return info


def bt_is_audio(info: dict) -> bool:
    """¿Reproduce sonido? (parlante, auriculares, equipo de música…; no micrófonos, teléfonos ni teclados)."""
    if info.get("icon") in BT_AUDIO_ICONS or any(BT_AUDIO_UUID in u for u in info.get("uuids", [])):
        return True
    try:
        cls = int(str(info.get("class", "")).split()[0], 16)
    except (ValueError, IndexError):
        return False
    return (cls >> 8) & 0x1F == 4 and (cls >> 2) & 0x3F in BT_AUDIO_MINORS


def _looks_like_mac(name: str) -> bool:
    """BlueZ usa la dirección como nombre mientras no sabe cómo se llama el dispositivo."""
    return bool(re.fullmatch(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", name.strip()))


def _bt_paired_audio() -> list[tuple[str, str]]:
    """(MAC, nombre) de los dispositivos Bluetooth emparejados que reproducen audio."""
    return [(mac, name) for mac, name in _bt_devices("Paired") if bt_is_audio(bt_info(mac))]


def bt_nearby_audio(skip: set[str]) -> list[Output]:
    """Parlantes y auriculares que BlueZ encontró cerca (en una búsqueda) y que no están en `skip`."""
    out = []
    for mac, name in _bt_devices():
        if mac in skip or _looks_like_mac(name):
            continue
        info = bt_info(mac)
        if info.get("paired") == "yes" or not bt_is_audio(info):
            continue
        out.append(Output(name=info.get("alias") or name, kind="bluetooth", mac=mac, paired=False))
    return out


class BluetoothScan:
    """Busca dispositivos Bluetooth mientras se ve la lista de salidas (bluetoothctl scan, con tope de tiempo).

    BlueZ busca mientras el cliente que lo pidió siga conectado: el bluetoothctl queda abierto hasta `seconds`
    o hasta stop(). Lo encontrado se lee con bt_nearby_audio (BlueZ lo recuerda un rato después de buscar).
    """

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("bluetoothctl"))

    async def start(self, seconds: int = 25) -> bool:
        await self.stop()
        await run_cmd(["bluetoothctl", "power", "on"], 5)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "--timeout", str(seconds), "scan", "on", env=graphical_env(),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            log.warning("No pude buscar dispositivos Bluetooth: %s", exc)
            return False
        log.info("Busco parlantes y auriculares Bluetooth (%d s)", seconds)
        return True

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def stop(self) -> None:
        if self.running():
            assert self.proc is not None
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None


def list_outputs() -> list[Output]:
    """Las salidas disponibles, con la actual marcada (HDMI primero, después Bluetooth y el resto)."""
    sinks, default = _sinks(_pw_dump())
    hdmi = [s for s in sinks if _kind(s["info"]["props"]) == "hdmi"]
    outs: list[Output] = []
    seen_macs: set[str] = set()
    for s in sinks:
        p = s["info"]["props"]
        kind = _kind(p)
        name = str(p.get("node.description") or p.get("node.nick") or p.get("node.name") or "?")
        mac = str(p.get("api.bluez5.address") or "").upper()
        if kind == "hdmi":
            card = re.search(r"hdmi-(\d)", str(p.get("alsa.card_name") or ""))
            name = "TV (HDMI)" if len(hdmi) == 1 or not card else f"TV (HDMI {int(card.group(1)) + 1})"
        if mac:
            seen_macs.add(mac)
        outs.append(Output(name=name, kind=kind, sink_id=int(s["id"]), node_name=str(p.get("node.name") or ""),
                           mac=mac, default=p.get("node.name") == default))
    for mac, name in _bt_paired_audio():
        if mac.upper() not in seen_macs:
            outs.append(Output(name=name, kind="bluetooth", mac=mac.upper()))
    order = {"hdmi": 0, "bluetooth": 1, "analog": 2, "usb": 3, "other": 4}
    outs.sort(key=lambda o: (order.get(o.kind, 9), o.sink_id is None, o.name.lower()))
    return outs


def current_name() -> str:
    cur = next((o for o in list_outputs() if o.default), None)
    return cur.name if cur else ""


# ---------------------------------------------------------------- volumen (el menú de la TV y el celular)
def parse_volume(text: str) -> tuple[int | None, bool]:
    """`wpctl get-volume` → (porcentaje, silenciado). «Volume: 0.40 [MUTED]» → (40, True)."""
    m = re.search(r"Volume:\s*([\d.]+)", text)
    if not m:
        return None, False
    return round(float(m.group(1)) * 100), "MUTED" in text


def volume_now() -> tuple[int | None, bool]:
    """Lo mismo que get_volume, sin async (para el menú, que se arma en un hilo aparte)."""
    return parse_volume(_run(["wpctl", "get-volume", SINK], 3))


async def get_volume() -> tuple[int | None, bool]:
    """(porcentaje, silenciado) de la salida actual; (None, False) si no se sabe."""
    rc, out = await run_cmd(["wpctl", "get-volume", SINK], 3)
    return parse_volume(out) if rc == 0 else (None, False)


async def change_volume(what: str, step: int = 5) -> bool:
    """«+» / «-» (de a step %), «=40» (fijarlo en 40 %) o «mute» (silencio sí/no). Subir, bajar o fijar el
    volumen también saca el silencio (como en una tele)."""
    if what == "mute":
        rc, _ = await run_cmd(["wpctl", "set-mute", SINK, "toggle"], 3)
        return rc == 0
    if what in ("+", "-"):
        amount = f"{step}%{what}"
    elif re.fullmatch(r"=\d{1,3}", what):
        amount = f"{min(100, int(what[1:]))}%"
    else:
        return False
    rc, _ = await run_cmd(["wpctl", "set-volume", "-l", "1.0", SINK, amount], 3)
    if rc == 0:
        await run_cmd(["wpctl", "set-mute", SINK, "0"], 3)
    return rc == 0


def default_kind() -> str:
    """Por dónde sale el sonido ahora: "hdmi", "bluetooth", "analog", "usb", "other" ("" = no se sabe)."""
    if not available():
        return ""
    sinks, default = _sinks(_pw_dump())
    cur = next((s for s in sinks if (s.get("info") or {}).get("props", {}).get("node.name") == default), None)
    return _kind(cur["info"]["props"]) if cur else ""


def sink_running() -> bool:
    """¿Sale sonido por alguna salida de PipeWire? (también Bluetooth, que no pasa por /proc/asound)."""
    sinks, _ = _sinks(_pw_dump())
    return any((s.get("info") or {}).get("state") == "running" for s in sinks)


async def pair(out: Output, timeout: float = 25.0) -> tuple[bool, str]:
    """Empareja un parlante o auriculares nuevos (sin PIN: «just works») y los marca de confianza, así después se
    reconectan solos."""
    if not bt_info(out.mac).get("found"):
        # BlueZ se olvida de lo encontrado un rato después de la búsqueda: buscarlo de nuevo un momento.
        await run_cmd(["bluetoothctl", "--timeout", "8", "scan", "on"], 12)
    log.info("Emparejo %s (%s)", out.name, out.mac)
    rc, text = await run_cmd(["bluetoothctl", "--agent", "NoInputNoOutput", "--timeout", str(int(timeout)),
                              "pair", out.mac], timeout + 5)
    if rc != 0 or not re.search(r"Pairing successful|AlreadyExists", text):
        log.warning("No pude emparejar %s: %s", out.name, text.strip()[-300:] or rc)
        return False, f"No pude emparejar {out.name}: ponelo en modo emparejamiento y probá de nuevo"
    await run_cmd(["bluetoothctl", "trust", out.mac], 5)
    return True, ""


async def select(out: Output, connect_timeout: float = 25.0) -> tuple[bool, str]:
    """Pasa el sonido a esa salida (conecta antes el Bluetooth si hace falta). Devuelve (ok, mensaje)."""
    if out.mac and not out.paired:
        # El panel del escritorio abre su propio aviso de «emparejado» (con un OK que el control no alcanza):
        # se lo va cerrando mientras dura.
        closer = desktop.PairingNoticeCloser()
        closer.start()
        try:
            return await _select(out, connect_timeout)
        finally:
            closer.finish()
    return await _select(out, connect_timeout)


async def _select(out: Output, connect_timeout: float) -> tuple[bool, str]:
    sink_id = out.sink_id
    if out.mac and not out.paired:
        ok, text = await pair(out, connect_timeout)
        if not ok:
            return False, text
    if sink_id is None and out.mac:
        log.info("Conecto %s (%s)", out.name, out.mac)
        rc, text = await run_cmd(["bluetoothctl", "--timeout", str(int(connect_timeout)), "connect", out.mac],
                                 connect_timeout + 5)
        if rc != 0 or "Failed" in text:
            log.warning("No pude conectar %s: %s", out.name, text.strip()[-200:] or rc)
            return False, f"No pude conectar {out.name} (¿está prendido y cerca?)"
        deadline = time.monotonic() + 10  # la salida aparece en PipeWire un instante después
        while time.monotonic() < deadline and sink_id is None:
            await asyncio.sleep(0.5)
            found = next((o for o in await asyncio.to_thread(list_outputs) if o.mac == out.mac and o.sink_id), None)
            sink_id = found.sink_id if found else None
        if sink_id is None:
            return False, f"{out.name} se conectó pero no apareció como salida de sonido"
    if sink_id is None:
        return False, "Esa salida ya no está"
    rc, text = await run_cmd(["wpctl", "set-default", str(sink_id)], 5)
    if rc != 0:
        log.warning("wpctl set-default %s: %s", sink_id, text.strip() or rc)
        return False, "No pude cambiar la salida de sonido"
    log.info("Sonido por %s (%s)", out.name, sink_id)
    if out.kind != "bluetooth":
        await disconnect_bluetooth()
    return True, f"Sonido por {out.name}"


async def disconnect_bluetooth() -> list[str]:
    """Al pasar el sonido a la TV (u otra salida que no es Bluetooth), los parlantes y auriculares Bluetooth
    conectados se desconectan: no quedan tomados (ni gastando batería) y al reiniciar la Pi no vuelven solos."""
    gone = []
    for o in await asyncio.to_thread(list_outputs):
        if o.kind == "bluetooth" and o.sink_id is not None and o.mac:
            log.info("Desconecto %s (%s): el sonido ya no sale por ahí", o.name, o.mac)
            rc, text = await run_cmd(["bluetoothctl", "disconnect", o.mac], 15)
            if rc != 0 or "Failed" in text:
                log.warning("No pude desconectar %s: %s", o.name, text.strip()[-200:] or rc)
            else:
                gone.append(o.name)
    return gone

