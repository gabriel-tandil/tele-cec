"""Ayuda en pantalla: qué hace cada tecla del control en cada app.

Se arma a partir de la configuración real ([keys.*] con sus cadenas de búsqueda), así que si cambiás una
tecla en tu config.toml, la ayuda lo refleja.
"""

from __future__ import annotations

from typing import Any

from ..config import CHAINS, Config
from ..apps import BUILTIN
from ..keys import key_name, parse_key

# Páginas: (contexto, título). Se muestran solo las de las apps que están instaladas.
PAGES: list[tuple[str, str]] = [
    ("menu", "Menú de actividades"),
    ("browse", "Netflix · inicio y fichas"),
    ("player", "Netflix · viendo un título"),
    ("youtube", "YouTube"),
    ("navegador", "Navegador · recorriendo la página"),
    ("navegador_text", "Navegador · escribiendo (los números son letras)"),
    ("navegador_mouse", "Navegador · modo mouse (CC)"),
    ("tidal_nav", "Tidal · recorrer la interfaz (así arranca)"),
    ("tidal", "Tidal · controles (después de 0)"),
    ("videos", "Videos"),
    ("musica", "Música"),
    ("radio", "Radio"),
    ("fotos", "Fotos"),
    ("fotos_zoom", "Fotos · con zoom (▲)"),
    ("android", "Android · la pantalla del celular"),
    ("juegos", "Juegos (MAME)"),
    ("app", "Otras apps"),
    ("offline", "Sin nada abierto"),
    ("joystick", "Joystick (Xbox y otros)"),
]
# Filas que no son teclas, al final de la página de una app (cómo se usa la primera vez). Una línea cada una.
NOTES: dict[str, list[tuple[str, str]]] = {
    "videos": [
        ("Menú › rojo ×2", "Quitar de «Seguir viendo» el video resaltado"),
        ("", "Sin abrirlo · en el celular: la ✕ de «Seguir viendo»"),
        ("Menú › 1", "★ Favorito el video resaltado (Carpetas › ★ Favoritos)"),
        ("Menú › 2–9", "Filtrar la lista como en un teléfono (266 = «amo», «con»…)"),
        ("", "◀ borra un número · 0 saca el filtro · en el celular: «Filtrar»"),
    ],
    "juegos": [
        ("Joystick", "Lo usa el juego (Start, Back = moneda…)"),
        ("Botón del medio", "Nuestro menú, con el juego en pausa"),
        ("", "Seguir · Salir · Opciones de MAME · otro juego"),
        ("Menú › ▶ Juegos", "Elegir otro juego o «Salir del juego»"),
        ("ROMs", "Los .zip de MAME en ~/mame/roms"),
    ],
    "android": [
        ("1.ª vez", "Celular: Ajustes › Acerca del teléfono"),
        ("", "Tocar 7 veces «Número de compilación»"),
        ("", "Opciones de desarrollador › «Depuración USB»"),
        ("USB", "Enchufarlo y aceptar «Permitir depuración USB»"),
        ("Wi‑Fi", "Menú › ▶ sobre Android › Vincular (QR o código)"),
        ("Desconectar", "Menú › OK en Android › «Dejar de compartir»"),
        ("Sonido", "Por la TV, con Android 11 o más nuevo"),
        ("Negro", "Desbloqueá el celular (0 – 9 escriben el PIN)"),
    ],
}

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
    "MUTE": "Silencio", "SUB_PICTURE": "CC", "HELP": "Ayuda", "DISPLAY_INFORMATION": "Info", "DOT": "•", "CLEAR": "Borrar",
    "RECORD": "●",
    **{f"NUMBER{i}": str(i) for i in range(10)},
}
# Orden en que se listan (lo más usado primero).
KEY_ORDER = ["▲", "▼", "◀", "▶", "OK", "Atrás", "⏯", "Play", "Pausa", "Stop", "⏭", "⏮", "⏩", "⏪",
             "Canal +", "Canal −", "Vol +", "Vol −", "Silencio", "CC", "●", "Rojo", "Amarillo", "Azul", "Verde",
             *[str(i) for i in range(10)], "Borrar", "Menú", "•", "Info", "Ayuda"]

# Descripción de cada acción. Las que tienen la misma descripción se agrupan en una fila.
ACTIONS = {
    "nav": "Moverse", "ok": "Abrir / elegir (la 1.ª vez muestra el resaltado)", "back": "Volver · cerrar ficha o menú",
    "smart_skip": "Saltar intro o resumen · siguiente episodio", "continue_watching": "Seguir viendo",
    "profiles": "¿Quién está viendo?", "profile": "Elegir perfil", "home": "Ir al inicio", "my_list": "Mi lista",
    "my_list_toggle": "★ Agregar o quitar de Mi lista (el título resaltado o la ficha)",
    "pause": "Pausa", "mute": "Silencio", "subtitles": "Subtítulos", "audio": "Idioma de audio",
    "next_episode": "Siguiente episodio", "nav_mode": "Recorrer los controles con las flechas",
    "type": "Escribir el número", "fullscreen": "Pantalla completa", "launch": "Abrir Netflix", "reload": "Recargar",
    "help": "Esta ayuda", "toast": "Aviso",
    "menu": "Menú de actividades", "close": "Cerrar y volver al menú",
}
SUB_ACTIONS = {
    "menu": {"": "Abrir el menú (abierto: volver atrás o cerrarlo)", "up": "Moverse", "down": "Moverse", "ok": "Abrir",
             "back": "Volver · cerrar", "close": "Cerrar", "left": "Otra columna · carpeta de arriba",
             "right": "Botón de la fila (Carpetas…) · otra columna", "pick": "Elegir directo", "help": "Ayuda",
             "options": "Opciones: carpetas, radios, conectar, juegos",
             "delete": "Borrar el resaltado (dos veces): un sitio o un «Seguir viendo»"},
    "music": {"play_pause": "Play / pausa", "play": "Play", "pause": "Pausa", "stop": "Pausa", "next": "Tema siguiente",
              "previous": "Tema anterior", "like": "Me gusta (favoritos)", "shuffle": "Aleatorio",
              "my_tracks": "Mis canciones → Colección → Inicio (en ciclo)", "lyrics": "Letras (otra vez: volver)", "back": "Atrás en la app (en la primera pantalla: cerrar)",
              "nav": "Cambia las flechas: recorrer la app ↔ controles (volumen, temas)", "move": "Moverse por la app",
              "tab": "Siguiente elemento (si las flechas se traban)", "info": "Qué suena (tema, artista, disco)"},
    "video": {"play_pause": "Play / pausa", "ok": "Play / pausa (buscando: saltar ahí)", "play": "Play",
              "pause": "Pausa", "stop": "Pausa", "next": "Video siguiente", "previous": "Video anterior",
              "subtitles": "Subtítulos", "audio": "Idioma de audio", "aspect": "Proporción de la imagen",
              "back": "Cerrar y volver al menú (buscando: dejar de buscar)", "info": "Qué es (nombre, por dónde va)",
              "favorite": "★ Guardar el video en Favoritos (otra vez: sacarlo)"},
    "musica": {"play_pause": "Play / pausa", "ok": "Play / pausa (buscando: saltar ahí)", "play": "Play",
               "pause": "Pausa", "stop": "Pausa", "next": "Tema siguiente", "previous": "Tema anterior",
               "shuffle": "Mezclar / volver al orden", "loop": "Repetir: la lista, el tema o nada",
               "back": "Cerrar y volver al menú (buscando: dejar de buscar)", "info": "Qué suena (tema, artista, disco)"},
    "web": {"nav": "Moverse entre los enlaces y botones", "ok": "Abrir (en un campo de texto: escribir ahí)",
            "back": "Volver atrás (en la primera página: cerrar; escribiendo: salir del campo)", "home": "La página de inicio (los sitios)",
            "reload": "Recargar la página", "go": "Escribir una dirección o qué buscar",
            "submit": "Aceptar: ir / buscar (Enter)", "t9": "Escribir: cada número, sus letras (seguido = la siguiente)",
            "key": "Tecla", "clear": "Borrar todo", "scroll": "Página arriba / abajo",
            "play_pause": "Play / pausa del video de la página", "play": "Play", "pause": "Pausa",
            "sites": "Los sitios guardados (ahí, 1 saca el resaltado)", "site": "Ese sitio de la lista",
            "url": "Abrir esa dirección", "save": "Guardar esta página en los sitios",
            "fav": "★ Guardar esta página en los sitios (otra vez: sacarla)"},
    "radio": {"play_pause": "Pausa / seguir (después de un rato: en vivo otra vez)", "ok": "Pausa / seguir",
              "play": "Seguir", "pause": "Pausa", "stop": "Pausa", "next": "Radio siguiente",
              "previous": "Radio anterior", "station": "Esa radio de la lista",
              "info": "Qué suena (la radio y el tema)", "list": "La lista de radios"},
    "fotos": {"next": "Foto siguiente", "previous": "Foto anterior", "pause": "Pausar o seguir la presentación",
              "rotate": "Girar la foto", "random": "Una foto al azar", "info": "Qué foto es (otra vez: dónde se sacó, en un mapa)",
              "filename": "Nombre del archivo (chico)", "zoom_in": "Zoom: acercar", "zoom_out": "Alejar",
              "fit": "Foto entera (y sigue la presentación)", "scroll": "Mover la foto",
              "media_play_pause": "Con música: play / pausa · sin música: pausar la presentación",
              "media_next": "Foto siguiente",
              "media_previous": "Foto anterior",
              "mode": "Cambiar: solo fotos → fotos y videos → solo videos",
              "map": "Dónde se sacó la foto (mapa, si tiene GPS)",
              "date": "La fecha en una esquina (prender / apagar)",
              "favorite": "Guardar la foto en Favoritas (otra vez: sacarla)"},
    "android": {"back": "Atrás (botón de Android)", "home": "Inicio de Android", "apps": "Apps recientes",
                "notifications": "Notificaciones (otra vez: cerrarlas)", "menu": "Menú de la app",
                "play_pause": "Play / pausa", "play": "Play", "pause": "Pausa", "stop": "Stop",
                "next": "Siguiente", "previous": "Anterior", "fast_forward": "Adelantar", "rewind": "Atrasar",
                "volume_up": "Subir volumen del celular", "volume_down": "Bajar volumen del celular",
                "mute": "Silencio en el celular", "power": "Botón de encendido del celular",
                "wakeup": "Prender la pantalla del celular",
                "list": "Celulares · «Dejar de compartir la pantalla»",
                "disconnect": "Dejar de compartir la pantalla"},
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
        return {"play_pause": "Play / pausa", "play": "Play", "pause": "Pausa", "next": "Video siguiente",
                "previous": "Video anterior", "home": "Inicio de YouTube", "subtitles": "Subtítulos",
                "like": "★ Me gusta en el video (otra vez: sacarlo)"}.get(sub, str(action))
    if name == "android" and sub == "key":
        if rest.isdigit():
            return "Escribir el número (p. ej. el PIN)"
        return {"Up": "Moverse", "Down": "Moverse", "Left": "Moverse", "Right": "Moverse", "Return": "Elegir / OK",
                "BackSpace": "Borrar", "Escape": "Atrás"}.get(rest, f"Tecla {rest}")
    if name == "juegos":
        return "Salir del juego" if sub in ("quit", "salir") else "Lista de juegos (OK sobre el actual: seguir)"
    if name == "android" and sub == "keyevent":
        return f"Tecla de Android {rest}"
    if name == "web" and sub == "key":
        return {"Backspace": "Borrar una letra", "ArrowLeft": "Mover el cursor", "ArrowRight": "Mover el cursor",
                "Enter": "Enter"}.get(rest, f"Tecla {rest}")
    if name == "web" and sub == "mouse":
        return {"up": "Mover la flecha (seguido: más rápido; en el borde corre la página)",
                "down": "Mover la flecha (seguido: más rápido; en el borde corre la página)",
                "left": "Mover la flecha (seguido: más rápido; en el borde corre la página)",
                "right": "Mover la flecha (seguido: más rápido; en el borde corre la página)",
                "click": "Clic donde está la flecha", "off": "Salir del modo mouse"}.get(
                    rest.lower(), "Modo mouse: las flechas mueven una flecha, OK hace clic")
    if name == "web" and sub == "t9" and rest.lower() == "mode":
        return "Mayúsculas / minúsculas / números (abc → ABC → 123)"
    if name in SUB_ACTIONS:
        if name == "music" and sub == "page":
            return {"inicio": "Ir a Inicio", "explorar": "Ir a Explorar"}.get(rest, "Ir a Colección")
        if name == "music" and sub in ("volume", "seek"):
            return _plus_minus(sub, rest or "+")
        if name in ("video", "musica", "radio") and sub in ("volume", "seek"):
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
    amount = f"{int(sec) // 60} min" if sec.isdigit() and int(sec) >= 60 and int(sec) % 60 == 0 else f"{sec} s"
    return f"Adelantar {amount}" if not arg.startswith("-") else f"Atrasar {amount}"


def _keys_text(labels: list[str]) -> str:
    """«1 2 3 4 5» → «1 – 5»; el resto separado por espacios."""
    nums = sorted(int(x) for x in labels if x.isdigit())
    other = [x for x in labels if not x.isdigit()]
    if len(nums) > 2 and nums == list(range(nums[0], nums[-1] + 1)):
        other.append(f"{nums[0]} – {nums[-1]}")
    else:
        other += [str(n) for n in nums]
    return "  ".join(other)


def _label(key: str) -> str:
    code = parse_key(key) if key else None
    return KEY_LABELS.get(key_name(code), key_name(code)) if code is not None else key


def joystick_rows(cfg: Config) -> list[tuple[str, str]]:
    """Botón del joystick → la tecla del control que equivale ([gamepad.buttons])."""
    from ..system import gamepad  # noqa: PLC0415
    keys = gamepad.button_keys(cfg.raw.get("gamepad"))
    out: list[tuple[str, str]] = []
    for prefix, what in (("DPAD_", "Cruz y palanca izq."), ("RS_", "Palanca derecha")):
        dirs = [_label(keys.get(prefix + d, "")) for d in ("UP", "DOWN", "LEFT", "RIGHT")]
        if any(dirs):
            out.append((what, "como " + " ".join(d for d in dirs if d) + " del control"))
    for btn, label in gamepad.BUTTON_LABELS:
        if keys.get(btn):
            out.append((label, f"como {_label(keys[btn])} del control"))
    only = [str(c) for c in (cfg.raw.get("gamepad") or {}).get("menu_only_in", ["juegos", "app"]) or []]
    if only:
        out.append(("En los juegos", "el joystick es del juego, salvo el del medio"))
    return out


def rows(cfg: Config, ctx: str) -> list[tuple[str, str]]:
    """Filas (teclas, qué hacen) de un contexto, según la configuración (con su cadena de búsqueda)."""
    if ctx == "joystick":
        return joystick_rows(cfg)
    codes: list[int] = []
    for c in CHAINS.get(ctx, (ctx, "global")):
        for code in cfg.bindings.get(c, {}):
            if code not in codes:
                codes.append(code)
    hidden = {parse_key(str(k)) for k in (cfg.raw.get("help") or {}).get("hide_keys", [])}
    groups: dict[str, list[str]] = {}
    for code in codes:
        if code in hidden:
            continue  # el control no la tiene ([help] hide_keys)
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
    extra = []
    timer_key = str((cfg.raw.get("power") or {}).get("timer_key", "DOT") or "")
    if timer_key and parse_key(timer_key) is not None:
        label = KEY_LABELS.get(key_name(parse_key(timer_key)), timer_key)
        extra.append((f"{label} {label} (dos veces)", "Temporizador para apagar (otra vez: cambiarlo)"))
    return [(k, d) for _o, k, d in out] + extra + NOTES.get(ctx, [])


def pages(cfg: Config, installed: set[str]) -> list[tuple[str, str]]:
    """Páginas que tienen sentido: las de las apps instaladas (installed = ids de actividades)."""
    needs = {"browse": "netflix", "player": "netflix", "youtube": "youtube", "tidal": "tidal", "tidal_nav": "tidal",
             "navegador": "navegador", "navegador_text": "navegador", "navegador_mouse": "navegador",
             "videos": "videos", "musica": "musica", "radio": "radio", "fotos": "fotos", "fotos_zoom": "fotos", "android": "android", "juegos": "juegos"}
    out = []
    for ctx, title in PAGES:
        if ctx in needs and needs[ctx] not in installed:
            continue
        if ctx == "app" and not any(i not in BUILTIN for i in installed):
            continue
        if ctx == "joystick" and not (cfg.raw.get("gamepad") or {}).get("enabled", True):
            continue
        out.append((ctx, title))
    return out


def page_for(ctx: str) -> str:
    return PAGE_FOR_CONTEXT.get(ctx, ctx)


__all__ = ["pages", "rows", "page_for", "describe"]
