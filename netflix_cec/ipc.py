"""Socket local del servicio para los comandos sueltos (accion, diagnostico, navegador).

Firefox admite una sola sesión de control y el servicio la tiene tomada todo el
tiempo, así que esos comandos le piden las cosas al servicio en vez de abrir otra.
Con Chromium se usa igual, para que todo pase por el mismo lado.

Protocolo: una línea JSON de pedido y una de respuesta. El socket vive en
$XDG_RUNTIME_DIR (solo accesible por el usuario) y tiene permisos 0600.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cdp import CDPError

if TYPE_CHECKING:
    from .controller import Controller

log = logging.getLogger("ipc")
LIMIT = 4 * 1024 * 1024


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "netflix-cec.sock"


async def _handle_request(ctl: Controller, req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    if op == "info":
        page = ctl.page
        return {"ok": True, "engine": ctl.browser.engine, "name": ctl.browser.name, "port": ctl.browser.port,
                "connected": page is not None, "version": getattr(page, "version", ""),
                "url": (await ctl.js("ctx")).get("url", "") if page else ""}
    if op == "action":
        before = await ctl.context()
        ctl._ctx = before
        await ctl.run_action(req.get("action"))
        await asyncio.sleep(float(req.get("wait", 0.5)))
        return {"ok": True, "ctx": before, "after": await ctl.context()}
    if op == "js":
        if ctl.page is None:
            return {"ok": False, "error": f"{ctl.browser.name} no está conectado"}
        return {"ok": True, "result": await ctl.js(str(req.get("name")), *(req.get("args") or []))}
    if op == "navigate":
        if ctl.page is None:
            return {"ok": False, "error": f"{ctl.browser.name} no está conectado"}
        await ctl.navigate(str(req.get("url")))
        return {"ok": True}
    if op == "front":
        if ctl.page is None:
            return {"ok": False, "error": f"{ctl.browser.name} no está conectado"}
        await ctl.page.bring_to_front()
        return {"ok": True}
    return {"ok": False, "error": f"operación desconocida: {op!r}"}


async def serve(ctl: Controller) -> asyncio.AbstractServer | None:
    path = socket_path()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            req = json.loads(line or b"{}")
            log.debug("Pedido local: %s", req)
            try:
                resp = await _handle_request(ctl, req)
            except CDPError as exc:
                resp = {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                log.exception("Error atendiendo %s", req)
                resp = {"ok": False, "error": str(exc)}
            writer.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except (ValueError, ConnectionError):
            pass
        finally:
            writer.close()

    try:
        if path.is_socket() or path.exists():
            path.unlink()
        server = await asyncio.start_unix_server(handle, path=str(path), limit=LIMIT)
        os.chmod(path, 0o600)
    except OSError as exc:
        log.warning("No pude crear el socket %s (los comandos sueltos van a conectarse directo): %s", path, exc)
        return None
    log.debug("Escuchando pedidos locales en %s", path)
    return server


async def close(server: asyncio.AbstractServer | None) -> None:
    if server is None:
        return
    server.close()
    try:
        await asyncio.wait_for(server.wait_closed(), 2)
    except asyncio.TimeoutError:
        pass
    try:
        socket_path().unlink()
    except OSError:
        pass


async def request(req: dict[str, Any], timeout: float = 60.0) -> dict[str, Any] | None:
    """Manda un pedido al servicio. None si el servicio no está corriendo."""
    path = socket_path()
    if not path.exists():
        return None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path), limit=LIMIT), 3)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(json.dumps(req, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        return json.loads(line) if line else None
    except (OSError, ValueError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()
