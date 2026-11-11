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
import hashlib
import logging
import os
import shlex
import shutil
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .browser import LOG_DIR, spawn_scoped
from .music import Mpris, find_pids, invalidate_procs, xdotool_key

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


def scan_media(folder: Path, exts: set[str]) -> list[str]:
    """Todos los archivos con esas extensiones (recorre todo: en una carpeta de red grande tarda)."""
    out = []
    for root, dirs, files in os.walk(folder, onerror=lambda e: None):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        out += [os.path.join(root, f) for f in sorted(files)
                if not f.startswith(".") and os.path.splitext(f)[1].lower() in exts and "\n" not in f]
    return out


def find_media_folder(patterns: list[str] | str, exts: set[str]) -> Path | None:
    """La primera carpeta de la lista que tenga archivos de ese tipo (los pendrives van primero)."""
    for folder in expand_folders(patterns):
        if count_media(folder, exts):
            return folder
    return None


NETWORK_FS = {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs", "fuse.rclone", "autofs", "davfs"}


def _fs_type(folder: Path) -> str:
    """Tipo de sistema de archivos donde está la carpeta (el montaje más largo que la contiene)."""
    best, fstype = "", ""
    try:
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (str(folder) == mnt or str(folder).startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                    best, fstype = mnt, parts[2]
    except OSError:
        pass
    return fstype


def describe_folder(folder: Path) -> str:
    s = str(folder)
    if _fs_type(folder) in NETWORK_FS:
        return f"En red: {folder.name}"
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
            await spawn_scoped(args, f"netflix-cec-{self.id}", log=self.id)
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
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        self.rc_socket = Path(runtime) / "netflix-cec-vlc.sock"

    def installed(self) -> bool:
        return self._have("vlc")

    def default_args(self, folder: Path) -> list[str]:
        # -I dummy = sin ventana de VLC (solo el video). dbus = controles por MPRIS (playerctl). oldrc = comandos
        # por un socket local para pistas y proporción (en Wayland el video de VLC no recibe teclas).
        with contextlib.suppress(OSError):
            self.rc_socket.unlink()
        args = ["vlc", "-I", "dummy", "--extraintf=dbus:oldrc", f"--rc-unix={self.rc_socket}", "--rc-fake-tty",
                "--fullscreen", "--play-and-exit",
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

    async def rc(self, command: str, timeout: float = 1.5) -> str:
        """Un comando a la interfaz de comandos de VLC (socket UNIX); devuelve lo que contesta."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.rc_socket)), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("VLC no responde en %s: %s", self.rc_socket, exc)
            return ""
        out = b""
        try:
            writer.write((command + "\n").encode())
            await writer.drain()
            # Al conectarse VLC manda primero mensajes de estado; la respuesta termina con «<comando>: returned».
            deadline = time.monotonic() + timeout
            while b"returned" not in out and time.monotonic() < deadline:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), max(0.05, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                out += chunk
        finally:
            writer.close()
        return out.decode("utf-8", "replace")

    PAUSED = "\0pausa"  # cycle(): VLC no cambia pistas en pausa

    async def cycle(self, kind: str) -> str:
        """Pasa a la siguiente pista de audio (atrack), de subtítulos (strack) o proporción (vratio).

        VLC contesta la lista con la actual marcada con «*»: «| 2 - Español *». Devuelve el nombre de la
        elegida ("" si no hay lista, por ejemplo en pausa: VLC no cambia pistas así).
        """
        items: list[tuple[str, str, bool]] = []
        reply = await self.rc(kind)
        for line in reply.splitlines():
            line = line.rstrip()
            if not line.startswith("|") or " - " not in line:
                continue
            # «|  - Predeterminado *»: el valor puede estar vacío, así que se parte antes de recortar.
            ident, _, name = line[1:].partition(" - ")
            current = name.endswith(" *")
            items.append((ident.strip(), name.removesuffix(" *").strip(), current))
        if kind == "vratio":
            items = [it for it in items if it[0]]  # «Predeterminado» no tiene valor para volver a elegirlo
        elif kind == "atrack":
            items = [it for it in items if it[0] != "-1"]  # «Deshabilitar» = sin sonido: no va en el ciclo
        if not items:
            return self.PAUSED if "paus" in reply.lower() else ""
        idx = next((i for i, it in enumerate(items) if it[2]), -1)
        ident, name, _ = items[(idx + 1) % len(items)]
        await self.rc(f"{kind} {ident}")
        return name


class PhotosActivity(MediaActivity):
    """Presentación con feh. Con ▲ se hace zoom: la presentación se pausa y las flechas recorren la foto."""

    id = "fotos"
    name = "Fotos"
    icon = "fotos.svg"
    procs = {"feh"}
    exts = IMAGE_EXT - {".heic"}  # feh (imlib2) no abre HEIC
    empty_text = "Sin fotos: conectá un pendrive"

    ZOOM_MAX = 12          # pasos de zoom (cada uno +25 % en feh): ~14 veces la foto ajustada
    RENDER_DELAY = 0.6     # s sin moverse antes de redibujar suavizado
    # Atajos de feh: el teclado numérico desplaza la foto (las flechas solas cambian de foto o hacen zoom).
    SCROLL_KEYS = {"left": "KP_Left", "right": "KP_Right", "up": "KP_Up", "down": "KP_Down"}

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self._render_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Future | None = None
        self.reset_view()

    def reset_view(self) -> None:
        self.paused = False         # la presentación, según lo que le mandamos (feh arranca andando)
        self.zoom = 0               # pasos de zoom sobre la foto ajustada a la pantalla (0 = sin zoom)
        self._zoom_paused = False   # la pausó el zoom: se retoma al salir
        self._cancel_render()

    @property  # type: ignore[override]
    def context(self) -> str:
        return "fotos_zoom" if self.zoom else "fotos"

    def installed(self) -> bool:
        return self._have("feh")

    def default_args(self, folder: Path, filelist: Path | None = None) -> list[str]:
        delay = float(self.cfg.get("delay_s", 8))
        step = int(self.cfg.get("scroll_step", 150))
        args = ["feh", "--fullscreen", "--hide-pointer", "--auto-zoom", "--quiet",
                "--image-bg", "black", "--slideshow-delay", f"{delay:g}", "--scroll-step", str(step),
                "--title", "netflix-cec fotos"]
        if self.cfg.get("zoom_step"):
            args += ["--zoom-step", f"{float(self.cfg['zoom_step']):g}"]
        # Sin --auto-rotate: con Imlib2 ≥ 1.7.5 (Debian trixie) las fotos ya salen derechas según el EXIF.
        args.append("--randomize" if self.shuffle else "--sort=filename")
        if filelist is not None:
            return args + ["--filelist", str(filelist)]
        return args + ["--recursive", str(folder)]

    # ------------------------------------------------------------ lista de fotos guardada
    # feh con --recursive recorre toda la carpeta antes de mostrar la primera foto: en una carpeta de red
    # con decenas de miles de fotos son 15-20 s de pantalla vacía. Por eso la lista se arma una vez (con el
    # menú mostrando «Abriendo Fotos…»), se guarda en ~/.cache/netflix-cec y feh arranca con --filelist al
    # instante; después de cada apertura se actualiza en segundo plano.
    def list_file(self, folder: Path) -> Path:
        return LOG_DIR / f"fotos-{hashlib.md5(str(folder).encode()).hexdigest()[:10]}.lst"

    def write_list(self, folder: Path) -> int:
        t0 = time.monotonic()
        files = scan_media(folder, self.exts)
        if not files:
            return 0
        dest = self.list_file(folder)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_text("\n".join(files) + "\n", encoding="utf-8")
        tmp.replace(dest)
        log.info("Fotos: lista de %s actualizada (%d fotos, %.1f s)", describe_folder(folder), len(files),
                 time.monotonic() - t0)
        return len(files)

    async def open(self) -> bool:
        self.reset_view()
        if self.command:
            return await super().open()
        folder = await asyncio.to_thread(self.folder)
        if folder is None:
            log.warning("%s: no hay contenido en %s", self.name, ", ".join(map(str, self.folders)) or "(sin carpetas)")
            return False
        saved = self.list_file(folder)
        had_list = saved.is_file() and saved.stat().st_size > 0
        if not had_list:
            log.info("Fotos: armo la lista de %s (solo la primera vez tarda)", describe_folder(folder))
            if not await asyncio.to_thread(self.write_list, folder):
                return False
        # feh reescribe su --filelist al cerrarse: se le da una copia para que no pise la actualización.
        for_feh = saved.with_suffix(".feh.lst")
        try:
            await asyncio.to_thread(shutil.copyfile, saved, for_feh)
        except OSError as exc:
            log.warning("Fotos: no pude preparar la lista (%s): uso la carpeta directamente", exc)
            return await self.spawn(self.default_args(folder))
        ok = await self.spawn(self.default_args(folder, for_feh))
        if ok and had_list:
            self._refresh_task = asyncio.get_running_loop().create_task(asyncio.to_thread(self.write_list, folder))
        return ok

    async def close(self) -> None:
        self.reset_view()
        await super().close()

    async def key(self, name: str) -> bool:
        """Un atajo de teclado de feh. feh es X11 (Xwayland): va con xdotool; con wtype le llega Escape y se cierra."""
        return await xdotool_key(name)

    async def next(self) -> bool:
        return self.send_signal(signal.SIGUSR1)  # feh, en modo presentación: siguiente

    async def previous(self) -> bool:
        return self.send_signal(signal.SIGUSR2)  # anterior

    async def toggle_pause(self) -> bool:
        ok = await self.key("h")  # h = pausar o seguir la presentación
        if ok:
            self.paused = not self.paused
            self._zoom_paused = False
        return ok

    # ------------------------------------------------------------ zoom
    async def zoom_in(self) -> bool:
        """Acerca un paso. El primero pausa la presentación (si andaba) para que no cambie de foto."""
        if self.zoom >= self.ZOOM_MAX:
            return False
        if not self.zoom and not self.paused:
            if not await self.key("h"):
                return False
            self.paused = self._zoom_paused = True
        if not await self.key("Up"):  # Up = zoom_in
            return False
        self.zoom += 1
        return True

    async def zoom_out(self) -> bool:
        """Aleja un paso; desde el primero vuelve a la foto entera (y sigue la presentación si el zoom la pausó)."""
        if not self.zoom:
            return False
        if self.zoom == 1:
            return await self.fit()
        if not await self.key("Down"):  # Down = zoom_out
            return False
        self.zoom -= 1
        return True

    async def fit(self) -> bool:
        """Sale del zoom: la foto entera, ajustada a la pantalla."""
        if not self.zoom:
            return False
        await self.key("KP_Divide")  # zoom_fit
        await self.photo_changed()
        return True

    async def scroll(self, direction: str) -> bool:
        """Mueve la parte de la foto que se ve (solo con zoom)."""
        name = self.SCROLL_KEYS.get(direction)
        if not self.zoom or not name:
            return False
        ok = await self.key(name)
        # feh desplaza sin suavizado (más rápido); cuando se deja de mover, R la redibuja bien.
        self._cancel_render()
        self._render_task = asyncio.get_running_loop().create_task(self._render_later())
        return ok

    async def photo_changed(self) -> None:
        """Otra foto (o la misma girada o ajustada): feh la muestra entera, así que se termina el zoom."""
        self._cancel_render()
        if not self.zoom and not self._zoom_paused:
            return
        self.zoom = 0
        if self._zoom_paused and self.paused and await self.key("h"):
            self.paused = False
        self._zoom_paused = False

    async def _render_later(self) -> None:
        await asyncio.sleep(self.RENDER_DELAY)
        self._render_task = None
        if self.zoom:
            await self.key("R")  # render

    def _cancel_render(self) -> None:
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
        self._render_task = None


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
