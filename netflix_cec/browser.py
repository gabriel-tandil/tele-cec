"""Lanzamiento de Chromium con el puerto de DevTools habilitado."""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from .cdp import endpoint_alive

log = logging.getLogger("browser")


def find_binary(configured: str = "") -> str | None:
    if configured:
        return configured if (os.path.isabs(configured) or shutil.which(configured)) else None
    for name in ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    return None


def graphical_env() -> dict[str, str]:
    """Entorno para abrir ventanas aunque el servicio no haya heredado WAYLAND_DISPLAY/DISPLAY.

    Raspberry Pi OS Bookworm usa Wayland (labwc/wayfire). Un servicio de usuario no
    siempre recibe esas variables, así que se buscan los sockets en XDG_RUNTIME_DIR.
    """
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    if not env.get("WAYLAND_DISPLAY"):
        sockets = sorted(p for p in glob.glob(os.path.join(runtime, "wayland-*")) if not p.endswith(".lock"))
        if sockets:
            env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
    if not env.get("DISPLAY"):
        xsockets = sorted(glob.glob("/tmp/.X11-unix/X*"))
        if xsockets:
            env["DISPLAY"] = ":" + os.path.basename(xsockets[0])[1:]
    if not env.get("XAUTHORITY"):
        xauth = Path.home() / ".Xauthority"
        if xauth.exists():
            env["XAUTHORITY"] = str(xauth)
    return env


def has_display(env: dict[str, str]) -> bool:
    return bool(env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"))


class Browser:
    MIN_RELAUNCH_INTERVAL = 15.0
    MAX_FAILED_LAUNCHES = 5      # lanzamientos sin conexión antes de enfriar
    COOLDOWN = 300.0

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.port = int(cfg.get("debug_port", 9222))
        self.proc: subprocess.Popen | None = None
        self._last_launch = 0.0
        self._failed = 0
        self._cooldown_until = 0.0
        self._no_display_logged = -1e9

    @property
    def user_data_dir(self) -> Path:
        return Path(self.cfg.get("user_data_dir") or "~/.config/chromium-netflix").expanduser()

    def args(self, url: str | None = None) -> list[str]:
        binary = find_binary(self.cfg.get("binary", ""))
        if not binary:
            raise FileNotFoundError("No encuentro Chromium (chromium-browser / chromium)")
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
        extra = self.cfg.get("extra_args") or []
        if isinstance(extra, str):
            extra = extra.split()
        args += [a for a in extra if a not in args]
        args.append(url or self.cfg.get("start_url") or "https://www.netflix.com/browse")
        return args

    def perf_args(self) -> list[str]:
        """Flags para gastar menos GPU/CPU en animaciones (browser.reduce_effects)."""
        if not self.cfg.get("reduce_effects", True):
            return []
        return ["--force-prefers-reduced-motion", "--disable-smooth-scrolling"]

    def notify_connected(self) -> None:
        self._failed = 0

    def can_autolaunch(self) -> bool:
        now = time.monotonic()
        if now < self._cooldown_until:
            return False
        return now - self._last_launch >= self.MIN_RELAUNCH_INTERVAL

    def _mark_clean_exit(self) -> None:
        """Evita el cartel "Chromium no se cerró correctamente" después de un corte."""
        prefs = self.user_data_dir / "Default" / "Preferences"
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        profile = data.setdefault("profile", {})
        if profile.get("exit_type") != "Normal" or profile.get("exited_cleanly") is not True:
            profile["exit_type"] = "Normal"
            profile["exited_cleanly"] = True
            try:
                prefs.write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                pass

    async def launch(self, url: str | None = None) -> bool:
        """Abre Chromium si no está escuchando en el puerto. Devuelve True si quedó disponible."""
        if await endpoint_alive(self.port):
            return True
        if self.proc and self.proc.poll() is None and time.monotonic() - self._last_launch < 30:
            log.info("Chromium ya se está abriendo…")
            return await self._wait_alive(20)

        env = graphical_env()
        self._last_launch = time.monotonic()
        if not has_display(env):
            # Pasa si el servicio arranca antes que el escritorio: se reintenta más tarde.
            if time.monotonic() - self._no_display_logged > 300:
                self._no_display_logged = time.monotonic()
                log.warning("Todavía no hay sesión gráfica (WAYLAND_DISPLAY/DISPLAY); no puedo abrir Chromium")
            return False

        try:
            args = self.args(url)
        except FileNotFoundError as exc:
            log.error("%s", exc)
            return False
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._mark_clean_exit()
        self._failed += 1
        if self._failed > self.MAX_FAILED_LAUNCHES:
            self._cooldown_until = time.monotonic() + self.COOLDOWN
            self._failed = 0
            log.error("Chromium falló %d veces seguidas; espero %.0f min antes de reintentar solo",
                      self.MAX_FAILED_LAUNCHES, self.COOLDOWN / 60)

        # Se lanza en un scope propio de systemd: así reiniciar o detener este
        # servicio no cierra el navegador.
        cmd = args
        if shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"):
            cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                   f"--unit=netflix-chromium-{int(time.time())}", "--"] + args
        log.info("Abriendo Chromium: %s", " ".join(args))
        try:
            self.proc = self._spawn(cmd, env)
            if cmd is not args:
                # Si systemd-run no puede crear el scope (sin bus de usuario) sale enseguida con error.
                await asyncio.sleep(1.5)
                rc = self.proc.poll()
                if rc not in (None, 0):
                    log.warning("systemd-run terminó con código %s; lanzo Chromium directamente", rc)
                    self.proc = self._spawn(args, env)
        except OSError as exc:
            log.error("No pude abrir Chromium: %s", exc)
            return False

        ok = await self._wait_alive(25)
        if not ok:
            log.error("Chromium no abrió el puerto %d. ¿Hay otro Chromium usando %s sin el puerto de depuración?",
                      self.port, self.user_data_dir)
        return ok

    @staticmethod
    def _spawn(cmd: list[str], env: dict[str, str]) -> subprocess.Popen:
        return subprocess.Popen(  # noqa: S603
            cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )

    async def _wait_alive(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if await endpoint_alive(self.port):
                return True
            await asyncio.sleep(0.5)
        return False
