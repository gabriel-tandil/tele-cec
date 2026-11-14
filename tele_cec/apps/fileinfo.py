"""Lo que dicen los archivos, para mostrarlo en las listas del menú (Música y Videos).

Temas: las etiquetas (título, artista, disco, duración) con mutagen; si la biblioteca de Música ya las leyó, salen de
su índice (~/.cache/tele-cec/musica-indice.json). Videos: duración, tamaño de la imagen y título con ffprobe (o, sin
ffprobe, lo que mutagen sepa de los MP4). Lo leído de los videos se guarda en ~/.cache/tele-cec/videos-info.json
(por ruta, tamaño y fecha).

Todo con tope de tiempo: lo que no se llega a leer se muestra sin datos (los videos se siguen leyendo de fondo y la
próxima vez ya aparecen).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from ..system.procs import LOG_DIR
from .library import read_tags

log = logging.getLogger("actividades")

TAGS_BUDGET_S = 1.5     # leer las etiquetas de una carpeta de temas
VIDEO_BUDGET_S = 2.0    # esperar a ffprobe (lo que falte sigue de fondo)
PROBE_TIMEOUT_S = 20
CACHE_VERSION = 1


def _sig(path: str) -> list[int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, int(st.st_mtime)]


def duration_text(seconds: float) -> str:
    """«3:45», «1:02:10» (temas)."""
    s = int(round(seconds))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _long_duration(seconds: float) -> str:
    """«1 h 49 min», «52 min» (videos)."""
    m = int(round(seconds / 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{max(1, m)} min"


def quality(width: int, height: int) -> str:
    """«4K», «1080p», «720p», «480p»… (por el lado más largo, así un video recortado sigue siendo 1080p)."""
    if not width or not height:
        return ""
    w = max(width, height * 16 / 9)
    for label, need in (("4K", 3800), ("1440p", 2500), ("1080p", 1800), ("720p", 1200), ("576p", 1000)):
        if w >= need:
            return label
    return f"{min(width, height)}p"


# ---------------------------------------------------------------- temas
def song_tags(paths: list[str], index_file: Path | None = None) -> dict[str, dict[str, Any]]:
    """Las etiquetas de esos temas: del índice de la biblioteca si están al día; si no, leyéndolas (con tope)."""
    try:
        data = json.loads((index_file or LOG_DIR / "musica-indice.json").read_text("utf-8"))
        index = data.get("files") if isinstance(data, dict) else None
    except (OSError, ValueError):
        index = None
    index = index if isinstance(index, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + TAGS_BUDGET_S
    for p in paths:
        hit = index.get(p)
        if isinstance(hit, dict) and hit.get("sig") == _sig(p):
            out[p] = hit.get("tags") or {}
        elif time.monotonic() < deadline:
            out[p] = read_tags(p)
    return out


# ---------------------------------------------------------------- videos
def _ffprobe(path: str) -> dict[str, Any] | None:
    """Lo que dice ffprobe (None si no está o no pudo)."""
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                           capture_output=True, timeout=PROBE_TIMEOUT_S, check=False)
        data = json.loads(r.stdout or b"{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    fmt = data.get("format") or {}
    info: dict[str, Any] = {}
    try:
        info["dur"] = round(float(fmt.get("duration") or 0), 1)
    except (TypeError, ValueError):
        pass
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    if str(tags.get("title") or "").strip():
        info["title"] = str(tags["title"]).strip()
    for st in data.get("streams") or []:
        if st.get("codec_type") == "video" and not (st.get("disposition") or {}).get("attached_pic"):
            info["w"], info["h"] = int(st.get("width") or 0), int(st.get("height") or 0)
            break
    subs = [st for st in data.get("streams") or [] if st.get("codec_type") == "subtitle"]
    if subs:
        info["subs"] = len(subs)
    return {k: v for k, v in info.items() if v}


def _mutagen_video(path: str) -> dict[str, Any]:
    tags = read_tags(path)  # MP4/M4V/MOV: duración y título
    return {k: v for k, v in (("dur", tags.get("dur")), ("title", tags.get("title"))) if v}


class VideoInfo:
    """Duración, tamaño de imagen y título de los videos, guardados entre usos."""

    def __init__(self, cache_file: Path | None = None):
        self.cache_file = cache_file or (LOG_DIR / "videos-info.json")
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ffprobe")
        self._pending: set[str] = set()

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            try:
                data = json.loads(self.cache_file.read_text("utf-8"))
                files = data.get("files") if isinstance(data, dict) and data.get("version") == CACHE_VERSION else None
            except (OSError, ValueError):
                files = None
            self._data = files if isinstance(files, dict) else {}
        return self._data

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": self._data}, ensure_ascii=False), "utf-8")
            tmp.replace(self.cache_file)
        except OSError as exc:
            log.warning("Videos: no pude guardar los datos de los videos (%s)", exc)

    def _probe(self, path: str, sig: list[int]) -> None:
        info = _ffprobe(path)
        if info is None:
            info = _mutagen_video(path)
        with self._lock:
            data = self._load()
            data[path] = {"sig": sig, "info": info}
            for p in [p for p in data if not os.path.exists(p)][:200]:  # los que ya no están
                del data[p]
            self._save()
            self._pending.discard(path)

    def get(self, paths: list[str], budget: float = VIDEO_BUDGET_S) -> dict[str, dict[str, Any]]:
        """Lo que se sepa de cada video; los que faltan se leen (se espera hasta budget segundos)."""
        out: dict[str, dict[str, Any]] = {}
        futures = []
        with self._lock:
            data = self._load()
            for p in paths:
                sig = _sig(p)
                hit = data.get(p)
                if isinstance(hit, dict) and hit.get("sig") == sig:
                    out[p] = hit.get("info") or {}
                elif sig is not None and p not in self._pending:
                    self._pending.add(p)
                    futures.append(self._pool.submit(self._probe, p, sig))
        if futures and budget > 0:
            wait(futures, timeout=budget)
            with self._lock:
                data = self._load()
                for p in paths:
                    if p not in out and isinstance(data.get(p), dict) and data[p].get("sig") == _sig(p):
                        out[p] = data[p].get("info") or {}
        return out


_video_info: VideoInfo | None = None


def video_info() -> VideoInfo:
    global _video_info  # noqa: PLW0603
    if _video_info is None:
        _video_info = VideoInfo()
    return _video_info


def video_text(info: dict[str, Any]) -> list[str]:
    """«1 h 49 min», «1080p» (para el renglón de abajo)."""
    parts = []
    if info.get("dur"):
        parts.append(_long_duration(info["dur"]))
    q = quality(int(info.get("w") or 0), int(info.get("h") or 0))
    if q:
        parts.append(q)
    return parts
