"""Cliente mínimo de WebDriver BiDi (Firefox) con la misma interfaz que cdp.CDP.

Firefox ya no tiene CDP: su puerto --remote-debugging-port habla WebDriver BiDi.
Diferencias que importan:

- Admite UNA sola sesión a la vez. Si un cliente se desconecta sin `session.end`,
  la sesión queda tomada hasta que se cierre Firefox (y no se puede retomar por id).
  Por eso close() siempre manda `session.end`, y los comandos sueltos le piden las
  cosas al servicio (ipc.py) en vez de abrir otra sesión.
- Los valores de JS vuelven serializados con tipo ("RemoteValue"): se convierten acá.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import urllib.error
import urllib.request
from typing import Any

import websockets

from .cdp import CDPClosed, CDPError, ContextLost, SessionBusy

log = logging.getLogger("bidi")

# Firefox abre el puerto enseguida pero recién crea la sesión cuando la ventana terminó de abrir:
# en una Raspberry Pi eso puede tardar bastante más que unos segundos.
SESSION_NEW_TIMEOUT = 90.0

# Sesiones que Firefox todavía está creando y que se van a cerrar apenas responda (ver BiDi._new_session).
_LATE: set[asyncio.Task] = set()


def late_session_pending() -> bool:
    """Hay una sesión tardía por cerrar: un "ocupado" de Firefox es pasajero, no hay que reiniciarlo."""
    return any(not t.done() for t in _LATE)

# Teclas especiales de WebDriver (puntos de código del estándar).
SPECIAL_KEYS = {
    "Escape": "", "Enter": "", " ": " ", "Space": " ", "Backspace": "", "Tab": "",
    "ArrowLeft": "", "ArrowUp": "", "ArrowRight": "", "ArrowDown": "",
    "PageUp": "", "PageDown": "", "Home": "", "End": "",
}


def _http_ok(port: int, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as resp:  # noqa: S310
            return resp.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


async def endpoint_alive(port: int) -> bool:
    """El agente remoto de Firefox responde una página simple en "/"."""
    return await asyncio.to_thread(_http_ok, port, 1.5)


def deserialize(v: Any) -> Any:
    """RemoteValue de BiDi -> valor de Python (como returnByValue de CDP)."""
    if not isinstance(v, dict):
        return v
    t = v.get("type")
    val = v.get("value")
    if t in ("undefined", "null"):
        return None
    if t in ("string", "boolean"):
        return val
    if t == "number":
        if isinstance(val, str):  # "NaN", "-0", "Infinity", "-Infinity"
            return float(val.replace("Infinity", "inf"))
        return val
    if t == "bigint":
        return int(val)
    if t in ("array", "set"):
        return [deserialize(x) for x in (val or [])]
    if t in ("object", "map"):
        out: dict[str, Any] = {}
        for pair in val or []:
            k, item = pair
            out[k if isinstance(k, str) else str(deserialize(k))] = deserialize(item)
        return out
    if t == "date":
        return val
    return None  # funciones, nodos del DOM, etc.


class BiDi:
    engine = "firefox"

    def __init__(self, port: int):
        self.ws_url = f"ws://127.0.0.1:{port}/session"
        self.ws: Any = None
        self.closed = asyncio.Event()
        self.url = ""
        self.version = ""
        self.context: str | None = None
        self._session = False
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------ conexión
    async def open(self) -> None:
        self.ws = await websockets.connect(
            self.ws_url, max_size=None, ping_interval=None, open_timeout=5, compression=None
        )
        self._reader = asyncio.create_task(self._read())
        t0 = asyncio.get_running_loop().time()
        try:
            r = await self._new_session({"capabilities": {"alwaysMatch": {
                # No tocar los diálogos de la página (alert, "¿salir?"…): que se comporte normal.
                "unhandledPromptBehavior": {"default": "ignore"},
            }}})
        except CDPError as exc:
            await self._close_ws()
            if "maximum number of active sessions" in str(exc).lower():
                raise SessionBusy("Firefox ya tiene otra sesión de control") from exc
            raise
        self._session = True
        took = asyncio.get_running_loop().time() - t0
        if took > 5:
            log.info("Firefox tardó %.0f s en aceptar la sesión de control (todavía estaba abriendo)", took)
        caps = r.get("capabilities", {})
        self.version = f"{caps.get('browserName', 'firefox')} {caps.get('browserVersion', '')}".strip()
        try:
            await self.send("session.subscribe", {"events": ["browsingContext.contextDestroyed"]})
            await self._pick_context()
        except BaseException:
            await self.close()  # no dejar la sesión tomada
            raise

    async def _pick_context(self) -> None:
        """Prefiere la pestaña de Netflix; si no hay, la primera."""
        tree = await self.send("browsingContext.getTree", {"maxDepth": 0})
        ctxs = tree.get("contexts", [])
        if not ctxs:
            ctxs = [await self.send("browsingContext.create", {"type": "tab"}) | {"url": "about:blank"}]
        chosen = next((c for c in ctxs if "netflix.com" in c.get("url", "")), ctxs[0])
        self.context = chosen["context"]
        self.url = chosen.get("url", "")

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "event":
                    if (msg.get("method") == "browsingContext.contextDestroyed"
                            and msg.get("params", {}).get("context") == self.context):
                        log.warning("Firefox: se cerró la pestaña")
                        break
                    continue
                fut = self._pending.pop(msg.get("id"), None) if msg.get("id") is not None else None
                if fut is None or fut.done():
                    continue
                if kind == "success":
                    fut.set_result(msg.get("result", {}))
                else:
                    fut.set_exception(CDPError(f"{msg.get('error')}: {msg.get('message')}"))
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Error leyendo de WebDriver BiDi")
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPClosed("conexión cerrada"))
            self._pending.clear()

    async def _new_session(self, params: dict) -> dict:
        """session.new sin abandonarla nunca a medias.

        Si se deja de esperar (límite de tiempo o cancelación) mientras Firefox la crea, Firefox la
        termina de crear igual y queda tomada para siempre: el próximo intento recibe "maximum number
        of active sessions" y la única salida es reiniciar Firefox. Por eso, en ese caso, se sigue
        esperando la respuesta en segundo plano y se cierra la sesión con session.end.
        """
        if self.closed.is_set() or self.ws is None:
            raise CDPClosed("sin conexión con Firefox")
        msg_id = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self.ws.send(json.dumps({"id": msg_id, "method": "session.new", "params": params}))
        except websockets.exceptions.ConnectionClosed as exc:
            self._pending.pop(msg_id, None)
            raise CDPClosed(str(exc)) from exc
        try:
            return await asyncio.wait_for(asyncio.shield(fut), SESSION_NEW_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if not fut.done():
                log.warning("Firefox no respondió la sesión de control a tiempo: la cierro apenas responda")
                self._reaper = asyncio.get_running_loop().create_task(self._end_late_session(fut))
                _LATE.add(self._reaper)
                self._reaper.add_done_callback(_LATE.discard)
            raise

    async def _end_late_session(self, fut: asyncio.Future) -> None:
        try:
            await asyncio.wait_for(fut, 180)
            self._session = True
            await self.send("session.end", {}, timeout=20)  # < TimeoutStopSec del servicio
            log.info("Sesión de control tardía cerrada: Firefox queda libre")
        except Exception as exc:  # noqa: BLE001
            log.debug("No pude cerrar la sesión tardía: %s", exc)
        finally:
            self._session = False
            self._reaper = None
            await self._close_ws()

    async def send(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        if self.closed.is_set() or self.ws is None:
            raise CDPClosed("sin conexión con Firefox")
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

    # ------------------------------------------------------------ JS
    async def evaluate(self, expression: str, timeout: float = 10.0) -> Any:
        try:
            res = await self.send("script.evaluate", {
                "expression": expression, "target": {"context": self.context}, "awaitPromise": True,
                "resultOwnership": "none", "serializationOptions": {"maxObjectDepth": 32, "maxDomDepth": 0},
            }, timeout=timeout)
        except CDPClosed:
            raise
        except CDPError as exc:
            # Durante una navegación el "realm" desaparece y BiDi responde con error de protocolo.
            raise ContextLost(str(exc)) from exc
        if res.get("type") == "exception":
            det = res.get("exceptionDetails", {})
            raise CDPError(f"Error de JavaScript: {det.get('text') or deserialize(det.get('exception'))}")
        return deserialize(res.get("result"))

    async def setup_page(self, script: str) -> None:
        """Inyecta el script en cada documento nuevo (antes que los scripts de la página)."""
        await self.send("script.addPreloadScript", {"functionDeclaration": "() => {\n" + script + "\n}"})

    # ------------------------------------------------------------ navegación
    async def navigate(self, url: str) -> None:
        await self.send("browsingContext.navigate", {"context": self.context, "url": url, "wait": "none"})

    async def reload(self) -> None:
        await self.send("browsingContext.reload", {"context": self.context, "wait": "none"})

    async def bring_to_front(self) -> None:
        await self.send("browsingContext.activate", {"context": self.context})

    # ------------------------------------------------------------ entrada "real" (isTrusted)
    async def _pointer(self, steps: list[dict]) -> None:
        await self.send("input.performActions", {"context": self.context, "actions": [
            {"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": steps},
        ]})

    @staticmethod
    def _move(x: float, y: float) -> dict:
        return {"type": "pointerMove", "x": max(0, int(round(x))), "y": max(0, int(round(y))), "duration": 0}

    async def mouse_move(self, x: float, y: float) -> None:
        await self._pointer([self._move(x, y)])

    async def mouse_click(self, x: float, y: float) -> None:
        await self._pointer([self._move(x, y), {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}])

    async def key_press(self, name: str) -> None:
        value = SPECIAL_KEYS.get(name)
        if value is None:
            if len(name) != 1:
                raise ValueError(f"tecla desconocida: {name}")
            value = name
        await self.send("input.performActions", {"context": self.context, "actions": [
            {"type": "key", "id": "keyboard", "actions": [{"type": "keyDown", "value": value}, {"type": "keyUp", "value": value}]},
        ]})

    async def screenshot(self) -> bytes:
        r = await self.send("browsingContext.captureScreenshot", {"context": self.context})
        return base64.b64decode(r["data"])

    # ------------------------------------------------------------ cierre
    async def close(self) -> None:
        """Termina la sesión (si no, Firefox la deja tomada) y cierra la conexión."""
        if self._reaper:
            return  # la conexión queda abierta hasta cerrar la sesión tardía (_end_late_session)
        if self._session and not self.closed.is_set():
            try:
                await self.send("session.end", {}, timeout=20)  # < TimeoutStopSec del servicio
            except CDPClosed:
                pass  # Firefox cierra la conexión al terminar la sesión
            except (CDPError, asyncio.TimeoutError) as exc:
                log.warning("Firefox no confirmó el cierre de la sesión de control: %s", exc or "sin respuesta")
        self._session = False
        await self._close_ws()

    async def _close_ws(self) -> None:
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


async def connect_firefox(port: int) -> BiDi | None:
    """Abre una sesión BiDi. None si Firefox no está escuchando; SessionBusy si ya hay otra sesión."""
    if not await endpoint_alive(port):
        return None
    bidi = BiDi(port)
    try:
        await asyncio.wait_for(bidi.open(), SESSION_NEW_TIMEOUT + 15)
    except BaseException:
        await bidi.close()
        raise
    return bidi
