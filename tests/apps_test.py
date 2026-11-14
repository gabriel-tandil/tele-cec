#!/usr/bin/env python3
"""Prueba del menú de actividades (Netflix, Tidal, Videos, Fotos) con navegador, High Tide, VLC, feh y
panel simulados.

No abre nada: verifica qué hace cada tecla según qué actividad está abierta y si el menú está abierto.

    python3 tests/apps_test.py
"""

from __future__ import annotations

import asyncio
import os
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
from tele_cec.keys import key_name, parse_key  # noqa: E402

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

    state = "Playing"

    async def status(self) -> dict:
        if not self.state:
            return {"status": "Stopped"}
        return {"status": self.state, "title": "Volver a Volver", "artist": "Gabo Ferro"}

    async def command(self, line: str, timeout: float = 10.0, wait: float = 0.0) -> str:
        self.log.append(f"music:cmd:{line}")
        return "ok"

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

    async def show(self, items, selected, title="Actividades", hint="", numbers=True, columns=1, chip=False,
                   qr=None) -> bool:
        self.items, self.sel, self.visible, self.columns, self.qr = items, selected, True, columns, qr
        self.log.append(f"ov:show:{selected}")
        return True

    async def help(self, title, rows, top, page, hint) -> bool:
        self.help_rows = rows
        self.log.append(f"ov:help:{title}")
        return True

    async def select(self, index: int, chip: bool = False) -> bool:
        self.sel, self.chip = index, chip
        self.log.append(f"ov:select:{index}" + (":chip" if chip else ""))
        return True

    async def image(self, title, path, lines, status="", hint="", size=9) -> bool:
        self.title, self.image_path, self.status_text = title, path, status
        self.log.append(f"ov:image:{title}")
        return True

    async def status(self, text: str) -> bool:
        self.status_text = text
        self.log.append(f"ov:status:{text}")
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

    async def open(self, folder=None, mode=None, start=None) -> bool:
        self.log.append(f"{self.id}:open" + (f":{Path(folder).name}" if folder else "")
                        + (f":{mode}" if mode and mode != "photos" else "") + (f":desde:{Path(start).name}" if start else ""))
        self.on = True
        if mode:
            self.mode = mode
        if hasattr(self, "reset_view"):  # como el real: abrir Fotos empieza sin zoom
            self.reset_view()
        if folder:
            self.current_folder = Path(folder)
        return True

    async def close(self) -> None:
        self.log.append(f"{self.id}:close")
        self.on = False


class FakePlayerMixin:
    """Videos o Música (mpv) simulados: lo que se le pide queda en el registro como «mpv:…»."""

    async def play_pause(self) -> bool:
        self.log.append("mpv:ok")
        return True

    async def set_pause(self, on: bool) -> bool:
        self.log.append(f"mpv:pause={on}")
        return True

    async def pause(self) -> None:
        self.log.append("mpv:pause")

    async def seek(self, seconds: int) -> bool:
        self.log.append(f"mpv:seek{seconds:+d}")
        return True

    async def next(self) -> bool:
        self.log.append("mpv:next")
        return True

    async def previous(self) -> bool:
        self.log.append("mpv:previous")
        return True

    async def volume(self, sign: str):
        self.log.append(f"mpv:volume{sign}")
        return 55

    async def osd(self, message, *args) -> bool:
        self.log.append("mpv:osd:" + " ".join([message, *map(str, args)]).strip())
        return True

    async def toggle_shuffle(self) -> bool:
        self.log.append("mpv:shuffle")
        self.shuffled = not getattr(self, "shuffled", False)
        return self.shuffled

    async def cycle(self, kind: str) -> str:
        self.log.append(f"mpv:{kind}")
        return {"sub": "Español", "audio": "Inglés", "aspect": "4:3"}[kind]

    async def playback(self) -> str:
        return "Playing" if self.on else ""


class FakeVideos(FakePlayerMixin, FakeProcMixin, activities_mod.VideosActivity):
    def __init__(self, log: list[str]):
        activities_mod.VideosActivity.__init__(self, {})
        self.setup(log)
        self.mpris = FakeMpris(log, "mpv")


class FakeMusicLocal(FakePlayerMixin, FakeProcMixin, activities_mod.MusicActivity):
    def __init__(self, log: list[str]):
        activities_mod.MusicActivity.__init__(self, {})
        self.setup(log)


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
    from tele_cec.controller import activities as ctl_activities
    ctl_activities.FOTOS_REMINDER_S = 0.0  # el aviso del modo de Fotos, sin esperar en las pruebas
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
    ctl._menu_columns = 1  # los tests de siempre, con el menú en una columna (la grilla se prueba aparte)
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
    check(opened(out) == ["music:quit", "videos:open"], f"cierra Tidal y abre Videos: {opened(out)}")
    check(await ctl.context() == "videos", "contexto 'videos'")
    out = await press("SELECT", "RIGHT", "LEFT", "FAST_FORWARD", "UP", "FORWARD", "CHANNEL_DOWN", "F1_BLUE", "F2_RED",
                      "F4_YELLOW", "EXIT")
    check(out == ["mpv:ok", "mpv:seek+10", "mpv:seek-10", "mpv:seek+60", "mpv:volume+", "ov:toast:Volumen 55 %",
                  "mpv:next", "mpv:previous", "mpv:sub", "ov:toast:Subtítulos: Español", "mpv:audio",
                  "ov:toast:Audio: Inglés", "mpv:aspect", "ov:toast:Proporción: 4:3", "mpv:osd:cancel"],
          f"OK, ▶ ◀ (buscar), ⏩ (1 min), ▲, ⏭, Canal−, azul, rojo, amarillo, Atrás (deja de buscar): {out}")

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

    print("Fotos: modos (▼)")
    fotos.current_folder = fotos.current_folder or Path("/mnt/fotosRemotas")
    out = await press("DOWN")
    opened_now = [x for x in out if x.startswith("fotos:")]
    check(opened_now == ["fotos:close", f"fotos:open:{fotos.current_folder.name}:mixed:desde:IMG_0001.jpg"]
          and fotos.mode == "mixed" and any(x.startswith("ov:toast:Fotos: fotos y videos") for x in out)
          and ctl.current_app() == "fotos" and not ctl._switching,
          f"▼ pasa a «fotos y videos» en la misma carpeta, desde la foto que se veía, y avisa: {out}")
    out = await press("DOWN")
    check(fotos.mode == "videos" and any(":videos:" in x for x in out), f"▼ otra vez: «solo videos»: {out}")
    out = await press("DOWN")
    check(fotos.mode == "photos" and any(x.startswith("ov:toast:Fotos: solo fotos") for x in out),
          f"y otra vez: vuelve a «solo fotos»: {out}")

    print("Fotos: zoom")
    fotos.RENDER_DELAY = 0.05
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
    check(log == ["mpv:pause"], f"con Videos: los pausa: {log}")
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
    out = await press("PAUSE_PLAY_FUNCTION")
    check(out[:1] == ["mpris:play-pause"] and any(x.startswith("ov:toast:♪") for x in out),
          f"⏯ maneja la música (y avisa qué suena): {out}")
    out = await press("FORWARD", "BACKWARD")
    check(out == ["feh:next", "feh:previous"], f"⏭ ⏮ pasan las fotos aunque suene música: {out}")
    out = await press("UP", "FORWARD")
    check("feh:next" in out and await ctl.context() == "fotos", f"con zoom, ⏭ pasa de foto y sale del zoom: {out}")
    out = await press("SELECT", "RIGHT")
    check(out == ["feh:h", "feh:next"], f"OK pausa las fotos y ▶ pasa de foto: {out}")
    log.clear()
    await fotos._set_video(True)
    check(log == ["mpis:pause".replace("mpis", "mpris")] and ctl._music_by_video,
          f"suena un video de Fotos: la música se pausa: {log}")
    ctl.music.state = "Paused"
    out = await press("PAUSE_PLAY_FUNCTION")
    check(out == ["feh:h"], f"mientras suena el video, ⏯ es del video (no de la música): {out}")
    await press("PAUSE_PLAY_FUNCTION")
    log.clear()
    await fotos._set_video(False)
    check(log == ["mpris:play"] and not ctl._music_by_video, f"terminó el video: la música sigue: {log}")
    ctl.music.state = "Playing"
    await press("SELECT")
    await press("ROOT_MENU")
    tid = next(it for it in ctl.overlay.items if it["id"] == "tidal")
    check(tid.get("badge") == "sonando", f"en el menú Tidal figura «sonando»: {tid}")
    await press("EXIT")
    ctl.music.state = "Paused"
    await press("ROOT_MENU")
    tid = next(it for it in ctl.overlay.items if it["id"] == "tidal")
    check(tid.get("badge") == "en pausa", f"con la música en pausa, Tidal figura «en pausa»: {tid}")
    await press("EXIT")
    ctl.music.state = ""
    await press("ROOT_MENU")
    tid = next(it for it in ctl.overlay.items if it["id"] == "tidal")
    check(tid.get("badge") == "abierto", f"High Tide abierto sin nada cargado: «abierto»: {tid}")
    await press("EXIT")
    ctl.music.state = "Playing"
    log.clear()
    await ctl.run_action("app:tidal")
    check("fotos:close" in log and "music:launch" not in log and "music:cmd:show" in log
          and ctl.current_app() == "tidal",
          f"elegir Tidal (de fondo) desde Fotos cierra las fotos y muestra Tidal, sin reabrirlo: {log}")
    out = await press("ROOT_MENU", "NUMBER4")
    check(opened(out) == ["fotos:open"] and ctl.current_app() == "fotos", f"y se vuelve a las fotos: {out}")
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

    print("Android (scrcpy)")
    android_mod = activities_mod.android

    class FakeAndroid(FakeProcMixin, activities_mod.AndroidActivity):
        def __init__(self, log):
            activities_mod.AndroidActivity.__init__(self, {})
            self.setup(log)
            self.ok = True

        async def open(self, folder=None) -> bool:
            if not self.ok:
                self.fail_text = "Aceptá «Permitir depuración USB» en el celular"
                return False
            return await FakeProcMixin.open(self)

        async def keyevent(self, what) -> bool:
            self.log.append(f"adb:{what}")
            return True

        async def shell(self, command: str) -> bool:
            self.log.append(f"adbsh:{command}")
            return True

        # «Android · conectar»: adb y la red simulados
        net_devices: list = []
        connect_services: list = []
        pairing_services: list = []
        paired: dict = {}

        def devices(self):
            self._last = list(self.net_devices)
            return self._last

        @staticmethod
        def discovery_available() -> bool:
            return True

        def browse(self, kind, timeout=5.0):
            return list(self.pairing_services if kind == android_mod.PAIRING else self.connect_services)

        def pair(self, addr, code):
            self.log.append(f"pair:{addr}:{code}")
            ok = self.paired.get(addr) == code
            return ok, "Successfully paired" if ok else "Failed: Wrong password"

        def connect(self, addr):
            self.log.append(f"connect:{addr}")
            ok = any(s.addr == addr for s in self.connect_services) and self.paired_ok
            if ok and not any(d.serial == addr for d in self.net_devices):
                self.net_devices.append(android_mod.Device(addr, "device", "Pixel_8"))
            return ok

        def write_qr(self, path, payload):
            self.qr = payload
            return True

        async def open(self, folder=None) -> bool:
            if not self.ok:
                self.fail_text = "Aceptá «Permitir depuración USB» en el celular"
                return False
            self.log.append(f"android:open:{self.want_serial}")
            self.serial = self.want_serial or self.serial
            self.want_serial = ""
            self.on = True
            return True
    andr = FakeAndroid(log)
    ctl.activities["android"] = andr
    await ctl.run_action("app:android")
    check(ctl.current_app() == "android" and await ctl.context() == "android",
          "app:android abre Android (contexto 'android')")
    out = await press("UP", "RIGHT", "SELECT", "NUMBER4", "CLEAR")
    check(out == ["wtype:Up", "wtype:Right", "wtype:Return", "wtype:4", "wtype:BackSpace"],
          f"flechas, OK, números y Borrar van a la ventana de scrcpy: {out}")
    out = await press("EXIT", "F1_BLUE", "F2_RED", "PAUSE_PLAY_FUNCTION", "FORWARD", "FAST_FORWARD", "STOP")
    check(out == ["adb:back", "adb:home", "adb:apps", "adb:play_pause", "adb:next", "adb:fast_forward", "adb:stop"],
          f"Atrás, azul (Inicio), rojo (recientes) y las multimedia van por adb (⏭ no es el botón inteligente): {out}")
    out = await press("F4_YELLOW", "F4_YELLOW")
    check(out == ["adbsh:cmd statusbar expand-notifications", "adbsh:cmd statusbar collapse"],
          f"amarillo abre las notificaciones y otra vez las cierra: {out}")
    rows = dict((d, k) for k, d in help_rows(ctl, "android"))
    check(rows.get("Inicio de Android") == "Azul" and rows.get("Atrás (botón de Android)") == "Atrás"
          and "Moverse" in rows and any("Depuración USB" in d for d in rows),
          f"la ayuda tiene página de Android, con las teclas y cómo conectar el celular: {list(rows)[:6]}")
    from tele_cec.ui import help as help_mod
    check(("android", "Android · la pantalla del celular") in help_mod.pages(ctl.cfg, {"netflix", "android"})
          and not any(c == "android" for c, _t in help_mod.pages(ctl.cfg, {"netflix"}))
          and not any(c == "app" for c, _t in help_mod.pages(ctl.cfg, {"netflix", "android"})),
          "la página de Android aparece solo si está instalado (y no cuenta como «Otras apps»)")
    out = await press("HELP")
    check(out == ["ov:help:Ayuda · Android · la pantalla del celular"], f"la tecla Ayuda empieza por Android: {out}")
    await press("EXIT")

    print("App que se cierra sola (el celular se desconectó)")
    check(ctl._left_alone() is None, "con Android abierto, nada que hacer")
    andr.on = False  # scrcpy se cerró: el celular apagó la depuración inalámbrica
    check(ctl._left_alone() is None, "recién abierto (o cambiando de app) todavía no cuenta")
    ctl._switch_until = 0.0
    check(ctl._left_alone() == "android", "después sí: Android se cerró sola")
    start = len(log)
    await ctl._app_gone("android")
    check(log[start:] == ["android:close", "ov:show:0", "ov:toast:Se desconectó el celular"] and ctl._menu_open
          and ctl._left_alone() is None, f"vuelve el menú, con el aviso: {log[start:]}")
    await press("EXIT")
    await ctl.run_action("app:android")
    ctl._switch_until = 0.0
    await ctl.sleep_apps()
    andr.on = False
    check(ctl._left_alone() is None, "las que cierra el reposo no cuentan como cerradas solas")
    await ctl.run_action("app:android")
    await ctl.run_action("app:videos")
    check(not andr.on, "abrir otra app cierra Android")
    print("Android · conectar (vincular por Wi‑Fi con el control)")
    ctl.cfg.menu["items"] = ["netflix", "tidal", "videos", "fotos", "android"]
    andr.on, andr.paired_ok = False, False
    andr.source = lambda: (setattr(andr, "ready", bool([d for d in andr.net_devices if d.ready]))  # type: ignore
                           or (android_mod.NO_DEVICE, True))

    async def settle() -> None:
        for _ in range(20):
            await asyncio.sleep(0)
        if ctl._and_task:
            await asyncio.wait([ctl._and_task], timeout=2)
        if ctl._macro_task:
            await ctl._macro_task
            ctl._macro_task = None

    android_ctl = sys.modules["tele_cec.controller.android"]
    android_ctl.SCAN_EVERY_S = 0.01  # type: ignore[attr-defined]
    fake_usb: list = []
    android_ctl.usb_phones = lambda: list(fake_usb)  # type: ignore[attr-defined]
    await ctl.run_action("app:videos")
    await press("ROOT_MENU")
    idx = [it["id"] for it in ctl._menu_items].index("android")
    out = await press(f"NUMBER{idx + 1}")
    await settle()
    ids = [it["id"] for it in ctl.overlay.items]
    check(ctl._menu_mode == "android" and ids[:3] == ["_qr", "_code", "_usb"] and "_rescan" in ids,
          f"sin celular, OK sobre Android abre «Android · conectar» (QR, código, USB): {ids}")
    out = await press("EXIT")
    check(ctl._menu_mode == "activities" and ctl.overlay.items[ctl._menu_sel]["id"] == "android",
          f"Atrás vuelve al menú, sobre Android: {out}")

    # Vincular con código: el celular lo muestra, se escribe con los números
    out = await press("RIGHT")
    await settle()
    out = await press("DOWN", "SELECT")
    check(ctl._menu_mode == "android_code" and ctl.overlay.items[0]["name"].startswith("Código:"),
          f"«Vincular con código» pide el código: {ctl.overlay.items[:1]}")
    andr.pairing_services = [android_mod.Service("adb-XYZ-1", "192.168.0.60", 37000)]
    andr.connect_services = [android_mod.Service("adb-XYZ-2", "192.168.0.60", 41000)]
    andr.paired = {"192.168.0.60:37000": "123456"}
    await settle()
    check("Celular encontrado" in ctl.overlay.items[0]["sub"], f"encuentra al celular en la red: {ctl.overlay.items[0]}")
    out = await press("NUMBER1", "NUMBER2", "NUMBER9", "LEFT", "NUMBER3")
    check(ctl.overlay.items[0]["name"] == "Código:  1  2  3  _  _  _", f"los números escriben y ◀ borra: {ctl.overlay.items[0]['name']}")
    andr.paired_ok = True
    out = await press("NUMBER4", "NUMBER5", "NUMBER6")
    await settle()
    check("pair:192.168.0.60:37000:123456" in log and "connect:192.168.0.60:41000" in log
          and "android:open:192.168.0.60:41000" in log and ctl.current_app() == "android",
          f"con el 6.º número vincula, conecta y muestra la pantalla de ese celular: {log[-6:]}")

    # Vincular con QR: el celular lo escanea y anuncia la vinculación con el nombre del QR
    await ctl.run_action("app:videos")
    andr.net_devices, andr.paired_ok = [], False
    await press("ROOT_MENU")
    idx = [it["id"] for it in ctl._menu_items].index("android")
    await press(*["DOWN"] * ((idx - ctl._menu_sel) % len(ctl._menu_items)), "RIGHT")
    await settle()
    qr_row = [it["id"] for it in ctl._menu_items].index("_qr")
    out = await press(*["DOWN"] * qr_row, "SELECT")
    check(ctl._menu_mode == "android_qr" and "ov:image:Android · vincular con código QR" in out
          and andr.qr.startswith("WIFI:T:ADB;S:tele-cec-"), f"«Vincular con QR» muestra el QR: {out[-2:]} {andr.qr}")
    name = andr.qr.split("S:")[1].split(";")[0]
    password = andr.qr.split("P:")[1].split(";")[0]
    andr.pairing_services = [android_mod.Service("adb-otro", "192.168.0.61", 1), android_mod.Service(name, "192.168.0.62", 38000)]
    andr.connect_services = [android_mod.Service("adb-Q", "192.168.0.62", 42000)]
    andr.paired = {"192.168.0.62:38000": password}
    andr.paired_ok = True
    await settle()
    await settle()
    check(f"pair:192.168.0.62:38000:{password}" in log and "android:open:192.168.0.62:42000" in log,
          f"cuando el celular lee el QR: vincula con esa contraseña (no con otro celular), conecta y abre: {log[-5:]}")

    # Ya vinculado (después de reiniciar la Pi): OK sobre Android lo conecta solo y lo abre
    await ctl.run_action("app:videos")
    andr.net_devices = []
    andr.pairing_services = []
    await press("ROOT_MENU")
    idx = [it["id"] for it in ctl._menu_items].index("android")
    start = len(log)
    await press(f"NUMBER{idx + 1}")
    await settle()
    await settle()
    check("connect:192.168.0.62:42000" in log[start:] and "android:open:192.168.0.62:42000" in log[start:],
          f"un celular ya vinculado se conecta y se abre directo: {log[start:]}")

    print("Android por USB: autorizar desde el celular, y dejar de compartir")
    android_ctl.WATCH_EVERY_S = 0.01  # type: ignore[attr-defined]

    async def wait_for(cond, tries: int = 200) -> None:
        for _ in range(tries):
            if cond():
                break
            await asyncio.sleep(0.005)
        await settle()

    await ctl.run_action("app:videos")
    andr.net_devices = [android_mod.Device("ZY327", "unauthorized", "moto_e7_plus")]
    andr.connect_services = []
    await press("ROOT_MENU")
    idx = [it["id"] for it in ctl._menu_items].index("android")
    start = len(log)
    await press(f"NUMBER{idx + 1}")
    await settle()
    ids = [it["id"] for it in ctl.overlay.items]
    check(ctl._menu_mode == "android" and ids[0] == "_bad" and ctl._menu_sel == 0,
          f"enchufado sin autorizar: la lista lo muestra esperando «Permitir»: {ids}")
    out = await press("SELECT")
    check("ov:toast:Aceptá «Permitir depuración USB» en el celular" in out,
          f"OK sobre él todavía sin autorizar: repite el aviso: {out[-2:]}")
    andr.net_devices = [android_mod.Device("ZY327", "device", "moto_e7_plus")]  # se tocó «Permitir»
    await wait_for(lambda: ctl._macro_task is not None or "android:open:ZY327" in log[start:])
    check("android:open:ZY327" in log[start:] and ctl.current_app() == "android" and not ctl._menu_open,
          f"apenas se acepta en el celular, la lista se entera sola y muestra su pantalla: {log[start:][-4:]}")

    ctl._switch_until = 0.0
    out = await press("F3_GREEN")
    items = ctl.overlay.items
    check(ctl._menu_open and ctl._menu_mode == "android" and [it["id"] for it in items[:2]] == ["_resume", "_stop"]
          and ctl._menu_sel == 0 and "moto e7 plus" in items[0]["name"] and andr.on
          and not any(it["id"] == "_dev" for it in items),
          f"verde, compartiendo: «Seguir viendo» y «Dejar de compartir» arriba (el celular no se repite): "
          f"{[(it['id'], it['name']) for it in items]}")
    out = await press("EXIT")
    check(not ctl._menu_open and andr.on and ctl.current_app() == "android",
          f"Atrás (entrando desde la pantalla) vuelve a la pantalla del celular: {out}")
    await press("F3_GREEN")
    out = await press("SELECT")
    check(not ctl._menu_open and andr.on, f"OK sobre «Seguir viendo»: vuelve a la pantalla: {out}")
    await press("ROOT_MENU")
    check(ctl._menu_mode == "activities" and ctl._menu_items[ctl._menu_sel]["id"] == "android",
          "compartiendo, el menú abre parado sobre Android")
    out = await press("SELECT")
    check(ctl._menu_open and ctl._menu_mode == "android" and ctl.overlay.items[0]["id"] == "_resume" and andr.on,
          f"Menú › OK sobre Android (sin el verde): la lista con «Seguir viendo» y «Dejar de compartir»: {out}")
    out = await press("EXIT")
    check(ctl._menu_mode == "activities" and ctl._menu_open and andr.on,
          f"ahí Atrás vuelve al menú (y la pantalla sigue): {out}")
    await press("SELECT")
    start = len(log)
    out = await press("DOWN", "SELECT")
    await settle()
    ids = [it["id"] for it in ctl.overlay.items]
    check("android:close" in log[start:] and not andr.on and ctl._menu_open and ctl._menu_mode == "android"
          and "_stop" not in ids and "_dev" in ids and ctl._left_alone() is None and ctl.current_app() is None,
          f"«Dejar de compartir» cierra scrcpy y queda la lista (sin el aviso de «se desconectó»): {log[start:]} {ids}")
    out = await press("EXIT")
    check(ctl._menu_mode == "activities" and ctl._menu_open, f"ahí Atrás vuelve al menú de actividades: {out}")
    andr.net_devices = [android_mod.Device("ZY327", "device", "moto_e7_plus")]
    await press("EXIT")
    await ctl.run_action("app:android")
    ctl._switch_until = 0.0
    start = len(log)
    await ctl.run_action("android:disconnect")
    await settle()
    check("android:close" in log[start:] and not andr.on and ctl._menu_mode == "android" and ctl._menu_open,
          f"android:disconnect hace lo mismo desde la pantalla: {log[start:]}")
    andr.net_devices = []  # se desenchufó
    await wait_for(lambda: not any(it["id"] == "_dev" for it in ctl.overlay.items))
    check(not any(it["id"] == "_dev" for it in ctl.overlay.items),
          f"desenchufado: la lista se actualiza sola: {[it['id'] for it in ctl.overlay.items]}")
    await press("EXIT")
    await press("EXIT")

    print("Android · la fila «Por USB» dice qué ve la Pi")
    with tempfile.TemporaryDirectory() as sysfs:
        def mk(path: str, **files: str) -> None:
            os.makedirs(os.path.join(sysfs, path), exist_ok=True)
            for k, v in files.items():
                Path(sysfs, path, k).write_text(v + "\n")
        mk("usb1")
        mk("1-1", product="USB2.0 Hub")
        mk("1-1:1.0", bInterfaceClass="09", bInterfaceSubClass="00", bInterfaceProtocol="00")
        mk("1-1.1", manufacturer="motorola", product="moto e(7) plus", serial="ZY327X8THP")
        mk("1-1.1:1.0", bInterfaceClass="ff", bInterfaceSubClass="ff", bInterfaceProtocol="00", interface="MTP")
        mk("1-1.1:1.1", bInterfaceClass="ff", bInterfaceSubClass="42", bInterfaceProtocol="01", interface="ADB Interface")
        mk("1-1.2", manufacturer="Samsung", product="Galaxy", serial="R5CR")
        mk("1-1.2:1.0", bInterfaceClass="06", bInterfaceSubClass="01", bInterfaceProtocol="01")
        mk("1-1.3", product="Raspberry Pi Internal Keyboard")
        mk("1-1.3:1.0", bInterfaceClass="03", bInterfaceSubClass="01", bInterfaceProtocol="01")
        found = android_mod.usb_phones(sysfs)
    check([(p.name, p.serial, p.adb) for p in found]
          == [("motorola moto e(7) plus", "ZY327X8THP", True), ("Samsung Galaxy", "R5CR", False)],
          f"/sys: encuentra los celulares (con y sin depuración USB) y no el hub ni el teclado: {found}")
    await ctl.run_action("app:videos")
    andr.net_devices = []
    await press("ROOT_MENU")
    idx = [it["id"] for it in ctl._menu_items].index("android")
    await press(f"NUMBER{idx + 1}")
    await settle()
    usb_row = next(it for it in ctl.overlay.items if it["id"] == "_usb")
    check("no detecto ningún celular" in usb_row["name"] and "cable de datos" in usb_row["sub"] and usb_row["enabled"],
          f"sin nada enchufado lo dice (y sugiere el cable): {usb_row}")
    ctl._menu_sel = [it["id"] for it in ctl.overlay.items].index("_usb")
    out = await press("SELECT")
    check(any(o.startswith("ov:toast:Por USB: no detecto") for o in out), f"OK sobre «Por USB» vuelve a mirar y avisa: {out}")
    fake_usb.append(android_mod.UsbPhone("motorola moto e(7) plus", "ZY327X8THP", False))
    await wait_for(lambda: "sin depuración" in next(it for it in ctl.overlay.items if it["id"] == "_usb")["name"])
    usb_row = next(it for it in ctl.overlay.items if it["id"] == "_usb")
    check("sin depuración" in usb_row["name"] and "Depuración USB" in usb_row["sub"]
          and ctl.overlay.items[ctl._menu_sel]["id"] == "_usb",
          f"enchufado sin la depuración: lo dice solo, y la selección sigue en esa fila: {usb_row}")
    fake_usb.clear()
    await press("EXIT")
    await press("EXIT")

    del andr.source
    andr.net_devices = []
    ctl.cfg.menu["items"] = ["netflix", "tidal", "videos", "fotos"]
    await ctl.run_action("app:videos")

    andr.ok = False
    andr.ready = True
    out = await press("ROOT_MENU")
    ctl._menu_items.append({"id": "android", "name": "Android", "icon": "", "sub": "", "enabled": True,
                            "current": False})
    out = await press(*["DOWN"] * (len(ctl._menu_items) - 1 - ctl._menu_sel), "SELECT")
    check("ov:toast:Aceptá «Permitir depuración USB» en el celular" in out,
          f"si no puede abrir, el menú dice por qué: {out[-3:]}")
    del ctl.activities["android"]
    await ctl.run_action("app:videos")

    devs = android_mod.parse_devices(
        "List of devices attached\n"
        "R58M1234   device usb:1-1 product:beyond1 model:SM_G973F device:beyond1 transport_id:1\n"
        "192.168.0.50:5555 unauthorized transport_id:2\n"
        "ZY22 no permissions (missing udev rules?); see [http://developer.android.com/tools/device.html] usb:1-2\n")
    check([(d.serial, d.state, d.model, d.wifi) for d in devs]
          == [("R58M1234", "device", "SM_G973F", False), ("192.168.0.50:5555", "unauthorized", "", True),
              ("ZY22", "no permissions", "", False)],
          f"lee `adb devices -l` (estado, modelo, USB o Wi‑Fi): {devs}")
    real = activities_mod.AndroidActivity({})
    real.devices = lambda: devs[:1]  # type: ignore[method-assign]
    check(real.source() == ("SM G973F · USB", True), f"el menú muestra el modelo y cómo está conectado: {real.source()}")
    real.devices = lambda: devs[1:2]  # type: ignore[method-assign]
    check(real.source() == (android_mod.STATE_TEXT["unauthorized"], True) and not real.ready,
          "sin autorizar: dice qué hacer (y OK abre «conectar»)")
    real.devices = lambda: []  # type: ignore[method-assign]
    check(real.source() == (android_mod.NO_DEVICE, True) and not real.ready, "sin celular: OK abre «conectar»")
    wifi = activities_mod.AndroidActivity({"serial": "192.168.0.50"})
    wifi.devices = lambda: []  # type: ignore[method-assign]
    check(wifi._target() == "192.168.0.50:5555" and wifi.source()[1],
          f"por Wi‑Fi (puerto 5555 si no se pone): se puede elegir aunque no esté conectado aún: {wifi.source()}")
    aargs = activities_mod.AndroidActivity({"max_size": 1280, "audio": False, "extra_args": ["--video-codec=h265"]}
                                           ).args("/usr/local/bin/scrcpy", "R58M1234")
    check(aargs[:2] == ["/usr/local/bin/scrcpy", "--serial=R58M1234"] and "--fullscreen" in aargs
          and "--max-size=1280" in aargs and "--max-fps=30" not in aargs and "--no-audio" in aargs
          and "--stay-awake" in aargs and aargs[-1] == "--video-codec=h265",
          f"scrcpy en pantalla completa, con el celular elegido y las opciones de [apps.android]: {aargs}")
    dargs = activities_mod.AndroidActivity(ctl.cfg.apps.get("android")).args("scrcpy", "X")
    check("--max-size=1920" in dargs and "--max-fps=30" in dargs and "--no-audio" not in dargs,
          f"por defecto: 1920 px, 30 cuadros y con sonido: {dargs}")

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

    print("Solo el menú, sin nada abierto: reposo corto")
    mp = power_mod.IdleShutdown(ctl, {"enabled": True})
    ctl.power = mp
    check(mp.menu_idle == 10 * 60, f"por defecto, a los 10 min: {mp.menu_idle / 60:g}")
    check(ctl.nothing_open(), "sin ninguna actividad abierta")
    t0 = mp.last_activity
    mp._note_open(t0)
    check(mp.idle_since == t0 and mp.reason(t0 + 9 * 60) is None and mp.reason(t0 + 11 * 60),
          f"a los 10 min sin teclas: reposo ({mp.reason(t0 + 11 * 60)})")
    mp.last_activity = t0 + 5 * 60
    check(mp.reason(t0 + 11 * 60) is None and mp.reason(t0 + 16 * 60), "una tecla reinicia la cuenta corta")
    mp.last_activity = t0 - 3600  # p. ej. una presentación de fotos sin música que terminó sola
    mp.idle_since = None
    mp._note_open(t0)
    check(mp.reason(t0 + 60) is None, "la cuenta corta arranca cuando se cerró la última app, no antes")
    await ctl.switch_to("fotos", tmp / "pick" / "media" / "gaby" / "USB")
    mp._note_open(t0 + 20 * 60)
    check(not ctl.nothing_open() and mp.idle_since is None and mp.reason(t0 + 20 * 60) is None,
          "con una actividad abierta no cuenta (vale idle_minutes)")
    off = power_mod.IdleShutdown(ctl, {"menu_idle_minutes": 0})
    off.idle_since = off.last_activity
    check(off.reason(off.last_activity + 60 * 60) is None, "menu_idle_minutes = 0: espera idle_minutes")
    await mp.sleep("prueba")
    mp._asleep_at -= 60
    mp.on_frame(CecFrame(initiator=0, destination=1, opcode=0x90, params=[0x00]))
    await mp._wake_task
    check(not mp.tv_ours, "despertada porque se prendió la TV: puede estar en otra entrada")
    sent.clear()
    await mp.sleep("prueba")
    check("standby 0" not in sent, f"si nadie tocó el control, vuelve al reposo sin apagar la TV: {sent}")
    await press("RIGHT")
    await mp._wake_task
    check(mp.tv_ours, "con una tecla, la TV está en la Pi")
    sent.clear()
    await mp.sleep("prueba")
    check(sent == ["standby 0"], f"y entonces sí apaga la TV: {sent}")
    await press("RIGHT")
    await mp._wake_task
    await ctl.menu_close()
    ctl.power = power_mod.IdleShutdown(ctl, {})

    print("Menú con el teclado")
    from tele_cec.keys import NAME_TO_CODE
    from tele_cec.ui.overlay import Overlay
    got: list = []
    kov = Overlay(True, lambda c: got.append(c), lambda: got.append("suelta"))
    check(kov.extra_env == {"TELE_CEC_MENU_KEYBOARD": "1"}, f"le avisa al panel que tome el teclado: {kov.extra_env}")
    check(Overlay().extra_env == {"TELE_CEC_MENU_KEYBOARD": "0"}, "por defecto (sin [menu] keyboard) no")
    for m in ({"key": "UP"}, {"release": True}, {"key": "SELECT"}, {"key": "NUMBER3"}, {"key": "???"}):
        kov.on_message(m)
    check(got == [NAME_TO_CODE["UP"], "suelta", NAME_TO_CODE["SELECT"], NAME_TO_CODE["NUMBER3"]],
          f"las teclas del panel llegan como las del control: {got}")
    try:
        from tele_cec.ui import menu_ui
        from gi.repository import Gdk
    except Exception as exc:  # noqa: BLE001  (sin GTK en esta máquina)
        print(f"  (sin GTK: no pruebo el mapa de teclas: {exc})")
    else:
        kb_names = {k: menu_ui.keyboard_key(Gdk.keyval_from_name(k))
                 for k in ("Up", "Down", "Left", "Right", "Return", "KP_Enter", "Escape", "BackSpace", "5", "KP_7",
                           "F1", "a")}
        check(kb_names == {"Up": "UP", "Down": "DOWN", "Left": "LEFT", "Right": "RIGHT", "Return": "SELECT",
                        "KP_Enter": "SELECT", "Escape": "EXIT", "BackSpace": "EXIT", "5": "NUMBER5", "KP_7": "NUMBER7",
                        "F1": "HELP", "a": None}, f"teclas del teclado → teclas del control: {kb_names}")
        check(all(menu_ui.KEYBOARD_KEYS[k] in NAME_TO_CODE for k in menu_ui.KEYBOARD_KEYS),
              "todas tienen código CEC")
        bindings = ctl.cfg.bindings.get("menu") or {}
        check(all(NAME_TO_CODE[v] in bindings for v in set(menu_ui.KEYBOARD_KEYS.values())),
              "y todas hacen algo en el menú")
    start = len(log)
    await ctl.menu_open()
    ctl.overlay.on_key = ctl.on_key  # type: ignore[attr-defined]
    ctl.on_key(NAME_TO_CODE["DOWN"])
    code, ts = await ctl.queue.get()
    await ctl.handle_key(code, ts)
    check("ov:select:1" in log[start:], f"▼ del teclado mueve la selección del menú: {log[start:]}")
    await ctl.menu_close()

    print("Joystick")
    from tele_cec.system import gamepad as gp
    devices = """I: Bus=0003 Vendor=045e Product=028e Version=0114
N: Name="Microsoft X-Box 360 pad"
H: Handlers=event7 js0
B: EV=20000b
B: KEY=7cdb000000000000 0 0 0 0

I: Bus=0003 Vendor=04d9 Product=0006 Version=0111
N: Name="Teclado"
H: Handlers=sysrq kbd event3 leds
B: KEY=1000000000007 ff9f207ac14057ff febeffdfffefffff fffffffffffffffe

I: Bus=0005 Vendor=2dc8 Product=6101 Version=0001
N: Name="8BitDo SN30 Pro"
H: Handlers=event9
B: KEY=7fff000000000000 0 0 0 0
"""
    found = gp.find_joysticks(devices)
    check(found == {"/dev/input/event7": "Microsoft X-Box 360 pad", "/dev/input/event9": "8BitDo SN30 Pro"},
          f"detecta los joysticks (y no el teclado): {found}")
    keys: list = []
    pads = gp.Gamepads({"repeat_delay_ms": 30, "repeat_ms": 20, "buttons": {"X": "", "Y": "F2_RED"}},
                       lambda c, b="": keys.append(key_name(c)), lambda: keys.append("suelta"))
    pad = gp.Pad("/dev/input/event7", "Xbox", -1)
    for ax in (gp.ABS_X, gp.ABS_Y, gp.ABS_RX, gp.ABS_RY):
        pad.axes[ax] = gp.Axis(-32768, 32767)
    pad.axes[gp.ABS_Z] = pad.axes[gp.ABS_RZ] = gp.Axis(0, 255)
    pad.axes[gp.ABS_HAT0X] = pad.axes[gp.ABS_HAT0Y] = gp.Axis(-1, 1)
    pad.triggers_on_z = True
    ev = lambda t, c, v: pads.handle(pad, t, c, v)  # noqa: E731
    ev(gp.EV_KEY, 0x130, 1); ev(gp.EV_KEY, 0x130, 0)
    ev(gp.EV_KEY, 0x133, 1); ev(gp.EV_KEY, 0x133, 0)
    ev(gp.EV_KEY, 0x134, 1); ev(gp.EV_KEY, 0x134, 0)
    ev(gp.EV_KEY, 0x13C, 1); ev(gp.EV_KEY, 0x13C, 0)
    check(keys == ["SELECT", "suelta", "F2_RED", "suelta", "CONTENTS_MENU", "suelta"],
          f"A = OK, X anulado, Y cambiado a rojo, el botón del medio = Menú: {keys}")
    keys.clear()
    ev(gp.EV_ABS, gp.ABS_HAT0Y, 1); ev(gp.EV_ABS, gp.ABS_HAT0Y, 0)
    check(keys == ["DOWN", "suelta"], f"la cruz: {keys}")
    keys.clear()
    ev(gp.EV_ABS, gp.ABS_X, 30000)
    await asyncio.sleep(0.1)
    ev(gp.EV_ABS, gp.ABS_X, 15000)   # todavía lejos del centro: sigue apretada
    n = len(keys)
    ev(gp.EV_ABS, gp.ABS_X, 1000)
    await asyncio.sleep(0.05)
    check(keys[0] == "RIGHT" and n >= 3 and all(k == "RIGHT" for k in keys[:n]) and keys[n:] == ["suelta"],
          f"la palanca: mantenerla repite, y se suelta cerca del centro: {keys}")
    keys.clear()
    ev(gp.EV_ABS, gp.ABS_Y, -30000); ev(gp.EV_ABS, gp.ABS_X, 30000); ev(gp.EV_ABS, gp.ABS_Y, 0); ev(gp.EV_ABS, gp.ABS_X, 0)
    check(keys == ["UP", "suelta", "RIGHT", "suelta"], f"de arriba a la derecha: {keys}")
    keys.clear()
    ev(gp.EV_ABS, gp.ABS_RZ, 250); ev(gp.EV_ABS, gp.ABS_RZ, 120); ev(gp.EV_ABS, gp.ABS_RZ, 10)
    ev(gp.EV_ABS, gp.ABS_RY, -32000); ev(gp.EV_ABS, gp.ABS_RY, 0)
    check(keys == ["FAST_FORWARD", "suelta", "VOLUME_UP", "suelta"], f"gatillo derecho y palanca derecha: {keys}")
    pads._stop_repeat()
    keys.clear()
    held_btns: list = []
    pads.on_key = lambda c, b="": held_btns.append(b)  # type: ignore[assignment]
    pads.hold_time = 0.05
    ev(gp.EV_KEY, 0x13A, 1); ev(gp.EV_KEY, 0x13A, 0)
    await asyncio.sleep(0.1)
    ev(gp.EV_KEY, 0x13A, 1)
    await asyncio.sleep(0.1)
    ev(gp.EV_KEY, 0x13A, 0)
    check(held_btns == ["BACK", "BACK", "BACK:HOLD"], f"Back mantenido avisa «BACK:HOLD» (un toque corto no): {held_btns}")
    check(gp.Axis(0, 255).norm(128) < 0.01 and gp.Axis(0, 255).norm(255) == 1.0, "palancas de 0 a 255 (otros joysticks)")

    from tele_cec.ui import help as help_mod
    jrows = dict(help_mod.rows(ctl.cfg, "joystick"))
    check(jrows.get("A") == "como OK del control" and jrows.get("Y") == "como Amarillo del control"
          and "▲ ▼ ◀ ▶" in jrows.get("Cruz y palanca izq.", ""), f"página de ayuda del joystick: {jrows}")

    got.clear()
    real_on_key = ctl.on_key
    ctl.on_key = lambda c: got.append(key_name(c))  # type: ignore[method-assign]
    ctl._gamepad_menu_only = {"fotos"}
    await ctl.switch_to("fotos", tmp / "pick" / "media" / "gaby" / "USB")
    for code, btn in (("RIGHT", "DPAD_RIGHT"), ("CONTENTS_MENU", "START"), ("F1_BLUE", "BACK"),
                      ("F1_BLUE", "BACK+START"), ("F1_BLUE", "BACK:HOLD")):
        ctl.on_gamepad_key(NAME_TO_CODE[code], btn)
    check(got == [], f"en un programa que usa el joystick, sus botones son suyos (Start, Back…): {got}")
    ctl.on_gamepad_key(NAME_TO_CODE["CONTENTS_MENU"], "GUIDE")
    check(len(got) == 1 and got[0] in ("ROOT_MENU", "CONTENTS_MENU", "SETUP_MENU"),
          f"el botón del medio abre nuestro menú: {got}")
    got.clear()
    ctl._gamepad_exit_buttons = {"BACK:HOLD"}
    ctl.on_gamepad_key(NAME_TO_CODE["F1_BLUE"], "BACK:HOLD")
    check(len(got) == 1, f"con exit_buttons = [\"BACK:HOLD\"], mantener Back abre el menú (fuera de Juegos): {got}")
    ctl._gamepad_exit_buttons = set()
    got.clear()
    ctl._gamepad_menu_only = set()
    ctl.on_gamepad_key(NAME_TO_CODE["F1_BLUE"], "BACK+START")
    ctl.on_gamepad_key(NAME_TO_CODE["F1_BLUE"], "BACK:HOLD")
    check(got == [], f"fuera de los juegos, Back+Start y Back mantenido no hacen nada extra: {got}")
    ctl._gamepad_menu_only = {"app"}
    got.clear()
    ctl.on_gamepad_key(NAME_TO_CODE["RIGHT"])
    check(got == ["RIGHT"], f"en las demás, todo: {got}")
    ctl._gamepad_menu_only = {"juegos", "app"}
    ctl.on_key = real_on_key  # type: ignore[method-assign]
    await ctl.activities["fotos"].close()
    ctl._foreground = None

    print("Juegos (MAME)")
    from tele_cec.apps import mame as mame_mod
    check(mame_mod.split_title("Bubble Bobble (Japan, Ver 0.1)") == ("Bubble Bobble", "Japan, Ver 0.1")
          and mame_mod.split_title("Bubble 2000") == ("Bubble 2000", ""), "separa el nombre de la versión")
    mroot = tmp / "mame"
    (mroot / "roms").mkdir(parents=True)
    (mroot / "roms2").mkdir()
    for f in ("bublbobl.zip", "wboy.zip", "neogeo.zip", "drivedroid.img", "drivedroid.nfo"):
        (mroot / "roms" / f).write_bytes(b"x")
    (mroot / "roms2" / "suprmrio.7z").write_bytes(b"x")
    (mroot / "mame.ini").write_text(f"window 0\nrompath   {mroot}/roms;{mroot}/roms2;/no/existe\n")
    check(mame_mod.mame_ini_rompath(mroot / "mame.ini") == [f"{mroot}/roms", f"{mroot}/roms2", "/no/existe"],
          "lee el rompath de mame.ini")
    calls = mroot / "calls.txt"
    fake_mame = mroot / "mame"
    fake_mame.write_text(f"""#!/bin/sh
echo "$@" >> {calls}
[ "$1" = "-listxml" ] || exit 0
shift
echo '<?xml version="1.0"?><mame build="0.276">'
for r in "$@"; do case $r in
  bublbobl) echo '<machine name="bublbobl"><description>Bubble Bobble (Japan, Ver 0.1)</description><year>1986</year><manufacturer>Taito</manufacturer></machine>';;
  wboy) echo '<machine name="wboy" sourcefile="x"><description>Wonder Boy (set 1, 315-5177)</description><year>1986</year><manufacturer>Escape (Sega license)</manufacturer></machine>';;
  suprmrio) echo '<machine name="suprmrio"><description>Vs. Super Mario Bros. (set SM4-4 E)</description><year>1986</year><manufacturer>Nintendo</manufacturer></machine>';;
  neogeo) echo '<machine name="neogeo" isbios="yes"><description>Neo-Geo MV-6F</description></machine>';;
esac; done
echo '</mame>'
""")
    fake_mame.chmod(0o755)
    real_mame = mame_mod.MameActivity({"binary": str(fake_mame), "rompath": [str(mroot / "roms"), str(mroot / "roms2")]},
                                      cache_dir=mroot / "cache")
    games = real_mame.games()
    check([g.title for g in games] == ["Bubble Bobble", "Vs. Super Mario Bros.", "Wonder Boy"],
          f"lista los juegos (sin el BIOS ni lo que no es de MAME), por nombre: {[g.rom for g in games]}")
    check(games[0].sub == "1986 · Taito · Japan, Ver 0.1", f"con año, fabricante y versión: {games[0].sub}")
    check(real_mame.source() == ("3 juegos", True), f"en el menú: {real_mame.source()}")
    n_calls = len(calls.read_text().splitlines())
    again = mame_mod.MameActivity({"binary": str(fake_mame), "rompath": [str(mroot / "roms"), str(mroot / "roms2")]},
                                  cache_dir=mroot / "cache")
    check([g.rom for g in again.games()] == [g.rom for g in games] and len(calls.read_text().splitlines()) == n_calls,
          "la segunda vez sale del caché, sin preguntarle a MAME")
    (mroot / "roms" / "wboy.zip").unlink()
    check([g.rom for g in again.games()] == ["bublbobl", "suprmrio"], "si se borra una ROM, deja de aparecer")
    margs = real_mame.args("bublbobl")
    check(margs == [str(fake_mame), "-rompath", f"{mroot}/roms;{mroot}/roms2", "-ctrlrpath", f"{mroot}/cache/mame-ctrlr",
                    "-ctrlr", "tele-cec", "-autoboot_script", f"{mroot}/cache/mame-tele-cec.lua", "-autoboot_delay", "0",
                    "bublbobl"], f"cómo abre MAME (con su archivo de controles y el script de órdenes): {margs}")
    real_mame.write_lua()
    lua = (mroot / "cache" / "mame-tele-cec.lua").read_text()
    check("emu.pause()" in lua and "manager.machine:exit()" in lua and str(real_mame.cmd_file) in lua,
          "el script Lua pausa, sigue y sale con la API de MAME")
    real_mame.write_ctrlr()
    ctrlr = (mroot / "cache" / "mame-ctrlr" / "tele-cec.cfg").read_text()
    check('<port type="UI_MENU">' in ctrlr and "KEYCODE_TAB" in ctrlr and "JOYCODE" not in ctrlr,
          "el menú de MAME queda solo en Tab (el botón del medio es del nuestro)")
    check("-ctrlr" not in mame_mod.MameActivity({"guide_menu": False}).args("x"), "guide_menu = false: sin eso")
    empty = mame_mod.MameActivity({"binary": str(fake_mame), "rompath": [str(mroot / "vacia")]}, cache_dir=mroot / "c2")
    check(empty.source()[1] is False, f"sin ROMs no se puede elegir: {empty.source()}")

    class FakeMame(FakeProcMixin, mame_mod.MameActivity):
        def __init__(self, log):
            mame_mod.MameActivity.__init__(self, {"binary": str(fake_mame), "rompath": [str(mroot / "roms"),
                                                                                         str(mroot / "roms2")]},
                                           cache_dir=mroot / "cache")
            self.setup(log)

        async def open(self, rom=None) -> bool:
            self.log.append(f"juegos:open:{rom}")
            self.on, self.current_game, self.frozen = True, rom or self.current_game, False
            return True

        async def key(self, name) -> bool:
            self.log.append(f"mame:key:{name}")
            return True

        async def command(self, what, timeout=1.5) -> bool:
            self.log.append(f"mame:cmd:{what}")
            return self.on

        async def close(self) -> None:
            self.frozen = False
            await FakeProcMixin.close(self)
    mnames = lambda: [it["name"] for it in ctl.overlay.items]  # noqa: E731
    jm = FakeMame(log)
    ctl.activities["juegos"] = jm
    menu_items_before = ctl.cfg.menu.get("items")
    ctl.cfg.menu["items"] = ["netflix", "tidal", "videos", "fotos", "juegos"]
    await ctl.menu_open()
    check("Juegos" in mnames(), f"«Juegos» en el menú: {mnames()}")
    jm.current_game, start = "bublbobl", len(log)
    await ctl.run_action("app:juegos")
    check(ctl._menu_mode == "games" and not any(x.startswith("juegos:open") for x in log[start:]),
          f"app:juegos sin elegir juego: la lista, no el último que se jugó: {log[start:]}")
    jm.current_game = ""
    await ctl.menu_open()
    await ctl.menu_open(mnames().index("Juegos"))
    await press("SELECT")
    check(ctl._menu_mode == "games" and mnames() == ["Bubble Bobble", "Vs. Super Mario Bros."],
          f"OK sobre Juegos muestra la lista: {mnames()}")
    out = await press("DOWN", "SELECT")
    check("juegos:open:suprmrio" in out and ctl.current_app() == "juegos" and not ctl._menu_open,
          f"OK abre ese juego: {[x for x in out if not x.startswith('ov:')]}")
    check(await ctl.context() == "juegos", "contexto «juegos»")
    start = len(log)
    out = await press("EXIT")
    check(ctl._menu_mode == "games" and mnames()[:4] == ["Seguir jugando", "Salir del juego", "Opciones de MAME",
                                                         "Otras actividades"] and ctl.overlay.sel == 0,
          f"Atrás (jugando) muestra la lista encima del juego: {mnames()} {ctl.overlay.sel}")
    check(out.index("mame:cmd:pause") < out.index("ov:show:0") and jm.frozen,
          f"y pausa el juego antes de mostrarla: {out}")
    out = await press("EXIT")
    check(not ctl._menu_open and jm.on and not jm.frozen and "mame:cmd:resume" in out, f"Atrás otra vez: sigue jugando, sin pausa: {out}")
    while not ctl.queue.empty():
        ctl.queue.get_nowait()
    ctl.on_gamepad_key(NAME_TO_CODE["CONTENTS_MENU"], "START")
    ctl.on_gamepad_key(NAME_TO_CODE["F1_BLUE"], "BACK")
    check(ctl.queue.empty(), "Start y Back del joystick son del juego")
    ctl.on_gamepad_key(NAME_TO_CODE["CONTENTS_MENU"], "GUIDE")
    code, ts = await ctl.queue.get()
    await ctl.handle_key(code, ts)
    check(ctl._menu_open and ctl._menu_mode == "games" and jm.frozen,
          f"el botón del medio del joystick muestra nuestra lista (con el juego en pausa): {mnames()}")
    out = await press("SELECT")
    check(not ctl._menu_open and not jm.frozen, "«Seguir jugando» la cierra y saca la pausa")
    await press("EXIT")
    out = await press("DOWN", "DOWN", "SELECT")
    check(not ctl._menu_open and out[-2:] == ["mame:cmd:resume", "mame:key:Tab"] and not jm.frozen,
          f"«Opciones de MAME»: saca la pausa y abre el menú de MAME (Tab): {out}")
    await press("EXIT")
    await ctl.menu_open(0)  # ▼▼▼ seguidas las descarta el filtro de repeticiones: se elige directo
    await ctl.menu_close()
    await press("EXIT")
    ctl._menu_sel = 3
    await press("SELECT")
    check(ctl._menu_open and ctl._menu_mode == "activities" and jm.frozen and jm.on,
          "«Otras actividades»: el menú principal, con el juego todavía en pausa")
    await ctl.menu_close()
    check(not jm.frozen, "al cerrar el menú, el juego sigue")
    await press("EXIT")
    ctl._menu_sel = mnames().index("Bubble Bobble")
    out = await press("SELECT")
    check("juegos:open:bublbobl" in out and not jm.frozen,
          f"elegir otro juego lo cambia: {[x for x in out if not x.startswith('ov:')]}")
    await press("EXIT")
    ctl._menu_sel = 1
    await press("SELECT")  # «Salir del juego»
    check(not jm.on and ctl._menu_open and ctl._menu_mode == "activities", "«Salir del juego» cierra MAME y vuelve al menú")
    ctl._gamepad_exit_buttons = {"BACK:HOLD"}
    await ctl.menu_close()
    await ctl.switch_to("juegos", "wboy")
    ctl.on_gamepad_key(NAME_TO_CODE["F1_BLUE"], "BACK:HOLD")
    await ctl._macro_task
    check(not jm.on and ctl._menu_open and ctl._menu_mode == "games",
          f"con exit_buttons = [\"BACK:HOLD\"], mantener Back sale del juego y muestra la lista: {mnames()}")
    ctl._gamepad_exit_buttons = set()
    await ctl.menu_close()
    await ctl.switch_to("juegos", "bublbobl")
    jm.on = False   # el jugador salió con Esc en MAME
    ctl._switch_until = 0.0
    await ctl._app_gone("juegos")
    check(ctl._menu_open and ctl._menu_mode == "games" and "Bubble Bobble" in mnames(),
          f"si MAME se cierra solo, aparece la lista de juegos: {mnames()}")
    await press("EXIT")
    check(ctl._menu_open and ctl._menu_mode == "activities", "y Atrás vuelve al menú (no había juego abierto)")
    await ctl.menu_close()
    del ctl.activities["juegos"]
    if menu_items_before is None:
        ctl.cfg.menu.pop("items", None)
    else:
        ctl.cfg.menu["items"] = menu_items_before

    print("Menú en dos columnas")
    ctl._menu_columns = 2
    menu_items_before = ctl.cfg.menu.get("items")
    ctl.cfg.menu["items"] = ["netflix", "tidal", "videos", "fotos", "juegos"]
    jm.on = False
    ctl.activities["juegos"] = jm
    await ctl.menu_open(0)
    grid = mnames()
    check(ctl.overlay.columns == 2 and grid == ["Netflix", "Tidal", "Videos", "Fotos", "Juegos", "Ayuda"],
          f"el menú va en dos columnas: {grid} ({ctl.overlay.columns})")
    # 6 opciones → 3 por columna: Netflix Tidal Videos | Fotos Juegos Ayuda
    moves = []
    for k in ("DOWN", "RIGHT", "UP", "LEFT", "UP", "RIGHT"):
        await ctl.run_action(f"menu:{k.lower()}")
        moves.append(grid[ctl._menu_sel])
    check(moves == ["Tidal", "Juegos", "Fotos", "Netflix", "Videos", "Videos"] and ctl._menu_chip,
          f"▲ ▼ dentro de la columna (dando la vuelta), ◀ ▶ a la otra (en Videos, ▶ va a su botoncito): {moves}")
    await ctl.run_action("menu:right")
    check(grid[ctl._menu_sel] == "Ayuda", "▶ desde el botoncito: la otra columna")
    await ctl.run_action("menu:right")
    check(grid[ctl._menu_sel] == "Ayuda", "▶ en la última columna no hace nada")
    check([it.get("action") for it in ctl.overlay.items] == [None, None, "Carpetas", "Carpetas", None, None],
          f"Videos y Fotos muestran el botoncito «Carpetas»: {[it.get('action') for it in ctl.overlay.items]}")
    await ctl.menu_open(grid.index("Videos"))
    out = await press("RIGHT")
    check(out == ["ov:select:2:chip"] and ctl._menu_chip, f"▶ sobre Videos enfoca su botoncito: {out}")
    out = await press("LEFT")
    check(out == ["ov:select:2"] and not ctl._menu_chip, f"◀ vuelve a la fila: {out}")
    await ctl.run_action("menu:right")
    await ctl.run_action("menu:right")
    check(grid[ctl._menu_sel] == "Ayuda" and not ctl._menu_chip, "▶ otra vez pasa a la otra columna")
    await ctl.run_action("menu:left")
    check(grid[ctl._menu_sel] == "Videos" and not ctl._menu_chip, "◀ desde la otra columna vuelve a la fila (no al botoncito)")
    await ctl.run_action("menu:right")
    await press("SELECT")
    check(ctl._menu_mode == "folders" and all(it.get("kind") == "dir" for it in ctl.overlay.items)
          and ctl.overlay.items[0]["sub"] == "predeterminada",
          f"OK sobre el botoncito: la lista de carpetas (sin repetir la de siempre): {mnames()}")
    await press("EXIT")
    check(ctl._menu_mode == "activities" and grid[ctl._menu_sel] == "Videos" and not ctl._menu_chip,
          "Atrás desde la lista vuelve a la misma casilla")
    await press("F4_YELLOW")
    check(ctl._menu_mode == "folders", "el amarillo también abre las carpetas de la fila")
    await press("EXIT")
    start = len(log)
    await press("SELECT")
    check("videos:open" in log[start:] and ctl.current_app() == "videos" and not ctl._menu_open,
          f"OK sobre la fila abre de una: {[x for x in log[start:] if not x.startswith('ov:')]}")
    await ctl.menu_open()
    check(grid[ctl._menu_sel] == "Videos", "con Videos abierto, el menú lo marca")
    out = await press("SELECT")
    check(not ctl._menu_open and not opened(out), "OK sobre la que está en uso: solo cierra el menú")
    await ctl.menu_close()
    await ctl.activities["videos"].close()
    ctl._foreground = None
    del ctl.activities["juegos"]
    if menu_items_before is None:
        ctl.cfg.menu.pop("items", None)
    else:
        ctl.cfg.menu["items"] = menu_items_before
    ctl._menu_columns = 1

    print("Menú: el grupo «Videos y música»")
    musica = FakeMusicLocal(log)
    ctl.activities["musica"] = musica
    items_before = ctl.cfg.menu.get("items")
    ctl.cfg.menu["items"] = ["netflix", "tidal", "local", "fotos"]
    ctl.cfg.menu["groups"] = {"local": {"name": "Videos y música", "icon": "local.svg", "items": ["videos", "musica"]}}
    try:
        await ctl.menu_close()
        await press("ROOT_MENU")
        names = [it["name"] for it in ctl.overlay.items]
        grp = next(it for it in ctl.overlay.items if it.get("group") == "local")
        check(names[:4] == ["Netflix", "Tidal", "Videos y música", "Fotos"] and "Videos" not in names
              and "Música" not in names and grp["sub"] == "Videos · Música",
              f"una sola fila «Videos y música» (Videos y Música van adentro): {names} {grp['sub']}")
        out = await press("NUMBER3")
        check([it["name"] for it in ctl.overlay.items] == ["Videos", "Música"] and ctl._menu_group == "local"
              and ctl._menu_open, f"OK la abre: su lista, con Videos y Música: {[it['name'] for it in ctl.overlay.items]}")
        out = await press("EXIT")
        check(ctl._menu_open and ctl._menu_group is None and ctl.overlay.items[ctl.overlay.sel].get("group") == "local",
              f"Atrás vuelve al menú, con «Videos y música» elegida: {ctl.overlay.sel}")
        await press("RIGHT")
        out = await press("DOWN", "SELECT")
        check("musica:open" in out and ctl.current_app() == "musica" and not ctl._menu_open,
              f"▶ también la abre; elegir Música la abre: {[x for x in out if not x.startswith('ov:')]}")
        check(await ctl.context() == "musica", "contexto «musica»")
        out = await press("SELECT", "RIGHT", "FORWARD", "F4_YELLOW")
        check(out == ["mpv:ok", "mpv:seek+10", "mpv:next", "mpv:shuffle", "ov:toast:Mezclado"],
              f"en Música: OK, ▶ (buscar), ⏭ (tema siguiente), amarillo (mezclar): {out}")
        await press("ROOT_MENU")
        grp = next(it for it in ctl.overlay.items if it.get("group") == "local")
        check(grp["current"] and ctl.overlay.items[ctl.overlay.sel] is grp,
              f"con Música abierta, el menú abre con «Videos y música» elegida y «en uso»: {grp}")
        fotos.content, fotos.on = True, False
        out = await press("NUMBER4")
        check("fotos:open" in out and "musica:close" not in out and musica.on,
              f"abrir Fotos con la música local sonando no la corta: {[x for x in out if not x.startswith('ov:')]}")
        await press("ROOT_MENU")
        grp = next(it for it in ctl.overlay.items if it.get("group") == "local")
        check(grp.get("badge") == "sonando", f"y en el menú, «Videos y música» dice «sonando»: {grp}")
        await press("EXIT")
        out = await press("PAUSE_PLAY_FUNCTION")
        check(out == ["mpv:ok"], f"en Fotos, ⏯ es de la música local: {out}")
        log.clear()
        await fotos._set_video(True)
        await fotos._set_video(False)
        check(log == ["mpv:pause=True", "mpv:pause=False"], f"un video de Fotos la pausa y al terminar sigue: {log}")
        await fotos.close()
    finally:
        ctl.cfg.menu["items"] = items_before
        ctl.cfg.menu.pop("groups", None)
        await musica.close()
        del ctl.activities["musica"]

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

    print("QR del celular en el menú")
    qr = {"path": "/tmp/q.svg", "url": "192.168.0.111:8765", "title": "Manejá la TV desde el celular", "text": "…"}
    ctl.phone_qr = lambda: qr
    await ctl.menu_open()
    check(ctl.overlay.qr == qr, f"el menú de actividades muestra el QR de la página del celular: {ctl.overlay.qr}")
    await ctl.menu_close()

    def broken_qr():
        raise OSError("sin qrencode")
    ctl.phone_qr = broken_qr
    await ctl.menu_open()
    check(ctl._menu_open and ctl.overlay.qr is None, "si el QR falla, el menú igual se abre (sin QR)")
    await ctl.menu_close()
    ctl.phone_qr = None

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
    print("Fotos y videos (mpv)")
    from tele_cec.apps import photos as photos_mod
    mix = tmp / "mix" / "2020" / "11"
    mix.mkdir(parents=True)
    for n in ("IMG_20201130_185306.jpg", "VID_20201108_154412.3gp", "IMG_20201101_090000.jpg",
              "VID-20201120-WA0001.mp4", "sin_fecha.png"):
        (mix / n).write_bytes(b"")
    pm = activities_mod.PhotosActivity({"folders": [str(tmp / "mix")], "delay_s": 6})
    pm.list_file = lambda folder: tmp / "cache" / "mix.lst"  # type: ignore[method-assign]
    pm.write_list(tmp / "mix")
    names = lambda files: [Path(f).name for f in files]  # noqa: E731
    mixed = pm.mode_files(tmp / "mix", "mixed")
    check(names(mixed) == ["IMG_20201101_090000.jpg", "VID_20201108_154412.3gp", "VID-20201120-WA0001.mp4",
                           "IMG_20201130_185306.jpg", "sin_fecha.png"],
          f"fotos y videos: por la fecha del nombre (cada video en su lugar): {names(mixed)}")
    check(names(pm.mode_files(tmp / "mix", "videos")) == ["VID_20201108_154412.3gp", "VID-20201120-WA0001.mp4"]
          and len(pm.mode_files(tmp / "mix", "photos")) == 3 and pm.counts(tmp / "mix") == (3, 2),
          f"solo videos / solo fotos / cuántos hay: {pm.counts(tmp / 'mix')}")
    import threading as _th
    for i in range(300):  # una carpeta más grande: que escribir la lista tarde un poco
        (mix / f"IMG_2020110{i % 9}_{i:06d}.jpg").write_bytes(b"")
    workers = [_th.Thread(target=pm.write_list, args=(tmp / "mix",)) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    check(pm.counts(tmp / "mix") == (303, 2) and not list((tmp / "cache").glob("*.tmp")),
          f"cuatro actualizaciones a la vez: la lista queda entera y sin temporales: {pm.counts(tmp / 'mix')}")
    pm._refresh_task = None
    pm._refresh_later(tmp / "mix")
    check(pm._refresh_task is None, "recién actualizada: no la vuelve a recorrer (cambiar de modo con ▼ no la repite)")
    vids = pm.mode_files(tmp / "mix", "videos")
    check(pm._start_at(vids, str(mix / "IMG_20201110_120000.jpg")) == 1 and pm._start_at(mixed, mixed[3]) == 3,
          "al cambiar de modo sigue desde ahí (el mismo archivo o el siguiente por fecha)")
    margs = pm.mpv_args(tmp / "cache" / "p.lst", 3)
    check(margs[0] == "mpv" and "--fs" in margs and "--image-display-duration=6" in margs
          and "--playlist-start=3" in margs and any(a.startswith("--input-ipc-server=") for a in margs),
          f"mpv: pantalla completa, el mismo tiempo por foto, desde ahí y con canal de control: {margs}")
    pm.shuffle = True
    sargs = pm.mpv_args(tmp / "cache" / "p.lst", 3)
    check("--shuffle" in sargs and not any(a.startswith("--playlist-start") for a in sargs),
          f"al azar: sin «desde dónde» (si no, empieza siempre por el mismo): {sargs[-3:]}")
    pm.shuffle = False

    class FakeMpv:
        def __init__(self):
            self.sent, self.props = [], {"playlist-count": 5, "playlist-pos-1": 2, "path": mixed[1]}

        async def command(self, *a, timeout=3.0):
            self.sent.append(" ".join(map(str, a)))
            return True, None

        async def get(self, prop):
            return self.props.get(prop)

        async def set(self, prop, value):
            self.sent.append(f"{prop}={value}")
            self.props[prop] = value
            return True

        def stop_watch(self):
            pass
    fm = FakeMpv()
    pm.mpv, pm.mode = fm, "mixed"  # type: ignore[assignment]
    events = []

    async def on_video(now):
        events.append(now)
    pm.on_video = on_video
    await pm.next()
    await pm.zoom_in()
    await pm.rotate()
    check(fm.sent == ["playlist-next force", "pause=True", "video-zoom=0.32", "video-rotate=90"] and pm.paused,
          f"con mpv: siguiente, zoom (pausa la presentación) y girar van por su canal: {fm.sent}")
    fm.sent.clear()
    await pm._mpv_file(mixed[1])
    check({"video-zoom=0", "video-rotate=0", "pause=False"} <= set(fm.sent) and events == [True] and not pm.zoom,
          f"pasa a un video: entero, derecho, sigue la presentación y avisa que suena un video: {fm.sent} {events}")
    await pm._mpv_file(mixed[3])
    check(events == [True, False], f"y al volver a una foto, avisa que terminó: {events}")
    info = await pm.photo_info()
    check(info == {"path": mixed[1], "index": 2, "total": 5}, f"qué foto es, preguntado a mpv: {info}")

    args = activities_mod.VideosActivity({}).default_args(usb, tmp / "v.lst")
    check(args[0] == "mpv" and "--fs" in args and "--load-scripts=no" in args and args[-1] == f"--playlist={tmp / 'v.lst'}"
          and any(a.endswith("tele_cec_osd.lua") for a in args) and any(a.endswith("thumbfast.lua") for a in args)
          and any(a.startswith("--input-ipc-server=") and a.endswith("videos-mpv.sock") for a in args),
          f"Videos con mpv: pantalla completa, sin la configuración personal, con la barra, miniaturas y canal: {args}")
    margs = activities_mod.MusicActivity({}).default_args(usb, tmp / "m.lst")
    check(not any(a.endswith("thumbfast.lua") for a in margs) and "--cover-art-auto=fuzzy" in margs
          and any("tele_cec_osd-audio=yes" in a for a in margs) and any(a.endswith("musica-mpv.sock") for a in margs),
          f"Música: la tapa del disco, la barra siempre a la vista, sin miniaturas y con su propio canal: {margs}")
    mdir = tmp / "musica" / "Disco"
    mdir.mkdir(parents=True)
    for n in ("10 - Diez.mp3", "2 - Dos.flac", "1 - Uno.ogg", "cover.jpg", "notas.txt"):
        (mdir / n).write_bytes(b"")
    check([Path(f).name for f in activities_mod.MusicActivity({}).files(tmp / "musica")]
          == ["1 - Uno.ogg", "2 - Dos.flac", "10 - Diez.mp3"], "Música: solo los temas, en orden natural (2 antes que 10)")

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

    print("Pasar el sonido a la TV desconecta el Bluetooth")
    real = (audio_mod2.list_outputs, audio_mod2.run_cmd)
    cmds: list[str] = []
    speaker_on = [True]

    async def fake_run(cmd, timeout=5.0):
        cmds.append(" ".join(cmd))
        if cmd[:2] == ["bluetoothctl", "disconnect"]:
            speaker_on[0] = False
        return 0, ""
    tv = audio_mod2.Output("TV (HDMI)", "hdmi", 55)
    try:
        audio_mod2.run_cmd = fake_run
        audio_mod2.list_outputs = lambda: [tv, audio_mod2.Output("KN320", "bluetooth", 77 if speaker_on[0] else None,
                                                                 mac="8B:D9:43:D1:A1:AE")]
        r = await audio_mod2.select(tv)
        check(r[0] and cmds == ["wpctl set-default 55", "bluetoothctl disconnect 8B:D9:43:D1:A1:AE"],
              f"elegir la TV: el sonido pasa a la TV y el parlante se desconecta: {cmds}")
        cmds.clear()
        r = await audio_mod2.select(tv)
        check(r[0] and cmds == ["wpctl set-default 55"], f"con el parlante ya desconectado, no hace nada más: {cmds}")
        cmds.clear()
        speaker_on[0] = True
        r = await audio_mod2.select(audio_mod2.Output("KN320", "bluetooth", 77, mac="8B:D9:43:D1:A1:AE"))
        check(r[0] and cmds == ["wpctl set-default 77"], f"elegir el parlante no desconecta nada: {cmds}")
    finally:
        audio_mod2.list_outputs, audio_mod2.run_cmd = real

    bad = [t for ok, t in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} verificaciones OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
