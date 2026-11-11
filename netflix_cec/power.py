"""Reposo o apagado automático: si no sale sonido y nadie toca el control, la Raspberry Pi descansa.

- Sonido: se mira la salida de audio (ALSA, /proc/asound): mientras algo suena está en «RUNNING».
- Uso: cada tecla del control reinicia la cuenta.
- TV apagada: si la TV avisó por CEC que pasó a standby, alcanza con un tiempo más corto.
- Antes, el panel muestra una cuenta regresiva; cualquier tecla la cancela. Después se apaga la TV por CEC.

Dos modos ([power] mode):
- «reposo» (por defecto): se guarda lo que estaba abierto, se pausa y se cierra todo. La Pi sigue
  escuchando CEC: al apretar una tecla o al prenderse la TV vuelve y restaura lo que había, en pausa.
- «apagar»: systemctl poweroff. Ahorra un poco más, pero el control no la vuelve a prender (CEC no funciona
  con la Pi apagada): en una Pi 400 se prende con Fn+F10, o desenchufando y enchufando.
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
TV_POLL_EVERY = 60.0   # en reposo: cada cuánto se le pregunta a la TV si está prendida
OP_REPORT_POWER = 0x90  # respuesta de la TV a «pow 0»: 0 = prendida, 1 = standby, 2 = prendiéndose, 3 = apagándose
# Mensajes CEC que manda la TV al prenderse (o al pedir la fuente activa).
TV_ON_OPCODES = {0x04, 0x0D, 0x82, 0x84, 0x85, 0x86, 0x87}


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
        self.mode = "apagar" if str(cfg.get("mode", "reposo")).strip().lower() in ("apagar", "poweroff") else "reposo"
        self.idle = float(cfg.get("idle_minutes", 180)) * 60
        self.tv_off_idle = float(cfg.get("tv_off_minutes", 15)) * 60
        self.warn = float(cfg.get("warn_seconds", 60))
        self.tv_standby = bool(cfg.get("tv_standby", True))
        self.wake_on_tv = bool(cfg.get("wake_on_tv", True))
        cmd = cfg.get("command") or ["systemctl", "poweroff"]
        self.command = [str(c) for c in cmd] if isinstance(cmd, list) else str(cmd).split()
        self.last_activity = time.monotonic()  # última vez que sonó algo o se apretó una tecla
        self.tv_off_since: float | None = None
        self.asleep = False
        self.saved: dict[str, Any] = {}
        self._asleep_at = 0.0
        self._countdown: asyncio.Task | None = None
        self._wake_task: asyncio.Task | None = None

    @property
    def verb(self) -> str:
        return "entra en reposo" if self.mode == "reposo" else "se apaga"

    # ------------------------------------------------------------ lo que avisa el controller
    def note_key(self) -> bool:
        """Se apretó una tecla. Devuelve True si solo sirvió para despertar (no hay que hacer nada más con ella)."""
        self.last_activity = time.monotonic()
        self.tv_off_since = None  # si alguien usa el control, la TV está prendida
        if self.asleep:
            self.wake(by_key=True)
            return True
        if self._countdown and not self._countdown.done():
            self._countdown.cancel()
            log.info("Reposo o apagado automático cancelado (tecla)")
            asyncio.get_running_loop().create_task(self.ctl.toast("Cancelado", 2500))
        return False

    def on_frame(self, frame: CecFrame) -> None:
        if frame.initiator != 0:  # solo lo que manda la TV
            return
        if frame.opcode == OP_STANDBY:
            if self.tv_off_since is None and not self.asleep:
                self.tv_off_since = time.monotonic()
                log.info("TV en standby: la Pi %s a los %s si no suena nada", self.verb, _duration(self.tv_off_idle))
            return
        tv_on = frame.opcode in TV_ON_OPCODES or (
            frame.opcode == OP_REPORT_POWER and bool(frame.params) and frame.params[0] in (0x00, 0x02))
        if not tv_on:
            return
        if self.asleep:
            # Recién dormida, la TV todavía puede contestar «prendida» un instante: se ignora.
            if self.wake_on_tv and time.monotonic() - self._asleep_at > 20:
                log.info("La TV se prendió")
                self.wake(by_key=False)
        elif self.tv_off_since is not None:
            self.tv_off_since = None
            log.info("TV prendida de nuevo")

    # ------------------------------------------------------------ bucle
    def reason(self, now: float) -> str | None:
        """Por qué habría que descansar ahora (o None)."""
        quiet = now - self.last_activity
        if self.tv_off_since is not None and now - max(self.tv_off_since, self.last_activity) >= self.tv_off_idle:
            return f"TV apagada y {quiet / 60:.0f} min sin sonido"
        if quiet >= self.idle:
            return f"{quiet / 60:.0f} min sin sonido ni teclas del control"
        return None

    async def run(self) -> None:
        if not self.enabled:
            return
        log.info("%s automático: a los %s sin sonido ni teclas (%s con la TV apagada)",
                 "Reposo" if self.mode == "reposo" else "Apagado", _duration(self.idle), _duration(self.tv_off_idle))
        last_check = last_poll = 0.0
        while True:
            await asyncio.sleep(5.0)
            now = time.monotonic()
            if self.asleep:
                # En reposo: preguntarle a la TV si se prendió (la respuesta llega a on_frame).
                if self.wake_on_tv and now - last_poll >= TV_POLL_EVERY and self.ctl.source is not None:
                    last_poll = now
                    await self.ctl.source.send("pow 0")
                continue
            if now - last_check < CHECK_EVERY:
                continue
            last_check = now
            if await asyncio.to_thread(audio_playing):
                self.last_activity = time.monotonic()
                continue
            why = self.reason(time.monotonic())
            if why and not (self._countdown and not self._countdown.done()):
                self._countdown = asyncio.get_running_loop().create_task(self._count_and_rest(why))

    async def _count_and_rest(self, why: str) -> None:
        log.warning("En %.0f s la Pi %s: %s", self.warn, self.verb, why)
        left = int(self.warn)
        while left > 0:
            # Si vuelve a sonar algo (alguien puso play desde otro lado), se cancela.
            if await asyncio.to_thread(audio_playing):
                self.last_activity = time.monotonic()
                log.info("Cancelado: volvió a sonar algo")
                await self.ctl.toast("Cancelado", 2500)
                return
            step = min(5, left) if left > 10 else 1
            # El aviso dura hasta la próxima actualización (así no parpadea).
            await self.ctl.toast(f"La Raspberry Pi {self.verb} en {left} s\nApretá cualquier tecla para cancelar",
                                 int(step * 1000) + 700)
            await asyncio.sleep(step)
            left -= step
        if self.mode == "reposo":
            await self.sleep(why)
        else:
            await self.shutdown(why)

    async def _tv_off(self) -> None:
        source = self.ctl.source
        if self.tv_standby and source is not None:
            try:
                await source.send("standby 0")  # la TV (dirección lógica 0)
                await asyncio.sleep(2)
            except Exception as exc:  # noqa: BLE001
                log.warning("No pude apagar la TV por CEC: %s", exc)

    # ------------------------------------------------------------ reposo
    async def sleep(self, why: str) -> None:
        log.warning("Reposo: %s", why)
        await self.ctl.toast("Reposo…", 2000)
        self.saved = await self.ctl.snapshot()
        log.info("Reposo: guardo %s", self.saved or "nada abierto")
        await self.ctl.sleep_apps()
        await self._tv_off()
        self.asleep = True
        self._asleep_at = time.monotonic()
        self.tv_off_since = None

    def wake(self, by_key: bool) -> None:
        if not self.asleep or (self._wake_task and not self._wake_task.done()):
            return
        self.asleep = False
        self.last_activity = time.monotonic()
        self.tv_off_since = None
        saved, self.saved = self.saved, {}
        log.info("Vuelvo del reposo (%s): %s", "tecla del control" if by_key else "se prendió la TV",
                 saved or "no había nada abierto")
        self._wake_task = asyncio.get_running_loop().create_task(self.ctl.restore(saved, activate=by_key))

    # ------------------------------------------------------------ apagado
    async def shutdown(self, why: str) -> None:
        log.warning("Apagando la Raspberry Pi: %s", why)
        await self.ctl.toast("Apagando…", 3000)
        await self._tv_off()
        rc, out = await run_cmd(self.command, 30)
        if rc != 0:
            log.error("No pude apagar (%s): %s", " ".join(self.command), out.strip()[:200] or rc)
            await self.ctl.toast("No pude apagar la Raspberry Pi (ver el log)", 4000)
            self.last_activity = time.monotonic()  # no reintentar enseguida
