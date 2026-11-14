"""Lo escribe tele-cec al abrir High Tide (no editar: se reescribe cada vez).

1. Hace que las tarjetas de High Tide (portadas de discos, playlists, artistas) puedan recibir el foco del
   teclado y abrirse con Enter, igual que con un clic.
2. Abre un canal de control (control.sock, al lado de este archivo) para que tele-cec cambie de pantalla
   (Inicio, Colección, Mis canciones, Atrás, Letras…) llamando a las mismas funciones que los botones, sin
   pasar por la accesibilidad (consultarla mientras High Tide dibuja puede cerrarlo: error de GTK 4).
3. Por el mismo canal, la página del celular lee la biblioteca (Mis canciones, playlists, mixes, discos,
   artistas), busca, ve la cola y pone a sonar lo que se elija (respuestas en JSON, en una línea).
Si algo falla, High Tide sigue como siempre.
"""
import importlib.abc
import importlib.util
import json
import re
import os
import socket
import sys
import threading

_SOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control.sock")
_MY_TRACKS = "tele-my-tracks"  # etiqueta de la página «Mis canciones» que abre tele-cec
_window = None


def _log(*args):
    print("tele-cec:", *args, file=sys.stderr, flush=True)


def _patch_cards(module):
    cls = module.HTCardWidget
    original_init = cls.__init__

    def __init__(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            # La tarjeta no toma el foco ella misma (se lo pasa a lo de adentro): lo recibe la portada, y el
            # Enter lo atiende la ventana (ver _on_window_key). El link del artista de adentro queda sin foco:
            # si no, OK abriría el artista en vez del disco (se llega desde la página del disco).
            image = getattr(self, "image", None)
            if image is not None:
                image.set_focusable(True)
            for name in ("track_artist_label", "detail_label", "title_label"):
                widget = getattr(self, name, None)
                if widget is not None:
                    widget.set_can_focus(False)
        except Exception as exc:  # noqa: BLE001
            _log("no pude hacer enfocable una tarjeta:", exc)

    cls.__init__ = __init__
    _log("tarjetas de High Tide enfocables (Enter = abrir)")


# ------------------------------------------------------------------ teclado: flechas y Enter
# GTK mueve el foco con las flechas solo dentro de cada bloque (una fila de portadas, una lista), así que hay
# lugares a los que no se llega. Acá las flechas van al elemento enfocable más cercano en esa dirección, mirando
# dónde está cada uno en la ventana (como en una TV). Y Enter abre lo que High Tide solo abre con un clic.
_ARROWS = {}


def _rect(widget, window):
    ok, r = widget.compute_bounds(window)
    if not ok or r.size.width < 2 or r.size.height < 2:
        return None
    return (r.origin.x, r.origin.y, r.origin.x + r.size.width, r.origin.y + r.size.height)


def _region(widget, window):
    """El panel con desplazamiento más externo que lo contiene (la página, el panel del reproductor…)."""
    from gi.repository import Gtk
    found, w = None, widget.get_parent()
    while w is not None and w is not window:
        if isinstance(w, Gtk.ScrolledWindow):
            found = w
        w = w.get_parent()
    return found


def _candidates(window):
    """Lo que se puede elegir: widgets enfocables visibles, sin entrar en los que ya lo son (una fila de la
    lista se elige entera, no el link del artista de adentro)."""
    from gi.repository import Gtk
    # Contenedores que se declaran enfocables pero lo que se elige es lo de adentro.
    containers = (Gtk.Window, Gtk.ScrolledWindow, Gtk.Viewport, Gtk.ListBox, Gtk.FlowBox, Gtk.ListView,
                  Gtk.GridView, Gtk.ColumnView, Gtk.Stack)
    out, stack = [], [window.get_first_child()]
    while stack:
        w = stack.pop()
        while w is not None:
            # Los botones de la ventana (minimizar, cerrar) no: OK cerraría High Tide.
            if w.get_mapped() and w.is_sensitive() and not isinstance(w, Gtk.WindowControls):
                inner = w.get_child() if isinstance(w, Gtk.FlowBoxChild) else None
                if inner is not None and inner.get_focusable():
                    # Casilla de grilla que envuelve un botón (Explorar: géneros, décadas…): el foco va al botón,
                    # que es lo que se aprieta con OK (la casilla solo se seleccionaría).
                    stack.append(inner)
                elif w.get_focusable() and w.get_can_focus() and not isinstance(w, containers):
                    out.append(w)
                else:
                    stack.append(w.get_first_child())
            w = w.get_next_sibling()
    return out


def _score(direction, cur, r):
    cx, cy = (cur[0] + cur[2]) / 2, (cur[1] + cur[3]) / 2
    rx, ry = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    if direction in ("up", "down"):
        ahead = ry > cy + 2 if direction == "down" else ry < cy - 2
        gap = max(0.0, r[1] - cur[3]) if direction == "down" else max(0.0, cur[1] - r[3])
        side = max(0.0, r[0] - cur[2], cur[0] - r[2])
        off = abs(rx - cx)
    else:
        ahead = rx > cx + 2 if direction == "right" else rx < cx - 2
        gap = max(0.0, r[0] - cur[2]) if direction == "right" else max(0.0, cur[0] - r[2])
        side = max(0.0, r[1] - cur[3], cur[1] - r[3])
        off = abs(ry - cy)
    if not ahead:
        return None
    return gap + 4 * side + 0.05 * off


def _move_focus(window, direction):
    from gi.repository import Gtk
    focus = window.get_focus()
    if focus is not None and focus.get_native() is not window:
        return False  # un menú o un popup: que lo maneje GTK
    if isinstance(focus, Gtk.Text) and direction in ("left", "right"):
        return False  # escribiendo (buscar): las flechas mueven el cursor
    items = _candidates(window)
    cur = _rect(focus, window) if focus is not None else None
    if cur is None:
        # Sin foco (recién abierto, o después de abrir otra página): el primer elemento de la página, arriba a
        # la izquierda; no el panel del reproductor.
        page = getattr(window, "navigation_view", None)
        # Lo que se desplaza en la página: sin la barra de arriba (el menú ☰ tiene «Salir»).
        inside = [w for w in items if page is not None and w.is_ancestor(page) and _region(w, window)]
        rects = [(r, w) for w in (inside or items) for r in [_rect(w, window)] if r]
        if not rects:
            return False
        target = min(rects, key=lambda x: (x[0][1] // 40, x[0][0]))[1]
    else:
        region = _region(focus, window)
        best = {True: None, False: None}  # misma región / cualquiera
        for w in items:
            if w is focus:
                continue
            r = _rect(w, window)
            if r is None:
                continue
            sc = _score(direction, cur, r)
            if sc is None:
                continue
            same = region is not None and _region(w, window) is region
            for key in ((True, False) if same else (False,)):
                if best[key] is None or sc < best[key][0]:
                    best[key] = (sc, w)
        # Primero dentro del mismo panel (si no, desde el final de la lista se iría a la barra de abajo
        # antes que al resto de la lista, que está fuera de la pantalla).
        pick = best[True] or best[False]
        if pick is None:
            return True  # nada en esa dirección: no hacer nada (antes que mover la canción o el volumen)
        target = pick[1]
    window.set_focus_visible(True)
    target.grab_focus()
    return True


def _activate(window):
    """Enter sobre algo que High Tide solo abre con un clic (portadas, atajos de Inicio): lo abre."""
    from gi.repository import Gtk
    focus = window.get_focus()
    if focus is None or isinstance(focus, (Gtk.Button, Gtk.Label, Gtk.Text, Gtk.ListBoxRow)):
        return False
    # La tarjeta puede estar arriba (el foco está en su portada) o adentro (el foco está en la casilla de una
    # grilla, que la envuelve).
    near, w = [focus.get_first_child()], focus
    for _ in range(8):
        if w is None:
            break
        near.append(w)
        w = w.get_parent()
    for w in near:
        if w is not None and callable(getattr(w, "_on_click", None)) and hasattr(w, "item"):
            w._on_click()
            return True
    return False


def _on_window_key(controller, keyval, _keycode, state):
    from gi.repository import Gdk
    window = controller.get_widget()
    if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK):
        return False
    try:
        if not _ARROWS:
            _ARROWS.update({Gdk.KEY_Up: "up", Gdk.KEY_Down: "down", Gdk.KEY_Left: "left", Gdk.KEY_Right: "right"})
        if keyval in _ARROWS:
            return _move_focus(window, _ARROWS[keyval])
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return _activate(window)
    except Exception as exc:  # noqa: BLE001
        _log("teclado:", exc)
    return False


# ------------------------------------------------------------------ canal de control
def _tag():
    page = _window.navigation_view.get_visible_page()
    return (page.get_tag() or "") if page is not None else ""


def _go(tag, handler):
    if _window.navigation_view.find_page(tag):
        _window.navigation_view.pop_to_tag(tag)
    else:
        handler(None)
    return tag


def _home():
    return _go("home", _window.on_home_button_clicked_func)


def _explore():
    return _go("explore", _window.on_explore_button_clicked_func)


def _collection():
    return _go("collection", _window.on_collection_button_clicked_func)


def _my_tracks(start="-1"):
    """Colección → «Canciones» (Mis canciones) y, con start >= 0, reproducirlas desde ese tema."""
    from gettext import gettext as _
    from high_tide.lib import utils
    from high_tide.pages import HTFromFunctionPage
    tracks = list(utils.favourite_tracks or [])
    # Recién abierto: todavía no entró a la cuenta o no cargó los favoritos. Al entrar, High Tide reemplaza
    # todas las páginas por Inicio, así que hay que esperar a eso (tele-cec reintenta).
    if not tracks or not getattr(_window, "is_logged_in", False):
        return "empty"
    nav = _window.navigation_view
    if nav.find_page(_MY_TRACKS):
        nav.pop_to_tag(_MY_TRACKS)
    else:
        _collection()
        page = HTFromFunctionPage(_("Tracks"))
        page.set_tag(_MY_TRACKS)
        page.set_items(tracks)
        page.load()
        nav.push(page)
    start = int(start)
    if start >= 0:
        utils.player_object.play_this(tracks, min(start, len(tracks) - 1))
    return "my_tracks"


def _cycle(start="0"):
    """Amarillo: Mis canciones → Colección → Inicio → Mis canciones."""
    tag = _tag()
    if tag == _MY_TRACKS:
        return _collection()
    if tag == "collection":
        return _home()
    return _my_tracks(start)


def _back():
    """Atrás: si hay un menú desplegado (☰, ⋮ de un tema), lo cierra; si no, la página anterior."""
    from gi.repository import Gtk
    w = _window.get_focus()
    while w is not None:
        if isinstance(w, Gtk.Popover):
            w.popdown()
            return "ok"
        w = w.get_parent()
    return "ok" if _window.navigation_view.pop() else "no"


def _show():
    """Mostrar la ventana y pasarla adelante (por si quedó escondida o detrás de otra)."""
    _window.set_visible(True)
    _window.present()
    return "ok"


def _like():
    _window.on_in_my_collection_button_clicked(_window.in_my_collection_button)
    return "ok"


def _shuffle():
    b = _window.shuffle_button
    b.set_active(not b.get_active())
    return "on" if b.get_active() else "off"


def _lyrics():
    stack = _window.sidebar_stack
    stack.set_visible_child_name("player" if stack.get_visible_child_name() == "lyrics" else "lyrics")
    return stack.get_visible_child_name()


# ------------------------------------------------------------------ biblioteca, búsqueda y cola (página del celular)
_PAGE_MAX = 200
_lists = {}  # (tipo, id) -> temas, para pedirlos de a páginas sin volver a bajarlos
_my_mixes = [0.0, []]
_recent_cache = [0.0, "", [], []]  # [cuándo, título, cosas, módulos del Inicio]
_my_playlists = [0.0, [], []]  # [cuándo, mías, favoritas] (se piden cada 10 min: recorrer carpetas tarda)
# High Tide, al entrar a la cuenta, retoma lo último que sonaba en un hilo (y lo pausa): si se pide algo antes de
# que termine, lo pisa. Se espera a esto antes de poner música.
_restored = threading.Event()  # [cuándo, lista]: los mixes que arma Tidal para la cuenta (se piden cada 15 min)


def _img(obj, sizes=(320, 160, 480, 640)):
    """La dirección de la imagen en el servidor de Tidal (se arma con el id: no baja nada)."""
    if obj is None:
        return ""
    for size in sizes:
        try:
            return obj.image(size)
        except Exception:  # noqa: BLE001  (tamaño no válido para ese tipo, o sin imagen)
            continue
    return ""


def _artists(track):
    names = [a.name for a in (getattr(track, "artists", None) or []) if getattr(a, "name", None)]
    if not names and getattr(track, "artist", None) is not None:
        names = [track.artist.name]
    return ", ".join(names)


def _item(obj):
    """Un tema, disco, playlist, mix o artista como diccionario para la página."""
    from tidalapi import Album, Artist, Mix, Playlist, Track
    try:
        from tidalapi.mix import MixV2
    except ImportError:  # tidalapi viejo
        MixV2 = Mix
    if isinstance(obj, Track):
        album = getattr(obj, "album", None)
        return {"type": "track", "id": str(obj.id), "title": obj.name or "", "sub": _artists(obj),
                "album": getattr(album, "name", "") or "", "dur": int(getattr(obj, "duration", 0) or 0),
                "img": _img(album, (160, 320)), "ok": bool(getattr(obj, "available", True))}
    if isinstance(obj, Album):
        artist = getattr(obj, "artist", None)
        return {"type": "album", "id": str(obj.id), "title": obj.name or "",
                "sub": getattr(artist, "name", "") or "", "img": _img(obj, (160, 320))}
    if isinstance(obj, Playlist):
        n = getattr(obj, "num_tracks", None)
        creator = getattr(obj, "creator", None)
        by = getattr(creator, "name", "") or ""
        if by and str(getattr(creator, "id", "")) == str(getattr(_user(), "id", "-")):
            by = ""  # una mía: no hace falta decir de quién es
        sub = " · ".join(x for x in (f"{n} temas" if n else "", by) if x)
        return {"type": "playlist", "id": str(obj.id), "title": obj.name or "", "sub": sub,
                "img": _img(obj, (320, 160, 480))}
    if isinstance(obj, (Mix, MixV2)):
        return {"type": "mix", "id": str(obj.id), "title": getattr(obj, "title", "") or "",
                "sub": getattr(obj, "sub_title", "") or "", "img": _img(obj, (320, 640))}
    if isinstance(obj, Artist):
        return {"type": "artist", "id": str(obj.id), "title": obj.name or "", "sub": "Artista",
                "img": _img(obj, (320, 160, 480))}
    return None


def _items(objs):
    return [x for x in (_item(o) for o in (objs or [])) if x]


def _need_login():
    if _window is None or not getattr(_window, "is_logged_in", False):
        raise RuntimeError("login")


def _lib(rest):
    """lib <tracks|playlists|mixes|albums|artists> [desde] [cuántos]: lo guardado en la cuenta."""
    from high_tide.lib import utils
    _need_login()
    what, *nums = rest.split()
    start, count = (int(nums[0]) if nums else 0), min(int(nums[1]) if len(nums) > 1 else 100, _PAGE_MAX)
    if what == "recent":
        title, source, modules = _recent()
        page = _items(source[start:start + count])
        return {"items": page, "total": len(source), "start": start, "title": title, "modules": modules}
    if what in ("playlists", "favplaylists"):
        mine, favs = _collection_playlists()
        source = mine if what == "playlists" else favs
    else:
        source = {"tracks": utils.favourite_tracks, "mixes": utils.favourite_mixes, "albums": utils.favourite_albums,
                  "artists": utils.favourite_artists}.get(what)
        if source is None:
            raise ValueError("biblioteca desconocida: " + what)
    source = list(source or [])
    if what == "mixes":
        source += [m for m in _account_mixes() if all(str(m.id) != str(f.id) for f in source)]
    return {"items": _items(source[start:start + count]), "total": len(source), "start": start}


def _user():
    try:
        from high_tide.lib import utils
        return utils.session.user
    except Exception:  # noqa: BLE001
        return None


def _pages(fetch, most=1000):
    """Pide de a 50 (lo máximo que da Tidal) hasta que una página venga corta."""
    out, offset = [], 0
    while offset < most:
        page = list(fetch(offset) or [])
        out += page
        offset += 50
        if len(page) < 50:
            break
    return out


def _collection_playlists():
    """(mías, favoritas): las playlists de la colección, también las que están dentro de carpetas.

    High Tide (y tidalapi) solo miran las que están sueltas en la raíz de la colección, y de la lista
    «mías + favoritas» piden una sola página: las favoritas guardadas en carpetas no aparecían.
    """
    import time
    from tidalapi.playlist import UserPlaylist
    from high_tide.lib import utils
    if time.monotonic() - _my_playlists[0] < 600 and (_my_playlists[1] or _my_playlists[2]):
        return list(_my_playlists[1]), list(_my_playlists[2])
    user = utils.session.user
    fav = user.favorites
    found = []

    def step(what, fn):
        try:
            found.extend(fn())
        except Exception as exc:  # noqa: BLE001  (una parte que falla no deja sin las demás)
            _log("playlists:", what, exc)

    step("mías", lambda: list(user.playlists() or []))
    step("raíz", lambda: _pages(lambda o: fav.playlists(limit=50, offset=o)))
    step("mías y favoritas", lambda: _pages(lambda o: user.playlist_and_favorite_playlists(offset=o, limit=50)))

    def folders(parent, depth):
        for folder in _pages(lambda o: fav.playlist_folders(limit=50, offset=o, parent_folder_id=parent)):
            step("carpeta " + str(getattr(folder, "name", "")), lambda f=folder: _pages(lambda o: f.items(offset=o)))
            if depth < 3:
                folders(folder.id, depth + 1)
    step("carpetas", lambda: folders("root", 0) or [])

    seen, mine, favs = set(), [], []
    for pl in found:
        pid = str(getattr(pl, "id", "") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        creator = getattr(pl, "creator", None)
        own = isinstance(pl, UserPlaylist) or str(getattr(creator, "id", "")) == str(user.id)
        (mine if own else favs).append(pl)
    _log(f"playlists: {len(mine)} mías, {len(favs)} favoritas")
    _my_playlists[:] = [time.monotonic(), mine, favs]
    return list(mine), list(favs)


_RECENT_RE = re.compile(r"recently|reciente|escuchad", re.I)


def _recent():
    """(título, cosas, módulos vistos): «Escuchado recientemente» del Inicio de Tidal (playlists, mixes, discos…).

    Si el módulo tiene «Ver todo», se trae la lista completa. Se guarda 2 minutos.
    """
    import time
    from high_tide.lib import utils
    if time.monotonic() - _recent_cache[0] < 120 and _recent_cache[2]:
        return _recent_cache[1], list(_recent_cache[2]), _recent_cache[3]
    session = utils.session
    raw = session.request.request("GET", "home/feed/static", base_url=session.config.api_v2_location,
                                  params={"deviceType": "BROWSER", "locale": session.locale,
                                          "platform": "WEB"}).json()
    modules = [m for m in raw.get("items", []) if isinstance(m, dict)]
    seen = [f"{m.get('title') or ''} ({m.get('type')}, {m.get('moduleId') or ''})" for m in modules]
    # «Recently played» (CONTINUE_LISTEN_TO); si Tidal le cambia el id, por el título (no «Your listening history»,
    # que son los mixes de cada mes).
    module = next((m for m in modules if m.get("moduleId") == "CONTINUE_LISTEN_TO"), None) or next(
        (m for m in modules if _RECENT_RE.search(str(m.get("title") or ""))), None)
    if module is None:
        _log("recientes: no encontré el módulo entre", seen)
        return "", [], seen
    cat = session.page.page_category_v2.parse_item(module)
    items = [x for x in (getattr(cat, "items", None) or []) if x is not None]
    path = module.get("viewAll") or (module.get("showMore") or {}).get("apiPath")
    if path and callable(getattr(cat, "get_item", None)):
        try:  # «Ver todo» (tidalapi no lo trae para el Inicio nuevo): la misma lista, más larga
            more = []
            for offset in range(0, 200, 50):
                j = session.request.request("GET", path, base_url=session.config.api_v2_location,
                                            params={"deviceType": "BROWSER", "locale": session.locale,
                                                    "platform": "WEB", "limit": 50, "offset": offset}).json()
                page = [it for it in (j.get("items") or []) if isinstance(it, dict)]
                for it in page:
                    try:
                        x = cat.get_item(it)
                    except Exception:  # noqa: BLE001  (una cosa rara no tira la lista)
                        x = None
                    if x is not None:
                        more.append(x)
                if len(page) < 50:
                    break
            if len(more) > len(items):
                items = more
        except Exception as exc:  # noqa: BLE001  (sin «Ver todo», lo del Inicio alcanza)
            _log("recientes: ver todo:", path, " ".join(str(exc).split())[:200])
    title = str(module.get("title") or "Recientes")
    _recent_cache[:] = [time.monotonic(), title, items, seen]
    return title, list(items), seen


def _diag(_rest=""):
    """diag: de qué cuenta es la sesión y qué devuelve cada fuente de playlists (para revisar si falta alguna)."""
    from high_tide.lib import utils
    _need_login()
    session, user = utils.session, utils.session.user
    out = {"user": {k: str(getattr(user, k, "") or "") for k in ("id", "username", "first_name", "last_name",
                                                                 "email", "profile_name")},
           "country": str(getattr(session, "country_code", "") or "")}

    def names(items):
        return [f"{getattr(p, 'name', '?')} [{type(p).__name__}, de {getattr(getattr(p, 'creator', None), 'name', '')}]"
                for p in items]

    def raw(url, base=None, **params):
        r = session.request.request("GET", url, base_url=base, params=params or None)
        j = r.json()
        items = j.get("items", []) if isinstance(j, dict) else []
        return {"status": r.status_code, "total": j.get("totalNumberOfItems") if isinstance(j, dict) else None,
                "items": [((it.get("item") or it.get("data") or it.get("playlist") or it) if isinstance(it, dict)
                           else it) for it in items][:80]}

    tries = {
        "v1 favorites/playlists": lambda: raw(f"users/{user.id}/favorites/playlists", limit=50),
        "v1 playlistsAndFavoritePlaylists": lambda: raw(f"users/{user.id}/playlistsAndFavoritePlaylists", limit=50),
        "v2 folders root": lambda: raw("my-collection/playlists/folders", session.config.api_v2_location,
                                       folderId="root", offset=0, limit=50, includeOnly="", order="DATE",
                                       orderDirection="DESC"),
        "v2 flattened": lambda: raw("my-collection/playlists/folders/flattened", session.config.api_v2_location,
                                    offset=0, limit=50, includeOnly="PLAYLIST", order="DATE", orderDirection="DESC"),
    }
    for name, fn in tries.items():
        try:
            r = fn()
            r["names"] = [str(it.get("title") or it.get("name") or "?") + " · " + str(
                (it.get("creator") or {}).get("name") or (it.get("creator") or {}).get("id") or it.get("type") or "")
                for it in r.pop("items") if isinstance(it, dict)]
            out[name] = r
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": " ".join(str(exc).split())[:300]}
    try:
        out["high_tide"] = {"favourite_playlists": names(utils.favourite_playlists or []),
                            "user_playlists": names(getattr(utils, "user_playlists", []) or [])}
    except Exception as exc:  # noqa: BLE001
        out["high_tide"] = {"error": str(exc)}
    return out


def _account_mixes():
    """«Mis mixes»: los que arma Tidal para la cuenta (Mi mix 1, Descubrimientos semanales…)."""
    import time
    from high_tide.lib import utils
    if time.monotonic() - _my_mixes[0] > 900 or not _my_mixes[1]:
        try:
            _my_mixes[1] = [m for m in utils.session.mixes() if hasattr(m, "id")]
        except Exception as exc:  # noqa: BLE001
            _log("no pude leer los mixes de la cuenta:", exc)
        _my_mixes[0] = time.monotonic()
    return list(_my_mixes[1])


def _fetch(kind, ident):
    """Los temas de un disco, playlist, mix o artista (o Mis canciones), y su nombre."""
    from tidalapi import Track
    from high_tide.lib import utils
    if kind == "fav":
        return list(utils.favourite_tracks or []), "Mis canciones", None
    key = (kind, ident)
    if kind == "album":
        obj = utils.get_album(ident)
        tracks = _lists.get(key) or list(obj.tracks())
        title = obj.name
    elif kind == "playlist":
        obj = utils.get_playlist(ident)
        tracks = _lists.get(key)
        if tracks is None:
            tracks, offset = [], 0
            while offset < 2000:  # las playlists se piden de a 100
                page = list(obj.tracks(limit=100, offset=offset))
                tracks += page
                offset += 100
                if len(page) < 100:
                    break
        title = obj.name
    elif kind == "mix":
        obj = utils.get_mix(ident)
        tracks = _lists.get(key) or [t for t in obj.items() if isinstance(t, Track)]
        title = getattr(obj, "title", "") or "Mix"
    elif kind == "artist":
        obj = utils.get_artist(ident)
        tracks = _lists.get(key) or list(obj.get_top_tracks(limit=50))
        title = obj.name
    elif kind == "track":
        obj = utils.get_track(ident)
        return [obj], obj.name, obj
    else:
        raise ValueError("tipo desconocido: " + kind)
    _lists[key] = tracks
    return tracks, title, obj


def _list(rest):
    """list <fav|album|playlist|mix|artist> <id> [desde] [cuántos]: los temas de eso."""
    _need_login()
    parts = rest.split()
    if len(parts) < 2:
        raise ValueError("falta el id (para Mis canciones: fav -)")
    kind, ident, nums = parts[0], parts[1], parts[2:]
    start, count = (int(nums[0]) if nums else 0), min(int(nums[1]) if len(nums) > 1 else 100, _PAGE_MAX)
    tracks, title, obj = _fetch(kind, ident)
    out = {"title": title, "items": _items(tracks[start:start + count]), "total": len(tracks), "start": start}
    if obj is not None and kind != "track":
        out["img"] = _img(obj, (320, 640, 480))
    return out


def _search(rest):
    """search <texto>: artistas, discos, playlists y temas (lo mismo que la búsqueda de High Tide)."""
    from tidalapi import Album, Artist, Playlist, Track
    from high_tide.lib import utils
    _need_login()
    query = rest.strip()[:120]
    if not query:
        return {"tracks": [], "albums": [], "playlists": [], "artists": [], "top": None}
    r = utils.session.search(query, [Artist, Album, Playlist, Track], 20)
    return {"top": _item(r.get("top_hit")), "tracks": _items(r.get("tracks")), "albums": _items(r.get("albums")),
            "playlists": _items(r.get("playlists")), "artists": _items(r.get("artists"))}


def _on_gtk(fn):
    """Corre fn en el hilo de GTK y espera (lo que toca el reproductor o la ventana)."""
    from gi.repository import GLib
    done, out = threading.Event(), [None]

    def call():
        try:
            out[0] = fn()
        except Exception as exc:  # noqa: BLE001
            out[0] = exc
        done.set()
        return False

    GLib.idle_add(call)
    if not done.wait(8):
        raise TimeoutError("High Tide no contestó")
    if isinstance(out[0], Exception):
        raise out[0]
    return out[0]


def _play(rest):
    """play <fav|album|playlist|mix|artist|track> <id> [n.º de tema]: lo pone a sonar desde ese tema."""
    from high_tide.lib import utils
    _need_login()
    parts = rest.split()
    kind, ident = parts[0], (parts[1] if len(parts) > 1 else "-")
    index = int(parts[2]) if len(parts) > 2 else 0
    tracks, title, obj = _fetch(kind, ident)  # bajar la lista acá, no en el hilo de GTK
    _restored.wait(20)
    if not tracks:
        raise ValueError("no tiene temas")
    index = max(0, min(index, len(tracks) - 1))

    def go():
        player = utils.player_object
        player.play_this(list(tracks), index)
        player.current_mix_album_playlist = obj if obj is not None and kind != "track" else list(tracks)
        return {"ok": True, "title": title}
    return _on_gtk(go)


def _enqueue(rest):
    """enqueue <id de tema> <next|end>: después de este tema, o al final de la cola."""
    from high_tide.lib import utils
    _need_login()
    ident, *mode = rest.split()
    track = utils.get_track(ident)
    _restored.wait(20)

    def go():
        if mode and mode[0] == "next":
            utils.player_object.add_next(track)
        else:
            utils.player_object.add_to_queue(track)
        return {"ok": True, "title": track.name}
    return _on_gtk(go)


def _upcoming(player):
    return player._shuffled_tracks_to_play if player.shuffle else player._tracks_to_play


def _queue(rest=""):
    """queue: el tema actual, la cola (lo agregado a mano) y lo que sigue de la lista."""
    from high_tide.lib import utils
    player = utils.player_object
    if player is None:
        return {"current": None, "queue": [], "next": [], "total_next": 0}
    upcoming = list(_upcoming(player) or [])
    ctx = player.current_mix_album_playlist
    ctx_title = getattr(ctx, "name", None) or getattr(ctx, "title", None) or ""
    current = player.playing_track
    return {"current": _item(current) if current is not None else None,
            "liked": bool(current is not None and utils.is_favourited(current)),
            "queue": _items(player.queue[:_PAGE_MAX]), "next": _items(upcoming[:100]),
            "total_next": len(upcoming), "context": ctx_title}


def _jump(rest):
    """jump <queue|next> <n>: saltar a ese tema de la cola o de lo que sigue (los anteriores se saltean)."""
    from high_tide.lib import utils
    where, n = rest.split()[:2]
    n = int(n)

    def go():
        player = utils.player_object
        lst = player.queue if where == "queue" else _upcoming(player)
        if not 0 <= n < len(lst):
            raise ValueError("ese tema ya no está")
        if player.playing_track is not None:
            player.played_songs.append(player.playing_track)
        skipped = lst[:n]
        del lst[:n]
        if where == "next":
            player.played_songs.extend(skipped)
        track = lst.pop(0)
        player.next_track = None  # que no siga con el que tenía preparado para empalmar
        player.playing = True
        player.play_track(track)
        return {"ok": True, "title": track.name}
    return _on_gtk(go)


def _liked(rest=""):
    from high_tide.lib import utils
    player = utils.player_object
    current = getattr(player, "playing_track", None)
    return {"liked": bool(current is not None and utils.is_favourited(current)),
            "id": str(current.id) if current is not None else ""}


def _ready(rest=""):
    """ready: contesta bien recién cuando entró a la cuenta (antes: error «login»)."""
    _need_login()
    return {"ok": True, "restored": _restored.is_set()}


# Comandos de datos: corren en el hilo del canal (bajan cosas de internet: no trabar la ventana) y devuelven JSON.
_DATA = {"lib": _lib, "list": _list, "search": _search, "play": _play, "enqueue": _enqueue, "queue": _queue,
         "jump": _jump, "liked": _liked, "ready": _ready, "diag": _diag}


_COMMANDS = {"ping": lambda: "pong", "page": _tag, "home": _home, "explore": _explore,
             "collection": _collection, "my_tracks": _my_tracks, "cycle": _cycle, "back": _back,
             "like": _like, "shuffle": _shuffle, "lyrics": _lyrics, "show": _show}


def _run(line):
    """Corre un comando (los de pantalla, en el hilo de GTK) y devuelve la respuesta."""
    from gi.repository import GLib
    name, _sep, rest = line.partition(" ")
    if name in _DATA:
        if _window is None:
            return json.dumps({"error": "todavía no hay ventana"})
        try:
            return json.dumps(_DATA[name](rest), ensure_ascii=False, separators=(",", ":"))
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": " ".join(str(exc).split()) or type(exc).__name__}, ensure_ascii=False)
    args = rest.split()
    fn = _COMMANDS.get(name)
    if fn is None:
        return "error comando desconocido"
    if _window is None:
        return "error todavía no hay ventana"
    done, out = threading.Event(), ["error tiempo agotado"]

    def call():
        try:
            out[0] = str(fn(*args))
        except Exception as exc:  # noqa: BLE001
            out[0] = "error " + " ".join(str(exc).split())
        done.set()
        return False

    GLib.idle_add(call)
    done.wait(8)
    return out[0]


def _serve():
    try:
        os.unlink(_SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(_SOCK)
    srv.listen(4)
    _log("canal de control en", _SOCK)
    while True:
        conn, _addr = srv.accept()
        # Cada pedido en su hilo: una búsqueda lenta no traba los botones del control.
        threading.Thread(target=_answer, args=(conn,), name="tele-cec-pedido", daemon=True).start()


def _answer(conn):
    with conn:
        try:
            conn.settimeout(30)
            data = b""
            while not data.endswith(b"\n") and len(data) < 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            line = data.decode("utf-8", "replace").strip()
            conn.sendall((_run(line) if line else "error vacío").encode("utf-8") + b"\n")
        except Exception as exc:  # noqa: BLE001
            _log("canal de control:", exc)


def _patch_window(module):
    cls = module.HighTideWindow
    original_init = cls.__init__

    def __init__(self, *args, **kwargs):
        global _window
        original_init(self, *args, **kwargs)
        first = _window is None
        _window = self
        try:
            from gi.repository import Gtk
            keys = Gtk.EventControllerKey()
            keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)  # antes que la lista o la fila de portadas
            keys.connect("key-pressed", _on_window_key)
            self.add_controller(keys)
            _log("flechas por posición y Enter en portadas y atajos")
        except Exception as exc:  # noqa: BLE001
            _log("no pude agregar el teclado:", exc)
        if first:
            threading.Thread(target=_serve, name="tele-cec", daemon=True).start()

    cls.__init__ = __init__

    restore = getattr(cls, "th_set_last_playing_song", None)
    if restore is None:
        _restored.set()
        return

    def th_set_last_playing_song(self, *args, **kwargs):
        try:
            return restore(self, *args, **kwargs)
        finally:
            _restored.set()

    cls.th_set_last_playing_song = th_set_last_playing_song


# ------------------------------------------------------------------ enganche
_TARGETS = {"high_tide.widgets.card_widget": _patch_cards, "high_tide.window": _patch_window}


class _Finder(importlib.abc.MetaPathFinder):
    """Espera a que High Tide cargue cada módulo de _TARGETS y lo parchea apenas termina."""

    def find_spec(self, name, path=None, target=None):
        patch = _TARGETS.pop(name, None)
        if patch is None:
            return None
        if not _TARGETS:
            sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        original_exec = spec.loader.exec_module

        def exec_module(module):
            original_exec(module)
            try:
                patch(module)
            except Exception as exc:  # noqa: BLE001
                _log("no pude aplicar el parche de", name + ":", exc)

        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _Finder())
