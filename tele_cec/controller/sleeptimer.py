"""Temporizador para apagar: en 15, 30… minutos o al terminar el tema o el video que suena.

Al cumplirse baja el volumen de a poco (unos segundos), pausa todo, deja el volumen como estaba y pone la Pi en
reposo ([power]; apaga también la TV). Con «al terminar este tema» no baja el volumen: pausa apenas termina. Se elige
desde Sonido en el menú de la TV o desde el celular; el menú y el celular muestran cuánto falta.

El último minuto la TV muestra una cuenta regresiva: cualquier tecla del control lo retrasa 15 minutos (también
mientras baja el volumen, que vuelve a como estaba). Esa tecla no le llega a la app.

Desde cualquier pantalla: el punto (•) dos veces seguidas muestra cuánto falta y, mientras se ve ese aviso, cada
toque del punto pasa a la opción siguiente (15 → 30 → 45 → 60 → 90 min → al terminar el tema → apagado), como el
botón Sleep de las teles; «apagado» termina el recorrido. Un toque solo sigue siendo la Ayuda. [power] timer_key y timer_double_press_ms.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..system import audio

if TYPE_CHECKING:
    from . import Controller

log = logging.getLogger("tele")

CHOICES: list[int | str] = [0, 15, 30, 45, 60, 90, "track"]   # 0 = sin temporizador; "track" = al terminar el tema
FADE_S = 15.0           # cuánto tarda en bajar el volumen antes de pausar
WARN_S = 60             # cuenta regresiva en la TV el último minuto
POSTPONE_MIN = 15       # cuánto lo retrasa una tecla durante la cuenta regresiva ([power] timer_postpone_minutes)
TRACK_POLL_S = 1.0
TIMER_KEY = "DOT"       # la tecla que, apretada dos veces seguidas, abre el temporizador ([power] timer_key)
DOUBLE_PRESS_MS = 1000  # cuánto puede pasar entre los dos toques ([power] timer_double_press_ms)
KEY_PANEL_S = 4.0       # cuánto se ve el aviso del temporizador (mientras se ve, cada toque lo cambia)
KEY_CYCLE: list[int | str] = [15, 30, 45, 60, 90, "track", 0]   # el recorrido con la tecla: termina apagado


def choice_text(choice: int | str) -> str:
    if choice == "track":
        return "Al terminar este tema o video"
    if not choice:
        return "Sin temporizador"
    m = int(choice)
    return f"En {m // 60} h y media" if m == 90 else f"En {m // 60} h" if m % 60 == 0 else f"En {m} minutos"


def left_text(seconds: float) -> str:
    """«en 28 min», «en 1 h 05 min», «en menos de un minuto»."""
    m = int(seconds // 60) + (1 if seconds % 60 >= 30 else 0)
    if m < 1:
        return "en menos de un minuto"
    return f"en {m // 60} h {m % 60:02d} min" if m >= 60 else f"en {m} min"


class SleepTimer:
    def __init__(self, ctl: Controller):
        self.ctl = ctl
        self.choice: int | str = 0
        self.until: float | None = None    # monotonic (en minutos)
        self.track: tuple[str, str] | None = None   # (reproductor, título) del tema que hay que dejar terminar
        self._task: asyncio.Task | None = None
        self.warning = False   # cuenta regresiva (o bajando el volumen): una tecla lo retrasa
        self._volume: int | None = None   # el volumen de antes de empezar a bajarlo (para devolverlo)
        try:
            power = ctl.cfg.raw.get("power") or {}
        except AttributeError:
            power = {}
        self.postpone_min = max(1, int(power.get("timer_postpone_minutes", POSTPONE_MIN) or POSTPONE_MIN))
        from ..keys import parse_key  # noqa: PLC0415
        key = str(power.get("timer_key", TIMER_KEY) or "").strip()
        self.key_code: int | None = parse_key(key) if key else None   # None = sin tecla para el temporizador
        if key and self.key_code is None:
            log.warning("[power] timer_key = %r: no conozco esa tecla (usá tele-cec aprender)", key)
        self.double_s = max(0.2, float(power.get("timer_double_press_ms", DOUBLE_PRESS_MS) or DOUBLE_PRESS_MS) / 1000)
        self._key_last = 0.0         # cuándo fue el último toque de esa tecla (para reconocer el doble toque)
        self._panel_until = 0.0      # hasta cuándo se ve el aviso (mientras tanto, un toque cambia la opción)

    def note_key(self) -> bool:
        """Se tocó el control. Durante la cuenta regresiva: lo retrasa (POSTPONE_MIN) y devuelve True (la tecla no
        tiene que llegarle a la app)."""
        if not (self.warning and self.active() and self.choice != "track"):
            return False
        minutes = self.postpone_min
        log.info("Temporizador: retrasado %d min (tecla)", minutes)
        loop = asyncio.get_running_loop()
        old = self._task
        self.warning = False
        self.until = time.monotonic() + minutes * 60
        self._task = loop.create_task(self._run_time())  # el nuevo, antes de cancelar el viejo (sigue «activo»)
        if old is not None and not old.done():
            old.cancel()
        loop.create_task(self.ctl.overlay.toast(f"{minutes} minutos más · se apaga {left_text(minutes * 60)}", 3000))
        return True

    # ------------------------------------------------------------ la tecla (• dos veces)
    def key_press(self, code: int) -> str | None:
        """Una tecla del control. Con la tecla del temporizador: "show" (el segundo toque seguido: mostrar cuánto
        falta), "step" (con el aviso a la vista: la opción siguiente) o None (el primer toque: lo de siempre, la
        Ayuda). Con otra tecla, None (y se olvida el primer toque)."""
        if self.key_code is None or code != self.key_code:
            self._key_last = 0.0
            return None
        now = time.monotonic()
        if now < self._panel_until:
            return "step"
        if now - self._key_last <= self.double_s:
            self._key_last = 0.0
            return "show"
        self._key_last = now
        return None

    async def key_text(self, what: str) -> tuple[str, int]:
        """El aviso para la tecla y cuánto dura: «show» dice cómo está; «step» pasa a la opción siguiente
        (15 → 30 → 45 → 60 → 90 min → al terminar el tema → apagado). Al llegar a «apagado» el recorrido termina:
        el aviso se va y el próximo toque vuelve a ser lo de siempre (para empezar de nuevo, otra vez dos toques)."""
        playing = await self._playing() is not None
        if what == "step":
            ok, _msg = await self.set(self._key_next(playing))
            if not ok:  # «al terminar este tema» y justo dejó de sonar: apagado
                await self.set(0)
            if not self.active():
                self._panel_until = self._key_last = 0.0
                return "Temporizador apagado", 2500
        self._panel_until = time.monotonic() + KEY_PANEL_S
        nxt = self._key_next(playing)
        hint = "apagarlo" if not nxt else choice_text(nxt).lower()
        return f"Temporizador · {self.text()}\n• otra vez: {hint}", int(KEY_PANEL_S * 1000)

    def _key_next(self, playing: bool) -> int | str:
        """Lo que deja un toque más: la opción siguiente de KEY_CYCLE («al terminar este tema» solo si suena
        algo); después de la última, 0 (apagado)."""
        cur = self.choice if self.active() else 0
        seq = [c for c in KEY_CYCLE if playing or c != "track"]
        if not cur:
            return seq[0]
        if cur == "track":
            return 0
        if cur in seq:
            return seq[seq.index(cur) + 1]
        return next((c for c in seq if isinstance(c, int) and c > int(cur)), 0)   # p. ej. 120 min puestos desde el celular

    def panel_visible(self) -> bool:
        return time.monotonic() < self._panel_until

    # ------------------------------------------------------------ estado
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def left(self) -> float | None:
        return max(0.0, self.until - time.monotonic()) if self.until is not None and self.active() else None

    def status(self) -> dict[str, Any] | None:
        """Para el celular: {"mode": "time", "left": segundos} o {"mode": "track"}; None sin temporizador."""
        if not self.active():
            return None
        if self.choice == "track":
            return {"mode": "track", "text": "al terminar este tema"}
        left = self.left() or 0.0
        return {"mode": "time", "left": round(left), "text": left_text(left)}

    def text(self) -> str:
        """«Se apaga en 28 min», «Se apaga al terminar este tema» o «Sin temporizador»."""
        st = self.status()
        return f"Se apaga {st['text']}" if st else "Sin temporizador"

    # ------------------------------------------------------------ elegir
    async def set(self, choice: int | str) -> tuple[bool, str]:
        """0 / None: lo saca. Minutos (1–600) o "track". Devuelve (ok, mensaje)."""
        self.cancel()
        if not choice:
            self.choice = 0
            log.info("Temporizador: ninguno")
            return True, "Sin temporizador"
        if choice == "track":
            track = await self._playing()
            if track is None:
                return False, "No suena nada"
            self.choice, self.track, self.until = "track", (track.player, track.title), None
            self._task = asyncio.get_running_loop().create_task(self._run_track())
            log.info("Temporizador: al terminar «%s»", track.title)
            return True, "Se apaga al terminar este tema"
        minutes = max(1, min(600, int(choice)))
        self.choice, self.until = minutes, time.monotonic() + minutes * 60
        self._task = asyncio.get_running_loop().create_task(self._run_time())
        log.info("Temporizador: en %d min", minutes)
        return True, f"Se apaga {left_text(minutes * 60)}"

    async def step(self, delta: int) -> tuple[bool, str]:
        """◀ ▶ en el menú de la TV: la opción anterior o siguiente de CHOICES."""
        cur = self.choice if self.active() else 0
        i = CHOICES.index(cur) if cur in CHOICES else 0
        ok, msg = await self.set(CHOICES[(i + delta) % len(CHOICES)])
        if not ok:  # «al terminar este tema» sin nada sonando: saltarla
            ok, msg = await self.set(CHOICES[(i + 2 * delta) % len(CHOICES)])
        return ok, msg

    def cancel(self) -> None:
        if self._task and not self._task.done() and self._task is not asyncio.current_task():
            self._task.cancel()
        self._task, self.until, self.track = None, None, None
        self.warning = False

    # ------------------------------------------------------------ esperar
    async def _playing(self):
        from ..web import nowplaying  # noqa: PLC0415  (playerctl: lo que suena, de cualquier app)
        tracks = await nowplaying.players()
        return next((t for t in tracks if t.status == "Playing" and t.title), None)

    async def _run_time(self) -> None:
        try:
            assert self.until is not None
            warn_at = self.until - WARN_S
            if time.monotonic() < warn_at:
                await asyncio.sleep(warn_at - time.monotonic())
            self.warning = True
            hint = f"Cualquier tecla: {self.postpone_min} minutos más"
            while (left := self.until - time.monotonic()) > 0.05:
                step = min(5.0, left) if left > 10 else min(1.0, left)
                # El aviso dura hasta la próxima actualización (así no parpadea).
                await self.ctl.overlay.toast(f"Se apaga en {max(1, round(left))} s\n{hint}", int(step * 1000) + 700)
                await asyncio.sleep(step)
            await self._finish("temporizador", fade=True)
        except asyncio.CancelledError:
            await self._restore_volume()  # retrasado mientras bajaba el volumen: vuelve a como estaba

    async def _run_track(self) -> None:
        """Hasta que termine ese tema: cambia el título, se detiene, o el tiempo llega al final."""
        from ..web import nowplaying  # noqa: PLC0415
        assert self.track is not None
        player, title = self.track
        try:
            while True:
                tracks = await nowplaying.players()
                t = next((x for x in tracks if x.player == player), None)
                if t is None or t.status == "Stopped" or (t.title and t.title != title):
                    break
                left = t.length - t.position if t.length else None
                if t.status == "Playing" and left is not None and left < TRACK_POLL_S + 0.5:
                    await asyncio.sleep(max(0.0, left - 0.3))  # pausar justo antes de que empiece el siguiente
                    break
                await asyncio.sleep(TRACK_POLL_S)
            await self._finish(f"terminó «{title}»", fade=False)
        except asyncio.CancelledError:
            pass

    async def _restore_volume(self) -> None:
        if self._volume is not None:
            level, self._volume = self._volume, None
            try:
                await audio.change_volume(f"={level}")
            except Exception:  # noqa: BLE001
                log.exception("Temporizador: no pude devolver el volumen")

    # ------------------------------------------------------------ apagar
    async def _finish(self, why: str, fade: bool) -> None:
        ctl = self.ctl
        log.info("Temporizador: %s, apago", why)
        level, muted = await audio.get_volume() if audio.available() else (None, False)
        if fade and level and not muted:
            await ctl.overlay.toast(f"Bajando el volumen…\nCualquier tecla: {self.postpone_min} minutos más",
                                    int(FADE_S * 1000) + 500)
            self._volume = level
            steps = 15
            for i in range(1, steps + 1):  # bajar de a poco hasta casi nada (una tecla todavía lo retrasa)
                await audio.change_volume(f"={round(level * (1 - i / steps))}")
                await asyncio.sleep(FADE_S / steps)
        self.warning = False  # desde acá ya no se retrasa: pausa y reposo
        await ctl._pause_if_playing(everything=True)
        if fade and level and not muted:
            await asyncio.sleep(0.5)
            await audio.change_volume(f"={level}")  # la próxima vez, con el volumen de siempre
            self._volume = None
        self._task, self.until, self.track, self.choice = None, None, None, 0
        pw = ctl.power
        if pw.asleep:
            return
        if pw.enabled and pw.mode == "apagar":
            await pw.shutdown(f"temporizador ({why})")
        elif pw.enabled:
            await pw.sleep(f"temporizador ({why})")
        else:
            await pw._tv_off()  # sin reposo automático: queda en pausa y se apaga la TV
