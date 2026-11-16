"""Lógica principal: teclas CEC -> contexto -> acción sobre la app que está en pantalla.

El Controller junta varias partes (mixins), cada una en su archivo con su estado:

    core.py        cola de teclas, contexto, run_action, macros
    browser.py     conexión con el navegador, primitivas sobre la página, indicador «trabajando»
    netflix.py     acciones de Netflix
    activities.py  qué actividad está en pantalla y el cambio de una a otra
    menu.py        menú de actividades, selector de carpetas y ayuda
    android.py     «Android · conectar»: elegir el celular y vincularlo por Wi‑Fi (QR o código)
    media.py       acciones de Videos, YouTube, Fotos y Tidal
    web.py         acciones del Navegador (flechas por la página, escribir con los números, sitios)
    sleep.py       reposo: guardar, cerrar y volver
"""

from __future__ import annotations

from typing import Any

from ..apps.netflix.browser import Browser
from ..apps.netflix.script import build_script
from ..apps.tidal.hightide import HighTide
from ..config import Config
from ..system.gamepad import Gamepads
from ..system.power import IdleShutdown
from ..ui.overlay import Overlay
from .activities import ActivitiesMixin
from .android import AndroidMenuMixin
from .browser import BrowserMixin
from .core import CoreMixin
from .games import GamesMenuMixin
from .media import MediaMixin
from .menu import MenuMixin
from .netflix import NetflixMixin
from .sleep import SleepMixin
from .web import WebMixin


class Controller(CoreMixin, BrowserMixin, NetflixMixin, ActivitiesMixin, MenuMixin, AndroidMenuMixin, GamesMenuMixin,
                 MediaMixin, WebMixin, SleepMixin):
    def __init__(self, cfg: Config, browser: Browser):
        self.cfg = cfg
        self.browser = browser
        self.source: Any = None  # CecClientSource / StdinSource (para "fuente activa")
        self.script, self.version = build_script(cfg)
        # Tidal (High Tide): app aparte, sin navegador.
        self.music = HighTide(cfg.apps.get("tidal"))
        # El panel del menú; con [menu] keyboard, mientras se ve, también se maneja con el teclado.
        self.overlay = Overlay(bool(cfg.menu.get("keyboard", True)), self.on_key, self.on_release)
        # Joysticks: cada botón es una tecla del control ([gamepad]).
        self.gamepads = Gamepads(cfg.raw.get("gamepad"), self.on_gamepad_key, self.on_release)
        pad_cfg = cfg.raw.get("gamepad") or {}
        self._gamepad_menu_only = {str(c) for c in pad_cfg.get("menu_only_in", ["app", "juegos"]) or []}
        # En los juegos, el botón del medio abre nuestro menú (MAME arranca sin él: ver apps/mame.py)…
        self._gamepad_menu_buttons = {str(b).upper() for b in pad_cfg.get("menu_buttons", ["GUIDE"]) or []}
        # …y, si se configura (p. ej. ["BACK:HOLD"]), otro botón sale del juego directo.
        self._gamepad_exit_buttons = {str(b).upper() for b in pad_cfg.get("exit_buttons", []) or []}
        self._init_core()
        self._init_browser()
        self._init_netflix()
        self._init_media()
        self._init_activities()
        self._init_menu()
        self._init_android_menu()
        # Reposo o apagado automático ([power]): sin sonido ni teclas durante un rato, la Pi descansa.
        self.power = IdleShutdown(self, cfg.raw.get("power"))
