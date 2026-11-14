"""Carpetas de contenido (Videos y Fotos): cuáles existen, si tienen archivos, cómo se llaman en el menú."""

from __future__ import annotations

import fnmatch
import glob
import os
import time
from pathlib import Path

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".wmv",
             ".flv", ".ogv", ".3gp", ".vob", ".divx"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".pnm", ".ppm", ".heic"}
AUDIO_EXT = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".aif", ".ape",
             ".alac", ".mka", ".wv", ".mpc"}

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


def _natural_key(name: str) -> list:
    """Orden natural: «2» antes que «10», sin importar mayúsculas."""
    import re  # noqa: PLC0415
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_subfolders(folder: Path, limit: int = 300) -> list[Path]:
    """Subcarpetas (no ocultas) en orden natural, para elegir una desde el menú."""
    try:
        with os.scandir(folder) as it:
            dirs = [e.name for e in it if not e.name.startswith(".") and e.is_dir(follow_symlinks=True)]
    except OSError:
        return []
    return [folder / d for d in sorted(dirs, key=_natural_key)[:limit]]


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
