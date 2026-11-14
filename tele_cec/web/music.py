"""La música local en la página del celular: artistas, discos, temas, carpetas, búsqueda y la cola, como en Tidal.

La biblioteca (apps/library.py) se arma con las carpetas de la actividad Música. Poner algo a sonar abre Música si
está cerrada (con esos temas, desde el elegido) o, si ya está abierta (en pantalla o de fondo detrás de Fotos),
le cambia la lista sin cerrarla. La cola es la lista de mpv.

El celular solo manda ids (de tema, disco o artista), nunca rutas.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import TYPE_CHECKING, Any

from ..apps.library import Album, Artist, MusicLibrary, Song
from ..apps.player import MpvMediaActivity

if TYPE_CHECKING:
    from ..controller import Controller

log = logging.getLogger("web")

ID_RE = re.compile(r"^[0-9a-f]{12}$")
LIBRARIES = {"artists", "albums", "songs"}
LIST_KINDS = {"artist", "album", "all"}
THUMB = 240          # lado de las tapas en las listas
BIG = 640            # la del disco abierto
QUEUE_MAX = 200


def _int(value: Any, default: int = 0, lo: int = 0, hi: int = 100_000) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


class LocalMusicWeb:
    def __init__(self, ctl: Controller, launch):
        self.ctl = ctl
        self.launch = launch   # WebRemote.launch
        self._lib: MusicLibrary | None = None

    @property
    def act(self) -> MpvMediaActivity | None:
        act = self.ctl.activities.get("musica")
        return act if isinstance(act, MpvMediaActivity) and not act.command else None

    def available(self) -> bool:
        return self.act is not None

    @property
    def lib(self) -> MusicLibrary | None:
        act = self.act
        if act is None:
            return None
        if self._lib is None:
            self._lib = MusicLibrary(act.roots)
        return self._lib

    async def _ready(self) -> MusicLibrary | None:
        lib = self.lib
        if lib is not None:
            await asyncio.to_thread(lib.refresh)
        return lib

    # ---------------------------------------------------------------- cómo se ve cada cosa
    def _song(self, lib: MusicLibrary, s: Song) -> dict[str, Any]:
        al = lib.album_of(s)
        return {"type": "song", "id": s.id, "title": s.title, "dur": round(s.dur),
                "sub": " · ".join(x for x in (s.artist or (al.artist if al else ""), s.album) if x),
                "img": f"/api/music/art?id={al.id}&s={THUMB}" if al else ""}

    @staticmethod
    def _album(a: Album) -> dict[str, Any]:
        n = len(a.songs)
        return {"type": "album", "id": a.id, "title": a.title,
                "sub": f"{a.artist} · {n} tema{'s' if n != 1 else ''}", "img": f"/api/music/art?id={a.id}&s={THUMB}"}

    @staticmethod
    def _artist(a: Artist) -> dict[str, Any]:
        n, d = len(a.songs), len(a.albums)
        return {"type": "artist", "id": a.id, "title": a.name,
                "sub": f"{d} disco{'s' if d != 1 else ''} · {n} tema{'s' if n != 1 else ''}",
                "img": f"/api/music/art?id={a.albums[0].id}&s={THUMB}" if a.albums else ""}

    def _songs_of(self, lib: MusicLibrary, kind: str, ident: str) -> tuple[str, list[Song]] | None:
        if kind == "all":
            return "Toda la música", lib.songs
        if kind == "album" and ident in lib.albums:
            a = lib.albums[ident]
            return a.title, a.songs
        if kind == "artist" and ident in lib.artists:
            a = lib.artists[ident]
            return a.name, a.songs
        return None

    # ---------------------------------------------------------------- lectura
    async def library(self, query: dict[str, str]) -> dict[str, Any]:
        what = query.get("what", "artists")
        if what not in LIBRARIES:
            return {"error": "biblioteca desconocida"}
        lib = await self._ready()
        if lib is None:
            return {"error": "Música no está configurada"}
        items: list[dict[str, Any]]
        if what == "artists":
            items = [self._artist(a) for a in lib.artists.values()]
        elif what == "albums":
            items = [self._album(a) for a in lib.albums.values()]
        else:
            items = [self._song(lib, s) for s in lib.songs]
        start, count = _int(query.get("start")), _int(query.get("count"), 100, 1, 300)
        return {"items": items[start:start + count], "total": len(items), "start": start}

    async def items(self, query: dict[str, str]) -> dict[str, Any]:
        kind, ident = query.get("type", ""), query.get("id", "")
        if kind not in LIST_KINDS or (kind != "all" and not ID_RE.match(ident)):
            return {"error": "pedido inválido"}
        lib = await self._ready()
        if lib is None:
            return {"error": "Música no está configurada"}
        found = self._songs_of(lib, kind, ident)
        if found is None:
            return {"error": "Ya no está (¿se sacó el pendrive?)"}
        title, songs = found
        start, count = _int(query.get("start")), _int(query.get("count"), 200, 1, 500)
        out: dict[str, Any] = {"title": title, "total": len(songs), "start": start,
                               "items": [self._song(lib, s) for s in songs[start:start + count]]}
        if kind == "album":
            a = lib.albums[ident]
            out.update(sub=a.artist, img=f"/api/music/art?id={a.id}&s={BIG}")
        elif kind == "artist":
            a = lib.artists[ident]
            out.update(sub=f"{len(a.albums)} disco{'s' if len(a.albums) != 1 else ''}",
                       img=f"/api/music/art?id={a.albums[0].id}&s={BIG}" if a.albums else "",
                       albums=[self._album(al) for al in a.albums])
        return out

    async def search(self, query: dict[str, str]) -> dict[str, Any]:
        text = " ".join(str(query.get("q", "")).split())[:120]
        if not text:
            return {"error": "escribí algo para buscar"}
        lib = await self._ready()
        if lib is None:
            return {"error": "Música no está configurada"}
        r = lib.search(text)
        return {"artists": [self._artist(a) for a in r["artists"]], "albums": [self._album(a) for a in r["albums"]],
                "songs": [self._song(lib, s) for s in r["songs"]]}

    async def queue(self) -> dict[str, Any]:
        act = self.act
        if act is None:
            return {"error": "Música no está configurada"}
        if not act.running():
            return {"closed": True, "current": None, "next": [], "pos": -1}
        lib = await self._ready()
        files, pos = await act.playlist()
        out = []
        for i, path in enumerate(files):
            s = lib.by_path.get(path) if lib else None
            item = self._song(lib, s) if s and lib else {"type": "song", "id": "", "title": path.rsplit("/", 1)[-1],
                                                         "sub": "", "img": ""}
            out.append({**item, "n": i})
        cur = out[pos] if 0 <= pos < len(out) else None
        nxt = out[pos + 1:] if pos >= 0 else out
        return {"current": cur, "next": nxt[:QUEUE_MAX], "total_next": len(nxt), "pos": pos}

    async def art(self, query: dict[str, str]) -> tuple[bytes, str] | None:
        ident = query.get("id", "")
        lib = self.lib
        if lib is None or not ID_RE.match(ident):
            return None
        if ident not in lib.albums:
            await asyncio.to_thread(lib.refresh)
        size = _int(query.get("s"), THUMB, 64, 1200)
        return await asyncio.to_thread(lib.art, ident, size)

    # ---------------------------------------------------------------- acciones
    async def play(self, data: dict[str, Any]) -> dict[str, Any]:
        """{type: album|artist|all|song, id, index, shuffle}: esos temas, desde el número index (un tema suelto:
        su disco, desde ese tema)."""
        kind, ident = str(data.get("type") or ""), str(data.get("id") or "")
        if kind not in LIST_KINDS | {"song"} or (kind != "all" and not ID_RE.match(ident)):
            return {"ok": False, "error": "pedido inválido"}
        act, lib = self.act, await self._ready()
        if act is None or lib is None:
            return {"ok": False, "error": "Música no está configurada"}
        if kind == "song":  # un tema: su disco desde ese tema (después sigue el disco)
            s = lib.by_id.get(ident)
            if s is None:
                return {"ok": False, "error": "Ese tema ya no está"}
            al = lib.album_of(s)
            songs = al.songs if al else [s]
            title, index = s.title, songs.index(s)
        else:
            found = self._songs_of(lib, kind, ident)
            if found is None:
                return {"ok": False, "error": "Ya no está (¿se sacó el pendrive?)"}
            title, songs = found
            index = _int(data.get("index"), 0, 0, max(0, len(songs) - 1))
        if not songs:
            return {"ok": False, "error": "No hay temas"}
        files = [s.path for s in songs]
        if data.get("shuffle"):
            first = files.pop(index)
            random.shuffle(files)
            files, index = [first, *files], 0
        log.info("Celular: pongo en Música «%s» desde el tema %d (%d temas)", title, index + 1, len(files))
        self.ctl.power.last_activity = time.monotonic()
        if act.running():
            ok = await act.play_files(files, index)
            return {"ok": ok} if ok else {"ok": False, "error": "No pude cambiar lo que suena"}
        act.want_files = (files, index)
        r = await self.launch("musica")
        act.want_files = None
        return r

    async def enqueue(self, data: dict[str, Any]) -> dict[str, Any]:
        ident, mode = str(data.get("id") or ""), str(data.get("mode") or "end")
        if not ID_RE.match(ident) or mode not in ("next", "end"):
            return {"ok": False, "error": "pedido inválido"}
        act, lib = self.act, await self._ready()
        if act is None or lib is None:
            return {"ok": False, "error": "Música no está configurada"}
        s = lib.by_id.get(ident)
        if s is None:
            return {"ok": False, "error": "Ese tema ya no está"}
        if not act.running():  # sin nada sonando: que suene este
            return await self.play({"type": "song", "id": ident})
        if not await act.enqueue(s.path, mode == "next"):
            return {"ok": False, "error": "No pude agregarlo"}
        return {"ok": True, "message": ("Suena después de este: " if mode == "next" else "Al final de la cola: ")
                + s.title}

    async def jump(self, data: dict[str, Any]) -> dict[str, Any]:
        act = self.act
        if act is None or not act.running():
            return {"ok": False, "error": "Música está cerrada"}
        ok = await act.jump(_int(data.get("n"), -1, -1))
        return {"ok": ok} if ok else {"ok": False, "error": "Ese tema ya no está en la lista"}
