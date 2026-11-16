"""Lanzamiento del navegador (Chromium o Firefox) con el puerto de control habilitado.

- Chromium: Chrome DevTools Protocol (cdp.py).
- Firefox: WebDriver BiDi (bidi.py). Admite una sola sesión de control a la vez.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


from . import bidi, cdp
from ..webprofile import (BINARIES, ENGINES, FIREFOX_PERF_PREFS, FIREFOX_PREFS, NAMES, apply_extension_states,
                          find_binary, mark_clean_exit, write_user_js)
from ...system.procs import graphical_env, has_display, spawn_scoped

# Primera página al abrir el navegador: negra y vacía (sin destello blanco). «Abriendo Netflix…» lo muestra
# el panel del menú; el controller navega a Netflix apenas instala su script en la pestaña. Es un archivo
# porque Firefox no abre direcciones data: desde la línea de comandos.
BLANK = Path(__file__).resolve().parent / "blank.html"

log = logging.getLogger("browser")


class Browser:
    MIN_RELAUNCH_INTERVAL = 15.0
    MAX_FAILED_LAUNCHES = 5      # lanzamientos sin conexión antes de enfriar
    COOLDOWN = 300.0

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.engine = str(cfg.get("engine") or "firefox").lower()
        if self.engine not in ENGINES:
            log.warning("browser.engine=%r inválido (firefox/chromium); uso firefox", self.engine)
            self.engine = "firefox"
        self.name = NAMES[self.engine]
        self.fx: dict = cfg.get("firefox") or {}
        # El navegador se abre en una página negra vacía y el controller navega a Netflix después de
        # instalar su script (pending_url es a dónde ir). Así, en Firefox, Netflix nunca ve el
        # navigator.webdriver = true que pone el control remoto: el script lo oculta antes.
        self.hide_webdriver = self.firefox and bool(self.fx.get("hide_webdriver", True))
        self.pending_url: str | None = None
        self.port = int(cfg.get("debug_port", 9222))
        self.unit = str(cfg.get("unit") or f"netflix-{self.engine}")  # el scope de systemd en que se lanza
        self.proc: subprocess.Popen | None = None
        self._last_launch = 0.0
        self._failed = 0
        self._cooldown_until = 0.0
        self._no_display_logged = -1e9

    @property
    def firefox(self) -> bool:
        return self.engine == "firefox"

    @staticmethod
    def is_blank(url: str) -> bool:
        """¿Es la página negra del arranque (o una vacía)? Entonces todavía falta abrir Netflix."""
        if url in ("", "about:blank"):
            return True
        return url.startswith("file:") and url.split("?")[0].endswith("/" + BLANK.name)

    def _first_page(self, url: str) -> str:
        """Primera página al abrir: la negra vacía; el controller navega a `url` después."""
        self.pending_url = url
        return BLANK.as_uri() if BLANK.is_file() else "about:blank"

    @property
    def user_data_dir(self) -> Path:
        if self.firefox:
            return Path(self.fx.get("profile") or "~/.config/firefox-netflix").expanduser()
        return Path(self.cfg.get("user_data_dir") or "~/.config/chromium-netflix").expanduser()

    def binary(self) -> str | None:
        configured = self.fx.get("binary", "") if self.firefox else self.cfg.get("binary", "")
        return find_binary(configured, self.engine)

    # ------------------------------------------------------------ línea de comandos
    def args(self, url: str | None = None) -> list[str]:
        binary = self.binary()
        if not binary:
            raise FileNotFoundError(f"No encuentro {self.name} ({' / '.join(BINARIES[self.engine])})")
        url = url or self.cfg.get("start_url") or "https://www.netflix.com/browse"
        if self.firefox:
            return self._firefox_args(binary, url)
        args = [
            binary,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--noerrdialogs",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--disable-features=Translate",
            "--autoplay-policy=no-user-gesture-required",
            "--overscroll-history-navigation=0",
        ]
        args += self.perf_args()
        if self.cfg.get("kiosk", True):
            args.append("--kiosk")
        else:
            args.append("--start-fullscreen")
        args += self._extra(self.cfg.get("extra_args"), args)
        args.append(self._first_page(url))
        return args

    def _firefox_args(self, binary: str, url: str) -> list[str]:
        # --no-remote: una instancia propia, independiente del Firefox de siempre del usuario.
        args = [binary, "--no-remote", "--profile", str(self.user_data_dir),
                "--remote-debugging-port", str(self.port)]
        if self.cfg.get("kiosk", True):
            args.append("--kiosk")
        args += self._extra(self.fx.get("extra_args"), args)
        args.append(self._first_page(url))
        return args

    @staticmethod
    def _extra(extra: Any, current: list[str]) -> list[str]:
        extra = extra or []
        if isinstance(extra, str):
            extra = extra.split()
        return [a for a in extra if a not in current]

    def perf_args(self) -> list[str]:
        """Flags de Chromium para gastar menos GPU/CPU en animaciones (browser.reduce_effects)."""
        if self.firefox or not self.cfg.get("reduce_effects", True):
            return []
        return ["--force-prefers-reduced-motion", "--disable-smooth-scrolling"]

    def firefox_prefs(self) -> dict[str, Any]:
        prefs = dict(FIREFOX_PREFS)
        if self.cfg.get("reduce_effects", True):
            prefs.update(FIREFOX_PERF_PREFS)
        user = self.fx.get("prefs") or {}
        if isinstance(user, dict):
            prefs.update(user)
        return prefs

    # ------------------------------------------------------------ conexión
    async def alive(self) -> bool:
        return await (bidi.endpoint_alive(self.port) if self.firefox else cdp.endpoint_alive(self.port))

    async def connect(self) -> Any:
        """Conecta con la pestaña de Netflix. None si el navegador no está escuchando.

        Con Firefox puede lanzar cdp.SessionBusy si otro cliente tiene la única sesión.
        """
        if self.firefox:
            return await bidi.connect_firefox(self.port)
        return await cdp.connect_chromium(self.port)

    def notify_connected(self) -> None:
        self._failed = 0

    def can_autolaunch(self) -> bool:
        now = time.monotonic()
        if now < self._cooldown_until:
            return False
        return now - self._last_launch >= self.MIN_RELAUNCH_INTERVAL

    # ------------------------------------------------------------ lanzamiento
    def prepare_profile(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        if self.firefox:
            write_user_js(self.user_data_dir, self.firefox_prefs())
            apply_extension_states(self.user_data_dir, self.fx.get("disable_extensions") or [])
        else:
            mark_clean_exit(self.user_data_dir)

    async def launch(self, url: str | None = None) -> bool:
        """Abre el navegador si no está escuchando en el puerto. Devuelve True si quedó disponible."""
        if await self.alive():
            return True
        if self.proc and self.proc.poll() is None and time.monotonic() - self._last_launch < 30:
            log.info("%s ya se está abriendo…", self.name)
            return await self._wait_alive(20)

        env = graphical_env()
        self._last_launch = time.monotonic()
        if not has_display(env):
            # Pasa si el servicio arranca antes que el escritorio: se reintenta más tarde.
            if time.monotonic() - self._no_display_logged > 300:
                self._no_display_logged = time.monotonic()
                log.warning("Todavía no hay sesión gráfica (WAYLAND_DISPLAY/DISPLAY); no puedo abrir %s", self.name)
            return False

        try:
            args = self.args(url)
        except FileNotFoundError as exc:
            log.error("%s", exc)
            return False
        self.prepare_profile()
        self._failed += 1
        if self._failed > self.MAX_FAILED_LAUNCHES:
            self._cooldown_until = time.monotonic() + self.COOLDOWN
            self._failed = 0
            log.error("%s falló %d veces seguidas; espero %.0f min antes de reintentar solo",
                      self.name, self.MAX_FAILED_LAUNCHES, self.COOLDOWN / 60)

        # Se lanza en un scope propio de systemd: así reiniciar o detener este
        # servicio no cierra el navegador.
        log.info("Abriendo %s: %s", self.name, " ".join(args))
        try:
            self.proc = await spawn_scoped(args, self.unit, env)
        except OSError as exc:
            log.error("No pude abrir %s: %s", self.name, exc)
            return False

        ok = await self._wait_alive(25)
        if not ok:
            log.error("%s no abrió el puerto %d. ¿Hay otro %s usando %s sin el puerto de control?",
                      self.name, self.port, self.name, self.user_data_dir)
        return ok

    async def _wait_alive(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if await self.alive():
                return True
            await asyncio.sleep(0.5)
        return False

    # ------------------------------------------------------------ Firefox con la sesión tomada
    def profile_pids(self) -> list[int]:
        """PIDs del proceso principal del navegador que usa nuestro perfil."""
        want = str(self.user_data_dir)
        pids = []
        for d in Path("/proc").glob("[0-9]*"):
            try:
                argv = (d / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            args = [a.decode("utf-8", "replace") for a in argv if a]
            if (not args or "-contentproc" in args or "systemd-run" in os.path.basename(args[0])
                    or "--type=" in " ".join(args)):
                continue
            # Chromium reescribe su línea de comandos en /proc como un solo texto separado por espacios.
            if want in args or f"--user-data-dir={want} " in " ".join(args) + " ":
                pids.append(int(d.name))
        return pids

    async def close(self, timeout: float = 30.0) -> bool:
        """Cierra ordenadamente el navegador de nuestro perfil (SIGTERM; si no responde, SIGKILL).

        En una Pi lenta Firefox puede tardar bastante más de 10 s en cerrarse prolijo.
        """
        pids = self.profile_pids()
        if not pids:
            return False
        log.info("Cierro %s (PID %s)", self.name, ", ".join(map(str, pids)))
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not await self.alive() and not self.profile_pids():
                return True
            await asyncio.sleep(0.25)
        log.warning("%s no se cerró en %.0f s: lo fuerzo", self.name, timeout)
        for pid in self.profile_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        await asyncio.sleep(1)
        return True

    async def restart(self) -> bool:
        """Cierra el navegador de nuestro perfil y lo vuelve a abrir.

        Se usa cuando Firefox quedó con una sesión de control colgada (un cliente que murió sin
        cerrarla): Firefox no la libera nunca y no hay otra forma de recuperar el control.
        """
        pids = self.profile_pids()
        if not pids:
            log.warning("No encontré el proceso de %s con el perfil %s", self.name, self.user_data_dir)
            return False
        log.warning("Reinicio %s (PID %s) para recuperar el control", self.name, ", ".join(map(str, pids)))
        await self.close()
        self._last_launch = 0.0
        return await self.launch()
