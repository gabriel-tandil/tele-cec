"""Música local con mpv: los temas de una carpeta (con sus subcarpetas), con la tapa del disco en la TV.

La tapa sale del archivo (si la trae) o de cover.jpg / folder.jpg / front.jpg… al lado de los temas. Abajo, la barra
para la TV queda siempre a la vista: tema, artista, disco y tiempo. Puede quedar sonando de fondo mientras pasan las
fotos, como Tidal.
"""

from __future__ import annotations

from .folders import AUDIO_EXT
from .player import MpvMediaActivity


class MusicActivity(MpvMediaActivity):
    id = "musica"
    name = "Música"
    icon = "musica.svg"
    context = "musica"
    exts = AUDIO_EXT
    empty_text = "Sin música: conectá un pendrive"
    play_label = "▶ Escuchar esta carpeta"
    empty_in_folder = "No hay música en esta carpeta"
    unit = ("tema", "temas")
    audio_only = True
    browse_on_ok = True   # OK en el menú: la carpeta predeterminada (▶ Escuchar esta carpeta, o un tema)
    pick_files = True     # un tema elegido: desde ese, con los demás de su carpeta
    pick_continues = True
    thumbnails = False
    TITLE = "tele-cec musica"

    def extra_args(self) -> list[str]:
        # La tapa: la del archivo o una imagen al lado (cover.jpg, folder.jpg…); sin tapa, fondo negro y la barra.
        return ["--cover-art-auto=fuzzy", "--audio-display=embedded-first", "--image-display-duration=inf",
                "--background-color=#101014",
                "--video-margin-ratio-top=0.05", "--video-margin-ratio-bottom=0.24"]  # la tapa entera, sobre la barra
