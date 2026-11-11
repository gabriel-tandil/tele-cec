"""Lógica principal: teclas CEC -> contexto -> acción/macro sobre Netflix en el navegador."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

from . import bidi
from .browser import Browser
from .cdp import CDPClosed, CDPError, ContextLost, SessionBusy
from .cec import OP_STANDBY, CecFrame
from .config import PKG_DIR, Config
from .keys import key_label, key_name
from .music import BACK, LIKE, SHUFFLE, HighTide

log = logging.getLogger("netflix")

# Acciones que tiene sentido repetir si se mantiene apretado el botón.
REPEATABLE = {"nav", "seek", "volume", "key", "type", "music"}
# Una repetición (botón mantenido) que esperó más que esto en la cola ya no se ejecuta.
STALE_REPEAT_S = 0.35
# Tope para los movimientos accesorios del puntero (sacarlo de encima de los títulos, despertar controles).
SOFT_MOVE_TIMEOUT_S = 3.0
# Tope de la espera automática a que una fila termine de pasar de página (netflix.page_delay_ms = 0).
PAGE_WAIT_MAX_MS = 1200
# Acciones largas: se ejecutan en segundo plano y cualquier otra tecla las cancela.
LONG = {"continue_watching", "profiles", "profile", "home", "launch", "macro", "wait", "reload", "app"}
# Acciones con argumento que también tardan (se reconocen por el texto completo).
LONG_FULL = {"music:my_tracks"}
# Apps que se eligen con los colores (acción app:<nombre>).
APPS = {"netflix": "Netflix", "tidal": "Tidal"}
PLAY_ACTIONS = {"play", "play_pause"}
# Indicador "trabajando": texto inicial por acción (las acciones largas lo van actualizando).
# Las que no figuran usan BUSY_DEFAULT; las de QUIET nunca lo muestran (ya se ve lo que hacen).
BUSY_TEXT = {
    "continue_watching": "Buscando «Seguir viendo»…",
    "profiles": "Abriendo los perfiles…",
    "profile": "Entrando al perfil…",
    "home": "Yendo al inicio…",
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
QUIET = REPEATABLE | {"none", "toast", "wait", "nav_mode", "mute", "subtitles", "audio", "pause", "app"}
# Acciones que no necesitan el navegador.
OFFLINE_OK = {"launch", "wait", "none", "macro", "app", "music"}

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
    if str(cfg.browser.get("engine", "chromium")).lower() == "firefox" and fx.get("hide_webdriver", True):
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
        # Tidal (High Tide): app aparte, sin navegador. Mientras se pasa a Tidal el navegador no se reabre solo.
        self.music = HighTide(cfg.apps.get("tidal"))
        self._tidal_nav = False
        self._tidal_until = 0.0

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
        if frame.opcode == OP_STANDBY:
            log.info("La TV pasó a standby")
            if self.cfg.cec.get("pause_on_standby", True) and (self.page or self.music.running()):
                asyncio.get_running_loop().create_task(self._pause_if_playing())

    async def _pause_if_playing(self) -> None:
        if not self.page and self.music.running():
            if (await self.music.status()).get("status") == "Playing":
                await self.music.ctl("pause")
                log.info("Música pausada por standby de la TV")
            return
        try:
            if await self.context() == "player":
                await self.js("pause")
                log.info("Reproducción pausada por standby de la TV")
        except CDPError:
            pass

    # ================================================================ bucles
    async def run(self) -> None:
        await asyncio.gather(self.browser_loop(), self.key_loop())

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
                             {"on_key": "se abre con OK", "always": "lo abro", "never": "esperando"}[launch_mode])
                    warned = True
                if launch_mode == "always" and self.browser.can_autolaunch() and not self._holding_for_tidal():
                    await self.browser.launch()
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
        if self.browser.is_splash(url) or (self.browser.firefox and url in ("", "about:blank")):
            # El navegador arranca en la pantalla de carga (o en blanco, ver Browser.hide_webdriver):
            # ahora que el script está instalado, recién se abre Netflix.
            await page.navigate(pending or start)
            return True
        if "netflix.com" not in url and url not in ("", "about:blank"):
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
        if not self.page:
            if self.music.running():
                return "tidal_nav" if self._tidal_nav else "tidal"
            self._tidal_nav = False
            return "offline"
        try:
            r = await self.js("ctx")
            return r.get("ctx", "browse")
        except CDPClosed:
            return "offline"
        except CDPError as exc:
            log.debug("No pude leer el contexto: %s", exc)
            return "browse"

    async def handle_key(self, code: int, ts: float | None = None) -> None:
        label = key_label(code)
        if ts is not None and time.monotonic() - ts > 0.15:
            log.debug("Tecla %s: %.0f ms en cola", label, (time.monotonic() - ts) * 1000)
        if self._key_echo and self.page:
            asyncio.get_running_loop().create_task(self._echo(code))
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
        """Para navegaciones a otro origen (pantalla de carga -> Netflix): sessionStorage no pasa de un
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
        try:
            await self.js("toast", text, ms)
        except CDPError:
            pass

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
        self._tidal_until = 0.0
        if self.music.running():
            await self.music.quit()
        if self.page:
            try:
                await self.page.bring_to_front()
            except CDPError:
                pass
            await self.toast("Netflix ya está abierto")
            return
        if self.cfg.browser.get("launch") == "never":
            log.info("browser.launch = 'never': no abro %s", self.bname)
            return
        if self.source and self.cfg.cec.get("activate_source") in ("launch", "both"):
            await self.source.activate_source()
        fb = self.cfg.raw.get("feedback") or {}
        if self.source and fb.get("cec_osd"):
            # Aviso en la pantalla de la TV (CEC "Set OSD String", máx. 13 caracteres ASCII): no todas lo muestran.
            text = str(fb.get("cec_osd_text") or "Netflix...").encode("ascii", "ignore").decode()[:13]
            await self.source.send(f"osd 0 {text}")
        if not await self.browser.launch():
            return
        try:
            await asyncio.wait_for(self.connected.wait(), 20)
        except asyncio.TimeoutError:
            log.warning("%s abrió pero no logré conectarme todavía", self.bname)
            return
        await self.wait_ctx(unwanted={"offline"}, timeout=10)

    # ---------------------------------------------------------------- apps (colores) y Tidal
    def _holding_for_tidal(self) -> bool:
        """Mientras se pasa a Tidal (o Tidal está abierto) el navegador no se reabre solo (launch = always)."""
        return self.music.running() or time.monotonic() < self._tidal_until

    def current_app(self) -> str | None:
        if self.page:
            return "netflix"
        return "tidal" if self.music.running() else None

    async def a_app(self, arg: str) -> None:
        """Color de una app: si es la que está abierta hace su función propia; si no, cierra la otra y la abre."""
        want = arg.strip().lower()
        if want not in APPS:
            log.warning("app:%s desconocida (válidas: %s)", arg, ", ".join(APPS))
            return
        if self.current_app() == want:
            again = (self.cfg.apps.get(want) or {}).get("on_reselect")
            if again:
                log.info("%s ya está abierto: %s", APPS[want], again)
                await self.run_action(again)
            return
        log.info("Cambio a %s", APPS[want])
        if want == "netflix":
            await self.run_action("launch")
        else:
            await self.open_tidal()

    async def open_tidal(self) -> None:
        if not self.music.installed():
            log.error("Tidal: High Tide no está instalado (ver README → Tidal)")
            await self.toast("Tidal no está instalado")
            return
        self._tidal_until = time.monotonic() + 120
        if self.page or await self.browser.alive():
            if self.page:
                await self.busy("Abriendo Tidal…", ttl=30)
                await self._pause_if_playing()
            await self.browser.close()
        if self.source and self.cfg.cec.get("activate_source") in ("launch", "both"):
            await self.source.activate_source()
        if not await self.music.launch():
            return
        if self.music.resume_on_open and not await self.music.resume():
            # High Tide no retoma "Mis canciones" por sí solo (solo guarda el número de tema).
            start = self.music.saved_index()
            log.info("Retomo «Mis canciones» desde el tema %d", start + 1)
            await self.music.my_tracks(start=start)

    async def a_music(self, arg: str) -> None:
        """Controles de Tidal (High Tide): music:play_pause, next, previous, volume:+, seek:-10, like, shuffle,
        my_tracks (Mis canciones), nav (flechas para recorrer la interfaz), key:<Tecla>, back."""
        cmd, _, rest = arg.strip().partition(":")
        m = self.music
        if not m.running():
            log.info("Tidal no está abierto: no ejecuto music:%s", arg)
            return
        mpris = {"play_pause": "play-pause", "play": "play", "pause": "pause", "next": "next",
                 "previous": "previous", "stop": "stop"}
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
            ok = await m.my_tracks()
            log.info("Mis canciones: %s", "reproduciendo" if ok else "no pude abrirlas")
        elif cmd == "nav":
            self._tidal_nav = not self._tidal_nav
            log.info("Tidal: %s", "flechas recorren la interfaz" if self._tidal_nav else "flechas = controles")
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
