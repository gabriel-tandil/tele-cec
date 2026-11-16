"""La tapa del disco de lo que suena en la Radio, buscada en línea.

Las emisoras mandan solo el texto «Artista - Tema» (ICY), sin imagen. Con eso se busca en iTunes
(https://itunes.apple.com/search, público, sin clave ni cuenta) y se usa la tapa del disco que devuelve. Se acepta
solo si el artista que dice iTunes se parece al que mandó la emisora: así una tanda, un aviso o un nombre raro no
salen con la tapa de cualquier otro disco. Lo que sale de la casa es el nombre del artista y del tema, nada más.

Las tapas quedan en ~/.cache/tele-cec/radio-tapas (y las búsquedas sin resultado, para no repetirlas).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..system.procs import LOG_DIR
from .library import fold

log = logging.getLogger("actividades")

SEARCH = "https://itunes.apple.com/search?media=music&entity=song&limit=5&term="
ART_SIZE = "600x600bb"      # iTunes da la tapa chica (100x100bb); con este nombre, la grande
USER_AGENT = "tele-cec"
TIMEOUT_S = 8
DIR_NAME = "radio-tapas"
INDEX = "indice.json"
MAX_JPG = 4 * 1024 * 1024


def song_parts(icy_title: str) -> tuple[str, str] | None:
    """«Artista - Tema» → (artista, tema); None si no tiene el guion (sin artista no se busca: saldría cualquiera)."""
    text = re.sub(r"\s+", " ", str(icy_title or "")).strip()
    m = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", text)
    if not m:
        return None
    artist, title = m.group(1).strip(), m.group(2).strip()
    return (artist, title) if artist and title else None


def _clean(text: str) -> str:
    """Sin «(feat. …)», «[Remaster]» ni «- Live»: iTunes encuentra más con el nombre pelado."""
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    text = re.sub(r"\b(feat\.?|ft\.?|featuring)\b.*$", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _similar(a: str, b: str) -> bool:
    """El artista de iTunes se parece al de la emisora: uno contiene al otro (sin tildes ni mayúsculas), o
    comparten la primera palabra larga («Los Pericos» ~ «Pericos», «Charly García» ~ «Charly Garcia»)."""
    fa, fb = fold(_clean(a)), fold(_clean(b))
    if not fa or not fb:
        return False
    if fa in fb or fb in fa:
        return True
    wa = [w for w in re.split(r"\W+", fa) if len(w) > 3]
    wb = [w for w in re.split(r"\W+", fb) if len(w) > 3]
    return bool(wa and wb and set(wa) & set(wb))


def cache_dir() -> Path:
    return LOG_DIR / DIR_NAME


def _key(artist: str, title: str) -> str:
    return hashlib.md5(f"{fold(artist)}|{fold(title)}".encode()).hexdigest()[:16]


def _load_index() -> dict[str, str]:
    try:
        data = json.loads((cache_dir() / INDEX).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(index: dict[str, str]) -> None:
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        tmp = cache_dir() / (INDEX + ".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False), "utf-8")
        tmp.replace(cache_dir() / INDEX)
    except OSError as exc:
        log.debug("Tapas: no pude guardar el índice (%s)", exc)


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310  (solo https de iTunes)
        return r.read(MAX_JPG + 1)


def search(artist: str, title: str) -> dict[str, Any] | None:
    """El resultado de iTunes que corresponde (mismo artista), o None. Puede tirar (sin red, etc.)."""
    term = urllib.parse.quote_plus(f"{_clean(artist)} {_clean(title)}")
    data = json.loads(_fetch(SEARCH + term).decode("utf-8", "replace"))
    for res in data.get("results") or []:
        if not isinstance(res, dict) or not str(res.get("artworkUrl100") or "").startswith("https://"):
            continue
        if _similar(str(res.get("artistName") or ""), artist):
            return res
    return None


def find(artist: str, title: str) -> Path | None:
    """La tapa (JPEG) de ese tema, de la caché o buscándola en iTunes; None si no hay (o no se pudo)."""
    key = _key(artist, title)
    index = _load_index()
    if key in index:
        if not index[key]:
            return None
        path = cache_dir() / index[key]
        if path.is_file():
            return path
    try:
        res = search(artist, title)
    except Exception as exc:  # noqa: BLE001  (sin red o iTunes caído: la próxima vez se vuelve a probar)
        log.info("Tapas: no pude buscar «%s - %s» (%s)", artist, title, exc)
        return None
    if res is None:
        log.info("Tapas: iTunes no tiene «%s - %s»", artist, title)
        index[key] = ""
        _save_index(index)
        return None
    url = str(res["artworkUrl100"]).replace("100x100bb", ART_SIZE)
    try:
        data = _fetch(url)
    except Exception as exc:  # noqa: BLE001
        log.info("Tapas: no pude bajar la tapa de «%s - %s» (%s)", artist, title, exc)
        return None
    if len(data) > MAX_JPG or not data.startswith((b"\xff\xd8", b"\x89PNG")):
        return None
    name = key + (".png" if data.startswith(b"\x89PNG") else ".jpg")
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        (cache_dir() / name).write_bytes(data)
    except OSError as exc:
        log.warning("Tapas: no pude guardar la tapa (%s)", exc)
        return None
    index[key] = name
    _save_index(index)
    log.info("Tapas: «%s - %s» → %s (%s)", artist, title, res.get("collectionName") or "?", name)
    return cache_dir() / name


def raw_bgra(image: Path, size: int) -> Path | None:
    """La imagen como píxeles BGRA de size × size (lo que mpv dibuja con overlay-add), con las puntas redondeadas.
    Queda al lado de la tapa; None si falta Pillow o la imagen no se puede leer."""
    out = image.with_name(f"{image.stem}.{size}.bgra")
    if out.is_file() and out.stat().st_mtime >= image.stat().st_mtime:
        return out
    try:
        from PIL import Image, ImageDraw  # noqa: PLC0415
    except ImportError:
        log.info("Tapas: sin Pillow (python3-pil) no se dibujan en la TV")
        return None
    try:
        with Image.open(image) as im:
            im = im.convert("RGBA").resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 24, fill=255)
            im.putalpha(mask)
            tmp = out.with_suffix(".tmp")
            tmp.write_bytes(im.tobytes("raw", "BGRA"))
            tmp.replace(out)
    except (OSError, ValueError) as exc:
        log.warning("Tapas: no pude convertir %s (%s)", image.name, exc)
        return None
    return out
