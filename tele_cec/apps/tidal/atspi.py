"""High Tide por accesibilidad (AT-SPI): respaldo cuando el canal de control del parche no está.

Se llama en un hilo aparte (asyncio.to_thread): las consultas por AT-SPI bloquean.
"""

from __future__ import annotations

from typing import Any

# Nombres de los botones de High Tide tal como los publica por accesibilidad (español e inglés).
HOME = ("Inicio", "Home")
EXPLORE = ("Explorar", "Explore")
COLLECTION = ("Colección", "Collection")
TRACKS = ("Canciones", "Tracks")
MORE = ("More", "Más")
LIKE = ("Agregar/Eliminar de favoritos", "Add/Remove from favorites")
SHUFFLE = ("Alternar modo aleatorio", "Toggle shuffle")
BACK = ("Atrás", "Back")
LYRICS = ("Letras", "Lyrics")
PLAYER_TAB = ("Reproductor", "Player")

def _atspi():
    import gi  # noqa: PLC0415  (opcional: solo hace falta para Tidal)
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi  # noqa: PLC0415
    return Atspi


def _app():
    Atspi = _atspi()
    desk = Atspi.get_desktop(0)
    for i in range(desk.get_child_count()):
        a = desk.get_child_at_index(i)
        if a is not None and "tide" in (a.get_name() or "").lower():
            return a
    return None


def _text(o, depth: int = 0) -> str:
    n = o.get_name() or ""
    if n or depth > 3:
        return n
    for j in range(min(o.get_child_count(), 6)):
        c = o.get_child_at_index(j)
        t = _text(c, depth + 1) if c is not None else ""
        if t:
            return t
    return ""


# Dentro de estos no hay nada que buscar (y son la mayor parte del árbol: filas de temas, tarjetas).
_NO_DESCEND = {"list item", "table cell", "label", "image", "heading", "link", "slider"}


def _walk(roles: tuple[str, ...], done=None, limit: int | None = None
          ) -> list[tuple[Any, str, str, tuple[int, int, int, int], bool]]:
    """Elementos visibles de High Tide con esos roles: (objeto, rol, texto, (x, y, ancho, alto), enfocable).

    Recorre por niveles (los botones de navegación están cerca de la raíz) y corta apenas `done(lista)`
    da verdadero o se juntan `limit` elementos. Cada consulta de accesibilidad es un viaje por D-Bus: en
    una Pi lenta, recorrer todo el árbol de High Tide (cientos de tarjetas y temas) tarda decenas de segundos.
    """
    Atspi = _atspi()
    S = Atspi.StateType
    app = _app()
    out: list = []
    if app is None:
        return out
    level = [app.get_child_at_index(i) for i in range(app.get_child_count())]
    depth = 0
    while level and depth < 60:
        nxt = []
        for o in level:
            if o is None:
                continue
            try:
                st = o.get_state_set()
                role = o.get_role_name()
            except Exception:  # noqa: BLE001
                continue
            if not st.contains(S.SHOWING):
                continue
            if role in roles:
                try:
                    e = o.get_extents(Atspi.CoordType.WINDOW)
                    rect = (e.x, e.y, e.width, e.height)
                except Exception:  # noqa: BLE001
                    rect = (0, 0, 0, 0)
                out.append((o, role, _text(o), rect, st.contains(S.FOCUSABLE)))
                if (limit and len(out) >= limit) or (done and done(out)):
                    return out
            if role not in _NO_DESCEND:
                try:
                    nxt.extend(o.get_child_at_index(i) for i in range(o.get_child_count()))
                except Exception:  # noqa: BLE001
                    pass
        level = nxt
        depth += 1
    return out


def _atspi_do(o) -> bool:
    try:
        act = o.get_action_iface()
        return bool(act and act.get_n_actions() and act.do_action(0))
    except Exception:  # noqa: BLE001
        return False


def _atspi_activate(names: tuple[str, ...]) -> bool:
    want = {n.lower() for n in names}
    found = _walk(("button", "toggle button"), done=lambda out: out[-1][2].strip().lower() in want)
    return bool(found) and found[-1][2].strip().lower() in want and _atspi_do(found[-1][0])


def _atspi_section_more(titles: tuple[str, ...]):
    """El botón "More" que está en la misma línea que el título de sección (p. ej. "Canciones")."""
    want = {t.lower() for t in titles}

    def header(out):
        return next((r for o, role, t, r, f in out if role == "label" and t.strip().lower() in want), None)

    def done(out):
        h = header(out)
        return bool(h) and any(role == "button" and t.strip() in MORE and abs(r[1] - h[1]) < 20
                               for o, role, t, r, f in out)

    items = _walk(("label", "button"), done=done)
    hdr = header(items)
    if not hdr:
        return None
    more = [(abs(r[1] - hdr[1]), o) for o, role, t, r, f in items
            if role == "button" and t.strip() in MORE and abs(r[1] - hdr[1]) < 20]
    return min(more, key=lambda x: x[0])[1] if more else None


def _atspi_toggle_tab(target: tuple[str, ...], back: tuple[str, ...]) -> bool:
    """Selecciona la pestaña `target`; si ya estaba seleccionada, la `back`."""
    want, home = {n.lower() for n in target}, {n.lower() for n in back}
    tabs = _walk(("page tab",), done=lambda out: {t.strip().lower() for _o, _r, t, _x, _f in out} & want
                 and {t.strip().lower() for _o, _r, t, _x, _f in out} & home)
    tab = next((o for o, _r, t, _x, _f in tabs if t.strip().lower() in want), None)
    if tab is None:
        return False
    try:
        selected = tab.get_state_set().contains(_atspi().StateType.SELECTED)
    except Exception:  # noqa: BLE001
        selected = False
    if selected:
        tab = next((o for o, _r, t, _x, _f in tabs if t.strip().lower() in home), tab)
    return _atspi_do(tab)


def _atspi_rows() -> list:
    rows = [(r[1], o) for o, role, t, r, f in _walk(("list item",), limit=8) if f and r[2] > 200]
    return [o for _y, o in sorted(rows, key=lambda x: x[0])[:5]]


def _atspi_any_focused(rows: list) -> bool:
    S = _atspi().StateType
    for o in rows:
        try:
            if o.get_state_set().contains(S.FOCUSED):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False
