"""Flechas en Tidal (High Tide) que llegan a todos lados (proceso aparte, bucle de GLib).

En el modo navegación de Tidal (tecla 0) las flechas van a High Tide como teclas (wtype). GTK 4 mueve el
foco con flechas solo dentro de cada bloque, y algunos lugares lo atrapan:

- en una barra deslizante (progreso, volumen) las cuatro flechas cambian su valor y el foco no sale más;
- dentro de una fila de la lista, desde el link del artista o del álbum, ◀ ▶ no mueven nada.

Tab sí recorre todo, pero de a uno. Este proceso escucha por accesibilidad (AT-SPI) a qué elemento va el
foco, que llega como evento y no cuesta nada. Recorrer el árbol de High Tide para buscar elementos tarda
decenas de segundos en una Pi, y GTK 4 no deja mover el foco por accesibilidad. Con eso:

- si el foco está en una barra deslizante, la flecha se reemplaza por Tab (▼ ▶) o Shift+Tab (▲ ◀);
- si ◀ ▶ no movieron el foco, se manda Shift+Tab o Tab (▲ ▼ no: en el borde Tab daría la vuelta);
- si así cae en una barra deslizante, se sigue de largo.

Órdenes por stdin, una por línea:  {"op": "nav", "dir": "Down"}   {"op": "quit"}
Al arrancar escribe {"ready": true}. Cada movimiento se describe en stderr (llega al log en modo debug).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import deque

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

# Elementos donde las flechas cambian un valor en vez de mover el foco.
TRAP_ROLES = {"slider", "scroll bar", "spin button"}
# En un campo de texto ◀ ▶ mueven el cursor: ahí no se usa Tab de respaldo.
TEXT_ROLES = {"text", "entry", "password text", "editbar"}
DIRS = {"Up", "Down", "Left", "Right"}
WAIT_MS = 700          # cuánto esperar el evento de foco (High Tide a veces tarda ~0,5 s en avisarlo)
POLL_MS = 20
MAX_SKIPS = 4          # barras deslizantes seguidas que se saltean
MAX_QUEUE = 2          # flechas en espera (botón mantenido): el resto se descarta


def wtype(key: str, shift: bool = False) -> None:
    # Shift+Tab tiene que ir como ISO_Left_Tab (con "-k Tab" GTK no lo toma como Shift+Tab).
    if shift and key == "Tab":
        key = "ISO_Left_Tab"
    cmd = ["wtype", "-M", "shift", "-k", key, "-m", "shift"] if shift else ["wtype", "-k", key]
    try:
        subprocess.run(cmd, check=False, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"wtype {key}: {exc}", file=sys.stderr, flush=True)


APP_ID = "io.github.nokse22.high-tide"
_PIDS: dict[int, bool] = {}


def is_high_tide(pid: int) -> bool:
    """¿Este PID es High Tide? Es el dueño de la conexión de accesibilidad: en Flatpak, su xdg-dbus-proxy.

    Mirar /proc tarda 1 ms; preguntarle al objeto de qué app es (get_application) sube por todo el árbol
    hasta la raíz y tarda medio segundo en una Pi.
    """
    if pid not in _PIDS:
        ok = False
        try:
            ok = f"name={APP_ID}" in open(f"/proc/{pid}/root/.flatpak-info", encoding="utf-8").read()
        except OSError:
            pass
        if not ok:
            try:
                ok = b"high-tide" in open(f"/proc/{pid}/cmdline", "rb").read()
            except OSError:
                pass
        _PIDS[pid] = ok
    return _PIDS[pid]


class Nav:
    def __init__(self) -> None:
        self.role = ""
        self.src = None
        self.seq = 0            # cuenta los cambios de foco en High Tide
        self.busy = False
        self.pending: deque[str] = deque()
        self.t0 = 0.0
        self.listener = Atspi.EventListener.new(self.on_focus)
        self.listener.register("object:state-changed:focused")

    # ------------------------------------------------------------ foco
    def on_focus(self, ev) -> None:
        if ev.detail1 != 1:
            return
        src = ev.source
        try:
            if not is_high_tide(src.get_process_id()):
                return
            self.role = src.get_role_name() or ""
        except Exception:  # noqa: BLE001
            return
        self.src = src
        self.seq += 1

    # ------------------------------------------------------------ órdenes
    def nav(self, direction: str) -> None:
        if direction not in DIRS:
            return
        if self.busy:
            if len(self.pending) < MAX_QUEUE:
                self.pending.append(direction)
            return
        self.busy = True
        self.t0 = time.monotonic()
        if self.role in TRAP_ROLES:
            self._tab(direction, MAX_SKIPS, f"{self.role}: salto con Tab")
            return
        start = self.seq
        wtype(direction)
        self._wait(start, lambda: self._after_arrow(direction, start))

    def _wait(self, start: int, then) -> None:
        """Llama a `then` apenas cambia el foco, o a los WAIT_MS si no cambia."""
        deadline = time.monotonic() + WAIT_MS / 1000

        def poll() -> bool:
            if self.seq != start or time.monotonic() >= deadline:
                then()
                return False
            return True
        GLib.timeout_add(POLL_MS, poll)

    def _after_arrow(self, direction: str, start: int) -> None:
        if self.seq != start:
            if self.role in TRAP_ROLES:
                self._tab(direction, MAX_SKIPS, f"flecha cayó en {self.role}: sigo con Tab")
            else:
                self._done(direction, "flecha")
        elif direction in ("Up", "Down"):
            # Arriba o abajo no hay nada (borde): Tab daría la vuelta a la otra punta de la ventana.
            self._done(direction, "no hay nada en esa dirección")
        elif self.role in TEXT_ROLES:
            self._done(direction, "flecha (cursor en el texto)")
        else:
            self._tab(direction, MAX_SKIPS, "la flecha no movió el foco: Tab")

    def _tab(self, direction: str, left: int, why: str) -> None:
        start = self.seq
        wtype("Tab", shift=direction in ("Up", "Left"))
        self._wait(start, lambda: self._after_tab(direction, start, left - 1, why))

    def _after_tab(self, direction: str, start: int, left: int, why: str) -> None:
        if self.seq != start and self.role in TRAP_ROLES and left > 0:
            self._tab(direction, left, why + " (salteo otra barra)")
        else:
            self._done(direction, why)

    def _done(self, direction: str, how: str) -> None:
        ms = (time.monotonic() - self.t0) * 1000
        self.busy = False
        try:
            name = (self.src.get_name() or "")[:40] if self.src is not None else ""
        except Exception:  # noqa: BLE001
            name = ""
        print(f"{direction}: {how} -> {self.role or '?'} «{name}» ({ms:.0f} ms)", file=sys.stderr, flush=True)
        if self.pending:
            self.nav(self.pending.popleft())

    def handle(self, msg: dict) -> None:
        op = msg.get("op")
        if op == "nav":
            self.nav(str(msg.get("dir") or ""))
        elif op == "quit":
            loop.quit()


def _on_stdin(channel, cond, nav: Nav) -> bool:
    if cond & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
        loop.quit()
        return False
    try:
        status, line, _len, _term = channel.read_line()
    except GLib.Error:
        loop.quit()
        return False
    if status == GLib.IOStatus.EOF:
        loop.quit()
        return False
    line = (line or "").strip()
    if line:
        try:
            msg = json.loads(line)
        except ValueError:
            print(f"orden inválida: {line[:100]}", file=sys.stderr, flush=True)
        else:
            if isinstance(msg, dict):
                nav.handle(msg)
    return True


loop = GLib.MainLoop()


def main() -> int:
    nav = Nav()
    channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
    channel.set_encoding("utf-8")
    GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                      _on_stdin, nav)
    print(json.dumps({"ready": True}), flush=True)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
