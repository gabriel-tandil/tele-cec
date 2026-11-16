"""El ícono de cada sitio guardado (favicon), para la lista de sitios del menú, del celular y la página de inicio.

Se le pide al sitio mismo (nada de servicios de terceros): su página principal dice cuál es su ícono
(<link rel="apple-touch-icon"> suele ser el más grande y nítido; si no, rel="icon"), y si no dice nada, /favicon.ico.
Se convierte a PNG cuadrado de 128 px con Pillow y queda en ~/.cache/tele-cec/favicons/<sitio>.png. Si no se pudo,
se recuerda y se vuelve a probar a la semana.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ...system.procs import LOG_DIR

log = logging.getLogger("actividades")

SIZE = 128
GOOD_PX = 96        # uno de este tamaño o más ya está bien: no se sigue buscando
TIMEOUT_S = 8
MAX_BYTES = 1024 * 1024
RETRY_S = 7 * 24 * 3600
USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64; rv:128.0) Gecko/20100101 Firefox/128.0"
_HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}(:\d{1,5})?$")
_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-z-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.IGNORECASE)
_lock = threading.Lock()
_busy: set[str] = set()


def cache_dir() -> Path:
    return LOG_DIR / "favicons"


def host_of(url: str) -> str:
    try:
        host = (urllib.parse.urlparse(url).netloc or "").lower()
    except ValueError:
        return ""
    return host if _HOST_RE.match(host) else ""


def path_for(host: str) -> Path | None:
    """El ícono ya bajado de ese sitio, o None."""
    if not _HOST_RE.match(host or ""):
        return None
    p = cache_dir() / f"{host.replace(':', '_')}.png"
    return p if p.is_file() and p.stat().st_size > 0 else None


def _failed_recently(host: str) -> bool:
    mark = cache_dir() / f"{host.replace(':', '_')}.no"
    try:
        return time.time() - mark.stat().st_mtime < RETRY_S
    except OSError:
        return False


def _fetch(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "es-AR,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310  (solo http(s) de los sitios guardados)
        return r.read(MAX_BYTES + 1), r.geturl()


def icon_links(page: str, base: str) -> list[str]:
    """Los íconos que declara la página, del mejor al peor: apple-touch-icon, después rel=icon por tamaño (sin SVG,
    que Pillow no abre)."""
    found: list[tuple[int, str]] = []
    for tag in _LINK_RE.findall(page[:300_000]):
        attrs = {m.group(1).lower(): (m.group(3) or m.group(4) or m.group(5) or "") for m in _ATTR_RE.finditer(tag)}
        rel = attrs.get("rel", "").lower().split()
        href = attrs.get("href", "").strip()
        if not href or "icon" not in " ".join(rel) or "mask-icon" in rel:
            continue
        if href.lower().split("?")[0].endswith(".svg") or "svg" in attrs.get("type", "").lower():
            continue
        sizes = [int(n) for n in re.findall(r"(\d+)x\d+", attrs.get("sizes", ""))]
        score = max(sizes) if sizes else (180 if "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel else 32)
        found.append((score, urllib.parse.urljoin(base, href)))
    found.sort(key=lambda t: -t[0])
    return [u for _s, u in found if u.startswith(("http://", "https://"))]


def meta_images(page: str, base: str) -> list[str]:
    """<meta itemprop="image">: algunos sitios ponen ahí su logo grande (Google, la «G» de 128 px). Sirve solo si es
    cuadrada (si no, es una imagen para compartir, no un ícono): lo revisa to_png(square=True)."""
    out = []
    for tag in _META_RE.findall(page[:300_000]):
        attrs = {m.group(1).lower(): (m.group(3) or m.group(4) or m.group(5) or "") for m in _ATTR_RE.finditer(tag)}
        if attrs.get("itemprop", "").lower() == "image" and attrs.get("content"):
            url = urllib.parse.urljoin(base, attrs["content"].strip())
            if url.startswith(("http://", "https://")) and not url.lower().split("?")[0].endswith(".svg"):
                out.append(url)
    return out


def to_png(data: bytes, square: bool = False) -> tuple[bytes, int] | None:
    """Cualquier imagen (ICO, PNG, JPEG, WebP) → (PNG cuadrado de SIZE px centrado sobre transparente, el lado
    original). Las chicas se agrandan para llenar el cuadro."""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            if getattr(im, "format", "") == "ICO" and hasattr(im, "ico"):
                sizes = sorted(im.ico.sizes(), key=lambda s: s[0] * s[1])
                if sizes:
                    im.size = sizes[-1]  # el más grande del .ico
            im.load()
            img = im.convert("RGBA")
    except Exception:  # noqa: BLE001  (no es una imagen, o una que Pillow no entiende)
        return None
    if img.width < 16 or img.height < 16:
        return None
    if square and max(img.width, img.height) > 1.15 * min(img.width, img.height):
        return None
    side = max(img.width, img.height)
    scale = SIZE / side
    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(img, ((SIZE - img.width) // 2, (SIZE - img.height) // 2), img)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue(), side


def fetch(url: str) -> Path | None:
    """Baja el ícono del sitio de esa dirección (bloquea: llamarlo en un hilo). Devuelve el PNG, o None."""
    host = host_of(url)
    if not host:
        return None
    done = path_for(host)
    if done is not None:
        return done
    if _failed_recently(host):
        return None
    with _lock:
        if host in _busy:
            return None
        _busy.add(host)
    try:
        scheme = urllib.parse.urlparse(url).scheme or "https"
        home = f"{scheme}://{host}/"
        candidates: list[str] = []
        metas: list[str] = []
        try:
            raw, final = _fetch(home)
            page = raw.decode("utf-8", "replace")
            candidates, metas = icon_links(page, final), meta_images(page, final)
        except Exception as exc:  # noqa: BLE001
            log.debug("Favicon de %s: no pude leer la página (%s)", host, exc)
        candidates += [home + "apple-touch-icon.png", home + "favicon.ico"]  # muchos los tienen sin declararlos
        best: tuple[bytes, int, str] | None = None
        for icon_url in dict.fromkeys(candidates + metas):
            try:
                data, _ = _fetch(icon_url)
            except Exception as exc:  # noqa: BLE001
                log.debug("Favicon de %s: %s (%s)", host, icon_url, exc)
                continue
            got = to_png(data, square=icon_url in metas) if len(data) <= MAX_BYTES else None
            if got and (best is None or got[1] > best[1]):
                best = (got[0], got[1], icon_url)
            if best and best[1] >= GOOD_PX:
                break
        if best:
            cache_dir().mkdir(parents=True, exist_ok=True)
            dest = cache_dir() / f"{host.replace(':', '_')}.png"
            tmp = dest.with_suffix(".tmp")
            tmp.write_bytes(best[0])
            tmp.replace(dest)
            log.info("Favicon de %s: %s (%d px)", host, best[2], best[1])
            return dest
        log.info("Favicon de %s: no encontré uno que sirva", host)
        try:
            cache_dir().mkdir(parents=True, exist_ok=True)
            (cache_dir() / f"{host.replace(':', '_')}.no").touch()
        except OSError:
            pass
        return None
    finally:
        with _lock:
            _busy.discard(host)


def ensure(urls: list[str]) -> threading.Thread | None:
    """Baja de fondo los íconos que falten (una sola tanda a la vez). Devuelve el hilo, o None si no falta nada."""
    missing = [u for u in urls if host_of(u) and path_for(host_of(u)) is None and not _failed_recently(host_of(u))
               and host_of(u) not in _busy]
    if not missing:
        return None
    t = threading.Thread(target=lambda: [fetch(u) for u in missing], name="favicons", daemon=True)
    t.start()
    return t
