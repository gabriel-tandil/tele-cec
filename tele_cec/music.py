"""Tidal con High Tide (cliente para Linux, instalado con Flatpak).

No hay script inyectado como en Netflix: High Tide es una app GTK y se controla por las vías que ofrece
el escritorio:

- MPRIS (D-Bus, con `playerctl`): reproducir, pausar, siguiente, anterior, volumen, qué está sonando.
- Accesibilidad (AT-SPI): apretar botones de la app por su nombre ("Colección", "Agregar/Eliminar de
  favoritos"…) sin mandar teclas a todo el escritorio.
- Teclas con `wtype` (Wayland): para lo que solo responde al teclado (por ejemplo, Enter sobre un tema).
  Van a la ventana con el foco, que en la TV es High Tide porque se abre sola (Netflix se cierra antes).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .browser import graphical_env, spawn_scoped
from .overlay import HelperProcess

log = logging.getLogger("music")

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


# ---------------------------------------------------------------- procesos
_PROC_CACHE: tuple[float, list[tuple[int, list[str]]]] = (0.0, [])
_PROC_TTL = 0.3


def proc_table(fresh: bool = False) -> list[tuple[int, list[str]]]:
    """(PID, argv) de los procesos del sistema. Se guarda un instante: cada tecla pregunta qué app está
    abierta, y con varias actividades serían varios recorridos de /proc por tecla."""
    global _PROC_CACHE
    now = time.monotonic()
    if not fresh and now - _PROC_CACHE[0] < _PROC_TTL:
        return _PROC_CACHE[1]
    out = []
    for d in Path("/proc").glob("[0-9]*"):
        try:
            raw = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if raw:
            out.append((int(d.name), [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]))
            continue
    _PROC_CACHE = (now, out)
    return out


def find_pids(pred: Callable[[list[str]], bool], fresh: bool = False) -> list[int]:
    own = os.getpid()
    return [pid for pid, argv in proc_table(fresh) if pid != own and argv and pred(argv)]


def invalidate_procs() -> None:
    global _PROC_CACHE
    _PROC_CACHE = (0.0, [])


# ---------------------------------------------------------------- MPRIS (playerctl) y teclas (wtype)
class Mpris:
    """Controles de un reproductor por MPRIS (D-Bus) con playerctl."""

    def __init__(self, player: str, volume_step: float = 0.05):
        self.player = player  # nombre para playerctl -p
        self.volume_step = volume_step

    async def ctl(self, *args: str) -> bool:
        rc, _ = await run_cmd(["playerctl", "-p", self.player, *args], 5)
        return rc == 0

    async def status(self) -> dict[str, str]:
        rc, out = await run_cmd(["playerctl", "-p", self.player, "metadata", "--format",
                                 "{{status}}\t{{title}}\t{{artist}}"], 5)
        if rc != 0:
            # Sin tema cargado, "metadata" falla aunque el reproductor esté abierto: el estado sí responde.
            rc, st = await run_cmd(["playerctl", "-p", self.player, "status"], 5)
            return {"status": st.strip()} if rc == 0 else {}
        parts = (out.strip().split("\t") + ["", "", ""])[:3]
        return {"status": parts[0], "title": parts[1], "artist": parts[2]}

    async def volume(self, sign: str) -> bool:
        return await self.ctl("volume", f"{self.volume_step:.2f}{'+' if sign != '-' else '-'}")

    async def seek(self, seconds: int) -> bool:
        return await self.ctl("position", f"{abs(seconds)}{'+' if seconds >= 0 else '-'}")

    async def quit(self) -> bool:
        """Pide al reproductor que se cierre (método Quit de MPRIS)."""
        rc, _ = await run_cmd(["gdbus", "call", "--session", "--dest", f"org.mpris.MediaPlayer2.{self.player}",
                               "--object-path", "/org/mpris/MediaPlayer2", "--method",
                               "org.mpris.MediaPlayer2.Quit"], 2)  # High Tide 1.4 no contesta: no esperar de más
        return rc == 0


async def wtype_key(name: str) -> bool:
    """Manda una tecla a la ventana con el foco (apps Wayland, wtype).

    No sirve para apps X11 (Xwayland, como feh): Xwayland no usa el mapa de teclas propio de wtype y la
    tecla llega como otra (a feh le llega Escape y se cierra). Para esas, xdotool_key.
    """
    rc, out = await run_cmd(["wtype", "-k", name], 5)
    if rc != 0:
        log.warning("wtype %s: %s", name, out.strip() or rc)
    return rc == 0


async def xdotool_key(name: str) -> bool:
    """Manda una tecla a la ventana X11 con el foco (apps que corren por Xwayland, como feh)."""
    if not shutil.which("xdotool"):
        log.error("Falta xdotool (sudo apt install xdotool): sin él no puedo mandarle teclas a %s", name)
        return False
    rc, out = await run_cmd(["xdotool", "key", "--clearmodifiers", name], 5)
    if rc != 0:
        log.warning("xdotool %s: %s", name, out.strip() or rc)
    return rc == 0


# usercustomize.py que se le carga a High Tide (ver HighTide.write_card_patch). Corre adentro de High Tide.
# No puede llamarse sitecustomize.py: el runtime de GNOME usa el suyo para agregar las librerías de la app
# (tidalapi…) y uno con el mismo nombre lo taparía. usercustomize se carga además, después de ese.
CARD_PATCH = """\
\"\"\"Lo escribe tele-cec al abrir High Tide (no editar: se reescribe cada vez).

1. Hace que las tarjetas de High Tide (portadas de discos, playlists, artistas) puedan recibir el foco del
   teclado y abrirse con Enter, igual que con un clic.
2. Abre un canal de control (control.sock, al lado de este archivo) para que tele-cec cambie de pantalla
   (Inicio, Colección, Mis canciones, Atrás, Letras…) llamando a las mismas funciones que los botones, sin
   pasar por la accesibilidad (consultarla mientras High Tide dibuja puede cerrarlo: error de GTK 4).
Si algo falla, High Tide sigue como siempre.
\"\"\"
import importlib.abc
import importlib.util
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
    \"\"\"El panel con desplazamiento más externo que lo contiene (la página, el panel del reproductor…).\"\"\"
    from gi.repository import Gtk
    found, w = None, widget.get_parent()
    while w is not None and w is not window:
        if isinstance(w, Gtk.ScrolledWindow):
            found = w
        w = w.get_parent()
    return found


def _candidates(window):
    \"\"\"Lo que se puede elegir: widgets enfocables visibles, sin entrar en los que ya lo son (una fila de la
    lista se elige entera, no el link del artista de adentro).\"\"\"
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
                if w.get_focusable() and w.get_can_focus() and not isinstance(w, containers):
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
    \"\"\"Enter sobre algo que High Tide solo abre con un clic (portadas, atajos de Inicio): lo abre.\"\"\"
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
    \"\"\"Colección → «Canciones» (Mis canciones) y, con start >= 0, reproducirlas desde ese tema.\"\"\"
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
    \"\"\"Amarillo: Mis canciones → Colección → Inicio → Mis canciones.\"\"\"
    tag = _tag()
    if tag == _MY_TRACKS:
        return _collection()
    if tag == "collection":
        return _home()
    return _my_tracks(start)


def _back():
    \"\"\"Atrás: si hay un menú desplegado (☰, ⋮ de un tema), lo cierra; si no, la página anterior.\"\"\"
    from gi.repository import Gtk
    w = _window.get_focus()
    while w is not None:
        if isinstance(w, Gtk.Popover):
            w.popdown()
            return "ok"
        w = w.get_parent()
    return "ok" if _window.navigation_view.pop() else "no"


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


_COMMANDS = {"ping": lambda: "pong", "page": _tag, "home": _home, "explore": _explore,
             "collection": _collection, "my_tracks": _my_tracks, "cycle": _cycle, "back": _back,
             "like": _like, "shuffle": _shuffle, "lyrics": _lyrics}


def _run(line):
    \"\"\"Corre un comando en el hilo de GTK y devuelve la respuesta.\"\"\"
    from gi.repository import GLib
    name, *args = line.split()
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
        with conn:
            try:
                conn.settimeout(5)
                data = b""
                while not data.endswith(b"\\n") and len(data) < 1024:
                    chunk = conn.recv(256)
                    if not chunk:
                        break
                    data += chunk
                line = data.decode("utf-8", "replace").strip()
                conn.sendall((_run(line) if line else "error vacío").encode() + b"\\n")
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


# ------------------------------------------------------------------ enganche
_TARGETS = {"high_tide.widgets.card_widget": _patch_cards, "high_tide.window": _patch_window}


class _Finder(importlib.abc.MetaPathFinder):
    \"\"\"Espera a que High Tide cargue cada módulo de _TARGETS y lo parchea apenas termina.\"\"\"

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
"""


class TidalNav(HelperProcess):
    """Flechas que llegan a todos lados en High Tide (tidal_nav.py: sigue el foco por accesibilidad).

    Solo se abre mientras se usa el modo navegación de Tidal y se cierra con High Tide: escuchar eventos de
    accesibilidad hace que otras apps (Firefox) activen la suya, y eso gasta CPU en una Pi.
    """

    module = "tele_cec.tidal_nav"
    what = "el seguimiento del foco de Tidal"
    logger = log


class HighTide:
    """Abre, cierra y controla High Tide."""

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.app_id = str(cfg.get("flatpak_id") or "io.github.nokse22.high-tide")
        self.player = str(cfg.get("mpris_player") or self.app_id)  # nombre para playerctl -p
        self.start_timeout = float(cfg.get("start_timeout", 60))
        self.resume_on_open = bool(cfg.get("resume_on_open", False))
        self.start_in_nav = bool(cfg.get("start_in_nav", True))  # flechas = mover el foco al abrir
        self.volume_step = float(cfg.get("volume_step", 0.05))
        self.mpris = Mpris(self.player, self.volume_step)
        self.nav = TidalNav()
        self.gsk_renderer = str(cfg.get("gsk_renderer") or "")
        # Foco bien visible en la TV (GTK lo dibuja muy tenue): color del marco, o "" para no tocar el estilo.
        self.focus_color = str(cfg.get("focus_color", "#FFD400") or "")
        # Que las portadas (tarjetas de discos, playlists, artistas) se puedan elegir con el control y abrir con OK.
        self.keyboard_cards = bool(cfg.get("keyboard_cards", True))
        self._installed: bool | None = None

    # ------------------------------------------------------------ estado
    def installed(self) -> bool:
        if self._installed is None:
            flatpak = shutil.which("flatpak")
            self._installed = bool(flatpak) and subprocess.run(  # noqa: S603
                [flatpak, "info", self.app_id], capture_output=True, timeout=15).returncode == 0
        return self._installed

    @staticmethod
    def pids(fresh: bool = False) -> list[int]:
        """Procesos de High Tide (el Python de adentro del flatpak se ve desde afuera)."""
        return find_pids(lambda argv: any("/app/bin/high-tide" in a for a in argv), fresh)

    def running(self) -> bool:
        return bool(self.pids())

    # ------------------------------------------------------------ abrir y cerrar
    async def launch(self) -> bool:
        """Abre High Tide (si no está abierto) y espera a que publique sus controles (MPRIS)."""
        if not self.installed():
            log.error("High Tide no está instalado (flatpak %s). Ver README → Tidal.", self.app_id)
            return False
        if not self.pids(fresh=True):
            self.write_focus_css()
            args = ["flatpak", "run"]
            if self.gsk_renderer:
                args.append(f"--env=GSK_RENDERER={self.gsk_renderer}")
            patch_dir = self.write_card_patch()
            self.control_socket.unlink(missing_ok=True)  # el de una vez anterior (si se cerró de golpe)
            if patch_dir:
                args.append(f"--env=PYTHONPATH={patch_dir}")
            args.append(self.app_id)
            # En un scope propio de systemd, igual que el navegador: reiniciar el servicio no lo cierra.
            log.info("Abriendo High Tide: %s", " ".join(args))
            try:
                await spawn_scoped(args, "tele-cec-hightide", log="tidal")
            except OSError as exc:
                log.error("No pude abrir High Tide: %s", exc)
                return False
            invalidate_procs()
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if (await self.status()).get("status"):
                return True
            await asyncio.sleep(1)
        log.error("High Tide no publicó sus controles (MPRIS) en %.0f s", self.start_timeout)
        return False

    def saved(self) -> dict[str, str]:
        """Lo que High Tide guardó de la última vez (last-playing-index, last-playing-thing-id…).

        Está en la configuración GSettings del Flatpak, un archivo de texto que se puede leer desde afuera.
        Las claves que tienen su valor por defecto no aparecen.
        """
        out: dict[str, str] = {}
        f = Path.home() / ".var" / "app" / self.app_id / "config" / "glib-2.0" / "settings" / "keyfile"
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip().startswith("last-"):
                out[k.strip()] = v.strip().strip("'\"")
        return out

    def saved_index(self) -> int:
        try:
            return max(0, int(self.saved().get("last-playing-index", "0")))
        except ValueError:
            return 0

    async def resume(self) -> bool:
        """Al abrir: seguir con lo último que estaba sonando, si High Tide lo puede restaurar solo.

        High Tide recuerda álbumes, playlists y mixes, pero no "Mis canciones": en ese caso no guarda
        qué se estaba escuchando (solo el número de tema) y devuelve False sin hacer esperar.
        """
        restorable = bool(self.saved().get("last-playing-thing-id"))
        for _ in range(20 if restorable else 3):
            st = await self.status()
            if st.get("title"):
                if st.get("status") != "Playing":
                    await self.ctl("play")
                log.info("Sigue sonando: %s - %s", st.get("title"), st.get("artist"))
                return True
            await asyncio.sleep(1)
        return False

    def write_focus_css(self) -> None:
        """Estilo para que el elemento con el foco se vea desde el sillón (marco grueso y fondo de color).

        High Tide es una app GTK 4 en Flatpak: lee el CSS del usuario de su propia carpeta de configuración
        (~/.var/app/<id>/config/gtk-4.0/gtk.css). tele-cec escribe el suyo aparte (tele-cec.css) y lo importa
        desde gtk.css, sin pisar lo que haya ahí. Se aplica al abrir High Tide.
        """
        d = Path.home() / ".var" / "app" / self.app_id / "config" / "gtk-4.0"
        css, main = d / "tele-cec.css", d / "gtk.css"
        try:
            if not self.focus_color:
                css.write_text("/* tele-cec: estilo del foco desactivado ([apps.tidal] focus_color = \"\") */\n")
                return
            d.mkdir(parents=True, exist_ok=True)
            c = self.focus_color
            # Solo lo que se elige: GTK también marca «con foco» a los contenedores (la ventana, los paneles).
            sel = (".card-bg, .card-image, row, flowboxchild, gridview > child, listview > row, button, togglebutton, menubutton > button, "
                   "scale, entry, checkbutton, switch, label, link, .card")
            targets = ", ".join(f"{s.strip()}:focus-visible" for s in sel.split(","))
            css.write_text(
                "/* Lo escribe tele-cec al abrir High Tide: foco bien visible en la TV. */\n"
                f"{targets} {{\n  outline: 4px solid {c};\n  outline-offset: -4px;\n}}\n"
                ".card-bg:focus-visible, row:focus-visible, flowboxchild:focus-visible, gridview > child:focus-visible, "
                "listview > row:focus-visible, button:focus-visible, togglebutton:focus-visible {\n"
                f"  background-color: alpha({c}, 0.25);\n}}\n", encoding="utf-8")
            text = main.read_text(encoding="utf-8") if main.exists() else ""
            if "tele-cec.css" not in text:
                main.write_text(text + ('\n' if text and not text.endswith('\n') else '') +
                                '@import url("tele-cec.css");\n', encoding="utf-8")
        except OSError as exc:
            log.warning("No pude escribir el estilo del foco de High Tide: %s", exc)

    def write_card_patch(self) -> str:
        """Parche para que las tarjetas de High Tide se puedan elegir con el teclado (y con el control).

        Las portadas de discos, playlists y artistas son un widget propio de High Tide (HTCardWidget) que solo se
        abre con un clic del mouse: no recibe el foco del teclado. High Tide está escrito en Python, así que al
        abrirlo se le carga un usercustomize.py (por PYTHONPATH, desde su propia carpeta de datos, que se ve dentro
        del Flatpak) que, cuando High Tide carga ese widget, lo hace enfocable y le agrega Enter = abrir (lo mismo
        que hace su clic). No modifica la app instalada; si algo falla, High Tide sigue igual que siempre.
        Devuelve la carpeta para PYTHONPATH ("" si está desactivado).
        """
        d = Path.home() / ".var" / "app" / self.app_id / "data" / "tele-cec"
        if not self.keyboard_cards:
            return ""
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "sitecustomize.py").unlink(missing_ok=True)  # versión anterior: tapaba el del runtime
            (d / "usercustomize.py").write_text(CARD_PATCH, encoding="utf-8")
        except OSError as exc:
            log.warning("No pude escribir el parche de tarjetas de High Tide: %s", exc)
            return ""
        return str(d)

    async def quit(self, timeout: float = 15.0) -> None:
        """Cierra High Tide: MPRIS Quit; si no responde (en High Tide 1.4 falla), SIGTERM; si no, flatpak kill."""
        await self.nav.close()
        pids = self.pids(fresh=True)
        if not pids:
            return
        await self.mpris.quit()
        for step, wait in (("term", 2.0), ("kill", timeout)):
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if not self.pids(fresh=True):
                    log.info("High Tide cerrado")
                    return
                await asyncio.sleep(0.25)
            if step == "term":
                log.info("High Tide no respondió a Quit: lo cierro con SIGTERM")
                for pid in self.pids(fresh=True):
                    try:
                        os.kill(pid, 15)
                    except OSError:
                        pass
        log.warning("High Tide no se cerró solo: lo cierro con flatpak kill")
        await run_cmd(["flatpak", "kill", self.app_id], 10)
        invalidate_procs()

    # ------------------------------------------------------------ reproducción (MPRIS)
    async def ctl(self, *args: str) -> bool:
        return await self.mpris.ctl(*args)

    async def status(self) -> dict[str, str]:
        return await self.mpris.status()

    async def volume(self, sign: str) -> bool:
        return await self.mpris.volume(sign)

    async def seek(self, seconds: int) -> bool:
        return await self.mpris.seek(seconds)

    # ------------------------------------------------------------ canal de control (parche de High Tide)
    @property
    def control_socket(self) -> Path:
        return Path.home() / ".var" / "app" / self.app_id / "data" / "tele-cec" / "control.sock"

    async def command(self, line: str, timeout: float = 10.0, wait: float = 0.0) -> str | None:
        """Manda un comando al canal de control que abre el parche dentro de High Tide (ver CARD_PATCH).

        Devuelve la respuesta, o None si el canal no está (parche desactivado, High Tide abierto sin tele-cec
        o todavía arrancando). Es la forma segura de cambiar de pantalla: la accesibilidad puede cerrarlo.
        """
        if not self.keyboard_cards:
            return None
        # wait: High Tide recién abierto abre el canal cuando termina de armar la ventana.
        deadline = time.monotonic() + wait
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(self.control_socket)), 2)
                break
            except (OSError, asyncio.TimeoutError):
                if time.monotonic() >= deadline or not self.pids():
                    return None
                await asyncio.sleep(0.5)
        try:
            writer.write(line.encode() + b"\n")
            await writer.drain()
            reply = (await asyncio.wait_for(reader.readline(), timeout)).decode("utf-8", "replace").strip()
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("Canal de control de High Tide (%s): %s", line, exc or "sin respuesta")
            return None
        finally:
            writer.close()
        if reply.startswith("error"):
            log.warning("High Tide no pudo hacer «%s»: %s", line, reply[6:])
        return reply

    async def go(self, page: str) -> bool:
        """Pantalla de High Tide: home, explore, collection, back (atrás)."""
        reply = await self.command(page)
        if reply is not None:
            return not reply.startswith("error") and reply != "no"
        names = {"home": HOME, "explore": EXPLORE, "collection": COLLECTION, "back": BACK}[page]
        return await self.press_button(names)

    async def like(self) -> bool:
        reply = await self.command("like")
        return reply == "ok" if reply is not None else await self.press_button(LIKE)

    async def shuffle(self) -> bool:
        reply = await self.command("shuffle")
        return reply in ("on", "off") if reply is not None else await self.press_button(SHUFFLE)

    async def cycle(self) -> str | None:
        """Amarillo: Mis canciones → Colección → Inicio → … según la pantalla que muestra High Tide.

        Devuelve a dónde fue ("my_tracks", "collection", "home"), "" si no pudo, o None sin canal de control.
        """
        for _ in range(30):
            reply = await self.command("cycle 0", wait=5)
            if reply != "empty":
                break
            await asyncio.sleep(1)  # recién abierto: los favoritos todavía están cargando
        if reply is None:
            return None
        return "" if reply.startswith("error") or reply == "empty" else reply

    # ------------------------------------------------------------ interfaz (AT-SPI + wtype)
    async def key(self, name: str) -> bool:
        return await wtype_key(name)

    async def toggle_lyrics(self) -> bool:
        """Pestaña Letras del panel de High Tide (otra vez = volver a Reproductor)."""
        reply = await self.command("lyrics")
        if reply is not None:
            return reply in ("lyrics", "player")
        ok = await asyncio.to_thread(_atspi_toggle_tab, LYRICS, PLAYER_TAB)
        if not ok:
            log.warning("No encontré en High Tide la pestaña %s", " / ".join(LYRICS))
        return ok

    async def move(self, direction: str) -> bool:
        """Flecha en el modo navegación: con Tab de respaldo donde GTK no mueve el foco (ver tidal_nav.py)."""
        if await self.nav.send({"op": "nav", "dir": direction}):
            return True
        return await self.key(direction)  # sin el auxiliar (falta AT-SPI): la flecha sola

    async def stop_nav(self) -> None:
        await self.nav.close()

    async def tab(self, back: bool = False) -> bool:
        """Al elemento siguiente (Tab) o anterior (Shift+Tab, que wtype manda como ISO_Left_Tab)."""
        cmd = ["wtype", "-M", "shift", "-k", "ISO_Left_Tab", "-m", "shift"] if back else ["wtype", "-k", "Tab"]
        rc, out = await run_cmd(cmd, 5)
        if rc != 0:
            log.warning("wtype %s: %s", "Shift+Tab" if back else "Tab", out.strip() or rc)
        return rc == 0

    async def press_button(self, names: tuple[str, ...]) -> bool:
        ok = await asyncio.to_thread(_atspi_activate, names)
        if not ok:
            log.warning("No encontré en High Tide el botón %s", " / ".join(names))
        return ok

    async def my_tracks(self, start: int = 0) -> bool:
        """Reproduce "Mis canciones" desde el tema número `start` (0 = el primero).

        Colección → "More" de Canciones → primer tema → (flecha abajo `start` veces) → Enter.
        High Tide no tiene una acción para esto. Activar una fila de esa lista reproduce todos los
        favoritos desde esa fila, y las filas solo se activan con Enter (o clic), así que se llega al
        primer tema con Tab y se confirma con Enter. Con el canal de control del parche se hace directo.
        """
        for _ in range(30):
            reply = await self.command(f"my_tracks {max(start, 0)}", wait=20)
            if reply != "empty":
                break
            await asyncio.sleep(1)  # recién abierto: los favoritos todavía están cargando
        if reply is not None:
            return reply == "my_tracks"
        # Recién abierto, High Tide publica sus controles antes de terminar de dibujar la interfaz. Y dentro
        # de una página (un álbum, la misma lista de Canciones) la barra Inicio/Explorar/Colección no está:
        # se vuelve con "Atrás" hasta encontrarla.
        for attempt in range(30):
            if await asyncio.to_thread(_atspi_activate, COLLECTION):
                break
            if attempt < 4 and await asyncio.to_thread(_atspi_activate, BACK):
                await asyncio.sleep(0.8)
                continue
            await asyncio.sleep(1)
        else:
            log.warning("No encontré en High Tide el botón %s", " / ".join(COLLECTION))
            return False
        more = None
        for _ in range(20):
            more = await asyncio.to_thread(_atspi_section_more, TRACKS)
            if more:
                break
            await asyncio.sleep(0.5)
        if not more:
            log.warning("No encontré la sección Canciones en Colección")
            return False
        await asyncio.to_thread(_atspi_do, more)
        rows: list[Any] = []
        for _ in range(30):
            rows = await asyncio.to_thread(_atspi_rows)
            if rows:
                break
            await asyncio.sleep(0.5)
        if not rows:
            log.warning("La lista de Canciones no cargó")
            return False
        for _ in range(40):
            if not await self.key("Tab"):
                return False
            await asyncio.sleep(0.05)
            if await asyncio.to_thread(_atspi_any_focused, rows):
                for _ in range(start):
                    await self.key("Down")
                await self.key("Return")
                return True
        log.warning("No llegué con Tab al primer tema de Canciones")
        return False


async def run_cmd(cmd: list[str], timeout: float) -> tuple[int, str]:
    env = graphical_env()
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        return 127, str(exc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "sin respuesta"
    return proc.returncode or 0, out.decode("utf-8", "replace")


_run = run_cmd  # nombre anterior


# ---------------------------------------------------------------- AT-SPI (se llama en un hilo aparte)
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


def tools_status() -> dict[str, bool]:
    """Para el diagnóstico: ¿están las herramientas que usa el control de Tidal?"""
    try:
        _atspi()
        atspi = True
    except Exception:  # noqa: BLE001
        atspi = False
    return {"flatpak": bool(shutil.which("flatpak")), "playerctl": bool(shutil.which("playerctl")),
            "wtype": bool(shutil.which("wtype")), "gdbus": bool(shutil.which("gdbus")), "atspi": atspi,
            "wayland": bool(os.environ.get("WAYLAND_DISPLAY") or graphical_env().get("WAYLAND_DISPLAY"))}
