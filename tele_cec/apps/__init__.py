"""Actividades que se eligen con el menú: Netflix, YouTube, Tidal, Videos y Música (mpv), Radio (mpv), Fotos (feh y
mpv), Android (scrcpy), Juegos (MAME) y programas propios.

Una sola actividad abierta a la vez: elegir otra cierra la actual. Cada una sabe si está instalada, si
está abierta, de dónde va a sacar el contenido (para la línea secundaria del menú), abrirse y cerrarse.

    base.py       Activity, ProcActivity (programa en su scope de systemd) y CommandActivity ([activities.<id>])
    folders.py    carpetas de contenido (pendrives, carpetas locales y de red)
    media.py      base de Videos, Música y Fotos (una carpeta)     player.py   base de Videos y Música (mpv)
    videos.py     videos (mpv)     music.py    música (mpv)     photos.py   feh y mpv     mpv.py   canal de mpv
    radio.py      radios por internet (mpv)
    android.py    scrcpy (pantalla del celular)
    mame.py       Juegos: las ROMs de MAME
    netflix/      navegador + script inyectado     youtube/   YouTube para TV
    tidal/        High Tide (MPRIS, parche y canal de control)
    webprofile.py perfiles de Firefox y Chromium (los usan Netflix y YouTube)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .android import AndroidActivity
from .mame import MameActivity
from .base import Activity, CommandActivity, ProcActivity, _custom_command
from .folders import (AUDIO_EXT, IMAGE_EXT, VIDEO_EXT, count_media, describe_folder, expand_folders,  # noqa: F401
                      find_media_folder, list_media, list_subfolders, scan_media)
from .media import MediaActivity
from .music import MusicActivity
from .player import MpvMediaActivity
from .netflix import NetflixActivity
from .photos import PhotosActivity
from .radio import RadioActivity
from .tidal import TidalActivity
from .videos import VideosActivity
from .youtube import YouTubeActivity

if TYPE_CHECKING:
    from ..controller import Controller

__all__ = ["AndroidActivity", "MameActivity", "BUILTIN", "Activity", "CommandActivity", "ProcActivity", "MediaActivity",
           "MpvMediaActivity", "MusicActivity", "NetflixActivity", "PhotosActivity", "RadioActivity", "AUDIO_EXT",
           "TidalActivity", "VideosActivity", "YouTubeActivity", "IMAGE_EXT", "VIDEO_EXT", "build", "count_media",
           "describe_folder", "expand_folders", "find_media_folder", "list_media", "list_subfolders", "scan_media",
           "_custom_command"]

log = logging.getLogger("actividades")

# Las actividades propias (las de [activities.<id>] son las demás).
BUILTIN = ("netflix", "youtube", "tidal", "videos", "musica", "radio", "fotos", "android", "juegos")


def build(ctl: Controller, cfg: Any) -> dict[str, Activity]:
    """Todas las actividades conocidas: las propias y las de [activities.<id>]."""
    apps = cfg.apps
    acts: dict[str, Activity] = {
        "netflix": NetflixActivity(ctl, apps.get("netflix")),
        "tidal": TidalActivity(ctl, apps.get("tidal")),
        "youtube": YouTubeActivity(apps.get("youtube"), cfg.browser),
        "videos": VideosActivity(apps.get("videos")),
        "musica": MusicActivity(apps.get("musica")),
        "radio": RadioActivity(apps.get("radio")),
        "fotos": PhotosActivity(apps.get("fotos")),
        "android": AndroidActivity(apps.get("android")),
        "juegos": MameActivity(apps.get("juegos")),
    }
    for ident, item in (cfg.raw.get("activities") or {}).items():
        if not isinstance(item, dict):
            continue
        if ident in acts:
            log.warning("[activities.%s]: ese nombre es de una actividad propia; elegí otro", ident)
            continue
        if not item.get("command"):
            log.warning("[activities.%s]: falta command", ident)
            continue
        acts[ident] = CommandActivity(ident, item)
    return acts
