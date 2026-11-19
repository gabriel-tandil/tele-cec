"""Fotos: presentación en pantalla completa, zoom y qué foto se está viendo.

Todo con mpv: la placa de video achica las fotos, hace el zoom y las mueve, y la foto siguiente se lee mientras se
ve la actual. feh (todo con el procesador) queda para «solo fotos» con [apps.fotos] viewer = "feh" o sin mpv.
Tres modos (▼ pasa de uno a otro mientras se mira, y se pueden elegir en «Carpetas ▸»):
- «solo fotos» (el de siempre);
- «fotos y videos»: las fotos el tiempo de siempre y los videos en el medio (ordenados por la fecha del nombre, así
  cada video cae en su lugar);
- «solo videos»: los videos de la carpeta.
Si la carpeta tiene subcarpetas con fotos o videos (los años, los viajes…), la presentación va en orden aleatorio en
los tres modos (también con [apps.fotos] shuffle = true); al cambiar de modo con ▼ sigue desde la foto que se veía.
«Un día como hoy» también va al azar (con [apps.fotos] today_shuffle = false, del año más viejo al más nuevo).
La fecha de cada foto en una esquina (CC en el control, con mpv): el día, o el mes, o el año, según lo que se sepa
(del nombre, de la foto o de las carpetas). Queda prendida o apagada para las próximas veces.
Favoritas (1 en el control): guarda la foto que se ve en una lista (~/.local/share/tele-cec/fotos-favoritas.txt);
otra vez la saca. En Carpetas ▸ está «♥ Favoritas» para verlas, y en la esquina se ve un ♥ en las guardadas.
Mientras suena un video, la música de Tidal de fondo se pausa (on_video, lo pone el controlador).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import shutil
import signal
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..system.keyboard import xdotool_key
from ..system.procs import DATA_DIR, LOG_DIR, find_pids, run_cmd
from . import photodates
from .folders import IMAGE_EXT, VIDEO_EXT, describe_folder, scan_media
from .media import MediaActivity
from .mpv import SCRIPTS, Mpv, base_args, mpv_pids

MODES = ("photos", "mixed", "videos")   # los que pasa ▼
TODAY = "today"                          # «Un día como hoy»: las fotos de esta fecha en otros años
FAVORITES = "favorites"                  # «♥ Favoritas»: las guardadas con 1
ALL_MODES = MODES + (TODAY, FAVORITES)
FAVORITES_FILE = "fotos-favoritas.txt"   # en ~/.local/share/tele-cec: una ruta por línea, en el orden en que se guardaron
TODAY_SPREAD = 3        # si no hay de este día exacto: esta semana (± 3 días) en otros años
REFRESH_EVERY_S = 600   # la lista guardada se vuelve a recorrer (en segundo plano) como mucho cada 10 minutos
CORNER = "tele_cec_corner"   # el script de mpv que escribe la fecha (o el año) en la esquina
SHOW_DATE_FILE = "fotos-fecha-en-pantalla"   # en ~/.cache/tele-cec: «si» o «no» (lo último elegido con CC)
MODE_NAMES = {"photos": "solo fotos", "mixed": "fotos y videos", "videos": "solo videos", TODAY: "un día como hoy",
              FAVORITES: "favoritas"}
_DATE_RE = re.compile(r"((?:19|20)\d{6})[_-]?(\d{6})?")  # IMG_20201130_185306 · VID-20201108-WA0001


def _date_key(path: str) -> tuple[str, str]:
    """Para ordenar por la fecha que traen los nombres (IMG_… y VID_… mezclados); sin fecha, por nombre."""
    name = os.path.basename(path)
    m = _DATE_RE.search(name)
    return ((m.group(1) + (m.group(2) or "")) if m else "~", name.lower())


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXT

log = logging.getLogger("actividades")


class PhotosActivity(MediaActivity):
    """Presentación con feh. Con ▲ se hace zoom: la presentación se pausa y las flechas recorren la foto."""

    id = "fotos"
    name = "Fotos"
    icon = "fotos.svg"
    procs = {"feh"}  # y su mpv, reconocido por su canal (pids)
    exts = IMAGE_EXT - {".heic"}  # feh (imlib2) no abre HEIC
    media_exts = exts | VIDEO_EXT  # la lista guardada tiene fotos y videos (cada modo toma lo suyo)
    empty_text = "Sin fotos: conectá un pendrive"
    play_label = "▶ Ver las fotos de esta carpeta"
    empty_in_folder = "No hay fotos en esta carpeta"
    unit = ("foto", "fotos")
    # feh pone en el título de su ventana qué foto muestra (%u = número, %l = total, %f = archivo):
    # así se puede mostrar en el panel con letra grande (azul).
    TITLE = "tele-cec fotos"
    TITLE_FORMAT = TITLE + "|%u|%l|%f"

    ZOOM_MAX = 12          # pasos de zoom (cada uno +25 % en feh): ~14 veces la foto ajustada
    RENDER_DELAY = 0.6     # s sin moverse antes de redibujar suavizado
    # Atajos de feh: el teclado numérico desplaza la foto (las flechas solas cambian de foto o hacen zoom).
    SCROLL_KEYS = {"left": "KP_Left", "right": "KP_Right", "up": "KP_Up", "down": "KP_Down"}

    def __init__(self, cfg: dict[str, Any] | None = None):
        super().__init__(cfg)
        self._render_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Future | None = None
        self.current_folder: Path | None = None
        self.mode = "photos"
        self.viewer = str(self.cfg.get("viewer") or "mpv").lower()
        self.mpv = Mpv(LOG_DIR / "fotos-mpv.sock")
        self.on_video: Callable[[bool], Awaitable[None]] | None = None  # suena un video / ya no
        self._video_now = False
        self.rotation = 0
        self._mpv_dirty = False
        self.fail_text = ""
        self.random_order = False   # la presentación abierta va al azar (la lista ya se mezcló)
        self._show_year = False     # «Un día como hoy»: el año de cada foto en una esquina
        self.show_date = self._load_show_date()   # la fecha de cada foto en una esquina (CC en el control)
        self._corner_path = ""      # de qué foto es lo que dice la esquina
        self._favorites: list[str] | None = None   # las guardadas con 1 (se leen la primera vez que hacen falta)
        self.reset_view()

    # ------------------------------------------------------------ favoritas (1 en el control)
    def favorites_file(self) -> Path:
        return DATA_DIR / FAVORITES_FILE

    def favorites(self) -> list[str]:
        """Las rutas guardadas, en el orden en que se guardaron (sin repetidas)."""
        if self._favorites is None:
            try:
                lines = self.favorites_file().read_text("utf-8").splitlines()
            except OSError:
                lines = []
            self._favorites = list(dict.fromkeys(ln.strip() for ln in lines if ln.strip()))
        return list(self._favorites)

    def _save_favorites(self) -> bool:
        f = self.favorites_file()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text("".join(p + "\n" for p in self._favorites or []), "utf-8")
            tmp.replace(f)
        except OSError as exc:
            log.warning("Fotos: no pude guardar las favoritas (%s)", exc)
            return False
        return True

    def is_favorite(self, path: str) -> bool:
        return path in self.favorites()

    def toggle_favorite(self, path: str) -> bool | None:
        """Guarda la foto en Favoritas (True) o la saca si ya estaba (False); None si no se pudo guardar."""
        favs = self.favorites()
        if path in favs:
            favs.remove(path)
            added = False
        else:
            favs.append(path)
            added = True
        self._favorites = favs
        if not self._save_favorites():
            return None
        log.info("Fotos: %s %s (%d favoritas)", "guardo" if added else "saco de Favoritas", os.path.basename(path),
                 len(favs))
        return added

    def favorite_files(self, folder: Path | None = None) -> list[str]:
        """Las favoritas que todavía existen (de todas las carpetas, no solo de esta: son pocas y van juntas)."""
        return [p for p in self.favorites() if os.path.isfile(p)]

    # ------------------------------------------------------------ la fecha en la esquina
    def _show_date_file(self) -> Path:
        return LOG_DIR / SHOW_DATE_FILE

    def _load_show_date(self) -> bool:
        """Lo último que se eligió con CC (o, si nunca se tocó, [apps.fotos] show_date)."""
        try:
            return self._show_date_file().read_text("utf-8").strip() == "si"
        except OSError:
            return bool(self.cfg.get("show_date", False))

    def _save_show_date(self) -> None:
        f = self._show_date_file()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("si\n" if self.show_date else "no\n", "utf-8")
        except OSError as exc:
            log.warning("Fotos: no pude guardar si va la fecha en la esquina (%s)", exc)

    def _corner_on(self) -> bool:
        return self.show_date or self._show_year

    async def toggle_date(self) -> bool | None:
        """CC en el control: prende o apaga la fecha en la esquina (y lo recuerda). None: con feh no se puede."""
        if self.backend != "mpv":
            return None
        self.show_date = not self.show_date
        self._save_show_date()
        path = await self.mpv.get("path")
        if path:
            await self._update_corner(str(path))
        log.info("Fotos: fecha en la esquina %s", "sí" if self.show_date else "no")
        return self.show_date

    def use_mpv(self, mode: str) -> bool:
        """mpv para todo (la placa de video achica, hace zoom y mueve la foto); feh solo para «solo fotos» si se
        eligió ([apps.fotos] viewer = "feh") o si no está mpv."""
        if mode in (TODAY, FAVORITES):  # con los videos (de ese día, o guardados), si hay mpv
            return bool(shutil.which("mpv"))
        if mode != "photos":
            return True
        return self.viewer != "feh" and bool(shutil.which("mpv"))

    @property
    def backend(self) -> str:
        return "mpv" if self.use_mpv(self.mode) else "feh"

    def reset_view(self) -> None:
        self.paused = False         # la presentación, según lo que le mandamos (feh arranca andando)
        self.zoom = 0               # pasos de zoom sobre la foto ajustada a la pantalla (0 = sin zoom)
        self._zoom_paused = False   # la pausó el zoom: se retoma al salir
        self._cancel_render()

    @property  # type: ignore[override]
    def context(self) -> str:
        return "fotos_zoom" if self.zoom else "fotos"

    def installed(self) -> bool:
        return self._have("feh") or (not self.command and bool(shutil.which("mpv")))

    def default_args(self, folder: Path, filelist: Path | None = None) -> list[str]:
        delay = float(self.cfg.get("delay_s", 8))
        step = int(self.cfg.get("scroll_step", 150))
        args = ["feh", "--fullscreen", "--hide-pointer", "--auto-zoom", "--quiet",
                "--image-bg", "black", "--slideshow-delay", f"{delay:g}", "--scroll-step", str(step),
                "--title", self.TITLE_FORMAT]
        if self.cfg.get("zoom_step"):
            args += ["--zoom-step", f"{float(self.cfg['zoom_step']):g}"]
        # Sin --auto-rotate: con Imlib2 ≥ 1.7.5 (Debian trixie) las fotos ya salen derechas según el EXIF.
        if filelist is None or not self.random_order:  # la lista ya mezclada va tal cual
            args.append("--randomize" if self.shuffle or self.random_order else "--sort=filename")
        if filelist is not None:
            return args + ["--filelist", str(filelist)]
        return args + ["--recursive", str(folder)]

    # ------------------------------------------------------------ lista de fotos guardada
    # feh con --recursive recorre toda la carpeta antes de mostrar la primera foto: en una carpeta de red
    # con decenas de miles de fotos son 15-20 s de pantalla vacía. Por eso la lista se arma una vez (con el
    # menú mostrando «Abriendo Fotos…»), se guarda en ~/.cache/tele-cec y feh arranca con --filelist al
    # instante; después de cada apertura se actualiza en segundo plano.
    def list_file(self, folder: Path) -> Path:
        return LOG_DIR / f"fotos-{hashlib.md5(str(folder).encode()).hexdigest()[:10]}.lst"

    def write_list(self, folder: Path) -> int:
        """Guarda las fotos y los videos de la carpeta (con subcarpetas). Devuelve cuántos archivos son."""
        t0 = time.monotonic()
        files = scan_media(folder, self.media_exts)
        if not files:
            return 0
        dest = self.list_file(folder)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Un temporal propio y se reemplaza de una: quien lee la lista la ve entera (la vieja o la nueva), nunca
        # a medio escribir (antes, dos actualizaciones a la vez compartían el temporal y la dejaban cortada).
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text("\n".join(files) + "\n", encoding="utf-8")
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        videos = sum(1 for f in files if is_video(f))
        log.info("Fotos: lista de %s actualizada (%d fotos, %d videos, %.1f s)", describe_folder(folder),
                 len(files) - videos, videos, time.monotonic() - t0)
        if self.cfg.get("on_this_day", True):
            dates = photodates.dates()
            dates.hold = self.running  # mientras se miran las fotos, no compite con ellas por la red
            dates.start(files)  # las fechas que falten, para «Un día como hoy» (de fondo y de a poco)
        return len(files)

    def today_files(self, folder: Path, today: Any = None) -> tuple[list[str], int]:
        """«Un día como hoy»: (las fotos y videos de esta fecha en otros años, de la más vieja a la más nueva; ±días).
        Si no hay de este día exacto, los de esta semana (± TODAY_SPREAD días). Sin mpv, solo las fotos."""
        try:
            files = [ln for ln in self.list_file(folder).read_text(encoding="utf-8").splitlines() if ln]
        except OSError:
            return [], 0
        if not self.use_mpv(TODAY):
            files = [f for f in files if os.path.splitext(f)[1].lower() in self.exts]
        d = photodates.dates()
        for spread in (0, TODAY_SPREAD):
            hits = d.on_this_day(files, today, spread)
            if hits:
                return hits, spread
        return [], 0

    def mode_files(self, folder: Path, mode: str) -> list[str]:
        """Los archivos de la lista guardada para ese modo, en el orden en que se muestran."""
        try:
            files = [ln for ln in self.list_file(folder).read_text(encoding="utf-8").splitlines() if ln]
        except OSError:
            return []
        if mode == "photos":
            return [f for f in files if os.path.splitext(f)[1].lower() in self.exts]
        if mode == "videos":
            files = [f for f in files if is_video(f)]
        out: list[str] = []
        by_dir: dict[str, list[str]] = {}
        for f in files:  # carpeta por carpeta (como feh), y adentro por fecha: los videos caen en su lugar
            by_dir.setdefault(os.path.dirname(f), []).append(f)
        for d in by_dir:
            out += sorted(by_dir[d], key=_date_key)
        return out

    def counts(self, folder: Path) -> tuple[int, int] | None:
        """(fotos, videos) de la lista guardada, si ya existe (None: todavía no se recorrió esa carpeta)."""
        try:
            files = self.list_file(folder).read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        videos = sum(1 for f in files if f and is_video(f))
        return sum(1 for f in files if f) - videos, videos

    async def open(self, folder: Path | None = None, mode: str | None = None, start: str | None = None) -> bool:
        """Abre la presentación. mode: «photos» (feh, el de siempre), «mixed» o «videos» (mpv). start: el archivo
        por el que empezar (al cambiar de modo, la foto que se estaba viendo: se sigue desde ahí)."""
        self.reset_view()
        self.fail_text = ""
        mode = mode if mode in ALL_MODES else "photos"
        if self.command:
            return await super().open(folder)
        if folder is None:
            folder = await asyncio.to_thread(self.folder)
        if folder is None:
            log.warning("%s: no hay contenido en %s", self.name, ", ".join(map(str, self.folders)) or "(sin carpetas)")
            return False
        if mode not in ("photos", TODAY, FAVORITES) and not shutil.which("mpv"):
            self.fail_text = "Para ver videos falta mpv (sudo apt install mpv)"
            log.warning("Fotos: falta mpv para «%s»", MODE_NAMES[mode])
            return False
        saved = self.list_file(folder)
        had_list = saved.is_file() and saved.stat().st_size > 0
        if not had_list:
            log.info("Fotos: armo la lista de %s (solo la primera vez tarda)", describe_folder(folder))
            if not await asyncio.to_thread(self.write_list, folder):
                return False
        if mode == TODAY:
            files, _spread = await asyncio.to_thread(self.today_files, folder)
        elif mode == FAVORITES:
            files = await asyncio.to_thread(self.favorite_files, folder)
        else:
            files = await asyncio.to_thread(self.mode_files, folder, mode)
        if not files:
            log.warning("Fotos: no hay archivos para «%s» en %s", MODE_NAMES[mode], describe_folder(folder))
            self.fail_text = {"photos": "No hay fotos en esta carpeta", "videos": "No hay videos en esta carpeta",
                              "mixed": "No hay fotos ni videos en esta carpeta",
                              TODAY: "No hay fotos de esta fecha en otros años",
                              FAVORITES: "Todavía no hay favoritas: 2 guarda la foto que se está viendo"}[mode]
            return False
        if mode == TODAY:  # un día como hoy: al azar (o año por año, con today_shuffle = false)
            self.random_order = bool(self.cfg.get("today_shuffle", True)) and len(files) > 1
        elif mode == FAVORITES:  # en el orden en que se guardaron (al azar solo con shuffle = true)
            self.random_order = self.shuffle and len(files) > 1
        else:
            self.random_order = self.shuffled_order(files)
        self._show_year = mode == TODAY
        if self.random_order:
            files, start = self.shuffle_from(files, start)
        ok = await (self._open_mpv(files, start) if self.use_mpv(mode) else self._open_feh(folder, files, start))
        if ok:
            self.current_folder, self.mode = folder, mode
            log.info("Fotos: %s (%s)", describe_folder(folder), MODE_NAMES[mode])
        if ok and had_list:
            self._refresh_later(folder)
        return ok

    def _refresh_later(self, folder: Path) -> None:
        """Actualiza la lista guardada en segundo plano (las fotos nuevas aparecen la vez siguiente). Una sola a la
        vez, y no si se actualizó hace poco: cambiar de modo con ▼ reabre Fotos, y en una carpeta de red grande
        recorrerla tarda medio minuto."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        try:
            age = time.time() - self.list_file(folder).stat().st_mtime
        except OSError:
            age = REFRESH_EVERY_S
        if age < REFRESH_EVERY_S:
            return
        self._refresh_task = asyncio.get_running_loop().create_task(asyncio.to_thread(self.write_list, folder))

    async def _open_feh(self, folder: Path, files: list[str], start: str | None) -> bool:
        # feh reescribe su --filelist al cerrarse: se le da una copia para que no pise la actualización.
        for_feh = self.list_file(folder).with_suffix(".feh.lst")
        try:
            await asyncio.to_thread(for_feh.write_text, "\n".join(files) + "\n", "utf-8")
        except OSError as exc:
            log.warning("Fotos: no pude preparar la lista (%s): uso la carpeta directamente", exc)
            return await self.spawn(self.default_args(folder))
        args = self.default_args(folder, for_feh)
        first = self._start_at(files, start)
        if first:
            args[-2:-2] = ["--start-at", files[first]]
        return await self.spawn(args)

    def shuffled_order(self, files: list[str]) -> bool:
        """Al azar: si las fotos están repartidas en subcarpetas (o con shuffle = true)."""
        if self.shuffle:
            return True
        first = os.path.dirname(files[0]) if files else ""
        return any(os.path.dirname(f) != first for f in files)

    def shuffle_from(self, files: list[str], start: str | None) -> tuple[list[str], str | None]:
        """La lista mezclada; si se viene de otro modo (start), primero la foto que se veía (o la más cercana)."""
        first = files[self._start_at(files, start)] if start else None
        out = [f for f in files if f != first]
        random.shuffle(out)
        return ([first, *out], first) if first else (out, None)

    @staticmethod
    def _start_at(files: list[str], start: str | None) -> int:
        """Dónde empezar: el archivo `start` o, si no está (otro modo), el siguiente en orden de fecha."""
        if not start:
            return 0
        if start in files:
            return files.index(start)
        key = (os.path.dirname(start), _date_key(start))
        for i, f in enumerate(files):
            if (os.path.dirname(f), _date_key(f)) >= key:
                return i
        return 0

    def pids(self, fresh: bool = False) -> list[int]:
        """feh, o el mpv de Fotos (por su canal: Videos y Música también usan mpv)."""
        if self.command:
            return super().pids(fresh)
        return find_pids(lambda argv: os.path.basename(argv[0]) == "feh", fresh) + mpv_pids(self.mpv.socket_path, fresh)

    def mpv_args(self, playlist: Path, start: int) -> list[str]:
        delay = float(self.cfg.get("delay_s", 8))
        args = base_args(self.mpv.socket_path, self.TITLE) + [
            f"--image-display-duration={delay:g}", "--loop-playlist=inf", "--background-color=#000000",
            "--prefetch-playlist=yes",  # la foto siguiente se lee mientras se ve esta: pasa en 0,2 s (12 MP, en red)
            # sin buscar subtítulos, audio ni tapas aparte: en una carpeta grande por la red tardaba 3,6 s cada video
            "--sub-auto=no", "--audio-file-auto=no", "--cover-art-auto=no",
            f"--playlist={playlist}", f"--playlist-start={start}"]  # al azar: la lista ya va mezclada
        # La esquina siempre (es liviana): así la fecha se puede prender con 1 sin volver a abrir la presentación.
        args.insert(-2, f"--script={SCRIPTS / (CORNER + '.lua')}")
        return args

    async def _open_mpv(self, files: list[str], start: str | None) -> bool:
        playlist = LOG_DIR / "fotos-mpv.lst"
        try:
            await asyncio.to_thread(playlist.write_text, "\n".join(files) + "\n", "utf-8")
        except OSError as exc:
            log.warning("Fotos: no pude armar la lista para mpv: %s", exc)
            return False
        self.mpv.socket_path.unlink(missing_ok=True)
        if not await self.spawn(self.mpv_args(playlist, self._start_at(files, start))):
            return False
        if not await self.mpv.ready():
            log.warning("Fotos: mpv no abrió su canal de control")
        self.mpv.watch(self._mpv_file)
        return True

    def rewatch(self) -> bool:
        """Vuelve a escuchar al mpv de Fotos si quedó abierto sin nadie que lo escuche (ver Mpv.lost_watch)."""
        if self.command or not self.mpv.lost_watch():
            return False
        self.mpv.watch(self._mpv_file)
        return True

    async def _mpv_file(self, path: str) -> None:
        """mpv pasó a otro archivo: sin zoom ni giro, y la música en pausa mientras suena un video."""
        await self._mpv_reset()
        if self._zoom_paused and self.paused:
            await self.mpv.set("pause", False)
            self.paused = False
        self._zoom_paused = False
        await self._set_video(is_video(path))
        await self._update_corner(path)

    async def _update_corner(self, path: str) -> None:
        """La esquina de abajo a la derecha (tele_cec_corner.lua): con la fecha prendida, la de la foto con lo que se
        sepa («sábado 20 de diciembre de 2008 · hace 17 años», «diciembre de 2008», «2008»); en «Un día como hoy»,
        el año («2014 · hace 11 años»). Si la foto está en Favoritas, un ♥ adelante (aunque la fecha esté apagada); con
        la fecha prendida, un globo terráqueo a la derecha si la foto o el video tiene la ubicación (GPS)."""
        self._corner_path = path
        heart = "♥" if self.is_favorite(path) else ""
        if not self._corner_on():
            await self._corner(heart, "")
            return
        full = await asyncio.to_thread(photodates.dates().date_of, path)
        # Con la fecha prendida, un globito si la foto (o el video) dice dónde se sacó: el verde muestra el mapa.
        where = bool(self.show_date and self.cfg.get("show_location", True)
                     and await asyncio.to_thread(photodates.gps_of, path))
        if self._corner_path != path:
            return  # ya pasó a otra foto mientras se leía la fecha
        parts = photodates.date_parts(path, full)
        if not parts:
            await self._corner(heart, "", where)
            return
        if not self.show_date:  # solo «Un día como hoy»: el año
            parts = (parts[0], None, None, parts[3])
        big, ago = photodates.date_text(parts)
        await self._corner(f"{heart} {big}".strip(), ago, where)

    async def _corner(self, big: str, small: str, globe: bool = False) -> None:
        if big or globe:
            await self.mpv.command("script-message-to", CORNER, "show", big, small, *(("globe",) if globe else ()))
        else:
            await self.mpv.command("script-message-to", CORNER, "clear")

    async def refresh_corner(self) -> None:
        """Volver a escribir la esquina de la foto que se ve (después de guardarla o sacarla de Favoritas)."""
        if self.backend == "mpv" and self._corner_path:
            await self._update_corner(self._corner_path)

    async def _mpv_reset(self) -> None:
        """mpv conserva zoom, corrimiento y giro entre archivos: el siguiente, entero y derecho."""
        if not self._mpv_dirty:
            return
        self._mpv_dirty = False
        self.zoom, self.rotation = 0, 0
        for prop in ("video-zoom", "video-pan-x", "video-pan-y", "video-rotate"):
            await self.mpv.set(prop, 0)

    async def _set_video(self, now: bool) -> None:
        if now != self._video_now:
            self._video_now = now
            if self.on_video is not None:
                await self.on_video(now)

    async def close(self) -> None:
        self.reset_view()
        self.mpv.stop_watch()
        await super().close()
        await self._set_video(False)  # si un video había pausado la música, que vuelva
        await asyncio.to_thread(photodates.dates().flush)  # las fechas leídas mientras se miraba

    def resume_dates(self) -> bool:
        """Al arrancar el servicio: sigue leyendo de fondo las fechas que falten de las listas ya guardadas (si no,
        esperaba a la próxima vez que se abriera Fotos, y cada reinicio la cortaba). True si quedó trabajando."""
        if self.command or not self.cfg.get("on_this_day", True):
            return False
        files: list[str] = []
        for lst in sorted(LOG_DIR.glob("fotos-*.lst")):
            if lst.name.endswith(".feh.lst") or lst.name == "fotos-mpv.lst":
                continue
            try:
                files += [ln for ln in lst.read_text(encoding="utf-8").splitlines() if ln]
            except OSError:
                continue
        if not files:
            return False
        dates = photodates.dates()
        dates.hold = self.running  # mientras se miran las fotos, no compite con ellas por la red
        working = dates.start(list(dict.fromkeys(files)))
        if working:
            log.info("Fotos: sigo leyendo las fechas que faltan (de fondo)")
        return working

    async def photo_info(self) -> dict[str, Any] | None:
        """Qué foto se ve: {"path", "index", "total"}, leído del título de la ventana de feh (o preguntado a mpv)."""
        if self.backend == "mpv":
            path = await self.mpv.get("path")
            if not path:
                return None
            return {"path": str(path), "index": int(await self.mpv.get("playlist-pos-1") or 0),
                    "total": int(await self.mpv.get("playlist-count") or 0)}
        rc, out = await run_cmd(["xdotool", "search", "--name", "^" + self.TITLE + r"\|", "getwindowname"], 5)
        line = next((ln for ln in out.splitlines() if ln.startswith(self.TITLE + "|")), "")
        if rc != 0 or not line:
            log.warning("Fotos: no pude leer qué foto se ve (xdotool: %s)", out.strip()[:120] or rc)
            return None
        _t, index, total, path = (line.split("|", 3) + ["", "", ""])[:4]
        return {"path": path, "index": int(index) if index.isdigit() else 0,
                "total": int(total) if total.isdigit() else 0}

    def describe_photo(self, info: dict[str, Any]) -> str:
        """Texto del aviso: dónde está (carpeta base › subcarpetas), el archivo y el número de foto."""
        path = Path(info["path"])
        # La ruta se cuenta desde la carpeta configurada que la contiene (aunque se haya elegido una subcarpeta).
        base = next((r for r in self.roots() if path.is_relative_to(r)), None) or self.current_folder
        where = describe_folder(base) if base else str(path.parent)
        try:
            rel = path.parent.relative_to(base) if base else None
        except ValueError:
            rel, where = None, str(path.parent)
        if rel is not None and str(rel) != ".":
            where += " › " + " › ".join(rel.parts)
        lines = [where, path.name]
        if info.get("total"):
            lines.append(f"Foto {info['index']:,} de {info['total']:,}".replace(",", "."))
        return "\n".join(lines)

    async def key(self, name: str) -> bool:
        """Un atajo de teclado de feh. feh es X11 (Xwayland): va con xdotool; con wtype le llega Escape y se cierra."""
        return await xdotool_key(name)

    async def next(self) -> bool:
        if self.backend == "mpv":
            return (await self.mpv.command("playlist-next", "force"))[0]
        return self.send_signal(signal.SIGUSR1)  # feh, en modo presentación: siguiente

    async def previous(self) -> bool:
        if self.backend == "mpv":
            return (await self.mpv.command("playlist-prev", "force"))[0]
        return self.send_signal(signal.SIGUSR2)  # anterior

    async def _pause(self, on: bool) -> bool:
        if self.backend == "mpv":
            return await self.mpv.set("pause", on)
        return await self.key("h")  # h = pausar o seguir la presentación (feh alterna)

    async def toggle_pause(self) -> bool:
        ok = await self._pause(not self.paused)
        if ok:
            self.paused = not self.paused
            self._zoom_paused = False
        return ok

    async def rotate(self) -> bool:
        """Gira la foto (rojo)."""
        if self.backend == "mpv":
            self.rotation = (self.rotation + 90) % 360
            self._mpv_dirty = True
            return await self.mpv.set("video-rotate", self.rotation)
        return await self.key("greater")

    async def random(self) -> bool:
        """Una foto al azar (amarillo)."""
        if self.backend == "mpv":
            total = int(await self.mpv.get("playlist-count") or 0)
            return total > 0 and await self.mpv.set("playlist-pos", random.randrange(total))
        return await self.key("z")

    async def filename(self) -> bool:
        """El nombre en letra chica, del propio visor (si no se pudo leer qué foto es)."""
        if self.backend == "mpv":
            return (await self.mpv.command("show-text", "${filename}", 4000))[0]
        return await self.key("d")

    # ------------------------------------------------------------ zoom
    async def zoom_in(self) -> bool:
        """Acerca un paso. El primero pausa la presentación (si andaba) para que no cambie de foto."""
        if self.zoom >= self.ZOOM_MAX:
            return False
        if not self.zoom and not self.paused:
            if not await self._pause(True):
                return False
            self.paused = self._zoom_paused = True
        if self.backend == "mpv":  # video-zoom va en potencias de 2: 0.32 ≈ +25 %, como un paso de feh
            self._mpv_dirty = True
            ok = await self.mpv.set("video-zoom", round(0.32 * (self.zoom + 1), 2))
        else:
            ok = await self.key("Up")  # Up = zoom_in
        if not ok:
            return False
        self.zoom += 1
        return True

    async def zoom_out(self) -> bool:
        """Aleja un paso; desde el primero vuelve a la foto entera (y sigue la presentación si el zoom la pausó)."""
        if not self.zoom:
            return False
        if self.zoom == 1:
            return await self.fit()
        if self.backend == "mpv":
            ok = await self.mpv.set("video-zoom", round(0.32 * (self.zoom - 1), 2))
        else:
            ok = await self.key("Down")  # Down = zoom_out
        if not ok:
            return False
        self.zoom -= 1
        return True

    async def fit(self) -> bool:
        """Sale del zoom: la foto entera, ajustada a la pantalla."""
        if not self.zoom:
            return False
        if self.backend == "mpv":
            for prop in ("video-zoom", "video-pan-x", "video-pan-y"):
                await self.mpv.set(prop, 0)
        else:
            await self.key("KP_Divide")  # zoom_fit
        await self.photo_changed(new_file=False)  # la misma foto: sin zoom, pero el giro queda
        return True

    async def scroll(self, direction: str) -> bool:
        """Mueve la parte de la foto que se ve (solo con zoom)."""
        name = self.SCROLL_KEYS.get(direction)
        if not self.zoom or not name:
            return False
        if self.backend == "mpv":  # se corre la foto al revés: «izquierda» muestra lo que está a la izquierda
            prop = "video-pan-x" if direction in ("left", "right") else "video-pan-y"
            step = 0.08 if direction in ("left", "up") else -0.08
            pan = float(await self.mpv.get(prop) or 0)
            return await self.mpv.set(prop, round(max(-2.0, min(2.0, pan + step)), 3))
        ok = await self.key(name)
        # feh desplaza sin suavizado (más rápido); cuando se deja de mover, R la redibuja bien.
        self._cancel_render()
        self._render_task = asyncio.get_running_loop().create_task(self._render_later())
        return ok

    async def photo_changed(self, new_file: bool = True) -> None:
        """Otra foto (new_file) o la misma girada o ajustada: se muestra entera, así que se termina el zoom."""
        self._cancel_render()
        if self.backend == "mpv" and new_file:
            await self._mpv_reset()  # ya, sin esperar el aviso de mpv (una foto grande tarda en cargar)
        elif self.backend == "mpv" and self.zoom:  # la misma foto (girada): entera, con el giro
            for prop in ("video-zoom", "video-pan-x", "video-pan-y"):
                await self.mpv.set(prop, 0)
        if not self.zoom and not self._zoom_paused:
            return
        self.zoom = 0
        if self._zoom_paused and self.paused and await self._pause(False):
            self.paused = False
        self._zoom_paused = False

    async def _render_later(self) -> None:
        await asyncio.sleep(self.RENDER_DELAY)
        self._render_task = None
        if self.zoom and self.backend == "feh":
            await self.key("R")  # render

    def _cancel_render(self) -> None:
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
        self._render_task = None
