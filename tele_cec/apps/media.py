"""Actividades de una carpeta de contenido: la base común de Videos, Música y Fotos."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .base import ProcActivity, _custom_command
from .folders import describe_folder, expand_folders, find_media_folder  # noqa: F401  (describe_folder: subclases)

log = logging.getLogger("actividades")


class MediaActivity(ProcActivity):
    """Videos o fotos de una carpeta (la primera de `folders` que tenga contenido: pendrive o la local)."""

    exts: set[str] = set()
    empty_text = "Sin contenido"

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.cfg = cfg
        self.name = str(cfg.get("name") or self.name)
        self.icon = str(cfg.get("icon") or self.icon)
        self.folders = cfg.get("folders") or []
        self.shuffle = bool(cfg.get("shuffle", False))
        self.command = cfg.get("command") or []
        self.current_folder: Path | None = None
        self.exe = ""
        if self.command:
            self.exe = _custom_command(self.command, None)[0]
            self.procs = {os.path.basename(self.exe)}

    def _have(self, default: str) -> bool:
        exe = self.exe or default
        return bool(shutil.which(exe) or os.path.isfile(exe))

    def folder(self) -> Path | None:
        return find_media_folder(self.folders, self.exts)

    def source(self) -> tuple[str, bool]:
        f = self.folder()
        return (describe_folder(f), True) if f else (self.empty_text, False)

    def unavailable_text(self) -> str:
        return self.empty_text

    # Texto de la opción que abre la carpeta elegida en el selector de carpetas del menú (y si está vacía).
    play_label = "▶ Abrir esta carpeta"
    empty_in_folder = "No hay nada para mostrar acá"
    # OK sobre la fila del menú: abre el selector en la carpeta predeterminada (en vez de arrancar con todo).
    browse_on_ok = False
    # En el selector se elige un archivo (Videos: una película), no se reproduce la carpeta entera.
    pick_files = False

    def roots(self) -> list[Path]:
        """Las carpetas configuradas que existen (punto de partida para elegir otra desde el menú)."""
        return expand_folders(self.folders)

    def default_args(self, folder: Path) -> list[str]:
        raise NotImplementedError

    async def open(self, folder: Path | None = None) -> bool:
        """Abre la carpeta predeterminada (la primera de `folders` con contenido) o la elegida en el menú."""
        if folder is None:
            folder = await asyncio.to_thread(self.folder)
        if folder is None:
            log.warning("%s: no hay contenido en %s", self.name, ", ".join(map(str, self.folders)) or "(sin carpetas)")
            return False
        args = _custom_command(self.command, folder) if self.command else self.default_args(folder)
        ok = await self.spawn(args)
        if ok:
            self.current_folder = folder
        return ok
