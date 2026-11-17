"""YouTube para TV (youtube.com/tv) en un navegador aparte, con su propio perfil.

Usa el mismo navegador que Netflix (browser.engine: Firefox por defecto, o Chromium), salvo que
[apps.youtube] engine diga otro.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any

from ...system.mpris import Mpris
from ...system.procs import find_pids, graphical_env
from ..base import ProcActivity
from ..netflix import bidi, cdp
from ..webprofile import FIREFOX_PERF_PREFS, FIREFOX_PREFS, NAMES, find_binary, mark_clean_exit, write_user_js

log = logging.getLogger("actividades")

DEBUG_PORT = 9224   # canal de control de la página (Netflix usa 9222 y el Navegador 9223); solo escucha en 127.0.0.1

# CC del control: el reproductor de YouTube para TV (probado en la Pi) no tiene toggleSubtitles como el de
# escritorio. toggleSubtitlesOn() solo los prende; para apagarlos, setOption('captions', 'track', {}) (o
# unloadModule('captions')). La tecla «c» ahí abre la búsqueda.
SUBTITLES_JS = """(() => {
  const p = document.querySelector('.html5-video-player');
  const v = document.querySelector('video');
  if (!p || typeof p.toggleSubtitlesOn !== 'function') return JSON.stringify({error: 'noplayer'});
  if (!v || !(v.currentSrc || v.src)) return JSON.stringify({error: 'novideo'});
  let n = 0;
  try { n = ((p.getAudioTrack && p.getAudioTrack()) || {}).captionTracks?.length || 0; } catch (e) {}
  try { n = n || (p.getOption('captions', 'tracklist') || []).length; } catch (e) {}
  if (p.isSubtitlesOn()) {
    p.setOption('captions', 'track', {});   // apagar (toggleSubtitlesOn solo prende)
    if (p.isSubtitlesOn()) p.unloadModule('captions');
  } else {
    if (!n) return JSON.stringify({error: 'none'});
    p.toggleSubtitlesOn();
  }
  const on = !!p.isSubtitlesOn();
  let t = {};
  try { t = on ? (p.getOption('captions', 'track') || {}) : {}; } catch (e) {}
  return JSON.stringify({on, name: t.displayName || t.languageName || ''});
})()"""


# Canal ±: el video siguiente / anterior de la lista (el reproductor de YouTube tiene nextVideo y previousVideo).
STEP_JS = """((dir) => {
  const p = document.querySelector('.html5-video-player');
  const fn = p && p[dir > 0 ? 'nextVideo' : 'previousVideo'];
  if (typeof fn !== 'function') return JSON.stringify({error: 'noplayer'});
  fn.call(p);
  return JSON.stringify({ok: true});
})(%d)"""


class NotConnected(Exception):
    """YouTube no tiene el canal de control (lo abrió una versión anterior, o debug_port = 0)."""


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
        self.reduce_effects = bool(browser_cfg.get("reduce_effects", True))  # como en Netflix
        # El binario: el de [apps.youtube] o, si no, el mismo que usa Netflix para ese navegador.
        fx = browser_cfg.get("firefox") or {}
        self.binary_cfg = str(cfg.get("binary") or (fx.get("binary") if self.firefox else browser_cfg.get("binary"))
                              or "")
        default_profile = "~/.config/firefox-youtube" if self.firefox else "~/.config/chromium-youtube"
        self.profile = Path(os.path.expanduser(str(cfg.get("profile") or default_profile)))
        self.mpris = Mpris(str(cfg.get("mpris_player") or engine), float(cfg.get("volume_step", 0.05)))
        # El canal de control de la página (WebDriver BiDi en Firefox, DevTools en Chromium), solo para lo que no
        # se puede con teclas (los subtítulos). 0 = sin canal.
        self.debug_port = int(cfg.get("debug_port", DEBUG_PORT) or 0)
        self._js_lock = asyncio.Lock()

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
        if self.reduce_effects:
            prefs.update(FIREFOX_PERF_PREFS)
        prefs["general.useragent.override"] = self.user_agent
        if self.cfg.get("force_h264", True):
            # Como la extensión h264ify: la Pi decodifica H.264 por hardware; VP9 y AV1 van por CPU.
            prefs["media.mediasource.vp9.enabled"] = False
            prefs["media.av1.enabled"] = False
        extra = self.cfg.get("prefs") or {}
        if isinstance(extra, dict):
            prefs.update(extra)
        return prefs

    def search_url(self, text: str) -> str:
        """La búsqueda de YouTube para TV con ese texto ya escrito (#/search?q=…, probado en la Pi)."""
        return self.url.split("#", 1)[0] + "#/search?q=" + urllib.parse.quote(text.strip())

    def args(self, binary: str, url: str | None = None) -> list[str]:
        url = url or self.url
        if self.firefox:
            args = [binary, "--no-remote", "--profile", str(self.profile)]
            if self.debug_port:
                args += ["--remote-debugging-port", str(self.debug_port)]
            if self.cfg.get("kiosk", True):
                args.append("--kiosk")
            return args + [url]
        args = [binary, f"--user-data-dir={self.profile}", f"--user-agent={self.user_agent}",
                "--no-first-run", "--no-default-browser-check", "--noerrdialogs", "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble", "--disable-features=Translate", "--password-store=basic",
                "--autoplay-policy=no-user-gesture-required"]
        if self.debug_port:
            args.append(f"--remote-debugging-port={self.debug_port}")
        if graphical_env().get("WAYLAND_DISPLAY"):
            # Wayland nativo: las teclas van con wtype, que no sirve con apps X11 (por Xwayland les llegan como
            # Escape). Chromium 156 arranca en X11 si no se le pide (ignora --ozone-platform-hint=auto).
            args.append("--ozone-platform=wayland")
        if self.cfg.get("force_h264", True) and (H264_EXTENSION / "manifest.json").is_file():
            args.append(f"--load-extension={H264_EXTENSION}")
        if self.reduce_effects:
            args += ["--force-prefers-reduced-motion", "--disable-smooth-scrolling"]
        args.append("--kiosk" if self.cfg.get("kiosk", True) else "--start-fullscreen")
        extra = self.cfg.get("extra_args") or []
        args += extra.split() if isinstance(extra, str) else [str(a) for a in extra]
        return args + [url]

    def prepare_profile(self) -> None:
        if self.firefox:
            write_user_js(self.profile, self.prefs())
        else:
            self.profile.mkdir(parents=True, exist_ok=True)
            mark_clean_exit(self.profile)

    async def open(self, url: str | None = None) -> bool:
        """Abre YouTube para TV (o esa dirección, p. ej. una búsqueda: search_url)."""
        binary = self.binary()
        if not binary:
            log.error("YouTube: no encontré %s", NAMES[self.engine])
            return False
        try:
            await asyncio.to_thread(self.prepare_profile)
        except OSError as exc:
            log.error("YouTube: no pude preparar el perfil %s: %s", self.profile, exc)
            return False
        if not await self.spawn(self.args(binary, url)):
            return False
        # El navegador tarda en abrir la ventana: el menú sigue mostrando «Abriendo YouTube…» un rato.
        await asyncio.sleep(float(self.cfg.get("open_wait_s", 10)))
        return True

    async def pause(self) -> None:
        await self.mpris.ctl("pause")

    async def js(self, expression: str, timeout: float = 8.0) -> Any:
        """Evalúa una expresión en la página de YouTube con una sesión de control corta: se abre, se usa y se
        cierra (Firefox admite una sola sesión; así nunca queda tomada). NotConnected si no hay canal."""
        if not self.debug_port:
            raise NotConnected("debug_port = 0")
        async with self._js_lock:
            if self.firefox:
                page = await bidi.connect_firefox(self.debug_port)
            else:
                page = await cdp.connect_chromium(self.debug_port)
            if page is None:
                raise NotConnected(f"nada escuchando en el puerto {self.debug_port}")
            try:
                return await page.evaluate(expression, timeout)
            finally:
                await page.close()

    async def step(self, direction: int) -> bool:
        """Canal ±: el video siguiente (1) o el anterior (-1). Por el reproductor; si no hay canal, por MPRIS."""
        try:
            raw = await self.js(STEP_JS % (1 if direction > 0 else -1))
            r = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            if r.get("ok"):
                return True
            log.info("YouTube: sin reproductor para pasar de video (%s)", r)
        except (NotConnected, cdp.CDPError, OSError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            log.info("YouTube: paso de video por MPRIS (%s)", exc)
        return await self.mpris.ctl("next" if direction > 0 else "previous")

    async def home(self) -> bool:
        """Azul: el inicio de YouTube (por el canal de control de la página)."""
        try:
            await self.js(f"(() => {{ location.assign({json.dumps(self.url + '#/')}); return 'ok'; }})()")
            return True
        except (NotConnected, cdp.CDPError, OSError, asyncio.TimeoutError) as exc:
            log.info("YouTube: no pude ir al inicio (%s)", exc)
            return False

    async def toggle_subtitles(self) -> str:
        """CC: prende o apaga los subtítulos del video. Devuelve el aviso para la pantalla."""
        try:
            raw = await self.js(SUBTITLES_JS)
        except NotConnected as exc:
            log.info("YouTube: sin canal de control para los subtítulos (%s)", exc)
            return "Para los subtítulos con CC, cerrá YouTube y abrilo de nuevo"
        except (cdp.CDPError, OSError, asyncio.TimeoutError) as exc:
            log.warning("YouTube: no pude cambiar los subtítulos: %s", exc)
            return "No pude cambiar los subtítulos"
        try:
            r = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (ValueError, TypeError):
            r = {"error": "?"}
        log.info("YouTube: subtítulos %s", r)
        err = r.get("error")
        if err == "none":
            return "Este video no tiene subtítulos"
        if err:
            return "Los subtítulos, con un video puesto"
        return f"Subtítulos: {r.get('name') or 'sí'}" if r.get("on") else "Sin subtítulos"
