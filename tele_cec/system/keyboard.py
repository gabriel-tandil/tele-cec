"""Teclas sintéticas para la ventana con el foco: wtype (apps Wayland) y xdotool (apps X11 por Xwayland)."""

from __future__ import annotations

import logging
import shutil

from .procs import run_cmd

log = logging.getLogger("teclado")


async def wtype_key(name: str) -> bool:
    """Manda una tecla a la ventana con el foco (apps Wayland, wtype).

    No sirve para apps X11 (Xwayland, como feh): Xwayland no usa el mapa de teclas propio de wtype y la
    tecla llega como otra (a feh le llega Escape y se cierra). Para esas, xdotool_key.
    """
    rc, out = await run_cmd(["wtype", "-k", name], 5)
    if rc != 0:
        log.warning("wtype %s: %s", name, out.strip() or rc)
    return rc == 0


async def xdotool_key(name: str) -> bool:
    """Manda una tecla a la ventana X11 con el foco (apps que corren por Xwayland, como feh)."""
    if not shutil.which("xdotool"):
        log.error("Falta xdotool (sudo apt install xdotool): sin él no puedo mandarle teclas a %s", name)
        return False
    rc, out = await run_cmd(["xdotool", "key", "--clearmodifiers", name], 5)
    if rc != 0:
        log.warning("xdotool %s: %s", name, out.strip() or rc)
    return rc == 0


async def wtype_text(text: str) -> bool:
    """Escribe un texto en la ventana con el foco (apps Wayland: YouTube, Tidal, Android)."""
    rc, out = await run_cmd(["wtype", "--", text], 10)
    if rc != 0:
        log.warning("wtype (texto): %s", out.strip() or rc)
    return rc == 0
