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
        self.resume_on_open = bool(cfg.get("resume_on_open", True))
        self.volume_step = float(cfg.get("volume_step", 0.05))
        self.mpris = Mpris(self.player, self.volume_step)
        self.nav = TidalNav()
        self.gsk_renderer = str(cfg.get("gsk_renderer") or "")
        # Foco bien visible en la TV (GTK lo dibuja muy tenue): color del marco, o "" para no tocar el estilo.
        self.focus_color = str(cfg.get("focus_color", "#FFD400") or "")
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
            sel = ("row, flowboxchild, gridview > child, listview > row, button, togglebutton, menubutton > button, "
                   "scale, entry, checkbutton, switch, label, link, .card")
            targets = ", ".join(f"{s.strip()}:focus-visible" for s in sel.split(","))
            css.write_text(
                "/* Lo escribe tele-cec al abrir High Tide: foco bien visible en la TV. */\n"
                f"{targets} {{\n  outline: 4px solid {c};\n  outline-offset: -4px;\n}}\n"
                "row:focus-visible, flowboxchild:focus-visible, gridview > child:focus-visible, "
                "listview > row:focus-visible, button:focus-visible, togglebutton:focus-visible {\n"
                f"  background-color: alpha({c}, 0.25);\n}}\n", encoding="utf-8")
            text = main.read_text(encoding="utf-8") if main.exists() else ""
            if "tele-cec.css" not in text:
                main.write_text(text + ('\n' if text and not text.endswith('\n') else '') +
                                '@import url("tele-cec.css");\n', encoding="utf-8")
        except OSError as exc:
            log.warning("No pude escribir el estilo del foco de High Tide: %s", exc)

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

    # ------------------------------------------------------------ interfaz (AT-SPI + wtype)
    async def key(self, name: str) -> bool:
        return await wtype_key(name)

    async def toggle_lyrics(self) -> bool:
        """Pestaña Letras del panel de High Tide (otra vez = volver a Reproductor)."""
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
        primer tema con Tab y se confirma con Enter.
        """
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
