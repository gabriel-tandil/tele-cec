"""El mapa de dónde se sacó una foto (las que tienen GPS en el EXIF), para mostrarlo en el panel de la TV.

Se arma una imagen con los mosaicos de OpenStreetMap alrededor del punto (una marca en el medio) con Pillow. Los
mosaicos se guardan en ~/.cache/tele-cec/mapas (un mes) para no volver a pedirlos: así lo pide OpenStreetMap, junto
con decir de quién es el mapa (va abajo a la derecha) y pedirlos de a poco (dos a la vez). Para eso la Pi le pide a
tile.openstreetmap.org los mosaicos de la zona: es lo único que sale de la casa ([apps.fotos] map = false lo apaga;
map_tiles cambia de dónde salen).
"""

from __future__ import annotations

import io
import logging
import math
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..system.procs import LOG_DIR

log = logging.getLogger("actividades")

TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "tele-cec/1.0 (control de la TV con HDMI-CEC; uso personal)"
TILE = 256
KEEP_S = 30 * 24 * 3600     # cuánto se usa un mosaico guardado antes de volver a pedirlo
MIN_ZOOM, MAX_ZOOM, START_ZOOM = 3, 18, 15
SIZE = (1280, 800)


def to_pixels(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Latitud y longitud → píxel en el mapa del mundo a ese zoom (Web Mercator, como OpenStreetMap)."""
    n = TILE * 2 ** zoom
    lat = max(-85.0511, min(85.0511, lat))
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


class MapMaker:
    def __init__(self, cache_dir: Path | None = None, tiles: str = TILES):
        self.cache_dir = cache_dir or (LOG_DIR / "mapas")
        self.tiles = tiles
        self.failed = 0   # mosaicos que no se pudieron bajar en el último mapa

    def _fetch(self, z: int, x: int, y: int) -> bytes | None:
        """Un mosaico: el guardado (si es de este mes) o pedido a OpenStreetMap."""
        path = self.cache_dir / str(z) / str(x) / f"{y}.png"
        try:
            if time.time() - path.stat().st_mtime < KEEP_S:
                return path.read_bytes()
        except OSError:
            pass
        url = self.tiles.format(z=z, x=x, y=y)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310  (la dirección es la de la configuración)
                data = r.read()
        except Exception as exc:  # noqa: BLE001  (sin internet: el mapa sale con huecos, no se cae)
            log.debug("Mapa: no pude bajar %s (%s)", url, exc)
            try:
                return path.read_bytes()  # uno viejo es mejor que nada
            except OSError:
                return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            pass
        return data

    def render(self, lat: float, lon: float, zoom: int, out: Path, size: tuple[int, int] = SIZE) -> Path:
        """Arma el mapa centrado en el punto, con la marca, y lo guarda en `out` (PNG). Bloquea: en un hilo."""
        from PIL import Image, ImageDraw  # noqa: PLC0415
        w, h = size
        cx, cy = to_pixels(lat, lon, zoom)
        left, top = cx - w / 2, cy - h / 2
        n = 2 ** zoom
        want = [(tx, ty) for tx in range(math.floor(left / TILE), math.floor((left + w) / TILE) + 1)
                for ty in range(math.floor(top / TILE), math.floor((top + h) / TILE) + 1) if 0 <= ty < n]
        with ThreadPoolExecutor(max_workers=2) as pool:  # de a dos, como pide OpenStreetMap
            got = list(pool.map(lambda t: self._fetch(zoom, t[0] % n, t[1]), want))
        img = Image.new("RGB", (w, h), (225, 225, 220))
        self.failed = 0
        for (tx, ty), data in zip(want, got):
            if data is None:
                self.failed += 1
                continue
            try:
                tile = Image.open(io.BytesIO(data)).convert("RGB")
            except Exception:  # noqa: BLE001
                self.failed += 1
                continue
            img.paste(tile, (round(tx * TILE - left), round(ty * TILE - top)))
        d = ImageDraw.Draw(img)
        x, y = w / 2, h / 2  # la marca: un alfiler rojo con borde blanco, la punta en el lugar
        r = 22
        d.polygon([(x, y), (x - r * 0.75, y - r * 1.6), (x + r * 0.75, y - r * 1.6)], fill=(220, 38, 38),
                  outline=(255, 255, 255))
        d.ellipse([x - r, y - r * 2.6, x + r, y - r * 0.6], fill=(220, 38, 38), outline=(255, 255, 255), width=4)
        d.ellipse([x - r * 0.38, y - r * 1.98, x + r * 0.38, y - r * 1.22], fill=(255, 255, 255))
        credit = "© OpenStreetMap"
        tw = d.textlength(credit)
        d.rectangle([w - tw - 16, h - 24, w, h], fill=(255, 255, 255))
        d.text((w - tw - 8, h - 20), credit, fill=(40, 40, 40))
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, "PNG")
        log.info("Mapa: %.5f, %.5f a zoom %d (%d mosaicos%s)", lat, lon, zoom, len(want),
                 f", {self.failed} sin bajar" if self.failed else "")
        return out


def available() -> bool:
    try:
        import PIL  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True
