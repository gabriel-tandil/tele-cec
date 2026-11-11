"""Apagado automático: si no sale sonido y nadie toca el control, la Raspberry Pi se apaga sola.

- Sonido: se mira la salida de audio (ALSA, /proc/asound): mientras algo suena está en «RUNNING».
- Uso: cada tecla del control reinicia la cuenta.
- TV apagada: si la TV avisó por CEC que pasó a standby, alcanza con un tiempo más corto.
- Antes de apagar, el panel muestra una cuenta regresiva; cualquier tecla la cancela. Después se apaga la TV
  por CEC y se ejecuta el comando de apagado (systemctl poweroff).

Con la Pi apagada el control remoto no la vuelve a prender (CEC no funciona): en una Pi 400 se prende con
Fn+F10 en su teclado, o desenchufando y enchufando.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import time
from typing import TYPE_CHECKING, Any

from .cec import OP_STANDBY, CecFrame
from .music import run_cmd

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger("apagado")

CHECK_EVERY = 30.0
# Mensajes CEC de la TV que indican que se prendió (o que alguien la está usando con la Pi).
TV_ON_OPCODES = {0x04, 0x0D, 0x82, 0x85, 0x86}


def _duration(seconds: float) -> str:
    minutes = seconds / 60
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes / 60:g} h"
    return f"{minutes:g} min"


def audio_playing() -> bool:
    """¿Sale sonido? Alguna salida de audio de ALSA en estado RUNNING (PipeWire la cierra al quedar en silencio)."""
    for path in glob.glob("/proc/asound/card*/pcm*p/sub*/status"):
        try:
            with open(path, encoding="utf-8") as fh:
                if "RUNNING" in fh.readline():
                    return True
        except OSError:
            continue
    return False


class IdleShutdown:
    def __init__(self, ctl: Controller, cfg: dict[str, Any] | None):
        cfg = cfg or {}
        self.ctl = ctl
        self.enabled = bool(cfg.get("enabled", False))
        self.idle = float(cfg.get("idle_minutes", 180)) * 60
        self.tv_off_idle = float(cfg.get("tv_off_minutes", 15)) * 60
        self.warn = float(cfg.get("warn_seconds", 60))
        self.tv_standby = bool(cfg.get("tv_standby", True))
        cmd = cfg.get("command") or ["systemctl", "poweroff"]
        self.command = [str(c) for c in cmd] if isinstance(cmd, list) else str(cmd).split()
        now = time.monotonic()
        self.last_activity = now      # última vez que sonó algo o se apretó una tecla
        self.tv_off_since: float | None = None
        self._countdown: asyncio.Task | None = None

    # ------------------------------------------------------------ lo que avisa el controller
    def note_key(self) -> None:
        self.last_activity = time.monotonic()
        self.tv_off_since = None  # si alguien usa el control, la TV está prendida
        if self._countdown and not self._countdown.done():
            self._countdown.cancel()
            log.info("Apagado automático cancelado (tecla)")
            asyncio.get_running_loop().create_task(self.ctl.toast("Apagado cancelado", 2500))

    def on_frame(self, frame: CecFrame) -> None:
        if frame.initiator != 0:  # solo lo que manda la TV
            return
        if frame.opcode == OP_STANDBY:
            if self.tv_off_since is None:
                self.tv_off_since = time.monotonic()
                log.info("TV en standby: la Pi se apaga a los %.0f min si no suena nada", self.tv_off_idle / 60)
        elif frame.opcode in TV_ON_OPCODES and self.tv_off_since is not None:
            self.tv_off_since = None
            log.info("TV prendida de nuevo")

    # ------------------------------------------------------------ bucle
    def reason(self, now: float) -> str | None:
        """Por qué habría que apagar ahora (o None)."""
        quiet = now - self.last_activity
        if self.tv_off_since is not None and now - max(self.tv_off_since, self.last_activity) >= self.tv_off_idle:
            return f"TV apagada y {quiet / 60:.0f} min sin sonido"
        if quiet >= self.idle:
            return f"{quiet / 60:.0f} min sin sonido ni teclas del control"
        return None

    async def run(self) -> None:
        if not self.enabled:
            return
        log.info("Apagado automático: a los %s sin sonido ni teclas (%s con la TV apagada)",
                 _duration(self.idle), _duration(self.tv_off_idle))
        while True:
            await asyncio.sleep(CHECK_EVERY)
            if await asyncio.to_thread(audio_playing):
                self.last_activity = time.monotonic()
                continue
            why = self.reason(time.monotonic())
            if why and not (self._countdown and not self._countdown.done()):
                self._countdown = asyncio.get_running_loop().create_task(self._count_and_shutdown(why))

    async def _count_and_shutdown(self, why: str) -> None:
        log.warning("Apagado automático en %.0f s: %s", self.warn, why)
        left = int(self.warn)
        while left > 0:
            # Si vuelve a sonar algo (alguien puso play desde otro lado), se cancela.
            if await asyncio.to_thread(audio_playing):
                self.last_activity = time.monotonic()
                log.info("Apagado automático cancelado (volvió a sonar algo)")
                await self.ctl.toast("Apagado cancelado", 2500)
                return
            step = min(5, left) if left > 10 else 1
            # El aviso dura hasta la próxima actualización (así no parpadea).
            await self.ctl.toast(f"La Raspberry Pi se apaga en {left} s\nApretá cualquier tecla para cancelar",
                                 int(step * 1000) + 700)
            await asyncio.sleep(step)
            left -= step
        await self.shutdown(why)

    async def shutdown(self, why: str) -> None:
        log.warning("Apagando la Raspberry Pi: %s", why)
        await self.ctl.toast("Apagando…", 3000)
        source = self.ctl.source
        if self.tv_standby and source is not None:
            try:
                await source.send("standby 0")  # la TV (dirección lógica 0)
                await asyncio.sleep(2)
            except Exception as exc:  # noqa: BLE001
                log.warning("No pude apagar la TV por CEC: %s", exc)
        rc, out = await run_cmd(self.command, 30)
        if rc != 0:
            log.error("No pude apagar (%s): %s", " ".join(self.command), out.strip()[:200] or rc)
            await self.ctl.toast("No pude apagar la Raspberry Pi (ver el log)", 4000)
            self.last_activity = time.monotonic()  # no reintentar enseguida
