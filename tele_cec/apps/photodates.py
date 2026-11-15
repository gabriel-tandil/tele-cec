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
import functools
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..system.procs import LOG_DIR

log = logging.getLogger("actividades")

CACHE_VERSION = 1
SAVE_EVERY = 400       # guardar lo leído cada tantas fotos (si se corta, sigue desde ahí)
# De a poco: leer miles de fotos de otra máquina de un tirón trabó la red de la Pi (Wi‑Fi y Bluetooth comparten el
# chip en la Pi 400). Unas 4 por segundo (64 KB/s) y en pausa mientras se miran las fotos.
READ_PAUSE_S = 0.25
HOLD_CHECK_S = 5.0
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


def _tiff_info(t: bytes) -> dict:
    """Del bloque TIFF del EXIF: {"date": «AAAAMMDD»} (DateTimeOriginal, o DateTimeDigitized, o DateTime) y
    {"gps": (latitud, longitud)} si la foto tiene ubicación."""
    if len(t) < 8 or t[:2] not in (b"II", b"MM"):
        return {}
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

    def degrees(entry: tuple[int, int, int] | None) -> float | None:
        """Grados, minutos y segundos (tres fracciones) → grados."""
        if entry is None or entry[0] != 5 or entry[1] < 3:
            return None
        off = u32(entry[2])
        if off + 24 > len(t):
            return None
        parts = []
        for k in range(3):
            num, den = u32(off + 8 * k), u32(off + 8 * k + 4)
            if not den:
                return None
            parts.append(num / den)
        return parts[0] + parts[1] / 60 + parts[2] / 3600

    out: dict = {}
    ifd0 = ifd(u32(4))
    exif = ifd(u32(ifd0[0x8769][2])) if 0x8769 in ifd0 else {}
    for s in (text(exif.get(0x9003)), text(exif.get(0x9004)), text(ifd0.get(0x0132))):
        m = re.match(r"(\d{4}):(\d{2}):(\d{2})", s)
        if m and (got := _valid(*m.groups())):
            out["date"] = got
            break
    gps = ifd(u32(ifd0[0x8825][2])) if 0x8825 in ifd0 else {}
    lat, lon = degrees(gps.get(2)), degrees(gps.get(4))
    if lat is not None and lon is not None and (lat, lon) != (0, 0) and lat <= 90 and lon <= 180:
        out["gps"] = (round(-lat if text(gps.get(1)).upper().startswith("S") else lat, 6),
                      round(-lon if text(gps.get(3)).upper().startswith("W") else lon, 6))
    return out


def _tiff_date(t: bytes) -> str | None:
    return _tiff_info(t).get("date")


def _jpeg_exif(data: bytes, want: str = "date") -> tuple[object, bool]:
    """(lo pedido —"date" o "gps"—, ¿faltan bytes?) del EXIF de un JPEG: «faltan» si el bloque EXIF sigue más allá
    de lo leído."""
    if data[:2] != b"\xff\xd8":
        return None, False
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:  # empieza la imagen: no hay EXIF
            return None, False
        size = int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\0\0":
            got = _tiff_info(data[i + 10:i + 2 + size]).get(want)
            return got, got is None and i + 2 + size > len(data)
        i += 2 + size
    return None, i + 4 > len(data)


def _read_exif(path: str, want: str) -> object:
    """Lee primero 16 KB (casi siempre alcanza: la fecha y el GPS van antes de la miniatura) y solo si no alcanza,
    más: en una carpeta de red son miles de fotos."""
    if os.path.splitext(path)[1].lower() not in (".jpg", ".jpeg"):
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read(FIRST_BYTES)
            got, more = _jpeg_exif(data, want)
            if got is None and more and len(data) == FIRST_BYTES:
                got, _ = _jpeg_exif(data + fh.read(EXIF_BYTES - FIRST_BYTES), want)
    except OSError:
        return None
    return got


def exif_date(path: str) -> str | None:
    """«AAAAMMDD» de cuándo se sacó la foto (EXIF de un JPEG), o None."""
    got = _read_exif(path, "date")
    return got if isinstance(got, str) else None


def exif_gps(path: str) -> tuple[float, float] | None:
    """(latitud, longitud) de dónde se sacó la foto (GPS del EXIF), o None."""
    got = _read_exif(path, "gps")
    return got if isinstance(got, tuple) else None


def photo_date(path: str) -> str | None:
    """La fecha de una foto: del nombre o, si no, de la foto."""
    return name_date(path) or exif_date(path)


def month_day_text(day: dt.date) -> str:
    return f"{day.day} de {MONTHS[day.month - 1]}"


# ---------------------------------------------------------------- fechas incompletas (la fecha en la esquina)
# Cuando ni el nombre ni la foto dicen el día, a veces las carpetas dicen el mes o el año: «2003/Diciembre/…»,
# «2008-12 Navidad/…», «Vacaciones enero 2010/…», «Fotos 1998/…».
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTH_WORDS = {name: i + 1 for i, name in enumerate(MONTHS)} | {
    "setiembre": 9, "ene": 1, "feb": 2, "abr": 4, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "sept": 9, "oct": 10,
    "nov": 11, "dic": 12}   # sin «mar» (Mar del Plata), «may» ni «set»: son palabras comunes
_YEAR = r"((?:19|20)\d{2})"
_YM = re.compile(r"(?<!\d)" + _YEAR + r"[-_. ](0?[1-9]|1[0-2])(?![\d])")          # 2008-12 · 2008_3
_MY = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[-_. ]" + _YEAR + r"(?!\d)")           # 12-2008 · 3.2008
_WORD = re.compile(r"[a-záéíóúñ]+")
_YEAR_ALONE = re.compile(r"(?<!\d)" + _YEAR + r"(?!\d)")
_RANGE = re.compile(r"(?<!\d)" + _YEAR + r"\s*(?:-|–|\s+a\s+|\s+al\s+)\s*" + _YEAR + r"(?!\d)")   # 1980-2000
_PAREN_MY = re.compile(r"\((0[1-9]|1[0-2])-(\d{2})\)")   # «108 (09-08).jpg»: septiembre de 2008


def _year_ok(y: int) -> bool:
    return 1900 <= y <= dt.date.today().year + 1


def _month_word(text: str) -> int | None:
    """El mes escrito con letras en un nombre de carpeta («Diciembre», «vacaciones dic 2008»), o None."""
    for w in _WORD.findall(text.lower()):
        if w in _MONTH_WORDS:
            return _MONTH_WORDS[w]
    return None


_MONTH_NUM = re.compile(r"^(0?[1-9]|1[0-2])(?!\d)")   # «11», «03 Marzo», «11 - cumple»


@functools.lru_cache(maxsize=8192)
def _folder_info(parent: str) -> tuple[int, int | None, int | None] | None:
    """(año, mes o None, hasta qué año o None) según las carpetas (la más cercana primero); None si no dicen el año.
    «1980-2000» es un rango: (1980, None, 2000). Un número de mes solo («2009/11») cuenta si arriba está el año."""
    parts = Path(parent).parts[-4:]
    month: int | None = None
    for i in range(len(parts) - 1, -1, -1):
        part = parts[i]
        r = _RANGE.search(part)
        if r and _year_ok(int(r.group(1))) and int(r.group(1)) < int(r.group(2)) <= dt.date.today().year + 1:
            return int(r.group(1)), None, int(r.group(2))
        for rx, yi, mi in ((_YM, 1, 2), (_MY, 2, 1)):
            m = rx.search(part)
            if m and _year_ok(int(m.group(yi))):
                return int(m.group(yi)), month or int(m.group(mi)), None
        y = next((int(g) for g in _YEAR_ALONE.findall(part) if _year_ok(int(g))), None)
        if y is not None:
            return y, month or _month_word(part), None  # el año más cercano manda
        if month is None:
            month = _month_word(part)
            num = _MONTH_NUM.match(part)
            if month is None and num and i > 0 and _YEAR_ALONE.fullmatch(parts[i - 1].strip()):
                month = int(num.group(1))
    return None


def folder_parts(path: str) -> tuple[int, int | None, int | None] | None:
    """(año, mes, hasta qué año) que dicen las carpetas de esa foto (ver _folder_info)."""
    return _folder_info(os.path.dirname(path))


def plausible(full: str, path: str) -> bool:
    """¿La fecha del nombre o de la foto («AAAAMMDD») cuadra con el año de su carpeta? Las cámaras con el reloj sin
    poner dicen «1 de enero de 2002» en una foto guardada en «2007/10»: ahí manda la carpeta. Se admite el año de al
    lado en diciembre y enero (las del 31 de diciembre que quedaron en la carpeta del año siguiente). Y un «1 de enero»
    en la carpeta de otro mes («2007/10») tampoco: es el reloj reiniciado al principio de ese año."""
    info = folder_parts(path)
    if info is None:
        return True
    y, m = int(full[:4]), int(full[4:6])
    fy, fm, until = info
    if until:
        return fy <= y <= until
    if full[4:] == "0101" and fm is not None and fm not in (12, 1, 2):
        return False  # «1 de enero» en la carpeta de octubre: el reloj se reinició (al 1.º de enero de ese año)
    return y == fy or (y == fy - 1 and m == 12) or (y == fy + 1 and m == 1)


def date_parts(path: str, full: str | None = None) -> tuple[int, int | None, int | None, int | None] | None:
    """(año, mes, día, hasta qué año) de la foto con lo que se sepa:
    - el día exacto si el nombre o la foto dicen la fecha (`full`, «AAAAMMDD») y cuadra con su carpeta;
    - si no, el mes y el año de las carpetas («2003/Diciembre», «2009/11»), o del nombre («108 (09-08).jpg»);
    - o el año, o un rango de años («1980-2000/»: (1980, None, None, 2000)). None si no hay nada."""
    info = folder_parts(path)
    if full and len(full) == 8 and plausible(full, path):
        return int(full[:4]), int(full[4:6]), int(full[6:]), None
    m = _PAREN_MY.search(os.path.basename(path))
    if m:
        yy = int(m.group(2))
        y = 2000 + yy if 2000 + yy <= dt.date.today().year else 1900 + yy
        if info is None or (info[2] is None and info[0] == y) or (info[2] and info[0] <= y <= info[2]):
            return y, int(m.group(1)), None, None
    if info is None:
        return None
    return info[0], info[1], None, info[2]


def date_text(parts: tuple[int, int | None, int | None, int | None], today: dt.date | None = None) -> tuple[str, str]:
    """(la fecha, hace cuánto): («sábado 20 de diciembre de 2008», «hace 17 años»), («diciembre de 2008», …),
    («2008», …) o («entre 1980 y 2000», ""), según lo que se sepa."""
    today = today or dt.date.today()
    y, m, d, until = (tuple(parts) + (None,))[:4]
    if until:
        return f"entre {y} y {until}", ""
    if m and d:
        day = dt.date(y, m, d)
        big = f"{WEEKDAYS[day.weekday()]} {d} de {MONTHS[m - 1]} de {y}"
        years = today.year - y - ((today.month, today.day) < (m, d))
        days = (today - day).days
    elif m:
        big = f"{MONTHS[m - 1]} de {y}"
        years = today.year - y - (today.month < m)
        days = None
    else:
        big, years, days = str(y), today.year - y, None
    if years >= 1:
        ago = f"hace {years} año{'s' if years != 1 else ''}"
    elif days is not None and days >= 0:
        months = days // 30
        ago = ("hoy" if days == 0 else "ayer" if days == 1 else f"hace {days} días" if days < 60
               else f"hace {months} meses")
    else:
        ago = ""
    return big, ago


class PhotoDates:
    """{ruta: «AAAAMMDD» o ""} guardado entre usos; se completa en segundo plano."""

    def __init__(self, cache_file: Path | None = None):
        self.cache_file = cache_file or (LOG_DIR / "fotos-fechas.json")
        self._lock = threading.Lock()
        self._data: dict[str, str] | None = None
        self._thread: threading.Thread | None = None
        self.progress: tuple[int, int] = (0, 0)   # (leídas, por leer) mientras trabaja
        self.hold: Callable[[], bool] | None = None   # mientras devuelva True, no se lee (Fotos abierta)
        self.pause_s = READ_PAUSE_S

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
        t0, read, checked = time.monotonic(), 0, 0.0
        self.progress = (0, len(todo))
        for n, f in enumerate(todo, 1):
            got = name_date(f)
            from_photo = got is None
            if from_photo:
                if self.hold is not None and time.monotonic() - checked >= HOLD_CHECK_S:
                    while self.hold():  # Fotos abierta: esperar (se pregunta cada tanto, no en cada foto)
                        time.sleep(HOLD_CHECK_S)
                    checked = time.monotonic()
                got = exif_date(f) or ""
                read += 1
                if self.pause_s:
                    time.sleep(self.pause_s)
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
            if d and (int(d[4:6]), int(d[6:8])) in days and int(d[:4]) < today.year and plausible(d, f):
                hits.append((d, os.path.basename(f).lower(), f))
        return [f for _d, _n, f in sorted(hits)]

    def date_of(self, path: str) -> str | None:
        """La fecha de un archivo: la guardada o, si todavía no se leyó, del nombre o de la foto (lee el archivo)."""
        with self._lock:
            got = self._load().get(path)
        return got or (photo_date(path) if got is None else None)

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
