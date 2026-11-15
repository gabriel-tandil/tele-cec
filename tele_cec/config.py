"""Carga de configuración: defaults.toml del paquete + ~/.config/tele-cec/config.toml."""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from .keys import key_name, parse_key

log = logging.getLogger("config")

PKG_DIR = Path(__file__).resolve().parent
DEFAULTS_FILE = PKG_DIR / "defaults.toml"
USER_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "tele-cec" / "config.toml"

CONTEXTS = ("global", "profiles", "browse", "player", "player_menu", "text", "offline", "tidal", "tidal_nav",
            "menu", "youtube", "videos", "musica", "radio", "fotos", "fotos_zoom", "android", "juegos", "app")

# Orden de búsqueda de una tecla según el contexto actual.
CHAINS: dict[str, tuple[str, ...]] = {
    "profiles": ("profiles", "browse", "global"),
    "browse": ("browse", "global"),
    "player": ("player", "global"),
    "player_menu": ("player_menu", "browse", "global"),
    "text": ("text", "browse", "global"),
    "offline": ("offline",),
    # Tidal (High Tide): navegador cerrado y High Tide abierto. tidal_nav = flechas recorren su interfaz.
    "tidal": ("tidal", "global"),
    "tidal_nav": ("tidal_nav", "tidal", "global"),
    # Menú de actividades abierto: captura todas las teclas (ninguna llega a la app de abajo).
    "menu": ("menu",),
    # Videos y Música (mpv), Fotos (feh y mpv) y los programas de [activities.<id>].
    "youtube": ("youtube", "global"),  # YouTube para TV
    "android": ("android", "global"),  # Android: la pantalla del celular (scrcpy)
    "juegos": ("juegos", "global"),    # Juegos (MAME)
    "videos": ("videos", "global"),
    "musica": ("musica", "global"),    # Música local
    "radio": ("radio", "global"),      # Radio por internet
    "fotos": ("fotos", "global"),
    "fotos_zoom": ("fotos_zoom", "fotos", "global"),  # Fotos con zoom: las flechas mueven la foto
    "app": ("app", "global"),
}

# Selectores de la web de Netflix (se pueden pisar en [selectors]).
DEFAULT_SELECTORS: dict[str, Any] = {
    # Elementos que se pueden enfocar con las flechas.
    "focusable": (
        'a[href], button, [role="button"], [role="link"], [role="menuitem"], '
        '[role="menuitemradio"], [role="option"], [role="tab"], [role="checkbox"], '
        '[role="radio"], [role="switch"], input:not([type="hidden"]), select, textarea, '
        '[tabindex]:not([tabindex="-1"])'
    ),
    # Nunca enfocar (flechas de las filas, títulos de fila "Explorar todo", enlaces para lectores de pantalla…).
    "ignore": (
        '.handle, .handleNext, .handlePrev, .pagination-indicator, a.rowTitle, .rowHeader a, '
        '.skip-link, [data-uia="skip-link"], [data-uia^="carousel-hawkins"], [data-uia="player"], #nfx-ring, #nfx-toast, footer *, .member-footer *'
    ),
    # Ventanas modales: si hay una abierta, la navegación queda dentro de ella.
    "modal": '[role="dialog"], [aria-modal="true"], .previewModal--container.detail-modal, .focus-trap-wrapper',
    # Barra superior fija (se evita al subir mientras haya otra cosa arriba).
    "header": '.pinning-header, .main-header, [role="banner"], header, [data-uia="navigation"]',
    # Fila de títulos y sus flechas para pasar de página.
    "row": '.lolomoRow, .rowContainer, [data-list-context], section.carousel-row, [data-uia^="carousel-row-section"]',
    "row_next": '.handleNext, [data-uia="slider-next"], [data-uia="carousel-hawkins-right-button"], button[aria-label*="next" i], button[aria-label*="siguiente" i], button[aria-label*="más títulos" i]',
    "row_prev": '.handlePrev, [data-uia="slider-prev"], [data-uia="carousel-hawkins-left-button"], button[aria-label*="previous" i], button[aria-label*="anterior" i]',
    "row_header": '.rowHeader, .row-header, .rowTitle, h2',
    "card": '.title-card a, .title-card-container a, .slider-item a, [data-uia="title-card"] a, a[data-uia="standard-card"], a[data-uia="progress-card"]',
    # Elementos que Netflix anima por JavaScript cuadro a cuadro (Framer Motion escribe transform y opacity en
    # el estilo del elemento en cada cuadro: el CSS de animaciones no lo alcanza). Con reduce_effects se les
    # fija transform/opacity con !important, que le gana a ese estilo: la ficha aparece de una vez.
    "no_motion": '.previewModal--container.detail-modal',
    # Primer elemento a enfocar cuando no hay ninguno (en orden de preferencia).
    "initial": [
        '[data-uia="player-skip-intro"]',
        '[data-uia="play-button"]',
        '[data-uia="play-video-button"]',
        'a.profile-link',
        '[data-uia="action-select-profile+primary"]',
        '[data-uia^="profile-selector+tile-"]',
        '.title-card a',
        'a[data-uia="standard-card"]',
    ],
    # Pantalla "¿Quién está viendo?".
    "profile_gate": '.list-profiles, .choose-profile, .profile-gate-container, [data-uia="profile-choices-page"], [data-uia="profiles-gate"], [data-uia="profile-gate-screen"], [data-uia="profile-selector"]',
    "profile_link": 'a.profile-link, [data-uia="action-select-profile+primary"], [data-uia="profile-link"], [data-uia^="profile-selector+tile-"]',
    "profile_exclude": '.addProfileIcon, .profile-button, [data-uia*="add-profile"], [data-uia*="manage-profile"], [data-uia="profile-selector+add"], [data-uia="profile-gate-screen+manage"], .account-dropdown-button, .account-menu-item',
    # "Seguir viendo".
    "cw_row": '[data-list-context="continueWatching"]',
    "cw_text": "seguir viendo|continuar viendo|continue watching|keep watching",
    # Botón inteligente: se prueba en este orden.
    "skip": [
        ['[data-uia="player-skip-intro"]', "Saltar intro"],
        ['[data-uia="player-skip-recap"]', "Saltar resumen"],
        ['[data-uia="player-skip-preplay"]', "Saltar"],
        ['[data-uia="next-episode-seamless-button"]', "Siguiente episodio"],
        ['[data-uia="next-episode-seamless-button-draining"]', "Siguiente episodio"],
        ['[data-uia="postplay-next-episode"]', "Siguiente episodio"],
    ],
    "skip_text": (
        r"(skip|saltar|omitir)\s+(intro|recap|resumen|introducci[oó]n|apertura|cr[eé]ditos)|"
        r"(next|siguiente|pr[oó]ximo)\s+episod"
    ),
    "next_episode": '[data-uia="control-next"], [data-uia="next-episode-seamless-button"]',
    "player_back": '[data-uia="nfplayer-exit"], [data-uia="player-back-to-browse"], [data-uia="control-nav-back"], button[aria-label*="back to browse" i], button[aria-label*="volver" i]',
    # Si alguno de estos está visible en el reproductor, las flechas navegan en vez de adelantar/volumen.
    "player_menu": (
        '[data-uia="selector-audio-subtitle"], [data-uia="episode-selector"], [data-uia*="episode-list"], '
        '[data-uia="playback-speed"], [data-uia*="postplay"], .postplay, [data-uia="player-error"], '
        '[role="dialog"], [role="menu"], [role="listbox"]'
    ),
    "respect_aria_hidden": True,
    "smooth_scroll": True,
    # Además del resaltado, mover el foco real del navegador (element.focus()).
    "native_focus": False,
}


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path | None
    bindings: dict[str, dict[int, Any]] = field(default_factory=dict)

    # Accesos cómodos
    @property
    def cec(self) -> dict[str, Any]:
        return self.raw["cec"]

    @property
    def browser(self) -> dict[str, Any]:
        return self.raw["browser"]

    @property
    def netflix(self) -> dict[str, Any]:
        return self.raw["netflix"]

    @property
    def apps(self) -> dict[str, Any]:
        return self.raw.get("apps") or {}

    @property
    def menu(self) -> dict[str, Any]:
        return self.raw.get("menu") or {}

    @property
    def macros(self) -> dict[str, Any]:
        return self.raw.get("macros", {})

    @property
    def selectors(self) -> dict[str, Any]:
        return self.raw["selectors"]

    def action_for(self, context: str, code: int) -> tuple[Any, str] | None:
        """Devuelve (acción, contexto donde se encontró) o None."""
        for ctx in CHAINS.get(context, (context, "global")):
            action = self.bindings.get(ctx, {}).get(code)
            if action is None:
                continue
            if action in ("", "none", []):
                return None  # desactivada explícitamente
            return action, ctx
        if context == "menu":
            # Con el menú abierto, la tecla que lo abre (si la cambiaste en [keys.global]) pasa a la
            # siguiente opción, aunque no esté en [keys.menu].
            action = self.bindings.get("global", {}).get(code)
            if isinstance(action, str) and action.strip().partition(":")[0] == "menu":
                return action, "global"
        return None


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _build_bindings(keys_tbl: dict[str, Any]) -> dict[str, dict[int, Any]]:
    bindings: dict[str, dict[int, Any]] = {}
    for ctx, table in keys_tbl.items():
        if ctx not in CONTEXTS:
            log.warning("Contexto desconocido en [keys.%s] (válidos: %s)", ctx, ", ".join(CONTEXTS))
            continue
        if not isinstance(table, dict):
            continue
        out: dict[int, Any] = {}
        for key, action in table.items():
            code = parse_key(key)
            if code is None:
                log.warning("Tecla desconocida en [keys.%s]: %r (usá tele-cec aprender)", ctx, key)
                continue
            if not isinstance(action, (str, list)):
                log.warning("Acción inválida para %s en [keys.%s]: %r", key, ctx, action)
                continue
            out[code] = action
        bindings[ctx] = out
    return bindings


def load(path: str | os.PathLike[str] | None = None) -> Config:
    with open(DEFAULTS_FILE, "rb") as fh:
        raw = tomllib.load(fh)
    raw["selectors"] = _merge(DEFAULT_SELECTORS, raw.get("selectors", {}))

    cfg_path = Path(path).expanduser() if path else USER_CONFIG
    used: Path | None = None
    if cfg_path.is_file():
        with open(cfg_path, "rb") as fh:
            user = tomllib.load(fh)
        # Las teclas se fusionan tecla por tecla (por eso _merge también entra en keys.*).
        raw = _merge(raw, user)
        used = cfg_path
    elif path:
        raise FileNotFoundError(f"No existe el archivo de configuración: {cfg_path}")

    cfg = Config(raw=raw, path=used)
    cfg.bindings = _build_bindings(raw.get("keys", {}))
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    b = cfg.browser
    if b.get("launch") not in ("on_key", "always", "never"):
        log.warning("browser.launch=%r inválido; uso 'on_key'", b.get("launch"))
        b["launch"] = "on_key"
    c = cfg.cec
    if c.get("activate_source") not in ("start", "launch", "both", "never"):
        log.warning("cec.activate_source=%r inválido; uso 'both'", c.get("activate_source"))
        c["activate_source"] = "both"
    if c.get("device_type") not in ("r", "p", "t", "a", "x"):
        log.warning("cec.device_type=%r inválido; uso 'r'", c.get("device_type"))
        c["device_type"] = "r"
    items = cfg.menu.get("items")
    if items is not None and not isinstance(items, list):
        log.warning("menu.items debe ser una lista de actividades; uso la de por defecto")
        cfg.raw.setdefault("menu", {})["items"] = ["netflix", "tidal", "videos", "fotos"]
    for name in cfg.macros:
        if not isinstance(cfg.macros[name], (list, str)):
            log.warning("La macro %r debe ser una lista de acciones", name)


def describe_bindings(cfg: Config, code: int) -> str:
    """Texto corto con lo que hace una tecla en cada contexto (para el modo aprendizaje)."""
    parts = []
    for ctx in ("browse", "player", "profiles", "player_menu", "tidal", "videos", "fotos", "fotos_zoom", "menu",
                "offline"):
        found = cfg.action_for(ctx, code)
        if found:
            action, _ = found
            parts.append(f"{ctx}: {action if isinstance(action, str) else ' + '.join(map(str, action))}")
    return " | ".join(parts) if parts else f"sin asignar (agregala en [keys.*] como {key_name(code)})"
