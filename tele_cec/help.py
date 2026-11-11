"""Ayuda en pantalla: qué hace cada tecla del control en cada app.

Se arma a partir de la configuración real ([keys.*] con sus cadenas de búsqueda), así que si cambiás una
tecla en tu config.toml, la ayuda lo refleja.
"""

from __future__ import annotations

from typing import Any

from .config import CHAINS, Config
from .keys import key_name

# Páginas: (contexto, título). Se muestran solo las de las apps que están instaladas.
PAGES: list[tuple[str, str]] = [
    ("menu", "Menú de actividades"),
    ("browse", "Netflix · inicio y fichas"),
    ("player", "Netflix · viendo un título"),
    ("youtube", "YouTube"),
    ("tidal", "Tidal"),
    ("tidal_nav", "Tidal · modo navegación (0)"),
    ("videos", "Videos"),
    ("fotos", "Fotos"),
    ("fotos_zoom", "Fotos · con zoom (▲)"),
    ("app", "Otras apps"),
    ("offline", "Sin nada abierto"),
]
# Qué página corresponde a cada contexto (para empezar por la app que se está usando).
PAGE_FOR_CONTEXT = {"profiles": "browse", "text": "browse", "player_menu": "player"}

# Cómo se llama cada tecla en pantalla (las que significan lo mismo se muestran una vez).
KEY_LABELS = {
    "UP": "▲", "DOWN": "▼", "LEFT": "◀", "RIGHT": "▶", "SELECT": "OK", "ENTER": "OK",
    "EXIT": "Atrás", "AN_RETURN": "Atrás", "ROOT_MENU": "Menú", "CONTENTS_MENU": "Menú", "SETUP_MENU": "Menú",
    "F2_RED": "Rojo", "F4_YELLOW": "Amarillo", "F1_BLUE": "Azul", "F3_GREEN": "Verde",
    "PLAY": "Play", "PAUSE": "Pausa", "PAUSE_PLAY_FUNCTION": "⏯", "STOP": "Stop",
    "FORWARD": "⏭", "BACKWARD": "⏮", "FAST_FORWARD": "⏩", "REWIND": "⏪",
    "CHANNEL_UP": "Canal +", "CHANNEL_DOWN": "Canal −", "VOLUME_UP": "Vol +", "VOLUME_DOWN": "Vol −",
    "MUTE": "Silencio", "HELP": "Ayuda", "DISPLAY_INFORMATION": "Info", "CLEAR": "Borrar",
    **{f"NUMBER{i}": str(i) for i in range(10)},
}
# Orden en que se listan (lo más usado primero).
KEY_ORDER = ["▲", "▼", "◀", "▶", "OK", "Atrás", "⏯", "Play", "Pausa", "Stop", "⏭", "⏮", "⏩", "⏪",
             "Canal +", "Canal −", "Vol +", "Vol −", "Silencio", "Rojo", "Amarillo", "Azul", "Verde",
             *[str(i) for i in range(10)], "Borrar", "Menú", "Info", "Ayuda"]

# Descripción de cada acción. Las que tienen la misma descripción se agrupan en una fila.
ACTIONS = {
    "nav": "Moverse", "ok": "Abrir / elegir (la 1.ª vez muestra el resaltado)", "back": "Volver · cerrar ficha o menú",
    "smart_skip": "Saltar intro o resumen · siguiente episodio", "continue_watching": "Seguir viendo",
    "profiles": "¿Quién está viendo?", "profile": "Elegir perfil", "home": "Ir al inicio", "my_list": "Mi lista",
    "pause": "Pausa", "mute": "Silencio", "subtitles": "Subtítulos", "audio": "Idioma de audio",
    "next_episode": "Siguiente episodio", "nav_mode": "Recorrer los controles con las flechas",
    "type": "Escribir el número", "fullscreen": "Pantalla completa", "launch": "Abrir Netflix", "reload": "Recargar",
    "help": "Esta ayuda", "toast": "Aviso",
    "menu": "Menú de actividades",
}
SUB_ACTIONS = {
    "menu": {"": "Abrir el menú (abierto: siguiente opción)", "up": "Moverse", "down": "Moverse", "ok": "Abrir",
             "back": "Volver · cerrar", "close": "Cerrar", "left": "Volver a la carpeta de arriba",
             "right": "Videos o Fotos: elegir otra carpeta · entrar", "pick": "Elegir directo", "help": "Ayuda"},
    "music": {"play_pause": "Play / pausa", "play": "Play", "pause": "Pausa", "stop": "Pausa", "next": "Tema siguiente",
              "previous": "Tema anterior", "like": "Me gusta (favoritos)", "shuffle": "Aleatorio",
              "my_tracks": "Mis canciones (otra vez: Inicio)", "lyrics": "Letras (otra vez: volver)", "back": "Atrás en la app",
              "nav": "Modo navegación: las flechas recorren la app (otra vez: volver)", "move": "Moverse por la app",
              "tab": "Siguiente elemento (si las flechas se traban)"},
    "video": {"play_pause": "Play / pausa", "play": "Play", "pause": "Pausa", "stop": "Pausa", "next": "Video siguiente",
              "previous": "Video anterior", "subtitles": "Subtítulos", "audio": "Idioma de audio",
              "aspect": "Proporción de la imagen"},
    "fotos": {"next": "Foto siguiente", "previous": "Foto anterior", "pause": "Pausar o seguir la presentación",
              "rotate": "Girar la foto", "random": "Una foto al azar", "info": "Qué foto es (carpeta y archivo)",
              "filename": "Nombre del archivo (chico)", "zoom_in": "Zoom: acercar", "zoom_out": "Alejar",
              "fit": "Foto entera (y sigue la presentación)", "scroll": "Mover la foto",
              "media_play_pause": "Con música: play / pausa · sin música: pausar la presentación",
              "media_next": "Con música: tema siguiente · sin música: foto siguiente",
              "media_previous": "Con música: tema anterior · sin música: foto anterior"},
}


def describe(action: Any, ctx: str, cfg: Config) -> str:
    """Texto corto de lo que hace una acción en ese contexto."""
    if isinstance(action, list):
        return "Varias acciones seguidas (macro)"
    name, _, arg = str(action).strip().partition(":")
    sub, _, rest = arg.partition(":")
    if name == "menu" and sub == "":
        return "Siguiente opción" if ctx == "menu" else "Menú de actividades (y la ayuda)"
    if name == "yt":
        if sub == "key":
            return {"Up": "Moverse", "Down": "Moverse", "Left": "Moverse", "Right": "Moverse",
                    "Return": "Elegir / OK", "Escape": "Volver"}.get(rest, f"Tecla {rest}")
        if sub == "seek":
            return _plus_minus("seek", rest or "+")
        return {"play_pause": "Play / pausa", "play": "Play", "pause": "Pausa"}.get(sub, str(action))
    if name in SUB_ACTIONS:
        if name == "music" and sub == "page":
            return {"inicio": "Ir a Inicio", "explorar": "Ir a Explorar"}.get(rest, "Ir a Colección")
        if name == "music" and sub in ("volume", "seek"):
            return _plus_minus(sub, rest or "+")
        if name == "video" and sub in ("volume", "seek"):
            return _plus_minus(sub, rest or "+")
        if name == "music" and sub == "key":
            return {"Return": "OK en la app", "Up": "Moverse por la app", "Down": "Moverse por la app",
                    "Left": "Moverse por la app", "Right": "Moverse por la app"}.get(rest, f"Tecla {rest} en la app")
        if name == "music" and sub == "tab" and rest:
            return "Elemento anterior (si las flechas se traban)"
        return SUB_ACTIONS[name].get(sub, str(action))
    if name in ("play", "play_pause"):
        return "Reproducir el título resaltado" if ctx in ("browse", "profiles") else "Play / pausa"
    if name in ("seek", "volume"):
        return _plus_minus(name, arg or "+")
    if name == "app":
        apps = cfg.apps.get(arg) or {}
        return f"Abrir {apps.get('name') or arg.capitalize()}"
    if name == "macro":
        return f"Macro «{arg}»"
    if name == "key":
        return f"Tecla {arg}"
    return ACTIONS.get(name, str(action))


def _plus_minus(kind: str, arg: str) -> str:
    if kind == "volume":
        return "Subir volumen" if not arg.startswith("-") else "Bajar volumen"
    sec = arg.lstrip("+-") or "10"
    return f"Adelantar {sec} s" if not arg.startswith("-") else f"Atrasar {sec} s"


def _keys_text(labels: list[str]) -> str:
    """«1 2 3 4 5» → «1 – 5»; el resto separado por espacios."""
    nums = sorted(int(x) for x in labels if x.isdigit())
    other = [x for x in labels if not x.isdigit()]
    if len(nums) > 2 and nums == list(range(nums[0], nums[-1] + 1)):
        other.append(f"{nums[0]} – {nums[-1]}")
    else:
        other += [str(n) for n in nums]
    return "  ".join(other)


def rows(cfg: Config, ctx: str) -> list[tuple[str, str]]:
    """Filas (teclas, qué hacen) de un contexto, según la configuración (con su cadena de búsqueda)."""
    codes: list[int] = []
    for c in CHAINS.get(ctx, (ctx, "global")):
        for code in cfg.bindings.get(c, {}):
            if code not in codes:
                codes.append(code)
    groups: dict[str, list[str]] = {}
    for code in codes:
        found = cfg.action_for(ctx, code)
        if not found:
            continue
        label = KEY_LABELS.get(key_name(code), key_name(code))
        desc = describe(found[0], ctx, cfg)
        keys = groups.setdefault(desc, [])
        if label not in keys:
            keys.append(label)

    def order(label: str) -> int:
        return KEY_ORDER.index(label) if label in KEY_ORDER else len(KEY_ORDER)

    out = []
    for desc, labels in groups.items():
        labels.sort(key=order)
        out.append((min(order(x) for x in labels), _keys_text(labels), desc))
    out.sort(key=lambda r: r[0])
    return [(k, d) for _o, k, d in out]


def pages(cfg: Config, installed: set[str]) -> list[tuple[str, str]]:
    """Páginas que tienen sentido: las de las apps instaladas (installed = ids de actividades)."""
    needs = {"browse": "netflix", "player": "netflix", "youtube": "youtube", "tidal": "tidal", "tidal_nav": "tidal",
             "videos": "videos", "fotos": "fotos", "fotos_zoom": "fotos"}
    out = []
    for ctx, title in PAGES:
        if ctx in needs and needs[ctx] not in installed:
            continue
        if ctx == "app" and not any(i not in ("netflix", "youtube", "tidal", "videos", "fotos") for i in installed):
            continue
        out.append((ctx, title))
    return out


def page_for(ctx: str) -> str:
    return PAGE_FOR_CONTEXT.get(ctx, ctx)


__all__ = ["pages", "rows", "page_for", "describe"]
