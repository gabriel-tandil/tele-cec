"""Navegador: cualquier página web en la TV, manejada con el control remoto.

Firefox (o Chromium) en pantalla completa con un perfil propio (~/.config/firefox-navegador), lanzado y controlado
como el de Netflix (apps/netflix/browser.py, con otro puerto): tele-cec le inyecta nav.js en cada página y con eso
las flechas van al enlace o botón más cercano (con un marco), OK lo abre, Atrás vuelve, y en un campo de texto los
números escriben como en un celular viejo (t9.py). 0 abre el cuadro «Ir a» (una dirección o una búsqueda). CC
prende el modo mouse: las flechas mueven una flecha dibujada y OK hace un clic de verdad en ese punto (para lo que el
marco no alcanza: un «No soy un robot», un video embebido, un menú que se abre al pasar por encima).

La página de inicio es un archivo propio con los sitios guardados en cuadros; 1 – 9 los abren directo. Los sitios
salen de [apps.navegador] bookmarks hasta que se guarda o se borra uno desde la TV (amarillo › «Guardar esta página»,
rojo sobre un sitio): desde ahí la lista vive en ~/.local/share/tele-cec/navegador-sitios.json.

Para que vaya rápido en la Pi: las mismas preferencias que Netflix (sin animaciones, sin trabajo de fondo), las
páginas más grandes (scale: todo al 150 %, para leer desde el sillón), sin ventanas nuevas ni avisos, uBlock
Origin (ublock: se copia el que tenga el Firefox de siempre; saca la publicidad, lo más pesado de las páginas) y las
extensiones que se le agreguen (extensions).

Cada sitio guardado se muestra con su ícono (favicons.py), en el menú, en el celular y en la página de inicio.
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

from ...system.procs import DATA_DIR, graphical_env
from ..base import Activity
from ..netflix.browser import Browser
from ..netflix import bidi
from ..netflix.cdp import CDPClosed, CDPError, ContextLost, SessionBusy
from ..webprofile import NAMES
from . import favicons
from .t9 import T9

log = logging.getLogger("actividades")

NAV_JS = Path(__file__).resolve().parent / "nav.js"
START_PAGE = "inicio.html"
CONNECT_TIMEOUT_S = 90      # Firefox en la Pi acepta la sesión de control recién cuando terminó de abrir la ventana
DEFAULT_SEARCH = "https://www.google.com/search?q={q}"
DEFAULT_BOOKMARKS = [
    {"name": "Google", "url": "https://www.google.com"},
    {"name": "DuckDuckGo", "url": "https://duckduckgo.com"},
    {"name": "Wikipedia", "url": "https://es.wikipedia.org"},
]
SITES_FILE = "navegador-sitios.json"   # en ~/.local/share/tele-cec: los sitios, una vez cambiados desde la TV
UBLOCK_XPI = "uBlock0@raymondhill.net.xpi"
# Modo mouse: cuánto se mueve la flecha por cada toque (en píxeles de la página); apretando seguido, cada vez más.
MOUSE_STEP = 8
MOUSE_MAX_STEP = 80
MOUSE_ACCEL = 1.3
MOUSE_REPEAT_S = 0.6
# Manteniendo la flecha apretada, se desliza sola: empieza a esta velocidad (px por segundo), acelera y llega a la máxima.
GLIDE_START = 120.0
GLIDE_ACCEL = 350.0
GLIDE_MAX = 700.0
GLIDE_TICK_S = 0.04
# Dónde guarda las extensiones el Firefox de siempre (Firefox 147+ usa ~/.config/mozilla; antes, ~/.mozilla).
USER_PROFILES = ("~/.config/mozilla/firefox/*/extensions", "~/.mozilla/firefox/*/extensions")
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
    "extensions.startupScanScopes": 1,             # y se buscan al arrancar (si no, una nueva puede no verse)
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


def same_site(a: str, b: str) -> bool:
    """¿La misma página guardada? (sin la barra final ni el www.)"""
    def norm(u: str) -> str:
        u = re.sub(r"^https?://(www\.)?", "", str(u).strip(), flags=re.IGNORECASE)
        return u.rstrip("/").lower()
    return norm(a) == norm(b)


def site_name(title: str, url: str) -> str:
    """El nombre de un sitio nuevo: el título de la página (corto), o el nombre del sitio."""
    title = " ".join(str(title or "").split())
    for sep in (" | ", " - ", " — ", " – ", " · "):
        if sep in title and len(title) > 32:
            title = title.split(sep)[0].strip()
    if len(title) > 40:
        title = title[:39].rstrip() + "…"
    return title or urllib.parse.urlparse(url).netloc.removeprefix("www.") or url


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
    icon = "navegador.svg"   # el globo: para los sitios sin ícono propio; la fila del menú lleva el del navegador
    context = "navegador"
    start_timeout = 30.0

    def __init__(self, cfg: dict[str, Any] | None = None, browser_cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        browser_cfg = browser_cfg or {}
        self.cfg = cfg
        self.name = str(cfg.get("name") or self.name)
        engine = str(cfg.get("engine") or browser_cfg.get("engine") or "firefox").strip().lower()
        if engine not in NAMES:
            log.warning("Navegador: navegador %r inválido (firefox/chromium); uso firefox", engine)
            engine = "firefox"
        self.engine = engine
        self.icon = str(cfg.get("icon") or f"{engine}.svg")  # en el menú, el de Firefox o el de Chromium
        self.reduce_effects = bool(browser_cfg.get("reduce_effects", True))
        self.profile = Path(os.path.expanduser(str(cfg.get("profile") or f"~/.config/{engine}-navegador")))
        self.scale = float(cfg.get("scale", 1.5) or 1.0)
        self.search = str(cfg.get("search") or DEFAULT_SEARCH)
        self.home = str(cfg.get("home") or "")
        self.config_bookmarks = bookmarks_from(cfg)
        self.sites_file = DATA_DIR / SITES_FILE
        self._bookmarks: list[dict[str, str]] | None = None   # los guardados desde la TV (se leen al usarlos)
        self.extensions = [os.path.expanduser(str(x)) for x in (cfg.get("extensions") or [])]
        self.ublock = bool(cfg.get("ublock", True))
        self.t9 = T9(int(cfg.get("t9_ms", 1200) or 1200))
        fx = browser_cfg.get("firefox") or {}
        binary = str(cfg.get("binary") or (fx.get("binary") if self.firefox else browser_cfg.get("binary")) or "")
        extra = cfg.get("extra_args") or []
        self.browser = Browser({
            "engine": engine, "binary": binary, "debug_port": int(cfg.get("debug_port", 9223) or 9223),
            "kiosk": bool(cfg.get("kiosk", True)), "reduce_effects": self.reduce_effects,
            "user_data_dir": str(self.profile), "start_url": "about:blank", "unit": f"navegador-{engine}",
            "direct_start": True,  # la página pedida de entrada (no la negra): se ve mientras se conecta el control
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
        # Modo mouse (CC): prendido, dónde está la flecha (None: donde esté el marco) y el último toque (aceleración).
        self.mouse = False
        self.mouse_xy: tuple[float, float] | None = None
        self._mouse_last: tuple[str, float, float] = ("", 0.0, 0.0)
        self.mouse_moved = False   # la flecha se movió desde la última letra escrita (escribiendo: OK = clic, no Enter)
        self._redraw: asyncio.Task | None = None

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
        favicons.ensure([b["url"] for b in self.bookmarks])  # de fondo: para cuando se abra la lista de sitios
        names = [b["name"] for b in self.bookmarks]
        return (" · ".join(names[:4]) + (" …" if len(names) > 4 else "")) or NAMES[self.engine], True

    # ------------------------------------------------------------ los sitios guardados
    @property
    def bookmarks(self) -> list[dict[str, str]]:
        """Los sitios: los guardados desde la TV (si alguna vez se cambió la lista ahí) o los de la configuración."""
        if self._bookmarks is None:
            self._bookmarks = self._read_sites()
        return self._bookmarks if self._bookmarks is not None else self.config_bookmarks

    def _read_sites(self) -> list[dict[str, str]] | None:
        try:
            raw = json.loads(self.sites_file.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("Navegador: no pude leer %s (%s); uso los sitios de la configuración", self.sites_file, exc)
            return None
        if not isinstance(raw, list):
            return None
        return bookmarks_from({"bookmarks": raw})

    def _save_sites(self, sites: list[dict[str, str]]) -> bool:
        try:
            self.sites_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.sites_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(sites, ensure_ascii=False, indent=1) + "\n", "utf-8")
            tmp.replace(self.sites_file)
        except OSError as exc:
            log.warning("Navegador: no pude guardar los sitios (%s)", exc)
            return False
        self._bookmarks = [dict(b) for b in sites]
        self.start_page()  # la página de inicio, al día
        return True

    def saved_index(self, url: str) -> int:
        """En qué lugar de la lista está esa página (-1: no está guardada)."""
        return next((i for i, b in enumerate(self.bookmarks) if same_site(b["url"], url)), -1)

    def add_bookmark(self, url: str, title: str = "") -> int:
        """Guarda la página (al final). Devuelve su lugar, o -1 si no se puede (no es una página web)."""
        if not allowed_url(url):
            return -1
        i = self.saved_index(url)
        if i >= 0:
            return i
        sites = [dict(b) for b in self.bookmarks] + [{"name": site_name(title, url), "url": url}]
        if not self._save_sites(sites):
            return -1
        favicons.ensure([url])
        log.info("Navegador: guardo el sitio %s (%s)", sites[-1]["name"], url)
        return len(sites) - 1

    def remove_bookmark(self, index: int) -> dict[str, str] | None:
        sites = [dict(b) for b in self.bookmarks]
        if not 0 <= index < len(sites):
            return None
        gone = sites.pop(index)
        if not self._save_sites(sites):
            return None
        log.info("Navegador: borro el sitio %s (%s)", gone["name"], gone["url"])
        return gone

    def on_start_page(self, url: str) -> bool:
        return bool(url) and not self.home and url.startswith("file:") and url.split("?")[0].endswith("/" + START_PAGE)

    # ------------------------------------------------------------ los íconos de los sitios
    def favicon(self, url: str) -> Path | None:
        """El ícono ya bajado del sitio de esa dirección (o None: se usa el genérico)."""
        return favicons.path_for(favicons.host_of(url))

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
            ico = self.favicon(b["url"])
            img = (f'<img src="{html.escape(ico.as_uri(), quote=True)}" alt="">' if ico
                   else f"<b>{html.escape((b['name'][:1] or '?').upper())}</b>")
            tiles.append(f'<a href="{html.escape(b["url"], quote=True)}">{n}{img}<div><span>{html.escape(b["name"])}</span>'
                         f'<i>{html.escape(urllib.parse.urlparse(b["url"]).netloc.removeprefix("www."))}</i></div></a>')
        body = "\n".join(tiles) or ("<p>No hay sitios guardados: abrí una página y guardala con el amarillo "
                                     "(«Guardar esta página»)</p>")
        return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>{html.escape(self.name)}</title>
<style>
html,body{{margin:0;background:#101014;color:#fff;font:500 17px/1.3 system-ui,Helvetica,Arial,sans-serif}}
*{{cursor:none!important}}
main{{max-width:1400px;margin:0 auto;padding:3.5vh 3vw}}
h1{{font-size:1.3em;font-weight:700;margin:0 0 .2em}}
p.h{{color:#999;margin:0 0 1.1em;font-size:.85em}}
nav{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}}
a{{display:flex;align-items:center;gap:12px;min-height:64px;padding:10px 14px;border-radius:12px;
  background:#1c1c22;color:#fff;text-decoration:none;position:relative;border:3px solid transparent;overflow:hidden}}
a div{{display:flex;flex-direction:column;min-width:0}}
a span{{font-size:1.05em;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
a i{{font-style:normal;color:#999;font-size:.78em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
a small{{position:absolute;top:6px;right:10px;color:#2f7cf6;font-size:.8em;font-weight:700}}
a img,a b{{flex:none;width:40px;height:40px;border-radius:9px}}
a b{{display:flex;align-items:center;justify-content:center;background:#2a2a33;color:#9cc0ff;font-size:1.2em}}
</style></head><body><main>
<h1>{html.escape(self.name)}</h1>
<p class="h">0 = escribir una dirección o buscar · 1 – 9 = estos sitios · flechas y OK · CC = mouse · Atrás vuelve · azul = esta página</p>
<nav>
{body}
</nav></main></body></html>
"""

    # ------------------------------------------------------------ abrir y cerrar
    def user_ublock(self) -> Path | None:
        """El uBlock Origin del Firefox de siempre (el más nuevo, si hay varios perfiles), o None."""
        found = []
        for pattern in USER_PROFILES:
            base = Path(os.path.expanduser(pattern))
            found += [p for p in Path(base.anchor).glob(str(base.relative_to(base.anchor)) + "/" + UBLOCK_XPI)
                      if p.is_file() and self.profile not in p.parents]
        return max(found, key=lambda p: p.stat().st_mtime, default=None)

    def _copy_extensions(self) -> None:
        """Los .xpi de [apps.navegador] extensions (y uBlock, si está en el Firefox de siempre), al perfil: Firefox
        los habilita solo (autoDisableScopes = 0). Se vuelven a copiar si el original es más nuevo."""
        if not self.firefox:
            return
        sources = list(self.extensions)
        if self.ublock and not any(Path(s).name == UBLOCK_XPI for s in sources):
            ub = self.user_ublock()
            if ub is not None:
                sources.append(str(ub))
            else:
                log.info("Navegador: no encontré uBlock Origin en el Firefox de siempre (instalalo ahí, o ver extensions)")
        dest = self.profile / "extensions"
        for src in sources:
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
        await asyncio.to_thread(lambda: (t := favicons.ensure([b["url"] for b in self.bookmarks])) and t.join(6))
        await asyncio.to_thread(self.start_page)
        await asyncio.to_thread(self._copy_extensions)
        if not await self.browser.launch(url):
            self.fail_text = f"No pude abrir {NAMES[self.engine]}"
            return False
        self.browser.notify_connected()
        # La página ya se ve (direct_start). La conexión de control, de fondo: en la Pi Firefox la acepta recién
        # cuando terminó de abrir la ventana (más de medio minuto); mientras tanto, las teclas esperan.
        self._start_connecting(CONNECT_TIMEOUT_S)
        return True

    def _start_connecting(self, timeout: float) -> None:
        if self._connecting is not None and not self._connecting.done():
            return
        self._connecting = asyncio.get_running_loop().create_task(self._connect_bg(timeout))

    async def _connect_bg(self, timeout: float) -> bool:
        ok = await self._connect(timeout)
        if not ok:
            log.warning("Navegador: %s abrió pero no acepta el control (las teclas no van a andar)",
                        NAMES[self.engine])
        return ok

    async def _connect(self, timeout: float) -> bool:
        t0 = time.monotonic()
        deadline = t0 + timeout
        told = 0.0
        restarted = False
        while time.monotonic() < deadline:
            if not self.running() and not await self.browser.alive():
                return False  # se cerró mientras tanto
            try:
                page = await self.browser.connect()
            except SessionBusy:
                if bidi.late_session_pending():
                    page = None  # es la nuestra: Firefox la está terminando de crear y se cierra sola
                elif restarted:
                    log.warning("Navegador: la sesión de control sigue tomada después de reiniciar %s", NAMES[self.engine])
                    return False
                else:
                    # Un servicio anterior (o una prueba) murió sin soltarla: Firefox no la libera nunca. La única
                    # salida es reabrirlo (vuelve a la página de inicio).
                    log.warning("Navegador: %s tiene la sesión de control tomada: lo reabro", NAMES[self.engine])
                    restarted = True
                    if not await self.browser.restart():
                        return False
                    continue
            except (CDPError, OSError, asyncio.TimeoutError) as exc:
                if time.monotonic() - told > 15:
                    told = time.monotonic()
                    log.info("Navegador: todavía no acepta el control (%s), sigo probando", str(exc)[:80] or type(exc).__name__)
                page = None
            if page is not None:
                self.page = page
                try:
                    await page.setup_page(self.script)
                except CDPError as exc:
                    log.warning("Navegador: no pude instalar el script (%s)", exc)
                log.info("Navegador: conectado a %s (puerto %d) en %.0f s", NAMES[self.engine], self.browser.port,
                         time.monotonic() - t0)
                return True
            await asyncio.sleep(1.0)
        return False

    async def _ensure_page(self) -> bool:
        """La conexión con la página: si se está conectando, la espera (hasta un rato); si se perdió, la reabre."""
        if self.page is not None:
            return True
        task = self._connecting
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 20)
            except asyncio.TimeoutError:
                return False
            return self.page is not None
        if not await self.browser.alive():
            return False
        return await self._connect(8)

    async def close(self) -> None:
        if self._connecting is not None and not self._connecting.done():
            self._connecting.cancel()
        self._connecting = None
        await self._drop_page()
        if self.running() or await self.browser.alive():
            await self.browser.close()
        self.t9.reset()
        self.mouse, self.mouse_xy = False, None

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
        if self.page is None and (self._connecting is not None and not self._connecting.done()
                                  or not await self.browser.alive()):
            return self.context  # todavía conectando (o sin navegador): sin preguntarle a la página
        try:
            r = await self.ctx()
        except CDPError:
            return self.context
        if r.get("ctx") == "text":
            return "navegador_text"
        return "navegador_mouse" if self.mouse else self.context

    async def ctx(self) -> dict[str, Any]:
        """Qué hay en la página (campo con el foco, dirección, título). Con el modo mouse prendido le recuerda a la
        página dónde va la flecha (en cada página nueva el script arranca de cero)."""
        if not self.mouse:
            return await self.js("ctx", timeout=3.0)
        at = {"x": self.mouse_xy[0], "y": self.mouse_xy[1]} if self.mouse_xy else {}
        return await self.js("ctx", at, timeout=3.0)

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
            return
        self.redraw_soon()

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

    async def click_landed(self) -> bool:
        """Después del clic de OK: ¿le llegó al elemento? Si no (cayó en otra cosa), el script le hace un clic por su
        cuenta. Sin reintentos: si el clic abrió otra página, esta pregunta ya no tiene a quién hacerse."""
        if self.page is None:
            return True
        try:
            r = await self.page.evaluate('(window.__tcnav && window.__tcnav.cmd("clicked", [])) || {}', 2.0)
        except CDPError:
            return True
        if isinstance(r, dict) and r.get("fallback"):
            log.info("Navegador: el clic no le llegó al elemento; le hago uno por script")
            return False
        return True

    async def current_url(self) -> str:
        if self.page is None:
            return ""
        try:
            return str((await self.ctx()).get("url") or "")
        except CDPError:
            return ""

    async def page_info(self) -> tuple[str, str]:
        """(dirección, título) de la página que se ve."""
        if self.page is None:
            return "", ""
        try:
            r = await self.ctx()
        except CDPError:
            return "", ""
        return str(r.get("url") or ""), str(r.get("title") or "")

    # ------------------------------------------------------------ modo mouse
    def mouse_step(self, direction: str) -> float:
        """Cuánto avanza la flecha: poco con un toque; apretando seguido en la misma dirección, cada vez más."""
        now = time.monotonic()
        last_dir, last_t, last_step = self._mouse_last
        step = (min(MOUSE_MAX_STEP, last_step * MOUSE_ACCEL) if direction == last_dir and now - last_t < MOUSE_REPEAT_S
                else MOUSE_STEP)
        self._mouse_last = (direction, now, step)
        return step

    async def mouse_on(self) -> dict[str, Any]:
        self.mouse = True
        x, y = self.mouse_xy or (None, None)
        r = await self.js("mouse", "show", x, y)
        self._keep_xy(r)
        return r

    async def mouse_off(self) -> None:
        self.mouse = False
        self._mouse_last = ("", 0.0, 0.0)
        try:
            await self.js("mouse", "hide")
        except CDPError:
            pass

    async def mouse_move(self, direction: str, step: float | None = None, hover: bool = True) -> dict[str, Any]:
        """Mueve la flecha en esa dirección (step: cuánto; sin step, según cuán seguido se aprieta)."""
        step = self.mouse_step(direction) if step is None else step
        dx, dy = {"up": (0, -step), "down": (0, step), "left": (-step, 0), "right": (step, 0)}.get(direction, (0, 0))
        if self.mouse_xy is None:
            await self.mouse_on()
        r = await self.js("mouse", "move", dx, dy)
        self._keep_xy(r)
        self.mouse_moved = True
        if hover:
            await self.mouse_hover()
        return r

    async def mouse_hover(self) -> None:
        """Lleva el puntero de verdad adonde está la flecha: los menús que se abren al pasar por encima."""
        if self.mouse_xy and self.page is not None:
            try:
                await self.page.mouse_move(*self.mouse_xy)
            except CDPError:
                pass

    async def mouse_glide(self, direction: str, held: Any) -> None:
        """Botón mantenido: la flecha se desliza sola, cada vez más rápido, mientras held() diga que sigue apretado."""
        t0 = last = time.monotonic()
        try:
            while self.mouse:
                await asyncio.sleep(GLIDE_TICK_S)
                if not held():
                    break
                now = time.monotonic()
                speed = min(GLIDE_MAX, GLIDE_START + GLIDE_ACCEL * (now - t0))
                r = await self.mouse_move(direction, speed * (now - last), hover=False)
                last = now
                if r.get("edge"):
                    break  # contra el borde y sin nada para correr
        finally:
            self._mouse_last = (direction, time.monotonic(), MOUSE_MAX_STEP)
            await self.mouse_hover()

    async def mouse_click(self) -> dict[str, Any]:
        """Clic de verdad donde está la flecha. Devuelve {x, y, text (era un campo de texto), label}."""
        r = await self.js("mouse", "where")
        if not r.get("on"):
            r = await self.mouse_on()
            r = await self.js("mouse", "where")
        if "x" in r and "y" in r:
            await self.click(r["x"], r["y"])
            self.mouse_moved = False
            self.redraw_soon()
        return r

    def _keep_xy(self, r: dict[str, Any]) -> None:
        if isinstance(r.get("x"), (int, float)) and isinstance(r.get("y"), (int, float)):
            self.mouse_xy = (float(r["x"]), float(r["y"]))

    def redraw_soon(self) -> None:
        """Después de un clic o de abrir otra página: la flecha otra vez, cuando la página nueva ya cargó."""
        if not self.mouse:
            return
        if self._redraw is not None and not self._redraw.done():
            self._redraw.cancel()

        async def later() -> None:
            for wait in (1.2, 2.5):
                await asyncio.sleep(wait)
                if not self.mouse or self.page is None:
                    return
                try:
                    await self.ctx()
                except CDPError:
                    pass
        try:
            self._redraw = asyncio.get_running_loop().create_task(later())
        except RuntimeError:
            self._redraw = None

    def env_ok(self) -> bool:
        return bool(graphical_env())
