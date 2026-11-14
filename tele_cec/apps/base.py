"""Actividades que se eligen con el menú: Netflix, Tidal, Videos (VLC), Fotos (feh) y programas propios.

Una sola actividad abierta a la vez: elegir otra cierra la actual. Cada una sabe si está instalada, si
está abierta, de dónde va a sacar el contenido (para la línea secundaria del menú), abrirse y cerrarse.

Netflix y Tidal envuelven lo que ya hace el controller (navegador y High Tide). VLC, feh y los programas
de [activities.<id>] son procesos: se abren en un scope propio de systemd y se cierran con SIGTERM.
"""


from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from ..system.procs import find_pids, invalidate_procs, spawn_scoped
from .folders import _argv_matches

log = logging.getLogger("actividades")


# ---------------------------------------------------------------- base
class Activity:
    id = ""
    name = ""
    icon = "application-x-executable"
    context = "app"  # contexto de teclas mientras está en primer plano ([keys.<context>])

    def installed(self) -> bool:
        return True

    def running(self) -> bool:
        return False

    async def active(self) -> bool:
        """Para cerrarla antes de abrir otra (Netflix también cuenta si el navegador abrió y no se conectó)."""
        return self.running()

    def source(self) -> tuple[str, bool]:
        """(línea secundaria del menú, ¿se puede elegir?). Se llama en un hilo aparte: puede tardar."""
        return "", True

    async def open(self) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    async def pause(self) -> None:
        """La TV pasó a standby."""
        return None

    async def bring_front(self) -> None:
        """Ya estaba abierta de fondo y pasa a primer plano: mostrar su ventana si hace falta."""
        return None

    async def playback(self) -> str:
        """Si suena de fondo: Playing / Paused / Stopped según MPRIS ("" = no se sabe)."""
        return ""

    def unavailable_text(self) -> str:
        return f"{self.name} no tiene contenido"


# ---------------------------------------------------------------- procesos
class ProcActivity(Activity):
    """Un programa que se abre en su scope de systemd y se cierra con SIGTERM."""

    procs: set[str] = set()      # nombres de ejecutable que la identifican en /proc
    start_timeout = 15.0

    def pids(self, fresh: bool = False) -> list[int]:
        return find_pids(lambda argv: _argv_matches(argv, self.procs), fresh) if self.procs else []

    def running(self) -> bool:
        return bool(self.pids())

    async def spawn(self, args: list[str], env: dict[str, str] | None = None) -> bool:
        log.info("Abriendo %s: %s", self.name, shlex.join(args))
        try:
            await spawn_scoped(args, f"tele-cec-{self.id}", env=env, log=self.id)
        except OSError as exc:
            log.error("No pude abrir %s: %s", self.name, exc)
            return False
        invalidate_procs()
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if self.pids(fresh=True):
                return True
            await asyncio.sleep(0.25)
        log.error("%s no arrancó (¿falta el programa o falló al abrir?)", self.name)
        return False

    async def terminate(self, timeout: float = 10.0) -> None:
        pids = self.pids(fresh=True)
        if not pids:
            return
        log.info("Cierro %s (PID %s)", self.name, ", ".join(map(str, pids)))
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.pids(fresh=True):
                return
            await asyncio.sleep(0.25)
        log.warning("%s no se cerró en %.0f s: lo fuerzo", self.name, timeout)
        for pid in self.pids(fresh=True):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        invalidate_procs()

    async def close(self) -> None:
        await self.terminate()

    def send_signal(self, sig: int) -> bool:
        pids = self.pids(fresh=True)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        return bool(pids)


def _custom_command(command: Any, folder: Path | None) -> list[str]:
    """[apps.X] command: lista o texto; {carpeta} se reemplaza por la carpeta elegida (si no está, va al final)."""
    args = shlex.split(command) if isinstance(command, str) else [str(a) for a in command]
    args = [os.path.expanduser(a) for a in args]
    if folder is None:
        return args
    if any("{carpeta}" in a for a in args):
        return [a.replace("{carpeta}", str(folder)) for a in args]
    return args + [str(folder)]


class CommandActivity(ProcActivity):
    """[activities.<id>]: cualquier programa (por ejemplo un emulador)."""

    def __init__(self, ident: str, cfg: dict[str, Any]):
        self.id = ident
        self.name = str(cfg.get("name") or ident.capitalize())
        self.icon = str(cfg.get("icon") or "application-x-executable")
        self.command = cfg.get("command") or []
        args = _custom_command(self.command, None) if self.command else []
        proc = cfg.get("process") or (os.path.basename(args[0]) if args else "")
        self.procs = {str(p) for p in (proc if isinstance(proc, list) else [proc]) if p}
        self.start_timeout = float(cfg.get("start_timeout", 20))

    def installed(self) -> bool:
        args = _custom_command(self.command, None) if self.command else []
        return bool(args) and bool(shutil.which(args[0]) or os.path.isfile(args[0]))

    async def open(self) -> bool:
        return await self.spawn(_custom_command(self.command, None))
