"""El corazón del controller: la cola de teclas, el contexto actual y la ejecución de acciones y macros."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..apps.netflix.cdp import CDPClosed, CDPError
from ..system.cec import OP_STANDBY, CecFrame
from ..keys import key_label

log = logging.getLogger("tele")

# Acciones que tiene sentido repetir si se mantiene apretado el botón.
REPEATABLE = {"nav", "seek", "volume", "key", "type", "music", "video", "musica", "radio", "fotos", "yt", "android"}
# Una repetición (botón mantenido) que esperó más que esto en la cola ya no se ejecuta.
STALE_REPEAT_S = 0.35
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
QUIET = REPEATABLE | {"none", "toast", "wait", "nav_mode", "mute", "subtitles", "audio", "pause", "app", "menu", "help"}
# Acciones que no necesitan el navegador.
OFFLINE_OK = {"launch", "wait", "none", "macro", "app", "music", "menu", "video", "musica", "radio", "fotos", "toast", "help", "yt", "android", "juegos"}


class CoreMixin:
    """Entrada de teclas, contexto y acciones. Los handlers a_<acción> están repartidos en los otros módulos."""

    def _init_core(self) -> None:
        self.queue: asyncio.Queue[tuple[int | None, float]] = asyncio.Queue(maxsize=8)
        self._ctx = "offline"
        self._last_key: tuple[int | None, float] = (None, 0.0)
        self._released = True
        self._macro_task: asyncio.Task | None = None
        from .sleeptimer import SleepTimer  # noqa: PLC0415
        self.sleep_timer = SleepTimer(self)  # temporizador para apagar (Sonido en el menú, o el celular)

    # ================================================================ entrada
    def on_key(self, code: int) -> None:
        try:
            self.queue.put_nowait((code, time.monotonic()))
        except asyncio.QueueFull:
            log.debug("Cola de teclas llena: descarto %s", key_label(code))

    def on_gamepad_key(self, code: int, button: str = "") -> None:
        """Un botón del joystick (ya traducido a tecla del control). En las apps que usan el joystick ellas
        mismas ([gamepad] menu_only_in: los juegos, un emulador propio) el servicio solo atiende exit_buttons
        (p. ej. mantener Back: salir del juego) y menu_buttons (el botón del medio: nuestro menú encima)."""
        mine = (self._gamepad_menu_only and not self._menu_open and not self._switching and not self.page
                and not self.power.asleep and (cur := self.current_app()) is not None
                and self.activities[cur].context in self._gamepad_menu_only)
        if not mine:
            if button == "BACK+START" or button.endswith(":HOLD"):
                return  # fuera de los juegos, esos botones ya hicieron lo suyo por separado
            self.on_key(code)
            return
        if button.upper() in self._gamepad_exit_buttons:
            # Mantener Back: salir del juego (y la lista de juegos) o, en otro programa, el menú.
            if self.power.note_key():
                return
            if self.activities[cur].context == "juegos":
                log.info("Joystick: %s mantenido, salgo del juego", button.partition(":")[0])
                self._macro_task = asyncio.get_running_loop().create_task(self._run_bg("juegos:quit"))
                return
            menu = next((c for c, a in (self.cfg.bindings.get("global") or {}).items() if a == "menu"), None)
            if menu is not None:
                self.on_key(menu)
            return
        if button.upper() in self._gamepad_menu_buttons:
            if self.activities[cur].context == "juegos":
                # En un juego: la lista encima del juego (seguir, salir, opciones de MAME, otras actividades).
                exit_code = next((c for c, a in (self.cfg.bindings.get("juegos") or {}).items()
                                  if a == "juegos:list"), None)
                if exit_code is not None:
                    self.on_key(exit_code)
                    return
            menu = next((c for c, a in (self.cfg.bindings.get("global") or {}).items() if a == "menu"), None)
            if menu is not None:
                self.on_key(menu)

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

    # ================================================================ bucles
    async def run(self) -> None:
        # El panel del menú se abre (oculto) de entrada: así aparece al instante con la tecla Menú.
        warm = asyncio.create_task(self.startup_menu())  # y el menú abierto, si no hay nada en pantalla
        power = asyncio.create_task(self.power.run())
        watch = asyncio.create_task(self.watch_apps())  # si la app en pantalla se cierra sola, vuelve el menú
        pads = asyncio.create_task(self.gamepads.run())  # joysticks: se detectan solos al conectarlos
        try:
            await asyncio.gather(self.browser_loop(), self.key_loop())
        finally:
            warm.cancel()
            power.cancel()
            watch.cancel()
            pads.cancel()

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

    # ================================================================ teclas
    async def context(self) -> str:
        if self._menu_open or self._switching:
            return "menu"
        if not self.page:
            cur = self.current_app()
            if cur == "tidal":
                return "tidal_nav" if self._tidal_nav else "tidal"
            self._tidal_nav = self.music.start_in_nav  # la próxima vez que se abra, arranca así
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

    async def toast(self, text: str, ms: int = 1800) -> None:
        """Aviso breve: en la página de Netflix o, sin navegador (o con el menú abierto), en el panel."""
        if self.page and not self._menu_open:
            try:
                await self.js("toast", text, ms)
                return
            except CDPError:
                pass
        await self.overlay.toast(text, ms)

    # ---------------------------------------------------------------- acciones generales
    async def a_none(self, arg: str) -> None:
        return None

    async def a_toast(self, arg: str) -> None:
        await self.toast(arg)

    async def a_wait(self, arg: str) -> None:
        try:
            await asyncio.sleep(max(0, int(arg or "500")) / 1000)
        except ValueError:
            log.warning("wait:%s inválido (milisegundos)", arg)

    async def a_macro(self, arg: str, depth: int = 0) -> None:
        macro = self.cfg.macros.get(arg)
        if macro is None:
            log.warning("No existe la macro %r (definila en [macros])", arg)
            return
        await self.run_action(macro if isinstance(macro, list) else [macro], depth + 1)
