"""mpv por su canal de control (--input-ipc-server): comandos y avisos de cuándo cambia de archivo.

Lo usan Fotos («fotos y videos», «solo videos»), Videos y Música. El protocolo es JSON por un socket Unix, una línea
por mensaje: {"command": [...], "request_id": N} → {"error": "success", "data": ..., "request_id": N}, y además avisos
sueltos ({"event": "property-change", ...}).

Cada actividad abre su propio mpv con su propio canal, y lo reconoce por ese canal (mpv_pids): por el nombre del
programa no se puede, porque las tres usan mpv (y thumbfast abre otro mpv para las miniaturas).

Scripts que se cargan (con --load-scripts=no: nada de la configuración personal de mpv):
- mpv_scripts/tele_cec_osd.lua: la barra para la TV (título, tiempo, avance con miniatura al adelantar);
- mpv_scripts/thumbfast.lua (MPL 2.0, https://github.com/po5/thumbfast): las miniaturas del video;
- mpv_scripts/tele_cec_vo.lua (en la Pi): con qué salida se dibuja cada archivo (ver video_args);
- mpv-mpris (paquete del sistema), si está: publica lo que suena por MPRIS (la página del celular lo muestra).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..system.procs import find_pids

log = logging.getLogger("actividades")
SCRIPTS = Path(__file__).resolve().parent / "mpv_scripts"
MPRIS_PLUGIN = ("/usr/lib/mpv-mpris/mpris.so", "/etc/mpv/scripts/mpris.so", "/usr/share/mpv/scripts/mpris.so",
                "/usr/lib/x86_64-linux-gnu/mpv-mpris/mpris.so", "/usr/lib/aarch64-linux-gnu/mpv-mpris/mpris.so")


HWDEC = "auto-safe"   # fuera de la Pi (en la Pi lo elige tele_cec_vo.lua para cada archivo)
PI_DECODER = "bcm2835-codec-decode"


def pi_decoder() -> bool:
    """¿Está el decodificador de video por hardware de la Raspberry Pi (/dev/video10)?"""
    try:
        return any(n.read_text().strip() == PI_DECODER for n in Path("/sys/class/video4linux").glob("*/name"))
    except OSError:
        return False


def video_args() -> list[str]:
    """Con qué salida dibuja mpv y si decodifica por hardware.

    En la Pi (con Wayland), Vulkan no sabe leer los cuadros del decodificador por hardware (el video se veía todo
    azul) y dmabuf-wayland, que sí los muestra (fluido: 1080p a 60 cuadros sin perder), no muestra fotos. Así que
    arranca en gpu (Vulkan) sin hardware y tele_cec_vo.lua cambia a dmabuf-wayland + hardware para cada video H.264 o
    HEVC, y vuelve para las fotos. Sin el decodificador de la Pi: la salida y el hardware que elija mpv."""
    if pi_decoder() and os.environ.get("WAYLAND_DISPLAY"):
        return ["--vo=gpu", "--hwdec=no", f"--script={SCRIPTS / 'tele_cec_vo.lua'}"]
    return [f"--hwdec={HWDEC}"]


def mpv_pids(socket_path: Path, fresh: bool = False) -> list[int]:
    """El mpv que se abrió con ese canal de control (no otro mpv, ni el de las miniaturas)."""
    flag = f"--input-ipc-server={socket_path}"
    return find_pids(lambda argv: os.path.basename(argv[0]) == "mpv" and flag in argv, fresh)


def mpris_plugin() -> str:
    """El complemento mpv-mpris (sudo apt install mpv-mpris), o "" si no está."""
    return next((p for p in MPRIS_PLUGIN if os.path.isfile(p)), "")


def base_args(socket_path: Path, title: str) -> list[str]:
    """Lo que comparten todos los mpv de tele-cec: pantalla completa, sin la configuración personal ni la barra
    propia de mpv, sin puntero y con canal de control."""
    return ["mpv", "--fs", "--no-terminal", "--load-scripts=no", "--osc=no", "--osd-level=0", "--title=" + title,
            "--force-window=yes", "--cursor-autohide=always", "--keep-open=no", "--ytdl=no",
            f"--input-ipc-server={socket_path}"] + video_args()


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
