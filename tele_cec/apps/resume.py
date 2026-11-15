"""Seguir viendo (Videos): dónde quedó cada video que se cortó a la mitad.

Lo guarda mpv al cerrarlo (--save-position-on-quit) en ~/.cache/tele-cec/videos-seguir: un archivo por video, con
su ruta («# /ruta», por --write-filename-in-watch-later-config) y «start=segundos» (y las pistas de audio y
subtítulos elegidas). Al volver a abrir ese video mpv sigue solo desde ahí y borra el archivo (al cerrarlo de nuevo
lo vuelve a escribir); si el video termina, no queda nada. Acá solo se leen (para el menú y el celular) y se borran
(para «Desde el principio»). mpv también deja «redirect entry» de las carpetas: no sirven y se borran al leer.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ..system.procs import LOG_DIR

log = logging.getLogger("actividades")

WATCH_DIR = LOG_DIR / "videos-seguir"
MIN_SECONDS = 60      # menos de un minuto visto: como si no se hubiera empezado
END_SECONDS = 180     # a menos de 3 minutos del final (los créditos): como si se hubiera terminado
END_FRACTION = 0.97


def mpv_args(watch_dir: Path | None = None) -> list[str]:
    d = watch_dir or WATCH_DIR
    return ["--save-position-on-quit", f"--watch-later-dir={d}", "--write-filename-in-watch-later-config",
            "--watch-later-options=start,aid,sid,sub-visibility"]


def _read(watch_dir: Path) -> list[tuple[Path, str, float, float]]:
    """(archivo de mpv, ruta del video, segundos, cuándo se guardó)."""
    out = []
    try:
        entries = list(os.scandir(watch_dir))
    except OSError:
        return []
    for e in entries:
        if not e.is_file():
            continue
        try:
            text = Path(e.path).read_text("utf-8", errors="replace")
            saved = e.stat().st_mtime
        except OSError:
            continue
        if text.startswith("# redirect entry"):
            Path(e.path).unlink(missing_ok=True)
            continue
        path = next((line[2:].strip() for line in text.splitlines() if line.startswith("# /")), "")
        m = re.search(r"^start=([\d.]+)", text, re.MULTILINE)
        if path and m:
            out.append((Path(e.path), path, float(m.group(1)), saved))
    return out


def positions(watch_dir: Path | None = None) -> dict[str, float]:
    """{ruta del video: segundos} de los que quedaron a la mitad (vistos al menos un minuto)."""
    return {path: secs for _f, path, secs, _t in _read(watch_dir or WATCH_DIR) if secs >= MIN_SECONDS}


def recent(limit: int = 3, watch_dir: Path | None = None) -> list[tuple[str, float]]:
    """Los últimos que quedaron a la mitad (el más reciente primero) y que siguen estando."""
    found = sorted(_read(watch_dir or WATCH_DIR), key=lambda x: -x[3])
    return [(path, secs) for _f, path, secs, _t in found if secs >= MIN_SECONDS and os.path.isfile(path)][:limit]


def finished(seconds: float, duration: float | None) -> bool:
    """Quedó en los créditos: no hace falta ofrecer seguir."""
    return bool(duration) and (seconds >= duration * END_FRACTION or duration - seconds <= END_SECONDS)


def forget(path: str, watch_dir: Path | None = None) -> bool:
    """«Desde el principio»: borra dónde quedó (mpv lo abre desde el comienzo)."""
    gone = False
    for f, p, _s, _t in _read(watch_dir or WATCH_DIR):
        if p == path:
            f.unlink(missing_ok=True)
            gone = True
    if gone:
        log.info("Videos: %s, desde el principio", os.path.basename(path))
    return gone
