"""El menú de actividades en el panel: elegir actividad, el selector de carpetas (Videos, Fotos), la salida de
sonido y la ayuda."""

from __future__ import annotations

import asyncio
import datetime
import logging
import shutil
import time
from pathlib import Path

from .. import apps as activities_mod
from ..apps import fileinfo, photodates, photomap, resume
from ..apps.library import fold
from ..system import audio
from ..system.procs import LOG_DIR
from ..ui import help as help_mod
from .activities import KEEP_PLAYING

log = logging.getLogger("tele")

# Ayuda al pie del menú.
MENU_HINT = "▲ ▼ elegir · OK abrir · ▶ más opciones (Videos, Fotos, Radio, Juegos, Android) · Atrás cerrar · • ayuda"
GRID_HINT = "▲ ▼ ◀ ▶ moverse · OK abrir · Atrás cerrar · • (punto) ayuda, en cualquier app"
FOLDER_HINT = "▲ ▼ elegir · OK abrir · ◀ volver"
SOUND_HINT = "▲ ▼ elegir · Volumen y temporizador: ◀ ▶ cambiar · OK usar esa salida · Atrás volver"
RADIO_HINT = "▲ ▼ o 1 – 9 elegir · OK escuchar · Atrás volver"
SITES_HINT = "▲ ▼ o 1 – 9 elegir · OK abrir · rojo borrar · Atrás volver"
SITES_DELETE_S = 5.0   # rojo sobre un sitio: hay que apretarlo otra vez antes de esto para borrarlo
VOLUME_STEP = 5        # % por cada ◀ ▶ en la fila «Volumen»
SOUND_FIRST = 2        # en Sonido, las salidas empiezan después de las filas «Volumen» y «Temporizador»
BT_SCAN_SECONDS = 25   # cuánto se buscan parlantes y auriculares Bluetooth al abrir «Sonido»
BT_POLL_SECONDS = 2.0  # cada cuánto se mira qué encontró la búsqueda
HELP_HINT = "◀ ▶ otra app · ▲ ▼ más filas · Atrás volver"
MAP_HINT = "▲ u OK acercar · ▼ alejar · Atrás volver a las fotos"
MAP_SIZE = 30          # el mapa en el panel, en tamaños de letra (30 = 900 px en 1080p)
GROUP_HINT = "▲ ▼ elegir · OK abrir · ▶ carpetas · Atrás volver"
KEEP = "\0mismo"   # menu_open(group=KEEP): quedarse en la lista en que estaba
BACKGROUND_BADGE = {"Playing": "sonando", "Paused": "en pausa"}  # si no, «abierto» (sin nada cargado)


SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".vtt"}


def _size_text(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB".replace(".", ",")
    return f"{max(1, round(n / (1 << 20)))} MB"


def _plural(n: int, unit: tuple[str, str]) -> str:
    """«1 tema», «12 temas», «49.403 fotos»."""
    return f"{n:,}".replace(",", ".") + f" {unit[0] if n == 1 else unit[1]}"


def _contents_text(files_here: int, subfolders: int, unit: tuple[str, str]) -> str:
    """Lo que tiene la carpeta: «12 temas», «12 temas y 3 subcarpetas», «en 3 subcarpetas»."""
    subs = _plural(subfolders, ("subcarpeta", "subcarpetas"))
    if files_here and subfolders:
        return f"{_plural(files_here, unit)} y {subs}"
    if files_here:
        return _plural(files_here, unit)
    return f"en {subs}" if subfolders else ""


def _song_items(folder: Path, files: list[Path]) -> list[dict]:
    """Los temas con lo que dicen sus etiquetas: el título y, abajo, artista (y disco, si no es esta carpeta) y
    duración."""
    tags = fileinfo.song_tags([str(f) for f in files])
    here = fold(folder.name)
    out = []
    for f in files:
        t = tags.get(str(f)) or {}
        info = [t.get("artist") or t.get("album_artist") or ""]
        if t.get("album") and fold(t["album"]) != here:
            info.append(t["album"])
        if t.get("dur"):
            info.append(fileinfo.duration_text(t["dur"]))
        out.append({"kind": "file", "path": str(f), "name": t.get("title") or f.stem, "icon": "audio-x-generic",
                    "sub": " · ".join(x for x in info if x), "enabled": True, "current": False})
    return out


def _file_items(folder: Path, act: activities_mod.MediaActivity, files: list[Path] | None = None) -> list[dict]:
    """Los archivos de la carpeta para elegir uno: los temas (Música) o los videos (con su título, duración,
    calidad, tamaño y si tienen subtítulos)."""
    if files is None:
        files = activities_mod.list_media(folder, act.exts)
    if not files:
        return []
    if getattr(act, "audio_only", False):
        return _song_items(folder, files)
    infos = fileinfo.video_info().get([str(f) for f in files])
    try:
        subs = [p.stem.lower() for p in folder.iterdir() if p.suffix.lower() in SUB_EXT]
    except OSError:
        subs = []
    left = resume.positions()  # Seguir viendo: los que quedaron a la mitad
    out = []
    for f in files:
        meta = infos.get(str(f)) or {}
        info = fileinfo.video_text(meta)
        pos = left.get(str(f))
        if pos and resume.finished(pos, meta.get("dur")):
            pos = None
        if pos:
            info.insert(0, f"quedó en {fileinfo.duration_text(pos)}")
        try:
            info.append(_size_text(f.stat().st_size))
        except OSError:
            pass
        stem = f.stem.lower()
        if meta.get("subs") or any(s.startswith(stem) for s in subs):  # adentro, o «peli.srt», «peli.es.srt»…
            info.append("con subtítulos")
        out.append({"kind": "file", "path": str(f), "name": meta.get("title") or f.stem, "icon": "video-x-generic",
                    "sub": " · ".join(info), "enabled": True, "current": False, **({"resume": pos} if pos else {})})
    return out


def _years_text(years: list[int]) -> str:
    """«de 2014», «de 2014, 2017 y 2021», «de 9 años, entre 2009 y 2023»."""
    if not years:
        return ""
    if len(years) > 4:
        return f"de {len(years)} años, entre {years[0]} y {years[-1]}"
    return "de " + (", ".join(map(str, years[:-1])) + " y " if len(years) > 1 else "") + str(years[-1])


def _today_item(act: activities_mod.PhotosActivity, path: Path) -> dict | None:
    """«Un día como hoy»: las fotos de esta fecha en otros años (si no hay, las de esta semana)."""
    if act.command or not act.cfg.get("on_this_day", True):
        return None
    try:
        listed = [ln for ln in act.list_file(path).read_text(encoding="utf-8").splitlines() if ln]
    except OSError:
        return None  # la carpeta todavía no se recorrió: se ve la próxima vez
    dates = photodates.dates()
    dates.hold = act.running  # mientras se miran las fotos, no compite con ellas por la red
    dates.start(listed)  # las fechas que falten (de fondo y de a poco)
    hits, spread = act.today_files(path)
    today = datetime.date.today()
    if spread:
        a, b = today - datetime.timedelta(days=spread), today + datetime.timedelta(days=spread)
        when = (f"del {a.day} al {photodates.month_day_text(b)}" if a.month == b.month
                else f"del {photodates.month_day_text(a)} al {photodates.month_day_text(b)}")
    else:
        when = photodates.month_day_text(today)
    item = {"kind": "play", "mode": activities_mod.photos.TODAY, "path": str(path), "icon": "media-playback-start",
            "name": "▶ Esta semana, otros años" if spread else "▶ Un día como hoy", "enabled": bool(hits),
            "current": False}
    if hits:
        videos = sum(1 for f in hits if activities_mod.photos.is_video(f))
        what = _plural(len(hits) - videos, act.unit) + (f" y {_plural(videos, ('video', 'videos'))}" if videos else "")
        item["sub"] = f"{when}: {what} {_years_text(dates.years(hits))}"
    else:
        item["sub"] = f"No hay fotos de un {photodates.month_day_text(today)} en otros años"
    if dates.busy():
        done, total = dates.progress
        item["sub"] += f" · buscando fechas ({100 * done // max(1, total)} %)"
    return item


def _favorites_item(act: activities_mod.PhotosActivity, path: Path) -> dict:
    """«♥ Favoritas»: las fotos guardadas con 1 (de todas las carpetas), en el orden en que se guardaron."""
    files = act.favorite_files()
    item = {"kind": "play", "mode": activities_mod.photos.FAVORITES, "path": str(path), "icon": "media-playback-start",
            "name": "▶ ♥ Favoritas", "enabled": bool(files), "current": False}
    if files:
        videos = sum(1 for f in files if activities_mod.photos.is_video(f))
        photos = len(files) - videos
        parts = [_plural(photos, act.unit)] if photos else []
        if videos:
            parts.append(_plural(videos, ("video", "videos")))
        item["sub"] = " y ".join(parts) + " guardadas con 1 · otra vez 1 las saca"
    else:
        item["sub"] = "Todavía no hay: 1 guarda la foto que se está viendo"
    return item


CONTINUE_MAX_ROOT = 20  # «Seguir viendo» en la lista de carpetas: todos (hasta este tope), del más reciente al más viejo
CONTINUE_MAX_FOLDER = 3


def _resume_on(act: activities_mod.MediaActivity) -> bool:
    """Videos con «Seguir viendo» ([apps.videos] resume)."""
    return isinstance(act, activities_mod.VideosActivity) and not act.command and bool(act.cfg.get("resume", True))


def _in_progress(act: activities_mod.MediaActivity, within: list[Path]) -> list[tuple[Path, float, dict]]:
    """Los videos de esas carpetas (o sus subcarpetas) que quedaron a la mitad, el más reciente primero, sin los que
    quedaron en los créditos: (ruta, segundos, datos del video)."""
    found = [(Path(p), s) for p, s in resume.recent(None, within=within) if Path(p).suffix.lower() in act.exts]
    if not found:
        return []
    infos = fileinfo.video_info().get([str(p) for p, _s in found], budget=0.5)
    out = []
    for p, secs in found:
        meta = infos.get(str(p)) or {}
        if not resume.finished(secs, meta.get("dur")):
            out.append((p, secs, meta))
    return out


def _continue_items(act: activities_mod.MediaActivity, found: list[tuple[Path, float, dict]],
                    limit: int = CONTINUE_MAX_FOLDER) -> list[dict]:
    """«Seguir viendo»: los últimos que quedaron a la mitad (arriba de todo; siguen directo, sin preguntar)."""
    out = []
    for p, secs, meta in found[:limit]:
        of = f" de {fileinfo.duration_text(meta['dur'])}" if meta.get("dur") else ""
        out.append({"kind": "file", "path": str(p), "name": f"Seguir viendo: {meta.get('title') or p.stem}",
                    "icon": "media-playback-start", "sub": f"quedó en {fileinfo.duration_text(secs)}{of}",
                    "enabled": True, "current": False, "resume": secs, "direct": True})
    return out


def _watching_text(folder: Path, found: list[tuple[Path, float, dict]]) -> str:
    """Abajo de una carpeta: qué se está viendo adentro («viendo: La película (quedó en 1:06:40) y 2 más»)."""
    here = [(p, s, m) for p, s, m in found if p.is_relative_to(folder)]
    if not here:
        return ""
    p, secs, meta = here[0]
    more = f" y {len(here) - 1} más" if len(here) > 1 else ""
    return f"viendo: {meta.get('title') or p.stem} (quedó en {fileinfo.duration_text(secs)}){more}"


def _add_watching(items: list[dict], found: list[tuple[Path, float, dict]]) -> None:
    """A cada carpeta de la lista le agrega qué se está viendo adentro."""
    for it in items:
        if it["kind"] == "dir":
            text = _watching_text(Path(it["path"]), found)
            if text:
                it["sub"] = " · ".join(x for x in (it["sub"], text) if x)


class MenuMixin:
    def _init_menu(self) -> None:
        self._menu_open = False
        self._menu_items: list[dict] = []
        self._menu_sel = 0
        self._menu_timer: asyncio.Task | None = None
        self._menu_timeout = float(self.cfg.menu.get("timeout_s", 10) or 0)
        # En grilla (2 columnas, sin desplazar), ◀ ▶ cambian de columna. Videos, Fotos y Android tienen un
        # botoncito en la fila («Carpetas», «Conectar»): ▶ lo enfoca y OK abre esas opciones; OK sobre la fila
        # abre de una. Con 1 columna, ▶ es «más opciones».
        self._menu_columns = max(1, min(int(self.cfg.menu.get("columns", 2) or 1), 3))
        self._menu_chip = False
        # Selector de carpetas (▶ sobre Videos o Fotos): qué actividad, en qué carpeta (None = las configuradas)
        # y por dónde se vino (para volver con ◀ a la carpeta de arriba y a la misma fila).
        self._menu_mode = "activities"
        self._menu_act_sel = 0
        self._menu_group: str | None = None   # adentro de un grupo del menú («Videos y música»)
        self._picker_act = ""
        self._picker_path: Path | None = None
        self._picker_stack: list[tuple[Path | None, int]] = []
        # Lista de juegos: si se abrió desde el juego (menú cerrado), Atrás vuelve al juego en vez de al menú.
        self._games_back_closes = False
        self._radio_back_closes = False  # la lista de radios abierta desde la Radio (amarillo): Atrás vuelve a ella
        self._sites_back_closes = False  # la lista de sitios abierta desde el Navegador (amarillo): Atrás vuelve a él
        self._sites_delete: tuple[int, float] | None = None  # rojo una vez sobre un sitio: (cuál, cuándo)
        # Ayuda (desde el menú, con • o con la tecla Ayuda): páginas por app y desde dónde se abrió.
        self._menu_from_ctx = "offline"
        self._help_pages: list[tuple[str, str]] = []
        self._help_idx = 0
        self._help_top = 0
        self._help_from_menu = False
        # Salida de sonido (entrada «Sonido» del menú): las salidas que se mostraron.
        self._sound_outputs: list[audio.Output] = []
        self._map: dict = {}  # el mapa de la foto que se está mostrando (verde en Fotos)
        self._sound_volume: tuple[int | None, bool] = (None, False)  # (porcentaje, silencio) de la fila «Volumen»
        self._sound_on = bool(self.cfg.menu.get("sound", True))
        self.phone_qr = None  # la página del celular lo pone (WebRemote.phone_qr): el QR abajo del menú
        self._bt_scan = audio.BluetoothScan()
        self._sound_scan_task: asyncio.Task | None = None
        self._sound_scanning = False

    async def startup_menu(self) -> None:
        """Al arrancar el servicio (al prender la Pi): el panel listo y el menú abierto, si no hay nada en pantalla."""
        await self.overlay.warm()
        if not self.cfg.menu.get("open_on_start", True) or not self.overlay.running():
            return
        await asyncio.sleep(2)  # que browser_loop alcance a encontrar un navegador que ya estaba abierto
        if (self._menu_open or self._switching or self.power.asleep or self.current_app()
                or await self.browser.alive()):
            return
        log.info("Arranque: abro el menú de actividades")
        await self.menu_open()

    # ---------------------------------------------------------------- menú de actividades
    def _menu_groups(self) -> dict[str, dict]:
        """[menu.groups.<id>]: filas que abren otra lista («Videos y música» → Videos, Música)."""
        out = {}
        for gid, g in (self.cfg.menu.get("groups") or {}).items():
            if not isinstance(g, dict):
                continue
            items = [str(i).strip().lower() for i in g.get("items") or []]
            items = [i for i in items if i in self.activities]
            if items:
                out[str(gid).strip().lower()] = {"name": str(g.get("name") or gid), "icon": str(g.get("icon") or ""),
                                                 "items": items}
        return out

    def _menu_order(self, group: str | None = None) -> list[str]:
        """Las filas, en orden: ids de actividades y «_group:<id>» para los grupos (o las de adentro de un grupo)."""
        groups = self._menu_groups()
        if group:
            return list(groups.get(group, {}).get("items", []))
        items = [str(i).strip().lower() for i in (self.cfg.menu.get("items") or [])]
        used = [i for i in items if i in groups and i not in self.activities]
        inside = {a for g in used for a in groups[g]["items"]}
        out: list[str] = []
        for i in items:
            if i in used:
                out.append("_group:" + i)
            elif i in self.activities and i not in inside:
                out.append(i)
            elif i not in self.activities:
                log.warning("menu.items: no hay ninguna actividad ni grupo «%s»", i)
        out += [k for k in self.activities if k not in out and k not in activities_mod.BUILTIN and k not in inside]
        return out

    def _menu_row(self, ident: str, cur: str | None) -> dict | None:
        act = self.activities[ident]
        try:
            if not act.installed():
                return None
            sub, enabled = act.source()
        except Exception:  # noqa: BLE001
            log.exception("Menú: no pude revisar %s", ident)
            return None
        item = {"id": ident, "name": act.name, "icon": act.icon, "sub": sub, "enabled": enabled,
                "current": ident == cur}
        if self._menu_columns > 1 and isinstance(act, (activities_mod.MediaActivity,
                                                       activities_mod.AndroidActivity)):
            item["action"] = "Conectar" if isinstance(act, activities_mod.AndroidActivity) else "Carpetas"
        elif self._menu_columns > 1 and isinstance(act, activities_mod.RadioActivity) and len(act.stations) > 1:
            item["action"] = "Radios"  # OK pone la última que sonó; el botoncito, la lista para elegir otra
        elif self._menu_columns > 1 and isinstance(act, activities_mod.WebActivity) and act.bookmarks:
            item["action"] = "Sitios"  # OK abre la página de inicio; el botoncito, la lista de sitios guardados
        if ident != cur and ident in KEEP_PLAYING.get(cur or "", set()) and act.running():
            item["badge"] = "sonando"  # p. ej. Tidal de fondo mientras pasan las fotos
            item["background"] = True  # _menu_playback la corrige si en realidad está en pausa
        return item

    def _group_row(self, gid: str, g: dict, cur: str | None) -> dict | None:
        rows = [r for r in (self._menu_row(i, cur) for i in g["items"]) if r is not None]
        if not rows:
            return None
        with_content = [r["name"] for r in rows if r["enabled"]]
        item = {"id": "_group:" + gid, "group": gid, "name": g["name"], "icon": g["icon"] or "local.svg",
                "sub": " · ".join(with_content) if with_content else "Sin contenido: conectá un pendrive",
                "enabled": True, "current": any(r["current"] for r in rows)}
        bg = next((r for r in rows if r.get("background")), None)
        if bg is not None:  # Música de fondo con las fotos: la fila del grupo lo dice
            item.update(badge="sonando", background=True, bg_id=bg["id"])
        return item

    def _menu_entries(self, group: str | None = None) -> list[dict]:
        """Las opciones del menú, o las de adentro de un grupo (en un hilo aparte: mira qué está instalado y busca
        contenido en carpetas)."""
        cur = self.current_app()
        groups = self._menu_groups()
        out = []
        for ident in self._menu_order(group):
            item = (self._group_row(ident[7:], groups[ident[7:]], cur) if ident.startswith("_group:")
                    else self._menu_row(ident, cur))
            if item is not None:
                out.append(item)
        if group:
            return out
        if self._sound_on and audio.available():
            try:
                current = audio.current_name()
            except Exception:  # noqa: BLE001  (sin la salida actual, el menú igual se abre)
                log.exception("Menú: no pude leer la salida de sonido")
                current = ""
            try:
                level, muted = audio.volume_now()
            except Exception:  # noqa: BLE001
                level, muted = None, False
            vol = "en silencio" if muted else f"volumen {level} %" if level is not None else ""
            timer = self.sleep_timer.text().lower() if self.sleep_timer.active() else ""
            out.append({"id": "_sound", "name": "Sonido", "icon": "sonido.svg",
                        "sub": " · ".join(x for x in (current, vol, timer) if x) or "Elegir por dónde sale el sonido",
                        "output": current, "enabled": True, "current": False})
        out.append({"id": "_help", "name": "Ayuda", "icon": "ayuda.svg", "sub": "Qué hace cada tecla (también con •)",
                    "enabled": True, "current": False})
        return out

    def _menu_restart_timer(self) -> None:
        if self._menu_timer:
            self._menu_timer.cancel()
        self._menu_timer = None
        # Sin nada abierto el menú es la pantalla de inicio: no se cierra solo (Atrás sí lo cierra).
        if self._menu_timeout > 0 and self.current_app() is not None:
            self._menu_timer = asyncio.get_running_loop().create_task(self._menu_autoclose())

    def _menu_wait(self) -> float:
        """Cuánto espera el menú sin teclas antes de cerrarse (la ayuda se lee con más calma, y en «Sonido» se
        espera a que aparezcan los Bluetooth; al vincular un celular, a que lea el QR o se escriba el código)."""
        if self._menu_mode.startswith("android"):
            return max(self._menu_timeout, 180.0)
        return max(self._menu_timeout, 60.0) if self._menu_mode in ("help", "sound", "radio", "sites", "games", "map") else self._menu_timeout

    async def _menu_autoclose(self) -> None:
        wait = self._menu_wait()
        await asyncio.sleep(wait)
        if self._menu_open and not self._switching:
            # Se vuelve a mirar al final: si la app se terminó de cerrar mientras tanto (el menú aparece apenas se
            # pide cerrarla, antes de que se vaya del todo), el menú quedó solo en pantalla y no se oculta.
            if self.current_app() is None:
                log.info("Menú: no se cierra solo, no quedó nada abierto detrás")
                return
            log.info("Menú cerrado solo (sin teclas en %.0f s)", wait)
            await self.menu_close()

    async def _menu_playback(self, items: list[dict]) -> None:
        """La app abierta de fondo (Tidal detrás de Fotos) dice «sonando» solo si de verdad suena."""
        for item in items:
            if not item.get("background"):
                continue
            try:
                st = await self.activities[item.get("bg_id") or item["id"]].playback()
            except Exception:  # noqa: BLE001
                log.exception("Menú: no pude ver si %s está sonando", item["id"])
                continue
            item["badge"] = BACKGROUND_BADGE.get(st, "sonando" if not st else "abierto")

    async def menu_open(self, sel: int | None = None, group: str | None = KEEP, select_id: str = "") -> None:
        """Abre el menú de actividades (o vuelve a él). group: la lista de adentro de un grupo; sin decirlo, se
        queda en la que estaba si el menú ya estaba abierto (al volver de Carpetas, Sonido…). select_id: elegir
        esa fila."""
        self._sound_scan_stop()
        self._android_stop()
        if group is KEEP:
            group = self._menu_group if self._menu_open else None
        items = await asyncio.to_thread(self._menu_entries, group)
        await self._menu_playback(items)
        self._menu_group = group
        if select_id:
            sel = next((i for i, it in enumerate(items) if it["id"] == select_id), sel)
        if not items:
            await self.toast("No hay actividades para elegir")
            return
        if sel is None:
            sel = next((i for i, it in enumerate(items) if it["current"]), 0)
        sel = max(0, min(sel, len(items) - 1))
        if self._menu_mode == "activities" and not self._menu_open and self._ctx != "menu":
            self._menu_from_ctx = self._ctx  # para que la ayuda empiece por la app que se estaba usando
        mame = self.activities.get("juegos")
        if not self._menu_open and isinstance(mame, activities_mod.MameActivity) and self.current_app() == "juegos":
            await mame.freeze()  # el menú encima del juego: en pausa
        self._menu_mode = "activities"
        self._menu_items, self._menu_sel = items, sel
        self._menu_chip = False
        qr = None
        if self.phone_qr is not None and not group:
            try:
                qr = await asyncio.to_thread(self.phone_qr)
            except Exception:  # noqa: BLE001  (sin QR el menú igual se abre)
                log.exception("Menú: no pude armar el QR del celular")
        title = self._menu_groups().get(group or "", {}).get("name") or "Actividades"
        hint = GROUP_HINT if group else GRID_HINT if self._grid() else MENU_HINT
        if not await self.overlay.show(items, sel, title=title, hint=hint,
                                       columns=self._menu_columns if self._grid() else 1, qr=qr):
            log.warning("No pude mostrar el menú: %s", self.overlay.error or "?")
            await self.toast("No pude mostrar el menú")
            return
        self._menu_open = True
        log.info("Menú: %s", " · ".join(("[" + it["name"] + "]") if i == sel else it["name"]
                                        for i, it in enumerate(items)))
        self._menu_restart_timer()

    async def menu_close(self) -> None:
        resume_photos = self._menu_mode == "map" and self._map.get("resume")  # el mapa había pausado las fotos
        self._sound_scan_stop()
        self._android_stop()
        self._menu_open = False
        self._menu_group = None
        self._menu_mode = "activities"
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        await self.overlay.hide()
        mame = self.activities.get("juegos")
        if isinstance(mame, activities_mod.MameActivity) and mame.frozen:
            await mame.unfreeze()  # se vuelve al juego
        fotos = self.activities.get("fotos")
        if resume_photos and isinstance(fotos, activities_mod.PhotosActivity) and fotos.paused:
            await fotos.toggle_pause()  # sigue la presentación

    def _grid(self) -> bool:
        return (self._menu_columns > 1 and self._menu_mode == "activities" and len(self._menu_items) > 1
                and not self._menu_group)  # adentro de un grupo, una lista simple

    async def _grid_move(self, drow: int, dcol: int) -> None:
        """Moverse por la grilla del menú (la primera columna se llena primero, como la dibuja el panel)."""
        n = len(self._menu_items)
        per_col = -(-n // self._menu_columns)
        cols = -(-n // per_col)
        col, row = divmod(self._menu_sel, per_col)
        has_chip = bool(self._menu_items[self._menu_sel].get("action"))
        if (dcol > 0 and has_chip and not self._menu_chip) or (dcol < 0 and self._menu_chip):
            # ▶ enfoca el botoncito de la fila («Carpetas», «Conectar»); ◀ vuelve a la fila.
            self._menu_chip = dcol > 0
            await self.overlay.select(self._menu_sel, chip=self._menu_chip)
            self._menu_restart_timer()
            return
        chip_before, self._menu_chip = self._menu_chip, False
        if dcol:
            col = max(0, min(cols - 1, col + dcol))
            length = min(per_col, n - col * per_col)
            row = min(row, length - 1)
        else:
            length = min(per_col, n - col * per_col)
            row = (row + drow) % length  # dentro de la columna, dando la vuelta
        sel = col * per_col + row
        if sel != self._menu_sel or chip_before:
            self._menu_sel = sel
            await self.overlay.select(sel)
            log.debug("Menú: %s", self._menu_items[sel]["name"])
        self._menu_restart_timer()

    async def _menu_options(self, index: int) -> bool:
        """Las opciones de una fila del menú (el botoncito, o el amarillo): carpetas, conectar, juegos, salidas."""
        item = self._menu_items[index]
        act = self.activities.get(item["id"])
        self._menu_act_sel = index
        self._menu_chip = False
        if item.get("group"):
            await self.menu_open(group=item["group"])
        elif isinstance(act, activities_mod.MediaActivity):
            self._menu_sel = index
            await self._picker_open(item["id"])
        elif isinstance(act, activities_mod.AndroidActivity):
            await self.android_connect_open(autostart=False)
        elif isinstance(act, activities_mod.MameActivity):
            await self.games_open()
        elif item["id"] == "_sound":
            await self._sound_open()
        elif isinstance(act, activities_mod.RadioActivity) and act.stations:
            await self._radio_open()
        elif isinstance(act, activities_mod.WebActivity) and act.bookmarks:
            await self._sites_open()
        else:
            return False
        return True

    async def _menu_move(self, delta: int) -> None:
        n = len(self._menu_items)
        if not n:
            return
        self._menu_sel = (self._menu_sel + delta) % n
        self._menu_chip = False
        await self.overlay.select(self._menu_sel)
        log.debug("Menú: %s", self._menu_items[self._menu_sel]["name"])
        self._menu_restart_timer()

    async def _menu_choose(self, index: int) -> None:
        if not 0 <= index < len(self._menu_items):
            self._menu_restart_timer()
            return
        if self._menu_mode == "folders":
            await self._picker_choose(index)
            return
        if self._menu_mode == "sound":
            await self._sound_choose(index)
            return
        if self._menu_mode == "radio":
            await self._radio_choose(index)
            return
        if self._menu_mode == "sites":
            await self._sites_choose(index)
            return
        if self._menu_mode == "games":
            await self._games_choose(index)
            return
        if self._menu_mode == "help":
            self._menu_restart_timer()
            return
        item = self._menu_items[index]
        if item.get("group"):
            await self.menu_open(group=item["group"])  # «Videos y música»: su lista
            return
        if item["id"] == "_help":
            self._menu_act_sel = index
            await self.help_open(from_menu=True)
            return
        if item["id"] == "_sound":
            self._menu_act_sel = index
            await self._sound_open()
            return
        act = self.activities.get(item["id"])
        if isinstance(act, activities_mod.AndroidActivity) and not act.ready and item["id"] != self.current_app():
            # Sin celular listo: elegirlo, conectarlo por USB o vincularlo por Wi‑Fi, en el mismo panel.
            self._menu_act_sel = index
            await self.android_connect_open(autostart=True)
            return
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        if isinstance(act, activities_mod.MameActivity) and item["enabled"] and item["id"] != self.current_app():
            self._menu_act_sel = index
            await self.games_open()  # hay que elegir qué juego
            return
        if self._grid() and self._menu_chip and index == self._menu_sel and item.get("action"):
            await self._menu_options(index)  # OK sobre el botoncito («Carpetas», «Conectar»)
            return
        if not item["enabled"]:
            await self.overlay.toast(self.activities[item["id"]].unavailable_text(), 2500)
            self._menu_restart_timer()
            return
        if isinstance(act, activities_mod.RadioActivity) and not act.running() and not act.has_last():
            self._menu_act_sel = index
            await self._radio_open()  # Radio por primera vez: la lista de emisoras, para elegir cuál
            return
        if item["id"] == self.current_app() and isinstance(act, activities_mod.AndroidActivity):
            # Compartiendo la pantalla del celular: «Seguir viendo», «Dejar de compartir» u otro celular.
            self._menu_act_sel = index
            await self.android_connect_open(autostart=False)
            return
        if item["id"] == self.current_app():
            log.info("Menú: sigo en %s", item["name"])
            await self.menu_close()
            return
        if isinstance(act, activities_mod.MediaActivity) and act.browse_on_ok and not act.running():
            await self._picker_open(item["id"], at_default=True)  # Videos, Música: elegir qué, desde la carpeta
            return
        log.info("Menú: elijo %s", item["name"])
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True  # desde ya: que ninguna tecla corte el cambio
        await self.overlay.busy(f"Abriendo {item['name']}…")
        self._macro_task = asyncio.create_task(self._menu_switch(item))

    async def _menu_switch(self, item: dict) -> None:
        ok = False
        try:
            ok = await self.switch_to(item["id"], item.get("folder"), item.get("mode"))
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Error cambiando a %s", item["name"])
        finally:
            self._switching = False
            self._menu_open = False
            await self.overlay.hide()
        if not ok:
            # La actividad puede saber por qué (Android: «Aceptá el aviso en el celular»…).
            why = getattr(self.activities.get(item["id"]), "fail_text", "")
            await self.overlay.toast(why or f"No pude abrir {item['name']}", 4000 if why else 3000)

    async def a_menu(self, arg: str) -> None:
        """menu (abrir; abierto = volver atrás o cerrarlo), menu:up|down|left|right|ok|back|close|open|next|pick:N.

        ▶ (right) sobre Videos o Fotos abre el selector de carpetas; ahí ◀ (left) o Atrás (back) sube. La tecla
        Menú con el menú abierto hace lo mismo que Atrás: sale de la lista de adentro (Carpetas, Sonido, Radios,
        Ayuda…) o, en el menú principal, lo cierra.
        """
        sub, _, rest = arg.strip().lower().partition(":")
        if self._switching:
            return
        if self._menu_open and sub in ("", "toggle"):
            sub = "back"
        if self._menu_open and self._menu_mode == "help":
            await self._help_key(sub)
            return
        if sub == "help":
            self._menu_act_sel = self._menu_sel if self._menu_open else 0
            await self.help_open(from_menu=self._menu_open)
            return
        if self._menu_open and self._menu_mode.startswith("android"):
            await self._android_key(sub, rest)
            return
        if self._menu_open and self._menu_mode == "map":
            await self._map_key(sub)
            return
        if sub in ("", "next", "toggle"):
            if self._menu_open:
                await self._menu_move(+1)  # menu:next: la opción siguiente (como era antes la tecla Menú)
            else:
                await self.menu_open()
            return
        if sub == "open":
            if not self._menu_open:
                await self.menu_open()
            return
        if not self._menu_open:
            log.debug("menu:%s con el menú cerrado: lo abro", arg)
            await self.menu_open()
            return
        if sub == "delete":
            if self._menu_mode == "sites":
                await self._sites_delete_key()
            else:
                self._menu_restart_timer()
            return
        if self._sites_delete is not None:
            self._sites_delete = None
            if self._menu_mode == "sites" and sub in ("up", "prev", "previous", "down"):
                await self.overlay.show(self._menu_items, self._menu_sel, title=self._sites_title(), hint=SITES_HINT,
                                        numbers=True)  # sin el «rojo otra vez para borrar»
        if sub in ("up", "prev", "previous", "down", "left", "right") and self._grid():
            await self._grid_move({"up": -1, "prev": -1, "previous": -1, "down": 1}.get(sub, 0),
                                  {"left": -1, "right": 1}.get(sub, 0))
        elif sub in ("up", "prev", "previous"):
            await self._menu_move(-1)
        elif sub == "down":
            await self._menu_move(+1)
        elif sub == "close":
            await self.menu_close()
        elif sub == "options":
            # Amarillo: las opciones de la fila resaltada (carpetas, conectar el celular, juegos, salidas).
            if not (self._menu_mode == "activities" and await self._menu_options(self._menu_sel)):
                self._menu_restart_timer()
        elif sub in ("left", "right") and self._menu_mode == "sound" and self._menu_sel == 0:
            await self._volume_change("+" if sub == "right" else "-")
        elif sub in ("left", "right") and self._menu_mode == "sound" and self._menu_sel == 1:
            await self._timer_step(1 if sub == "right" else -1)
        elif sub == "right":
            if self._menu_mode == "folders":
                await self._picker_enter(self._menu_sel)
            elif self._menu_mode == "activities" and self._menu_items[self._menu_sel].get("group"):
                await self.menu_open(group=self._menu_items[self._menu_sel]["group"])
            elif self._menu_mode == "activities" and self._menu_items[self._menu_sel]["id"] == "_sound":
                self._menu_act_sel = self._menu_sel
                await self._sound_open()
            elif self._menu_mode == "activities" and self._menu_items[self._menu_sel]["id"] == "android":
                self._menu_act_sel = self._menu_sel
                await self.android_connect_open(autostart=False)
            elif self._menu_mode == "activities" and self._is_media(self._menu_items[self._menu_sel]["id"]):
                await self._picker_open(self._menu_items[self._menu_sel]["id"])
            elif self._menu_mode == "activities" and self._menu_items[self._menu_sel]["id"] == "juegos":
                self._menu_act_sel = self._menu_sel
                await self.games_open()
            elif self._menu_mode == "activities" and self._menu_items[self._menu_sel]["id"] in ("radio", "navegador"):
                await self._menu_options(self._menu_sel)
            else:
                self._menu_restart_timer()
        elif sub in ("left", "back") and self._menu_mode == "games":
            if self._games_back_closes:
                await self.menu_close()  # se había abierto desde el juego: seguir jugando
            else:
                await self.menu_open(self._menu_act_sel)
        elif sub in ("left", "back") and self._menu_mode == "radio" and self._radio_back_closes:
            await self.menu_close()  # se había abierto desde la Radio (amarillo): seguir escuchando
        elif sub in ("left", "back") and self._menu_mode == "sites":
            if self._sites_back_closes:
                await self.menu_close()  # se había abierto desde el Navegador (amarillo): seguir ahí
            else:
                await self.menu_open(self._menu_act_sel)
        elif sub == "left":
            if self._menu_mode == "folders":
                await self._picker_back()
            elif self._menu_mode in ("sound", "radio"):
                await self.menu_open(self._menu_act_sel)
            elif self._menu_mode == "activities" and self._menu_group:
                await self.menu_open(group=None, select_id="_group:" + self._menu_group)
            else:
                self._menu_restart_timer()
        elif sub == "back":
            if self._menu_mode == "folders":
                await self._picker_back()
            elif self._menu_mode in ("sound", "radio"):
                await self.menu_open(self._menu_act_sel)
            elif self._menu_mode == "activities" and self._menu_group:
                await self.menu_open(group=None, select_id="_group:" + self._menu_group)  # al menú principal
            else:
                await self.menu_close()
        elif sub == "ok":
            await self._menu_choose(self._menu_sel)
        elif sub == "pick":
            try:
                await self._menu_choose(int(rest) - 1)
            except ValueError:
                log.warning("menu:pick:%s inválido (número de opción)", rest)
        else:
            log.warning("menu:%s desconocida (up, down, left, right, ok, back, close, pick:N)", arg)

    # ---------------------------------------------------------------- selector de carpetas (Videos, Fotos)
    def _is_media(self, ident: str) -> bool:
        return isinstance(self.activities.get(ident), activities_mod.MediaActivity)

    def _picker_title(self, act: activities_mod.MediaActivity, path: Path) -> str:
        """«Fotos · En red: fotos-red › 2017 › 07» (las últimas partes, si es muy largo)."""
        for root in act.roots():
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            parts = list(rel.parts)
            if len(parts) > 3:
                parts = ["…"] + parts[-3:]
            return " › ".join([activities_mod.describe_folder(root), *parts])
        return str(path)

    def _picker_entries(self, ident: str, path: Path | None) -> tuple[str, list[dict]]:
        """Título y opciones del selector (en un hilo aparte: lista carpetas, que pueden estar en la red)."""
        act = self.activities[ident]
        assert isinstance(act, activities_mod.MediaActivity)
        items: list[dict] = []
        if path is None:
            default = act.folder()
            for root in act.roots():
                items.append({"kind": "dir", "path": str(root), "name": activities_mod.describe_folder(root),
                              "icon": "folder", "sub": "predeterminada" if root == default else "",
                              "enabled": True, "current": False})
            if isinstance(act, activities_mod.PhotosActivity) and not act.command and items:
                items.append(_favorites_item(act, Path(items[0]["path"])))  # las guardadas con 1
            if _resume_on(act) and items:
                found = _in_progress(act, act.roots())
                _add_watching(items, found)
                items[:0] = _continue_items(act, found, CONTINUE_MAX_ROOT)  # todos, el más reciente primero
            return f"{act.name} · elegir carpeta", items
        pick = act.pick_files and not act.command  # los archivos, para elegir uno (Videos, Música)
        subfolders = activities_mod.list_subfolders(path)
        files = activities_mod.list_media(path, act.exts, limit=None)  # los de acá (sin las subcarpetas)
        if act.play_folder or not pick:
            has = bool(files) or activities_mod.count_media(path, act.exts) > 0  # corta en el primero (con tope)
            items.append({"kind": "play", "path": str(path), "name": act.play_label, "icon": "media-playback-start",
                          "sub": (_contents_text(len(files), len(subfolders), act.unit) or "con sus subcarpetas")
                          if has else act.empty_in_folder, "enabled": has, "current": False})
        if isinstance(act, activities_mod.PhotosActivity) and not act.command and shutil.which("mpv"):
            # Los otros modos (▼ también los cambia mientras se mira). Cuántos hay se sabe si ya se recorrió.
            counts = act.counts(path)
            photos, videos = counts if counts else (None, None)
            if videos is None and activities_mod.count_media(path, activities_mod.VIDEO_EXT) == 0:
                videos = 0  # sin la lista guardada: alcanza con saber si hay alguno (corta en el primero)
            no_videos = videos == 0
            nothing = no_videos and not has
            vunit = ("video", "videos")
            # Al azar si hay fotos o videos en las subcarpetas (como lo decide la presentación al abrir).
            here = len(activities_mod.list_media(path, act.media_exts, None))
            spread = act.shuffle or (photos + videos > here if counts else bool(subfolders))
            order = "al azar" if spread else ""
            if photos and has:  # ya se recorrió: cuántas fotos hay en total (con las subcarpetas)
                items[-1]["sub"] = " · ".join([_plural(photos, act.unit)]
                                              + ([_plural(len(subfolders), ("subcarpeta", "subcarpetas"))]
                                                 if subfolders else []) + ([order] if order else []))
            elif has and order:
                items[-1]["sub"] += " · " + order
            if nothing:
                mixed = "No hay fotos ni videos en esta carpeta"
            elif photos is not None and videos is not None:
                mixed = f"{_plural(photos, act.unit)} y {_plural(videos, vunit)}, {order or 'intercalados por fecha'}"
            else:
                mixed = f"las fotos y los videos, {order or 'intercalados por fecha'}"
            items.append({"kind": "play", "mode": "mixed", "path": str(path), "name": "▶ Fotos y videos",
                          "icon": "media-playback-start", "enabled": not nothing, "current": False, "sub": mixed})
            videos_here = 0 if no_videos else len(activities_mod.list_media(path, activities_mod.VIDEO_EXT, None))
            items.append({"kind": "play", "mode": "videos", "path": str(path), "name": "▶ Solo los videos",
                          "icon": "media-playback-start", "enabled": not no_videos, "current": False,
                          "sub": "No hay videos en esta carpeta" if no_videos
                          else " · ".join(x for x in (_plural(videos, vunit) if videos
                                                      else _contents_text(videos_here, len(subfolders), vunit)
                                                      or "con sus subcarpetas", order) if x)})
        if isinstance(act, activities_mod.PhotosActivity) and path in act.roots():
            today_item = _today_item(act, path)
            if today_item is not None:
                items.append(today_item)
            if not act.command:
                items.append(_favorites_item(act, path))
        for d in subfolders:
            items.append({"kind": "dir", "path": str(d), "name": d.name, "icon": "folder", "sub": "",
                          "enabled": True, "current": False})
        if pick:
            if _resume_on(act):
                found = _in_progress(act, [path])  # solo los de esta carpeta (y sus subcarpetas)
                _add_watching(items, found)
                items[:0] = _continue_items(act, found)
            items += _file_items(path, act, files[:500])
            if not items:
                items.append({"kind": "empty", "path": str(path), "name": act.empty_in_folder, "icon": "folder",
                              "sub": "Conectá un pendrive o elegí otra carpeta", "enabled": False, "current": False})
        return f"{act.name} · {self._picker_title(act, path)}", items

    async def _picker_show(self, ident: str, path: Path | None, sel: int = 0, came_from: Path | None = None) -> bool:
        """Muestra una carpeta del selector, parado en `sel` o, si está, en la subcarpeta de la que se vuelve (las filas
        «Seguir viendo» de arriba pueden haber cambiado y correr las demás)."""
        title, items = await asyncio.to_thread(self._picker_entries, ident, path)
        if came_from is not None:
            sel = next((i for i, it in enumerate(items) if it["kind"] == "dir" and it["path"] == str(came_from)), sel)
        if not items:
            await self.overlay.toast("No hay carpetas configuradas (ver [apps.videos] / [apps.fotos] folders)", 3000)
            self._menu_restart_timer()
            return False
        self._menu_mode, self._picker_act, self._picker_path = "folders", ident, path
        self._menu_items, self._menu_sel = items, max(0, min(sel, len(items) - 1))
        await self.overlay.show(items, self._menu_sel, title=title, hint=FOLDER_HINT, numbers=False)
        log.info("Menú: %s", title)
        self._menu_restart_timer()
        return True

    async def _picker_open(self, ident: str, at_default: bool = False) -> None:
        """El selector: la lista de carpetas configuradas o, con at_default, directo en la predeterminada (◀ vuelve a
        la lista si hay más de una, o al menú)."""
        self._menu_act_sel = self._menu_sel
        self._picker_stack = []
        if at_default:
            act = self.activities[ident]
            roots, default = await asyncio.to_thread(lambda: (act.roots(), act.folder()))
            if default is not None:
                if len(roots) > 1:
                    self._picker_stack = [(None, roots.index(default) if default in roots else 0)]
                if await self._picker_show(ident, default):
                    return
                self._picker_stack = []
        await self._picker_show(ident, None)

    async def _picker_enter(self, index: int) -> None:
        item = self._menu_items[index]
        if item.get("kind") != "dir":
            self._menu_restart_timer()
            return
        self._picker_stack.append((self._picker_path, index))
        if not await self._picker_show(self._picker_act, Path(item["path"])):
            self._picker_stack.pop()

    async def _picker_back(self) -> None:
        if self._picker_stack:
            came_from = self._picker_path
            path, sel = self._picker_stack.pop()
            await self._picker_show(self._picker_act, path, sel, came_from)
        else:
            await self.menu_open(self._menu_act_sel)

    async def _picker_choose(self, index: int) -> None:
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        item = self._menu_items[index]
        if item.get("kind") == "dir":
            await self._picker_enter(index)
            return
        act = self.activities[self._picker_act]
        if not item.get("enabled", True):
            why = item["name"] if item.get("kind") == "empty" else item.get("sub")
            await self.overlay.toast(why or act.unavailable_text(), 2500)
            self._menu_restart_timer()
            return
        if item.get("kind") == "file" and item.get("resume") and not item.get("direct"):
            await self._resume_ask(index, item)  # quedó a la mitad: ¿seguir o desde el principio?
            return
        if item.get("kind") == "restart":
            await asyncio.to_thread(resume.forget, item["path"])
        log.info("Menú: %s %s", act.name, ("en " if item.get("kind") == "play" else "con ") + item["path"])
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True
        await self.overlay.busy(f"Abriendo {act.name}…")
        self._macro_task = asyncio.create_task(
            self._menu_switch({"id": self._picker_act, "name": act.name, "folder": item["path"],
                               "mode": item.get("mode")}))

    async def _resume_ask(self, index: int, item: dict) -> None:
        """Un video que quedó a la mitad: «Seguir desde 1:12:30» o «Desde el principio» (◀ vuelve a la carpeta)."""
        at = fileinfo.duration_text(item["resume"])
        self._picker_stack.append((self._picker_path, index))
        items = [{"kind": "resume", "path": item["path"], "name": f"▶ Seguir desde {at}", "icon": "media-playback-start",
                  "sub": "Donde quedó la última vez", "enabled": True, "current": False},
                 {"kind": "restart", "path": item["path"], "name": "Desde el principio", "icon": "view-refresh",
                  "sub": "Empezar de nuevo", "enabled": True, "current": False}]
        self._menu_items, self._menu_sel = items, 0
        await self.overlay.show(items, 0, title=item["name"], hint=FOLDER_HINT, numbers=False)
        log.info("Menú: %s quedó en %s", item["name"], at)
        self._menu_restart_timer()

    # ---------------------------------------------------------------- Radio: elegir la emisora
    async def _radio_open(self, from_app: bool = False) -> None:
        """La lista de [apps.radio] stations: resaltada la que suena (o la última que sonó). OK la pone."""
        self._radio_back_closes = from_app
        r = self.activities["radio"]
        assert isinstance(r, activities_mod.RadioActivity)
        playing, state = -1, ""
        if r.running():
            playing, state = await r.current(), await r.playback()
        last = r.last_index()
        items = []
        for i, st in enumerate(r.stations):
            item = {"id": f"_radio{i}", "name": st["name"], "icon": r.icon, "enabled": True,
                    "current": i == playing, "sub": "la última que sonó" if playing < 0 and i == last else ""}
            if i == playing:
                item["badge"] = BACKGROUND_BADGE.get(state, "sonando")
            items.append(item)
        sel = playing if playing >= 0 else last
        self._menu_mode, self._menu_items, self._menu_sel = "radio", items, max(0, min(sel, len(items) - 1))
        await self.overlay.show(items, self._menu_sel, title=r.name, hint=RADIO_HINT, numbers=True)
        log.info("Menú: radios: %s", " · ".join(s["name"] for s in r.stations))
        self._menu_restart_timer()

    async def _radio_choose(self, index: int) -> None:
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        r = self.activities["radio"]
        assert isinstance(r, activities_mod.RadioActivity)
        st = r.stations[index]
        if r.running():
            if await r.current() != index:
                await r.play_station(index)
            elif await r.playback() == "Paused":
                await r.set_pause(False)
            if self.current_app() == "radio":
                log.info("Menú: Radio: %s", st["name"])
                await self.menu_close()
                return
        else:
            r.want_station = index
        log.info("Menú: elijo la Radio: %s", st["name"])
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True
        await self.overlay.busy(f"Abriendo {st['name']}…")
        self._macro_task = asyncio.create_task(self._menu_switch({"id": "radio", "name": r.name}))

    # ---------------------------------------------------------------- Navegador: los sitios guardados
    async def _sites_open(self, from_app: bool = False, sel: int | None = None) -> None:
        """Los sitios guardados en el panel (desde el menú, o con el amarillo desde el Navegador). OK abre; rojo
        borra. Con el Navegador abierto, al final: «Guardar esta página» (si no está guardada) y «Escribir una
        dirección»."""
        w = self.activities["navegador"]
        assert isinstance(w, activities_mod.WebActivity)
        cur, title = await w.page_info() if w.running() else ("", "")
        items = []
        for i, b in enumerate(w.bookmarks):
            host = b["url"].split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")
            ico = w.favicon(b["url"])
            items.append({"id": f"_site{i}", "url": b["url"], "name": b["name"], "enabled": True,
                          "icon": str(ico) if ico else "navegador.svg",
                          "sub": host, "current": bool(cur) and cur.rstrip("/").startswith(b["url"].rstrip("/"))})
        save = -1
        if w.running() and activities_mod.navegador.allowed_url(cur) and w.saved_index(cur) < 0:
            save = len(items)
            items.append({"id": "_site_save", "url": cur, "name": "Guardar esta página", "icon": "★",
                          "sub": activities_mod.navegador.site_name(title, cur), "enabled": True, "current": False})
        if w.running():
            items.append({"id": "_site_go", "url": "", "name": "Escribir una dirección o buscar", "icon": "edit-find",
                          "sub": "con los números del control (0 en el Navegador)", "enabled": True, "current": False})
        if sel is None:
            # Desde el Navegador, con una página sin guardar: «Guardar esta página» ya resaltado.
            sel = save if from_app and save >= 0 else next((i for i, it in enumerate(items) if it["current"]), 0)
        sel = max(0, min(sel, len(items) - 1))
        self._sites_back_closes = from_app
        self._sites_delete = None
        self._menu_mode, self._menu_items, self._menu_sel = "sites", items, sel
        await self.overlay.show(items, sel, title=self._sites_title(), hint=SITES_HINT, numbers=True)
        log.info("Menú: sitios: %s", " · ".join(b["name"] for b in w.bookmarks))
        self._menu_restart_timer()

    def _sites_title(self) -> str:
        return f"{self.activities['navegador'].name} · sitios"

    async def _sites_delete_key(self) -> None:
        """Rojo sobre un sitio: la primera vez pregunta; la segunda (enseguida), lo borra."""
        item = self._menu_items[self._menu_sel] if 0 <= self._menu_sel < len(self._menu_items) else None
        self._menu_restart_timer()
        if item is None or not str(item["id"]).removeprefix("_site").isdigit():
            await self.overlay.show(self._menu_items, self._menu_sel, title=self._sites_title(),
                                    hint="Rojo borra un sitio guardado (este no es uno)", numbers=True)
            return
        index = int(str(item["id"]).removeprefix("_site"))
        pending, self._sites_delete = self._sites_delete, None
        if pending is None or pending[0] != index or time.monotonic() - pending[1] > SITES_DELETE_S:
            self._sites_delete = (index, time.monotonic())
            await self.overlay.show(self._menu_items, self._menu_sel, title=self._sites_title(),
                                    hint=f"Rojo otra vez = borrar «{item['name']}» · otra tecla, no", numbers=True)
            return
        w = self.activities["navegador"]
        assert isinstance(w, activities_mod.WebActivity)
        gone = w.remove_bookmark(index)
        await self._sites_open(from_app=self._sites_back_closes, sel=self._menu_sel)
        if gone is not None:
            await self.overlay.show(self._menu_items, self._menu_sel, title=self._sites_title(),
                                    hint=f"Borré «{gone['name']}» · " + SITES_HINT, numbers=True)
            if w.running() and w.on_start_page(await w.current_url()):
                await w.navigate(w.home_url())  # la página de inicio, sin el sitio borrado

    async def radio_open_from_app(self) -> None:
        """Amarillo en la Radio: la lista de radios encima; OK cambia, Atrás vuelve a la Radio."""
        r = self.activities.get("radio")
        if self._menu_open or self._switching or not isinstance(r, activities_mod.RadioActivity) or not r.stations:
            return
        self._menu_from_ctx = self._ctx
        self._menu_act_sel = 0
        self._menu_open = True
        await self._radio_open(from_app=True)

    async def sites_open_from_app(self) -> None:
        """Amarillo en el Navegador: la lista de sitios encima de la página; Atrás vuelve a la página."""
        if self._menu_open or self._switching:
            return
        self._menu_from_ctx = self._ctx
        self._menu_act_sel = 0
        self._menu_open = True
        await self._sites_open(from_app=True)

    async def _sites_choose(self, index: int) -> None:
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        w = self.activities["navegador"]
        assert isinstance(w, activities_mod.WebActivity)
        item = self._menu_items[index]
        if item["id"] == "_site_go":
            await self.menu_close()
            await self.a_web("go")
            return
        if item["id"] == "_site_save":
            added = w.add_bookmark(item["url"], item["sub"])
            if added < 0:
                await self.overlay.show(self._menu_items, index, title=self._sites_title(),
                                        hint="No pude guardar esta página · " + SITES_HINT, numbers=True)
                return
            await self._sites_open(from_app=self._sites_back_closes, sel=added)
            num = f" · {added + 1} lo abre desde el Navegador" if added < 9 else ""
            await self.overlay.show(self._menu_items, added, title=self._sites_title(),
                                    hint=f"Guardé «{w.bookmarks[added]['name']}»{num} · Atrás volver", numbers=True)
            if w.on_start_page(await w.current_url()):
                await w.navigate(w.home_url())
            return
        if w.running():
            log.info("Menú: Navegador: %s", item["name"])
            await w.navigate(item["url"])
            if self.current_app() == "navegador":
                await self.menu_close()
                return
        else:
            w.want_url = item["url"]
        log.info("Menú: elijo el Navegador en %s", item["name"])
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True
        await self.overlay.busy(f"Abriendo {item['name']}…")
        self._macro_task = asyncio.create_task(self._menu_switch({"id": "navegador", "name": w.name}))

    # ---------------------------------------------------------------- salida de sonido
    # La lista aparece enseguida con lo que ya se conoce (HDMI, Bluetooth emparejados…) y, mientras tanto, se buscan
    # parlantes y auriculares Bluetooth cercanos: los que aparecen se agregan al final, antes de la fila de búsqueda.
    async def _sound_open(self) -> None:
        outs = await asyncio.to_thread(audio.list_outputs)
        if not outs and not self._bt_scan.available():
            await self.overlay.toast("No encontré salidas de sonido", 2500)
            self._menu_restart_timer()
            return
        self._sound_outputs = outs
        self._sound_volume = await audio.get_volume()
        self._sound_scanning = self._bt_scan.available()  # la fila dice «Buscando…» desde el primer momento
        await self._sound_render(SOUND_FIRST + next((i for i, o in enumerate(outs) if o.default), 0))
        log.info("Menú: salidas de sonido: %s", " · ".join(("[" + o.name + "]") if o.default else o.name for o in outs))
        self._sound_scan_start()

    def _volume_item(self) -> dict:
        """La fila «Volumen» (la primera de Sonido): con barra; ◀ ▶ lo cambian y OK pone o saca el silencio."""
        level, muted = self._sound_volume
        return {"id": "_vol", "name": "Volumen", "icon": "audio-volume-muted" if muted else "audio-volume-high",
                "sub": "" if level is not None else "No sé cuánto está", "level": level if level is not None else -1,
                "muted": muted, "badge": "en silencio" if muted else "", "enabled": level is not None,
                "current": False}

    async def _volume_change(self, what: str) -> None:
        """◀ ▶ (bajar, subir) u OK (silencio) sobre la fila «Volumen»."""
        await audio.change_volume(what, VOLUME_STEP)
        self._sound_volume = await audio.get_volume()
        level, muted = self._sound_volume
        log.info("Menú: volumen %s", "en silencio" if muted else f"{level} %")
        if self._in_sound():
            self._menu_items[0] = self._volume_item()
            await self.overlay.level(0, self._menu_items[0])
        self._menu_restart_timer()

    def _timer_item(self) -> dict:
        """La fila «Temporizador» (la segunda de Sonido): ◀ ▶ u OK pasan por 15, 30, 45 min, 1 h, 1 h y media,
        al terminar este tema, ninguno."""
        t = self.sleep_timer
        return {"id": "_timer", "name": "Temporizador para apagar", "icon": "temporizador.svg",
                "sub": t.text() + (" · ◀ ▶ cambiarlo" if t.active() else " · ◀ ▶ elegir"), "enabled": True,
                "current": False, **({"badge": "prendido"} if t.active() else {})}

    async def timer_key(self, what: str) -> None:
        """• dos veces seguidas («show»), y cada toque mientras se ve el aviso («step»): el temporizador para apagar,
        en un aviso encima de lo que esté en pantalla (la app no se entera de esas teclas)."""
        if self._menu_open and self._menu_mode == "help":  # el primer toque abrió la Ayuda: se va
            if self._help_from_menu:
                await self.menu_open(self._menu_act_sel)
            else:
                await self.menu_close()
        text, ms = await self.sleep_timer.key_text(what)
        log.info("Temporizador (tecla): %s", text.replace("\n", " · "))
        if self._menu_open:
            if self._in_sound():
                await self._sound_render(1)  # la fila del temporizador, al día
            self._menu_restart_timer()
            await self.overlay.toast(text.replace("\n", " · "), ms)  # en la línea de abajo del menú
        else:
            await self.overlay.toast(text, ms)  # el panel: encima de todo (como la cuenta regresiva del final)

    async def _timer_step(self, delta: int) -> None:
        ok, msg = await self.sleep_timer.step(delta)
        if self._in_sound():
            await self._sound_render(1)
        if not ok:
            await self.overlay.toast(msg, 2000)
        self._menu_restart_timer()

    def _sound_items(self) -> list[dict]:
        icons = {"bluetooth": "bluetooth", "hdmi": "video-display"}
        items = [self._volume_item(), self._timer_item()]
        items += [{"id": f"_out{i}", "name": o.name, "icon": icons.get(o.kind, "audio-speakers"), "sub": o.detail,
                  "enabled": True, "current": o.default} for i, o in enumerate(self._sound_outputs)]
        if self._bt_scan.available():
            if self._sound_scanning:
                items.append({"id": "_scan", "name": "Buscando parlantes y auriculares…", "icon": "bluetooth",
                              "sub": "Ponelos en modo emparejamiento", "enabled": False, "current": False})
            else:
                items.append({"id": "_scan", "name": "Buscar Bluetooth otra vez", "icon": "bluetooth",
                              "sub": "Parlantes y auriculares cercanos", "enabled": True, "current": False})
        return items

    async def _sound_render(self, sel: int) -> None:
        items = self._sound_items()
        sel = max(0, min(sel, len(items) - 1))
        self._menu_mode, self._menu_items, self._menu_sel = "sound", items, sel
        await self.overlay.show(items, sel, title="Sonido", hint=SOUND_HINT, numbers=False)
        self._menu_restart_timer()

    def _sound_scan_start(self) -> None:
        self._sound_scan_stop()
        if self._bt_scan.available():
            self._sound_scan_task = asyncio.get_running_loop().create_task(self._sound_scan_loop())

    def _sound_scan_stop(self) -> None:
        if self._sound_scan_task and not self._sound_scan_task.done():
            self._sound_scan_task.cancel()
        self._sound_scan_task = None

    def _in_sound(self) -> bool:
        return self._menu_open and self._menu_mode == "sound" and not self._switching

    async def _sound_scan_loop(self) -> None:
        self._sound_scanning = True
        try:
            if not await self._bt_scan.start(BT_SCAN_SECONDS):
                return
            deadline = time.monotonic() + BT_SCAN_SECONDS
            while time.monotonic() < deadline and self._bt_scan.running():
                await asyncio.sleep(BT_POLL_SECONDS)
                if not self._in_sound():
                    return
                skip = {o.mac for o in self._sound_outputs if o.mac}
                new = await asyncio.to_thread(audio.bt_nearby_audio, skip)
                if not new or not self._in_sound():
                    continue
                log.info("Bluetooth: encontré %s", ", ".join(o.name for o in new))
                sel = self._menu_sel
                if sel >= SOUND_FIRST + len(self._sound_outputs):  # estaba sobre la fila de búsqueda: que siga ahí
                    sel += len(new)
                self._sound_outputs += new
                await self._sound_render(sel)
        finally:
            current = self._sound_scan_task is asyncio.current_task()
            if current or self._sound_scan_task is None:  # si ya arrancó otra búsqueda, no tocar la suya
                self._sound_scanning = False
                await self._bt_scan.stop()
            if current and self._in_sound():
                await self._sound_render(self._menu_sel)  # la fila pasa a «Buscar Bluetooth otra vez»

    async def _sound_choose(self, index: int) -> None:
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        if index == 0:  # la fila «Volumen»: OK = silencio
            await self._volume_change("mute")
            return
        if index == 1:  # «Temporizador»: OK = la opción siguiente
            await self._timer_step(1)
            return
        index -= SOUND_FIRST
        if index >= len(self._sound_outputs):  # la fila de búsqueda
            if not self._sound_scanning:
                self._sound_scanning = True
                await self._sound_render(SOUND_FIRST + index)
                self._sound_scan_start()
            else:
                self._menu_restart_timer()
            return
        out = self._sound_outputs[index]
        if out.default:
            await self.menu_open(self._menu_act_sel)
            return
        self._sound_scan_stop()  # emparejar y conectar anda mejor sin una búsqueda andando
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True  # conectar un Bluetooth tarda: que ninguna tecla lo corte
        busy = (f"Emparejando {out.name}…" if not out.paired else f"Conectando {out.name}…" if out.sink_id is None
                else f"Cambiando a {out.name}…")
        await self.overlay.busy(busy)
        self._macro_task = asyncio.create_task(self._sound_switch(out))

    async def _sound_switch(self, out: audio.Output) -> None:
        ok, text = False, ""
        try:
            ok, text = await audio.select(out)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Error cambiando la salida de sonido a %s", out.name)
            text = "No pude cambiar la salida de sonido"
        finally:
            self._switching = False
        await self.menu_open(self._menu_act_sel)
        if text:
            await self.overlay.toast(text, 3000 if ok else 4000)

    # ---------------------------------------------------------------- ayuda
    async def help_open(self, from_menu: bool = False) -> None:
        installed = await asyncio.to_thread(
            lambda: {i for i, a in self.activities.items() if a.installed()})
        self._help_pages = help_mod.pages(self.cfg, installed)
        if not from_menu:
            self._menu_from_ctx = self._ctx if self._ctx != "menu" else self._menu_from_ctx
        start = help_mod.page_for(self._menu_from_ctx or "menu")
        self._help_idx = next((i for i, (ctx, _t) in enumerate(self._help_pages) if ctx == start), 0)
        self._help_top = 0
        self._help_from_menu = from_menu
        self._menu_mode = "help"
        self._menu_open = True
        await self._help_render()
        log.info("Ayuda: %s", self._help_pages[self._help_idx][1])

    async def _help_render(self) -> None:
        ctx, title = self._help_pages[self._help_idx]
        rows = help_mod.rows(self.cfg, ctx)
        self._help_top = max(0, min(self._help_top, max(0, len(rows) - 10)))
        page = f"{self._help_idx + 1} / {len(self._help_pages)}"
        await self.overlay.help(f"Ayuda · {title}", rows, self._help_top, page, HELP_HINT)
        self._menu_restart_timer()

    async def _help_key(self, sub: str) -> None:
        if sub in ("left", "right"):
            n = len(self._help_pages)
            self._help_idx = (self._help_idx + (1 if sub == "right" else -1)) % n
            self._help_top = 0
            await self._help_render()
        elif sub in ("up", "down"):
            self._help_top += 1 if sub == "down" else -1
            self._help_top = max(0, self._help_top)
            await self._help_render()
        elif sub in ("ok", "back", "", "next", "help", "close"):
            if self._help_from_menu and sub != "close":
                await self.menu_open(self._menu_act_sel)
            else:
                await self.menu_close()
        else:
            self._menu_restart_timer()

    # ---------------------------------------------------------------- mapa de la foto (verde en Fotos)
    async def map_open(self, fotos: activities_mod.PhotosActivity, path: str, gps: tuple[float, float]) -> None:
        """Dónde se sacó la foto: el mapa en el panel (la presentación se pausa; Atrás la sigue)."""
        resume = not fotos.paused
        if resume:
            await fotos.toggle_pause()
        self._map = {"path": path, "lat": gps[0], "lon": gps[1], "zoom": photomap.START_ZOOM, "resume": resume,
                     "n": 0, "date": await asyncio.to_thread(photodates.photo_date, path),
                     "where": fotos.describe_photo({"path": path}).split("\n")[0]}
        self._menu_from_ctx = self._ctx
        self._menu_mode, self._menu_open = "map", True
        await self.overlay.busy("Buscando el mapa…")
        await self._map_render()

    async def _map_render(self) -> None:
        m = self._map
        m["n"] += 1
        out = LOG_DIR / f"mapa-foto-{m['n'] % 2}.png"  # dos nombres: el panel siempre lee la imagen nueva
        maker = photomap.MapMaker(tiles=str(self.activities["fotos"].cfg.get("map_tiles") or photomap.TILES))
        try:
            await asyncio.to_thread(maker.render, m["lat"], m["lon"], m["zoom"], out)
        except Exception:  # noqa: BLE001
            log.exception("Mapa: no pude armarlo")
            await self.menu_close()
            await self.overlay.toast("No pude armar el mapa", 3000)
            return
        if self._menu_mode != "map":  # se cerró mientras se armaba
            return
        d = m["date"]
        lines = [f"Sacada el {int(d[6:8])} de {photodates.MONTHS[int(d[4:6]) - 1]} de {d[:4]}" if d else "",
                 m["where"], f"{abs(m['lat']):.5f}° {'S' if m['lat'] < 0 else 'N'} · "
                 f"{abs(m['lon']):.5f}° {'O' if m['lon'] < 0 else 'E'}",
                 f"Zoom {m['zoom']} de {photomap.MAX_ZOOM}"]
        status = "Sin conexión: faltan partes del mapa" if maker.failed else ""
        await self.overlay.image(f"Dónde se sacó · {Path(m['path']).name}", str(out), [x for x in lines if x],
                                 status=status, hint=MAP_HINT, size=MAP_SIZE)
        self._menu_restart_timer()

    async def _map_key(self, sub: str) -> None:
        m = self._map
        zoom = m["zoom"] + {"up": 1, "ok": 1, "right": 1, "down": -1, "left": -1}.get(sub, 0)
        if sub in ("back", "close", "", "next", "toggle"):
            await self.menu_close()
            return
        zoom = max(photomap.MIN_ZOOM, min(photomap.MAX_ZOOM, zoom))
        if zoom != m["zoom"]:
            m["zoom"] = zoom
            await self._map_render()
        else:
            self._menu_restart_timer()

    async def a_help(self, arg: str) -> None:
        """Ayuda: qué hace cada tecla del control en cada app (empieza por la que se está usando)."""
        if self._switching:
            return
        await self.help_open(from_menu=False)
