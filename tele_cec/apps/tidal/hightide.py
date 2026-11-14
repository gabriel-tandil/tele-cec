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
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ...system.helper import HelperProcess
from ...system.keyboard import wtype_key
from ...system.mpris import Mpris
from ...system.procs import find_pids, graphical_env, invalidate_procs, run_cmd, spawn_scoped
from .atspi import (BACK, COLLECTION, EXPLORE, HOME, LIKE, LYRICS, PLAYER_TAB, SHUFFLE, TRACKS, _atspi, _atspi_activate, _atspi_any_focused, _atspi_do, _atspi_rows, _atspi_section_more,
                    _atspi_toggle_tab)

log = logging.getLogger("music")

# El parche que se le carga a High Tide como usercustomize.py (ver HighTide.write_card_patch). Corre adentro
# de High Tide. No puede llamarse sitecustomize.py: el runtime de GNOME usa el suyo para agregar las librerías
# de la app (tidalapi…) y uno con el mismo nombre lo taparía. usercustomize se carga además, después de ese.
PATCH_FILE = Path(__file__).resolve().parent / "hightide_patch.py"



class TidalNav(HelperProcess):
    """Flechas que llegan a todos lados en High Tide (nav_helper.py: sigue el foco por accesibilidad).

    Solo se abre mientras se usa el modo navegación de Tidal y se cierra con High Tide: escuchar eventos de
    accesibilidad hace que otras apps (Firefox) activen la suya, y eso gasta CPU en una Pi.
    """

    module = "tele_cec.apps.tidal.nav_helper"
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
            (d / "usercustomize.py").write_text(PATCH_FILE.read_text(encoding="utf-8"), encoding="utf-8")
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
        """Manda un comando al canal de control que abre el parche dentro de High Tide (ver hightide_patch.py).

        Devuelve la respuesta, o None si el canal no está (parche desactivado, High Tide abierto sin tele-cec
        o todavía arrancando). Es la forma segura de cambiar de pantalla: la accesibilidad puede cerrarlo.
        """
        if not self.keyboard_cards:
            return None
        # wait: High Tide recién abierto abre el canal cuando termina de armar la ventana.
        deadline = time.monotonic() + wait
        while True:
            try:
                reader, writer = await asyncio.wait_for(  # respuestas largas: listas de temas en JSON
                    asyncio.open_unix_connection(str(self.control_socket), limit=8 * 1024 * 1024), 2)
                break
            except (OSError, asyncio.TimeoutError):
                if time.monotonic() >= deadline or not self.pids():
                    return None
                await asyncio.sleep(0.5)
        try:
            writer.write(line.encode() + b"\n")
            await writer.drain()
            reply = (await asyncio.wait_for(reader.readline(), timeout)).decode("utf-8", "replace").strip()
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            log.warning("Canal de control de High Tide (%s): %s", line[:60], exc or "sin respuesta")
            return None
        finally:
            writer.close()
        if reply.startswith("error"):
            log.warning("High Tide no pudo hacer «%s»: %s", line, reply[6:])
        return reply

    async def data(self, line: str, timeout: float = 25.0) -> dict:
        """Un comando de datos del canal (biblioteca, búsqueda, cola, poner a sonar): la respuesta en JSON.
        Siempre devuelve un diccionario; si algo falló, con "error" ("login": todavía no entró a la cuenta)."""
        if not self.running():
            return {"error": "cerrado"}
        reply = await self.command(" ".join(line.split()), timeout=timeout, wait=8)
        if reply is None:
            return {"error": "sin canal de control"}
        try:
            out = json.loads(reply)
        except ValueError:
            return {"error": reply[:200]}
        return out if isinstance(out, dict) else {"error": "respuesta inesperada"}

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
        """Flecha en el modo navegación: con Tab de respaldo donde GTK no mueve el foco (ver nav_helper.py)."""
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
