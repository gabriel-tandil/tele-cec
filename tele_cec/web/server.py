"""Página web para manejar tele-cec desde el celular (en la misma red): http://<pi>:8765/

- el control remoto en la pantalla: las teclas entran a la misma cola que las del control de la TV (contexto, menú,
  reposo: una tecla despierta a la Pi, igual que el control);
- lo que está sonando (MPRIS) con play/pausa, anterior y siguiente;
- el volumen de la salida actual y la salida de sonido (TV por HDMI, parlante Bluetooth…).

Es un servidor HTTP mínimo con asyncio (sin dependencias). El celular pregunta el estado cada un par de segundos.

Seguridad: no tiene contraseña. Solo atiende direcciones de la red local (privadas o loopback), y los pedidos que
cambian algo tienen que traer el encabezado X-Tele-CEC: así una página de internet abierta en el celular no puede
mandarle teclas (el navegador no deja agregar ese encabezado a otro sitio sin permiso, y no lo damos).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import mimetypes
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..keys import parse_key
from ..system import audio
from . import nowplaying

if TYPE_CHECKING:
    from ..controller import Controller

log = logging.getLogger("web")
STATIC = Path(__file__).with_name("static")
MAX_BODY = 16 * 1024
STATE_TTL_S = 0.8       # varios celulares mirando: el estado se arma como mucho una vez por este lapso
OUTPUTS_TTL_S = 6.0     # las salidas de sonido cambian poco (pw-dump y bluetoothctl tardan un poco)
HEADER = "x-tele-cec"

# Teclas que acepta la página (las del control remoto que dibuja).
KEYS = {"UP", "DOWN", "LEFT", "RIGHT", "SELECT", "EXIT", "CONTENTS_MENU", "HELP", "F1_BLUE", "F2_RED", "F3_GREEN",
        "F4_YELLOW", "PAUSE_PLAY_FUNCTION", "PLAY", "PAUSE", "STOP", "FORWARD", "BACKWARD", "REWIND",
        "FAST_FORWARD", "CLEAR"} | {f"NUMBER{i}" for i in range(10)}


def allowed_host(host: str) -> bool:
    """El nombre con el que se pidió la página: una IP, un nombre .local, localhost o un nombre sin puntos (el de la
    Pi en la red). Un dominio de internet que apunte a la Pi (DNS rebinding) no."""
    name = host.strip().lower()
    if name.startswith("["):  # [IPv6]:puerto
        name = name[1:].partition("]")[0]
    elif name.count(":") == 1:
        name = name.partition(":")[0]
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or name.endswith((".local", ".lan", ".home", ".internal")) or "." not in name


def allowed_client(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


class WebRemote:
    def __init__(self, ctl: Controller, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.ctl = ctl
        self.enabled = bool(cfg.get("enabled", True))
        self.port = int(cfg.get("port", 8765))
        self.bind = str(cfg.get("bind") or "0.0.0.0")  # noqa: S104  (es para la red de la casa; ver allowed_client)
        self.lan_only = bool(cfg.get("lan_only", True))
        self.volume_step = int(cfg.get("volume_step", 5))
        self.server: asyncio.AbstractServer | None = None
        self._state: dict[str, Any] | None = None
        self._state_at = 0.0
        self._state_lock = asyncio.Lock()
        self._outputs: list[audio.Output] = []
        self._outputs_at = 0.0
        self._art: str = ""          # archivo de la tapa que se está mostrando (file://)

    # ---------------------------------------------------------------- servidor
    async def start(self) -> bool:
        if not self.enabled:
            return False
        try:
            self.server = await asyncio.start_server(self._handle, self.bind, self.port)
        except OSError as exc:
            log.warning("No pude abrir la página del celular en el puerto %d: %s", self.port, exc)
            return False
        log.info("Control desde el celular: http://%s:%d/", _hostname(), self.port)
        return True

    async def close(self) -> None:
        if self.server is None:
            return
        self.server.close()
        try:
            await asyncio.wait_for(self.server.wait_closed(), 2)
        except asyncio.TimeoutError:
            pass
        self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        try:
            if self.lan_only and not allowed_client(str(peer[0])):
                log.warning("Página del celular: rechazo %s (no es de la red local)", peer[0])
                await _respond(writer, 403, b"Solo desde la red de la casa\n", "text/plain; charset=utf-8")
                return
            req = await asyncio.wait_for(_read_request(reader), 10)
            if req is None:
                return
            method, path, query, headers, body = req
            if self.lan_only and not allowed_host(headers.get("host", "")):
                log.warning("Página del celular: rechazo el nombre %r", headers.get("host", ""))
                await _respond(writer, 403, b"Nombre no permitido\n", "text/plain; charset=utf-8")
                return
            status, payload, ctype, extra = await self.route(method, path, query, headers, body)
            await _respond(writer, status, payload, ctype, extra)
        except (asyncio.TimeoutError, ConnectionError, ValueError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("Página del celular: error atendiendo un pedido")
            try:
                await _respond(writer, 500, b"error\n", "text/plain")
            except ConnectionError:
                pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- rutas
    async def route(self, method: str, path: str, query: dict[str, str], headers: dict[str, str],
                    body: bytes) -> tuple[int, bytes, str, dict[str, str]]:
        if method == "GET":
            if path == "/api/state":
                return _json(await self.state())
            if path == "/api/art":
                return self._art_file()
            if path in ("/", "/index.html"):
                return _static("index.html", no_cache=True)
            name = path.lstrip("/")
            if name in ("manifest.webmanifest", "icon.svg"):
                return _static(name)
            return 404, b"no existe\n", "text/plain", {}
        if method != "POST":
            return 405, b"", "text/plain", {}
        if headers.get(HEADER) != "1":
            return 403, b"falta el encabezado X-Tele-CEC\n", "text/plain", {}
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return _json({"ok": False, "error": "JSON inválido"}, 400)
        if not isinstance(data, dict):
            return _json({"ok": False, "error": "JSON inválido"}, 400)
        if path == "/api/key":
            return _json(await self.key(str(data.get("key") or "")))
        if path == "/api/media":
            return _json(await self.media(str(data.get("command") or ""), str(data.get("player") or "")))
        if path == "/api/volume":
            ok = await nowplaying.change_volume(str(data.get("change") or ""), self.volume_step)
            self._state = None
            return _json({"ok": ok})
        if path == "/api/sound":
            return _json(await self.sound(str(data.get("id") or "")))
        return 404, b"no existe\n", "text/plain", {}

    # ---------------------------------------------------------------- acciones
    async def key(self, name: str) -> dict[str, Any]:
        name = name.strip().upper()
        code = parse_key(name) if name in KEYS else None
        if code is None:
            return {"ok": False, "error": f"tecla no permitida: {name!r}"}
        log.info("Celular: tecla %s", name)
        self.ctl.on_key(code)
        await asyncio.sleep(0.03)
        self.ctl.on_release()
        self._state = None
        return {"ok": True}

    async def media(self, command: str, player: str) -> dict[str, Any]:
        tracks = await nowplaying.players()
        if not any(t.player == player for t in tracks):  # solo reproductores que existen ahora
            return {"ok": False, "error": "ese reproductor ya no está"}
        log.info("Celular: %s en %s", command, player)
        self.ctl.power.last_activity = time.monotonic()  # alguien está usando la música: no entrar en reposo
        ok = await nowplaying.control(player, command)
        self._state = None
        return {"ok": ok}

    async def sound(self, ident: str) -> dict[str, Any]:
        outs = await asyncio.to_thread(audio.list_outputs)
        out = next((o for o in outs if _out_id(o) == ident), None)
        if out is None:
            return {"ok": False, "error": "esa salida ya no está"}
        log.info("Celular: sonido por %s", out.name)
        ok, msg = await audio.select(out)
        self._outputs_at = 0.0
        self._state = None
        if ok:
            await self.ctl.overlay.toast(msg, 2000)
        return {"ok": ok, "message": msg}

    # ---------------------------------------------------------------- estado
    async def state(self) -> dict[str, Any]:
        async with self._state_lock:
            if self._state is not None and time.monotonic() - self._state_at < STATE_TTL_S:
                return self._state
            self._state = await self._build_state()
            self._state_at = time.monotonic()
            return self._state

    def _mpris(self, ident: str) -> Any:
        """El MPRIS de una actividad (Tidal lo tiene en ctl.music, High Tide; las demás, propio)."""
        if ident == "tidal":
            return getattr(getattr(self.ctl, "music", None), "mpris", None)
        return getattr(self.ctl.activities.get(ident), "mpris", None)

    def _players_for(self, ident: str | None) -> list[str]:
        """Los nombres MPRIS de la app en pantalla (para preferir su reproductor)."""
        if not ident:
            return []
        mpris = self._mpris(ident)
        names = [mpris.player] if mpris is not None else []
        if ident == "netflix":
            names.append(self.ctl.browser.engine)
        return names

    def _app_of(self, player: str) -> str:
        """De qué app es un reproductor (para mostrarlo: «Tidal», «Videos»…)."""
        base = player.split(".")[0]
        if base in ("firefox", "chromium"):
            # Netflix y YouTube son el mismo navegador para MPRIS: la que esté en pantalla.
            cur = self.ctl.current_app()
            return self.ctl.activities[cur].name if cur in ("netflix", "youtube") else "Navegador"
        for ident, act in self.ctl.activities.items():
            mpris = self._mpris(ident)
            if mpris is not None and (player == mpris.player or player.startswith(mpris.player + ".")):
                return act.name
        return base

    async def _build_state(self) -> dict[str, Any]:
        ctl = self.ctl
        cur = ctl.current_app()
        tracks, (vol, muted) = await asyncio.gather(nowplaying.players(), nowplaying.volume())
        track = nowplaying.pick(tracks, self._players_for(cur))
        now = None
        if track is not None:
            now = track.to_dict()
            now["app"] = self._app_of(track.player)
            if track.art.startswith("file://"):
                self._art = urllib.parse.unquote(urllib.parse.urlparse(track.art).path)
                now["art"] = f"/api/art?v={abs(hash(self._art)) % 10 ** 8}"
            elif not track.art.startswith(("http://", "https://")):
                now["art"] = ""
        if time.monotonic() - self._outputs_at > OUTPUTS_TTL_S and audio.available():
            try:
                self._outputs = await asyncio.to_thread(audio.list_outputs)
            except Exception:  # noqa: BLE001
                log.exception("Página del celular: no pude leer las salidas de sonido")
            self._outputs_at = time.monotonic()
        return {
            "app": {"id": cur, "name": ctl.activities[cur].name if cur else ""},
            "menu": bool(ctl._menu_open),
            "asleep": bool(ctl.power.asleep),
            "now": now,
            "volume": {"level": vol, "muted": muted},
            "outputs": [{"id": _out_id(o), "name": o.name, "detail": _detail(o), "kind": o.kind,
                         "current": o.default} for o in self._outputs if o.paired],
        }

    def _art_file(self) -> tuple[int, bytes, str, dict[str, str]]:
        """La tapa del tema actual cuando el reproductor la da como archivo local (solo esa, nada más)."""
        path = Path(self._art) if self._art else None
        if path is None or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            return 404, b"", "text/plain", {}
        ctype = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        if not ctype.startswith("image/"):
            return 404, b"", "text/plain", {}
        return 200, path.read_bytes(), ctype, {"Cache-Control": "max-age=3600"}


# ---------------------------------------------------------------- HTTP mínimo
def _detail(o: audio.Output) -> str:
    if o.kind == "bluetooth" and o.sink_id is None:
        return "Bluetooth · desconectado (tocá para conectarlo)"
    return o.detail


def _out_id(o: audio.Output) -> str:
    return o.mac or o.node_name or o.name


def _hostname() -> str:
    import socket  # noqa: PLC0415
    name = socket.gethostname()
    return name if "." in name else f"{name}.local"


def _json(obj: Any, status: int = 200) -> tuple[int, bytes, str, dict[str, str]]:
    return status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", \
        {"Cache-Control": "no-store"}


def _static(name: str, no_cache: bool = False) -> tuple[int, bytes, str, dict[str, str]]:
    path = STATIC / name
    try:
        data = path.read_bytes()
    except OSError:
        return 404, b"", "text/plain", {}
    ctype = {"webmanifest": "application/manifest+json"}.get(path.suffix.lstrip("."), "") \
        or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype.endswith(("+json", "+xml")):
        ctype += "; charset=utf-8"
    return 200, data, ctype, {"Cache-Control": "no-cache" if no_cache else "max-age=86400"}


async def _read_request(reader: asyncio.StreamReader):
    line = await reader.readline()
    if not line:
        return None
    parts = line.decode("latin-1").strip().split()
    if len(parts) != 3:
        raise ValueError("pedido inválido")
    method, target, _ = parts
    headers: dict[str, str] = {}
    while True:
        h = await reader.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        k, _, v = h.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
        if len(headers) > 64:
            raise ValueError("demasiados encabezados")
    length = int(headers.get("content-length") or 0)
    if length < 0 or length > MAX_BODY:
        raise ValueError("cuerpo demasiado grande")
    body = await reader.readexactly(length) if length else b""
    url = urllib.parse.urlsplit(target)
    query = dict(urllib.parse.parse_qsl(url.query))
    return method.upper(), url.path, query, headers, body


async def _respond(writer: asyncio.StreamWriter, status: int, body: bytes, ctype: str,
                   extra: dict[str, str] | None = None) -> None:
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
              500: "Internal Server Error"}.get(status, "OK")
    head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {ctype}", f"Content-Length: {len(body)}",
            "Connection: close", "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer"]
    head += [f"{k}: {v}" for k, v in (extra or {}).items()]
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()
