"""Las fechas de las fotos, para «Un día como hoy» (las fotos de esta fecha en otros años).

De dónde sale la fecha, en este orden:
- el nombre: IMG_20170226_141339.jpg, IMG-20180321-WA0001.jpg, VID_20201108…, «2017-02-26 14.13.39.jpg»;
- una carpeta con la fecha completa: «20-12-2008 presentacion/DSC02815.JPG»;
- la foto misma (EXIF: cuándo se sacó), para las de cámara que se llaman DSC02815.JPG o 100_4558.JPG.
Leer el EXIF de decenas de miles de fotos en otra máquina lleva unos minutos: se hace una vez, en segundo plano, y
queda en ~/.cache/tele-cec/fotos-fechas.json (las que se agregan después se leen solas, al actualizarse la lista).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from ..system.procs import LOG_DIR

log = logging.getLogger("actividades")

CACHE_VERSION = 1
SAVE_EVERY = 400       # guardar lo leído cada tantas fotos (si se corta, sigue desde ahí)
FIRST_BYTES = 16384    # lo que se lee primero de cada foto
EXIF_BYTES = 131072    # el EXIF va al principio del JPEG (con la miniatura, hasta 64 KB)
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre")
_YMD = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])")
_DMY = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])[-_.](0[1-9]|1[0-2])[-_.]((?:19|20)\d{2})(?!\d)")


def _valid(y: str, m: str, d: str) -> str | None:
    try:
        dt.date(int(y), int(m), int(d))
    except ValueError:
        return None
    return f"{y}{m}{d}" if 1990 <= int(y) <= 2100 else None


def name_date(path: str) -> str | None:
    """«AAAAMMDD» de la fecha que trae el nombre del archivo o de una de sus carpetas (o None)."""
    name = os.path.basename(path)
    for rx, order in ((_YMD, (0, 1, 2)), (_DMY, (2, 1, 0))):
        m = rx.search(name)
        if m and (got := _valid(*(m.group(i + 1) for i in order))):
            return got
    for part in reversed(Path(path).parent.parts[-3:]):  # «20-12-2008 presentacion», «2008-12-20 Cumple»
        for rx, order in ((_DMY, (2, 1, 0)), (_YMD, (0, 1, 2))):
            m = rx.search(part)
            if m and (got := _valid(*(m.group(i + 1) for i in order))):
                return got
    return None


def _tiff_date(t: bytes) -> str | None:
    """DateTimeOriginal (o DateTimeDigitized, o DateTime) del bloque TIFF del EXIF."""
    if len(t) < 8 or t[:2] not in (b"II", b"MM"):
        return None
    bo = "little" if t[:2] == b"II" else "big"

    def u16(o: int) -> int:
        return int.from_bytes(t[o:o + 2], bo)

    def u32(o: int) -> int:
        return int.from_bytes(t[o:o + 4], bo)

    def ifd(off: int) -> dict[int, tuple[int, int, int]]:
        if not 0 < off <= len(t) - 2:
            return {}
        out = {}
        for k in range(min(u16(off), 200)):
            e = off + 2 + 12 * k
            if e + 12 > len(t):
                break
            out[u16(e)] = (u16(e + 2), u32(e + 4), e + 8)
        return out

    def text(entry: tuple[int, int, int] | None) -> str:
        if entry is None or entry[0] != 2:
            return ""
        _typ, count, vo = entry
        off = vo if count <= 4 else u32(vo)
        return t[off:off + count].split(b"\0")[0].decode("ascii", "replace")

    ifd0 = ifd(u32(4))
    exif = ifd(u32(ifd0[0x8769][2])) if 0x8769 in ifd0 else {}
    for s in (text(exif.get(0x9003)), text(exif.get(0x9004)), text(ifd0.get(0x0132))):
        m = re.match(r"(\d{4}):(\d{2}):(\d{2})", s)
        if m and (got := _valid(*m.groups())):
            return got
    return None


def _jpeg_exif(data: bytes) -> tuple[str | None, bool]:
    """(fecha, ¿faltan bytes?) del EXIF de un JPEG: «faltan» si el bloque EXIF sigue más allá de lo leído."""
    if data[:2] != b"\xff\xd8":
        return None, False
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:  # empieza la imagen: no hay EXIF
            return None, False
        size = int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\0\0":
            got = _tiff_date(data[i + 10:i + 2 + size])
            return got, got is None and i + 2 + size > len(data)
        i += 2 + size
    return None, i + 4 > len(data)


def exif_date(path: str) -> str | None:
    """«AAAAMMDD» de cuándo se sacó la foto (EXIF de un JPEG), o None. Lee primero 16 KB (casi siempre alcanza: la
    fecha va antes de la miniatura) y solo si no alcanza, más: en una carpeta de red son miles de fotos."""
    if os.path.splitext(path)[1].lower() not in (".jpg", ".jpeg"):
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read(FIRST_BYTES)
            got, more = _jpeg_exif(data)
            if got is None and more and len(data) == FIRST_BYTES:
                got, _ = _jpeg_exif(data + fh.read(EXIF_BYTES - FIRST_BYTES))
    except OSError:
        return None
    return got


def month_day_text(day: dt.date) -> str:
    return f"{day.day} de {MONTHS[day.month - 1]}"


class PhotoDates:
    """{ruta: «AAAAMMDD» o ""} guardado entre usos; se completa en segundo plano."""

    def __init__(self, cache_file: Path | None = None):
        self.cache_file = cache_file or (LOG_DIR / "fotos-fechas.json")
        self._lock = threading.Lock()
        self._data: dict[str, str] | None = None
        self._thread: threading.Thread | None = None
        self.progress: tuple[int, int] = (0, 0)   # (leídas, por leer) mientras trabaja

    def _load(self) -> dict[str, str]:
        if self._data is None:
            try:
                raw = json.loads(self.cache_file.read_text("utf-8"))
                data = raw.get("files") if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION else None
            except (OSError, ValueError):
                data = None
            self._data = data if isinstance(data, dict) else {}
        return self._data

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_name(f"{self.cache_file.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": self._data}, ensure_ascii=False,
                                      separators=(",", ":")), "utf-8")
            tmp.replace(self.cache_file)
        except OSError as exc:
            log.warning("Fotos: no pude guardar las fechas (%s)", exc)

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def missing(self, files: list[str]) -> list[str]:
        with self._lock:
            data = self._load()
            return [f for f in files if f not in data]

    def update(self, files: list[str]) -> int:
        """Lee las fechas que faltan (bloquea: llamarla en un hilo). Devuelve cuántas leyó."""
        todo = self.missing(files)
        if not todo:
            return 0
        t0, read = time.monotonic(), 0
        self.progress = (0, len(todo))
        for n, f in enumerate(todo, 1):
            got = name_date(f)
            from_photo = got is None
            if from_photo:
                got = exif_date(f) or ""
                read += 1
            with self._lock:
                self._load()[f] = got
                if from_photo and read % SAVE_EVERY == 0:
                    self._save()
            self.progress = (n, len(todo))
        with self._lock:
            self._save()
        log.info("Fotos: fechas de %d archivos (%d leídas de la foto, %.0f s)", len(todo), read, time.monotonic() - t0)
        return len(todo)

    def start(self, files: list[str]) -> bool:
        """Completa las que faltan en segundo plano (una sola vez a la vez). True si quedó trabajando."""
        if self.busy():
            return True
        if not self.missing(files):
            return False
        self._thread = threading.Thread(target=self.update, args=(files,), name="fotos-fechas", daemon=True)
        self._thread.start()
        return True

    def on_this_day(self, files: list[str], today: dt.date | None = None, spread: int = 0) -> list[str]:
        """Las fotos y videos de esta fecha (± spread días) de otros años, de la más vieja a la más nueva."""
        today = today or dt.date.today()
        days = {((today + dt.timedelta(days=k)).month, (today + dt.timedelta(days=k)).day)
                for k in range(-spread, spread + 1)}
        with self._lock:
            data = self._load()
        hits = []
        for f in files:
            d = data.get(f)
            if d is None:
                d = name_date(f) or ""
            if d and (int(d[4:6]), int(d[6:8])) in days and int(d[:4]) < today.year:
                hits.append((d, os.path.basename(f).lower(), f))
        return [f for _d, _n, f in sorted(hits)]

    def years(self, files: list[str]) -> list[int]:
        """Los años de esas fotos (para decir «de 2014, 2017 y 2021»)."""
        with self._lock:
            data = self._load()
        return sorted({int((data.get(f) or name_date(f) or "0")[:4]) for f in files} - {0})


_dates: PhotoDates | None = None


def dates() -> PhotoDates:
    global _dates  # noqa: PLW0603
    if _dates is None:
        _dates = PhotoDates()
    return _dates
