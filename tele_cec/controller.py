"""Lógica principal: teclas CEC -> contexto -> acción/macro sobre Netflix en el navegador."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import activities as activities_mod
from . import bidi
from . import help as help_mod
from .browser import Browser
from .cdp import CDPClosed, CDPError, ContextLost, SessionBusy
from .cec import OP_STANDBY, CecFrame
from .config import PKG_DIR, Config
from .keys import key_label, key_name
from .music import BACK, COLLECTION, EXPLORE, HOME, LIKE, SHUFFLE, HighTide, wtype_key
from .overlay import Overlay
from .power import IdleShutdown

log = logging.getLogger("tele")

# Acciones que tiene sentido repetir si se mantiene apretado el botón.
REPEATABLE = {"nav", "seek", "volume", "key", "type", "music", "video", "fotos", "yt"}
# Una repetición (botón mantenido) que esperó más que esto en la cola ya no se ejecuta.
STALE_REPEAT_S = 0.35
# Tope para los movimientos accesorios del puntero (sacarlo de encima de los títulos, despertar controles).
SOFT_MOVE_TIMEOUT_S = 3.0
# Tope de la espera automática a que una fila termine de pasar de página (netflix.page_delay_ms = 0).
PAGE_WAIT_MAX_MS = 1200
# Acciones largas: se ejecutan en segundo plano y cualquier otra tecla las cancela.
LONG = {"continue_watching", "profiles", "profile", "home", "my_list", "launch", "macro", "wait", "reload", "app"}
# Acciones con argumento que también tardan (se reconocen por el texto completo).
LONG_FULL = {"music:my_tracks"}
PLAY_ACTIONS = {"play", "play_pause"}
# Indicador "trabajando": texto inicial por acción (las acciones largas lo van actualizando).
# Las que no figuran usan BUSY_DEFAULT; las de QUIET nunca lo muestran (ya se ve lo que hacen).
BUSY_TEXT = {
    "continue_watching": "Buscando «Seguir viendo»…",
    "profiles": "Abriendo los perfiles…",
    "profile": "Entrando al perfil…",
    "home": "Yendo al inicio…",
    "my_list": "Abriendo Mi lista…",
    "launch": "Abriendo Netflix…",
    "reload": "Recargando…",
    "play": "Reproduciendo…",
    "play_pause": "Reproduciendo…",
    "next_episode": "Siguiente episodio…",
    "smart_skip": "Un momento…",
}
BUSY_DEFAULT = "Un momento…"
# Eco de teclas (feedback.key_echo): cómo se muestra cada tecla.
ECHO = {
    "UP": "▲", "DOWN": "▼", "LEFT": "◀", "RIGHT": "▶", "SELECT": "OK", "ENTER": "OK",
    "EXIT": "Atrás", "AN_RETURN": "Atrás", "PLAY": "Play", "PAUSE": "Pausa", "PAUSE_PLAY_FUNCTION": "Play/Pausa",
    "STOP": "Stop", "FORWARD": "⏭", "BACKWARD": "⏮", "FAST_FORWARD": "⏩", "REWIND": "⏪",
    "F1_BLUE": "Azul", "F2_RED": "Rojo", "F3_GREEN": "Verde", "F4_YELLOW": "Amarillo",
}
QUIET = REPEATABLE | {"none", "toast", "wait", "nav_mode", "mute", "subtitles", "audio", "pause", "app", "menu", "help"}
# Acciones que no necesitan el navegador.
OFFLINE_OK = {"launch", "wait", "none", "macro", "app", "music", "menu", "video", "fotos", "toast", "help", "yt"}
# Ayuda al pie del menú.
MENU_HINT = "▲ ▼ elegir · OK abrir · ▶ otra carpeta (Videos, Fotos) · Atrás cerrar"
FOLDER_HINT = "▲ ▼ elegir · OK abrir · ◀ volver"
HELP_HINT = "◀ ▶ otra app · ▲ ▼ más filas · Atrás volver"
# Apps que pueden quedar sonando de fondo al abrir otra: con Fotos, la música de Tidal sigue.
KEEP_PLAYING = {"fotos": {"tidal"}}

# Con el control remoto activo Firefox pone navigator.webdriver = true (Chromium no); esto lo oculta.
HIDE_WEBDRIVER_JS = (
    "try { if (navigator.webdriver) Object.defineProperty(Navigator.prototype, 'webdriver', "
    "{ get: () => false, configurable: true, enumerable: true }); } catch (e) { /* nada */ }\n"
)


def loading_text(title: str) -> str:
    return f"Cargando «{title}»…" if title else "Cargando el video…"


def build_script(cfg: Config) -> tuple[str, str]:
    src = (PKG_DIR / "nfx.js").read_text(encoding="utf-8")
    fx = cfg.browser.get("firefox") or {}
    if str(cfg.browser.get("engine", "firefox")).lower() == "firefox" and fx.get("hide_webdriver", True):
        src = HIDE_WEBDRIVER_JS + src
    js_cfg = dict(cfg.selectors)
    js_cfg["trusted_click"] = bool(cfg.netflix.get("trusted_click", True))
    js_cfg["reduce_effects"] = bool(cfg.browser.get("reduce_effects", True))
    js_cfg["stop_previews"] = bool(cfg.browser.get("stop_previews", True))
    if js_cfg["reduce_effects"]:
        js_cfg["smooth_scroll"] = False
    cfg_json = json.dumps(js_cfg, ensure_ascii=False)
    version = hashlib.sha1((src + cfg_json).encode("utf-8")).hexdigest()[:12]
    script = src.replace("__NFX_VERSION__", version).replace("__NFX_CONFIG__", cfg_json)
    return script, version


class Controller:
    def __init__(self, cfg: Config, browser: Browser):
        self.cfg = cfg
        self.browser = browser
        self.source: Any = None  # CecClientSource / StdinSource (para "fuente activa")
        self.page: Any = None  # cdp.CDP (Chromium) o bidi.BiDi (Firefox)
        self.connected = asyncio.Event()
        self.queue: asyncio.Queue[tuple[int | None, float]] = asyncio.Queue(maxsize=8)
        self.script, self.version = build_script(cfg)
        self._ctx = "offline"
        self._last_key: tuple[int | None, float] = (None, 0.0)
        self._released = True
        self._smart_none_at = 0.0
        self._macro_task: asyncio.Task | None = None
        self._wiggle = 0
        self._was_connected = False
        fb = cfg.raw.get("feedback") or {}
        self._busy_delay = float(fb.get("busy_delay_ms", 250)) / 1000
        self._busy_on = bool(fb.get("busy", True))
        self._key_echo = bool(fb.get("key_echo", False))
        self._busy_text = ""
        self._busy_shown = False
        self._busy_handoff = False
        self._busy_persist_until = 0.0
        self._busy_timer: asyncio.Task | None = None
        # Tidal (High Tide): app aparte, sin navegador.
        self.music = HighTide(cfg.apps.get("tidal"))
        self._tidal_nav = False
        # Qué pantalla de High Tide dejó el último amarillo («my_tracks», «inicio» o "" si no se sabe), y en qué
        # proceso de High Tide (si se reinició, ya no vale).
        self._tidal_view = ""
        self._tidal_pids: tuple[int, ...] = ()
        # Actividades (Netflix, Tidal, Videos, Fotos, [activities.*]) y el menú para elegirlas.
        self.activities: dict[str, activities_mod.Activity] = activities_mod.build(self, cfg)
        self.overlay = Overlay()
        self._menu_open = False
        self._menu_items: list[dict] = []
        self._menu_sel = 0
        self._menu_timer: asyncio.Task | None = None
        self._menu_timeout = float(cfg.menu.get("timeout_s", 10) or 0)
        # Selector de carpetas (▶ sobre Videos o Fotos): qué actividad, en qué carpeta (None = las configuradas)
        # y por dónde se vino (para volver con ◀ a la carpeta de arriba y a la misma fila).
        self._menu_mode = "activities"
        self._menu_act_sel = 0
        self._picker_act = ""
        self._picker_path: Path | None = None
        self._picker_stack: list[tuple[Path | None, int]] = []
        # Ayuda (desde el menú o con la tecla Info/Ayuda): páginas por app y desde dónde se abrió.
        self._menu_from_ctx = "offline"
        self._help_pages: list[tuple[str, str]] = []
        self._help_idx = 0
        self._help_top = 0
        self._help_from_menu = False
        # La app que se eligió última (la que está en pantalla), aunque otra siga abierta de fondo.
        self._foreground: str | None = None
        # Apagado automático ([power]): sin sonido ni teclas durante un rato, la Pi se apaga sola.
        self.power = IdleShutdown(self, cfg.raw.get("power"))
        # Mientras se cambia de actividad las teclas se ignoran (cortar el cambio a mitad dejaría todo
        # cerrado), y con launch = "always" el navegador no se reabre solo.
        self._switching = False
        self._switch_until = 0.0
        self._launch_panel_on = False  # «Abriendo Netflix…» en el panel hasta conectarse al navegador
        self._no_activate = False      # al volver del reposo porque se prendió la TV: no cambiarle la entrada
        self._last_profile = ""        # perfil de Netflix elegido último (para volver a él después del reposo)

    @property
    def cdp(self) -> Any:
        """Nombre anterior de self.page (lo usan las pruebas)."""
        return self.page

    @property
    def bname(self) -> str:
        return self.browser.name

    # ================================================================ entrada
    def on_key(self, code: int) -> None:
        try:
            self.queue.put_nowait((code, time.monotonic()))
        except asyncio.QueueFull:
            log.debug("Cola de teclas llena: descarto %s", key_label(code))

    def on_release(self) -> None:
        """La TV avisó que se soltó el botón (0x45)."""
        try:
            self.queue.put_nowait((None, time.monotonic()))
        except asyncio.QueueFull:
            self._released = True

    def on_frame(self, frame: CecFrame) -> None:
        self.power.on_frame(frame)
        if frame.opcode == OP_STANDBY:
            log.info("La TV pasó a standby")
            if self.cfg.cec.get("pause_on_standby", True) and (self.page or self.current_app()):
                asyncio.get_running_loop().create_task(self._pause_if_playing())

    async def _pause_if_playing(self) -> None:
        if self.page:
            await self.pause_netflix()
            return
        for ident, act in self.activities.items():  # p. ej. Fotos con música de fondo: pausa las dos
            if ident != "netflix" and act.running():
                await act.pause()

    async def pause_netflix(self) -> None:
        try:
            if await self.context() == "player":
                await self.js("pause")
                log.info("Reproducción de Netflix pausada")
        except CDPError:
            pass

    # ================================================================ bucles
    async def run(self) -> None:
        # El panel del menú se abre (oculto) de entrada: así aparece al instante con la tecla Menú.
        warm = asyncio.create_task(self.overlay.warm())
        power = asyncio.create_task(self.power.run())
        try:
            await asyncio.gather(self.browser_loop(), self.key_loop())
        finally:
            warm.cancel()
            power.cancel()

    async def key_loop(self) -> None:
        while True:
            code, ts = await self.queue.get()
            if code is None:
                self._released = True
                continue
            age = time.monotonic() - ts
            if age > 3:
                log.debug("Descarto tecla vieja %s", key_label(code))
                continue
            # Repetición de un botón mantenido que quedó esperando porque la página venía lenta: si se
            # ejecutara, el foco seguiría moviéndose después de soltar el botón.
            if age > STALE_REPEAT_S and code == self._last_key[0] and not self._released:
                log.debug("Descarto repetición atrasada %s (%.0f ms en cola)", key_label(code), age * 1000)
                continue
            try:
                await self.handle_key(code, ts)
            except CDPClosed:
                log.warning("Se perdió %s mientras se ejecutaba la acción", self.bname)
            except Exception:  # noqa: BLE001
                log.exception("Error manejando la tecla %s", key_label(code))

    async def browser_loop(self) -> None:
        """Mantiene la conexión con el navegador y la recupera si se cierra o se cae."""
        port = self.browser.port
        launch_mode = self.cfg.browser.get("launch", "on_key")
        warned = False
        busy_since = 0.0
        while True:
            try:
                page = await self.browser.connect()
            except SessionBusy:
                # Firefox con la sesión de control tomada por un cliente que murió sin cerrarla:
                # no la libera nunca, así que la única salida es reiniciar ese Firefox.
                now = time.monotonic()
                busy_since = busy_since or now
                if bidi.late_session_pending():
                    # Es la nuestra: Firefox la está terminando de crear y se cierra apenas responda.
                    log.info("%s todavía está abriendo: espero a que libere la sesión de control", self.bname)
                elif launch_mode != "never" and self.browser.can_autolaunch():
                    log.warning("%s tiene la sesión de control tomada por otro cliente", self.bname)
                    await self.browser.restart()
                elif now - busy_since > 60:
                    log.warning("%s sigue con la sesión de control tomada: reinicialo (o el servicio lo hace "
                                "solo si browser.launch no es 'never')", self.bname)
                    busy_since = now
                await asyncio.sleep(2)
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("No pude conectarme a %s: %s", self.bname,
                            str(exc) or ("no respondió a tiempo" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__))
                page = None
                await asyncio.sleep(2)
                continue
            busy_since = 0.0
            if page is None:
                if not warned:
                    log.info("%s no está disponible en el puerto %d (%s)", self.bname, port,
                             {"on_key": "se abre desde el menú", "always": "lo abro", "never": "esperando"}[launch_mode])
                    warned = True
                if launch_mode == "always" and self.browser.can_autolaunch() and not self._holding_for_activity():
                    await self.launch_panel(True)
                    if not await self.browser.launch():
                        await self.launch_panel(False)
                await asyncio.sleep(2)
                continue
            warned = False
            try:
                navigated = await self._setup(page)
                self.page = page
                self.connected.set()
                if navigated:
                    asyncio.get_running_loop().create_task(self.busy_after_navigation("Cargando Netflix…"))
                self.browser.notify_connected()
                await self.launch_panel(False)
                log.info("Conectado a %s: %s", self.bname, page.url or "?")
                self._was_connected = True
                await page.closed.wait()
                log.warning("Se perdió la conexión con %s (¿se cerró la pestaña o el navegador?)", self.bname)
            except Exception as exc:  # noqa: BLE001
                log.warning("No pude preparar la pestaña de %s: %s", self.bname, exc)
            finally:
                self.page = None
                self.connected.clear()
                await page.close()
            await asyncio.sleep(1)

    async def _setup(self, page: Any) -> bool:
        """Instala el script en la pestaña. Devuelve True si además la mandó a Netflix (arranque)."""
        await page.setup_page(self.script)
        try:
            await page.evaluate(self.script)
        except CDPError as exc:
            log.debug("Inyección inicial falló (se reintenta luego): %s", exc)
        url = page.url or ""
        start = self.cfg.browser.get("start_url")
        pending, self.browser.pending_url = self.browser.pending_url, None
        if self.browser.is_blank(url):
            # El navegador arranca en una página negra vacía (ver Browser._first_page): ahora que el
            # script está instalado, recién se abre Netflix.
            await page.navigate(pending or start)
            return True
        if "netflix.com" not in url:
            log.info("La pestaña no es de Netflix (%s): abro %s", url or "vacía", start)
            await page.navigate(start)
            return True
        return False

    async def connect_once(self, timeout: float = 120.0) -> bool:
        """Para comandos sueltos (accion, diagnostico): conectarse sin bucle de recuperación.

        Con Firefox puede lanzar SessionBusy (el servicio u otro cliente tiene la única sesión).
        """
        page = await asyncio.wait_for(self.browser.connect(), timeout)
        if page is None:
            return False
        try:
            await self._setup(page)
        except BaseException:
            await page.close()
            raise
        self.page = page
        self.connected.set()
        return True

    async def disconnect(self) -> None:
        if self.page:
            page, self.page = self.page, None
            await page.close()

    # ================================================================ teclas
    async def context(self) -> str:
        if self._menu_open or self._switching:
            return "menu"
        if not self.page:
            cur = self.current_app()
            if cur == "tidal":
                return "tidal_nav" if self._tidal_nav else "tidal"
            self._tidal_nav = False
            return self.activities[cur].context if cur else "offline"
        try:
            r = await self.js("ctx")
            return r.get("ctx", "browse")
        except CDPClosed:
            return "offline"
        except CDPError as exc:
            log.debug("No pude leer el contexto: %s", exc)
            return "browse"

    async def handle_key(self, code: int, ts: float | None = None) -> None:
        if self.power.note_key():  # cualquier tecla: alguien usa la TV (cancela un reposo en curso)
            log.info("Tecla %s: vuelvo del reposo", key_label(code))
            return  # esa tecla solo despierta
        label = key_label(code)
        if ts is not None and time.monotonic() - ts > 0.15:
            log.debug("Tecla %s: %.0f ms en cola", label, (time.monotonic() - ts) * 1000)
        if self._key_echo and self.page:
            asyncio.get_running_loop().create_task(self._echo(code))
        if self._switching:
            log.info("Tecla %s ignorada: cambiando de actividad", label)
            return
        self._ctx = await self.context()
        found = self.cfg.action_for(self._ctx, code)
        if not found:
            log.info("Tecla %s: sin acción en el contexto '%s'", label, self._ctx)
            return
        action, where = found
        first = action[0] if isinstance(action, list) and action else action
        name = str(first).partition(":")[0]

        # Botón mantenido = 0x44 repetidos sin 0x45 en el medio. Las acciones "no repetibles"
        # (macros, perfiles, botón inteligente…) solo se ejecutan una vez por pulsación.
        # Si la TV no manda 0x45, se usa debounce_ms como respaldo.
        now = time.monotonic()
        last_code, last_t = self._last_key
        held = (last_code == code and not self._released
                and now - last_t < self.cfg.netflix.get("debounce_ms", 700) / 1000)
        self._last_key = (code, now)
        self._released = False
        repeatable = isinstance(action, str) and name in REPEATABLE
        if held and not repeatable:
            log.debug("Tecla %s mantenida: ignorada", label)
            return

        if self._macro_task and not self._macro_task.done():
            self._macro_task.cancel()
            log.info("Acción en curso cancelada por otra tecla")
        if name == "back" and self._busy_persist_until > time.monotonic():
            # Atrás mientras carga lo que se pidió antes: ese indicador ya no corresponde.
            await self.busy_clear()

        log.info("Tecla %s [%s] -> %s", label, self._ctx, action if isinstance(action, str) else " + ".join(map(str, action)))
        # En la lista, PLAY abre la ficha y espera su botón: también va en segundo plano.
        if (isinstance(action, list) or name in LONG or str(first).strip() in LONG_FULL
                or (name in PLAY_ACTIONS and self._ctx == "browse")):
            self._macro_task = asyncio.create_task(self._run_bg(action))
        else:
            await self.run_action(action)

    async def _run_bg(self, action: Any) -> None:
        try:
            await self.run_action(action)
        except asyncio.CancelledError:
            pass
        except CDPClosed:
            log.warning("Se perdió %s durante la acción %s", self.bname, action)
        except Exception:  # noqa: BLE001
            log.exception("Error en la acción %s", action)

    async def run_action(self, action: Any, depth: int = 0) -> None:
        if depth > 8:
            log.warning("Macros demasiado anidadas; corto en %s", action)
            return
        if depth == 0:
            name = str(action[0] if isinstance(action, list) and action else action).strip().partition(":")[0]
            text = "" if name in QUIET else BUSY_TEXT.get(name, BUSY_DEFAULT)
            if name in PLAY_ACTIONS and self._ctx != "browse":
                text = ""  # en el reproductor play/pausa es instantáneo
            start = time.monotonic()
            try:
                async with self.feedback(text):
                    await self.run_action(action, 1)
            finally:
                took = time.monotonic() - start
                (log.info if took >= 1 else log.debug)("%s: %.1f s", action if isinstance(action, str)
                                                       else " + ".join(map(str, action)), took)
            return
        if isinstance(action, list):
            for i, step in enumerate(action):
                if i:
                    self._ctx = await self.context()
                await self.run_action(step, depth + 1)
            return
        name, _, arg = str(action).strip().partition(":")
        handler = getattr(self, f"a_{name}", None)
        if handler is None:
            log.warning("Acción desconocida: %r", action)
            return
        if self.page is None and name not in OFFLINE_OK:
            log.info("%s no está conectado: no puedo ejecutar %s", self.bname, action)
            return
        if name == "macro":
            await self.a_macro(arg, depth)
        else:
            await handler(arg)

    # ================================================================ primitivas
    async def js(self, name: str, *args: Any, timeout: float = 8.0) -> dict:
        expr = (
            f"(window.__nfx && window.__nfx.v === {json.dumps(self.version)}) ? "
            f"window.__nfx.cmd({json.dumps(name)}, {json.dumps(list(args), ensure_ascii=False)}) : {{__missing: true}}"
        )
        for attempt in range(3):
            page = self.page
            if page is None:
                raise CDPClosed(f"sin conexión con {self.bname}")
            try:
                val = await page.evaluate(expr, timeout)
            except ContextLost:
                # Durante una navegación el contexto de JS desaparece un instante.
                if attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                raise
            if isinstance(val, dict) and val.get("__missing"):
                log.debug("Script no presente (página nueva): lo inyecto")
                try:
                    await page.evaluate(self.script)
                except CDPError as exc:
                    log.debug("Falló la inyección: %s", exc)
                    await asyncio.sleep(0.5)
                continue
            if isinstance(val, dict):
                if val.get("error"):
                    log.warning("JS %s: %s", name, str(val["error"]).splitlines()[0])
                return val
            return {}
        return {}

    # ---------------------------------------------------------------- indicador "trabajando"
    @contextlib.asynccontextmanager
    async def feedback(self, text: str):
        """Si la acción tarda más de feedback.busy_delay_ms, muestra el indicador hasta que termine."""
        self._busy_text, self._busy_shown, self._busy_handoff = text, False, False
        self._busy_timer = None
        if text and self._busy_on and self.page:
            self._busy_timer = asyncio.get_running_loop().create_task(self._busy_later())
        try:
            yield
        finally:
            if self._busy_timer:
                self._busy_timer.cancel()
            if self._busy_shown and not self._busy_handoff:
                await self.busy_clear()

    async def _busy_later(self) -> None:
        await asyncio.sleep(self._busy_delay)
        await self._busy_js(self._busy_text)

    async def _busy_js(self, text: str, until: str | None = None, ttl: float = 15, nav: bool = False) -> dict:
        if not self._busy_on or not self.page:
            return {}
        try:
            r = await self.js("busy", text, until, int(ttl * 1000), nav)
        except CDPError as exc:
            log.debug("No pude mostrar el indicador: %s", exc)
            return {}
        self._busy_shown = True
        return r

    async def busy(self, text: str, until: str | None = None, ttl: float = 15, nav: bool = False) -> None:
        """Actualiza el texto del indicador.

        Con until ("playing", "page", "profiles") el indicador pasa a la página: se muestra ya, sobrevive a
        la navegación y se apaga solo cuando se cumple la condición. nav=True: no mirar la condición hasta
        que cargue un documento nuevo (si no, la página vieja ya la cumple).
        """
        self._busy_text = text
        if until:
            # El indicador pasa a la página: que el temporizador del inicio no lo pise con uno sin condición.
            if self._busy_timer:
                self._busy_timer.cancel()
            self._busy_handoff = True
            self._busy_persist_until = time.monotonic() + ttl
            await self._busy_js(text, until, ttl, nav)
        elif self._busy_shown:
            await self._busy_js(text)

    async def busy_clear(self) -> None:
        self._busy_shown = False
        self._busy_persist_until = 0.0
        try:
            await self.js("done")
        except CDPError:
            pass

    async def busy_after_navigation(self, text: str, until: str = "page", ttl: float = 20) -> None:
        """Para navegaciones a otro origen (página negra del arranque -> Netflix): sessionStorage no pasa de un
        origen a otro, así que se espera a que cargue el documento nuevo y se pone ahí."""
        if not self._busy_on:
            return
        old = self.page.url if self.page else ""
        for _ in range(30):
            await asyncio.sleep(0.25)
            if not self.page:
                return
            try:
                r = await self.js("busy", text, until, int(ttl * 1000), False)
            except CDPError:
                continue
            url = r.get("url", "")
            if url and url != old and not url.startswith(("about:", "file:")):
                self._busy_persist_until = time.monotonic() + ttl
                return

    async def _echo(self, code: int) -> None:
        try:
            name = key_name(code)
            await self.js("echo", ECHO.get(name) or (name[6:] if name.startswith("NUMBER") else name))
        except CDPError:
            pass

    def _page(self) -> Any:
        if self.page is None:
            raise CDPClosed(f"sin conexión con {self.bname}")
        return self.page

    async def mouse_move(self, x: float, y: float) -> None:
        await self._page().mouse_move(x, y)

    async def _soft_move(self, x: float, y: float) -> None:
        """Movimiento accesorio: si la página está navegando, Firefox lo rechaza y no importa.

        Tampoco importa si tarda: con la página ocupada (por ejemplo, cargando el inicio en una Pi lenta)
        Firefox puede demorar segundos en aceptarlo, y eso no debe cortar la acción que lo pidió.
        """
        try:
            await asyncio.wait_for(self.mouse_move(x, y), SOFT_MOVE_TIMEOUT_S)
        except CDPClosed:
            raise
        except CDPError as exc:
            log.debug("Movimiento del puntero ignorado: %s", str(exc).splitlines()[0][:120])
        except asyncio.TimeoutError:
            log.debug("Movimiento del puntero ignorado: la página no respondió en %s s", SOFT_MOVE_TIMEOUT_S)

    async def click(self, x: float, y: float) -> None:
        await self._page().mouse_click(x, y)
        await asyncio.sleep(0.05)
        await self.park()

    async def park(self) -> None:
        """Saca el puntero de encima de los títulos (si no, Netflix abre la vista previa)."""
        await self._soft_move(1, 1)

    async def wiggle(self) -> None:
        """Mueve apenas el puntero para que el reproductor no oculte sus controles."""
        # Dos movimientos a puntos distintos: si el puntero ya estaba en el primero, el navegador no
        # genera "mousemove" para ese, pero el segundo siempre es un movimiento real.
        self._wiggle ^= 1
        await self._soft_move(2 + self._wiggle, 2)
        await self._soft_move(3 - self._wiggle, 3)

    async def wake_controls(self) -> None:
        """Muestra los controles del reproductor y espera a que estén en la página.

        Netflix los quita del DOM cuando se ocultan; tras mover el puntero tardan un instante en aparecer.
        """
        await self.wiggle()
        for _ in range(8):
            if (await self.js("controlsShown")).get("shown"):
                return
            await asyncio.sleep(0.08)

    async def apply_click(self, r: dict) -> bool:
        if "x" in r and "y" in r:
            await self.click(r["x"], r["y"])
            return True
        return bool(r.get("clicked"))

    async def key(self, name: str, times: int = 1) -> None:
        for _ in range(max(1, times)):
            await self._page().key_press(name)

    async def toast(self, text: str, ms: int = 1800) -> None:
        """Aviso breve: en la página de Netflix o, sin navegador (o con el menú abierto), en el panel."""
        if self.page and not self._menu_open:
            try:
                await self.js("toast", text, ms)
                return
            except CDPError:
                pass
        await self.overlay.toast(text, ms)

    async def navigate(self, url: str) -> None:
        log.info("Abriendo %s", url)
        await self._page().navigate(url)
        await asyncio.sleep(0.8)

    async def wait_ctx(self, wanted: set[str] | None = None, unwanted: set[str] | None = None,
                       timeout: float = 10.0) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            c = await self.context()
            if (wanted is None or c in wanted) and (unwanted is None or c not in unwanted):
                return c
            await asyncio.sleep(0.4)
        return None

    async def _fallback(self, r: dict) -> None:
        if r.get("fallback") == "key":
            await self.key(r["key"], int(r.get("times", 1)))

    # ================================================================ acciones
    async def a_none(self, arg: str) -> None:
        return None

    async def a_nav(self, arg: str) -> None:
        direction = arg.lower() or "down"
        if direction not in ("up", "down", "left", "right"):
            log.warning("nav:%s inválido (up/down/left/right)", arg)
            return
        if self._ctx == "player_menu":
            await self.wake_controls()
        r = await self.js("nav", direction)
        if r.get("paged"):
            delay = int(self.cfg.netflix.get("page_delay_ms", 0) or 0)
            if delay > 0:
                await asyncio.sleep(delay / 1000)
                r = await self.js("afterPage", direction)
            else:
                # La página espera sola a que la fila termine de moverse (sin animaciones, unos 50-100 ms).
                r = await self.js("afterPage", direction, PAGE_WAIT_MAX_MS)
                log.debug("Fila lista en %s ms", r.get("waited"))
        if r.get("label"):
            log.debug("Foco: %s", r["label"])

    async def a_ok(self, arg: str) -> None:
        if self._ctx == "player_menu":
            await self.wake_controls()
        r = await self.js("activate")
        if r.get("focused") is not None:
            return  # primera pulsación: solo muestra el resaltado
        await self.apply_click(r)
        if r.get("label"):
            log.info("Clic en: %s", r["label"])
            if self._ctx == "profiles":
                self._last_profile = str(r["label"]).strip()

    async def a_back(self, arg: str) -> None:
        if self._ctx in ("player", "player_menu"):
            # El botón "volver" del reproductor solo existe mientras se ven los controles; sin él,
            # back() recurre a history.back(), que puede llevar al episodio anterior.
            await self.wake_controls()
        r = await self.js("back")
        if r.get("key"):
            await self.key(r["key"])
        if r.get("restore"):
            # La página espera sola a que el modal termine de cerrarse (tope 800 ms).
            r = await self.js("restore", 800)
            log.debug("Modal cerrado en %s ms", r.get("waited"))

    async def a_smart_skip(self, arg: str) -> None:
        r = await self.js("smart")
        if not r.get("none"):
            await self.apply_click(r)
            log.info("Botón inteligente: %s", r.get("label", "?"))
            await self.toast(r.get("label") or "Listo", 1200)
            self._smart_none_at = 0.0
            return
        if r.get("player"):
            now = time.monotonic()
            if now - self._smart_none_at < self.cfg.netflix.get("smart_double_press_ms", 4000) / 1000:
                self._smart_none_at = 0.0
                await self.a_next_episode("")
            else:
                self._smart_none_at = now
                await self.toast("Nada para saltar · otra vez: siguiente episodio", 2500)
        else:
            await self.toast("Nada para saltar")

    async def a_next_episode(self, arg: str) -> None:
        r = await self.js("nextEpisode")
        if r.get("none"):
            await self.wiggle()  # mostrar los controles
            await asyncio.sleep(0.6)
            r = await self.js("nextEpisode")
        if r.get("none"):
            await self.toast("No encontré «Siguiente episodio»")
        else:
            log.info("Siguiente episodio")
            await self.toast("Siguiente episodio", 1200)

    async def a_play_pause(self, arg: str) -> None:
        if self._ctx == "browse":
            await self.play_selected()
            return
        await self._fallback(await self.js("playPause"))

    async def a_play(self, arg: str) -> None:
        if self._ctx == "browse":
            await self.play_selected()
            return
        await self._fallback(await self.js("play"))

    async def play_selected(self) -> bool:
        """En la lista o en una ficha: reproducir el título resaltado (o el de la ficha)."""
        r = await self.js("playSelected")
        if r.get("none"):
            await self.toast("No encontré «Reproducir» en la ficha" if r.get("modal")
                             else "Elegí un título con las flechas")
            return False
        title = r.get("title") or ""
        log.info("Reproducir: %s", title or r.get("label") or "?")
        if r.get("href"):
            await self.busy(loading_text(title), until="playing", ttl=20)
            await self.navigate(r["href"])
            return True
        if r.get("opened"):
            return await self.open_and_play(r)
        await self.busy(loading_text(title), until="playing", ttl=20)
        await self.apply_click(r)
        return True

    async def open_and_play(self, r: dict, timeout: float = 5.0) -> bool:
        """Abre la ficha de una tarjeta y aprieta su botón «Reproducir» apenas aparece."""
        title = r.get("title") or ""
        await self.busy(f"Abriendo «{title}»…" if title else "Abriendo la ficha…")
        await self.apply_click(r)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            pb = await self.js("playButton")
            if not pb.get("none"):
                await asyncio.sleep(0.2)  # que termine la animación de apertura
                pb = await self.js("playButton")
                if not pb.get("none"):
                    await self.busy(loading_text(title), until="playing", ttl=20)
                    await self.apply_click(pb)
                    return True
        log.warning("La ficha no mostró el botón «Reproducir»")
        await self.toast("No encontré «Reproducir» en la ficha")
        return False

    async def a_pause(self, arg: str) -> None:
        await self._fallback(await self.js("pause"))

    async def a_seek(self, arg: str) -> None:
        default = float(self.cfg.netflix.get("seek_seconds", 10))
        if arg in ("+", ""):
            sec = default
        elif arg == "-":
            sec = -default
        else:
            sec = float(arg)
        await self._fallback(await self.js("seek", sec))

    async def a_volume(self, arg: str) -> None:
        step = float(self.cfg.netflix.get("volume_step", 0.1))
        delta = step if arg in ("+", "") else -step if arg == "-" else float(arg)
        await self._fallback(await self.js("volume", delta))

    async def a_mute(self, arg: str) -> None:
        await self._fallback(await self.js("mute"))

    async def a_subtitles(self, arg: str) -> None:
        r = await self.js("subtitles")
        if r.get("name"):
            log.info("Subtítulos: %s", r["name"])

    async def a_audio(self, arg: str) -> None:
        r = await self.js("audio")
        if r.get("name"):
            log.info("Audio: %s", r["name"])

    async def a_fullscreen(self, arg: str) -> None:
        await self.key("f")

    async def a_nav_mode(self, arg: str) -> None:
        r = await self.js("navMode")
        if r.get("navMode"):
            await self.wiggle()

    async def a_type(self, arg: str) -> None:
        for ch in arg:
            await self.key(ch)

    async def a_key(self, arg: str) -> None:
        try:
            await self.key(arg)
        except ValueError as exc:
            log.warning("%s", exc)

    async def a_toast(self, arg: str) -> None:
        await self.toast(arg)

    async def a_wait(self, arg: str) -> None:
        try:
            await asyncio.sleep(max(0, int(arg or "500")) / 1000)
        except ValueError:
            log.warning("wait:%s inválido (milisegundos)", arg)

    async def a_reload(self, arg: str) -> None:
        await self.busy("Recargando…", until="page", nav=True)
        await self._page().reload()

    async def a_my_list(self, arg: str) -> None:
        await self.busy("Abriendo Mi lista…", until="page", nav=True)
        await self.navigate(self.cfg.netflix.get("my_list_url") or "https://www.netflix.com/browse/my-list")

    async def a_home(self, arg: str) -> None:
        await self.busy("Cargando el inicio…", until="page", nav=True)
        await self.navigate(self.cfg.browser.get("start_url"))

    async def a_macro(self, arg: str, depth: int = 0) -> None:
        macro = self.cfg.macros.get(arg)
        if macro is None:
            log.warning("No existe la macro %r (definila en [macros])", arg)
            return
        await self.run_action(macro if isinstance(macro, list) else [macro], depth + 1)

    async def a_launch(self, arg: str) -> None:
        """Abrir Netflix. Si hay otra actividad abierta, es lo mismo que elegir Netflix en el menú."""
        if not self._switching and any(a.running() for k, a in self.activities.items() if k != "netflix"):
            await self.switch_to("netflix")
            return
        await self.launch_browser()

    async def launch_browser(self) -> bool:
        if self.page:
            try:
                await self.page.bring_to_front()
            except CDPError:
                pass
            await self.toast("Netflix ya está abierto")
            return True
        if self.cfg.browser.get("launch") == "never":
            log.info("browser.launch = 'never': no abro %s", self.bname)
            return False
        if self.source and not self._no_activate and self.cfg.cec.get("activate_source") in ("launch", "both"):
            await self.source.activate_source()
        fb = self.cfg.raw.get("feedback") or {}
        if self.source and fb.get("cec_osd"):
            # Aviso en la pantalla de la TV (CEC "Set OSD String", máx. 13 caracteres ASCII): no todas lo muestran.
            text = str(fb.get("cec_osd_text") or "Netflix...").encode("ascii", "ignore").decode()[:13]
            await self.source.send(f"osd 0 {text}")
        await self.launch_panel(True)
        if not await self.browser.launch():
            await self.launch_panel(False)
            return False
        # En una Pi, Firefox abre el puerto enseguida pero recién acepta la sesión de control cuando
        # terminó de abrir la ventana: puede tardar más de medio minuto (ver bidi.py).
        try:
            await asyncio.wait_for(self.connected.wait(), 90 if self.browser.firefox else 30)
        except asyncio.TimeoutError:
            log.warning("%s abrió pero no logré conectarme todavía", self.bname)
            await self.launch_panel(False)
            return await self.browser.alive()  # está abierto: browser_loop se conecta solo
        # Esperar a que la página de Netflix responda. Se le pregunta a ella: durante un cambio desde el menú
        # context() responde «menu» y la espera se iría entera.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.page:
            try:
                if (await self.js("ctx")).get("ctx"):
                    break
            except CDPError:
                pass
            await asyncio.sleep(0.4)
        return True

    async def launch_panel(self, on: bool) -> None:
        """«Abriendo Netflix…» en el panel mientras arranca el navegador (que muestra una página negra).

        Si Netflix se eligió en el menú, el panel ya lo dice y lo oculta el menú al terminar el cambio.
        """
        if on and not self._switching and not self._launch_panel_on:
            self._launch_panel_on = await self.overlay.busy("Abriendo Netflix…")
        elif not on and self._launch_panel_on:
            self._launch_panel_on = False
            await self.overlay.hide()

    # ---------------------------------------------------------------- actividades
    def _holding_for_activity(self) -> bool:
        """Con otra actividad abierta (o mientras se cambia, o en reposo) el navegador no se reabre solo."""
        if time.monotonic() < self._switch_until or self.power.asleep:
            return True
        return any(a.running() for k, a in self.activities.items() if k != "netflix")

    def current_app(self) -> str | None:
        """La actividad en primer plano: Netflix si hay pestaña conectada; si no, la última elegida si sigue
        abierta (Fotos, aunque Tidal suene de fondo); si no, la primera abierta."""
        if self.page:
            return "netflix"
        fg = self._foreground
        if fg and fg != "netflix" and fg in self.activities and self.activities[fg].running():
            return fg
        for ident, act in self.activities.items():
            if ident != "netflix" and act.running():
                return ident
        return None

    async def a_app(self, arg: str) -> None:
        """app:<actividad>: la abre (cerrando la actual) o, si ya está abierta, hace su on_reselect."""
        want = arg.strip().lower()
        if want not in self.activities:
            log.warning("app:%s desconocida (válidas: %s)", arg, ", ".join(self.activities))
            return
        if self.current_app() == want:
            again = (self.cfg.apps.get(want) or {}).get("on_reselect")
            if again:
                log.info("%s ya está abierto: %s", self.activities[want].name, again)
                await self.run_action(again)
            return
        await self.switch_to(want)

    async def switch_to(self, want: str, folder: str | Path | None = None) -> bool:
        """Cierra la actividad abierta (pausando antes lo que suena) y abre `want`.

        Con `folder` (Videos o Fotos, elegida en el menú) la abre en esa carpeta; si ya estaba abierta, la reabre.
        """
        act = self.activities[want]
        if not await asyncio.to_thread(act.installed):
            log.error("%s no está instalado (ver README)", act.name)
            await self.toast(f"{act.name} no está instalado", 2500)
            return False
        if folder is None and want != "netflix" and act.running() and self.current_app() != want:
            # Ya estaba abierta (por ejemplo, de fondo o tras reiniciar el servicio): solo pasa a primer plano.
            log.info("%s ya estaba abierto: pasa a primer plano", act.name)
            self._foreground = want
            return True
        log.info("Cambio a %s", act.name)
        outer = self._switching  # el menú ya lo marcó (y lo desmarca él al ocultar el panel)
        self._switching = True
        self._switch_until = time.monotonic() + 120
        try:
            keep = KEEP_PLAYING.get(want, set())
            for ident, other in self.activities.items():
                if ident in keep and other.running():
                    log.info("%s sigue de fondo", other.name)
                    continue
                if (ident != want or folder is not None) and await other.active():
                    log.info("Cierro %s", other.name)
                    await other.close()
            self._tidal_nav = False
            if (want != "netflix" and self.source and not self._no_activate
                    and self.cfg.cec.get("activate_source") in ("launch", "both")):
                await self.source.activate_source()  # a_launch lo hace para Netflix
            if folder is not None and isinstance(act, activities_mod.MediaActivity):
                log.info("%s en %s", act.name, folder)
                ok = await act.open(Path(folder))
            else:
                ok = await act.open()
            if ok:
                self._foreground = want
        finally:
            self._switching = outer
            self._switch_until = 0.0 if want == "netflix" else time.monotonic() + 5
        if not ok:
            log.warning("No pude abrir %s", act.name)
        return ok

    async def open_tidal(self) -> None:
        """Nombre anterior: elegir Tidal."""
        await self.switch_to("tidal")

    # ---------------------------------------------------------------- menú de actividades
    def _menu_order(self) -> list[str]:
        items = [str(i).strip().lower() for i in (self.cfg.menu.get("items") or [])]
        out = [i for i in items if i in self.activities]
        for unknown in (i for i in items if i not in self.activities):
            log.warning("menu.items: no hay ninguna actividad «%s»", unknown)
        out += [k for k in self.activities if k not in out and k not in ("netflix", "youtube", "tidal", "videos", "fotos")]
        return out

    def _menu_entries(self) -> list[dict]:
        """Las opciones del menú (en un hilo aparte: mira qué está instalado y busca contenido en carpetas)."""
        cur = self.current_app()
        out = []
        for ident in self._menu_order():
            act = self.activities[ident]
            try:
                if not act.installed():
                    continue
                sub, enabled = act.source()
            except Exception:  # noqa: BLE001
                log.exception("Menú: no pude revisar %s", ident)
                continue
            item = {"id": ident, "name": act.name, "icon": act.icon, "sub": sub, "enabled": enabled,
                    "current": ident == cur}
            if ident != cur and ident in KEEP_PLAYING.get(cur or "", set()) and act.running():
                item["badge"] = "sonando"  # p. ej. Tidal de fondo mientras pasan las fotos
            out.append(item)
        out.append({"id": "_help", "name": "Ayuda", "icon": "ayuda.svg", "sub": "Qué hace cada tecla del control",
                    "enabled": True, "current": False})
        return out

    def _menu_restart_timer(self) -> None:
        if self._menu_timer:
            self._menu_timer.cancel()
        self._menu_timer = None
        if self._menu_timeout > 0:
            self._menu_timer = asyncio.get_running_loop().create_task(self._menu_autoclose())

    def _menu_wait(self) -> float:
        """Cuánto espera el menú sin teclas antes de cerrarse (la ayuda se lee con más calma)."""
        return max(self._menu_timeout, 60.0) if self._menu_mode == "help" else self._menu_timeout

    async def _menu_autoclose(self) -> None:
        wait = self._menu_wait()
        await asyncio.sleep(wait)
        if self._menu_open and not self._switching:
            log.info("Menú cerrado solo (sin teclas en %.0f s)", wait)
            await self.menu_close()

    async def menu_open(self, sel: int | None = None) -> None:
        items = await asyncio.to_thread(self._menu_entries)
        if not items:
            await self.toast("No hay actividades para elegir")
            return
        if sel is None:
            sel = next((i for i, it in enumerate(items) if it["current"]), 0)
        sel = max(0, min(sel, len(items) - 1))
        if self._menu_mode == "activities" and not self._menu_open and self._ctx != "menu":
            self._menu_from_ctx = self._ctx  # para que la ayuda empiece por la app que se estaba usando
        self._menu_mode = "activities"
        self._menu_items, self._menu_sel = items, sel
        if not await self.overlay.show(items, sel, hint=MENU_HINT):
            log.warning("No pude mostrar el menú: %s", self.overlay.error or "?")
            await self.toast("No pude mostrar el menú")
            return
        self._menu_open = True
        log.info("Menú: %s", " · ".join(("[" + it["name"] + "]") if i == sel else it["name"]
                                        for i, it in enumerate(items)))
        self._menu_restart_timer()

    async def menu_close(self) -> None:
        self._menu_open = False
        self._menu_mode = "activities"
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        await self.overlay.hide()

    async def _menu_move(self, delta: int) -> None:
        n = len(self._menu_items)
        if not n:
            return
        self._menu_sel = (self._menu_sel + delta) % n
        await self.overlay.select(self._menu_sel)
        log.debug("Menú: %s", self._menu_items[self._menu_sel]["name"])
        self._menu_restart_timer()

    async def _menu_choose(self, index: int) -> None:
        if not 0 <= index < len(self._menu_items):
            self._menu_restart_timer()
            return
        if self._menu_mode == "folders":
            await self._picker_choose(index)
            return
        if self._menu_mode == "help":
            self._menu_restart_timer()
            return
        item = self._menu_items[index]
        if item["id"] == "_help":
            self._menu_act_sel = index
            await self.help_open(from_menu=True)
            return
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        if not item["enabled"]:
            await self.overlay.toast(self.activities[item["id"]].unavailable_text(), 2500)
            self._menu_restart_timer()
            return
        if item["id"] == self.current_app():
            log.info("Menú: sigo en %s", item["name"])
            await self.menu_close()
            return
        log.info("Menú: elijo %s", item["name"])
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True  # desde ya: que ninguna tecla corte el cambio
        await self.overlay.busy(f"Abriendo {item['name']}…")
        self._macro_task = asyncio.create_task(self._menu_switch(item))

    async def _menu_switch(self, item: dict) -> None:
        ok = False
        try:
            ok = await self.switch_to(item["id"], item.get("folder"))
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Error cambiando a %s", item["name"])
        finally:
            self._switching = False
            self._menu_open = False
            await self.overlay.hide()
        if not ok:
            await self.overlay.toast(f"No pude abrir {item['name']}", 3000)

    # ---------------------------------------------------------------- reposo
    async def snapshot(self) -> dict[str, Any]:
        """Qué está abierto, para restaurarlo al volver del reposo."""
        cur = self.current_app()
        if not cur:
            return {}
        state: dict[str, Any] = {"app": cur}
        if cur == "netflix" and self.page:
            try:
                state["url"] = (await self.js("ctx")).get("url") or ""
            except CDPError:
                pass
            if self._last_profile:
                state["profile"] = self._last_profile
        act = self.activities.get(cur)
        if isinstance(act, activities_mod.MediaActivity) and act.current_folder:
            state["folder"] = str(act.current_folder)
        if isinstance(act, activities_mod.VideosActivity):
            state.update(await act.where())
        if cur in KEEP_PLAYING and self.music.running():
            state["music"] = True  # Tidal sonando de fondo con las fotos
        return state

    async def sleep_apps(self) -> None:
        """Al entrar en reposo: pausar lo que suena y cerrar todo (se restaura al volver)."""
        if self._menu_open:
            await self.menu_close()
        await self._pause_if_playing()
        for ident, act in self.activities.items():
            if await act.active():
                log.info("Reposo: cierro %s", act.name)
                await act.close()
        await self.overlay.hide()

    async def restore(self, state: dict[str, Any], activate: bool = False) -> None:
        """Al volver del reposo: abrir lo que había, en pausa (listo para seguir con ⏯).

        activate: anunciarse como fuente activa (la TV cambia a la Pi). Solo si se despertó con el control:
        si se prendió la TV para ver otra cosa, no se le cambia la entrada.
        """
        if activate and self.source:
            await self.source.activate_source()
        app = state.get("app")
        if not app or app not in self.activities:
            if activate:
                await self.menu_open()
            return
        name = self.activities[app].name
        self._switching = True
        self._no_activate = not activate
        await self.overlay.busy(f"Volviendo a {name}…")
        try:
            if app == "netflix":
                await self._restore_netflix(state.get("url") or "", state.get("profile") or "")
            elif app == "tidal":
                if await self.activities["tidal"].open(resume=False):
                    self._foreground = "tidal"
            elif app in ("videos", "fotos"):
                if state.get("music"):
                    await self.activities["tidal"].open(resume=False)
                ok = await self.switch_to(app, state.get("folder"))
                if ok and app == "videos":
                    await self._restore_video(state)
            else:
                await self.switch_to(app)
        except Exception:  # noqa: BLE001
            log.exception("No pude restaurar %s", name)
        finally:
            self._switching = False
            self._no_activate = False
            self.power.last_activity = time.monotonic()  # la cuenta empieza cuando terminó de volver
            await self.overlay.hide()
        await self.toast(f"{name} en pausa · ⏯ para seguir" if app in ("netflix", "videos") else f"Volviste a {name}",
                         3500)

    async def _page_ctx(self, timeout: float = 15.0, unwanted: str = "") -> str:
        """Contexto que informa la página (sin pasar por context(): durante la restauración dice «menu»)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.page:
            try:
                c = (await self.js("ctx")).get("ctx") or ""
            except CDPError:
                c = ""
            if c and c != unwanted:
                return c
            await asyncio.sleep(0.5)
        return ""

    async def _restore_profile(self, profile: str) -> None:
        """En «¿Quién está viendo?»: elegir el perfil que se estaba usando (o netflix.default_profile)."""
        names = [str(n).strip() for n in (await self.js("profiles")).get("names") or []]
        idx = next((i + 1 for i, n in enumerate(names) if profile and n.lower() == profile.lower()), 0)
        if not idx:
            idx = int(self.cfg.netflix.get("default_profile", 0) or 0)
        if not idx:
            log.info("Reposo: Netflix pide perfil y no sé cuál era: queda para elegir")
            return
        r = await self.js("profile", idx)
        if r.get("none"):
            return
        log.info("Reposo: vuelvo al perfil %s", r.get("name") or idx)
        await self.apply_click(r)
        await self._page_ctx(15, unwanted="profiles")

    async def _restore_netflix(self, url: str, profile: str = "") -> None:
        if not await self.switch_to("netflix"):
            return
        if await self._page_ctx(20) == "profiles":
            await self._restore_profile(profile)
        if url and "netflix.com" in url and urlparse(url).path.rstrip("/") not in ("", "/browse"):
            await self.navigate(url)
        if "/watch/" in url:
            # El reproductor arranca solo: se pausa apenas empieza (Netflix recuerda el minuto). Se le pregunta a
            # la página directamente: durante la restauración context() responde «menu».
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and self.page:
                try:
                    r = await self.js("ctx")
                except CDPError:
                    r = {}
                if r.get("ctx") == "player":
                    await asyncio.sleep(1.0)  # que el video arranque, si no la pausa no toma
                    await self.js("pause")
                    break
                await asyncio.sleep(0.5)

    async def _restore_video(self, state: dict[str, Any]) -> None:
        v = self.activities["videos"]
        assert isinstance(v, activities_mod.VideosActivity)
        await asyncio.sleep(2)  # que VLC termine de abrir y publique su interfaz de comandos
        if state.get("file"):
            await v.go_to(state["file"], int(state.get("seconds") or 0))
        await asyncio.sleep(0.5)
        await v.mpris.ctl("pause")

    # ---------------------------------------------------------------- ayuda
    async def help_open(self, from_menu: bool = False) -> None:
        installed = await asyncio.to_thread(
            lambda: {i for i, a in self.activities.items() if a.installed()})
        self._help_pages = help_mod.pages(self.cfg, installed)
        if not from_menu:
            self._menu_from_ctx = self._ctx if self._ctx != "menu" else self._menu_from_ctx
        start = help_mod.page_for(self._menu_from_ctx or "menu")
        self._help_idx = next((i for i, (ctx, _t) in enumerate(self._help_pages) if ctx == start), 0)
        self._help_top = 0
        self._help_from_menu = from_menu
        self._menu_mode = "help"
        self._menu_open = True
        await self._help_render()
        log.info("Ayuda: %s", self._help_pages[self._help_idx][1])

    async def _help_render(self) -> None:
        ctx, title = self._help_pages[self._help_idx]
        rows = help_mod.rows(self.cfg, ctx)
        self._help_top = max(0, min(self._help_top, max(0, len(rows) - 10)))
        page = f"{self._help_idx + 1} / {len(self._help_pages)}"
        await self.overlay.help(f"Ayuda · {title}", rows, self._help_top, page, HELP_HINT)
        self._menu_restart_timer()

    async def _help_key(self, sub: str) -> None:
        if sub in ("left", "right"):
            n = len(self._help_pages)
            self._help_idx = (self._help_idx + (1 if sub == "right" else -1)) % n
            self._help_top = 0
            await self._help_render()
        elif sub in ("up", "down"):
            self._help_top += 1 if sub == "down" else -1
            self._help_top = max(0, self._help_top)
            await self._help_render()
        elif sub in ("ok", "back", "", "next", "help", "close"):
            if self._help_from_menu and sub != "close":
                await self.menu_open(self._menu_act_sel)
            else:
                await self.menu_close()
        else:
            self._menu_restart_timer()

    async def a_help(self, arg: str) -> None:
        """Ayuda: qué hace cada tecla del control en cada app (empieza por la que se está usando)."""
        if self._switching:
            return
        await self.help_open(from_menu=False)

    # ---------------------------------------------------------------- selector de carpetas (Videos, Fotos)
    def _is_media(self, ident: str) -> bool:
        return isinstance(self.activities.get(ident), activities_mod.MediaActivity)

    def _picker_title(self, act: activities_mod.MediaActivity, path: Path) -> str:
        """«Fotos · En red: fotosRemotas › 2017 › 07» (las últimas partes, si es muy largo)."""
        for root in act.roots():
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            parts = list(rel.parts)
            if len(parts) > 3:
                parts = ["…"] + parts[-3:]
            return " › ".join([activities_mod.describe_folder(root), *parts])
        return str(path)

    def _picker_entries(self, ident: str, path: Path | None) -> tuple[str, list[dict]]:
        """Título y opciones del selector (en un hilo aparte: lista carpetas, que pueden estar en la red)."""
        act = self.activities[ident]
        assert isinstance(act, activities_mod.MediaActivity)
        items: list[dict] = []
        if path is None:
            default = act.folder()
            for root in act.roots():
                items.append({"kind": "dir", "path": str(root), "name": activities_mod.describe_folder(root),
                              "icon": "folder", "sub": "predeterminada" if root == default else "",
                              "enabled": True, "current": False})
            return f"{act.name} · elegir carpeta", items
        has = activities_mod.count_media(path, act.exts) > 0  # corta en el primero (y tiene tope de tiempo)
        items.append({"kind": "play", "path": str(path), "name": act.play_label, "icon": "media-playback-start",
                      "sub": "con sus subcarpetas" if has else act.empty_in_folder, "enabled": has, "current": False})
        for d in activities_mod.list_subfolders(path):
            items.append({"kind": "dir", "path": str(d), "name": d.name, "icon": "folder", "sub": "",
                          "enabled": True, "current": False})
        return f"{act.name} · {self._picker_title(act, path)}", items

    async def _picker_show(self, ident: str, path: Path | None, sel: int = 0) -> bool:
        title, items = await asyncio.to_thread(self._picker_entries, ident, path)
        if not items:
            await self.overlay.toast("No hay carpetas configuradas (ver [apps.videos] / [apps.fotos] folders)", 3000)
            self._menu_restart_timer()
            return False
        self._menu_mode, self._picker_act, self._picker_path = "folders", ident, path
        self._menu_items, self._menu_sel = items, max(0, min(sel, len(items) - 1))
        await self.overlay.show(items, self._menu_sel, title=title, hint=FOLDER_HINT, numbers=False)
        log.info("Menú: %s", title)
        self._menu_restart_timer()
        return True

    async def _picker_open(self, ident: str) -> None:
        self._menu_act_sel = self._menu_sel
        self._picker_stack = []
        await self._picker_show(ident, None)

    async def _picker_enter(self, index: int) -> None:
        item = self._menu_items[index]
        if item.get("kind") != "dir":
            self._menu_restart_timer()
            return
        self._picker_stack.append((self._picker_path, index))
        if not await self._picker_show(self._picker_act, Path(item["path"])):
            self._picker_stack.pop()

    async def _picker_back(self) -> None:
        if self._picker_stack:
            path, sel = self._picker_stack.pop()
            await self._picker_show(self._picker_act, path, sel)
        else:
            await self.menu_open(self._menu_act_sel)

    async def _picker_choose(self, index: int) -> None:
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        item = self._menu_items[index]
        if item.get("kind") == "dir":
            await self._picker_enter(index)
            return
        act = self.activities[self._picker_act]
        if not item.get("enabled", True):
            await self.overlay.toast(item.get("sub") or act.unavailable_text(), 2500)
            self._menu_restart_timer()
            return
        log.info("Menú: %s en %s", act.name, item["path"])
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True
        await self.overlay.busy(f"Abriendo {act.name}…")
        self._macro_task = asyncio.create_task(
            self._menu_switch({"id": self._picker_act, "name": act.name, "folder": item["path"]}))

    async def a_menu(self, arg: str) -> None:
        """menu (abrir; abierto = siguiente opción), menu:up|down|left|right|ok|back|close|open|pick:N.

        ▶ (right) sobre Videos o Fotos abre el selector de carpetas; ahí ◀ (left) o Atrás (back) sube.
        """
        sub, _, rest = arg.strip().lower().partition(":")
        if self._switching:
            return
        if self._menu_open and self._menu_mode == "help":
            await self._help_key(sub)
            return
        if sub == "help":
            self._menu_act_sel = self._menu_sel if self._menu_open else 0
            await self.help_open(from_menu=self._menu_open)
            return
        if sub in ("", "next", "toggle"):
            if self._menu_open:
                await self._menu_move(+1)
            else:
                await self.menu_open()
            return
        if sub == "open":
            if not self._menu_open:
                await self.menu_open()
            return
        if not self._menu_open:
            log.debug("menu:%s con el menú cerrado: lo abro", arg)
            await self.menu_open()
            return
        if sub in ("up", "prev", "previous"):
            await self._menu_move(-1)
        elif sub == "down":
            await self._menu_move(+1)
        elif sub == "close":
            await self.menu_close()
        elif sub == "right":
            if self._menu_mode == "folders":
                await self._picker_enter(self._menu_sel)
            elif self._is_media(self._menu_items[self._menu_sel]["id"]):
                await self._picker_open(self._menu_items[self._menu_sel]["id"])
            else:
                self._menu_restart_timer()
        elif sub == "left":
            if self._menu_mode == "folders":
                await self._picker_back()
            else:
                self._menu_restart_timer()
        elif sub == "back":
            if self._menu_mode == "folders":
                await self._picker_back()
            else:
                await self.menu_close()
        elif sub == "ok":
            await self._menu_choose(self._menu_sel)
        elif sub == "pick":
            try:
                await self._menu_choose(int(rest) - 1)
            except ValueError:
                log.warning("menu:pick:%s inválido (número de opción)", rest)
        else:
            log.warning("menu:%s desconocida (up, down, left, right, ok, back, close, pick:N)", arg)

    # ---------------------------------------------------------------- Videos (VLC) y Fotos (feh)
    async def a_video(self, arg: str) -> None:
        """Videos (VLC): video:play_pause, play, pause, next, previous, stop, seek:+10, volume:+, subtitles, audio,
        aspect (proporción de la imagen)."""
        v = self.activities.get("videos")
        if not isinstance(v, activities_mod.VideosActivity) or not v.running():
            log.info("Videos no está abierto: no ejecuto video:%s", arg)
            return
        cmd, _, rest = arg.strip().partition(":")
        mpris = {"play_pause": "play-pause", "play": "play", "pause": "pause", "next": "next",
                 "previous": "previous", "stop": "stop"}
        if cmd in mpris:
            await v.mpris.ctl(mpris[cmd])
        elif cmd == "volume":
            await v.mpris.volume(rest or "+")
        elif cmd == "seek":
            try:
                await v.mpris.seek(int(rest or 10))
            except ValueError:
                log.warning("video:seek:%s inválido (segundos, p. ej. -10)", rest)
        elif cmd in ("subtitles", "audio", "aspect"):
            kind, label = {"subtitles": ("strack", "Subtítulos"), "audio": ("atrack", "Audio"),
                           "aspect": ("vratio", "Proporción")}[cmd]
            name = await v.cycle(kind)
            log.info("Videos: %s %s", label.lower(), name.strip("\0") or "(sin opciones)")
            if name == v.PAUSED:
                await self.toast(f"Poné play para cambiar {label.lower()}", 1800)
            else:
                await self.toast(f"{label}: {name}" if name else f"{label}: este video no tiene opciones", 1800)
        else:
            log.warning("video:%s desconocida", arg)

    async def a_yt(self, arg: str) -> None:
        """YouTube para TV: yt:key:<Tecla> (flechas, Return, Escape: la interfaz las entiende de fábrica),
        yt:play_pause|play|pause (MPRIS), yt:seek:+10|-10."""
        y = self.activities.get("youtube")
        if not isinstance(y, activities_mod.YouTubeActivity) or not y.running():
            log.info("YouTube no está abierto: no ejecuto yt:%s", arg)
            return
        cmd, _, rest = arg.strip().partition(":")
        mpris = {"play_pause": "play-pause", "play": "play", "pause": "pause"}
        if cmd == "key":
            if rest:
                await wtype_key(rest)
        elif cmd in mpris:
            await y.mpris.ctl(mpris[cmd])
        elif cmd == "seek":
            try:
                await y.mpris.seek(int(rest or 10))
            except ValueError:
                log.warning("yt:seek:%s inválido (segundos, p. ej. -10)", rest)
        else:
            log.warning("yt:%s desconocida", arg)

    async def a_fotos(self, arg: str) -> None:
        """Fotos (feh): fotos:next, previous, pause, rotate, random, info, zoom_in, zoom_out, fit (sin zoom),
        scroll:left|right|up|down (mover la foto con zoom)."""
        f = self.activities.get("fotos")
        if not isinstance(f, activities_mod.PhotosActivity) or not f.running():
            log.info("Fotos no está abierto: no ejecuto fotos:%s", arg)
            return
        cmd, _, rest = arg.strip().lower().partition(":")
        # Atajos de feh: > girar, z foto al azar, d el nombre en letra chica de feh (filename).
        keys = {"rotate": "greater", "random": "z", "filename": "d"}
        if cmd == "next":
            await f.next()
            await f.photo_changed()
        elif cmd in ("previous", "prev"):
            await f.previous()
            await f.photo_changed()
        elif cmd == "pause":
            await f.toggle_pause()
        elif cmd in ("media_play_pause", "media_next", "media_previous"):
            if self.music.running():  # Tidal sonando de fondo: estas teclas son de la música
                mpris = {"media_play_pause": "play-pause", "media_next": "next", "media_previous": "previous"}[cmd]
                await self.music.ctl(mpris)
                await asyncio.sleep(0.4)
                st = await self.music.status()
                what = {"Playing": "♪", "Paused": "♪ en pausa"}.get(st.get("status", ""), "♪")
                title = " — ".join(x for x in (st.get("title"), st.get("artist")) if x)
                await self.toast(f"{what} {title}".strip(), 2500)
            elif cmd == "media_play_pause":
                await f.toggle_pause()
            else:
                await (f.next() if cmd == "media_next" else f.previous())
                await f.photo_changed()
        elif cmd == "info":
            info = await f.photo_info()
            if info:
                text = f.describe_photo(info)
                log.info("Fotos: %s", text.replace("\n", " · "))
                await self.toast(text, 6000)
            else:
                await f.key("d")  # sin el título de la ventana: el texto chico de feh
        elif cmd in keys:
            await f.key(keys[cmd])
            if cmd != "filename":
                await f.photo_changed()
        elif cmd == "zoom_in":
            entering = not f.zoom
            if await f.zoom_in() and entering:
                log.info("Fotos: zoom (flechas mueven la foto)")
                await self.toast("Zoom: flechas mueven · OK acerca · Atrás aleja", 2500)
        elif cmd == "zoom_out":
            await f.zoom_out()
        elif cmd in ("fit", "zoom_fit"):
            await f.fit()
        elif cmd == "scroll":
            if not await f.scroll(rest or "down"):
                log.debug("fotos:scroll:%s sin zoom o dirección inválida (left, right, up, down)", rest)
        else:
            log.warning("fotos:%s desconocida", arg)

    async def a_music(self, arg: str) -> None:
        """Controles de Tidal (High Tide): music:play_pause, next, previous, volume:+, seek:-10, like, shuffle,
        my_tracks (Mis canciones), nav (flechas para recorrer la interfaz), move:<Flecha> (mover el foco),
        page:inicio|explorar|coleccion, key:<Tecla>, back."""
        cmd, _, rest = arg.strip().partition(":")
        m = self.music
        if not m.running():
            log.info("Tidal no está abierto: no ejecuto music:%s", arg)
            return
        mpris = {"play_pause": "play-pause", "play": "play", "pause": "pause", "next": "next",
                 "previous": "previous", "stop": "stop"}
        # Lo que cambia la pantalla de High Tide: después de eso ya no se sabe si sigue en «Mis canciones».
        if cmd in ("back", "nav", "move", "page", "key"):
            self._tidal_view = ""
        if cmd in mpris:
            await m.ctl(mpris[cmd])
        elif cmd == "volume":
            await m.volume(rest or "+")
        elif cmd == "seek":
            try:
                await m.seek(int(rest or 10))
            except ValueError:
                log.warning("music:seek:%s inválido (segundos, p. ej. -10)", rest)
        elif cmd == "like":
            await m.press_button(LIKE)
        elif cmd == "shuffle":
            await m.press_button(SHUFFLE)
        elif cmd == "back":
            await m.press_button(BACK)
        elif cmd == "my_tracks":
            # Otra vez sin haberse movido de «Mis canciones»: a Inicio. Se lleva la cuenta acá en vez de
            # preguntarle a High Tide qué pantalla muestra: consultarlo por accesibilidad mientras cambia de
            # pantalla puede cerrarlo (error de GTK 4).
            pids = tuple(m.pids())
            if self._tidal_view == "my_tracks" and pids == self._tidal_pids:
                if await m.press_button(HOME):
                    self._tidal_view = "inicio"
                    log.info("Mis canciones otra vez: voy a Inicio")
                return
            ok = await m.my_tracks()
            self._tidal_view, self._tidal_pids = ("my_tracks" if ok else ""), pids
            log.info("Mis canciones: %s", "reproduciendo" if ok else "no pude abrirlas")
        elif cmd == "nav":
            self._tidal_nav = not self._tidal_nav
            log.info("Tidal: %s", "flechas recorren la interfaz" if self._tidal_nav else "flechas = controles")
            if not self._tidal_nav:
                await m.stop_nav()
        elif cmd == "move":
            await m.move(rest.capitalize() or "Down")
        elif cmd == "lyrics":
            await m.toggle_lyrics()
        elif cmd == "page":
            pages = {"inicio": HOME, "home": HOME, "explorar": EXPLORE, "explore": EXPLORE,
                     "coleccion": COLLECTION, "colección": COLLECTION, "collection": COLLECTION}
            names = pages.get(rest.lower())
            if names is None:
                log.warning("music:page:%s desconocida (inicio, explorar, coleccion)", rest)
            else:
                await m.press_button(names)
        elif cmd == "key":
            if rest:
                await m.key(rest)
        else:
            log.warning("music:%s desconocida", arg)

    async def a_profiles(self, arg: str) -> None:
        if await self.context() == "profiles":
            return
        await self.navigate(self.cfg.netflix.get("profiles_url"))
        if not await self.wait_ctx({"profiles"}, timeout=10):
            log.warning("No apareció la pantalla de perfiles (revisá netflix.profiles_url)")
            await self.toast("No encontré la pantalla de perfiles")

    async def a_profile(self, arg: str) -> None:
        try:
            n = int(arg)
        except ValueError:
            log.warning("profile:%s inválido (número de perfil)", arg)
            return
        if await self.context() != "profiles":
            await self.a_profiles("")
        r = await self.js("profile", n)
        if r.get("none"):
            log.warning("No hay perfil %d (encontré %s)", n, r.get("count", 0))
            return
        name = r.get("name") or ""
        await self.busy(f"Entrando como {name}…" if name else "Entrando al perfil…")
        await self.apply_click(r)
        log.info("Perfil elegido: %d (%s)", n, name or "?")
        if name:
            self._last_profile = name
        if await self.wait_ctx(unwanted={"profiles"}, timeout=10):
            await self.busy("Cargando Netflix…", until="page")

    async def a_continue_watching(self, arg: str) -> None:
        start_url = self.cfg.browser.get("start_url")
        default_profile = int(self.cfg.netflix.get("default_profile", 0) or 0)
        ctx = await self.context()
        if ctx == "profiles" and not default_profile:
            await self.toast("Elegí un perfil (números o flechas + OK)", 2500)
            return
        if ctx != "profiles" and (ctx in ("player", "player_menu")
                                  or urlparse((await self.js("ctx")).get("url", "")).path.rstrip("/") != "/browse"):
            await self.navigate(start_url)

        deadline = time.monotonic() + 20
        attempt = 0
        while time.monotonic() < deadline:
            ctx = await self.context()
            if ctx == "profiles":
                if not default_profile:
                    await self.toast("Elegí un perfil (números o flechas + OK)", 2500)
                    return
                r = await self.js("profile", default_profile)
                if not r.get("none"):
                    await self.busy(f"Entrando como {r.get('name')}…" if r.get("name") else "Entrando al perfil…")
                    await self.apply_click(r)
                    log.info("Perfil por defecto: %s", r.get("name", default_profile))
                    await self.wait_ctx(unwanted={"profiles"}, timeout=10)
                    continue
            r = await self.js("cw", attempt >= 4)
            if r.get("href"):
                title = r.get("title") or ""
                log.info("Seguir viendo: %s", title or r["href"])
                if self._busy_on:
                    await self.busy(loading_text(title), until="playing", ttl=20)
                else:
                    await self.toast(f"Seguir viendo: {title}" if title else "Seguir viendo…", 2000)
                    await asyncio.sleep(0.3)
                await self.navigate(r["href"])
                return
            if r.get("opened"):
                log.info("Seguir viendo: %s", r.get("title", "?"))
                if await self.open_and_play(r):
                    return
            attempt += 1
            await asyncio.sleep(0.8)
        log.warning("No encontré la fila «Seguir viendo»")
        await self.toast("No encontré «Seguir viendo»")
