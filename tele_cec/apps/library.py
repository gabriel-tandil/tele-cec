"""La música local ordenada por artista, disco y tema (para elegir desde el celular, como en Tidal).

Lee las etiquetas de cada archivo (título, artista, disco, número, duración) con mutagen (python3-mutagen); sin
mutagen, o si el archivo no las trae, las saca de las carpetas: Artista/Disco/NN Tema.ext. Un disco son los temas de
una misma carpeta con el mismo nombre de disco; el artista de un disco es su «artista del álbum» (o el que más
aparece en sus temas).

Leer las etiquetas de miles de temas tarda: lo leído se guarda en ~/.cache/tele-cec/musica-indice.json (por ruta,
tamaño y fecha) y la próxima vez solo se leen los archivos nuevos o cambiados. La carpeta se vuelve a recorrer como
mucho cada RESCAN_S segundos (al pedir la biblioteca).

Los ids (de tema, disco y artista) son un resumen corto de la ruta o del nombre: el celular nunca manda rutas.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..system.procs import LOG_DIR
from .folders import AUDIO_EXT, scan_media

log = logging.getLogger("actividades")

RESCAN_S = 30.0
CACHE_VERSION = 1
COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "albumartsmall")
COVER_EXT = (".jpg", ".jpeg", ".png", ".webp")
ART_CACHE = 64            # tapas guardadas en memoria (ya achicadas)
VARIOUS = {"various artists", "varios artistas", "varios", "various", "va"}


def _id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def fold(text: str) -> str:
    """Para ordenar y buscar: sin mayúsculas ni tildes («Andrés» = «andres»)."""
    norm = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in norm if not unicodedata.combining(ch))


def _sort_key(text: str) -> list:
    t = fold(text)
    t = re.sub(r"^(the|los|las|el|la)\s+", "", t)  # «The Beatles» con la B, «Los Pericos» con la P
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", t)]


def _num(value: Any) -> int:
    """«3/12» → 3."""
    m = re.match(r"\s*(\d+)", str(value or ""))
    return int(m.group(1)) if m else 0


@dataclass
class Song:
    path: str
    title: str
    artist: str
    album: str
    album_artist: str = ""
    track: int = 0
    disc: int = 0
    dur: float = 0.0
    id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        self.id = self.id or _id(self.path)


@dataclass
class Album:
    id: str
    title: str
    artist: str
    folder: str
    songs: list[Song]


@dataclass
class Artist:
    id: str
    name: str
    albums: list[Album]

    @property
    def songs(self) -> list[Song]:
        return [s for a in self.albums for s in a.songs]


# ---------------------------------------------------------------- etiquetas
def from_path(path: str, root: str) -> dict[str, Any]:
    """Lo que dicen las carpetas: Artista/Disco/NN Tema.ext (o lo que haya)."""
    rel = Path(os.path.relpath(path, root))
    stem = rel.stem
    m = re.match(r"^(?:(\d+)[-.])?(\d+)[\s.\-_]+(.+)$", stem)
    disc, track, title = (_num(m.group(1)), _num(m.group(2)), m.group(3).strip()) if m else (0, 0, stem)
    parts = rel.parts[:-1]
    album = parts[-1] if parts else ""
    artist = parts[-2] if len(parts) >= 2 else ""
    return {"title": title, "artist": artist, "album": album, "album_artist": artist, "track": track, "disc": disc}


def read_tags(path: str) -> dict[str, Any]:
    """Las etiquetas del archivo ({} si no hay mutagen o no se pueden leer)."""
    try:
        import mutagen  # noqa: PLC0415
    except ImportError:
        return {}
    try:
        f = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001  (archivos rotos: mutagen tira de todo)
        return {}
    if f is None:
        return {}
    tags = f.tags or {}

    def one(key: str) -> str:
        try:
            v = tags.get(key)
        except Exception:  # noqa: BLE001
            return ""
        return str(v[0]).strip() if v else ""
    out = {"title": one("title"), "artist": one("artist"), "album": one("album"),
           "album_artist": one("albumartist"), "track": _num(one("tracknumber")), "disc": _num(one("discnumber"))}
    try:
        out["dur"] = round(float(f.info.length), 1)
    except (AttributeError, TypeError, ValueError):
        pass
    return {k: v for k, v in out.items() if v}


def song_for(path: str, root: str, tags: dict[str, Any]) -> Song:
    base = from_path(path, root)
    merged = {**base, **{k: v for k, v in tags.items() if v}}
    if not tags.get("album_artist"):
        merged["album_artist"] = ""   # se decide por disco (el que más aparece)
    return Song(path=path, title=merged["title"] or Path(path).stem, artist=merged.get("artist") or "",
                album=merged.get("album") or "", album_artist=merged.get("album_artist") or "",
                track=int(merged.get("track") or 0), disc=int(merged.get("disc") or 0),
                dur=float(merged.get("dur") or 0.0))


# ---------------------------------------------------------------- tapas
def embedded_art(path: str) -> bytes | None:
    """La tapa que trae el archivo (MP4/M4A, MP3, FLAC, OGG…)."""
    try:
        import mutagen  # noqa: PLC0415
        f = mutagen.File(path)
    except Exception:  # noqa: BLE001
        return None
    if f is None:
        return None
    pics = getattr(f, "pictures", None)  # FLAC
    if pics:
        return bytes(pics[0].data)
    tags = f.tags
    if tags is None:
        return None
    try:
        if "covr" in tags and tags["covr"]:  # MP4
            return bytes(tags["covr"][0])
        for key in list(tags.keys()):
            if str(key).startswith("APIC"):  # ID3
                return bytes(tags[key].data)
        b64 = tags.get("metadata_block_picture")  # OGG/Opus
        if b64:
            import base64  # noqa: PLC0415
            from mutagen.flac import Picture  # noqa: PLC0415
            return bytes(Picture(base64.b64decode(b64[0])).data)
    except Exception:  # noqa: BLE001
        return None
    return None


def folder_art(folder: str) -> bytes | None:
    """cover.jpg, folder.jpg… al lado de los temas."""
    try:
        names = {e.name.lower(): e.path for e in os.scandir(folder) if e.is_file()}
    except OSError:
        return None
    for base in COVER_NAMES:
        for ext in COVER_EXT:
            p = names.get(base + ext)
            if p:
                try:
                    return Path(p).read_bytes() if os.path.getsize(p) < 16 * 1024 * 1024 else None
                except OSError:
                    return None
    return None


def image_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def shrink(data: bytes, size: int) -> tuple[bytes, str]:
    """Achica la tapa a `size` píxeles de lado (para las listas del celular); sin Pillow, la deja como está."""
    ctype = image_type(data)
    try:
        from PIL import Image  # noqa: PLC0415
        im = Image.open(io.BytesIO(data))
        if max(im.size) <= size:
            return data, ctype
        im.thumbnail((size, size))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001  (sin Pillow o una imagen rara: la original)
        return data, ctype


# ---------------------------------------------------------------- la biblioteca
class MusicLibrary:
    def __init__(self, roots: Callable[[], list[Path]], cache_file: Path | None = None):
        self.roots = roots
        self.cache_file = cache_file or (LOG_DIR / "musica-indice.json")
        self.songs: list[Song] = []
        self.by_id: dict[str, Song] = {}
        self.by_path: dict[str, Song] = {}
        self.albums: dict[str, Album] = {}
        self.artists: dict[str, Artist] = {}
        self._scanned_at = 0.0
        self._roots_key: tuple[str, ...] = ()
        self._lock = threading.Lock()
        self._art: dict[tuple[str, int], tuple[bytes, str]] = {}

    # -------------------------------------------------------- índice
    def refresh(self, force: bool = False) -> None:
        """Vuelve a recorrer las carpetas si pasó un rato (o si cambiaron las carpetas configuradas)."""
        with self._lock:
            roots = [str(r) for r in self.roots()]
            key = tuple(roots)
            if not force and key == self._roots_key and time.monotonic() - self._scanned_at < RESCAN_S:
                return
            t0 = time.monotonic()
            cache = self._load_cache()
            fresh: dict[str, Any] = {}
            songs: list[Song] = []
            read = 0
            for root in roots:
                for path in scan_media(Path(root), AUDIO_EXT):
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    sig = [st.st_size, int(st.st_mtime)]
                    hit = cache.get(path)
                    if hit and hit.get("sig") == sig:
                        tags = hit.get("tags") or {}
                    else:
                        tags = read_tags(path)
                        read += 1
                    fresh[path] = {"sig": sig, "tags": tags}
                    songs.append(song_for(path, root, tags))
            self._build(songs)
            self._roots_key = key
            self._scanned_at = time.monotonic()
            if read or len(fresh) != len(cache):
                self._save_cache(fresh)
            if read:
                log.info("Música: %d temas (%d leídos de nuevo) en %.1f s", len(songs), read, time.monotonic() - t0)

    def _build(self, songs: list[Song]) -> None:
        groups: dict[tuple[str, str], list[Song]] = {}
        for s in songs:
            groups.setdefault((os.path.dirname(s.path), fold(s.album)), []).append(s)
        albums: dict[str, Album] = {}
        for (folder, _), items in groups.items():
            items.sort(key=lambda s: (s.disc, s.track, _sort_key(os.path.basename(s.path))))
            named = Counter(s.album_artist for s in items if s.album_artist)
            if named:
                artist = named.most_common(1)[0][0]
            else:
                artists = Counter(s.artist for s in items if s.artist)
                artist = artists.most_common(1)[0][0] if len(artists) == 1 or (
                    artists and artists.most_common(1)[0][1] > len(items) / 2) else ("Varios" if artists else "")
            if fold(artist) in VARIOUS:
                artist = "Varios"
            title = items[0].album or os.path.basename(folder) or "Sin disco"
            album = Album(id=_id(folder + "\0" + fold(items[0].album)), title=title, artist=artist or "Sin artista",
                          folder=folder, songs=items)
            albums[album.id] = album
        artists: dict[str, Artist] = {}
        for album in albums.values():
            name = album.artist
            aid = _id("artist\0" + fold(name))
            artists.setdefault(aid, Artist(id=aid, name=name, albums=[])).albums.append(album)
        for artist in artists.values():
            artist.albums.sort(key=lambda a: _sort_key(a.title))
        last = {"Varios": 1, "Sin artista": 2}   # al final de la lista
        self.artists = dict(sorted(artists.items(), key=lambda kv: (last.get(kv[1].name, 0), _sort_key(kv[1].name))))
        self.albums = dict(sorted(albums.items(), key=lambda kv: (_sort_key(kv[1].artist), _sort_key(kv[1].title))))
        # Todos los temas en el orden de la biblioteca: artista, disco, número.
        self.songs = [s for a in self.artists.values() for al in a.albums for s in al.songs]
        self.by_id = {s.id: s for s in self.songs}
        self.by_path = {s.path: s for s in self.songs}
        self._album_of = {s.id: al for al in albums.values() for s in al.songs}

    def album_of(self, song: Song) -> Album | None:
        return self._album_of.get(song.id)

    def _load_cache(self) -> dict[str, Any]:
        try:
            data = json.loads(self.cache_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return {}
        files = data.get("files")
        return files if isinstance(files, dict) else {}

    def _save_cache(self, files: dict[str, Any]) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": files}, ensure_ascii=False), "utf-8")
            tmp.replace(self.cache_file)
        except OSError as exc:
            log.warning("Música: no pude guardar el índice (%s)", exc)

    # -------------------------------------------------------- búsqueda
    def search(self, text: str, limit: int = 50) -> dict[str, list]:
        words = fold(text).split()
        if not words:
            return {"artists": [], "albums": [], "songs": []}

        def hit(*fields: str) -> bool:
            hay = fold(" ".join(fields))
            return all(w in hay for w in words)
        return {"artists": [a for a in self.artists.values() if hit(a.name)][:limit],
                "albums": [a for a in self.albums.values() if hit(a.title, a.artist)][:limit],
                "songs": [s for s in self.songs if hit(s.title, s.artist, s.album, s.album_artist)][:limit]}

    # -------------------------------------------------------- tapas
    def art(self, album_id: str, size: int = 0) -> tuple[bytes, str] | None:
        """La tapa del disco (la del primer tema que la traiga, o cover.jpg…), achicada a `size` si se pide."""
        album = self.albums.get(album_id)
        if album is None:
            return None
        key = (album_id, size)
        if key in self._art:
            return self._art[key]
        data = None
        for s in album.songs[:3]:
            data = embedded_art(s.path)
            if data:
                break
        data = data or folder_art(album.folder)
        if not data or not image_type(data):
            return None
        out = shrink(data, size) if size else (data, image_type(data))
        if len(self._art) >= ART_CACHE:
            self._art.pop(next(iter(self._art)))
        self._art[key] = out
        return out
