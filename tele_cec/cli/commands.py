"""Comandos sueltos sobre el navegador: accion y navegador (le piden las cosas al servicio si está corriendo)."""

from __future__ import annotations

import argparse
import asyncio

from .. import config as config_mod
from ..apps.netflix.browser import Browser
from ..apps.netflix.cdp import SessionBusy
from ..controller import Controller
from . import ipc


# ---------------------------------------------------------------- conexión de los comandos sueltos
BUSY_HELP = ("{name} ya tiene una sesión de control (Firefox admite una sola): la del servicio u otro cliente.\n"
             "Sin --directo los comandos se la piden al servicio. Si el servicio no está corriendo, reiniciá {name}.")


async def _connect_direct(ctl: Controller) -> bool | None:
    """Conecta sin pasar por el servicio. None si la sesión está tomada (Firefox)."""
    try:
        return await ctl.connect_once()
    except SessionBusy:
        print(BUSY_HELP.format(name=ctl.browser.name))
        return None


# ---------------------------------------------------------------- accion
async def cmd_action(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    browser = Browser(cfg.browser)
    action = args.accion if len(args.accion) == 1 else list(args.accion)
    first = action if isinstance(action, str) else action[0]
    if not args.directo:
        resp = await ipc.request({"op": "action", "action": action, "wait": args.esperar})
        if resp is not None:
            if not resp.get("ok"):
                print(f"El servicio no pudo ejecutar la acción: {resp.get('error')}")
                return 1
            print(f"Contexto: {resp.get('ctx')}   (lo ejecutó el servicio: el detalle está en su log)")
            print(f"Contexto después: {resp.get('after')}")
            return 0
    ctl = Controller(cfg, browser)
    connected = await _connect_direct(ctl)
    if connected is None:
        return 1
    if not connected:
        if str(first).startswith("launch"):
            if await browser.launch():
                connected = await _connect_direct(ctl)
        if not connected:
            print(f"No hay {browser.name} escuchando en el puerto {browser.port}. Abrilo con:  tele-cec navegador")
            return 1
    try:
        ctl._ctx = await ctl.context()
        print(f"Contexto: {ctl._ctx}")
        await ctl.run_action(action)
        await asyncio.sleep(args.esperar)
        print(f"Contexto después: {await ctl.context()}")
    finally:
        await ctl.disconnect()
    return 0


# ---------------------------------------------------------------- navegador
async def cmd_browser(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    browser = Browser(cfg.browser)
    if await browser.alive():
        resp = await ipc.request({"op": "front"})
        if resp is None:
            ctl = Controller(cfg, browser)
            if await _connect_direct(ctl):
                try:
                    await ctl.page.bring_to_front()
                finally:
                    await ctl.disconnect()
        print(f"{browser.name} ya estaba abierto: lo traje al frente.")
        return 0
    ok = await browser.launch(args.url)
    if ok and browser.pending_url:
        # Abrió en la página negra del arranque: alguien tiene que llevarlo a Netflix.
        await _first_navigation(cfg, browser, args.url)
    print(f"{browser.name} abierto." if ok else f"No pude abrir {browser.name} (mirá el log de arriba).")
    return 0 if ok else 1


async def _first_navigation(cfg: config_mod.Config, browser: Browser, url: str | None) -> None:
    if await ipc.request({"op": "info"}) is not None:
        # El servicio se conecta solo (en unos segundos) y abre browser.start_url; si se pidió otra
        # URL, se la mando cuando esté conectado.
        if not url:
            return
        for _ in range(30):
            info = await ipc.request({"op": "info"})
            if info and info.get("connected"):
                await ipc.request({"op": "navigate", "url": url})
                return
            await asyncio.sleep(0.5)
        return
    ctl = Controller(cfg, browser)
    try:
        await _connect_direct(ctl)  # _setup navega a pending_url
    finally:
        await ctl.disconnect()
