"""Tidal en la página del celular: la biblioteca, la búsqueda, la cola y poner a sonar, a través de High Tide.

Todo pasa por el canal de control que el parche abre dentro de High Tide (apps/tidal/hightide_patch.py): una sola
sesión de Tidal y una sola cola, así lo que se elige en el celular también se ve en la TV, y al revés.
Si High Tide está cerrado, poner algo a sonar lo abre primero (y espera a que entre a la cuenta).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..controller import Controller

log = logging.getLogger("web")

LIBRARIES = {"tracks", "playlists", "mixes", "albums", "artists"}
LIST_KINDS = {"fav", "album", "playlist", "mix", "artist", "track"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # ids de Tidal: números o uuid
OPEN_WAIT_S = 45.0                             # abrir High Tide y entrar a la cuenta


def _int(value: Any, default: int = 0, lo: int = 0, hi: int = 100_000) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


class TidalWeb:
    def __init__(self, ctl: Controller, launch):
        self.ctl = ctl
        self.launch = launch   # WebRemote.launch: abre una app como desde el celular (despierta, no prende la TV…)

    @property
    def music(self) -> Any:
        return getattr(self.ctl, "music", None)

    def available(self) -> bool:
        m = self.music
        return m is not None and "tidal" in self.ctl.activities

    async def _data(self, line: str, timeout: float = 25.0) -> dict[str, Any]:
        m = self.music
        if m is None:
            return {"error": "Tidal no está configurado"}
        out = await m.data(line, timeout)
        if out.get("error") == "login":
            out["error"] = "Tidal todavía no entró a la cuenta (mirá la TV)"
            out["login"] = True
        elif out.get("error") in ("cerrado", "sin canal de control"):
            out["closed"] = True
            out["error"] = "Tidal está cerrado"
        return out

    async def _ensure_open(self) -> dict[str, Any] | None:
        """Abre Tidal si hace falta y espera a que conteste. None = listo; si no, el error para la página."""
        m = self.music
        if m is None:
            return {"error": "Tidal no está configurado"}
        if not m.running():
            r = await self.launch("tidal")
            if not r.get("ok"):
                return {"error": r.get("error") or "No pude abrir Tidal"}
        deadline = time.monotonic() + OPEN_WAIT_S
        while True:
            r = await self._data("ready", 5)
            if "error" not in r:
                return None
            if time.monotonic() > deadline or not m.running():
                return {"error": r["error"]}
            await asyncio.sleep(1.0)

    # ---------------------------------------------------------------- lectura
    async def library(self, query: dict[str, str]) -> dict[str, Any]:
        what = query.get("what", "tracks")
        if what not in LIBRARIES:
            return {"error": "biblioteca desconocida"}
        return await self._data(f"lib {what} {_int(query.get('start'))} {_int(query.get('count'), 100, 1, 200)}")

    async def items(self, query: dict[str, str]) -> dict[str, Any]:
        kind, ident = query.get("type", ""), query.get("id", "-") or "-"
        if kind not in LIST_KINDS or (kind != "fav" and not ID_RE.match(ident)):
            return {"error": "pedido inválido"}
        return await self._data(f"list {kind} {ident if kind != 'fav' else '-'} {_int(query.get('start'))} "
                                f"{_int(query.get('count'), 100, 1, 200)}", 40)

    async def search(self, query: dict[str, str]) -> dict[str, Any]:
        text = " ".join(str(query.get("q", "")).split())[:120]
        if not text:
            return {"error": "escribí algo para buscar"}
        log.info("Celular: busco en Tidal «%s»", text)
        return await self._data(f"search {text}", 30)

    async def queue(self) -> dict[str, Any]:
        return await self._data("queue", 10)

    # ---------------------------------------------------------------- acciones
    async def play(self, data: dict[str, Any]) -> dict[str, Any]:
        kind, ident = str(data.get("type") or ""), str(data.get("id") or "-")
        if kind not in LIST_KINDS or (kind != "fav" and not ID_RE.match(ident)):
            return {"ok": False, "error": "pedido inválido"}
        err = await self._ensure_open()
        if err:
            return {"ok": False, **err}
        index = _int(data.get("index"))
        log.info("Celular: pongo en Tidal %s %s desde el tema %d", kind, ident, index + 1)
        self.ctl.power.last_activity = time.monotonic()
        r = await self._data(f"play {kind} {ident if kind != 'fav' else '-'} {index}", 40)
        if data.get("shuffle") and "error" not in r:
            mpris = getattr(self.music, "mpris", None)
            if mpris is not None:
                await mpris.ctl("shuffle", "On")
        return {"ok": "error" not in r, **r}

    async def enqueue(self, data: dict[str, Any]) -> dict[str, Any]:
        ident, mode = str(data.get("id") or ""), str(data.get("mode") or "end")
        if not ID_RE.match(ident) or mode not in ("next", "end"):
            return {"ok": False, "error": "pedido inválido"}
        err = await self._ensure_open()
        if err:
            return {"ok": False, **err}
        r = await self._data(f"enqueue {ident} {mode}", 20)
        if "error" not in r:
            r["message"] = ("Suena después de este: " if mode == "next" else "Al final de la cola: ") + r.get("title", "")
        return {"ok": "error" not in r, **r}

    async def jump(self, data: dict[str, Any]) -> dict[str, Any]:
        where = str(data.get("where") or "")
        if where not in ("queue", "next"):
            return {"ok": False, "error": "pedido inválido"}
        r = await self._data(f"jump {where} {_int(data.get('n'))}", 15)
        return {"ok": "error" not in r, **r}

    async def like(self) -> dict[str, Any]:
        m = self.music
        if m is None or not m.running():
            return {"ok": False, "error": "Tidal está cerrado"}
        before = (await self._data("liked", 5)).get("liked")
        reply = await m.command("like", timeout=5)
        return {"ok": bool(reply) and not str(reply).startswith("error"),
                "message": "Quitado de tus favoritos" if before else "Agregado a tus favoritos"}

    async def liked(self) -> bool | None:
        m = self.music
        if m is None or not m.running():
            return None
        r = await m.data("liked", 3)
        return r.get("liked") if "error" not in r else None
