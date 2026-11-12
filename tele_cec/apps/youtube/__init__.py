"""YouTube para TV (youtube.com/tv) en un navegador aparte, con su propio perfil.

Usa el mismo navegador que Netflix (browser.engine: Firefox por defecto, o Chromium), salvo que
[apps.youtube] engine diga otro.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from ...system.mpris import Mpris
from ...system.procs import find_pids, graphical_env
from ..base import ProcActivity
from ..webprofile import FIREFOX_PREFS, NAMES, find_binary, mark_clean_exit, write_user_js

log = logging.getLogger("actividades")

# Como h264ify, para Chromium: una extensión que esconde VP9 y AV1 (en Firefox alcanza con preferencias).
H264_EXTENSION = Path(__file__).resolve().parent / "h264"


class YouTubeActivity(ProcActivity):
    """YouTube para TV (youtube.com/tv) en un Firefox o Chromium aparte, con su propio perfil.

    Es la interfaz de los Smart TV: se maneja de fábrica con flechas, OK y Atrás (Escape), así que las teclas
    del control se le mandan tal cual (wtype; el navegador es una app Wayland). YouTube solo la muestra a
    navegadores que se presentan como TV: por eso el perfil propio se identifica como una (no afecta al de
    Netflix). Se inicia sesión con el celular (yt.be/activate) o se mira como invitado.
    """

    id = "youtube"
    name = "YouTube"
    icon = "youtube.svg"
    context = "youtube"
    start_timeout = 30.0
    # Cómo se presenta a YouTube (con un navegador común, YouTube redirige a la versión de escritorio):
    # Firefox como la consola PS4 (YouTube le sirve la versión para TV que anda con Gecko); Chromium como
    # Cobalt, el motor de las apps de YouTube de los televisores.
    USER_AGENTS = {
        "firefox": ("Mozilla/5.0 (PS4; Leanback Shell) Gecko/20100101 Firefox/65.0 LeanbackShell/01.00.01.75 "
                    "Sony PS4/ (PS4, , no, CH)"),
        "chromium": ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 "
                     "Safari/537.36 Cobalt/25.lts.30.1034943-gold (unlike Gecko), Unknown_TV_Unknown_0/Unknown "
                     "(Unknown, Unknown)"),
    }
    USER_AGENT = USER_AGENTS["firefox"]  # nombre anterior

    def __init__(self, cfg: dict[str, Any] | None = None, browser_cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        browser_cfg = browser_cfg or {}
        self.cfg = cfg
        self.name = str(cfg.get("name") or self.name)
        self.icon = str(cfg.get("icon") or self.icon)
        self.url = str(cfg.get("url") or "https://www.youtube.com/tv")
        engine = str(cfg.get("engine") or browser_cfg.get("engine") or "firefox").strip().lower()
        if engine not in NAMES:
            log.warning("YouTube: navegador %r inválido (firefox/chromium); uso firefox", engine)
            engine = "firefox"
        self.engine = engine
        # El binario: el de [apps.youtube] o, si no, el mismo que usa Netflix para ese navegador.
        fx = browser_cfg.get("firefox") or {}
        self.binary_cfg = str(cfg.get("binary") or (fx.get("binary") if self.firefox else browser_cfg.get("binary"))
                              or "")
        default_profile = "~/.config/firefox-youtube" if self.firefox else "~/.config/chromium-youtube"
        self.profile = Path(os.path.expanduser(str(cfg.get("profile") or default_profile)))
        self.mpris = Mpris(str(cfg.get("mpris_player") or engine), float(cfg.get("volume_step", 0.05)))

    @property
    def firefox(self) -> bool:
        return self.engine == "firefox"

    def binary(self) -> str | None:
        return find_binary(self.binary_cfg, self.engine)

    def installed(self) -> bool:
        return self.binary() is not None

    def pids(self, fresh: bool = False) -> list[int]:
        """El proceso principal del navegador con el perfil de YouTube (sin sus subprocesos)."""
        prof = str(self.profile)
        flag = f"--user-data-dir={prof}"

        def ours(argv: list[str]) -> bool:
            if "systemd-run" in os.path.basename(argv[0]):
                return False
            if self.firefox:
                return prof in argv and "-contentproc" not in argv
            # Chromium reescribe su línea de comandos en /proc como un solo texto separado por espacios.
            cmdline = " ".join(argv) + " "
            return flag + " " in cmdline and "--type=" not in cmdline
        return find_pids(ours, fresh)

    @property
    def user_agent(self) -> str:
        return str(self.cfg.get("user_agent") or self.USER_AGENTS[self.engine])

    def prefs(self) -> dict[str, Any]:
        """Preferencias del perfil de Firefox (user.js)."""
        prefs = dict(FIREFOX_PREFS)
        prefs["general.useragent.override"] = self.user_agent
        if self.cfg.get("force_h264", True):
            # Como la extensión h264ify: la Pi decodifica H.264 por hardware; VP9 y AV1 van por CPU.
            prefs["media.mediasource.vp9.enabled"] = False
            prefs["media.av1.enabled"] = False
        extra = self.cfg.get("prefs") or {}
        if isinstance(extra, dict):
            prefs.update(extra)
        return prefs

    def args(self, binary: str) -> list[str]:
        if self.firefox:
            args = [binary, "--no-remote", "--profile", str(self.profile)]
            if self.cfg.get("kiosk", True):
                args.append("--kiosk")
            return args + [self.url]
        args = [binary, f"--user-data-dir={self.profile}", f"--user-agent={self.user_agent}",
                "--no-first-run", "--no-default-browser-check", "--noerrdialogs", "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble", "--disable-features=Translate", "--password-store=basic",
                "--autoplay-policy=no-user-gesture-required"]
        if graphical_env().get("WAYLAND_DISPLAY"):
            # Wayland nativo: las teclas van con wtype, que no sirve con apps X11 (por Xwayland les llegan como
            # Escape). Chromium 156 arranca en X11 si no se le pide (ignora --ozone-platform-hint=auto).
            args.append("--ozone-platform=wayland")
        if self.cfg.get("force_h264", True) and (H264_EXTENSION / "manifest.json").is_file():
            args.append(f"--load-extension={H264_EXTENSION}")
        args.append("--kiosk" if self.cfg.get("kiosk", True) else "--start-fullscreen")
        extra = self.cfg.get("extra_args") or []
        args += extra.split() if isinstance(extra, str) else [str(a) for a in extra]
        return args + [self.url]

    def prepare_profile(self) -> None:
        if self.firefox:
            write_user_js(self.profile, self.prefs())
        else:
            self.profile.mkdir(parents=True, exist_ok=True)
            mark_clean_exit(self.profile)

    async def open(self) -> bool:
        binary = self.binary()
        if not binary:
            log.error("YouTube: no encontré %s", NAMES[self.engine])
            return False
        try:
            await asyncio.to_thread(self.prepare_profile)
        except OSError as exc:
            log.error("YouTube: no pude preparar el perfil %s: %s", self.profile, exc)
            return False
        if not await self.spawn(self.args(binary)):
            return False
        # El navegador tarda en abrir la ventana: el menú sigue mostrando «Abriendo YouTube…» un rato.
        await asyncio.sleep(float(self.cfg.get("open_wait_s", 10)))
        return True

    async def pause(self) -> None:
        await self.mpris.ctl("pause")
