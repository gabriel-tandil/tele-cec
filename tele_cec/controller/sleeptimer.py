"""Temporizador para apagar: en 15, 30… minutos o al terminar el tema o el video que suena.

Al cumplirse baja el volumen de a poco (unos segundos), pausa todo, deja el volumen como estaba y pone la Pi en
reposo ([power]; apaga también la TV). Con «al terminar este tema» no baja el volumen: pausa apenas termina. Se elige
desde Sonido en el menú de la TV o desde el celular; el menú y el celular muestran cuánto falta.
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
WARN_S = 60             # aviso en la TV un minuto antes
TRACK_POLL_S = 1.0


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
                await self.ctl.overlay.toast("Se apaga en 1 minuto", 4000)
            await asyncio.sleep(max(0.0, self.until - time.monotonic()))
            await self._finish("temporizador", fade=True)
        except asyncio.CancelledError:
            pass

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

    # ------------------------------------------------------------ apagar
    async def _finish(self, why: str, fade: bool) -> None:
        ctl = self.ctl
        log.info("Temporizador: %s, apago", why)
        level, muted = await audio.get_volume() if audio.available() else (None, False)
        if fade and level and not muted:
            steps = 15
            for i in range(1, steps + 1):  # bajar de a poco hasta casi nada
                await audio.change_volume(f"={round(level * (1 - i / steps))}")
                await asyncio.sleep(FADE_S / steps)
        await ctl._pause_if_playing(everything=True)
        if fade and level and not muted:
            await asyncio.sleep(0.5)
            await audio.change_volume(f"={level}")  # la próxima vez, con el volumen de siempre
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
