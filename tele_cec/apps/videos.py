"""Videos locales con mpv: el elegido en el menú, en pantalla completa (ver player.py: controles y barra para la TV).

Seguir viendo: al cerrarlo a la mitad, mpv guarda dónde quedó y la próxima vez sigue desde ahí (resume.py); el menú
y el celular preguntan si seguir o empezar desde el principio.
"""

from __future__ import annotations

from . import resume
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
    UNIT = "Video"

    SUB_SCALE = 1.15   # tamaño de la letra de los subtítulos (1 = el de mpv)

    def sub_scale(self) -> float:
        try:
            scale = float(self.cfg.get("sub_scale", self.SUB_SCALE))
        except (TypeError, ValueError):
            return self.SUB_SCALE
        return scale if 0.3 <= scale <= 4 else self.SUB_SCALE

    def extra_args(self) -> list[str]:
        # Subtítulos: los que estén al lado del video aunque el nombre no sea idéntico; idioma preferido español.
        args = ["--sub-auto=fuzzy", "--slang=spa,es,es-419,eng,en", "--alang=spa,es,es-419"]
        args.append(f"--sub-scale={self.sub_scale():g}")   # letra de los subtítulos un poco más grande, para la TV
        if self.cfg.get("resume", True):  # Seguir viendo: mpv guarda dónde quedó al cerrarlo (ver resume.py)
            args += resume.mpv_args()
        if self.cfg.get("hide_title"):
            args.append("--script-opts-append=tele_cec_osd-title=no")
        return args
