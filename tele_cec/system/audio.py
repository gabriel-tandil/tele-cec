"""Salida de sonido: por dónde sale el audio (HDMI de la TV, Bluetooth, analógica, USB) y cambiarla.

Raspberry Pi OS usa PipeWire con WirePlumber:
- `pw-dump` (JSON) lista las salidas («sinks») y cuál es la predeterminada.
- `wpctl set-default <id>` la cambia; WirePlumber pasa ahí lo que esté sonando.
- Los dispositivos Bluetooth emparejados pero desconectados no tienen salida todavía: se listan con
  `bluetoothctl` y al elegirlos se conectan (emparejar uno nuevo se hace una vez desde el escritorio).

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

from .procs import graphical_env, run_cmd

log = logging.getLogger("sonido")

BT_AUDIO_UUID = "0000110b"  # «Audio Sink»: el dispositivo reproduce audio (parlante, auriculares)


@dataclass
class Output:
    name: str                 # cómo se muestra en el menú
    kind: str                 # "hdmi", "bluetooth", "analog", "usb" u "other"
    sink_id: int | None = None  # id de PipeWire (None: Bluetooth emparejado sin conectar)
    node_name: str = ""
    mac: str = ""             # Bluetooth
    default: bool = False     # la salida actual

    @property
    def detail(self) -> str:
        """Línea secundaria del menú."""
        kind = {"hdmi": "HDMI", "bluetooth": "Bluetooth", "analog": "Analógica", "usb": "USB"}.get(self.kind, "")
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


def _bt_paired_audio() -> list[tuple[str, str]]:
    """(MAC, nombre) de los dispositivos Bluetooth emparejados que reproducen audio."""
    if not shutil.which("bluetoothctl"):
        return []
    out = []
    for line in _run(["bluetoothctl", "devices", "Paired"]).splitlines():
        m = re.match(r"Device ([0-9A-F:]{17}) (.*)", line.strip())
        if not m:
            continue
        info = _run(["bluetoothctl", "info", m.group(1)])
        if BT_AUDIO_UUID in info.lower() or re.search(r"Icon: audio-", info):
            out.append((m.group(1), m.group(2).strip()))
    return out


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


def sink_running() -> bool:
    """¿Sale sonido por alguna salida de PipeWire? (también Bluetooth, que no pasa por /proc/asound)."""
    sinks, _ = _sinks(_pw_dump())
    return any((s.get("info") or {}).get("state") == "running" for s in sinks)


async def select(out: Output, connect_timeout: float = 25.0) -> tuple[bool, str]:
    """Pasa el sonido a esa salida (conecta antes el Bluetooth si hace falta). Devuelve (ok, mensaje)."""
    sink_id = out.sink_id
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
    return True, f"Sonido por {out.name}"
