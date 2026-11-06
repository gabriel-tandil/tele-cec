"""Lanzamiento del navegador (Chromium o Firefox) con el puerto de control habilitado.

- Chromium: Chrome DevTools Protocol (cdp.py).
- Firefox: WebDriver BiDi (bidi.py). Admite una sola sesión de control a la vez.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from . import bidi, cdp

log = logging.getLogger("browser")

ENGINES = ("chromium", "firefox")
BINARIES = {
    "chromium": ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable"),
    "firefox": ("firefox", "firefox-esr"),
}
NAMES = {"chromium": "Chromium", "firefox": "Firefox"}

# Preferencias que netflix-cec escribe en el user.js del perfil de Firefox.
FIREFOX_PREFS: dict[str, Any] = {
    # Las "preferencias recomendadas" del agente remoto son para pruebas automáticas (apagan
    # Safe Browsing, el guardado de contraseñas, la protección contra rastreo…): no las queremos.
    "remote.prefs.recommended": False,
    # DRM (Widevine) y reproducción automática.
    "media.eme.enabled": True,
    "media.gmp-widevinecdm.enabled": True,
    "media.gmp-widevinecdm.visible": True,
    "media.autoplay.default": 0,
    "media.autoplay.blocking_policy": 0,
    # Sin carteles de primer inicio, de navegador predeterminado ni de restaurar la sesión.
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "browser.sessionstore.resume_from_crash": False,
    "browser.tabs.warnOnClose": False,
    "browser.warnOnQuit": False,
    "browser.translations.automaticallyPopup": False,
    # Nada encima del video: ni el botón de imagen en imagen ni el aviso de pantalla completa.
    "media.videocontrols.picture-in-picture.video-toggle.enabled": False,
    "full-screen-api.warning.timeout": 0,
}
FIREFOX_PERF_PREFS: dict[str, Any] = {
    "ui.prefersReducedMotion": 1,
    "general.smoothScroll": False,
}

# Variables que inyecta un snap (p. ej. la terminal de VS Code instalado como snap) y que
# rompen la parte gráfica de otros programas: el navegador se lanza sin ellas.
SNAP_LEAKS = ("GTK_PATH", "GTK_EXE_PREFIX", "GIO_MODULE_DIR", "LOCPATH", "GDK_PIXBUF_MODULE_FILE",
              "GDK_PIXBUF_MODULEDIR", "GTK_IM_MODULE_FILE", "GSETTINGS_SCHEMA_DIR")


def find_binary(configured: str = "", engine: str = "chromium") -> str | None:
    if configured:
        return configured if (os.path.isabs(configured) or shutil.which(configured)) else None
    for name in BINARIES.get(engine, BINARIES["chromium"]):
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
    if env.get("SNAP_NAME"):
        for k in SNAP_LEAKS:
            env.pop(k, None)
        for k in [k for k in env if k.endswith("_VSCODE_SNAP_ORIG")]:
            env[k.removesuffix("_VSCODE_SNAP_ORIG")] = env.pop(k)
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


def _js_value(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def write_user_js(profile: Path, prefs: dict[str, Any]) -> None:
    lines = ["// Lo genera netflix-cec cada vez que abre Firefox: los cambios a mano se pierden.",
             "// Para agregar o cambiar preferencias usá [browser.firefox] prefs en config.toml."]
    lines += [f"user_pref({_js_value(k)}, {_js_value(v)});" for k, v in prefs.items()]
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "user.js").write_text("\n".join(lines) + "\n", encoding="utf-8")


class Browser:
    MIN_RELAUNCH_INTERVAL = 15.0
    MAX_FAILED_LAUNCHES = 5      # lanzamientos sin conexión antes de enfriar
    COOLDOWN = 300.0

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.engine = str(cfg.get("engine") or "chromium").lower()
        if self.engine not in ENGINES:
            log.warning("browser.engine=%r inválido (chromium/firefox); uso chromium", self.engine)
            self.engine = "chromium"
        self.name = NAMES[self.engine]
        self.fx: dict = cfg.get("firefox") or {}
        # Firefox con control remoto expone navigator.webdriver = true desde que abre. Para que Netflix
        # no lo vea nunca, se abre en about:blank y el controller navega después de instalar el script
        # que lo oculta (pending_url es a dónde ir).
        self.hide_webdriver = self.firefox and bool(self.fx.get("hide_webdriver", True))
        self.pending_url: str | None = None
        self.port = int(cfg.get("debug_port", 9222))
        self.proc: subprocess.Popen | None = None
        self._last_launch = 0.0
        self._failed = 0
        self._cooldown_until = 0.0
        self._no_display_logged = -1e9

    @property
    def firefox(self) -> bool:
        return self.engine == "firefox"

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
        args.append(url)
        return args

    def _firefox_args(self, binary: str, url: str) -> list[str]:
        # --no-remote: una instancia propia, independiente del Firefox de siempre del usuario.
        args = [binary, "--no-remote", "--profile", str(self.user_data_dir),
                "--remote-debugging-port", str(self.port)]
        if self.cfg.get("kiosk", True):
            args.append("--kiosk")
        args += self._extra(self.fx.get("extra_args"), args)
        if self.hide_webdriver:
            self.pending_url = url
            url = "about:blank"
        args.append(url)
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

    def prepare_profile(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        if self.firefox:
            write_user_js(self.user_data_dir, self.firefox_prefs())
        else:
            self._mark_clean_exit()

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
        cmd = args
        if shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"):
            cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                   f"--unit=netflix-{self.engine}-{int(time.time())}", "--"] + args
        log.info("Abriendo %s: %s", self.name, " ".join(args))
        try:
            self.proc = self._spawn(cmd, env)
            if cmd is not args:
                # Si systemd-run no puede crear el scope (sin bus de usuario) sale enseguida con error.
                await asyncio.sleep(1.5)
                rc = self.proc.poll()
                if rc not in (None, 0):
                    log.warning("systemd-run terminó con código %s; lanzo %s directamente", rc, self.name)
                    self.proc = self._spawn(args, env)
        except OSError as exc:
            log.error("No pude abrir %s: %s", self.name, exc)
            return False

        ok = await self._wait_alive(25)
        if not ok:
            log.error("%s no abrió el puerto %d. ¿Hay otro %s usando %s sin el puerto de control?",
                      self.name, self.port, self.name, self.user_data_dir)
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
            if not args or "-contentproc" in args or "systemd-run" in os.path.basename(args[0]):
                continue
            if want in args or f"--user-data-dir={want}" in args:
                pids.append(int(d.name))
        return pids

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
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for _ in range(40):
            if not await self.alive() and not self.profile_pids():
                break
            await asyncio.sleep(0.25)
        else:
            for pid in self.profile_pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            await asyncio.sleep(1)
        self._last_launch = 0.0
        return await self.launch()
