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
from typing import Any

from .browser import graphical_env

log = logging.getLogger("music")

# Nombres de los botones de High Tide tal como los publica por accesibilidad (español e inglés).
COLLECTION = ("Colección", "Collection")
TRACKS = ("Canciones", "Tracks")
MORE = ("More", "Más")
LIKE = ("Agregar/Eliminar de favoritos", "Add/Remove from favorites")
SHUFFLE = ("Alternar modo aleatorio", "Toggle shuffle")
BACK = ("Atrás", "Back")


class HighTide:
    """Abre, cierra y controla High Tide."""

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.app_id = str(cfg.get("flatpak_id") or "io.github.nokse22.high-tide")
        self.player = str(cfg.get("mpris_player") or self.app_id)  # nombre para playerctl -p
        self.start_timeout = float(cfg.get("start_timeout", 60))
        self.resume_on_open = bool(cfg.get("resume_on_open", True))
        self.volume_step = float(cfg.get("volume_step", 0.05))
        self.gsk_renderer = str(cfg.get("gsk_renderer") or "")
        self._installed: bool | None = None

    # ------------------------------------------------------------ estado
    def installed(self) -> bool:
        if self._installed is None:
            flatpak = shutil.which("flatpak")
            self._installed = bool(flatpak) and subprocess.run(  # noqa: S603
                [flatpak, "info", self.app_id], capture_output=True, timeout=15).returncode == 0
        return self._installed

    @staticmethod
    def pids() -> list[int]:
        """Procesos de High Tide (el Python de adentro del flatpak se ve desde afuera)."""
        out = []
        for d in Path("/proc").glob("[0-9]*"):
            try:
                if b"/app/bin/high-tide" in (d / "cmdline").read_bytes():
                    out.append(int(d.name))
            except OSError:
                continue
        return out

    def running(self) -> bool:
        return bool(self.pids())

    # ------------------------------------------------------------ abrir y cerrar
    async def launch(self) -> bool:
        """Abre High Tide (si no está abierto) y espera a que publique sus controles (MPRIS)."""
        if not self.installed():
            log.error("High Tide no está instalado (flatpak %s). Ver README → Tidal.", self.app_id)
            return False
        if not self.running():
            env = graphical_env()
            args = ["flatpak", "run"]
            if self.gsk_renderer:
                args.append(f"--env=GSK_RENDERER={self.gsk_renderer}")
            args.append(self.app_id)
            # En un scope propio de systemd, igual que el navegador: reiniciar el servicio no lo cierra.
            cmd = args
            if shutil.which("systemd-run") and env.get("XDG_RUNTIME_DIR"):
                cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                       f"--unit=netflix-cec-hightide-{int(time.time())}", "--"] + args
            log.info("Abriendo High Tide: %s", " ".join(args))
            try:
                subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # noqa: S603
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                log.error("No pude abrir High Tide: %s", exc)
                return False
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if (await self.status()).get("status"):
                return True
            await asyncio.sleep(1)
        log.error("High Tide no publicó sus controles (MPRIS) en %.0f s", self.start_timeout)
        return False

    async def resume(self) -> None:
        """Al abrir: seguir con lo último que estaba sonando, si High Tide lo recuerda."""
        for _ in range(15):
            st = await self.status()
            if st.get("title"):
                if st.get("status") != "Playing":
                    await self.ctl("play")
                    log.info("Sigue sonando: %s - %s", st.get("title"), st.get("artist"))
                return
            await asyncio.sleep(1)
        log.info("High Tide no tenía nada para seguir reproduciendo")

    async def quit(self, timeout: float = 15.0) -> None:
        """Cierra High Tide ordenadamente (MPRIS Quit); si no responde, lo cierra Flatpak."""
        if not self.running():
            return
        await _run(["gdbus", "call", "--session", "--dest", f"org.mpris.MediaPlayer2.{self.player}",
                    "--object-path", "/org/mpris/MediaPlayer2", "--method", "org.mpris.MediaPlayer2.Quit"], 5)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                log.info("High Tide cerrado")
                return
            await asyncio.sleep(0.5)
        log.warning("High Tide no se cerró solo: lo cierro con flatpak kill")
        await _run(["flatpak", "kill", self.app_id], 10)

    # ------------------------------------------------------------ reproducción (MPRIS)
    async def ctl(self, *args: str) -> bool:
        rc, _ = await _run(["playerctl", "-p", self.player, *args], 5)
        return rc == 0

    async def status(self) -> dict[str, str]:
        rc, out = await _run(["playerctl", "-p", self.player, "metadata", "--format",
                              "{{status}}\t{{title}}\t{{artist}}"], 5)
        if rc != 0:
            # Sin tema cargado, "metadata" falla aunque High Tide esté abierto: el estado sí responde.
            rc, st = await _run(["playerctl", "-p", self.player, "status"], 5)
            return {"status": st.strip()} if rc == 0 else {}
        parts = (out.strip().split("\t") + ["", "", ""])[:3]
        return {"status": parts[0], "title": parts[1], "artist": parts[2]}

    async def volume(self, sign: str) -> bool:
        return await self.ctl("volume", f"{self.volume_step:.2f}{'+' if sign != '-' else '-'}")

    async def seek(self, seconds: int) -> bool:
        return await self.ctl("position", f"{abs(seconds)}{'+' if seconds >= 0 else '-'}")

    # ------------------------------------------------------------ interfaz (AT-SPI + wtype)
    async def key(self, name: str) -> bool:
        rc, out = await _run(["wtype", "-k", name], 5)
        if rc != 0:
            log.warning("wtype %s: %s", name, out.strip() or rc)
        return rc == 0

    async def press_button(self, names: tuple[str, ...]) -> bool:
        ok = await asyncio.to_thread(_atspi_activate, names)
        if not ok:
            log.warning("No encontré en High Tide el botón %s", " / ".join(names))
        return ok

    async def my_tracks(self) -> bool:
        """Reproduce "Mis canciones": Colección → "More" de Canciones → primer tema → Enter.

        High Tide no tiene una acción para esto. Activar una fila de esa lista reproduce todos los
        favoritos desde esa fila, y las filas solo se activan con Enter (o clic), así que se llega al
        primer tema con Tab y se confirma con Enter.
        """
        if not await self.press_button(COLLECTION):
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
            await asyncio.sleep(0.15)
            if await asyncio.to_thread(_atspi_any_focused, rows):
                await self.key("Return")
                return True
        log.warning("No llegué con Tab al primer tema de Canciones")
        return False


async def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
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


def _walk(roles: tuple[str, ...]) -> list[tuple[Any, str, str, tuple[int, int, int, int], bool]]:
    """Elementos visibles de High Tide con esos roles: (objeto, rol, texto, (x, y, ancho, alto), enfocable)."""
    Atspi = _atspi()
    S = Atspi.StateType
    app = _app()
    out: list = []
    if app is None:
        return out

    def walk(o, d: int = 0) -> None:
        if o is None or d > 60:
            return
        try:
            st = o.get_state_set()
            role = o.get_role_name()
        except Exception:  # noqa: BLE001
            return
        if not st.contains(S.SHOWING):
            return
        if role in roles:
            try:
                e = o.get_extents(Atspi.CoordType.WINDOW)
                rect = (e.x, e.y, e.width, e.height)
            except Exception:  # noqa: BLE001
                rect = (0, 0, 0, 0)
            out.append((o, role, _text(o), rect, st.contains(S.FOCUSABLE)))
        for i in range(o.get_child_count()):
            walk(o.get_child_at_index(i), d + 1)

    for i in range(app.get_child_count()):
        walk(app.get_child_at_index(i))
    return out


def _atspi_do(o) -> bool:
    try:
        act = o.get_action_iface()
        return bool(act and act.get_n_actions() and act.do_action(0))
    except Exception:  # noqa: BLE001
        return False


def _atspi_activate(names: tuple[str, ...]) -> bool:
    want = {n.lower() for n in names}
    for o, _role, text, _r, _f in _walk(("button", "toggle button")):
        if text.strip().lower() in want and _atspi_do(o):
            return True
    return False


def _atspi_section_more(titles: tuple[str, ...]):
    """El botón "More" que está en la misma línea que el título de sección (p. ej. "Canciones")."""
    items = _walk(("label", "button"))
    want = {t.lower() for t in titles}
    hdr = next((r for o, role, t, r, f in items if role == "label" and t.strip().lower() in want), None)
    if not hdr:
        return None
    more = [(abs(r[1] - hdr[1]), o) for o, role, t, r, f in items
            if role == "button" and t.strip() in MORE and abs(r[1] - hdr[1]) < 20]
    return min(more, key=lambda x: x[0])[1] if more else None


def _atspi_rows() -> list:
    rows = [(r[1], o) for o, role, t, r, f in _walk(("list item",)) if f and r[2] > 200]
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
