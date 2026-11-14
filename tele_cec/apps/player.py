"""Videos y Música locales con mpv: la carpeta entera (con sus subcarpetas), en pantalla completa.

Todo se maneja por el canal de control de mpv (Mpv): play/pausa, siguiente, volumen, pistas de audio y subtítulos,
proporción. La barra para la TV es un script de mpv (mpv_scripts/tele_cec_osd.lua): aparece al pausar o adelantar y
deja «buscar» con ◀ ▶ antes de saltar, mostrando el tiempo y (en videos, con thumbfast) una miniatura de ese punto.
En Música la barra queda siempre a la vista, con la tapa del disco si la hay.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ..system.mpris import Mpris
from ..system.procs import LOG_DIR
from .folders import scan_media
from .media import MediaActivity
from .mpv import SCRIPTS, Mpv, base_args, mpris_plugin, mpv_pids

log = logging.getLogger("actividades")
OSD = "tele_cec_osd"   # nombre del script de la barra (para script-message-to)


def _natural(path: str) -> list:
    """Orden natural por carpeta y nombre: «2 - Tema» antes que «10 - Tema»."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path)]


class MpvMediaActivity(MediaActivity):
    """Base de Videos y Música: mpv con la lista de la carpeta y su canal de control."""

    audio_only = False     # Música: la barra queda siempre a la vista, sin miniaturas
    thumbnails = True      # Videos: miniaturas al buscar (thumbfast)
    TITLE = "tele-cec"
    fail_text = ""

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self.mpv = Mpv(LOG_DIR / f"{self.id}-mpv.sock")
        # MPRIS (mpv-mpris): para la página del celular (qué suena) y para preferir este reproductor.
        self.mpris = Mpris(str(self.cfg.get("mpris_player") or "mpv"), float(self.cfg.get("volume_step", 0.05)))
        # Temas elegidos desde el celular para el próximo open (en vez de la carpeta): (archivos, desde cuál).
        self.want_files: tuple[list[str], int] | None = None

    # ------------------------------------------------------------ proceso
    def installed(self) -> bool:
        return self._have("mpv")

    def pids(self, fresh: bool = False) -> list[int]:
        if self.command:
            return super().pids(fresh)
        return mpv_pids(self.mpv.socket_path, fresh)

    def playlist_file(self) -> Path:
        return LOG_DIR / f"{self.id}-mpv.lst"

    def files(self, folder: Path) -> list[str]:
        """Los archivos de la carpeta y sus subcarpetas, en orden (carpeta por carpeta, orden natural)."""
        return sorted(scan_media(folder, self.exts), key=_natural)

    def scripts(self) -> list[str]:
        out = []
        plugin = mpris_plugin()
        if plugin:
            out.append(f"--script={plugin}")
        out.append(f"--script={SCRIPTS / (OSD + '.lua')}")
        opts = [f"{OSD}-audio={'yes' if self.audio_only else 'no'}"]
        if self.thumbnails and not self.audio_only and (SCRIPTS / "thumbfast.lua").is_file():
            out.append(f"--script={SCRIPTS / 'thumbfast.lua'}")
            # Miniaturas grandes (se ven desde el sillón); sin aceleración (en la Pi el decodificador es uno solo).
            opts += ["thumbfast-max_width=384", "thumbfast-max_height=216", "thumbfast-hwdec=no",
                     "thumbfast-spawn_first=yes", "thumbfast-network=no"]
        out.append("--script-opts=" + ",".join(opts))
        return out

    def default_args(self, folder: Path, playlist: Path | None = None, start: int | None = None) -> list[str]:
        args = base_args(self.mpv.socket_path, self.TITLE) + self.scripts() + self.extra_args()
        if start is not None:
            args.append(f"--playlist-start={start}")  # temas elegidos: en ese orden, desde ese
        elif self.shuffle:
            args.append("--shuffle")
        return args + [f"--playlist={playlist}" if playlist else str(folder)]

    def extra_args(self) -> list[str]:
        return []

    async def open(self, folder: Path | None = None) -> bool:
        self.fail_text = ""
        if self.command:
            return await super().open(folder)
        start = None
        if self.want_files:
            (files, start), self.want_files = self.want_files, None
            folder = folder or Path(os.path.commonpath(files) if len(files) > 1 else os.path.dirname(files[0]))
        else:
            if folder is None:
                folder = await asyncio.to_thread(self.folder)
            if folder is None:
                log.warning("%s: no hay contenido en %s", self.name,
                            ", ".join(map(str, self.folders)) or "(sin carpetas)")
                return False
            if await asyncio.to_thread(folder.is_file):  # un archivo elegido en el menú (Videos): solo ese
                files, folder = [str(folder)], folder.parent
            else:
                files = await asyncio.to_thread(self.files, folder)
        if not files:
            self.fail_text = self.empty_in_folder
            return False
        playlist = await self._write_playlist(files)
        if playlist is None:
            return False
        self.mpv.socket_path.unlink(missing_ok=True)
        if not await self.spawn(self.default_args(folder, playlist, start)):
            return False
        self.current_folder = folder
        log.info("%s: %s (%d archivos)", self.name, folder, len(files))
        if not await self.mpv.ready():
            log.warning("%s: mpv no abrió su canal de control", self.name)
        return True

    async def _write_playlist(self, files: list[str]) -> Path | None:
        playlist = self.playlist_file()
        try:
            playlist.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(playlist.write_text, "\n".join(files) + "\n", "utf-8")
        except OSError as exc:
            log.warning("%s: no pude armar la lista (%s)", self.name, exc)
            return None
        return playlist

    # ------------------------------------------------------------ la lista (desde el celular)
    async def play_files(self, files: list[str], start: int = 0) -> bool:
        """Con mpv abierto: cambia la lista por estos temas y arranca en el número `start` (sin que suene antes el
        primero: playlist-start vale para el loadlist que sigue)."""
        playlist = await self._write_playlist(files)
        if playlist is None:
            return False
        await self.mpv.set("options/shuffle", False)
        await self.mpv.set("options/playlist-start", max(0, min(start, len(files) - 1)))
        ok = (await self.mpv.command("loadlist", str(playlist), "replace"))[0]
        await self.mpv.set("options/playlist-start", "auto")
        self.shuffled = False
        if ok:
            await self.mpv.set("pause", False)
            await self.osd("show")
        return ok

    async def enqueue(self, path: str, next_: bool) -> bool:
        """Un tema más: que suene después del actual o al final de la lista."""
        return (await self.mpv.command("loadfile", path, "insert-next" if next_ else "append"))[0]

    async def jump(self, index: int) -> bool:
        ok = (await self.mpv.command("playlist-play-index", index))[0]
        if ok:
            await self.mpv.set("pause", False)
        return ok

    async def playlist(self) -> tuple[list[str], int]:
        """Los archivos de la lista y cuál suena (-1 = ninguno)."""
        entries = await self.mpv.get("playlist") or []
        files = [str(e.get("filename") or "") for e in entries if isinstance(e, dict)]
        pos = await self.mpv.get("playlist-pos")
        return files, pos if isinstance(pos, int) else -1

    async def close(self) -> None:
        if self.pids(fresh=True) and not self.command:
            await self.mpv.command("quit", timeout=2)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and self.pids(fresh=True):
                await asyncio.sleep(0.25)
        await self.terminate()

    # ------------------------------------------------------------ controles
    async def osd(self, message: str, *args: Any) -> bool:
        """Un aviso a la barra de la TV (tele_cec_osd.lua): show, scrub ±s, ok, cancel."""
        return (await self.mpv.command("script-message-to", OSD, message, *map(str, args)))[0]

    async def play_pause(self) -> bool:
        """OK / ⏯: si se está buscando con ◀ ▶, salta ahí; si no, play o pausa (lo decide la barra)."""
        if await self.osd("ok"):
            return True
        return (await self.mpv.command("cycle", "pause"))[0]

    async def set_pause(self, on: bool) -> bool:
        ok = await self.mpv.set("pause", on)
        await self.osd("show")
        return ok

    async def pause(self) -> None:
        """La TV pasó a standby."""
        if await self.mpv.set("pause", True):
            log.info("%s en pausa por standby de la TV", self.name)

    async def seek(self, seconds: int) -> bool:
        """◀ ▶: buscar (la barra muestra a dónde, con miniatura, y salta al soltar); sin barra, salta directo."""
        if await self.osd("scrub", seconds):
            return True
        return (await self.mpv.command("seek", seconds, "relative"))[0]

    async def next(self) -> bool:
        return (await self.mpv.command("playlist-next", "force"))[0]

    async def previous(self) -> bool:
        return (await self.mpv.command("playlist-prev", "force"))[0]

    async def volume(self, sign: str) -> int | None:
        step = round(float(self.cfg.get("volume_step", 0.05)) * 100) or 5
        ok, _ = await self.mpv.command("add", "volume", step if sign != "-" else -step)
        vol = await self.mpv.get("volume") if ok else None
        return round(vol) if isinstance(vol, (int, float)) else None

    async def toggle_shuffle(self) -> bool:
        """Mezcla la lista (o vuelve al orden de la carpeta). Devuelve si quedó mezclada."""
        self.shuffled = not getattr(self, "shuffled", self.shuffle)
        await self.mpv.command("playlist-shuffle" if self.shuffled else "playlist-unshuffle")
        await self.osd("show")
        return self.shuffled

    async def cycle(self, kind: str) -> str:
        """Pasa a la siguiente pista de subtítulos (sub), de audio (audio) o proporción (aspect). Devuelve cómo
        quedó ("" = este archivo no tiene opciones)."""
        if kind == "aspect":
            await self.mpv.command("cycle-values", "video-aspect-override", "16:9", "4:3", "2.35:1", "-1")
            value = await self.mpv.get("video-aspect-override")
            try:
                ratio = float(value)
            except (TypeError, ValueError):
                return "Original"
            names = {1.778: "16:9", 1.333: "4:3", 2.35: "2.35:1"}
            return next((n for r, n in names.items() if abs(ratio - r) < 0.01), "Original" if ratio <= 0 else f"{ratio:.2f}")
        tracks = [t for t in (await self.mpv.get("track-list") or []) if isinstance(t, dict) and t.get("type") == kind]
        if not tracks:
            return ""
        await self.mpv.command("cycle", "sid" if kind == "sub" else "aid")
        cur = await self.mpv.get(f"current-tracks/{kind}")
        if not isinstance(cur, dict):
            return "No"
        lang, title = str(cur.get("lang") or ""), str(cur.get("title") or "")
        return " · ".join(x for x in (LANGS.get(lang, lang), title) if x) or f"Pista {cur.get('id')}"

    async def playback(self) -> str:
        """Playing / Paused (para el menú: «sonando» o «en pausa» si queda de fondo)."""
        paused = await self.mpv.get("pause")
        if paused is None:
            return ""
        return "Paused" if paused else "Playing"

    # ------------------------------------------------------------ reposo
    async def where(self) -> dict[str, Any]:
        """Qué archivo se está viendo y en qué segundo (para volver ahí después del reposo)."""
        path, pos = await self.mpv.get("path"), await self.mpv.get("time-pos")
        if not path:
            return {}
        return {"file": str(path), "seconds": int(pos or 0)}

    async def go_to(self, file: str, seconds: int) -> bool:
        """Ir a ese archivo de la lista y a ese segundo."""
        playlist = await self.mpv.get("playlist") or []
        idx = next((i for i, e in enumerate(playlist) if isinstance(e, dict) and e.get("filename") == file), None)
        if idx is None:
            log.info("%s: no encontré «%s» en la lista", self.name, os.path.basename(file))
            return False
        await self.mpv.command("playlist-play-index", idx)
        await asyncio.sleep(1.0)
        if seconds > 5:
            await self.mpv.command("seek", max(0, seconds - 3), "absolute")
        return True


LANGS = {"spa": "Español", "es": "Español", "eng": "Inglés", "en": "Inglés", "por": "Portugués", "pt": "Portugués",
         "fre": "Francés", "fra": "Francés", "fr": "Francés", "ita": "Italiano", "it": "Italiano", "ger": "Alemán",
         "deu": "Alemán", "de": "Alemán", "jpn": "Japonés", "ja": "Japonés"}


def have_mpv() -> bool:
    return bool(shutil.which("mpv"))
