"""Videos locales con mpv: el elegido en el menú, en pantalla completa (ver player.py: controles y barra para la TV).

Seguir viendo: al cerrarlo a la mitad, mpv guarda dónde quedó y la próxima vez sigue desde ahí (resume.py); el menú
y el celular preguntan si seguir o empezar desde el principio.

Favoritos (1 = ★ en el control): mirando un video, o parado sobre él en la lista, lo guarda en
~/.local/share/tele-cec/videos-favoritos.txt (otra vez lo saca). En Carpetas está «★ Favoritos» para verlos juntos.
"""

from __future__ import annotations

from pathlib import Path

from ..system.procs import DATA_DIR
from . import resume
from .favorites import FavoritesMixin
from .folders import VIDEO_EXT
from .player import MpvMediaActivity

FAVORITES_FILE = "videos-favoritos.txt"
# «★ Favoritos» en el selector de carpetas: no es una carpeta de verdad (las configuradas son rutas absolutas).
FAVORITES_DIR = Path("★ Favoritos")


class VideosActivity(FavoritesMixin, MpvMediaActivity):
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

    def favorites_file(self) -> Path:
        return DATA_DIR / FAVORITES_FILE

    async def current_path(self) -> str:
        """El archivo que se está viendo ("" si no se sabe)."""
        path = await self.mpv.get("path") if self.running() else None
        return str(path) if path else ""

    SUB_SCALE = 1.15   # tamaño de la letra de los subtítulos (1 = el de mpv)

    def sub_scale(self) -> float:
        try:
            scale = float(self.cfg.get("sub_scale", self.SUB_SCALE))
        except (TypeError, ValueError):
            return self.SUB_SCALE
        return scale if 0.3 <= scale <= 4 else self.SUB_SCALE

    def tag_titles(self) -> bool:
        # Los títulos de las etiquetas de los videos bajados suelen estar mal (el del grupo que lo subió, otra
        # película...): por defecto se muestra el nombre del archivo. tag_titles = true vuelve a las etiquetas.
        return bool(self.cfg.get("tag_titles", False))

    def extra_args(self) -> list[str]:
        # Subtítulos: los que estén al lado del video aunque el nombre no sea idéntico; idioma preferido español.
        args = ["--sub-auto=fuzzy", "--slang=spa,es,es-419,eng,en", "--alang=spa,es,es-419"]
        args.append(f"--sub-scale={self.sub_scale():g}")   # letra de los subtítulos un poco más grande, para la TV
        if self.cfg.get("resume", True):  # Seguir viendo: mpv guarda dónde quedó al cerrarlo (ver resume.py)
            args += resume.mpv_args()
        if self.cfg.get("hide_title"):
            args.append("--script-opts-append=tele_cec_osd-title=no")
        return args
