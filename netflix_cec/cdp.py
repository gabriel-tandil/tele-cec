"""Cliente mínimo de Chrome DevTools Protocol (CDP) sobre WebSocket."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable

import websockets

log = logging.getLogger("cdp")


class CDPError(Exception):
    pass


class CDPClosed(CDPError):
    pass


def _http_json(port: int, path: str, timeout: float = 2.0) -> Any:
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (solo localhost)
        return json.loads(resp.read().decode("utf-8"))


async def http_json(port: int, path: str, timeout: float = 2.0) -> Any:
    return await asyncio.to_thread(_http_json, port, path, timeout)


async def endpoint_alive(port: int) -> bool:
    try:
        await http_json(port, "/json/version", 1.5)
        return True
    except (OSError, urllib.error.URLError, ValueError):
        return False


async def list_pages(port: int) -> list[dict[str, Any]]:
    targets = await http_json(port, "/json/list")
    return [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def pick_page(pages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefiere una pestaña de Netflix; si no hay, la primera pestaña común."""
    for p in pages:
        if "netflix.com" in p.get("url", ""):
            return p
    for p in pages:
        url = p.get("url", "")
        if not url.startswith(("devtools://", "chrome-extension://")):
            return p
    return None


class CDP:
    def __init__(self, ws_url: str, on_event: Callable[[str, dict], None] | None = None):
        self.ws_url = ws_url
        self.on_event = on_event
        self.ws: Any = None
        self.closed = asyncio.Event()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    async def open(self) -> None:
        self.ws = await websockets.connect(
            self.ws_url, max_size=None, ping_interval=None, open_timeout=5, compression=None
        )
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"]
                            fut.set_exception(CDPError(f"{err.get('message')} ({err.get('code')})"))
                        else:
                            fut.set_result(msg.get("result", {}))
                elif "method" in msg:
                    if msg["method"] in ("Inspector.detached", "Inspector.targetCrashed"):
                        log.warning("Chromium: %s", msg["method"])
                        await self.ws.close()
                        break
                    if self.on_event:
                        try:
                            self.on_event(msg["method"], msg.get("params", {}))
                        except Exception:  # noqa: BLE001
                            log.exception("Error manejando evento %s", msg["method"])
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Error leyendo de DevTools")
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPClosed("conexión cerrada"))
            self._pending.clear()

    async def send(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        if self.closed.is_set() or self.ws is None:
            raise CDPClosed("sin conexión con Chromium")
        msg_id = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        except websockets.exceptions.ConnectionClosed as exc:
            self._pending.pop(msg_id, None)
            raise CDPClosed(str(exc)) from exc
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def evaluate(self, expression: str, timeout: float = 10.0) -> Any:
        res = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            det = res["exceptionDetails"]
            text = det.get("exception", {}).get("description") or det.get("text")
            raise CDPError(f"Error de JavaScript: {text}")
        return res.get("result", {}).get("value")

    async def close(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._reader:
            try:
                await asyncio.wait_for(self._reader, 2)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
