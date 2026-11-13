#!/usr/bin/env python3
"""Prueba del menú de actividades (Netflix, Tidal, Videos, Fotos) con navegador, High Tide, VLC, feh y
panel simulados.

No abre nada: verifica qué hace cada tecla según qué actividad está abierta y si el menú está abierto.

    python3 tests/apps_test.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tele_cec import apps as activities_mod  # noqa: E402
from tele_cec import config as config_mod  # noqa: E402
from tele_cec.system import keyboard as keyboard_mod  # noqa: E402
from tele_cec.controller import Controller  # noqa: E402
from tele_cec.keys import parse_key  # noqa: E402

results: list[tuple[bool, str]] = []


def check(cond: bool, text: str) -> None:
    results.append((bool(cond), text))
    print(f"  [{'OK ' if cond else 'MAL'}] {text}", flush=True)


class FakePage:
    """Pestaña de Netflix simulada: responde a window.__nfx.cmd(...)."""

    def __init__(self, log: list[str]):
        self.log = log
        self.closed = asyncio.Event()
        self.url = "https://www.netflix.com/browse"
        self.ctx = "browse"

    async def evaluate(self, expr: str, timeout: float = 10.0):
        m = re.search(r'cmd\("([a-zA-Z]+)"', expr)
        name = m.group(1) if m else "?"
        self.log.append(f"js:{name}")
        if name == "ctx":
            return {"ctx": self.ctx, "url": self.url}
        return {}

    async def bring_to_front(self) -> None:
        pass

    async def navigate(self, url: str) -> None:
        self.log.append(f"navigate:{url}")
        self.url = url
        if "ProfilesGate" in url:
            self.ctx = "profiles"
        elif "/watch/" in url:
            self.ctx = "player"

    async def close(self) -> None:
        self.closed.set()


class FakeBrowser:
    name = "Firefox"
    port = 9222
    firefox = True
    pending_url = None

    def __init__(self, log: list[str]):
        self.log = log
        self.is_alive = False
        self.ctl: Controller | None = None

    async def alive(self) -> bool:
        return self.is_alive

    def can_autolaunch(self) -> bool:
        return True

    async def launch(self, url=None) -> bool:
        self.log.append("browser:launch")
        self.is_alive = True
        # Lo que haría browser_loop al conectarse.
        self.ctl.page = FakePage(self.log)
        self.ctl.connected.set()
        return True

    async def close(self, timeout: float = 30.0) -> bool:
        self.log.append("browser:close")
        self.is_alive = False
        if self.ctl.page:
            await self.ctl.page.close()
        self.ctl.page = None
        self.ctl.connected.clear()
        return True


class FakeMusic:
    resume_on_open = True
    start_in_nav = False  # los tests de abajo arrancan en controles; el arranque en navegación se prueba aparte

    def __init__(self, log: list[str]):
        self.log = log
        self.on = False
        self.can_resume = True

    def installed(self) -> bool:
        return True

    def running(self) -> bool:
        return self.on

    def pids(self, fresh: bool = False) -> list[int]:
        return [self.launches] if self.on else []

    async def launch(self) -> bool:
        self.log.append("music:launch")
        self.on = True
        self.launches = getattr(self, "launches", 0) + 1
        return True

    async def resume(self) -> bool:
        self.log.append("music:resume")
        return self.can_resume

    def saved_index(self) -> int:
        return 11

    async def quit(self, timeout: float = 15.0) -> None:
        self.log.append("music:quit")
        self.on = False

    async def ctl(self, *args: str) -> bool:
        self.log.append("mpris:" + " ".join(args))
        return True

    async def status(self) -> dict:
        return {"status": "Playing", "title": "Volver a Volver", "artist": "Gabo Ferro"}

    async def volume(self, sign: str) -> bool:
        self.log.append(f"mpris:volume{sign}")
        return True

    async def seek(self, seconds: int) -> bool:
        self.log.append(f"mpris:seek{seconds:+d}")
        return True

    async def key(self, name: str) -> bool:
        self.log.append(f"wtype:{name}")
        return True

    async def press_button(self, names) -> bool:
        self.log.append(f"boton:{names[0]}")
        return True

    # Canal de control del parche: channel=False simula High Tide sin él (respaldo por la cuenta propia).
    channel = False
    page = "home"

    async def go(self, page: str) -> bool:
        self.log.append(f"go:{page}")
        return True

    async def like(self) -> bool:
        self.log.append("like")
        return True

    async def shuffle(self) -> bool:
        self.log.append("shuffle")
        return True

    async def cycle(self):
        if not self.channel:
            return None
        self.page = {"my_tracks": "collection", "collection": "home"}.get(self.page, "my_tracks")
        self.log.append(f"cycle:{self.page}")
        return self.page

    async def move(self, direction: str) -> bool:
        self.log.append(f"move:{direction}")
        return True

    async def tab(self, back: bool = False) -> bool:
        self.log.append("tab:back" if back else "tab")
        return True

    async def toggle_lyrics(self) -> bool:
        self.log.append("letras")
        return True

    async def stop_nav(self) -> None:
        self.log.append("nav:stop")

    async def my_tracks(self, start: int = 0) -> bool:
        self.log.append("music:my_tracks" + (f"@{start}" if start else ""))
        return True


class FakeOverlay:
    """Panel del menú simulado: anota lo que se le pide."""

    error = ""

    def __init__(self, log: list[str]):
        self.log = log
        self.visible = False
        self.items: list[dict] = []
        self.sel = -1

    async def show(self, items, selected, title="Actividades", hint="", numbers=True) -> bool:
        self.items, self.sel, self.visible = items, selected, True
        self.log.append(f"ov:show:{selected}")
        return True

    async def help(self, title, rows, top, page, hint) -> bool:
        self.help_rows = rows
        self.log.append(f"ov:help:{title}")
        return True

    async def select(self, index: int) -> bool:
        self.sel = index
        self.log.append(f"ov:select:{index}")
        return True

    async def busy(self, text: str) -> bool:
        self.log.append(f"ov:busy:{text}")
        return True

    async def toast(self, text: str, ms: int = 2000) -> bool:
        self.log.append(f"ov:toast:{text}")
        return True

    async def hide(self) -> bool:
        self.visible = False
        self.log.append("ov:hide")
        return True

    async def close(self) -> None:
        pass


class FakeMpris:
    def __init__(self, log: list[str], name: str):
        self.log, self.name = log, name

    async def ctl(self, *args: str) -> bool:
        self.log.append(f"{self.name}:" + " ".join(args))
        return True

    async def volume(self, sign: str) -> bool:
        self.log.append(f"{self.name}:volume{sign}")
        return True

    async def seek(self, seconds: int) -> bool:
        self.log.append(f"{self.name}:seek{seconds:+d}")
        return True

    async def quit(self) -> bool:
        return True


class FakeProcMixin:
    """Actividad de proceso simulada (VLC o feh)."""

    def setup(self, log: list[str]) -> None:
        self.log = log
        self.on = False
        self.content = True

    def installed(self) -> bool:
        return True

    def running(self) -> bool:
        return self.on

    def source(self):
        return ("Pendrive TEST", True) if self.content else (self.empty_text, False)

    async def open(self, folder=None) -> bool:
        self.log.append(f"{self.id}:open" + (f":{Path(folder).name}" if folder else ""))
        self.on = True
        if hasattr(self, "reset_view"):  # como el real: abrir Fotos empieza sin zoom
            self.reset_view()
        if folder:
            self.current_folder = Path(folder)
        return True

    async def close(self) -> None:
        self.log.append(f"{self.id}:close")
        self.on = False


class FakeVideos(FakeProcMixin, activities_mod.VideosActivity):
    def __init__(self, log: list[str]):
        activities_mod.VideosActivity.__init__(self, {})
        self.setup(log)
        self.mpris = FakeMpris(log, "vlc")

    async def pause(self) -> None:
        await self.mpris.ctl("pause")

    async def cycle(self, kind: str) -> str:
        self.log.append(f"vlc:{kind}")
        return {"strack": "Español", "atrack": "Inglés", "vratio": "4:3"}[kind]


class FakePhotos(FakeProcMixin, activities_mod.PhotosActivity):
    def __init__(self, log: list[str]):
        activities_mod.PhotosActivity.__init__(self, {})
        self.setup(log)

    async def next(self) -> bool:
        self.log.append("feh:next")
        return True

    async def previous(self) -> bool:
        self.log.append("feh:previous")
        return True

    async def key(self, name: str) -> bool:
        self.log.append(f"feh:{name}")
        return True

    async def photo_info(self):
        base = self.current_folder or Path("/mnt/fotosRemotas")
        return {"path": str(base / "2017" / "07" / "IMG_0001.jpg"), "index": 12, "total": 49403}


def help_rows(ctl, ctx):
    from tele_cec.ui import help as help_mod
    return help_mod.rows(ctl.cfg, ctx)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="nfx-apps-"))
    cfg_file = tmp / "config.toml"
    cfg_file.write_text('[browser]\nengine = "firefox"\nlaunch = "on_key"\n[feedback]\nbusy = false\n'
                        '[menu]\nitems = ["netflix", "tidal", "videos", "fotos"]\nsound = false\n')  # YouTube y Sonido, aparte
    cfg = config_mod.load(cfg_file)
    log: list[str] = []
    browser = FakeBrowser(log)
    ctl = Controller(cfg, browser)  # type: ignore[arg-type]
    browser.ctl = ctl
    ctl.music = FakeMusic(log)  # type: ignore[assignment]
    ctl.overlay = FakeOverlay(log)  # type: ignore[assignment]
    videos, fotos = FakeVideos(log), FakePhotos(log)
    ctl.activities["videos"], ctl.activities["fotos"] = videos, fotos
    cw: list[str] = []

    async def fake_cw(arg: str) -> None:
        cw.append("continue_watching")
    ctl.a_continue_watching = fake_cw  # type: ignore[method-assign]

    async def fake_wtype(name: str) -> bool:
        log.append(f"wtype:{name}")
        return True
    keyboard_mod.wtype_key = fake_wtype  # type: ignore[assignment]

    async def press(*names: str) -> list[str]:
        start = len(log)
        for n in names:
            await ctl.handle_key(parse_key(n))
            ctl._released = True
            if ctl._macro_task:
                await ctl._macro_task
                ctl._macro_task = None
        return log[start:]

    def opened(out: list[str]) -> list[str]:
        return [x for x in out if x.split(":")[0] in ("browser", "music", "videos", "fotos")
                and x.split(":")[1] in ("launch", "open", "close", "quit")]

    names = lambda: [it["name"] for it in ctl.overlay.items]  # noqa: E731

    print("Nada abierto")
    check(await ctl.context() == "offline", "sin nada abierto: contexto 'offline'")
    out = await press("F2_RED", "F4_YELLOW")
    check(not opened(out), f"rojo y amarillo ya no abren apps: {out}")
    out = await press("SELECT")
    check(out == ["ov:show:0"] and names() == ["Netflix", "Tidal", "Videos", "Fotos", "Ayuda"],
          f"OK abre el menú con las 4 actividades y la ayuda: {out} {names()}")
    check(await ctl.context() == "menu", "contexto 'menu'")
    check(not any(it["current"] for it in ctl.overlay.items), "ninguna marcada «en uso»")
    out = await press("DOWN", "ROOT_MENU", "UP", "DOWN")
    check(out == ["ov:select:1", "ov:select:2", "ov:select:1", "ov:select:2"], f"▼, Menú y ▲ mueven: {out}")
    out = await press("DOWN", "DOWN", "DOWN")
    check(out[-1] == "ov:select:0", f"después de la última vuelve a la primera: {out}")
    out = await press("EXIT")
    check(out == ["ov:hide"] and await ctl.context() == "offline", f"Atrás cierra el menú sin abrir nada: {out}")

    print("Menú → Tidal (número)")
    out = await press("ROOT_MENU", "NUMBER2")
    check(out[:2] == ["ov:show:0", "ov:select:1"] and "ov:busy:Abriendo Tidal…" in out,
          f"Menú y 2 eligen Tidal y muestran «Abriendo…»: {out[:3]}")
    check(opened(out) == ["music:launch"] and "music:resume" in out, f"abre Tidal y sigue lo último: {opened(out)}")
    check(out[-1] == "ov:hide" and await ctl.context() == "tidal", "al terminar se oculta el panel: contexto 'tidal'")

    print("Tidal abierto")
    out = await press("F4_YELLOW")
    check(out == ["music:my_tracks"], f"amarillo = Mis canciones: {out}")
    out = await press("F4_YELLOW")
    check(out == ["go:back"], f"amarillo otra vez = Colección (Atrás desde Mis canciones): {out}")
    out = await press("F4_YELLOW")
    check(out == ["go:home"], f"amarillo otra vez = Inicio: {out}")
    out = await press("F4_YELLOW")
    check(out == ["music:my_tracks"], f"y otra vez, de nuevo Mis canciones: {out}")
    await press("NUMBER0", "DOWN", "NUMBER0")  # se movió por la interfaz
    out = await press("F4_YELLOW")
    check(out == ["music:my_tracks"], f"si se movió con las flechas, el amarillo vuelve a Mis canciones: {out}")
    ctl.music.channel = True  # con el canal del parche: High Tide decide según la pantalla que muestra
    out = await press("F4_YELLOW", "F4_YELLOW", "F4_YELLOW", "F4_YELLOW")
    check(out == ["cycle:my_tracks", "cycle:collection", "cycle:home", "cycle:my_tracks"],
          f"con el canal, el amarillo cicla Mis canciones → Colección → Inicio: {out}")
    ctl.music.channel = False
    out = await press("SELECT", "FORWARD", "BACKWARD", "UP", "DOWN", "F1_BLUE")
    check(out == ["mpris:play-pause", "mpris:next", "mpris:previous", "mpris:volume+", "mpris:volume-",
                  "like"], f"OK, ⏭, ⏮, ▲, ▼, azul: {out}")
    out = await press("EXIT", "F3_GREEN")
    check(out == [], f"Atrás y verde no hacen nada en Tidal: {out}")
    out = await press("F2_RED")
    check(out == ["letras"], f"rojo = letras: {out}")
    await press("NUMBER0")
    check(await ctl.context() == "tidal_nav", "0 activa el modo navegación de Tidal")
    out = await press("DOWN", "LEFT", "SELECT", "EXIT")
    check(out == ["wtype:Down", "wtype:Left", "wtype:Return", "go:back"],
          f"en modo navegación: flechas, OK y Atrás van tal cual a la app (sin seguir el foco): {out}")
    out = await press("FORWARD", "BACKWARD", "CHANNEL_UP", "CHANNEL_DOWN")
    check(out == ["mpris:next", "mpris:previous", "tab", "tab:back"],
          f"⏭ ⏮ siguen cambiando de tema; Canal ± saltan al elemento siguiente / anterior: {out}")
    out = await press("NUMBER3", "NUMBER1")
    check(out == ["go:collection", "go:home"], f"3 = Colección, 1 = Inicio: {out}")
    out = await press("ROOT_MENU")
    check(await ctl.context() == "menu", "Menú también se abre en el modo navegación")
    await press("EXIT")
    out = await press("NUMBER0")
    check(await ctl.context() == "tidal" and out == ["nav:stop"],
          f"0 otra vez vuelve a los controles (y deja de seguir el foco): {out}")
    check(ctl._holding_for_activity(), "con Tidal abierto el navegador no se reabre solo (launch = always)")

    print("Menú sobre Tidal")
    out = await press("ROOT_MENU")
    check(out == ["ov:show:1"] and ctl.overlay.items[1]["current"], f"abre con Tidal seleccionada y «en uso»: {out}")
    out = await press("PLAY", "F4_YELLOW", "NUMBER0", "FORWARD")
    check(out == [], f"con el menú abierto ninguna tecla llega a Tidal: {out}")
    out = await press("SELECT")
    check(out == ["ov:hide"], f"OK sobre la actual solo cierra el menú: {out}")

    print("Tidal → Videos")
    out = await press("ROOT_MENU", "DOWN", "SELECT")
    check(opened(out) == ["music:quit", "videos:open"], f"cierra Tidal y abre VLC: {opened(out)}")
    check(await ctl.context() == "videos", "contexto 'videos'")
    out = await press("SELECT", "RIGHT", "LEFT", "UP", "FORWARD", "CHANNEL_DOWN", "F1_BLUE", "F2_RED", "F4_YELLOW", "EXIT")
    check(out == ["vlc:play-pause", "vlc:seek+10", "vlc:seek-10", "vlc:volume+", "vlc:next", "vlc:previous",
                  "vlc:strack", "ov:toast:Subtítulos: Español", "vlc:atrack", "ov:toast:Audio: Inglés",
                  "vlc:vratio", "ov:toast:Proporción: 4:3"],
          f"OK, ▶, ◀, ▲, ⏭, Canal−, azul (subtítulos), rojo (audio), amarillo (proporción), Atrás nada: {out}")

    print("Fotos sin contenido")
    fotos.content = False
    out = await press("ROOT_MENU")
    check(ctl.overlay.items[3]["enabled"] is False and "Sin fotos" in ctl.overlay.items[3]["sub"],
          f"Fotos aparece en gris: {ctl.overlay.items[3]}")
    out = await press("NUMBER4")
    check(not opened(out) and any(x.startswith("ov:toast:Sin fotos") for x in out),
          f"elegirla avisa y no abre nada: {out}")
    check(await ctl.context() == "menu", "el menú sigue abierto")
    await press("EXIT")

    print("Videos → Fotos")
    fotos.content = True
    out = await press("ROOT_MENU", "NUMBER4")
    check(opened(out) == ["videos:close", "fotos:open"], f"cierra VLC y abre feh: {opened(out)}")
    check(await ctl.context() == "fotos", "contexto 'fotos'")
    out = await press("RIGHT", "LEFT", "SELECT", "EXIT")
    check(out == ["feh:next", "feh:previous", "feh:h"], f"▶, ◀, OK (Atrás nada): {out}")
    check(fotos.paused, "queda en pausa")
    await press("SELECT")
    fotos.current_folder = Path("/mnt/fotosRemotas")
    out = await press("F2_RED", "F4_YELLOW", "F1_BLUE")
    where = activities_mod.describe_folder(Path("/mnt/fotosRemotas"))  # «En red: …» o «Pendrive …» según la máquina
    check(out == ["feh:greater", "feh:z", f"ov:toast:{where} › 2017 › 07\nIMG_0001.jpg\nFoto 12 de 49.403"],
          f"rojo gira, amarillo al azar, azul = qué foto es (carpeta, archivo, número): {out}")

    print("Fotos: zoom")
    fotos.RENDER_DELAY = 0.05
    out = await press("DOWN")
    check(out == [] and await ctl.context() == "fotos", f"▼ sin zoom no hace nada: {out}")
    out = await press("UP")
    check(out[:2] == ["feh:h", "feh:Up"] and any(x.startswith("ov:toast:Zoom") for x in out),
          f"▲ pausa la presentación, acerca y avisa: {out}")
    check(await ctl.context() == "fotos_zoom" and fotos.zoom == 1, "contexto 'fotos_zoom'")
    out = await press("SELECT", "CHANNEL_UP", "LEFT", "UP", "RIGHT", "DOWN")
    check(out == ["feh:Up", "feh:Up", "feh:KP_Left", "feh:KP_Up", "feh:KP_Right", "feh:KP_Down"],
          f"OK y Canal+ acercan, flechas mueven la foto: {out}")
    await asyncio.sleep(0.15)
    check(log[-1] == "feh:R", f"al dejar de moverla se redibuja suavizada: {log[-1]}")
    out = await press("EXIT", "EXIT")
    check(out == ["feh:Down", "feh:Down"] and fotos.zoom == 1, f"Atrás aleja: {out}")
    out = await press("EXIT")
    check(out == ["feh:KP_Divide", "feh:h"], f"desde el primer paso: foto entera y sigue la presentación: {out}")
    check(await ctl.context() == "fotos" and not fotos.paused, "vuelve a 'fotos' y la presentación anda")
    await press("SELECT")  # pausa a mano
    out = await press("UP", "PLAY")
    check([x for x in out if x.startswith("feh:")] == ["feh:Up", "feh:KP_Divide"],
          f"si ya estaba en pausa no la toca; ⏯ = foto entera: {out}")
    check(fotos.paused and await ctl.context() == "fotos", "y sigue en pausa")
    await press("SELECT")
    out = await press("UP", "FORWARD")
    check([x for x in out if x.startswith("feh:")] == ["feh:h", "feh:Up", "feh:next", "feh:h"],
          f"⏭ con zoom: otra foto, sin zoom, sigue la presentación: {out}")
    check(await ctl.context() == "fotos" and not fotos.paused, "vuelve a 'fotos'")
    await press("UP")

    print("Fotos → Netflix")
    out = await press("ROOT_MENU", "NUMBER1")
    check(opened(out) == ["fotos:close", "browser:launch"], f"cierra feh y abre Netflix: {opened(out)}")
    check(await ctl.context() == "browse" and ctl.current_app() == "netflix", "queda Netflix (contexto 'browse')")
    check(not ctl._holding_for_activity(), "y el navegador vuelve a poder abrirse solo")

    print("Netflix abierto")
    await press("F2_RED")
    check(cw == ["continue_watching"], f"rojo en el inicio = Seguir viendo: {cw}")
    out = await press("F4_YELLOW")
    check(not opened(out) and "navigate:https://www.netflix.com/browse/my-list" in out,
          f"amarillo en el inicio = Mi lista (ya no abre Tidal): {[x for x in out if not x.startswith('js:')]}")
    out = await press("NUMBER0")
    check(any(x.startswith("navigate:") and "ProfilesGate" in x for x in out), "0 en el inicio abre ¿Quién está viendo?")
    out = await press("ROOT_MENU")
    check(out[-1] == "ov:show:0" and ctl.overlay.items[0]["current"], f"Menú sobre Netflix abre el panel: {out}")
    out = await press("DOWN", "UP", "DOWN")
    check(out == ["ov:select:1", "ov:select:0", "ov:select:1"],
          f"con el menú abierto las teclas no tocan la página de Netflix: {out}")
    out = await press("SELECT")
    check("browser:close" in out and out.index("browser:close") < out.index("music:launch"),
          f"Netflix → Tidal: cierra el navegador y después abre Tidal: {opened(out)}")

    print("Retomar cuando High Tide no puede (lo último fue «Mis canciones»)")
    await press("ROOT_MENU", "NUMBER1")
    ctl.music.can_resume = False
    out = await press("ROOT_MENU", "NUMBER2")
    check([x for x in out if x.startswith("music:")][-3:] == ["music:launch", "music:resume", "music:my_tracks@11"],
          f"abre Tidal y retoma «Mis canciones» en el tema guardado: {[x for x in out if x.startswith('music:')]}")
    ctl.music.can_resume = True

    print("Tidal como viene por defecto: en Inicio, en pausa y con las flechas recorriendo la interfaz")
    await press("ROOT_MENU", "NUMBER1")
    ctl.music.resume_on_open, ctl.music.start_in_nav = False, True
    out = await press("ROOT_MENU", "NUMBER2")
    music = [x for x in out if x.startswith("music:")]
    check(music[-1:] == ["music:launch"], f"abre Tidal sin poner música: {music}")
    check(await ctl.context() == "tidal_nav", "arranca con las flechas recorriendo la interfaz")
    out = await press("DOWN", "SELECT", "NUMBER0", "DOWN")
    check(out == ["wtype:Down", "wtype:Return", "nav:stop", "mpris:volume-"], f"▼ y OK van a la app; 0 pasa a controles: {out}")
    ctl.music.resume_on_open, ctl.music.start_in_nav = True, False
    ctl._tidal_nav = False

    print("Autocierre y teclas durante el cambio")
    ctl._menu_timeout = 0.2
    await press("ROOT_MENU")
    await asyncio.sleep(0.4)
    check(not ctl._menu_open and log[-1] == "ov:hide", "sin teclas, el menú se cierra solo")
    ctl.current_app = lambda: None  # type: ignore[method-assign]  # sin nada abierto
    await press("ROOT_MENU")
    await asyncio.sleep(0.4)
    check(ctl._menu_open, "sin nada abierto, el menú es la pantalla de inicio: no se cierra solo")
    del ctl.current_app
    await ctl.menu_close()
    ctl._menu_timeout = 10
    ctl._switching = True
    out = await press("ROOT_MENU", "DOWN", "SELECT")
    check(out == [], f"mientras se cambia de actividad, las teclas se ignoran: {out}")
    ctl._switching = False

    print("Elegir otra carpeta (▶ sobre Videos)")
    usb = tmp / "pick" / "media" / "gaby" / "USB"
    for d in ("Series/T1", "Series/T2", "Pelis", ".oculta"):
        (usb / d).mkdir(parents=True)
    (usb / "Pelis" / "a.mp4").write_bytes(b"")
    (usb / "Series" / "T1" / "e1.mkv").write_bytes(b"")
    (tmp / "pick" / "Vídeos").mkdir()
    videos.folders = [str(tmp / "pick" / "media" / "*" / "*"), str(tmp / "pick" / "Vídeos")]
    await ctl.menu_close()
    out = await press("ROOT_MENU")
    vid = next(i for i, it in enumerate(ctl.overlay.items) if it["id"] == "videos")
    for _ in range(6):
        if ctl._menu_mode == "activities" and ctl._menu_sel == vid:
            break
        await press("DOWN")
    out = await press("RIGHT")
    names = [it["name"] for it in ctl.overlay.items]
    check(ctl._menu_mode == "folders" and len(names) == 2 and ctl.overlay.items[0]["sub"] == "predeterminada",
          f"▶ sobre Videos: las carpetas configuradas, con la predeterminada marcada: {names}")
    out = await press("RIGHT")
    names = [it["name"] for it in ctl.overlay.items]
    check(names == ["▶ Reproducir esta carpeta", "Pelis", "Series"], f"▶ entra: primero reproducir, después subcarpetas (sin ocultas): {names}")
    await press("DOWN", "DOWN")
    out = await press("SELECT")
    check([it["name"] for it in ctl.overlay.items] == ["▶ Reproducir esta carpeta", "T1", "T2"], "OK sobre una subcarpeta también entra")
    out = await press("LEFT")
    check(ctl._menu_sel == 2 and [it["name"] for it in ctl.overlay.items][2] == "Series",
          f"◀ sube y vuelve a la misma fila: {ctl._menu_sel}")
    await press("RIGHT", "DOWN", "RIGHT")  # Series → T1
    check(ctl.overlay.items[0]["kind"] == "play" and ctl._menu_sel == 0, "en T1, la primera opción es reproducir")
    out = await press("SELECT")
    check(opened(out)[-1:] == ["videos:open:T1"] and not ctl._menu_open,
          f"Reproducir abre VLC en esa carpeta y cierra el menú: {opened(out)}")
    await press("ROOT_MENU")
    for _ in range(6):
        if ctl._menu_mode == "activities" and ctl._menu_sel == vid:
            break
        await press("DOWN")
    await press("RIGHT", "RIGHT", "SELECT")
    out = log[-6:]
    check("videos:close" in out and out.index("videos:close") < out.index("videos:open:USB"),
          f"con Videos ya abierto, otra carpeta lo reabre: {opened(out)}")
    await press("ROOT_MENU")
    for _ in range(6):
        if ctl._menu_mode == "activities" and ctl._menu_sel == vid:
            break
        await press("DOWN")
    await press("RIGHT", "DOWN", "RIGHT")  # ~/Vídeos (vacía)
    check(ctl.overlay.items[0]["enabled"] is False and "No hay videos" in ctl.overlay.items[0]["sub"],
          f"en una carpeta sin videos, «Reproducir» aparece en gris: {ctl.overlay.items[0]}")
    out = await press("SELECT")
    check(not opened(out) and any(x.startswith("ov:toast:No hay videos") for x in out) and ctl._menu_open,
          f"elegirla avisa y no abre nada: {out}")
    await press("LEFT")
    out = await press("EXIT")
    check(ctl._menu_mode == "activities" and out[-1] == f"ov:show:{vid}",
          f"Atrás en las carpetas configuradas vuelve al menú, en Videos: {out}")
    out = await press("EXIT")
    check(out == ["ov:hide"] and not ctl._menu_open, "Atrás otra vez cierra el menú")

    print("app:<actividad> (para ponerla en un color)")
    await ctl.run_action("app:tidal")
    start = len(log)
    await ctl.run_action("app:videos")
    check(opened(log[start:]) == ["music:quit", "videos:open"], f"app:videos cambia directo: {opened(log[start:])}")

    print("Standby de la TV")
    log.clear()
    await ctl._pause_if_playing()
    check(log == ["vlc:pause"], f"con Videos: pausa VLC: {log}")
    await ctl.run_action("app:tidal")
    log.clear()
    await ctl._pause_if_playing()
    check(log == ["mpris:pause"], f"con Tidal sonando: pausa la música: {log}")

    print("Música de fondo mientras pasan las fotos")
    fotos.content = True
    out = await press("ROOT_MENU", "NUMBER4")
    check("music:quit" not in out and opened(out) == ["fotos:open"],
          f"abrir Fotos con Tidal sonando no cierra la música: {opened(out)}")
    check(await ctl.context() == "fotos" and ctl.current_app() == "fotos",
          "las teclas van a Fotos (la que está en pantalla)")
    out = await press("PAUSE_PLAY_FUNCTION", "FORWARD", "BACKWARD")
    check([x for x in out if x.startswith("mpris:")] == ["mpris:play-pause", "mpris:next", "mpris:previous"]
          and any(x.startswith("ov:toast:♪") for x in out),
          f"⏯ ⏭ ⏮ manejan la música (y avisan qué suena): {out}")
    out = await press("SELECT", "RIGHT")
    check(out == ["feh:h", "feh:next"], f"OK pausa las fotos y ▶ pasa de foto: {out}")
    await press("SELECT")
    await press("ROOT_MENU")
    tid = next(it for it in ctl.overlay.items if it["id"] == "tidal")
    check(tid.get("badge") == "sonando", f"en el menú Tidal figura «sonando»: {tid}")
    await press("EXIT")
    log.clear()
    await ctl._pause_if_playing()
    check(log == ["mpris:pause"], f"standby con fotos y música: pausa la música: {log}")
    out = await press("ROOT_MENU", "NUMBER1")
    check("fotos:close" in out and "music:quit" in out and "browser:launch" in out,
          f"ir a Netflix cierra las fotos y la música: {opened(out)}")
    fotos.on = False
    out = await press("ROOT_MENU", "NUMBER4")
    out = await press("PAUSE_PLAY_FUNCTION", "FORWARD")
    check(out == ["feh:h", "feh:next"], f"sin música, ⏯ pausa las fotos y ⏭ pasa de foto: {out}")

    print("Ayuda")
    out = await press("ROOT_MENU")
    for _ in range(6):
        if ctl.overlay.items[ctl._menu_sel]["id"] == "_help":
            break
        await press("UP")
    out = await press("SELECT")
    check(out == ["ov:help:Ayuda · Fotos"] and await ctl.context() == "menu",
          f"«Ayuda» en el menú empieza por la app que se estaba usando: {out}")
    check(any(k.startswith("Rojo") and "Girar" in d for k, d in ctl.overlay.help_rows),
          f"y lista las teclas de esa app: {ctl.overlay.help_rows[:4]}")
    out = await press("RIGHT")
    check(out and out[-1].startswith("ov:help:Ayuda · Fotos · con zoom"), f"▶ pasa a la página siguiente: {out}")
    out = await press("PLAY")
    check(out == [], f"en la ayuda las demás teclas no hacen nada: {out}")
    out = await press("EXIT")
    check(out and out[-1].startswith("ov:show:") and ctl._menu_mode == "activities",
          f"Atrás vuelve al menú: {out}")
    await press("EXIT")
    await ctl.run_action("app:tidal")
    ctl._ctx = await ctl.context()
    out = await press("HELP")
    check(out == ["ov:help:Ayuda · Tidal · controles (después de 0)"], f"la tecla Ayuda abre la ayuda de la app actual: {out}")
    out = await press("EXIT")
    check(out == ["ov:hide"] and not ctl._menu_open, f"Atrás la cierra: {out}")

    print("YouTube")
    class FakeYouTube(FakeProcMixin, activities_mod.YouTubeActivity):
        def __init__(self, log):
            activities_mod.YouTubeActivity.__init__(self, {})
            self.setup(log)
            self.mpris = FakeMpris(log, "firefox")
    yt = FakeYouTube(log)
    ctl.activities["youtube"] = yt
    out = await ctl.run_action("app:youtube") or log[-3:]
    check(ctl.current_app() == "youtube" and await ctl.context() == "youtube", "app:youtube abre YouTube (contexto 'youtube')")
    out = await press("DOWN", "RIGHT", "SELECT", "EXIT", "PAUSE_PLAY_FUNCTION", "FAST_FORWARD", "F1_BLUE")
    check(out == ["wtype:Down", "wtype:Right", "wtype:Return", "wtype:Escape", "firefox:play-pause", "firefox:seek+10"],
          f"flechas, OK y Atrás van tal cual a YouTube; ⏯ y ⏩ por MPRIS; azul nada: {out}")
    rows = dict((d, k) for k, d in help_rows(ctl, "youtube"))
    check("Elegir / OK" in rows and "Volver" in rows, f"la ayuda tiene página de YouTube: {list(rows)[:5]}")
    await ctl.run_action("app:tidal")
    check(not yt.on, "abrir otra app cierra YouTube")
    del ctl.activities["youtube"]

    print("YouTube con Chromium")
    ych = activities_mod.YouTubeActivity({}, {"engine": "chromium"})
    yargs = ych.args("/usr/bin/chromium")
    check(ych.engine == "chromium" and ych.profile.name == "chromium-youtube" and ych.mpris.player == "chromium",
          f"sigue browser.engine: perfil y MPRIS de Chromium: {ych.profile} {ych.mpris.player}")
    check(any(a.startswith("--user-agent=") and "Cobalt" in a for a in yargs)
          and any(a.startswith("--load-extension=") and a.endswith("h264") for a in yargs)
          and "--kiosk" in yargs and yargs[-1] == "https://www.youtube.com/tv",
          f"con user agent de TV, la extensión H.264 y en kiosco: {yargs}")
    check(activities_mod.YouTubeActivity({}, {"engine": "firefox"}).engine == "firefox"
          and activities_mod.YouTubeActivity({"engine": "firefox"}, {"engine": "chromium"}).engine == "firefox",
          "Firefox por defecto, y [apps.youtube] engine manda")
    fake_argv = {101: ["/usr/lib/chromium/chromium", f"--user-data-dir={ych.profile}", "https://www.youtube.com/tv"],
                 102: ["/usr/lib/chromium/chromium", "--type=renderer", f"--user-data-dir={ych.profile}"],
                 # Chromium reescribe su línea de comandos en /proc como un solo texto:
                 104: [f"/usr/lib/chromium/chromium --user-data-dir={ych.profile} --kiosk https://www.youtube.com/tv"],
                 105: [f"/usr/lib/chromium/chromium --type=gpu-process --user-data-dir={ych.profile}"],
                 103: ["/usr/lib/chromium/chromium", "--user-data-dir=/home/x/.config/chromium-netflix"]}
    from tele_cec.system import procs as procs_mod
    real_table = procs_mod.proc_table
    procs_mod.proc_table = lambda fresh=False: list(fake_argv.items())  # type: ignore[assignment]
    try:
        check(ych.pids() == [101, 104], f"encuentra solo el proceso principal de su perfil: {ych.pids()}")
    finally:
        procs_mod.proc_table = real_table  # type: ignore[assignment]

    print("Apagado automático")
    from tele_cec.system import power as power_mod
    from tele_cec.system.cec import CecFrame
    pw = power_mod.IdleShutdown(ctl, {"enabled": True, "mode": "apagar", "idle_minutes": 180, "tv_off_minutes": 15,
                                      "warn_seconds": 2, "command": ["true"]})
    ctl.power = pw
    power_mod.audio_playing = lambda: False  # type: ignore[assignment]  # que no dependa del sonido de esta máquina
    now = pw.last_activity
    check(pw.reason(now + 179 * 60) is None and pw.reason(now + 181 * 60), "a las 3 h sin sonido ni teclas: apagar")
    pw.on_frame(CecFrame(initiator=0, destination=15, opcode=0x36))
    check(pw.tv_off_since is not None and pw.reason(pw.tv_off_since + 16 * 60), "TV en standby: a los 15 min")
    pw.on_frame(CecFrame(initiator=0, destination=15, opcode=0x86))
    check(pw.tv_off_since is None and pw.reason(now + 16 * 60) is None, "la TV vuelve a avisar que está en uso")
    await press("EXIT")
    check(pw.last_activity > now, "una tecla reinicia la cuenta")
    sent: list[str] = []

    class FakeSource:
        async def send(self, cmd: str) -> bool:
            sent.append(cmd)
            return True

        async def activate_source(self) -> None:
            sent.append("as")
    ctl.source = FakeSource()
    task = asyncio.create_task(pw._count_and_rest("prueba"))
    pw._countdown = task
    await asyncio.sleep(0.3)
    start = len(log)
    await press("EXIT")
    await asyncio.sleep(0.1)
    check(task.cancelled() or task.done(), "una tecla durante la cuenta regresiva cancela el apagado")
    check("ov:toast:Cancelado" in log[start:], f"y avisa: {log[start:]}")
    check(sent == [], f"sin apagar la TV: {sent}")
    pw.tv_standby = True
    real_sleep = asyncio.sleep

    async def fast_sleep(t, *a):
        await real_sleep(0)
    power_mod.asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        start = len(log)
        await pw._count_and_rest("prueba")
    finally:
        power_mod.asyncio.sleep = real_sleep  # type: ignore[assignment]
    check(sent == ["standby 0"], f"al apagar: primero la TV por CEC: {sent}")
    check(any("se apaga en 2 s" in x for x in log[start:]) and "ov:toast:Apagando…" in log[start:],
          f"con cuenta regresiva en pantalla: {[x for x in log[start:] if x.startswith('ov:')]}")

    print("Reposo")
    rp = power_mod.IdleShutdown(ctl, {"enabled": True, "warn_seconds": 2, "resume_apps": True})
    ctl.power = rp
    check(rp.mode == "reposo" and rp.verb == "entra en reposo", "el modo por defecto es reposo")
    check(not power_mod.IdleShutdown(ctl, {}).resume_apps, "por defecto, al volver muestra el menú (resume_apps = false)")
    await ctl.run_action("app:tidal")
    await ctl.switch_to("fotos", tmp / "pick" / "media" / "gaby" / "USB")
    check(ctl.current_app() == "fotos" and ctl.music.running(), "fotos en pantalla con música de fondo")
    sent.clear()
    start = len(log)
    await rp.sleep("prueba")
    out = log[start:]
    check(rp.asleep and rp.saved.get("app") == "fotos" and rp.saved.get("music") and rp.saved.get("folder", "").endswith("USB"),
          f"guarda qué estaba abierto: {rp.saved}")
    check("mpris:pause" in out and "fotos:close" in out and "music:quit" in out and sent == ["standby 0"],
          f"pausa, cierra todo y apaga la TV: {[x for x in out if not x.startswith('ov:')]} {sent}")
    start = len(log)
    out = await press("RIGHT")
    check(not any(x.startswith("feh:") for x in out), f"la tecla que despierta no hace nada más: {out}")
    await rp._wake_task
    out = log[start:]
    check("music:launch" in out and "music:resume" not in out and "fotos:open:USB" in out,
          f"al volver: la música (sin ponerla) y las fotos en la misma carpeta: {opened(out)}")
    check(sent[-1:] == ["as"] and ctl.current_app() == "fotos", f"despertada con el control: la TV cambia a la Pi: {sent}")
    check(any(x.startswith("ov:busy:Volviendo a Fotos") for x in out), "muestra «Volviendo a Fotos…»")

    print("Tecla mientras entra en reposo")
    real_sleep_apps = ctl.sleep_apps
    entering_seen: list[bool] = []

    async def slow_sleep_apps():
        entering_seen.append(rp.entering)
        out = await press("LEFT")  # alguien aprieta una tecla a mitad de camino
        entering_seen.append(bool(out))
        await real_sleep_apps()
    ctl.sleep_apps = slow_sleep_apps  # type: ignore[method-assign]
    start = len(log)
    await rp.sleep("prueba")
    ctl.sleep_apps = real_sleep_apps  # type: ignore[method-assign]
    if rp._wake_task:
        await rp._wake_task
    out = log[start:]
    check(entering_seen == [True, False] and "fotos:close" in out and "fotos:open:USB" in out and not rp.asleep,
          f"no se corta a la mitad: termina de entrar y vuelve enseguida: {entering_seen} {opened(out)}")

    print("Varios días en reposo: apagarla de verdad")
    lp = power_mod.IdleShutdown(ctl, {"enabled": True, "command": ["true"]})
    check(lp.off_after == 72 * 3600, f"por defecto, a las 72 h seguidas en reposo: {lp.off_after / 3600:g} h")
    lp.asleep, lp._asleep_at = True, 0.0
    sent.clear()
    start = len(log)
    await lp.long_rest_off()
    check("standby 0" not in sent and "ov:toast:Apagando…" in log[start:],
          f"se apaga sin tocar la TV (puede estar mirando otra entrada): {sent} {log[start:]}")
    lp.command = ["false"]
    before = lp._asleep_at
    await lp.long_rest_off()
    check(lp._asleep_at > before, "si no puede apagar, reintenta recién dentro de otro período")

    print("Reposo con Netflix y despertar al prender la TV")
    await ctl.run_action("app:netflix")
    await ctl.page.navigate("https://www.netflix.com/watch/80001?trackId=1")
    sent.clear()
    await rp.sleep("prueba")
    check(rp.saved == {"app": "netflix", "url": "https://www.netflix.com/watch/80001?trackId=1"},
          f"guarda la página de Netflix: {rp.saved}")
    check(ctl.page is None, "cerró el navegador")
    rp.on_frame(CecFrame(initiator=0, destination=1, opcode=0x90, params=[0x00]))
    check(rp.asleep, "recién dormida, un «prendida» de la TV se ignora")
    rp._asleep_at -= 60
    rp.on_frame(CecFrame(initiator=0, destination=1, opcode=0x90, params=[0x01]))
    check(rp.asleep, "«en standby» no la despierta")
    start = len(log)
    rp.on_frame(CecFrame(initiator=0, destination=1, opcode=0x90, params=[0x00]))
    check(not rp.asleep, "la TV prendida la despierta")
    await rp._wake_task
    out = log[start:]
    check("browser:launch" in out and "navigate:https://www.netflix.com/watch/80001?trackId=1" in out and "js:pause" in out,
          f"vuelve al mismo título y lo pausa: {[x for x in out if not x.startswith(('js:ctx', 'ov:'))]}")
    check("as" not in sent, f"despertada por la TV: no le cambia la entrada: {sent}")

    print("Reposo por defecto: al volver, el menú")
    dp = power_mod.IdleShutdown(ctl, {"enabled": True})
    ctl.power = dp
    await ctl.run_action("app:netflix")
    sent.clear()
    await dp.sleep("prueba")
    check(dp.asleep and dp.saved == {} and ctl.page is None, f"no guarda nada y cierra todo: {dp.saved}")
    start = len(log)
    await press("RIGHT")
    await dp._wake_task
    out = log[start:]
    check(not opened(out) and ctl._menu_open and "ov:show:0" in out, f"con una tecla: no abre nada, muestra el menú: {out}")
    check(sent[-1:] == ["as"], f"y la TV cambia a la Pi: {sent}")
    await ctl.menu_close()
    await dp.sleep("prueba")
    dp._asleep_at -= 60
    sent.clear()
    start = len(log)
    dp.on_frame(CecFrame(initiator=0, destination=1, opcode=0x90, params=[0x00]))
    await dp._wake_task
    out = log[start:]
    check(not opened(out) and ctl._menu_open and "as" not in sent,
          f"al prender la TV: el menú, sin cambiarle la entrada: {out} {sent}")
    await ctl.menu_close()
    ctl.power = power_mod.IdleShutdown(ctl, {})

    print("Menú al arrancar")
    ov_running = True
    ctl.overlay.running = lambda: ov_running  # type: ignore[attr-defined]

    async def no_warm() -> None:
        return None
    ctl.overlay.warm = no_warm  # type: ignore[attr-defined]
    real_sleep = asyncio.sleep

    async def quick_sleep(t, *a):
        await real_sleep(0)
    from tele_cec.controller import menu as menu_mod
    menu_mod.asyncio.sleep = quick_sleep  # type: ignore[assignment]
    try:
        start = len(log)
        await ctl.startup_menu()
        check(ctl._menu_open and log[start:] == ["ov:show:0"], f"sin nada abierto: el menú aparece solo: {log[start:]}")
        await ctl.menu_close()
        await ctl.run_action("app:tidal")
        start = len(log)
        await ctl.startup_menu()
        check(not ctl._menu_open and log[start:] == [], "con algo abierto (servicio reiniciado): no lo tapa")
        ctl.cfg.menu["open_on_start"] = False
        await ctl.run_action("app:netflix")
        await ctl.browser.close()
        await ctl.startup_menu()
        check(not ctl._menu_open, "menu.open_on_start = false: no")
        ctl.cfg.menu["open_on_start"] = True
    finally:
        menu_mod.asyncio.sleep = real_sleep  # type: ignore[assignment]

    print("Sonido")
    menu_names = lambda: [it["name"] for it in ctl.overlay.items]  # noqa: E731
    from tele_cec.controller import menu as menu_mod
    from tele_cec.system import audio as audio_mod
    outs = [audio_mod.Output("TV (HDMI)", "hdmi", 53, "alsa_output.hdmi", default=True),
            audio_mod.Output("Parlante JBL", "bluetooth", None, mac="AA:BB:CC:DD:EE:FF")]
    chosen: list[str] = []
    nearby = [[audio_mod.Output("Auriculares Sony", "bluetooth", mac="11:22:33:44:55:66", paired=False)]]

    async def fake_select(o):
        chosen.append(o.name)
        if all(x.name != o.name for x in outs):
            outs.append(audio_mod.Output(**vars(o)))
        for x in outs:
            x.default = x.name == o.name
            x.paired = True
        return True, f"Sonido por {o.name}"

    class FakeScan:
        def __init__(self):
            self.on = False
            self.starts = 0

        def available(self):
            return True

        async def start(self, seconds=25):
            self.on, self.starts = True, self.starts + 1
            return True

        def running(self):
            return self.on

        async def stop(self):
            self.on = False

    def fake_nearby(skip):
        found = nearby.pop(0) if nearby else []
        return [o for o in found if o.mac not in skip]
    saved_audio = (audio_mod.available, audio_mod.list_outputs, audio_mod.current_name, audio_mod.select,
                   audio_mod.bt_nearby_audio, menu_mod.BT_SCAN_SECONDS, menu_mod.BT_POLL_SECONDS, ctl._bt_scan)
    audio_mod.available = lambda: True  # type: ignore[assignment]
    audio_mod.list_outputs = lambda: [audio_mod.Output(**vars(o)) for o in outs]  # type: ignore[assignment]
    audio_mod.current_name = lambda: next(o.name for o in outs if o.default)  # type: ignore[assignment]
    audio_mod.select = fake_select  # type: ignore[assignment]
    audio_mod.bt_nearby_audio = fake_nearby  # type: ignore[assignment]
    menu_mod.BT_SCAN_SECONDS, menu_mod.BT_POLL_SECONDS = 0.3, 0.02  # type: ignore[assignment]
    scan = FakeScan()
    ctl._bt_scan = scan  # type: ignore[assignment]
    ctl._sound_on = True
    try:
        await press("ROOT_MENU")
        check(menu_names()[-2:] == ["Sonido", "Ayuda"] and ctl.overlay.items[-2]["sub"] == "TV (HDMI)",
              f"«Sonido» antes de la ayuda, con la salida actual: {menu_names()} {ctl.overlay.items[-2]['sub']}")
        snd = menu_names().index("Sonido") + 1
        nearby_backup = list(nearby)
        nearby.clear()  # primero, sin nada nuevo cerca
        await press(f"NUMBER{snd}")
        await asyncio.sleep(0.01)  # que arranque la búsqueda (va en segundo plano)
        check(ctl._menu_mode == "sound" and menu_names() == ["TV (HDMI)", "Parlante JBL", "Buscando parlantes y auriculares…"]
              and ctl.overlay.sel == 0 and scan.starts == 1,
              f"OK abre las salidas al instante, con la actual elegida, y busca Bluetooth: {menu_names()}")
        check("desconectado" in ctl.overlay.items[1]["sub"], f"el Bluetooth desconectado lo dice: {ctl.overlay.items[1]['sub']}")
        start = len(log)
        await press("DOWN", "SELECT")
        out = log[start:]
        check(chosen == ["Parlante JBL"] and "ov:busy:Conectando Parlante JBL…" in out and not scan.on,
              f"elegir el Bluetooth corta la búsqueda y lo conecta: {chosen} {out}")
        check(ctl._menu_mode == "activities" and "ov:toast:Sonido por Parlante JBL" in out
              and ctl.overlay.items[snd - 1]["sub"] == "Parlante JBL", f"vuelve al menú con la salida nueva: {out}")

        nearby[:] = nearby_backup
        await press("RIGHT")
        check(ctl._menu_mode == "sound", "▶ sobre Sonido también abre las salidas")
        await press("DOWN")  # de JBL (la actual) a la fila de búsqueda
        await ctl._sound_scan_task
        check(menu_names() == ["TV (HDMI)", "Parlante JBL", "Auriculares Sony", "Buscar Bluetooth otra vez"]
              and "nuevo" in ctl.overlay.items[2]["sub"] and ctl.overlay.sel == 3,
              f"los auriculares que aparecen cerca se agregan al final (la selección no salta): {menu_names()} sel={ctl.overlay.sel}")
        await press("SELECT")
        await asyncio.sleep(0.01)
        check(scan.starts == 3 and menu_names()[-1] == "Buscando parlantes y auriculares…",
              f"«Buscar Bluetooth otra vez» vuelve a buscar: {menu_names()[-1]}")
        start = len(log)
        await press("UP", "SELECT")
        out = log[start:]
        check(chosen[-1] == "Auriculares Sony" and "ov:busy:Emparejando Auriculares Sony…" in out
              and ctl.overlay.items[snd - 1]["sub"] == "Auriculares Sony",
              f"elegir uno nuevo lo empareja y pasa el sonido ahí: {out}")
        await press("RIGHT")
        out = await press("EXIT")
        check(ctl._menu_mode == "activities" and ctl._menu_open and not scan.on,
              f"Atrás vuelve al menú y corta la búsqueda: {out}")
        await press("EXIT")
    finally:
        (audio_mod.available, audio_mod.list_outputs, audio_mod.current_name, audio_mod.select,
         audio_mod.bt_nearby_audio, menu_mod.BT_SCAN_SECONDS, menu_mod.BT_POLL_SECONDS, ctl._bt_scan) = saved_audio
        ctl._sound_on = False

    print("Carpetas de contenido")
    usb = tmp / "media" / "gaby" / "KINGSTON"
    local = tmp / "Vídeos"
    (local / "serie").mkdir(parents=True)
    (local / "serie" / "cap1.MKV").write_bytes(b"")
    usb.mkdir(parents=True)
    (usb / "notas.txt").write_text("x")
    pats = [str(tmp / "media" / "*" / "*"), str(local)]
    found = activities_mod.find_media_folder(pats, activities_mod.VIDEO_EXT)
    check(found == local, f"pendrive sin videos: usa la carpeta local: {found}")
    (usb / "peli.mp4").write_bytes(b"")
    found = activities_mod.find_media_folder(pats, activities_mod.VIDEO_EXT)
    check(found == usb and activities_mod.describe_folder(Path("/media/gaby/KINGSTON")) == "Pendrive KINGSTON",
          f"con videos en el pendrive, va primero: {found}")
    check(activities_mod.find_media_folder(pats, activities_mod.IMAGE_EXT) is None, "sin fotos en ninguna: None")
    v = activities_mod.VideosActivity({"folders": pats, "command": ["vlc", "--qt-minimal-view", "{carpeta}"]})
    check(activities_mod._custom_command(v.command, usb) == ["vlc", "--qt-minimal-view", str(usb)]
          and v.procs == {"vlc"}, "command propio: {carpeta} se reemplaza")
    fotos_dir = tmp / "fotos" / "2020"
    fotos_dir.mkdir(parents=True)
    for n in ("b.jpg", "a.JPG", ".oculta.jpg", "nota.txt"):
        (fotos_dir / n).write_bytes(b"")
    ph = activities_mod.PhotosActivity({"folders": [str(tmp / "fotos")]})
    ph_list = ph.list_file(tmp / "fotos")
    ph.list_file = lambda folder: tmp / "cache" / "fotos.lst"  # type: ignore[method-assign]  # en la prueba, fuera de la caché
    n = ph.write_list(tmp / "fotos")
    lines = (tmp / "cache" / "fotos.lst").read_text().split()
    check(n == 2 and [Path(x).name for x in lines] == ["a.JPG", "b.jpg"] and ph_list.name.startswith("fotos-"),
          f"lista de fotos guardada (sin ocultos ni otros archivos): {[Path(x).name for x in lines]}")
    fargs = ph.default_args(tmp / "fotos", tmp / "cache" / "fotos.feh.lst")
    check(fargs[-2:] == ["--filelist", str(tmp / "cache" / "fotos.feh.lst")] and "--recursive" not in fargs,
          f"feh arranca con la lista (--filelist), sin recorrer la carpeta: {fargs[-2:]}")
    args = activities_mod.VideosActivity({}).default_args(usb)
    check(args[:3] == ["vlc", "-I", "dummy"] and "--extraintf=dbus:oldrc" in args
          and any(a.startswith("--rc-unix=") for a in args) and args[-1] == str(usb),
          f"VLC por defecto sin interfaz, con MPRIS y la carpeta al final: {args}")

    print("Aviso de emparejamiento del panel del escritorio")
    from tele_cec.system import audio as audio_mod2
    from tele_cec.system import desktop as desktop_mod

    class Node:
        def __init__(self, role, name="", children=(), showing=True, on_click=None):
            self.role, self.name, self.children, self.showing, self.on_click = role, name, list(children), showing, on_click

        def get_child_count(self):
            return len(self.children)

        def get_child_at_index(self, i):
            return self.children[i]

        def get_role_name(self):
            return self.role

        def get_name(self):
            return self.name

        def get_state_set(self):
            node = self

            class States:
                def contains(self, s):
                    return node.showing
            return States()

        def get_action_iface(self):
            node = self

            class Act:
                def get_n_actions(self):
                    return 1 if node.on_click else 0

                def do_action(self, i):
                    node.on_click(node)
                    return True
            return Act()

    clicked: list[str] = []

    def click(n):
        clicked.append(n.name)

    def pair_dialog(*body):
        return Node("frame", "Pairing...", [Node("filler", "", body)])

    notice = pair_dialog(Node("label", "Connection successful"), Node("text", showing=False),
                         Node("filler", "", [Node("button", "Cancel", showing=False, on_click=click),
                                             Node("button", "OK", on_click=click)]))
    asks = pair_dialog(Node("label", "Please confirm that 'X' is showing the code '123456' to pair"),
                       Node("filler", "", [Node("button", "Cancel", on_click=click),
                                           Node("button", "OK", on_click=click)]))
    other = Node("frame", "Add New Device", [Node("button", "OK", on_click=click)])
    bar = Node("frame", "", [Node("button", "Menú", on_click=click)])
    desk = Node("desktop", "", [Node("application", "high-tide", [Node("frame", "Pairing...", [
        Node("button", "OK", on_click=click)])]), Node("application", "wf-panel-pi", [bar, other, asks, notice])])

    class FakeAtspi:
        class StateType:
            SHOWING = "showing"

        @staticmethod
        def get_desktop(i):
            return desk

    real_atspi = desktop_mod._atspi
    desktop_mod._atspi = lambda: FakeAtspi  # type: ignore[assignment]
    closed = desktop_mod.close_pairing_notices()
    check(clicked == ["OK"] and closed == ["Connection successful"],
          f"cierra solo el aviso del panel que tiene únicamente OK: {clicked} {closed}")
    desktop_mod._atspi = lambda: None  # type: ignore[assignment]
    check(desktop_mod.close_pairing_notices() == [], "sin accesibilidad no hace nada")
    desktop_mod._atspi = real_atspi  # type: ignore[assignment]

    closer_log: list[str] = []

    class FakeCloser:
        def start(self):
            closer_log.append("start")

        def finish(self, grace=8.0):
            closer_log.append("finish")

    async def fail_pair(o, timeout=25.0):
        closer_log.append("pair")
        return False, "no"

    real_closer, real_pair = desktop_mod.PairingNoticeCloser, audio_mod2.pair
    desktop_mod.PairingNoticeCloser = FakeCloser  # type: ignore[assignment,misc]
    audio_mod2.pair = fail_pair  # type: ignore[assignment]
    try:
        r = await audio_mod2.select(audio_mod2.Output("KN320", "bluetooth", mac="8B:D9:43:D1:A1:AE", paired=False))
        check(r == (False, "no") and closer_log == ["start", "pair", "finish"],
              f"al emparejar se vigilan los avisos del panel (y se suelta aunque falle): {closer_log}")
        closer_log.clear()
        await audio_mod2.select(audio_mod2.Output("TV (HDMI)", "hdmi", None))
        check(closer_log == [], "cambiar a una salida ya emparejada no vigila nada")
    finally:
        desktop_mod.PairingNoticeCloser = real_closer  # type: ignore[misc]
        audio_mod2.pair = real_pair  # type: ignore[assignment]

    bad = [t for ok, t in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} verificaciones OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
