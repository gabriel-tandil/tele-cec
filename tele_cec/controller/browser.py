"""Netflix en el navegador: la conexión (y su recuperación), las primitivas sobre la página (JS, puntero,
teclas), el indicador «trabajando» y la apertura del navegador."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from ..apps.netflix import bidi
from ..apps.netflix.cdp import CDPClosed, CDPError, ContextLost, SessionBusy
from ..keys import key_name

log = logging.getLogger("tele")

# Tope para los movimientos accesorios del puntero (sacarlo de encima de los títulos, despertar controles).
SOFT_MOVE_TIMEOUT_S = 3.0
# Eco de teclas (feedback.key_echo): cómo se muestra cada tecla.
ECHO = {
    "UP": "▲", "DOWN": "▼", "LEFT": "◀", "RIGHT": "▶", "SELECT": "OK", "ENTER": "OK",
    "EXIT": "Atrás", "AN_RETURN": "Atrás", "PLAY": "Play", "PAUSE": "Pausa", "PAUSE_PLAY_FUNCTION": "Play/Pausa",
    "STOP": "Stop", "FORWARD": "⏭", "BACKWARD": "⏮", "FAST_FORWARD": "⏩", "REWIND": "⏪",
    "F1_BLUE": "Azul", "F2_RED": "Rojo", "F3_GREEN": "Verde", "F4_YELLOW": "Amarillo",
}


class BrowserMixin:
    def _init_browser(self) -> None:
        self.page: Any = None  # cdp.CDP (Chromium) o bidi.BiDi (Firefox)
        self.connected = asyncio.Event()
        self._wiggle = 0
        self._was_connected = False
        fb = self.cfg.raw.get("feedback") or {}
        self._busy_delay = float(fb.get("busy_delay_ms", 250)) / 1000
        self._busy_on = bool(fb.get("busy", True))
        self._key_echo = bool(fb.get("key_echo", False))
        self._busy_text = ""
        self._busy_shown = False
        self._busy_handoff = False
        self._busy_persist_until = 0.0
        self._busy_timer: asyncio.Task | None = None
        self._launch_panel_on = False  # «Abriendo Netflix…» en el panel hasta conectarse al navegador

    @property
    def cdp(self) -> Any:
        """Nombre anterior de self.page (lo usan las pruebas)."""
        return self.page

    @property
    def bname(self) -> str:
        return self.browser.name

    async def pause_netflix(self) -> None:
        try:
            if await self.context() == "player":
                await self.js("pause")
                log.info("Reproducción de Netflix pausada")
        except CDPError:
            pass

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
        """Al detener el servicio: soltar las sesiones de control (Netflix y el Navegador); si no, Firefox las deja
        tomadas y el servicio siguiente no puede manejarlo."""
        web = self.activities.get("navegador") if hasattr(self, "activities") else None
        if web is not None and getattr(web, "page", None) is not None:
            try:
                await web._drop_page()
            except Exception:  # noqa: BLE001
                pass
        await self._disconnect_netflix()

    async def _disconnect_netflix(self) -> None:
        if self.page:
            page, self.page = self.page, None
            await page.close()

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
