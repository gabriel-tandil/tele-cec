"""Cliente del panel del menú de actividades (menu_ui.py, un proceso GTK aparte).

El panel se abre una sola vez y queda oculto, así en una Raspberry Pi aparece al instante cuando se aprieta
Menú. Si se cae, se vuelve a lanzar en el próximo uso (ver system/helper.py).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..keys import parse_key
from ..system.helper import HelperProcess

log = logging.getLogger("menu")


class Overlay(HelperProcess):
    """El panel del menú de actividades."""

    module = "tele_cec.ui.menu_ui"
    what = "el panel del menú"

    def __init__(self, keyboard: bool = False, on_key: Callable[[int], None] | None = None,
                 on_release: Callable[[], None] | None = None) -> None:
        """keyboard: mientras el menú está a la vista, manejarlo también con el teclado (flechas, Enter, Esc…):
        el panel manda las teclas y llegan a on_key / on_release como si fueran del control."""
        super().__init__()
        self.keyboard = keyboard
        self.extra_env = {"TELE_CEC_MENU_KEYBOARD": "1" if keyboard else "0"}
        self.on_key = on_key
        self.on_release = on_release

    def on_message(self, msg: dict[str, Any]) -> None:
        if msg.get("release"):
            if self.on_release:
                self.on_release()
            return
        code = parse_key(str(msg.get("key") or ""))
        if code is not None and self.on_key:
            log.debug("Tecla del teclado en el menú: %s", msg.get("key"))
            self.on_key(code)

    @property
    def layer_shell(self) -> bool | None:
        return self.info.get("layer_shell") if self.info else None

    def on_ready(self, info: dict[str, Any]) -> None:
        log.info("Panel del menú listo (%s%s)", "layer-shell: encima de todo"
                 if info.get("layer_shell") else "sin layer-shell: ventana común siempre encima",
                 "; con el teclado mientras se ve el menú" if self.keyboard else "")

    # ------------------------------------------------------------ órdenes
    async def show(self, items: list[dict], selected: int, title: str = "Actividades", hint: str = "",
                   numbers: bool = True, columns: int = 1, chip: bool = False, qr: dict | None = None) -> bool:
        """qr: {"path", "title", "text", "url"}: el QR de la página del celular abajo de la lista."""
        msg = {"op": "show", "items": items, "selected": selected, "title": title, "hint": hint,
               "numbers": numbers, "columns": columns, "chip": chip}
        if qr:
            msg["qr"] = qr
        return await self.send(msg)

    async def help(self, title: str, rows: list[tuple[str, str]], top: int, page: str, hint: str) -> bool:
        return await self.send({"op": "help", "title": title, "rows": [list(r) for r in rows], "top": top,
                                "page": page, "hint": hint})

    async def image(self, title: str, path: str, lines: list[str], status: str = "", hint: str = "",
                    size: float = 9) -> bool:
        """Una imagen (el QR para vincular un celular) con unas líneas al lado; size en tamaños de letra."""
        return await self.send({"op": "image", "title": title, "path": path, "lines": lines, "status": status,
                                "hint": hint, "size": size})

    async def status(self, text: str) -> bool:
        """Cambia la línea de estado de la página de la imagen."""
        return await self.send({"op": "status", "text": text})

    async def select(self, index: int, chip: bool = False) -> bool:
        """chip: el foco en el botoncito de la fila («Carpetas», «Conectar»)."""
        return await self.send({"op": "select", "index": index, "chip": chip})

    async def busy(self, text: str) -> bool:
        return await self.send({"op": "busy", "text": text})

    async def toast(self, text: str, ms: int = 2000) -> bool:
        return await self.send({"op": "toast", "text": text, "ms": ms})

    async def hide(self) -> bool:
        if not self.running():
            return True
        return await self.send({"op": "hide"})


def available() -> tuple[bool, bool, str]:
    """Para el diagnóstico: (GTK 3 disponible, gtk-layer-shell disponible, detalle)."""
    try:
        import gi  # noqa: PLC0415
        gi.require_version("Gtk", "3.0")
    except (ImportError, ValueError) as exc:
        return False, False, f"falta GTK 3 para Python ({exc}): sudo apt install python3-gi gir1.2-gtk-3.0"
    try:
        gi.require_version("GtkLayerShell", "0.1")
    except ValueError:
        return True, False, "falta gtk-layer-shell: sudo apt install gir1.2-gtklayershell-0.1"
    return True, True, "GTK 3 y gtk-layer-shell"
