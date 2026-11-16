"""Página web para manejar tele-cec desde el celular (en la misma red): http://<pi>:8765/

- el control remoto en la pantalla: las teclas entran a la misma cola que las del control de la TV (contexto, menú,
  reposo: una tecla despierta a la Pi, igual que el control);
- lo que está sonando (MPRIS) con play/pausa, anterior, siguiente, ir a otra parte del tema, aleatorio, repetir
  y, en Tidal, «Me gusta»;
- abrir una app (Netflix, YouTube, Tidal…) y escribir texto en la TV (buscar en Netflix o YouTube);
- Tidal: la biblioteca, la búsqueda y la cola, a través de High Tide (web/tidal.py);
- la música local, igual: artistas, discos, temas, búsqueda y la cola de mpv (web/music.py);
- el volumen de la salida actual y la salida de sonido (TV por HDMI, parlante Bluetooth…).

Es un servidor HTTP mínimo con asyncio (sin dependencias). El celular pregunta el estado cada un par de segundos.

Seguridad: no tiene contraseña. Solo atiende direcciones de la red local (privadas o loopback), y los pedidos que
cambian algo tienen que traer el encabezado X-Tele-CEC: así una página de internet abierta en el celular no puede
mandarle teclas (el navegador no deja agregar ese encabezado a otro sitio sin permiso, y no lo damos).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import apps as activities_mod
from ..apps import resume
from ..apps.netflix.cdp import CDPError
from ..apps.photos import ALL_MODES
from ..keys import parse_key
from ..system import audio, keyboard
from ..system.procs import LOG_DIR
from . import nowplaying
from .menu import MenuWeb
from .music import LocalMusicWeb
from .tidal import TidalWeb

if TYPE_CHECKING:
    from ..controller import Controller

log = logging.getLogger("web")
# Lo que se sirve tal cual de static/: el manifiesto y el logo (el ícono de la pestaña y del acceso directo).
STATIC_FILES = {"manifest.webmanifest", "icon.svg", "icon-192.png", "icon-512.png", "icon-maskable-512.png",
                "apple-touch-icon.png", "favicon.ico"}
STATIC = Path(__file__).with_name("static")


def _ui_version() -> str:
    """Un resumen de la página del celular: si cambia (se instaló otra versión), la página abierta se recarga sola."""
    try:
        return hashlib.sha1((STATIC / "index.html").read_bytes()).hexdigest()[:10]
    except OSError:
        return ""


UI_VERSION = _ui_version()
ICONS = Path(__file__).resolve().parent.parent / "ui" / "icons"
TEXT_APPS = {"youtube", "tidal", "android"}  # (el Navegador escribe por su script, no con wtype)
NETFLIX_SEARCH = "https://www.netflix.com/search?q="   # apps Wayland: el texto se escribe con wtype (Netflix: por el navegador)
APPS_TTL_S = 30.0
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
        self.menu_qr = bool(cfg.get("menu_qr", True))
        self._qr_url = ""
        self._qr_warned = False
        self.server: asyncio.AbstractServer | None = None
        self._state: dict[str, Any] | None = None
        self._state_at = 0.0
        self._state_lock = asyncio.Lock()
        self._outputs: list[audio.Output] = []
        self._outputs_at = 0.0
        self._art: str = ""          # archivo de la tapa que se está mostrando (file://)
        self._apps: list[dict[str, Any]] = []
        self._apps_at = 0.0
        self.tidal = TidalWeb(ctl, self.launch)
        self.music = LocalMusicWeb(ctl, self.launch)
        self.menu = MenuWeb(ctl, self.launch)

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
        if self.menu_qr:
            self.ctl.phone_qr = self.phone_qr  # el menú de la TV muestra el QR de esta página
        return True

    def phone_qr(self) -> dict[str, Any] | None:
        """El QR para abrir esta página desde el celular (con la IP de la Pi en la red de la casa: los nombres .local
        no andan en todos los celulares). Se regenera solo si cambia la dirección. None = no se puede mostrar."""
        if self.server is None:
            return None
        url = f"http://{_lan_ip() or _hostname()}:{self.port}/"
        path = LOG_DIR / "celular-qr.svg"
        if url != self._qr_url or not path.is_file():
            if not shutil.which("qrencode"):
                if not self._qr_warned:
                    log.warning("Falta qrencode (sudo apt install qrencode): el menú no muestra el QR del celular")
                    self._qr_warned = True
                return None
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                subprocess.run(["qrencode", "-t", "SVG", "-m", "1", "-o", str(path), url],  # noqa: S603,S607
                               check=True, capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("No pude armar el QR del celular: %s", exc)
                return None
            self._qr_url = url
        return {"path": str(path), "url": url.removeprefix("http://").rstrip("/"),
                "title": "Desde el celular", "text": "Apuntá la cámara del celular a este código"}

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
            if path == "/api/menu":
                return _json(await self.menu.items())
            if path == "/api/folders":
                return _json(await self.menu.folders(query))
            if path == "/api/games":
                return _json(await self.menu.games())
            if path == "/api/radio":
                return _json(await self.menu.radio())
            if path == "/api/sites":
                return _json(await self.menu.sites())
            if path == "/api/favicon":  # el ícono de un sitio guardado (solo los ya bajados, por nombre de sitio)
                ico = activities_mod.navegador.favicons.path_for(query.get("h", ""))
                if ico is None:
                    return 404, b"", "text/plain", {}
                return 200, ico.read_bytes(), "image/png", {"Cache-Control": "max-age=86400"}
            if path == "/api/android":
                return _json(await self.menu.android())
            if path == "/api/tidal/library":
                return _json(await self.tidal.library(query))
            if path == "/api/tidal/list":
                return _json(await self.tidal.items(query))
            if path == "/api/tidal/search":
                return _json(await self.tidal.search(query))
            if path == "/api/tidal/queue":
                return _json(await self.tidal.queue())
            if path == "/api/music/library":
                return _json(await self.music.library(query))
            if path == "/api/music/list":
                return _json(await self.music.items(query))
            if path == "/api/music/search":
                return _json(await self.music.search(query))
            if path == "/api/music/queue":
                return _json(await self.music.queue())
            if path == "/api/music/art":
                art = await self.music.art(query)
                if art is None:
                    return 404, b"", "text/plain", {}
                return 200, art[0], art[1], {"Cache-Control": "max-age=86400"}
            if path in ("/", "/index.html"):
                return _static("index.html", no_cache=True)
            if path.startswith("/icons/"):
                return _icon(path[len("/icons/"):])
            name = path.lstrip("/")
            if name in STATIC_FILES:
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
            return _json(await self.media(str(data.get("command") or ""), str(data.get("player") or ""),
                                          data.get("value")))
        if path == "/api/app":
            return _json(await self.open_app(data))
        if path == "/api/close":
            return _json(await self.menu.close(str(data.get("id") or "")))
        if path == "/api/fotos/favorite":  # la foto que se ve en la TV: guardarla en Favoritas (o sacarla)
            return _json(await self.photo_favorite())
        if path == "/api/search":
            return _json(await self.search(str(data.get("app") or ""), str(data.get("text") or "")))
        if path == "/api/text":
            return _json(await self.type_text(str(data.get("text") or ""), str(data.get("key") or "")))
        if path == "/api/tidal/play":
            return _json(await self.tidal.play(data))
        if path == "/api/tidal/enqueue":
            return _json(await self.tidal.enqueue(data))
        if path == "/api/tidal/jump":
            return _json(await self.tidal.jump(data))
        if path == "/api/music/play":
            return _json(await self.music.play(data))
        if path == "/api/music/enqueue":
            return _json(await self.music.enqueue(data))
        if path == "/api/music/jump":
            return _json(await self.music.jump(data))
        if path == "/api/sleep":  # temporizador para apagar: {"choice": 30} (minutos), "track" o 0 (sacarlo)
            choice = data.get("choice")
            if not (choice == "track" or (isinstance(choice, int) and not isinstance(choice, bool)
                                          and 0 <= choice <= 600)):
                return _json({"ok": False, "error": "pedido inválido"})
            ok, msg = await self.ctl.sleep_timer.set(choice)
            self._state = None
            return _json({"ok": ok, "message": msg} if ok else {"ok": False, "error": msg})
        if path == "/api/client-log":
            # Lo que pasa en el celular con los controles de la pantalla bloqueada (para ver por qué no aparecen).
            msg = re.sub(r"[^\w\s:·.,;()/+%-]", "", str(data.get("msg") or ""))[:200].strip()
            if msg:
                log.info("Celular: %s", msg)
            return _json({"ok": True})
        if path == "/api/volume":
            level = data.get("level")  # la barra del celular: fijarlo en ese porcentaje
            change = str(data.get("change") or "")
            if isinstance(level, (int, float)) and not isinstance(level, bool):
                change = f"={max(0, min(100, int(level)))}"
            ok = await nowplaying.change_volume(change, self.volume_step)
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

    async def media(self, command: str, player: str, value: Any = None) -> dict[str, Any]:
        tracks = await nowplaying.players()
        track = next((t for t in tracks if t.player == player), None)
        if track is None:  # solo reproductores que existen ahora
            return {"ok": False, "error": "ese reproductor ya no está"}
        log.info("Celular: %s en %s", command, player)
        self.ctl.power.last_activity = time.monotonic()  # alguien está usando la música: no entrar en reposo
        self._state = None
        if command == "like":
            if not self._is_tidal(player):
                return {"ok": False, "error": "«Me gusta» es solo para Tidal"}
            return await self.tidal.like()
        if command == "loop":
            value = track.loop
        act = self._act_of(player)
        if command == "shuffle" and isinstance(act, activities_mod.MpvMediaActivity) and act.running():
            # Música: como el amarillo de la TV (mpv-mpris mezcla, pero al apagarlo no vuelve al orden)
            on = await act.toggle_shuffle()
            return {"ok": True, "message": "Mezclado" if on else "En orden"}
        ok = await nowplaying.control(player, command, value)
        return {"ok": ok} if ok else {"ok": False, "error": "Ese reproductor no lo permite"}

    def _is_tidal(self, player: str) -> bool:
        mpris = self._mpris("tidal")
        return mpris is not None and (player == mpris.player or player.startswith(mpris.player + "."))

    # ---------------------------------------------------------------- abrir apps
    def _output_kind(self) -> str:
        cur = next((o for o in self._outputs if o.default), None)
        return cur.kind if cur else ""

    async def open_app(self, data: dict[str, Any]) -> dict[str, Any]:
        """/api/app: {id} y, según la app, {folder} (Videos, Fotos), {rom} (Juegos), {serial} (Android) o
        {station} (Radio: el número de la emisora, desde 0)."""
        ident = str(data.get("id") or "")
        folder: str | None = None
        mode = str(data.get("mode") or "") or None
        if mode is not None and (mode not in ALL_MODES or not isinstance(self.ctl.activities.get(ident),
                                                                       activities_mod.PhotosActivity)):
            return {"ok": False, "error": "pedido inválido"}
        if data.get("folder"):
            path = self.menu.allowed_folder(ident, str(data["folder"]))
            if path is None:
                return {"ok": False, "error": "esa carpeta no está entre las configuradas"}
            folder = str(path)
            if data.get("restart") and isinstance(self.ctl.activities.get(ident), activities_mod.VideosActivity):
                await asyncio.to_thread(resume.forget, folder)  # Seguir viendo: «Desde el principio»
        elif data.get("rom"):
            if ident != "juegos" or not self.menu.allowed_rom(str(data["rom"])):
                return {"ok": False, "error": "ese juego no está"}
            folder = str(data["rom"])
        elif data.get("serial"):
            act = self.ctl.activities.get(ident)
            if not isinstance(act, activities_mod.AndroidActivity):
                return {"ok": False, "error": "pedido inválido"}
            serial = str(data["serial"])
            if not any(d.serial == serial and d.ready for d in await asyncio.to_thread(act.devices)):
                return {"ok": False, "error": "ese celular ya no está listo"}
            if act.running() and act.serial != serial:
                await self.menu.close(ident)  # otro celular: se cierra y se abre con el elegido
            act.want_serial = serial
        elif ident == "juegos" and isinstance(self.ctl.activities.get(ident), activities_mod.MameActivity):
            return {"ok": False, "error": "Elegí un juego"}  # sin uno por defecto (no el último que se jugó)
        elif data.get("url"):  # el Navegador: una dirección (un sitio de la lista)
            act = self.ctl.activities.get(ident)
            if ident != "navegador" or not isinstance(act, activities_mod.WebActivity):
                return {"ok": False, "error": "pedido inválido"}
            url = str(data["url"]).strip()
            if not activities_mod.navegador.allowed_url(url):
                return {"ok": False, "error": "esa dirección no es una página web"}
            if act.running():
                await act.navigate(url)
                self._state = None
                if self.ctl.current_app() == ident:
                    if self.ctl._menu_open:
                        await self.ctl.menu_close()
                    return {"ok": True, "message": f"{act.name}: {url.split('//', 1)[-1].split('/', 1)[0]}"}
                return await self.launch(ident)  # abierto de fondo: al frente
            folder = url
        elif ident == "radio" and isinstance(self.ctl.activities.get(ident), activities_mod.RadioActivity):
            act = self.ctl.activities[ident]
            if "station" in data:
                station = self.menu.allowed_station(data.get("station"))
                if station is None:
                    return {"ok": False, "error": "esa radio no está en la lista"}
                if act.running():  # ya suena (en pantalla o de fondo): se cambia la emisora
                    if await act.current() != station:
                        await act.play_station(station)
                    elif await act.playback() == "Paused":
                        await act.set_pause(False)
                    self._state = None
                    if self.ctl.current_app() == ident:
                        if self.ctl._menu_open:
                            await self.ctl.menu_close()
                        return {"ok": True, "message": f"Radio: {act.stations[station]['name']}"}
                else:
                    act.want_station = station
            elif not act.running() and not act.has_last() and len(act.stations) > 1:
                return {"ok": False, "error": "Elegí una radio"}  # la primera vez no hay una «última»
        return await self.launch(ident, folder, mode)

    async def launch(self, ident: str, folder: str | None = None, mode: str | None = None) -> dict[str, Any]:
        """Abrir una app desde el celular, como elegirla en el menú. Despierta a la Pi si está en reposo. Tidal con
        el sonido en un parlante Bluetooth no prende la TV ni le cambia la entrada (es solo música). Con folder:
        esa carpeta (Videos, Fotos), ese juego (Juegos) o esa dirección (YouTube)."""
        ctl = self.ctl
        act = ctl.activities.get(ident)
        if act is None:
            return {"ok": False, "error": "esa app no existe"}
        if not await asyncio.to_thread(act.installed):
            return {"ok": False, "error": f"{act.name} no está instalado"}
        if time.monotonic() - self._outputs_at > OUTPUTS_TTL_S and audio.available():
            await self._refresh_outputs()
        quiet = ident in ("tidal", "musica", "radio") and self._output_kind() == "bluetooth"  # solo sonido: sin prender la TV
        if ctl.power.asleep:
            log.info("Celular: despierto la Pi para abrir %s", act.name)
            ctl.power.wake(by_key=not quiet)
            task = getattr(ctl.power, "_wake_task", None)
            if task is not None:
                await asyncio.wait([task], timeout=30)
        else:
            ctl.power.note_key()  # cuenta como uso (cancela una cuenta regresiva de reposo)
        if ctl._switching:
            return {"ok": False, "error": "Esperá: está cambiando de app"}
        if ctl.current_app() == ident and folder is None:
            if ctl._menu_open:
                await ctl.menu_close()
            return {"ok": True, "message": f"{act.name} ya está abierto"}
        if ctl._menu_open:
            await ctl.menu_close()
        log.info("Celular: abro %s%s", act.name, " (sin prender la TV: el sonido va al Bluetooth)" if quiet else "")
        before = ctl._no_activate
        ctl._no_activate = before or quiet
        ok = False
        try:
            await ctl.overlay.busy(f"Abriendo {act.name}…")
            ok = await ctl.switch_to(ident, folder, mode) if mode else await ctl.switch_to(ident, folder)
        finally:
            ctl._no_activate = before
            await ctl.overlay.hide()
            self._state = None
            self._apps_at = 0.0
            self.menu.invalidate()
        if not ok:
            why = getattr(act, "fail_text", "") or f"No pude abrir {act.name}"
            await ctl.overlay.toast(why, 3000)
            return {"ok": False, "error": why}
        return {"ok": True}

    # ---------------------------------------------------------------- Fotos: favoritas
    async def photo_favorite(self) -> dict[str, Any]:
        act = self.ctl.activities.get("fotos")
        if not isinstance(act, activities_mod.PhotosActivity) or not act.running():
            return {"ok": False, "error": "Fotos no está abierto"}
        info = await act.photo_info()
        if not info:
            return {"ok": False, "error": "No sé qué foto se está viendo"}
        added = await asyncio.to_thread(act.toggle_favorite, info["path"])
        if added is None:
            return {"ok": False, "error": "No pude guardar la lista de favoritas"}
        await act.refresh_corner()
        n = len(act.favorites())
        log.info("Celular: %s %s", "guardo en Favoritas" if added else "saco de Favoritas", Path(info["path"]).name)
        return {"ok": True, "added": added, "count": n,
                "message": f"♥ Guardada en Favoritas ({n})" if added else f"Sacada de Favoritas ({n})"}

    # ---------------------------------------------------------------- escribir
    async def type_text(self, text: str, key: str) -> dict[str, Any]:
        """Escribir en la TV (buscar en Netflix o YouTube…): el texto y, si se pide, Enter o Borrar."""
        ctl = self.ctl
        text = "".join(ch for ch in text if ch.isprintable())[:200]
        if key not in ("", "Enter", "BackSpace"):
            return {"ok": False, "error": "tecla no permitida"}
        if not text and not key:
            return {"ok": False, "error": "no hay nada para escribir"}
        if ctl.power.asleep:
            return {"ok": False, "error": "La Pi está en reposo: tocá una tecla del control primero"}
        if ctl._menu_open:
            return {"ok": False, "error": "Cerrá el menú de la TV primero (Atrás)"}
        cur = ctl.current_app()
        ctl.power.note_key()
        if cur == "netflix" and ctl.page is not None:
            info = await ctl.js("ctx")
            where, url = str(info.get("ctx") or ""), str(info.get("url") or "")
            if where == "profiles":
                return {"ok": False, "error": "Elegí el perfil en la TV primero"}
            if text and (where != "text" or "/search" in url):
                # Sin un campo de texto enfocado (o con el del buscador, que tiene la búsqueda anterior): la
                # búsqueda de Netflix con ese texto. Un PIN u otro campo sí se escribe letra por letra.
                log.info("Celular: busco en Netflix «%s»", text)
                await ctl.navigate(NETFLIX_SEARCH + urllib.parse.quote(text.strip()))
                return {"ok": True, "message": f"Buscando «{text.strip()}» en Netflix"}
            for ch in text:
                await ctl.key(ch)
            if key:
                await ctl.key("Backspace" if key == "BackSpace" else "Enter")
        elif cur == "youtube" and text and hasattr(ctl.activities[cur], "search_url"):
            return await self._youtube_search(text)
        elif cur == "navegador" and isinstance(ctl.activities[cur], activities_mod.WebActivity):
            act = ctl.activities[cur]
            try:
                info = await act.js("ctx")
                if text and info.get("ctx") != "text":  # sin un campo con el foco: una dirección o una búsqueda
                    url = await act.go(text)
                    return {"ok": True, "message": f"Abriendo {url.split('//', 1)[-1][:60]}"}
                if text:
                    await act.js("type", text, False)
                if key == "Enter":
                    r = await act.js("submit")
                    if r.get("go"):
                        await act.go(r["go"])
                    elif r.get("enter"):
                        await act.key_press("Enter")
                elif key:
                    await act.key_press("Backspace")
            except CDPError as exc:
                return {"ok": False, "error": f"No pude escribir en el Navegador ({exc})"}
        elif cur in TEXT_APPS:
            if text and not await keyboard.wtype_text(text):
                return {"ok": False, "error": "No pude escribir en la TV"}
            if key and not await keyboard.wtype_key("Return" if key == "Enter" else "BackSpace"):
                return {"ok": False, "error": "No pude escribir en la TV"}
        else:
            name = ctl.activities[cur].name if cur else "Sin nada abierto, la TV"
            return {"ok": False, "error": f"{name} no recibe texto"}
        log.info("Celular: escribo %d letras en %s%s", len(text), cur, f" + {key}" if key else "")
        return {"ok": True}

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

    async def search(self, app: str, text: str) -> dict[str, Any]:
        """Buscar en Netflix o YouTube desde el celular; si la app está cerrada, la abre primero."""
        text = " ".join("".join(ch for ch in text if ch.isprintable()).split())[:120]
        if app not in ("netflix", "youtube", "navegador") or app not in self.ctl.activities:
            return {"ok": False, "error": "solo Netflix, YouTube y el Navegador"}
        if not text:
            return {"ok": False, "error": "escribí qué buscar"}
        ctl = self.ctl
        act = ctl.activities[app]
        if app == "navegador":
            url = activities_mod.navegador.target_url(text, act.search)
            if act.running():
                await act.navigate(url)
                if ctl.current_app() != app:
                    await self.launch(app)
                return {"ok": True, "message": f"Abriendo {url.split('//', 1)[-1][:60]}"}
            log.info("Celular: abro el Navegador en «%s»", text)
            r = await self.launch(app, url)
            return {**r, "message": f"Abriendo {url.split('//', 1)[-1][:60]}"} if r.get("ok") else r
        if app == "youtube":
            if act.running():
                return await self._youtube_search(text)
            log.info("Celular: abro YouTube en la búsqueda «%s»", text)
            r = await self.launch("youtube", act.search_url(text))
            return {**r, "message": f"Buscando «{text}» en YouTube"} if r.get("ok") else r
        if ctl.page is None:
            r = await self.launch("netflix")
            if not r.get("ok"):
                return r
            deadline = time.monotonic() + 30
            while ctl.page is None and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
            if ctl.page is None:
                return {"ok": False, "error": "Netflix no terminó de abrir: probá de nuevo"}
            await asyncio.sleep(2)  # que cargue la página (perfiles o inicio)
        return await self.type_text(text, "")

    async def _youtube_search(self, text: str) -> dict[str, Any]:
        """YouTube para TV: se reabre en su búsqueda con el texto puesto. Escribir con el teclado no sirve: la
        primera tecla abre la búsqueda y las siguientes se pierden, el campo conserva lo anterior, y Borrar con el
        campo vacío es Atrás."""
        ctl, act = self.ctl, self.ctl.activities["youtube"]
        log.info("Celular: busco en YouTube «%s»", text)
        ctl._switching = True
        ok = False
        try:
            await ctl.overlay.busy(f"Buscando «{text.strip()}» en YouTube…")
            await act.close()
            ok = await act.open(act.search_url(text))
        finally:
            ctl._switching = False
            ctl._switch_until = time.monotonic() + 5  # que no parezca que YouTube se cerró solo
            await ctl.overlay.hide()
        if not ok:
            return {"ok": False, "error": "No pude abrir la búsqueda de YouTube"}
        return {"ok": True, "message": f"Buscando «{text.strip()}» en YouTube"}

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
        act = self._act_of(player)
        return act.name if act is not None else base

    def _act_of(self, player: str) -> Any:
        """La actividad de un reproductor MPRIS (o None)."""
        found = []
        for ident, act in self.ctl.activities.items():
            mpris = self._mpris(ident)
            if mpris is not None and (player == mpris.player or player.startswith(mpris.player + ".")):
                found.append(act)
        # Videos y Música son los dos mpv: el que esté abierto (si están los dos, el de la pantalla).
        cur = self.ctl.activities.get(self.ctl.current_app() or "")
        return min(found, key=lambda a: (a is not cur, not a.running()), default=None)

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
            if not now["art"] and track.url.startswith("file://"):  # Música local: la tapa del disco
                now["art"] = await self.music.art_for(urllib.parse.unquote(urllib.parse.urlparse(track.url).path))
            if not now["art"]:  # la Radio: la tapa que se encontró en línea para lo que está sonando
                act = self._act_of(track.player)
                if isinstance(act, activities_mod.RadioActivity) and act.cover_path is not None:
                    self._art = str(act.cover_path)
                    now["art"] = f"/api/art?v={abs(hash(self._art)) % 10 ** 8}"
            now.pop("url", None)
            now["tidal"] = self._is_tidal(track.player)
            if now["tidal"]:
                now["liked"] = await self.tidal.liked()
        if time.monotonic() - self._outputs_at > OUTPUTS_TTL_S and audio.available():
            await self._refresh_outputs()
        if time.monotonic() - self._apps_at > APPS_TTL_S:
            self._apps = await asyncio.to_thread(self._app_list)
            self._apps_at = time.monotonic()
        for a in self._apps:
            a["current"] = a["id"] == cur
        return {
            "apps": self._apps,
            "tidal": self.tidal.available(),
            "tidal_open": bool(self.tidal.music is not None and self.tidal.music.running()),
            "text": cur in TEXT_APPS or cur == "navegador" or (cur == "netflix" and ctl.page is not None),
            "app": {"id": cur, "name": ctl.activities[cur].name if cur else ""},
            "menu": bool(ctl._menu_open),
            "asleep": bool(ctl.power.asleep),
            "sleep": ctl.sleep_timer.status(),  # temporizador para apagar (None = no hay)
            "now": now,
            "volume": {"level": vol, "muted": muted},
            "outputs": [{"id": _out_id(o), "name": o.name, "detail": _detail(o), "kind": o.kind,
                         "current": o.default} for o in self._outputs if o.paired],
            "ui": UI_VERSION,
        }

    async def _refresh_outputs(self) -> None:
        try:
            self._outputs = await asyncio.to_thread(audio.list_outputs)
        except Exception:  # noqa: BLE001
            log.exception("Página del celular: no pude leer las salidas de sonido")
        self._outputs_at = time.monotonic()

    def _app_list(self) -> list[dict[str, Any]]:
        """Las apps del menú que están instaladas, en el orden del menú."""
        order = list(self.ctl.cfg.menu.get("items") or []) if hasattr(self.ctl, "cfg") else []
        order += [i for i in self.ctl.activities if i not in order]
        out = []
        for ident in order:
            act = self.ctl.activities.get(ident)
            if act is None:
                continue
            try:
                if not act.installed():
                    continue
            except Exception:  # noqa: BLE001
                continue
            icon = str(getattr(act, "icon", "") or "")
            out.append({"id": ident, "name": act.name,
                        "icon": f"/icons/{icon}" if icon.endswith(".svg") and (ICONS / icon).is_file() else ""})
        return out

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
def _icon(name: str) -> tuple[int, bytes, str, dict[str, str]]:
    """Los íconos del menú (tele_cec/ui/icons/*.svg), solo por nombre: nada de rutas."""
    if not re.match(r"^[a-z0-9_-]{1,40}\.svg$", name):
        return 404, b"", "text/plain", {}
    path = ICONS / name
    if not path.is_file():
        return 404, b"", "text/plain", {}
    return 200, path.read_bytes(), "image/svg+xml", {"Cache-Control": "max-age=86400"}


def _detail(o: audio.Output) -> str:
    if o.kind == "bluetooth" and o.sink_id is None:
        return "Bluetooth · desconectado (tocá para conectarlo)"
    return o.detail


def _out_id(o: audio.Output) -> str:
    return o.mac or o.node_name or o.name


def _lan_ip() -> str:
    """La IP de la Pi en la red de la casa (la de la ruta por defecto). Un socket UDP «conectado» no manda nada."""
    import socket  # noqa: PLC0415
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.254.254.254", 1))
            ip = sock.getsockname()[0]
    except OSError:
        return ""
    return "" if ip.startswith("127.") or ip == "0.0.0.0" else ip  # noqa: S104


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
