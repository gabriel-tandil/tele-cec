"""Lógica principal: teclas CEC -> contexto -> acción/macro sobre Netflix en el navegador."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

from .browser import Browser
from .cdp import CDPClosed, CDPError, ContextLost, SessionBusy
from .cec import OP_STANDBY, CecFrame
from .config import PKG_DIR, Config
from .keys import key_label

log = logging.getLogger("netflix")

# Acciones que tiene sentido repetir si se mantiene apretado el botón.
REPEATABLE = {"nav", "seek", "volume", "key", "type"}
# Acciones largas: se ejecutan en segundo plano y cualquier otra tecla las cancela.
LONG = {"continue_watching", "profiles", "profile", "home", "launch", "macro", "wait", "reload"}
# Acciones que no necesitan el navegador.
OFFLINE_OK = {"launch", "wait", "none", "macro"}

# Con el control remoto activo Firefox pone navigator.webdriver = true (Chromium no); esto lo oculta.
HIDE_WEBDRIVER_JS = (
    "try { if (navigator.webdriver) Object.defineProperty(Navigator.prototype, 'webdriver', "
    "{ get: () => false, configurable: true, enumerable: true }); } catch (e) { /* nada */ }\n"
)


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
            if self.cfg.cec.get("pause_on_standby", True) and self.page:
                asyncio.get_running_loop().create_task(self._pause_if_playing())

    async def _pause_if_playing(self) -> None:
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
            if time.monotonic() - ts > 3:
                log.debug("Descarto tecla vieja %s", key_label(code))
                continue
            try:
                await self.handle_key(code)
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
                if launch_mode != "never" and self.browser.can_autolaunch():
                    log.warning("%s tiene la sesión de control tomada por otro cliente", self.bname)
                    await self.browser.restart()
                elif now - busy_since > 60:
                    log.warning("%s sigue con la sesión de control tomada: reinicialo (o el servicio lo hace "
                                "solo si browser.launch no es 'never')", self.bname)
                    busy_since = now
                await asyncio.sleep(2)
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("No pude conectarme a %s: %s", self.bname, exc)
                page = None
                await asyncio.sleep(2)
                continue
            busy_since = 0.0
            if page is None:
                if not warned:
                    log.info("%s no está disponible en el puerto %d (%s)", self.bname, port,
                             {"on_key": "se abre con OK", "always": "lo abro", "never": "esperando"}[launch_mode])
                    warned = True
                if launch_mode == "always" and self.browser.can_autolaunch():
                    await self.browser.launch()
                await asyncio.sleep(2)
                continue
            warned = False
            try:
                await self._setup(page)
                self.page = page
                self.connected.set()
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

    async def _setup(self, page: Any) -> None:
        await page.setup_page(self.script)
        try:
            await page.evaluate(self.script)
        except CDPError as exc:
            log.debug("Inyección inicial falló (se reintenta luego): %s", exc)
        url = page.url or ""
        start = self.cfg.browser.get("start_url")
        pending, self.browser.pending_url = self.browser.pending_url, None
        if self.browser.firefox and url in ("", "about:blank"):
            # Firefox arranca en blanco (ver Browser.hide_webdriver): ahora que el script está
            # instalado, recién se abre Netflix.
            await page.navigate(pending or start)
        elif "netflix.com" not in url and url not in ("", "about:blank"):
            log.info("La pestaña no es de Netflix (%s): abro %s", url or "vacía", start)
            await page.navigate(start)

    async def connect_once(self, timeout: float = 10.0) -> bool:
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
            return "offline"
        try:
            r = await self.js("ctx")
            return r.get("ctx", "browse")
        except CDPClosed:
            return "offline"
        except CDPError as exc:
            log.debug("No pude leer el contexto: %s", exc)
            return "browse"

    async def handle_key(self, code: int) -> None:
        label = key_label(code)
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

        log.info("Tecla %s [%s] -> %s", label, self._ctx, action if isinstance(action, str) else " + ".join(map(str, action)))
        if isinstance(action, list) or name in LONG:
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

    def _page(self) -> Any:
        if self.page is None:
            raise CDPClosed(f"sin conexión con {self.bname}")
        return self.page

    async def mouse_move(self, x: float, y: float) -> None:
        await self._page().mouse_move(x, y)

    async def _soft_move(self, x: float, y: float) -> None:
        """Movimiento accesorio: si la página está navegando, Firefox lo rechaza y no importa."""
        try:
            await self.mouse_move(x, y)
        except CDPClosed:
            raise
        except CDPError as exc:
            log.debug("Movimiento del puntero ignorado: %s", str(exc).splitlines()[0][:120])

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
            if delay <= 0:
                delay = 250 if self.cfg.browser.get("reduce_effects", True) else 700
            await asyncio.sleep(delay / 1000)
            r = await self.js("afterPage", direction)
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
            await asyncio.sleep(0.35)  # que el modal termine de cerrarse
            await self.js("restore")

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
        await self._fallback(await self.js("playPause"))

    async def a_play(self, arg: str) -> None:
        await self._fallback(await self.js("play"))

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
        await self._page().reload()

    async def a_home(self, arg: str) -> None:
        await self.navigate(self.cfg.browser.get("start_url"))

    async def a_macro(self, arg: str, depth: int = 0) -> None:
        macro = self.cfg.macros.get(arg)
        if macro is None:
            log.warning("No existe la macro %r (definila en [macros])", arg)
            return
        await self.run_action(macro if isinstance(macro, list) else [macro], depth + 1)

    async def a_launch(self, arg: str) -> None:
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
        if not await self.browser.launch():
            return
        try:
            await asyncio.wait_for(self.connected.wait(), 20)
        except asyncio.TimeoutError:
            log.warning("%s abrió pero no logré conectarme todavía", self.bname)
            return
        await self.wait_ctx(unwanted={"offline"}, timeout=10)

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
        await self.apply_click(r)
        log.info("Perfil elegido: %d (%s)", n, r.get("name", "?"))
        await self.wait_ctx(unwanted={"profiles"}, timeout=10)

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
                    await self.apply_click(r)
                    log.info("Perfil por defecto: %s", r.get("name", default_profile))
                    await self.wait_ctx(unwanted={"profiles"}, timeout=10)
                    continue
            r = await self.js("cw", attempt >= 4)
            if r.get("href"):
                title = r.get("title") or ""
                log.info("Seguir viendo: %s", title or r["href"])
                await self.toast(f"Seguir viendo: {title}" if title else "Seguir viendo…", 2000)
                await asyncio.sleep(0.3)
                await self.navigate(r["href"])
                return
            if r.get("opened"):
                await self.apply_click(r)
                await asyncio.sleep(1.5)
                pb = await self.js("playButton")
                if not pb.get("none"):
                    await self.apply_click(pb)
                    log.info("Seguir viendo: %s", r.get("title", "?"))
                    return
            attempt += 1
            await asyncio.sleep(0.8)
        log.warning("No encontré la fila «Seguir viendo»")
        await self.toast("No encontré «Seguir viendo»")
