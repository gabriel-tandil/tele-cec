"""Videos locales con mpv: el elegido en el menú, en pantalla completa (ver player.py: controles y barra para la TV)."""

from __future__ import annotations

from .folders import VIDEO_EXT
from .player import MpvMediaActivity


class VideosActivity(MpvMediaActivity):
    id = "videos"
    name = "Videos"
    icon = "peliculas.svg"
    context = "videos"
    exts = VIDEO_EXT
    empty_text = "Sin videos: conectá un pendrive"
    play_label = "▶ Reproducir esta carpeta"
    empty_in_folder = "No hay videos en esta carpeta"
    unit = ("video", "videos")
    browse_on_ok = True   # OK en el menú: la carpeta predeterminada, para elegir qué ver
    pick_files = True     # se elige el video (solo ese), no la carpeta entera
    play_folder = False
    TITLE = "tele-cec videos"

    def extra_args(self) -> list[str]:
        # Subtítulos: los que estén al lado del video aunque el nombre no sea idéntico; idioma preferido español.
        args = ["--sub-auto=fuzzy", "--slang=spa,es,es-419,eng,en", "--alang=spa,es,es-419"]
        if self.cfg.get("hide_title"):
            args.append("--script-opts-append=tele_cec_osd-title=no")
        return args
