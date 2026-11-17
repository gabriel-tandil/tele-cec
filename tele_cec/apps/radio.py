"""Radio por internet con mpv: las emisoras de [apps.radio] stations, en vivo.

En la TV, una pantalla propia (mpv_scripts/tele_cec_radio.lua): la emisora, «En vivo» y el tema que suena (lo manda
la emisora), con la tapa del disco si se encuentra en línea (covers.py). ◀ ▶ (o Canal ±) cambian de radio, OK pausa
y ▲▼ el volumen. Se acuerda de la última radio que sonó: OK en el menú la pone de una, y el botoncito «Radios» (o ▶)
abre la lista para elegir otra. Puede quedar sonando de fondo mientras pasan las fotos, como la música.

Una transmisión en vivo no se puede pausar de verdad: después de una pausa larga, al seguir se vuelve a conectar
para escuchar lo que está saliendo ahora (y no lo de hace un rato).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from ..system.mpris import Mpris
from ..system.procs import LOG_DIR
from . import covers
from .base import ProcActivity
from .mpv import SCRIPTS, Mpv, base_args, mpris_plugin, mpv_pids

log = logging.getLogger("actividades")

SCREEN = "tele_cec_radio"   # el script de la pantalla de la TV
LIVE_AGAIN_S = 20           # una pausa más larga que esto: al seguir, se vuelve a conectar (en vivo de nuevo)
BUFFER_S = 3                # segundos de colchón antes de empezar a sonar (la de 320 kbps llega justo a tiempo)
COVER_PX = 480              # la tapa del disco en la pantalla de la TV (píxeles, en 1080p)


def stations_from(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """Las emisoras de [apps.radio] stations: [{name, url}], sin las que no tienen dirección."""
    out = []
    for i, st in enumerate(cfg.get("stations") or []):
        if isinstance(st, str):
            st = {"url": st}
        if not isinstance(st, dict) or not str(st.get("url") or "").strip():
            log.warning("[apps.radio] stations: la %d.ª no tiene url", i + 1)
            continue
        url = str(st["url"]).strip()
        out.append({"name": str(st.get("name") or "").strip() or url.split("//")[-1].split("/")[0], "url": url})
    return out


class RadioActivity(ProcActivity):
    id = "radio"
    name = "Radio"
    icon = "radio.svg"
    context = "radio"
    audio_only = True     # con la TV apagada y el sonido por Bluetooth, sigue
    TITLE = "tele-cec radio"
    fail_text = ""

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.cfg = cfg
        self.name = str(cfg.get("name") or self.name)
        self.icon = str(cfg.get("icon") or self.icon)
        self.stations = stations_from(cfg)
        self.mpv = Mpv(LOG_DIR / "radio-mpv.sock")
        self.mpris = Mpris(str(cfg.get("mpris_player") or "mpv"), float(cfg.get("volume_step", 0.05)))
        self._paused_at: float | None = None
        self.want_station: int | None = None   # la próxima vez que se abra, esta (la elegida en el menú)
        self.covers = bool(cfg.get("covers", True))   # buscar la tapa del disco de lo que suena (en iTunes)
        self._song = ""                        # «Artista - Tema» que está saliendo (lo que mandó la emisora)
        self._cover_task: asyncio.Task | None = None
        self.cover_path: Path | None = None    # la tapa de lo que suena (la página del celular la muestra)

    # ------------------------------------------------------------ menú
    def installed(self) -> bool:
        return bool(shutil.which("mpv"))

    def source(self) -> tuple[str, bool]:
        if not self.stations:
            return "Sin radios: agregalas en [apps.radio] stations", False
        if self.has_last() and len(self.stations) > 1:  # OK la pone; «Radios ▸» para elegir otra
            return f"{self.last_name()} (la última) · {len(self.stations)} radios", True
        return " · ".join(s["name"] for s in self.stations), True

    def unavailable_text(self) -> str:
        return "No hay radios: agregalas en [apps.radio] stations (config.toml)"

    # ------------------------------------------------------------ la última que sonó
    def _last_file(self) -> Path:
        return LOG_DIR / "radio-ultima.txt"

    def _last_url(self) -> str:
        try:
            return self._last_file().read_text("utf-8").strip()
        except OSError:
            return ""

    def last_index(self) -> int:
        url = self._last_url()
        return next((i for i, s in enumerate(self.stations) if s["url"] == url), 0)

    def has_last(self) -> bool:
        """Ya sonó alguna de las radios de la lista (OK en el menú la vuelve a poner sin preguntar)."""
        url = self._last_url()
        return bool(url) and any(s["url"] == url for s in self.stations)

    def last_name(self) -> str:
        return self.stations[self.last_index()]["name"] if self.stations else ""

    def _remember(self, index: int) -> None:
        if 0 <= index < len(self.stations):
            try:
                self._last_file().parent.mkdir(parents=True, exist_ok=True)
                self._last_file().write_text(self.stations[index]["url"] + "\n", "utf-8")
            except OSError as exc:
                log.debug("Radio: no pude guardar la última (%s)", exc)

    # ------------------------------------------------------------ proceso
    def pids(self, fresh: bool = False) -> list[int]:
        return mpv_pids(self.mpv.socket_path, fresh)

    def playlist_file(self) -> Path:
        return LOG_DIR / "radio.m3u"

    def playlist_text(self) -> str:
        lines = ["#EXTM3U"]
        for s in self.stations:
            lines += [f"#EXTINF:-1,{s['name'].replace(chr(10), ' ')}", s["url"]]
        return "\n".join(lines) + "\n"

    def args(self, start: int) -> list[str]:
        args = base_args(self.mpv.socket_path, self.TITLE)
        plugin = mpris_plugin()
        if plugin:
            args.append(f"--script={plugin}")  # qué suena, para la página del celular
        args += [f"--script={SCRIPTS / (SCREEN + '.lua')}",
                 "--audio-display=no", "--background-color=#101014",
                 # Un colchón de unos segundos antes de sonar: absorbe los tirones de la red.
                 "--cache=yes", "--cache-pause-initial=yes", f"--cache-pause-wait={BUFFER_S}",
                 # Si la emisora corta la conexión, ffmpeg vuelve a conectarse solo.
                 "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,reconnect_delay_max=15",
                 "--loop-playlist=inf",  # y si igual termina, mpv la vuelve a abrir (no se cierra la Radio)
                 f"--playlist={self.playlist_file()}", f"--playlist-start={start}"]
        return args

    async def open(self, station: int | None = None) -> bool:
        self.fail_text = ""
        if not self.stations:
            self.fail_text = self.unavailable_text()
            return False
        if station is None:
            station, self.want_station = self.want_station, None
        start = station if station is not None and 0 <= station < len(self.stations) else self.last_index()
        playlist = self.playlist_file()
        try:
            playlist.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(playlist.write_text, self.playlist_text(), "utf-8")
        except OSError as exc:
            log.warning("Radio: no pude armar la lista (%s)", exc)
            return False
        self.mpv.socket_path.unlink(missing_ok=True)
        self._paused_at = None
        if not await self.spawn(self.args(start)):
            return False
        self._remember(start)
        log.info("Radio: %s (%s)", self.stations[start]["name"], self.stations[start]["url"])
        if not await self.mpv.ready():
            log.warning("Radio: mpv no abrió su canal de control")
        self._song, self.cover_path = "", None
        if self.covers:
            self.mpv.watch(self._on_metadata, "metadata")  # el tema que sale: para buscarle la tapa
        return True

    async def close(self) -> None:
        self.mpv.stop_watch()
        self._cancel_cover()
        if self.pids(fresh=True):
            await self.mpv.command("quit", timeout=2)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and self.pids(fresh=True):
                await asyncio.sleep(0.25)
        await self.terminate()
        self._paused_at = None
        self._song, self.cover_path = "", None

    # ------------------------------------------------------------ la tapa del disco
    @staticmethod
    def song_of(meta: Any) -> str:
        """«Artista - Tema» de los metadatos de mpv (icy-title), o ""."""
        if isinstance(meta, dict):
            for k, v in meta.items():
                if k.lower() == "icy-title" and str(v).strip():
                    return str(v).strip()
        return ""

    def _cancel_cover(self) -> None:
        if self._cover_task is not None and not self._cover_task.done():
            self._cover_task.cancel()
        self._cover_task = None

    async def _on_metadata(self, meta: Any) -> None:
        """Cambió lo que manda la emisora: otro tema → se saca la tapa de antes y se busca la de este."""
        song = self.song_of(meta)
        if song == self._song:
            return
        self._song = song
        self._cancel_cover()
        if self.cover_path is not None:
            self.cover_path = None
            await self.mpv.command("script-message-to", SCREEN, "nocover")
        if song:
            self._cover_task = asyncio.get_running_loop().create_task(self._show_cover(song))

    async def _show_cover(self, song: str) -> None:
        parts = covers.song_parts(song)
        if parts is None:
            return  # sin «Artista - Tema» no se busca (saldría cualquier disco)
        try:
            jpg = await asyncio.to_thread(covers.find, *parts)
            raw = await asyncio.to_thread(covers.raw_bgra, jpg, COVER_PX) if jpg else None
        except Exception:  # noqa: BLE001
            log.exception("Radio: error buscando la tapa de «%s»", song)
            return
        if song != self._song or raw is None or jpg is None:
            return  # ya cambió el tema, o no hay tapa
        self.cover_path = jpg
        ok, _ = await self.mpv.command("script-message-to", SCREEN, "cover", str(raw), str(COVER_PX), str(COVER_PX))
        if ok:
            log.info("Radio: tapa de «%s»", song)

    # ------------------------------------------------------------ controles
    async def current(self) -> int:
        pos = await self.mpv.get("playlist-pos")
        return pos if isinstance(pos, int) else -1

    async def station_name(self) -> str:
        i = await self.current()
        return self.stations[i]["name"] if 0 <= i < len(self.stations) else self.name

    async def set_pause(self, on: bool) -> bool:
        if not on and self._paused_at is not None and time.monotonic() - self._paused_at > LIVE_AGAIN_S:
            return await self.play_station(await self.current())  # en vivo otra vez, no lo de hace un rato
        ok = await self.mpv.set("pause", on)
        if ok:
            self._paused_at = time.monotonic() if on else None
        return ok

    async def play_pause(self) -> bool:
        paused = await self.mpv.get("pause")
        return await self.set_pause(not paused)

    async def pause(self) -> None:
        """La TV pasó a standby."""
        if await self.set_pause(True):
            log.info("Radio en pausa por standby de la TV")

    async def play_station(self, index: int) -> bool:
        """Esa emisora (de la lista), desde ahora."""
        if not 0 <= index < len(self.stations):
            return False
        ok = (await self.mpv.command("playlist-play-index", index))[0]
        if ok:
            await self.mpv.set("pause", False)
            self._paused_at = None
            self._remember(index)
            log.info("Radio: %s", self.stations[index]["name"])
        return ok

    async def step(self, delta: int) -> str:
        """Otra radio (⏭ +1, ⏮ −1, dando la vuelta). Devuelve su nombre ("" si hay una sola)."""
        n = len(self.stations)
        if n < 2:
            return ""
        cur = await self.current()
        nxt = ((cur if cur >= 0 else 0) + delta) % n
        return self.stations[nxt]["name"] if await self.play_station(nxt) else ""

    async def volume(self, sign: str) -> int | None:
        step = round(float(self.cfg.get("volume_step", 0.05)) * 100) or 5
        ok, _ = await self.mpv.command("add", "volume", step if sign != "-" else -step)
        vol = await self.mpv.get("volume") if ok else None
        return round(vol) if isinstance(vol, (int, float)) else None

    async def playback(self) -> str:
        """Playing / Paused (para el menú: «sonando» o «en pausa» si queda de fondo)."""
        paused = await self.mpv.get("pause")
        if paused is None:
            return ""
        return "Paused" if paused else "Playing"

    async def now_playing(self) -> str:
        """«Artista - Tema» de lo que está saliendo (si la emisora lo manda), o ""."""
        return self.song_of(await self.mpv.get("metadata"))

