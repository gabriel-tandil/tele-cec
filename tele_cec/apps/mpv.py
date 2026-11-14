"""mpv por su canal de control (--input-ipc-server): comandos y avisos de cuándo cambia de archivo.

Lo usa Fotos para «fotos y videos» y «solo videos» (feh no reproduce videos). El protocolo es JSON por un socket
Unix, una línea por mensaje: {"command": [...], "request_id": N} → {"error": "success", "data": ..., "request_id": N},
y además avisos sueltos ({"event": "property-change", ...}).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("actividades")


class Mpv:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self._ids = itertools.count(1)
        self._watch: asyncio.Task | None = None

    async def command(self, *args: Any, timeout: float = 3.0) -> tuple[bool, Any]:
        """Un comando (p. ej. "playlist-next"); (salió bien, lo que devolvió)."""
        rid = next(self._ids)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.socket_path)), timeout)
        except (OSError, asyncio.TimeoutError):
            return False, None
        try:
            writer.write(json.dumps({"command": list(args), "request_id": rid}).encode() + b"\n")
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout)
                if not line:
                    return False, None
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("request_id") == rid:
                    return msg.get("error") == "success", msg.get("data")
        except (OSError, asyncio.TimeoutError):
            return False, None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def get(self, prop: str) -> Any:
        ok, data = await self.command("get_property", prop)
        return data if ok else None

    async def set(self, prop: str, value: Any) -> bool:
        return (await self.command("set_property", prop, value))[0]

    async def ready(self, timeout: float = 10.0) -> bool:
        """Espera a que mpv abra su canal (recién lanzado tarda un instante)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if (await self.command("get_property", "pid", timeout=1.0))[0]:
                return True
            await asyncio.sleep(0.2)
        return False

    # ------------------------------------------------------------ avisos
    def watch(self, on_file: Callable[[str], Awaitable[None]]) -> None:
        """Llama a on_file(ruta) cada vez que mpv pasa a otro archivo (y una vez al empezar)."""
        self.stop_watch()
        self._watch = asyncio.get_running_loop().create_task(self._watch_loop(on_file))

    def stop_watch(self) -> None:
        if self._watch and not self._watch.done():
            self._watch.cancel()
        self._watch = None

    async def _watch_loop(self, on_file: Callable[[str], Awaitable[None]]) -> None:
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        except OSError as exc:
            log.warning("mpv: no pude escuchar sus avisos: %s", exc)
            return
        try:
            writer.write(json.dumps({"command": ["observe_property", 1, "path"]}).encode() + b"\n")
            await writer.drain()
            last = None
            while True:
                line = await reader.readline()
                if not line:
                    return  # mpv se cerró
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("event") == "property-change" and msg.get("name") == "path":
                    path = msg.get("data")
                    if path and path != last:
                        last = path
                        try:
                            await on_file(str(path))
                        except Exception:  # noqa: BLE001
                            log.exception("mpv: error con el aviso de archivo nuevo")
        except asyncio.CancelledError:
            raise
        except OSError:
            return
        finally:
            writer.close()
