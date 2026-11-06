"""Cliente mínimo de Chrome DevTools Protocol (CDP) sobre WebSocket (Chromium).

Además del protocolo, ofrece las operaciones de alto nivel que usa el controller
(evaluar JS, inyectar el script, navegar, clics y teclas "reales", captura). El
cliente de WebDriver BiDi (bidi.py, para Firefox) ofrece las mismas, así que el
controller no depende del navegador.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable

import websockets

log = logging.getLogger("cdp")


# Excepciones comunes a CDP y BiDi (los nombres quedaron por historia).
class CDPError(Exception):
    pass


class CDPClosed(CDPError):
    """Se perdió la conexión con el navegador."""


class ContextLost(CDPError):
    """El contexto de JavaScript desapareció un instante (navegación en curso): se puede reintentar."""


class SessionBusy(CDPError):
    """El navegador ya tiene otra sesión de control (Firefox admite una sola)."""


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


# Teclas para Input.dispatchKeyEvent: nombre -> (virtual key code, code, texto)
KEYDEFS: dict[str, tuple[int, str, str | None]] = {
    "Escape": (27, "Escape", None),
    "Enter": (13, "Enter", "\r"),
    " ": (32, "Space", " "),
    "Space": (32, "Space", " "),
    "Backspace": (8, "Backspace", None),
    "Tab": (9, "Tab", None),
    "ArrowLeft": (37, "ArrowLeft", None),
    "ArrowUp": (38, "ArrowUp", None),
    "ArrowRight": (39, "ArrowRight", None),
    "ArrowDown": (40, "ArrowDown", None),
    "PageUp": (33, "PageUp", None),
    "PageDown": (34, "PageDown", None),
    "Home": (36, "Home", None),
    "End": (35, "End", None),
}


def keydef(name: str) -> tuple[str, int, str, str | None]:
    if name in KEYDEFS:
        vk, code, text = KEYDEFS[name]
        return (" " if name == "Space" else name), vk, code, text
    if len(name) == 1:
        ch = name
        if ch.isdigit():
            return ch, ord(ch), f"Digit{ch}", ch
        if ch.isalpha():
            return ch, ord(ch.upper()), f"Key{ch.upper()}", ch
        return ch, 0, "", ch
    raise ValueError(f"tecla desconocida: {name}")


class CDP:
    engine = "chromium"

    def __init__(self, ws_url: str, on_event: Callable[[str, dict], None] | None = None):
        self.ws_url = ws_url
        self.on_event = on_event
        self.ws: Any = None
        self.closed = asyncio.Event()
        self.url = ""          # URL de la pestaña al conectarse
        self.version = ""      # versión del navegador (para el diagnóstico)
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
        try:
            res = await self.send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True},
                timeout=timeout,
            )
        except CDPClosed:
            raise
        except CDPError as exc:
            # "Cannot find context with specified id", "Execution context was destroyed"…
            if "context" in str(exc).lower():
                raise ContextLost(str(exc)) from exc
            raise
        if "exceptionDetails" in res:
            det = res["exceptionDetails"]
            text = det.get("exception", {}).get("description") or det.get("text")
            if "context" in str(text).lower() and "destroyed" in str(text).lower():
                raise ContextLost(str(text))
            raise CDPError(f"Error de JavaScript: {text}")
        return res.get("result", {}).get("value")

    # ------------------------------------------------------------ alto nivel
    async def setup_page(self, script: str) -> None:
        """Inyecta el script en cada documento nuevo de la pestaña."""
        await self.send("Page.enable")
        await self.send("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        try:
            # Que la página crea que tiene el foco aunque la ventana no lo tenga.
            await self.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except CDPError:
            pass

    async def navigate(self, url: str) -> None:
        await self.send("Page.navigate", {"url": url})

    async def reload(self) -> None:
        await self.send("Page.reload", {"ignoreCache": False})

    async def bring_to_front(self) -> None:
        await self.send("Page.bringToFront")

    async def mouse_move(self, x: float, y: float) -> None:
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    async def mouse_click(self, x: float, y: float) -> None:
        await self.mouse_move(x, y)
        await self.send("Input.dispatchMouseEvent",
                        {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1})
        await self.send("Input.dispatchMouseEvent",
                        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1})

    async def key_press(self, name: str) -> None:
        keyname, vk, code, text = keydef(name)
        down: dict[str, Any] = {
            "type": "keyDown" if text else "rawKeyDown", "key": keyname, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
        }
        if text:
            down["text"] = text
            down["unmodifiedText"] = text
        await self.send("Input.dispatchKeyEvent", down)
        await self.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": keyname, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
        })

    async def screenshot(self) -> bytes:
        r = await self.send("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(r["data"])

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


async def connect_chromium(port: int) -> CDP | None:
    """Se conecta a la pestaña de Netflix (o la primera común). None si Chromium no está escuchando."""
    try:
        page = pick_page(await list_pages(port))
    except Exception:  # noqa: BLE001 (no hay nada escuchando)
        return None
    if page is None:
        return None
    cdp = CDP(page["webSocketDebuggerUrl"])
    await asyncio.wait_for(cdp.open(), 5)
    cdp.url = page.get("url", "")
    try:
        cdp.version = (await http_json(port, "/json/version")).get("Browser", "")
    except (OSError, urllib.error.URLError, ValueError):
        pass
    return cdp
