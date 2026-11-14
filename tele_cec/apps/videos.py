"""Videos con VLC: la carpeta entera en pantalla completa, controles por MPRIS y por su interfaz de comandos."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..system.mpris import Mpris
from ..system.procs import run_cmd
from .folders import VIDEO_EXT
from .media import MediaActivity

log = logging.getLogger("actividades")


class VideosActivity(MediaActivity):
    id = "videos"
    name = "Videos"
    icon = "videos.svg"
    context = "videos"
    procs = {"vlc", "cvlc"}
    exts = VIDEO_EXT
    empty_text = "Sin videos: conectá un pendrive"
    play_label = "▶ Reproducir esta carpeta"
    empty_in_folder = "No hay videos en esta carpeta"

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.mpris = Mpris(str(self.cfg.get("mpris_player") or "vlc"), float(self.cfg.get("volume_step", 0.05)))
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        self.rc_socket = Path(runtime) / "tele-cec-vlc.sock"

    def installed(self) -> bool:
        return self._have("vlc")

    def default_args(self, folder: Path) -> list[str]:
        # -I dummy = sin ventana de VLC (solo el video). dbus = controles por MPRIS (playerctl). oldrc = comandos
        # por un socket local para pistas y proporción (en Wayland el video de VLC no recibe teclas).
        with contextlib.suppress(OSError):
            self.rc_socket.unlink()
        args = ["vlc", "-I", "dummy", "--extraintf=dbus:oldrc", f"--rc-unix={self.rc_socket}", "--rc-fake-tty",
                "--fullscreen", "--play-and-exit",
                "--video-title-timeout=3000", "--no-video-title-show" if self.cfg.get("hide_title") else "--video-title-show"]
        args.append("--random" if self.shuffle else "--no-random")
        return args + [str(folder)]

    async def close(self) -> None:
        if self.pids(fresh=True):
            await self.mpris.quit()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and self.pids(fresh=True):
                await asyncio.sleep(0.25)
        await self.terminate()

    async def pause(self) -> None:
        await self.mpris.ctl("pause")
        log.info("Video pausado por standby de la TV")

    async def rc(self, command: str, timeout: float = 1.5) -> str:
        """Un comando a la interfaz de comandos de VLC (socket UNIX); devuelve lo que contesta."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.rc_socket)), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("VLC no responde en %s: %s", self.rc_socket, exc)
            return ""
        out = b""
        try:
            writer.write((command + "\n").encode())
            await writer.drain()
            # Al conectarse VLC manda primero mensajes de estado; la respuesta termina con «<comando>: returned».
            deadline = time.monotonic() + timeout
            while b"returned" not in out and time.monotonic() < deadline:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), max(0.05, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                out += chunk
        finally:
            writer.close()
        return out.decode("utf-8", "replace")

    PAUSED = "\0pausa"  # cycle(): VLC no cambia pistas en pausa

    async def where(self) -> dict[str, Any]:
        """Qué video se está viendo y en qué segundo (para volver ahí después del reposo)."""
        rc, out = await run_cmd(["playerctl", "-p", self.mpris.player, "metadata", "--format",
                                 "{{xesam:url}}\t{{position}}"], 5)
        if rc != 0 or "\t" not in out:
            return {}
        url, _, pos = out.strip().partition("\t")
        from urllib.parse import unquote, urlparse  # noqa: PLC0415
        path = unquote(urlparse(url).path) if url.startswith("file:") else ""
        try:
            seconds = int(pos) // 1_000_000
        except ValueError:
            seconds = 0
        return {"file": path, "seconds": seconds} if path else {}

    async def go_to(self, file: str, seconds: int) -> bool:
        """En la lista de VLC, ir a ese archivo y a ese segundo (la lista es la carpeta que se abrió)."""
        name = Path(file).name
        target = ""
        for line in (await self.rc("playlist", timeout=3)).splitlines():
            line = line.strip().lstrip("|*").strip()
            ident, sep, rest = line.partition(" - ")
            if sep and ident.strip().isdigit() and rest.startswith(name):
                target = ident.strip()
                break
        if not target:
            log.info("Videos: no encontré «%s» en la lista de VLC", name)
            return False
        await self.rc(f"goto {target}")
        await asyncio.sleep(1.0)
        if seconds > 5:
            await self.rc(f"seek {max(0, seconds - 3)}")
        return True

    async def cycle(self, kind: str) -> str:
        """Pasa a la siguiente pista de audio (atrack), de subtítulos (strack) o proporción (vratio).

        VLC contesta la lista con la actual marcada con «*»: «| 2 - Español *». Devuelve el nombre de la
        elegida ("" si no hay lista, por ejemplo en pausa: VLC no cambia pistas así).
        """
        items: list[tuple[str, str, bool]] = []
        reply = await self.rc(kind)
        for line in reply.splitlines():
            line = line.rstrip()
            if not line.startswith("|") or " - " not in line:
                continue
            # «|  - Predeterminado *»: el valor puede estar vacío, así que se parte antes de recortar.
            ident, _, name = line[1:].partition(" - ")
            current = name.endswith(" *")
            items.append((ident.strip(), name.removesuffix(" *").strip(), current))
        if kind == "vratio":
            items = [it for it in items if it[0]]  # «Predeterminado» no tiene valor para volver a elegirlo
        elif kind == "atrack":
            items = [it for it in items if it[0] != "-1"]  # «Deshabilitar» = sin sonido: no va en el ciclo
        if not items:
            return self.PAUSED if "paus" in reply.lower() else ""
        idx = next((i for i, it in enumerate(items) if it[2]), -1)
        ident, name, _ = items[(idx + 1) % len(items)]
        await self.rc(f"{kind} {ident}")
        return name
