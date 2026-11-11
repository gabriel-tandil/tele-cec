"""Actividades que se eligen con el menú: Netflix, Tidal, Videos (VLC), Fotos (feh) y programas propios.

Una sola actividad abierta a la vez: elegir otra cierra la actual. Cada una sabe si está instalada, si
está abierta, de dónde va a sacar el contenido (para la línea secundaria del menú), abrirse y cerrarse.

Netflix y Tidal envuelven lo que ya hace el controller (navegador y High Tide). VLC, feh y los programas
de [activities.<id>] son procesos: se abren en un scope propio de systemd y se cierran con SIGTERM.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import glob
import logging
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .browser import spawn_scoped
from .music import Mpris, find_pids, invalidate_procs, wtype_key

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger("actividades")

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".wmv",
             ".flv", ".ogv", ".3gp", ".vob", ".divx"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".pnm", ".ppm", ".heic"}

# Tope de la búsqueda de contenido en una carpeta (un disco USB grande no debe trabar el menú).
SCAN_MAX_DEPTH = 5
SCAN_MAX_ENTRIES = 20000
SCAN_MAX_SECONDS = 1.5


# ---------------------------------------------------------------- carpetas de contenido
def expand_folders(patterns: list[str] | str) -> list[Path]:
    """Carpetas que existen, en el orden de la lista. Acepta ~, $VARIABLES y comodines (/media/*/*)."""
    if isinstance(patterns, str):
        patterns = [patterns]
    out: list[Path] = []
    for pat in patterns or []:
        pat = os.path.expandvars(os.path.expanduser(str(pat)))
        for m in (sorted(glob.glob(pat)) if glob.has_magic(pat) else [pat]):
            p = Path(m)
            if p.is_dir() and p not in out:
                out.append(p)
    return out


def count_media(folder: Path, exts: set[str], stop_at: int = 1) -> int:
    """Cuántos archivos con esas extensiones hay (corta al llegar a stop_at o a los topes de búsqueda)."""
    found = 0
    seen = 0
    deadline = time.monotonic() + SCAN_MAX_SECONDS
    base_depth = len(folder.parts)
    for root, dirs, files in os.walk(folder, onerror=lambda e: None):
        if len(Path(root).parts) - base_depth >= SCAN_MAX_DEPTH:
            dirs[:] = []
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in files:
            seen += 1
            if os.path.splitext(f)[1].lower() in exts:
                found += 1
                if found >= stop_at:
                    return found
        if seen > SCAN_MAX_ENTRIES or time.monotonic() > deadline:
            break
    return found


def find_media_folder(patterns: list[str] | str, exts: set[str]) -> Path | None:
    """La primera carpeta de la lista que tenga archivos de ese tipo (los pendrives van primero)."""
    for folder in expand_folders(patterns):
        if count_media(folder, exts):
            return folder
    return None


def describe_folder(folder: Path) -> str:
    s = str(folder)
    if s.startswith(("/media/", "/mnt/")):
        return f"Pendrive {folder.name}"
    home = str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def _argv_matches(argv: list[str], names: set[str]) -> bool:
    """¿Es uno de estos programas? Mira el nombre del ejecutable y, para scripts, el primer argumento."""
    exe = os.path.basename(argv[0])
    if exe in names:
        return True
    if exe.startswith(("python", "sh", "bash", "perl")) and len(argv) > 1:
        return os.path.basename(argv[1]) in names
    return any(fnmatch.fnmatch(exe, n) for n in names if glob.has_magic(n))


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

    def unavailable_text(self) -> str:
        return f"{self.name} no tiene contenido"


# ---------------------------------------------------------------- Netflix y Tidal (envuelven al controller)
class NetflixActivity(Activity):
    id = "netflix"
    name = "Netflix"
    icon = "netflix.svg"
    context = "browse"

    def __init__(self, ctl: Controller, cfg: dict[str, Any] | None = None):
        self.ctl = ctl
        cfg = cfg or {}
        self.name = str(cfg.get("name") or self.name)
        self.icon = str(cfg.get("icon") or self.icon)

    def installed(self) -> bool:
        binary = getattr(self.ctl.browser, "binary", None)
        return binary() is not None if callable(binary) else True

    def running(self) -> bool:
        return self.ctl.page is not None

    async def active(self) -> bool:
        return self.ctl.page is not None or await self.ctl.browser.alive()

    async def open(self) -> bool:
        return await self.ctl.launch_browser()

    async def close(self) -> None:
        if self.ctl.page:
            await self.ctl.pause_netflix()
        await self.ctl.browser.close()

    async def pause(self) -> None:
        await self.ctl.pause_netflix()


class TidalActivity(Activity):
    id = "tidal"
    name = "Tidal"
    icon = "tidal.svg"
    context = "tidal"

    def __init__(self, ctl: Controller, cfg: dict[str, Any] | None = None):
        self.ctl = ctl  # ctl.music se lee cada vez (las pruebas lo reemplazan)
        cfg = cfg or {}
        self.name = str(cfg.get("name") or self.name)
        self.icon = str(cfg.get("icon") or self.icon)

    def installed(self) -> bool:
        return self.ctl.music.installed()

    def running(self) -> bool:
        return self.ctl.music.running()

    async def open(self) -> bool:
        m = self.ctl.music
        if not await m.launch():
            return False
        if m.resume_on_open and not await m.resume():
            # High Tide no retoma "Mis canciones" por sí solo (solo guarda el número de tema).
            start = m.saved_index()
            log.info("Retomo «Mis canciones» desde el tema %d", start + 1)
            await m.my_tracks(start=start)
        return True

    async def close(self) -> None:
        await self.ctl.music.quit()

    async def pause(self) -> None:
        m = self.ctl.music
        if (await m.status()).get("status") == "Playing":
            await m.ctl("pause")
            log.info("Música pausada por standby de la TV")


# ---------------------------------------------------------------- procesos
class ProcActivity(Activity):
    """Un programa que se abre en su scope de systemd y se cierra con SIGTERM."""

    procs: set[str] = set()      # nombres de ejecutable que la identifican en /proc
    start_timeout = 15.0

    def pids(self, fresh: bool = False) -> list[int]:
        return find_pids(lambda argv: _argv_matches(argv, self.procs), fresh) if self.procs else []

    def running(self) -> bool:
        return bool(self.pids())

    async def spawn(self, args: list[str]) -> bool:
        log.info("Abriendo %s: %s", self.name, shlex.join(args))
        try:
            await spawn_scoped(args, f"netflix-cec-{self.id}")
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

    def default_args(self, folder: Path) -> list[str]:
        raise NotImplementedError

    async def open(self) -> bool:
        folder = await asyncio.to_thread(self.folder)
        if folder is None:
            log.warning("%s: no hay contenido en %s", self.name, ", ".join(map(str, self.folders)) or "(sin carpetas)")
            return False
        args = _custom_command(self.command, folder) if self.command else self.default_args(folder)
        return await self.spawn(args)


class VideosActivity(MediaActivity):
    id = "videos"
    name = "Videos"
    icon = "videos.svg"
    context = "videos"
    procs = {"vlc", "cvlc"}
    exts = VIDEO_EXT
    empty_text = "Sin videos: conectá un pendrive"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.mpris = Mpris(str(self.cfg.get("mpris_player") or "vlc"), float(self.cfg.get("volume_step", 0.05)))

    def installed(self) -> bool:
        return self._have("vlc")

    def default_args(self, folder: Path) -> list[str]:
        # -I dummy = sin ventana de VLC (solo el video). dbus = controles por MPRIS (playerctl).
        args = ["vlc", "-I", "dummy", "--extraintf=dbus", "--fullscreen", "--play-and-exit",
                "--video-title-timeout=3000", "--no-video-title-show" if self.cfg.get("hide_title") else "--video-title-show"]
        args.append("--random" if self.shuffle else "--no-random")
        return args + [str(folder)]

    async def close(self) -> None:
        if self.pids(fresh=True):
            await self.mpris.quit()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and self.pids(fresh=True):
                await asyncio.sleep(0.25)
        await self.terminate()

    async def pause(self) -> None:
        await self.mpris.ctl("pause")
        log.info("Video pausado por standby de la TV")


class PhotosActivity(MediaActivity):
    id = "fotos"
    name = "Fotos"
    icon = "fotos.svg"
    context = "fotos"
    procs = {"feh"}
    exts = IMAGE_EXT - {".heic"}  # feh (imlib2) no abre HEIC
    empty_text = "Sin fotos: conectá un pendrive"
    _exif: bool | None = None

    def installed(self) -> bool:
        return self._have("feh")

    @classmethod
    def feh_has_exif(cls) -> bool:
        """--auto-rotate solo existe si feh se compiló con EXIF (con otra opción desconocida no abre)."""
        if cls._exif is None:
            try:
                out = subprocess.run(["feh", "--version"], capture_output=True, text=True, timeout=5).stdout  # noqa: S603, S607
            except (OSError, subprocess.SubprocessError):
                out = ""
            cls._exif = "exif" in out
        return cls._exif

    def default_args(self, folder: Path) -> list[str]:
        delay = float(self.cfg.get("delay_s", 8))
        args = ["feh", "--fullscreen", "--hide-pointer", "--auto-zoom", "--recursive", "--quiet",
                "--image-bg", "black", "--slideshow-delay", f"{delay:g}", "--title", "netflix-cec fotos"]
        if self.feh_has_exif():
            args.append("--auto-rotate")
        args.append("--randomize" if self.shuffle else "--sort=filename")
        return args + [str(folder)]

    async def next(self) -> bool:
        return self.send_signal(signal.SIGUSR1)  # feh, en modo presentación: siguiente

    async def previous(self) -> bool:
        return self.send_signal(signal.SIGUSR2)  # anterior

    async def toggle_pause(self) -> bool:
        return await wtype_key("h")  # h = pausar o seguir la presentación


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


def build(ctl: Controller, cfg: Any) -> dict[str, Activity]:
    """Todas las actividades conocidas: las propias y las de [activities.<id>]."""
    apps = cfg.apps
    acts: dict[str, Activity] = {
        "netflix": NetflixActivity(ctl, apps.get("netflix")),
        "tidal": TidalActivity(ctl, apps.get("tidal")),
        "videos": VideosActivity(apps.get("videos")),
        "fotos": PhotosActivity(apps.get("fotos")),
    }
    for ident, item in (cfg.raw.get("activities") or {}).items():
        if not isinstance(item, dict):
            continue
        if ident in acts:
            log.warning("[activities.%s]: ese nombre es de una actividad propia; elegí otro", ident)
            continue
        if not item.get("command"):
            log.warning("[activities.%s]: falta command", ident)
            continue
        acts[ident] = CommandActivity(ident, item)
    return acts
