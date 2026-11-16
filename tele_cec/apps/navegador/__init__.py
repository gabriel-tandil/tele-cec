"""Navegador: cualquier página web en la TV, manejada con el control remoto.

Firefox (o Chromium) en pantalla completa con un perfil propio (~/.config/firefox-navegador), lanzado y controlado
como el de Netflix (apps/netflix/browser.py, con otro puerto): tele-cec le inyecta nav.js en cada página y con eso
las flechas van al enlace o botón más cercano (con un marco), OK lo abre, Atrás vuelve, y en un campo de texto los
números escriben como en un celular viejo (t9.py). 0 abre el cuadro «Ir a» (una dirección o una búsqueda). La
página de inicio es un archivo propio con los sitios de [apps.navegador] bookmarks en cuadros grandes; 1 – 9 los
abren directo.

Para que vaya rápido en la Pi: las mismas preferencias que Netflix (sin animaciones, sin trabajo de fondo), las
páginas más grandes (scale: todo al 150 %, para leer desde el sillón), sin ventanas nuevas ni avisos, y las
extensiones que se le copien (extensions: p. ej. uBlock Origin, que saca la publicidad, lo más pesado).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any

from ...system.procs import graphical_env
from ..base import Activity
from ..netflix.browser import Browser
from ..netflix.cdp import CDPClosed, CDPError, ContextLost, SessionBusy
from ..webprofile import NAMES
from .t9 import T9

log = logging.getLogger("actividades")

NAV_JS = Path(__file__).resolve().parent / "nav.js"
START_PAGE = "inicio.html"
CONNECT_TIMEOUT_S = 90      # Firefox en la Pi acepta la sesión de control recién cuando terminó de abrir la ventana
DEFAULT_SEARCH = "https://www.google.com/search?q={q}"
DEFAULT_BOOKMARKS = [
    {"name": "Google", "url": "https://www.google.com"},
    {"name": "Wikipedia", "url": "https://es.wikipedia.org"},
    {"name": "YouTube", "url": "https://www.youtube.com"},
    {"name": "Mercado Libre", "url": "https://www.mercadolibre.com.ar"},
]
# Enlaces y botones por los que se mueven las flechas, y lo que no (lo invisible lo filtra el script).
FOCUSABLE = ('a[href], button, input, select, textarea, summary, [role="button"], [role="link"], [role="tab"], '
             '[role="menuitem"], [role="option"], [role="checkbox"], [role="switch"], [tabindex]:not([tabindex="-1"]), '
             '[onclick], video[controls]')
IGNORE = 'input[type="hidden"], [aria-hidden="true"] *, #tcn-go *'
GO_HINT = "OK ir · Atrás cerrar · 2–9 letras · 0 espacio · 1 signos · rojo borrar · amarillo ABC/123"
# Preferencias propias del Navegador (además de las de Netflix: ver webprofile.FIREFOX_PREFS).
NAV_PREFS: dict[str, Any] = {
    "browser.link.open_newwindow": 1,              # los enlaces «en otra ventana» van en la misma pestaña (kiosk)
    "browser.link.open_newwindow.restriction": 0,
    "browser.startup.page": 0,
    "dom.disable_open_during_load": True,          # sin ventanas emergentes
    "dom.popup_maximum": 0,
    "dom.webnotifications.enabled": False,
    "permissions.default.desktop-notification": 2,  # sin preguntar por avisos ni ubicación (no hay quién conteste)
    "permissions.default.geo": 2,
    "privacy.trackingprotection.enabled": True,     # menos rastreadores = páginas más livianas
    "browser.contentblocking.category": "strict",
    "extensions.autoDisableScopes": 0,              # las extensiones copiadas al perfil quedan habilitadas solas
    "extensions.enabledScopes": 5,
    "browser.fullscreen.autohide": True,
    "full-screen-api.ignore-widgets": True,
    "browser.tabs.closeWindowWithLastTab": False,
    "accessibility.typeaheadfind.enablesound": False,
}


def build_nav_script(cfg: dict[str, Any], reduce_effects: bool) -> tuple[str, str]:
    src = NAV_JS.read_text(encoding="utf-8")
    js_cfg = {"focusable": str(cfg.get("focusable") or FOCUSABLE), "ignore": str(cfg.get("ignore") or IGNORE),
              "reduce_effects": reduce_effects, "go_hint": GO_HINT}
    cfg_json = json.dumps(js_cfg, ensure_ascii=False)
    version = hashlib.sha1((src + cfg_json).encode("utf-8")).hexdigest()[:12]
    return src.replace("__NAV_VERSION__", version).replace("__NAV_CONFIG__", cfg_json), version


def bookmarks_from(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """[apps.navegador] bookmarks: [{name, url}] (sin los que no tienen dirección). Sin la lista, unos de muestra."""
    raw = cfg.get("bookmarks")
    if raw is None:
        return [dict(b) for b in DEFAULT_BOOKMARKS]
    out = []
    for i, b in enumerate(raw or []):
        if isinstance(b, str):
            b = {"url": b}
        if not isinstance(b, dict) or not str(b.get("url") or "").strip():
            log.warning("[apps.navegador] bookmarks: el %d.º no tiene url", i + 1)
            continue
        url = str(b["url"]).strip()
        if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
            url = "https://" + url
        name = str(b.get("name") or "").strip() or urllib.parse.urlparse(url).netloc.removeprefix("www.")
        out.append({"name": name, "url": url})
    return out


_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:\d+)?(/\S*)?$", re.IGNORECASE)


def target_url(text: str, search: str = DEFAULT_SEARCH) -> str:
    """Lo que se escribió en «Ir a»: una dirección (con o sin https://) o qué buscar (va al buscador)."""
    text = " ".join(str(text).split())
    if not text:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
        return text
    if " " not in text and _DOMAIN_RE.match(text):
        return "https://" + text
    if "{q}" in search:
        return search.replace("{q}", urllib.parse.quote_plus(text))
    return search + urllib.parse.quote_plus(text)


def allowed_url(url: str) -> bool:
    """Solo páginas web (http, https): nada de file:, about: ni otros esquemas desde el celular."""
    try:
        p = urllib.parse.urlparse(str(url))
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


class WebActivity(Activity):
    id = "navegador"
    name = "Navegador"
    icon = "navegador.svg"
    context = "navegador"
    start_timeout = 30.0

    def __init__(self, cfg: dict[str, Any] | None = None, browser_cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        browser_cfg = browser_cfg or {}
        self.cfg = cfg
        self.name = str(cfg.get("name") or self.name)
        self.icon = str(cfg.get("icon") or self.icon)
        engine = str(cfg.get("engine") or browser_cfg.get("engine") or "firefox").strip().lower()
        if engine not in NAMES:
            log.warning("Navegador: navegador %r inválido (firefox/chromium); uso firefox", engine)
            engine = "firefox"
        self.engine = engine
        self.reduce_effects = bool(browser_cfg.get("reduce_effects", True))
        self.profile = Path(os.path.expanduser(str(cfg.get("profile") or f"~/.config/{engine}-navegador")))
        self.scale = float(cfg.get("scale", 1.5) or 1.0)
        self.search = str(cfg.get("search") or DEFAULT_SEARCH)
        self.home = str(cfg.get("home") or "")
        self.bookmarks = bookmarks_from(cfg)
        self.extensions = [os.path.expanduser(str(x)) for x in (cfg.get("extensions") or [])]
        self.t9 = T9(int(cfg.get("t9_ms", 1200) or 1200))
        fx = browser_cfg.get("firefox") or {}
        binary = str(cfg.get("binary") or (fx.get("binary") if self.firefox else browser_cfg.get("binary")) or "")
        extra = cfg.get("extra_args") or []
        self.browser = Browser({
            "engine": engine, "binary": binary, "debug_port": int(cfg.get("debug_port", 9223) or 9223),
            "kiosk": bool(cfg.get("kiosk", True)), "reduce_effects": self.reduce_effects,
            "user_data_dir": str(self.profile), "start_url": "about:blank", "unit": f"navegador-{engine}",
            "extra_args": (["--password-store=basic", f"--force-device-scale-factor={self.scale:g}"] + list(extra)
                           if not self.firefox else []),
            "firefox": {"profile": str(self.profile), "binary": binary, "prefs": self.prefs(),
                        "extra_args": list(extra) if self.firefox else [], "hide_webdriver": False},
        })
        self.page: Any = None
        self.script, self.version = build_nav_script(cfg, self.reduce_effects)
        self.want_url: str | None = None   # la próxima vez que se abra, esta página (un sitio del menú)
        self.fail_text = ""
        self._connecting: asyncio.Task | None = None

    @property
    def firefox(self) -> bool:
        return self.engine == "firefox"

    def prefs(self) -> dict[str, Any]:
        """Preferencias propias (se suman a las de Netflix en el user.js del perfil)."""
        prefs = dict(NAV_PREFS)
        if self.scale and abs(self.scale - 1.0) > 0.01:
            prefs["layout.css.devPixelsPerPx"] = f"{self.scale:g}"
        extra = self.cfg.get("prefs") or {}
        if isinstance(extra, dict):
            prefs.update(extra)
        return prefs

    # ------------------------------------------------------------ menú
    def binary(self) -> str | None:
        return self.browser.binary()

    def installed(self) -> bool:
        return self.binary() is not None

    def running(self) -> bool:
        return bool(self.browser.profile_pids())

    async def active(self) -> bool:
        return self.running() or await self.browser.alive()

    def source(self) -> tuple[str, bool]:
        names = [b["name"] for b in self.bookmarks]
        return (" · ".join(names[:4]) + (" …" if len(names) > 4 else "")) or NAMES[self.engine], True

    # ------------------------------------------------------------ la página de inicio
    def home_url(self) -> str:
        return self.home or self.start_page().as_uri()

    def start_page(self) -> Path:
        """Los sitios en cuadros grandes (se vuelve a escribir en cada apertura: refleja la configuración)."""
        page = self.profile / START_PAGE
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            page.write_text(self.start_page_html(), "utf-8")
        except OSError as exc:
            log.warning("Navegador: no pude escribir la página de inicio (%s)", exc)
        return page

    def start_page_html(self) -> str:
        tiles = []
        for i, b in enumerate(self.bookmarks):
            n = f"<small>{i + 1}</small>" if i < 9 else ""
            tiles.append(f'<a href="{html.escape(b["url"], quote=True)}">{n}<span>{html.escape(b["name"])}</span>'
                         f'<i>{html.escape(urllib.parse.urlparse(b["url"]).netloc.removeprefix("www."))}</i></a>')
        body = "\n".join(tiles) or "<p>No hay sitios: agregalos en [apps.navegador] bookmarks (config.toml)</p>"
        return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>{html.escape(self.name)}</title>
<style>
html,body{{margin:0;background:#101014;color:#fff;font:500 22px/1.3 system-ui,Helvetica,Arial,sans-serif;cursor:none}}
main{{max-width:1500px;margin:0 auto;padding:5vh 4vw}}
h1{{font-size:1.6em;font-weight:700;margin:0 0 .3em}}
p.h{{color:#999;margin:0 0 1.4em}}
nav{{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:24px}}
a{{display:flex;flex-direction:column;justify-content:center;min-height:150px;padding:18px 22px;border-radius:18px;
  background:#1c1c22;color:#fff;text-decoration:none;position:relative;border:3px solid transparent}}
a span{{font-size:1.35em;font-weight:700}} a i{{font-style:normal;color:#999;margin-top:.3em;font-size:.85em}}
a small{{position:absolute;top:12px;right:16px;color:#2f7cf6;font-size:.9em;font-weight:700}}
</style></head><body><main>
<h1>{html.escape(self.name)}</h1>
<p class="h">0 = escribir una dirección o buscar · 1 – 9 = estos sitios · flechas y OK · Atrás vuelve · azul = esta página</p>
<nav>
{body}
</nav></main></body></html>
"""

    # ------------------------------------------------------------ abrir y cerrar
    def _copy_extensions(self) -> None:
        """Los .xpi de [apps.navegador] extensions, al perfil (Firefox los habilita solo: autoDisableScopes = 0)."""
        if not self.firefox or not self.extensions:
            return
        dest = self.profile / "extensions"
        for src in self.extensions:
            p = Path(src)
            if not p.is_file():
                log.warning("Navegador: no existe la extensión %s", src)
                continue
            try:
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / p.name
                if not target.is_file() or target.stat().st_mtime < p.stat().st_mtime:
                    shutil.copy2(p, target)
                    log.info("Navegador: extensión %s", p.name)
            except OSError as exc:
                log.warning("Navegador: no pude copiar la extensión %s (%s)", p.name, exc)

    async def open(self, url: str | None = None) -> bool:
        """Abre el navegador en esa página (o en la de inicio). Si ya está abierto, solo navega."""
        self.fail_text = ""
        if url is None:
            url, self.want_url = self.want_url, None
        url = url or self.home_url()
        self.t9.reset()
        if await self.browser.alive():
            if await self._ensure_page():
                await self.navigate(url)
                await self.bring_front()
                return True
        await asyncio.to_thread(self.start_page)
        await asyncio.to_thread(self._copy_extensions)
        if not await self.browser.launch(url):
            self.fail_text = f"No pude abrir {NAMES[self.engine]}"
            return False
        self.browser.notify_connected()
        if not await self._connect(CONNECT_TIMEOUT_S):
            log.warning("Navegador: %s abrió pero no acepta el control todavía (las teclas no van a andar)",
                        NAMES[self.engine])
            return True
        pending, self.browser.pending_url = self.browser.pending_url, None
        await self.navigate(pending or url)
        return True

    async def _connect(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                page = await self.browser.connect()
            except SessionBusy:
                log.warning("Navegador: la sesión de control está tomada (¿otro cliente?)")
                return False
            except (CDPError, OSError, asyncio.TimeoutError) as exc:
                log.debug("Navegador: todavía no conecta (%s)", exc)
                page = None
            if page is not None:
                self.page = page
                try:
                    await page.setup_page(self.script)
                except CDPError as exc:
                    log.warning("Navegador: no pude instalar el script (%s)", exc)
                log.info("Navegador: conectado a %s (puerto %d)", NAMES[self.engine], self.browser.port)
                return True
            await asyncio.sleep(1.0)
        return False

    async def _ensure_page(self) -> bool:
        """La conexión con la página (si se perdió, se vuelve a abrir, rápido)."""
        if self.page is not None:
            return True
        if not await self.browser.alive():
            return False
        return await self._connect(8)

    async def close(self) -> None:
        await self._drop_page()
        if self.running() or await self.browser.alive():
            await self.browser.close()
        self.t9.reset()

    async def _drop_page(self) -> None:
        page, self.page = self.page, None
        if page is not None:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    async def pause(self) -> None:
        """La TV pasó a standby: el video que esté sonando, en pausa."""
        if self.page is not None:
            try:
                await self.js("playPause", "pause")
            except CDPError:
                pass

    async def bring_front(self) -> None:
        if self.page is not None:
            try:
                await self.page.bring_to_front()
            except CDPError:
                pass

    # ------------------------------------------------------------ la página
    async def js(self, name: str, *args: Any, timeout: float = 8.0) -> dict[str, Any]:
        """Un comando del script (window.__tcnav.cmd); si la página es nueva y no lo tiene, se inyecta."""
        if not await self._ensure_page():
            raise CDPClosed("sin conexión con el navegador")
        expr = (f"(window.__tcnav && window.__tcnav.v === {json.dumps(self.version)}) ? "
                f"window.__tcnav.cmd({json.dumps(name)}, {json.dumps(list(args), ensure_ascii=False)}) : {{__missing: true}}")
        for attempt in range(3):
            page = self.page
            if page is None:
                raise CDPClosed("sin conexión con el navegador")
            try:
                val = await page.evaluate(expr, timeout)
            except ContextLost:
                if attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                raise
            except CDPClosed:
                await self._drop_page()
                raise
            if isinstance(val, dict) and val.get("__missing"):
                try:
                    await page.evaluate(self.script)
                except CDPError as exc:
                    log.debug("Navegador: falló la inyección: %s", exc)
                    await asyncio.sleep(0.5)
                continue
            if isinstance(val, dict):
                if val.get("error"):
                    log.warning("Navegador JS %s: %s", name, str(val["error"]).splitlines()[0])
                return val
            return {}
        return {}

    async def key_context(self) -> str:
        """Qué teclas valen ahora: escribiendo (un campo de texto con el foco) o recorriendo la página."""
        if self.page is None and not await self.browser.alive():
            return self.context
        try:
            r = await self.js("ctx", timeout=3.0)
        except CDPError:
            return self.context
        return "navegador_text" if r.get("ctx") == "text" else self.context

    async def navigate(self, url: str) -> None:
        if not await self._ensure_page():
            log.warning("Navegador: sin conexión, no puedo abrir %s", url)
            return
        log.info("Navegador: abro %s", url)
        self.t9.reset()
        try:
            await self.page.navigate(url)
        except CDPError as exc:
            log.warning("Navegador: no pude abrir %s (%s)", url, exc)

    async def go(self, text: str) -> str:
        """Lo que se escribió (en la TV o en el celular): abre la dirección o la búsqueda. Devuelve la dirección."""
        url = target_url(text, self.search)
        if url:
            await self.navigate(url)
        return url

    async def key_press(self, name: str) -> None:
        if await self._ensure_page():
            await self.page.key_press(name)

    async def click(self, x: float, y: float) -> None:
        if await self._ensure_page():
            await self.page.mouse_click(x, y)

    async def current_url(self) -> str:
        if self.page is None:
            return ""
        try:
            return str((await self.js("ctx", timeout=3.0)).get("url") or "")
        except CDPError:
            return ""

    def env_ok(self) -> bool:
        return bool(graphical_env())
