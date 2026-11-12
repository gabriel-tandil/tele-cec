"""Procesos auxiliares con su propio bucle de GLib (panel del menú, foco de Tidal) y el cliente del panel.

El panel (menu_ui.py) es un proceso aparte con su propio bucle de GTK. Se abre una sola vez y queda
oculto, así en una Raspberry Pi aparece al instante cuando se aprieta Menú. Si se cae, se vuelve a lanzar
en el próximo uso. HelperProcess es lo común: lanzarlo, esperar su {"ready": true} y mandarle órdenes en
JSON por stdin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from .browser import graphical_env, has_display
from .config import PKG_DIR

log = logging.getLogger("menu")

READY_TIMEOUT = 20.0   # GTK tarda en arrancar en una Pi
RETRY_AFTER = 30.0     # tras un fallo, no reintentar en cada tecla


class HelperProcess:
    module = ""          # p. ej. "tele_cec.menu_ui"
    what = "el auxiliar"
    logger = log

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.info: dict[str, Any] = {}
        self.error = ""
        self._lock = asyncio.Lock()
        self._failed_at = -1e9
        self._tasks: list[asyncio.Task] = []

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _start(self) -> bool:
        if self.running():
            return True
        if time.monotonic() - self._failed_at < RETRY_AFTER:
            return False
        env = graphical_env()
        if not has_display(env):
            self.error = "todavía no hay sesión gráfica"
            self._failed_at = time.monotonic()
            return False
        root = str(PKG_DIR.parent)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", self.module, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            return self._fail(f"no pude abrir {self.what}: {exc}")
        assert self.proc.stdout is not None and self.proc.stderr is not None
        self._tasks = [asyncio.create_task(self._drain(self.proc.stderr, "stderr"))]
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), READY_TIMEOUT)
        except asyncio.TimeoutError:
            self.proc.kill()
            return self._fail(f"{self.what} no respondió en {READY_TIMEOUT:.0f} s")
        try:
            info = json.loads(line.decode() or "{}")
        except ValueError:
            info = {}
        if not info.get("ready"):
            await asyncio.sleep(0.3)  # que el drenaje de stderr alcance a registrar el motivo
            return self._fail(f"{self.what} no arrancó (¿faltan python3-gi / gir1.2-gtk-3.0?)")
        self.info = info
        self.error = ""
        self._tasks.append(asyncio.create_task(self._drain(self.proc.stdout, "stdout")))
        self.on_ready(info)
        return True

    def on_ready(self, info: dict[str, Any]) -> None:
        self.logger.info("%s listo", self.what[:1].upper() + self.what[1:])

    def _fail(self, why: str) -> bool:
        self.error = why
        self._failed_at = time.monotonic()
        self.proc = None
        self.logger.warning("%s", why)
        return False

    async def _drain(self, stream: asyncio.StreamReader, name: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text and "Failed to load module" not in text:
                self.logger.debug("%s: %s", self.module.rpartition(".")[2], text)

    async def send(self, msg: dict[str, Any]) -> bool:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        for attempt in range(2):  # si el panel se había cerrado, un reintento con uno nuevo
            async with self._lock:
                if not await self._start():
                    return False
                assert self.proc is not None and self.proc.stdin is not None
                try:
                    self.proc.stdin.write(data)
                    await self.proc.stdin.drain()
                    return True
                except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
                    if not attempt:
                        self.logger.warning("Se cerró %s (%s): lo vuelvo a abrir", self.what, exc)
                    self.proc = None
                    self._failed_at = -1e9
        return False

    async def warm(self) -> None:
        """Al arrancar el servicio: abrirlo (oculto) apenas haya sesión gráfica."""
        for _ in range(40):
            if await self._start_quiet():
                return
            await asyncio.sleep(15)

    async def _start_quiet(self) -> bool:
        async with self._lock:
            self._failed_at = -1e9
            if not has_display(graphical_env()):
                return False
            return await self._start()

    async def close(self) -> None:
        if self.running():
            assert self.proc is not None
            try:
                if self.proc.stdin:
                    self.proc.stdin.write(b'{"op": "quit"}\n')
                    await self.proc.stdin.drain()
                await asyncio.wait_for(self.proc.wait(), 3)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError, RuntimeError):
                self.proc.kill()
        for t in self._tasks:
            t.cancel()
        self.proc = None


class Overlay(HelperProcess):
    """El panel del menú de actividades."""

    module = "tele_cec.menu_ui"
    what = "el panel del menú"

    @property
    def layer_shell(self) -> bool | None:
        return self.info.get("layer_shell") if self.info else None

    def on_ready(self, info: dict[str, Any]) -> None:
        log.info("Panel del menú listo (%s)", "layer-shell: encima de todo, sin tomar el foco"
                 if info.get("layer_shell") else "sin layer-shell: ventana común siempre encima")

    # ------------------------------------------------------------ órdenes
    async def show(self, items: list[dict], selected: int, title: str = "Actividades", hint: str = "",
                   numbers: bool = True) -> bool:
        return await self.send({"op": "show", "items": items, "selected": selected, "title": title, "hint": hint,
                                "numbers": numbers})

    async def help(self, title: str, rows: list[tuple[str, str]], top: int, page: str, hint: str) -> bool:
        return await self.send({"op": "help", "title": title, "rows": [list(r) for r in rows], "top": top,
                                "page": page, "hint": hint})

    async def select(self, index: int) -> bool:
        return await self.send({"op": "select", "index": index})

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
