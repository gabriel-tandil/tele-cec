#!/usr/bin/env python3
"""Prueba del menú de actividades (Netflix, Tidal, Videos, Fotos) con navegador, High Tide, VLC, feh y
panel simulados.

No abre nada: verifica qué hace cada tecla según qué actividad está abierta y si el menú está abierto.

    python3 tests/apps_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
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

    async def details(self) -> dict:
        return {"status": "Paused", "title": "Volver a Volver", "artist": "Gabo Ferro", "album": "Mancha de Rolando",
                "position": "1:02", "length": "3:40"}

    back_reply = "ok"  # «no»: High Tide en su primera pantalla, sin a dónde volver

    async def command(self, line: str, timeout: float = 10.0, wait: float = 0.0) -> str:
        self.log.append(f"music:cmd:{line}")
        return self.back_reply if line == "back" else "ok"

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
        self.busy_on = False  # como el panel de verdad: con «Abriendo…» a la vista, los avisos no se ven

    async def show(self, items, selected, title="Actividades", hint="", numbers=True, columns=1, chip=False,
                   qr=None) -> bool:
        self.items, self.sel, self.visible, self.columns, self.qr = items, selected, True, columns, qr
        self.busy_on = False
        self.log.append(f"ov:show:{selected}")
        return True

    async def help(self, title, rows, top, page, hint) -> bool:
        self.help_rows, self.busy_on = rows, False
        self.log.append(f"ov:help:{title}")
        return True

    async def select(self, index: int, chip: bool = False) -> bool:
        self.sel, self.chip = index, chip
        self.log.append(f"ov:select:{index}" + (":chip" if chip else ""))
        return True

    async def image(self, title, path, lines, status="", hint="", size=9) -> bool:
        self.title, self.image_path, self.status_text = title, path, status
        self.busy_on = False
        self.log.append(f"ov:image:{title}")
        return True

    async def status(self, text: str) -> bool:
        self.status_text = text
        self.log.append(f"ov:status:{text}")
        return True

    async def level(self, index: int, item: dict) -> bool:
        self.items[index] = item
        self.log.append(f"ov:level:{index}:{item.get('level')}" + (":mute" if item.get("muted") else ""))
        return True

    async def busy(self, text: str) -> bool:
        self.busy_on = True
        self.log.append(f"ov:busy:{text}")
        return True

    async def toast(self, text: str, ms: int = 2000) -> bool:
        self.log.append(f"ov:toast-tapado:{text}" if self.busy_on else f"ov:toast:{text}")
        return True

    async def hide(self) -> bool:
        self.visible = self.busy_on = False
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

    async def info(self) -> str:
        self.log.append("mpv:info")
        return "Obsession\n1:02 de 1:40:00\nVideo 1 de 3"

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

    scrub = True  # buscando con ◀ ▶ (Atrás deja de buscar); False: Atrás cierra

    async def scrubbing(self) -> bool:
        return self.scrub

    async def toggle_shuffle(self) -> bool:
        self.log.append("mpv:shuffle")
        self.shuffled = not getattr(self, "shuffled", False)
        return self.shuffled

    async def cycle_loop(self) -> str:
        self.log.append("mpv:loop")
        self.looping = {"none": "playlist", "playlist": "track", "track": "none"}[getattr(self, "looping", "none")]
        return self.looping

    async def cycle(self, kind: str) -> str:
        self.log.append(f"mpv:{kind}")
        return {"sub": "Español", "audio": "Inglés", "aspect": "4:3"}[kind]

    async def playback(self) -> str:
        return "Playing" if self.on else ""


class FakeVideos(FakePlayerMixin, FakeProcMixin, activities_mod.VideosActivity):
    # Como un `command` propio: OK abre de una y en las carpetas se reproduce la carpeta (el de verdad se prueba en
    # «OK sobre Videos y Música: la carpeta predeterminada»).
    browse_on_ok = False
    pick_files = False

    def __init__(self, log: list[str]):
        activities_mod.VideosActivity.__init__(self, {})
        self.setup(log)
        self.mpris = FakeMpris(log, "mpv")


class FakeMusicLocal(FakePlayerMixin, FakeProcMixin, activities_mod.MusicActivity):
    browse_on_ok = False
    pick_files = False

    def __init__(self, log: list[str]):
        activities_mod.MusicActivity.__init__(self, {})
        self.setup(log)


class FakePhotos(FakeProcMixin, activities_mod.PhotosActivity):
    def __init__(self, log: list[str]):
        activities_mod.PhotosActivity.__init__(self, {"viewer": "feh"})  # las teclas a feh (mpv se prueba aparte)
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
        base = self.current_folder or Path("/mnt/fotos-red")
        return {"path": str(base / "2017" / "07" / "IMG_0001.jpg"), "index": 12, "total": 49403}


def help_rows(ctl, ctx):
    from tele_cec.ui import help as help_mod
    return help_mod.rows(ctl.cfg, ctx)


async def main() -> int:
    from tele_cec.controller import activities as ctl_activities
    ctl_activities.FOTOS_REMINDER_S = 0.0  # el aviso del modo de Fotos, sin esperar en las pruebas
    tmp = Path(tempfile.mkdtemp(prefix="nfx-apps-"))
    from tele_cec.apps import fileinfo as fileinfo_mod
    fileinfo_mod._video_info = fileinfo_mod.VideoInfo(tmp / "videos-info.json")  # no tocar el de verdad
    from tele_cec.apps import resume as resume_mod
    resume_mod.WATCH_DIR = tmp / "videos-seguir"  # Seguir viendo: tampoco
    from tele_cec.apps import photodates as photodates_mod
    photodates_mod._dates = photodates_mod.PhotoDates(tmp / "fotos-fechas.json")  # las fechas de las fotos: tampoco
    photodates_mod._dates.pause_s = 0.0  # sin la pausa entre fotos (en la Pi va de a poco)
    from tele_cec.apps import photos as photos_main
    photos_main.DATA_DIR = tmp / "datos"  # las fotos favoritas: no tocar las de verdad
    from tele_cec.apps import navegador as navegador_main
    navegador_main.DATA_DIR = tmp / "datos"  # los sitios guardados desde la TV: tampoco
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
    out = await press("DOWN", "UP", "DOWN")
    check(out == ["ov:select:1", "ov:select:0", "ov:select:1"], f"▼ y ▲ mueven: {out}")
    out = await press("DOWN", "DOWN", "DOWN", "DOWN")
    check(out[-1] == "ov:select:0", f"después de la última vuelve a la primera: {out}")
    out = await press("ROOT_MENU")
    check(out == ["ov:hide"] and not ctl._menu_open and await ctl.context() == "offline",
          f"Menú otra vez, en el menú principal, lo cierra (ya no pasa a la opción siguiente): {out}")
    out = await press("ROOT_MENU", "NUMBER5")
    check(out[-1].startswith("ov:help:") and ctl._menu_mode == "help", f"Menú lo abre y 5 es la Ayuda: {out}")
    out = await press("ROOT_MENU")
    check(out == ["ov:show:4"] and ctl._menu_open and ctl._menu_mode == "activities",
          f"Menú desde una lista de adentro (la Ayuda) vuelve al menú, como Atrás: {out}")
    await ctl.run_action("menu:next")
    check(ctl._menu_sel == 0, "menu:next sigue siendo «la opción siguiente» (para quien lo quiera en una tecla)")
    check(ctl.cfg.action_for("menu", parse_key("ROOT_MENU")) == ("menu:back", "menu"),
          "por defecto, la tecla Menú con el menú abierto es menu:back")
    out = await press("EXIT")
    check(out == ["ov:hide"] and await ctl.context() == "offline", f"Atrás cierra el menú sin abrir nada: {out}")
    check(ctl.cfg.action_for("browse", parse_key("NUMBER1"))[0] == "my_list_toggle"
          and ctl.cfg.action_for("player", parse_key("NUMBER1"))[0] == "my_list_toggle"
          and ctl.cfg.action_for("player_menu", parse_key("NUMBER1"))[0] == "my_list_toggle",
          "Netflix: 1 = ♥ Mi lista en el inicio, en una ficha y viendo un video")
    check(ctl.cfg.action_for("profiles", parse_key("NUMBER1"))[0] == "profile:1"
          and ctl.cfg.action_for("text", parse_key("NUMBER1"))[0] == "type:1",
          "pero en los perfiles sigue eligiendo el 1, y escribiendo escribe el 1")
    check(ctl.cfg.action_for("youtube", parse_key("NUMBER1"))[0] == "yt:like", "YouTube: 1 = ♥ me gusta")
    check(ctl.cfg.action_for("menu", parse_key("DOT")) == ("menu:help", "menu"),
          "con el menú abierto, el punto (•) abre la ayuda (muchas TVs no mandan la tecla Ayuda)")
    check(ctl.cfg.action_for("offline", parse_key("EXIT")) == ("menu", "offline"),
          "sin nada abierto, Atrás abre el menú de actividades")

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
    out = await press("SELECT", "FORWARD", "BACKWARD", "UP", "DOWN", "NUMBER1", "F1_BLUE")
    check(out == ["mpris:play-pause", "mpris:next", "mpris:previous", "mpris:volume+", "mpris:volume-",
                  "like", "ov:toast:Volver a Volver\nGabo Ferro · Mancha de Rolando\n1:02 de 3:40 · en pausa"],
          f"OK, ⏭, ⏮, ▲, ▼, 1 (♥ me gusta), azul (qué suena): {out}")
    out = await press("CHANNEL_UP", "CHANNEL_DOWN", "RIGHT", "LEFT", "FAST_FORWARD", "REWIND")
    check(out == ["mpris:next", "mpris:previous", "mpris:seek+10", "mpris:seek-10", "mpris:seek+60", "mpris:seek-60"],
          f"Canal ± = tema, ◀ ▶ = 10 s, ⏪ ⏩ = 1 min (como en Música y Videos): {out}")
    out = await press("EXIT", "F3_GREEN")
    check(out == ["music:cmd:back"], f"Atrás vuelve a la pantalla anterior de High Tide; el verde no hace nada: {out}")
    closed = []
    real_close = ctl.a_close

    async def fake_close(ident: str) -> None:
        closed.append(ident)
    ctl.a_close = fake_close  # type: ignore[method-assign]
    ctl.music.back_reply = "no"
    await press("EXIT")
    check(closed == ["tidal"], f"en la primera pantalla de High Tide (sin a dónde volver), Atrás cierra Tidal: {closed}")
    ctl.music.back_reply = "ok"
    ctl.a_close = real_close  # type: ignore[method-assign]
    out = await press("SUB_PICTURE", "F2_RED")
    check(out == ["letras", "shuffle"], f"CC = letras, rojo = mezclar: {out}")
    await press("NUMBER0")
    check(await ctl.context() == "tidal_nav", "0 activa el modo navegación de Tidal")
    out = await press("DOWN", "LEFT", "SELECT", "EXIT")
    check(out == ["wtype:Down", "wtype:Left", "wtype:Return", "music:cmd:back"],
          f"en modo navegación: flechas, OK y Atrás van tal cual a la app (sin seguir el foco): {out}")
    out = await press("FORWARD", "BACKWARD", "CHANNEL_UP", "CHANNEL_DOWN", "NUMBER1")
    check(out == ["mpris:next", "mpris:previous", "mpris:next", "mpris:previous", "like"],
          f"⏭ ⏮ y Canal ± cambian de tema también recorriendo la interfaz; 1 = ♥ me gusta: {out}")
    out = await press("NUMBER4", "NUMBER2")
    check(out == ["go:collection", "go:home"], f"4 = Colección, 2 = Inicio (el 1 es ♥): {out}")
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
    out = await press("SELECT", "RIGHT", "LEFT", "FAST_FORWARD", "UP", "FORWARD", "CHANNEL_DOWN", "SUB_PICTURE", "F2_RED",
                      "F4_YELLOW", "EXIT", "F1_BLUE")
    check(out == ["mpv:ok", "mpv:seek+10", "mpv:seek-10", "mpv:seek+60", "mpv:volume+", "ov:toast:Volumen 55 %",
                  "mpv:next", "mpv:previous", "mpv:sub", "ov:toast:Subtítulos: Español", "mpv:audio",
                  "ov:toast:Audio: Inglés", "mpv:aspect", "ov:toast:Proporción: 4:3", "mpv:osd:cancel", "mpv:info",
                  "ov:toast:Obsession\n1:02 de 1:40:00\nVideo 1 de 3"],
          f"OK, ▶ ◀ (buscar), ⏩ (1 min), ▲, ⏭, Canal−, CC, rojo, amarillo, Atrás (deja de buscar), azul (qué es): {out}")
    closed.clear()
    ctl.a_close = fake_close  # type: ignore[method-assign]
    ctl.activities["videos"].scrub = False
    out = await press("EXIT")
    check(closed == ["videos"] and "mpv:osd:cancel" not in out, f"sin estar buscando, Atrás cierra Videos (como ■): {closed} {out}")
    ctl.activities["videos"].scrub = True
    ctl.a_close = real_close  # type: ignore[method-assign]

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
    out = await press("RIGHT", "LEFT", "SELECT")
    check(out == ["feh:next", "feh:previous", "feh:h"], f"▶, ◀, OK: {out}")
    check(ctl.cfg.action_for("fotos", parse_key("EXIT")) == ("close:fotos", "fotos")
          and ctl.cfg.action_for("radio", parse_key("EXIT")) == ("close:radio", "radio"),
          "en Fotos y en la Radio, Atrás cierra y vuelve al menú (como ■)")
    check(fotos.paused, "queda en pausa")
    out = await press("NUMBER1")
    check(out == ["ov:toast:♥ Guardada en Favoritas (1) · 1 otra vez la saca"]
          and fotos.favorites() == ["/mnt/fotos-red/2017/07/IMG_0001.jpg"],
          f"1 guarda la foto que se ve en Favoritas (también con feh): {out}")
    out = await press("NUMBER1")
    check(out == ["ov:toast:Sacada de Favoritas (0)"] and fotos.favorites() == [], f"otra vez 1 la saca: {out}")
    check(ctl.cfg.action_for("fotos_zoom", parse_key("NUMBER1"))[0] == "fotos:favorite"
          and ctl.cfg.action_for("tidal", parse_key("NUMBER1"))[0] == "music:like"
          and ctl.cfg.action_for("fotos", parse_key("NUMBER2")) is None,
          "también con zoom; el 1 es ♥ también en Tidal; el 2 queda libre")
    await press("SELECT")
    fotos.current_folder = Path("/mnt/fotos-red")
    out = await press("F2_RED", "F4_YELLOW", "F1_BLUE")
    where = activities_mod.describe_folder(Path("/mnt/fotos-red"))  # «En red: …» o «Pendrive …» según la máquina
    check(out == ["feh:greater", "feh:z", f"ov:toast:{where} › 2017 › 07\nIMG_0001.jpg\nFoto 12 de 49.403"],
          f"rojo gira, amarillo al azar, azul = qué foto es (carpeta, archivo, número): {out}")

    print("Fotos: el mapa de dónde se sacó (azul dos veces)")
    out = await press("F3_GREEN")
    check(out == [], f"el verde queda libre (la TV no siempre lo manda): {out}")
    out = await press("F1_BLUE")
    check(len(out) == 1 and out[0].startswith("ov:toast:") and "Azul otra vez" not in out[0] and not ctl._menu_open,
          f"una foto sin GPS: el azul otra vez solo vuelve a decir qué foto es: {out}")
    start = len(log)
    await ctl.run_action("fotos:map")
    check(log[start:] == ["ov:toast:Esta foto no tiene la ubicación (GPS)"], f"fotos:map sin GPS lo dice: {log[start:]}")
    from tele_cec.apps import photomap as photomap_mod
    if photomap_mod.available():
        from PIL import Image as PILImage
        gps_jpg = tmp / "fotos-gps" / "2019" / "01" / "IMG_20190120_120000.jpg"
        gps_jpg.parent.mkdir(parents=True)
        ex = PILImage.Exif()
        g = ex.get_ifd(0x8825)
        g[1], g[2], g[3], g[4] = "S", (41.0, 8.0, 3.6), "W", (71.0, 18.0, 36.0)
        PILImage.new("RGB", (16, 16)).save(gps_jpg, exif=ex.tobytes())
        renders: list = []

        def fake_render(self, lat, lon, zoom, out, size=photomap_mod.SIZE):  # sin internet
            renders.append((lat, lon, zoom))
            PILImage.new("RGB", (64, 40), "white").save(out)
            return out
        real_render, real_info = photomap_mod.MapMaker.render, fotos.photo_info

        async def gps_info():
            return {"path": str(gps_jpg), "index": 3, "total": 10}
        photomap_mod.MapMaker.render = fake_render  # type: ignore[method-assign]
        fotos.photo_info = gps_info  # type: ignore[method-assign]
        try:
            ctl._fotos_info = ("", 0.0)
            out = await press("F1_BLUE")
            check(out and out[-1].endswith("Azul otra vez: dónde se sacó (mapa)") and ctl._menu_mode != "map",
                  f"el azul dice qué foto es y avisa que tiene mapa: {out}")
            out = await press("F1_BLUE")
            check(out[:1] == ["feh:h"] and "ov:busy:Buscando el mapa…" in out
                  and out[-1] == "ov:image:Dónde se sacó · IMG_20190120_120000.jpg"
                  and renders == [(-41.134333, -71.31, 15)] and ctl._menu_mode == "map" and fotos.paused,
                  f"azul otra vez: pausa las fotos y muestra el mapa en el lugar de la foto: {out} {renders}")
            await press("UP", "UP", "DOWN")
            check([r[2] for r in renders] == [15, 16, 17, 16], f"▲ acerca, ▼ aleja: {renders}")
            out = await press("EXIT")
            check(out == ["ov:hide", "feh:h"] and not ctl._menu_open and not fotos.paused
                  and await ctl.context() == "fotos", f"Atrás vuelve a las fotos, que siguen: {out}")
        finally:
            photomap_mod.MapMaker.render = real_render  # type: ignore[method-assign]
            fotos.photo_info = real_info  # type: ignore[method-assign]
        check(photomap_mod.to_pixels(0, 0, 0) == (128.0, 128.0)
              and [int(v // 256) for v in photomap_mod.to_pixels(-34.60374, -58.38157, 15)] == [11069, 19744],
              "de latitud y longitud al mapa: el Obelisco cae en el mosaico 15/11069/19744, como en OpenStreetMap")

    print("Temporizador: el punto (•) dos veces, desde cualquier pantalla")
    ctl.sleep_timer.cancel()
    ctl.sleep_timer._panel_until = 0.0
    out = await press("DOT")
    check(ctl._menu_open and ctl._menu_mode == "help", f"un toque: la Ayuda, como siempre: {out}")
    out = await press("DOT")
    check(not ctl._menu_open and "ov:hide" in out
          and out[-1] == "ov:toast:Temporizador · Sin temporizador\n• otra vez: en 15 minutos",
          f"el segundo toque seguido: se va la Ayuda y dice cómo está el temporizador: {out}")
    out = await press("DOT")
    check(ctl.sleep_timer.choice == 15 and ctl.sleep_timer.active()
          and out[-1] == "ov:toast:Temporizador · Se apaga en 15 min\n• otra vez: en 30 minutos"
          and not ctl._menu_open, f"con el aviso a la vista, cada toque pasa a la opción siguiente: {out}")
    await press("DOT")
    check(ctl.sleep_timer.choice == 30 and await ctl.context() == "fotos", "30 minutos, y las fotos siguen ahí")
    ctl.sleep_timer._panel_until = 0.0  # se fue el aviso
    out = await press("DOT")
    check(ctl._menu_mode == "help" and ctl.sleep_timer.choice == 30, f"después, un toque es la Ayuda otra vez: {out}")
    await press("EXIT")
    ctl.sleep_timer._key_last = 0.0
    out = await press("DOT", "RIGHT")
    check(not any(x.startswith("ov:toast:Temporizador") for x in out), f"• y otra tecla: nada del temporizador: {out}")
    await press("EXIT")
    ctl.sleep_timer.cancel()
    ctl.sleep_timer._panel_until = ctl.sleep_timer._key_last = 0.0

    print("La tecla CC (SUB_PICTURE, así la mandan algunas TVs)")
    af = ctl.cfg.action_for
    check(af("radio", 0x71) == ("radio:info", "radio") and af("radio", 0x74) == ("radio:list", "radio")
          and af("musica", 0x71) == ("musica:info", "musica") and af("tidal_nav", 0x71) == ("music:info", "tidal")
          and af("youtube", 0x71) == ("yt:home", "youtube"),
          "azul = qué es / qué suena (Videos, Música, Tidal, Radio), amarillo en Radio = la lista, azul en YouTube = inicio")
    check(af("player", 0x51) == ("subtitles", "player") and af("player_menu", 0x51) == ("subtitles", "player_menu")
          and af("player", 0x71) == ("profiles", "player") and af("videos", 0x51) == ("video:subtitles", "videos")
          and af("videos", 0x71) == ("video:info", "videos") and af("tidal", 0x51) == ("music:lyrics", "tidal")
          and af("tidal_nav", 0x51) == ("music:lyrics", "tidal") and af("tidal", 0x72) == ("music:shuffle", "tidal")
          and af("youtube", 0x51) == ("yt:subtitles", "youtube") and af("fotos", 0x51) == ("fotos:date", "fotos")
          and af("fotos_zoom", 0x51) == ("fotos:date", "fotos"),
          "CC: subtítulos (Netflix, Videos, YouTube), letras (Tidal), la fecha (Fotos); azul en Netflix = perfiles, "
          "en Videos = qué es; rojo en Tidal = mezclar")
    from tele_cec.ui import help as help_t
    check(any(k == "CC" and d == "Subtítulos" for k, d in help_t.rows(ctl.cfg, "player")), "la Ayuda la muestra como CC")

    print("Fotos: modos (▼)")
    fotos.current_folder = fotos.current_folder or Path("/mnt/fotos-red")
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
    await press("ROOT_MENU")  # con una app abierta arranca la cuenta…
    ctl.current_app = lambda: None  # type: ignore[method-assign]  # …y la app termina de cerrarse mientras tanto
    await asyncio.sleep(0.4)
    check(ctl._menu_open, "si la app se cerró después de abrirse el menú, el menú queda a la vista")
    del ctl.current_app
    await ctl.menu_close()
    ctl._menu_timeout = 10
    ctl._switching = True
    out = await press("ROOT_MENU", "DOWN", "SELECT")
    check(out == [], f"mientras se cambia de actividad, las teclas se ignoran: {out}")
    ctl._switching = False

    print("Elegir otra carpeta (▶ sobre Videos)")
    usb = tmp / "pick" / "media" / "usuario" / "USB"
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

    print("OK sobre Videos y Música: la carpeta predeterminada (Videos: se elige el archivo)")
    (usb / "peli.mkv").write_bytes(b"x" * 3 * (1 << 20))
    (usb / "peli.es.srt").write_text("1\n")
    (usb / "notas.txt").write_text("")
    videos.browse_on_ok = videos.pick_files = True
    await videos.close()
    ctl._foreground = None
    try:
        await press("ROOT_MENU")
        for _ in range(6):
            if ctl._menu_mode == "activities" and ctl._menu_sel == vid:
                break
            await press("DOWN")
        out = await press("SELECT")
        kinds = [(it.get("kind"), it["name"]) for it in ctl.overlay.items]
        check(not opened(out) and ctl._menu_mode == "folders" and kinds == [("dir", "Pelis"), ("dir", "Series"),
                                                                           ("file", "peli")],
              f"OK no arranca nada: abre la predeterminada, con subcarpetas y videos (sin «Reproducir»): {kinds}")
        check(ctl.overlay.items[2]["sub"] == "3 MB · con subtítulos", f"el video dice tamaño y subtítulos: {ctl.overlay.items[2]}")
        out = await press("LEFT")
        check(ctl._menu_mode == "folders" and ctl.overlay.items[0]["sub"] == "predeterminada" and ctl._menu_sel == 0,
              f"◀ sube a las carpetas configuradas (hay dos): {[it['name'] for it in ctl.overlay.items]}")
        out = await press("DOWN", "SELECT")
        check([(it["kind"], it["enabled"]) for it in ctl.overlay.items] == [("empty", False)],
              f"una carpeta sin videos lo dice: {ctl.overlay.items}")
        out = await press("SELECT")
        check(not opened(out) and any(x.startswith("ov:toast:No hay videos") for x in out), f"y no abre nada: {out}")
        await press("LEFT", "LEFT")
        check(ctl._menu_mode == "activities" and ctl._menu_sel == vid, "◀ otra vez: al menú, en Videos")
        await press("SELECT", "UP")
        out = await press("SELECT")
        check(opened(out)[-1:] == ["videos:open:peli.mkv"] and not ctl._menu_open,
              f"OK sobre un video: solo ese: {opened(out)}")
        await press("ROOT_MENU")
        for _ in range(6):
            if ctl._menu_mode == "activities" and ctl._menu_sel == vid:
                break
            await press("DOWN")
        out = await press("SELECT")
        check(not ctl._menu_open and not opened(out), f"con Videos abierto, OK sigue viendo: {out}")
        await ctl.activities["videos"].close()
        ctl._foreground = None

        mus_root = tmp / "pick" / "Música"
        (mus_root / "Disco").mkdir(parents=True)
        for n in ("1 - Uno.mp3", "2 - Dos.mp3", "10 - Diez.mp3", "tapa.jpg"):
            (mus_root / "Disco" / n).write_bytes(b"")
        mlocal = FakeMusicLocal(log)
        mlocal.folders, mlocal.browse_on_ok, mlocal.pick_files = [str(mus_root)], True, True
        ctl.activities["musica"] = mlocal
        items_before = ctl.cfg.menu.get("items")
        ctl.cfg.menu["items"] = ["netflix", "tidal", "videos", "musica", "fotos"]
        await press("ROOT_MENU")
        mi = next(i for i, it in enumerate(ctl.overlay.items) if it["id"] == "musica")
        for _ in range(8):
            if ctl._menu_mode == "activities" and ctl._menu_sel == mi:
                break
            await press("DOWN")
        idx = {"version": 1, "files": {str(mus_root / "Disco" / "2 - Dos.mp3"): {
            "sig": fileinfo_mod._sig(str(mus_root / "Disco" / "2 - Dos.mp3")),
            "tags": {"title": "Dos (en vivo)", "artist": "La Banda", "album": "Disco", "dur": 225.4}}}}
        (tmp / "cache-musica").mkdir(exist_ok=True)
        (tmp / "cache-musica" / "musica-indice.json").write_text(json.dumps(idx))
        orig_fi_dir, fileinfo_mod.LOG_DIR = fileinfo_mod.LOG_DIR, tmp / "cache-musica"
        out = await press("SELECT")
        check(not mlocal.on and ctl._menu_mode == "folders" and [it['name'] for it in ctl.overlay.items] == ["▶ Escuchar esta carpeta", "Disco"],
              f"Música: OK abre la carpeta, con «Escuchar esta carpeta» y los discos: {[it['name'] for it in ctl.overlay.items]}")
        check(ctl.overlay.items[0]["sub"] == "en 1 subcarpeta",
              f"sin temas sueltos: «en 1 subcarpeta» (no «con sus subcarpetas»): {ctl.overlay.items[0]['sub']}")
        await press("DOWN", "SELECT")
        got = [(it.get("kind"), it["name"]) for it in ctl.overlay.items]
        check(got == [("play", "▶ Escuchar esta carpeta"), ("file", "1 - Uno"), ("file", "Dos (en vivo)"),
                      ("file", "10 - Diez")],
              f"en un disco: escucharlo entero o elegir un tema (en orden, sin la tapa; con su título): {got}")
        check(ctl.overlay.items[0]["sub"] == "3 temas" and ctl.overlay.items[2]["sub"] == "La Banda · 3:45",
              f"«3 temas» (sin subcarpetas) y el tema dice artista y duración (el disco es esta carpeta): "
              f"{ctl.overlay.items[0]['sub']} / {ctl.overlay.items[2]['sub']}")
        fileinfo_mod.LOG_DIR = orig_fi_dir
        out = await press("DOWN", "DOWN", "SELECT")
        check("musica:open:2 - Dos.mp3" in out and mlocal.on, f"OK sobre un tema lo pone: {out}")
        await mlocal.close()
        ctl._foreground = None
        await press("ROOT_MENU")
        for _ in range(8):
            if ctl._menu_mode == "activities" and ctl._menu_sel == mi:
                break
            await press("DOWN")
        out = await press("SELECT", "SELECT")
        check("musica:open:Música" in out and mlocal.on, f"y escuchar la carpeta entera sigue igual: {out}")
        await mlocal.close()
        del ctl.activities["musica"]
        if items_before is None:
            ctl.cfg.menu.pop("items", None)
        else:
            ctl.cfg.menu["items"] = items_before
    finally:
        videos.browse_on_ok = videos.pick_files = False
        ctl._foreground = None
        await ctl.menu_close()

    print("Temporizador para apagar")
    import time as time_mod
    from types import SimpleNamespace
    from tele_cec.controller import sleeptimer as st_mod
    from tele_cec.system import audio as audio_st
    from tele_cec.web import nowplaying as np_st
    st_log: list = []
    vol_st = {"level": 40}

    async def st_get_volume():
        return vol_st["level"], False

    async def st_change(what, step=5):
        vol_st["level"] = int(what[1:])
        st_log.append(f"vol{what}")
        return True

    async def st_pause(everything=False):
        st_log.append("pausa")

    async def st_sleep(why):
        st_log.append(f"reposo:{why}")

    async def st_toast(text, ms=2000):
        st_log.append(f"aviso:{text}")
    playing = [np_st.Track("mpv", "Playing", "Tema uno", length=200.0, position=100.0)]

    async def st_players():
        return [np_st.Track(**vars(t)) for t in playing]
    fake_ctl = SimpleNamespace(overlay=SimpleNamespace(toast=st_toast), _pause_if_playing=st_pause,
                               power=SimpleNamespace(asleep=False, enabled=True, mode="reposo", sleep=st_sleep))
    saved_st = (audio_st.available, audio_st.get_volume, audio_st.change_volume, np_st.players, st_mod.FADE_S,
                st_mod.TRACK_POLL_S)
    audio_st.available, audio_st.get_volume, audio_st.change_volume = (lambda: True), st_get_volume, st_change
    np_st.players, st_mod.FADE_S, st_mod.TRACK_POLL_S = st_players, 0.15, 0.02
    try:
        tm = st_mod.SleepTimer(fake_ctl)  # type: ignore[arg-type]
        ok, msg = await tm.set(30)
        check(ok and msg == "Se apaga en 30 min" and tm.status()["mode"] == "time" and 1790 < tm.status()["left"] <= 1800
              and tm.text() == "Se apaga en 30 min", f"en 30 minutos: {msg} {tm.status()}")
        tm.until = time_mod.monotonic() + 0.05  # que se cumpla ya
        tm._task.cancel()
        tm._task = asyncio.get_running_loop().create_task(tm._run_time())
        await asyncio.wait_for(tm._task, 3)
        vols = [x for x in st_log if x.startswith("vol")]
        check(vols[0] == "vol=37" and "vol=0" in vols and st_log.index("pausa") > st_log.index("vol=0")
              and st_log[-2:] == ["vol=40", "reposo:temporizador (temporizador)"] and not tm.active(),
              f"al cumplirse: baja el volumen de a poco, pausa, lo deja como estaba y entra en reposo: {st_log}")
        st_log.clear()
        ok, msg = await tm.set("track")
        check(ok and tm.status() == {"mode": "track", "text": "al terminar este tema"}, f"al terminar este tema: {msg}")
        await asyncio.sleep(0.1)
        check(not st_log, "mientras suena, espera")
        playing[0] = np_st.Track("mpv", "Playing", "Tema dos", length=200.0, position=0.5)
        await asyncio.wait_for(tm._task, 3)
        check(st_log == ["pausa", "reposo:temporizador (terminó «Tema uno»)"],
              f"cuando pasa al siguiente: pausa enseguida, sin bajar el volumen, y reposo: {st_log}")
        st_log.clear()
        playing[0] = np_st.Track("mpv", "Playing", "Tema tres", length=200.0, position=199.6)
        await tm.set("track")
        await asyncio.wait_for(tm._task, 3)
        check(st_log[:1] == ["pausa"], f"a punto de terminar: pausa justo antes del siguiente: {st_log}")
        playing.clear()
        ok, msg = await tm.set("track")
        check(not ok and msg == "No suena nada" and not tm.active(), f"sin nada sonando, no: {msg}")
        await tm.set(15)
        ok, msg = await tm.step(1)
        check(tm.choice == 30, f"▶: la opción siguiente: {tm.choice}")
        await tm.step(1)
        await tm.step(1)
        await tm.step(1)
        ok, _ = await tm.step(1)  # «al terminar este tema» sin nada sonando: la saltea
        check(ok and tm.choice == 0 and not tm.active(), f"sin nada sonando, saltea «al terminar» y vuelve a ninguno: "
                                                        f"{tm.choice}")
        await tm.set(60)
        await tm.set(0)
        check(not tm.active() and tm.status() is None and tm.text() == "Sin temporizador", "sacarlo")
        dot = tm.key_code
        check(dot == 0x2A and tm.key_press(dot) is None and tm.key_press(dot) == "show" and tm.key_press(dot) is None,
              "la tecla: el primer toque es lo de siempre, el segundo seguido muestra el temporizador")
        tm._key_last = time_mod.monotonic() - 2
        check(tm.key_press(dot) is None, "dos toques separados (más de 1 s) no cuentan")
        tm.key_press(0x01)
        check(tm.key_press(dot) is None, "otra tecla en el medio, tampoco")
        txt, ms = await tm.key_text("show")
        check(txt == "Temporizador · Sin temporizador\n• otra vez: en 15 minutos" and ms == 4000 and tm.panel_visible()
              and tm.key_press(0x01) is None and tm.key_press(dot) == "step",
              f"con el aviso a la vista, el toque siguiente cambia la opción: {txt}")
        txt, _ = await tm.key_text("step")
        check(tm.choice == 15 and txt.startswith("Temporizador · Se apaga en 15 min"), f"15 min: {txt}")
        seen = [(await tm.key_text("step"))[0].split("\n")[1] for _ in range(4)]
        check(tm.choice == 90 and seen[-1] == "• otra vez: apagarlo",
              f"sin nada sonando, después de 1 h y media no ofrece «al terminar este tema»: apagarlo: {seen}")
        txt, ms = await tm.key_text("step")
        check(txt == "Temporizador apagado" and not tm.active() and not tm.panel_visible() and tm.key_press(dot) is None,
              f"el recorrido termina apagado: el aviso se va y el próximo toque vuelve a ser la Ayuda: {txt}")
        playing.append(np_st.Track("mpv", "Playing", "Tema cuatro", length=200.0, position=10.0))
        await tm.set(90)
        txt, _ = await tm.key_text("show")
        check(txt.endswith("• otra vez: al terminar este tema o video"), f"con algo sonando, sí lo ofrece: {txt}")
        txt, _ = await tm.key_text("step")
        check(tm.choice == "track" and txt.endswith("• otra vez: apagarlo"), f"y después, apagarlo: {txt}")
        txt, _ = await tm.key_text("step")
        check(txt == "Temporizador apagado" and not tm.active(), f"apagado: {txt}")
        playing.clear()
        await tm.set(120)  # desde el celular, algo que no está en la lista
        check(tm._key_next(False) == 0 and (await tm.set(20)) and tm._key_next(False) == 30,
              "un tiempo puesto desde el celular sigue con el siguiente más largo (o apagado)")
        await tm.set(0)
        tm._panel_until = tm._key_last = 0.0

        # el último minuto: cuenta regresiva, y una tecla lo retrasa
        st_log.clear()
        await tm.set(30)
        check(tm.note_key() is False and tm.until - time_mod.monotonic() > 1700,
              "antes del último minuto, las teclas son de la app (no lo retrasan)")
        tm.until = time_mod.monotonic() + 1.2
        tm._task.cancel()
        tm._task = asyncio.get_running_loop().create_task(tm._run_time())
        await asyncio.sleep(0.3)
        check(tm.warning and any(x.startswith("aviso:Se apaga en 1 s\nCualquier tecla: 15 minutos más") for x in st_log),
              f"cuenta regresiva en la TV, diciendo cómo retrasarlo: {st_log}")
        old_task = tm._task
        check(tm.note_key() is True and not tm.warning and 890 < tm.left() <= 900, "una tecla: 15 minutos más")
        await asyncio.sleep(0.05)
        check(old_task.done() and tm.active() and "aviso:15 minutos más · se apaga en 15 min" in st_log
              and not any(x.startswith("reposo") or x == "pausa" for x in st_log),
              f"y lo dice; no pausa ni entra en reposo: {st_log}")
        st_log.clear()
        vol_st["level"] = 40
        tm.until = time_mod.monotonic() + 0.05
        tm._task.cancel()
        tm._task = asyncio.get_running_loop().create_task(tm._run_time())
        await asyncio.sleep(0.08)  # ya bajando el volumen
        check(tm.warning and any(x.startswith("aviso:Bajando el volumen") for x in st_log) and vol_st["level"] < 40,
              f"bajando el volumen, también se puede retrasar: {st_log}")
        tm.note_key()
        await asyncio.sleep(0.05)
        check(vol_st["level"] == 40 and "pausa" not in st_log and tm.active() and 890 < tm.left() <= 900,
              f"retrasado mientras bajaba: el volumen vuelve a como estaba: {vol_st} {st_log}")
        await tm.set(0)
        check([st_mod.choice_text(c) for c in st_mod.CHOICES]
              == ["Sin temporizador", "En 15 minutos", "En 30 minutos", "En 45 minutos", "En 1 h", "En 1 h y media",
                  "Al terminar este tema o video"]
              and st_mod.left_text(3900) == "en 1 h 05 min" and st_mod.left_text(20) == "en menos de un minuto",
              "los textos")
    finally:
        (audio_st.available, audio_st.get_volume, audio_st.change_volume, np_st.players, st_mod.FADE_S,
         st_mod.TRACK_POLL_S) = saved_st

    print("Fechas de las fotos: guardadas")
    pdc = photodates_mod.PhotoDates(tmp / "fechas-vistas.json")
    pdc.pause_s = 0.0
    seen = tmp / "vistas"
    seen.mkdir()
    for i in range(photodates_mod.SAVE_SEEN_EVERY + 1):
        (seen / f"DSC{i:05d}.JPG").write_bytes(b"\xff\xd8\xff\xda\0\x02")
    pdc.date_of(str(seen / "DSC00000.JPG"))
    check(not (tmp / "fechas-vistas.json").exists() and pdc._data[str(seen / "DSC00000.JPG")] == "",
          "la fecha leída al mirar una foto queda en memoria…")
    for i in range(1, photodates_mod.SAVE_SEEN_EVERY):
        pdc.date_of(str(seen / f"DSC{i:05d}.JPG"))
    saved = json.loads((tmp / "fechas-vistas.json").read_text())["files"]
    check(len(saved) == photodates_mod.SAVE_SEEN_EVERY, f"…y se guarda cada {photodates_mod.SAVE_SEEN_EVERY}: {len(saved)}")
    pdc.date_of(str(seen / f"DSC{photodates_mod.SAVE_SEEN_EVERY:05d}.JPG"))
    pdc.flush()
    check(len(json.loads((tmp / "fechas-vistas.json").read_text())["files"]) == photodates_mod.SAVE_SEEN_EVERY + 1,
          "y al cerrar Fotos, las que falten (flush)")
    order: list = []
    real_exif = photodates_mod.exif_date

    def spy_exif(path):
        order.append(("exif", json.loads((tmp / "fechas-orden.json").read_text())["files"]
                      if (tmp / "fechas-orden.json").exists() else {}))
        return None
    photodates_mod.exif_date = spy_exif
    try:
        po = photodates_mod.PhotoDates(tmp / "fechas-orden.json")
        po.pause_s = 0.0
        po.update(["/x/DSC1.JPG", "/x/IMG_20170226_141339.jpg", "/x/IMG-20180321-WA0001.jpg"])
    finally:
        photodates_mod.exif_date = real_exif
    check(order and order[0][1].get("/x/IMG_20170226_141339.jpg") == "20170226"
          and order[0][1].get("/x/IMG-20180321-WA0001.jpg") == "20180321",
          "las del nombre se guardan primero (al instante); después, de a poco, las que hay que abrir")
    from tele_cec.apps import photos as photos_rd
    real_pd_dir = photos_rd.LOG_DIR
    photos_rd.LOG_DIR = tmp / "cache-rd"
    photos_rd.LOG_DIR.mkdir()
    (photos_rd.LOG_DIR / "fotos-abc.lst").write_text("/y/IMG_20100101_000000.jpg\n/y/b.jpg\n")
    (photos_rd.LOG_DIR / "fotos-abc.feh.lst").write_text("/y/no-esta.jpg\n")
    started: list = []
    real_dates = photodates_mod._dates
    photodates_mod._dates = SimpleNamespace(hold=None, start=lambda files: started.append(files) or True)
    try:
        prd = activities_mod.PhotosActivity({"folders": [str(tmp)]})
        ok = prd.resume_dates()
    finally:
        photos_rd.LOG_DIR = real_pd_dir
        photodates_mod._dates = real_dates
    check(ok and started == [["/y/IMG_20100101_000000.jpg", "/y/b.jpg"]],
          f"al arrancar el servicio, sigue con las fechas que falten de las listas guardadas: {started}")

    print("Datos de los archivos (Música y Videos)")
    from tele_cec.controller import menu as menu_ctl
    check([menu_ctl._contents_text(*a, ("tema", "temas")) for a in ((12, 0), (1, 3), (0, 2), (0, 1))]
          == ["12 temas", "1 tema y 3 subcarpetas", "en 2 subcarpetas", "en 1 subcarpeta"],
          "lo que tiene la carpeta, en vez de «con sus subcarpetas»")
    check([fileinfo_mod.quality(*wh) for wh in ((1920, 1080), (1620, 1080), (1280, 720), (3840, 1600), (640, 480))]
          == ["1080p", "1080p", "720p", "4K", "480p"], "la calidad del video (aunque esté recortado)")
    check(fileinfo_mod.video_text({"dur": 6533.2, "w": 1620, "h": 1080}) == ["1 h 49 min", "1080p"]
          and fileinfo_mod.duration_text(225.4) == "3:45", "duraciones")
    vdir = tmp / "vinfo"
    vdir.mkdir()
    (vdir / "rip.2019.1080p.mkv").write_bytes(b"x" * (2 << 20))
    probes: list = []
    orig_probe = fileinfo_mod._ffprobe

    def fake_probe(path):
        probes.append(path)
        return {"dur": 5400.0, "w": 1920, "h": 1080, "title": "La película", "subs": 2}
    fileinfo_mod._ffprobe = fake_probe
    try:
        vi_items = menu_ctl._file_items(vdir, activities_mod.VideosActivity({}))
        check([(i["name"], i["sub"]) for i in vi_items] == [("La película", "1 h 30 min · 1080p · 2 MB · con subtítulos")],
              f"el video con su título, duración, calidad, tamaño y subtítulos de adentro: {vi_items}")
        menu_ctl._file_items(vdir, activities_mod.VideosActivity({}))
        check(len(probes) == 1 and json.loads((tmp / "videos-info.json").read_text())["files"],
              f"lo leído se guarda: la segunda vez no se vuelve a leer: {probes}")

        print("Seguir viendo (Videos)")
        peli = str(vdir / "rip.2019.1080p.mkv")
        wdir = resume_mod.WATCH_DIR
        wdir.mkdir(parents=True)
        (wdir / "AAA").write_text(f"# {peli}\nstart=4000.500000\n")      # como lo escribe mpv al cerrarlo
        (wdir / "BBB").write_text("# redirect entry\n# /una/carpeta\n")  # lo que deja de las carpetas
        vargs = activities_mod.VideosActivity({}).extra_args()
        check("--save-position-on-quit" in vargs and f"--watch-later-dir={wdir}" in vargs
              and "--write-filename-in-watch-later-config" in vargs
              and not any("watch-later" in a for a in activities_mod.VideosActivity({"resume": False}).extra_args()),
              f"mpv guarda dónde quedó al cerrarlo (salvo resume = false): {vargs}")
        check("--sub-scale=1.15" in vargs
              and "--sub-scale=1.3" in activities_mod.VideosActivity({"sub_scale": 1.3}).extra_args()
              and "--sub-scale=1.15" in activities_mod.VideosActivity({"sub_scale": "grande"}).extra_args(),
              f"subtítulos un poco más grandes (sub_scale; uno que no es número queda en 1.15): {vargs}")
        vi_items = menu_ctl._file_items(vdir, activities_mod.VideosActivity({}))
        check(vi_items[0]["sub"].startswith("quedó en 1:06:40 · 1 h 30 min") and vi_items[0]["resume"] == 4000.5
              and not (wdir / "BBB").exists(),
              f"en la lista dice dónde quedó (y se limpian las entradas de carpetas): {vi_items[0]}")
        real_videos = ctl.activities["videos"]
        ctl.activities["videos"] = activities_mod.VideosActivity({"folders": [str(vdir)]})
        switched: list = []

        async def fake_switch(item):
            switched.append(item)
            ctl._switching = False
        orig_switch = ctl._menu_switch
        ctl._menu_switch = fake_switch  # type: ignore[method-assign]
        try:
            _, its = ctl._picker_entries("videos", vdir)
            check(its[0]["name"] == "Seguir viendo: La película" and its[0]["sub"] == "quedó en 1:06:40 de 1:30:00"
                  and its[0].get("direct") and its[1]["kind"] == "file",
                  f"arriba de todo, «Seguir viendo» con el último que quedó a la mitad: {[i['name'] for i in its]}")
            ctl._menu_open, ctl._menu_mode, ctl._picker_act, ctl._picker_path = True, "folders", "videos", vdir
            ctl._menu_items, ctl._menu_sel, ctl._picker_stack = its, 1, []
            await ctl._picker_choose(1)
            check([i["name"] for i in ctl.overlay.items] == ["▶ Seguir desde 1:06:40", "Desde el principio"]
                  and not switched, f"elegir el video pregunta: seguir o desde el principio: {ctl.overlay.items}")
            await ctl._picker_back()
            check(ctl._menu_items[0]["name"].startswith("Seguir viendo") and ctl._menu_sel == 1,
                  "◀ vuelve a la carpeta, en el mismo video")
            await ctl._picker_choose(1)
            await ctl._picker_choose(1)  # «Desde el principio»
            await ctl._macro_task
            check(switched and switched[-1]["folder"] == peli and not (wdir / "AAA").exists(),
                  f"desde el principio: se borra dónde quedó y se abre (mpv empieza de cero): {switched}")
            (wdir / "AAA").write_text(f"# {peli}\nstart=4000.5\n")
            ctl._menu_items, ctl._menu_sel, ctl._picker_stack = its, 0, []
            await ctl._picker_choose(0)  # la fila «Seguir viendo»: sin preguntar
            await ctl._macro_task
            check(switched[-1]["folder"] == peli and (wdir / "AAA").exists() and len(switched) == 2,
                  "«Seguir viendo» abre directo (mpv sigue desde ahí)")
            (wdir / "AAA").write_text(f"# {peli}\nstart=5300\n")
            _, its = ctl._picker_entries("videos", vdir)
            check(not its[0]["name"].startswith("Seguir") and "quedó" not in its[0]["sub"],
                  f"si quedó en los créditos, como si se hubiera terminado: {[i['name'] for i in its]}")
            (wdir / "AAA").write_text(f"# {peli}\nstart=30\n")
            check(not resume_mod.positions(), "menos de un minuto visto: como si no se hubiera empezado")
        finally:
            ctl.activities["videos"] = real_videos
            ctl._menu_switch = orig_switch  # type: ignore[method-assign]
            ctl._menu_open, ctl._menu_mode, ctl._picker_stack = False, "activities", []
    finally:
        fileinfo_mod._ffprobe = orig_probe

    print("Fotos: los modos del selector según lo que haya en la carpeta")
    if shutil.which("mpv"):
        fdirs = {k: tmp / "fmodos" / k for k in ("vacia", "solo-video", "fotos")}
        for d in fdirs.values():
            d.mkdir(parents=True)
        (fdirs["solo-video"] / "v.mp4").write_bytes(b"")
        (fdirs["fotos"] / "a.jpg").write_bytes(b"")
        for k, want in (("vacia", [False, False, False]), ("solo-video", [False, True, True]),
                        ("fotos", [True, True, False])):
            _, its = ctl._picker_entries("fotos", fdirs[k])
            got = [it["enabled"] for it in its if it["kind"] == "play"]
            check(got == want, f"{k}: Ver las fotos / Fotos y videos / Solo los videos habilitadas = {got}")
        _, its = ctl._picker_entries("fotos", fdirs["vacia"])
        check(its[1]["sub"] == "No hay fotos ni videos en esta carpeta", f"y dice por qué: {its[1]['sub']}")
        (fdirs["fotos"] / "2016").mkdir()
        orig_counts = fotos.counts
        fotos.counts = lambda folder: (49403, 2016)  # type: ignore[method-assign]  # ya recorrida
        try:
            _, its = ctl._picker_entries("fotos", fdirs["fotos"])
        finally:
            fotos.counts = orig_counts  # type: ignore[method-assign]
        subs = [it["sub"] for it in its if it["kind"] == "play"]
        check(subs == ["49.403 fotos · 1 subcarpeta · al azar", "49.403 fotos y 2.016 videos, al azar",
                       "2.016 videos · al azar"],
              f"ya recorrida, con subcarpetas: cuántas fotos y videos hay (no «(2016)», que parece un año), al azar: {subs}")
        _, its = ctl._picker_entries("fotos", fdirs["fotos"])
        check(its[1]["sub"] == "las fotos y los videos, al azar" and its[0]["sub"] == "1 foto y 1 subcarpeta · al azar",
              f"sin recorrer todavía: {[it['sub'] for it in its[:2]]}")
        _, its = ctl._picker_entries("fotos", fdirs["solo-video"])
        check(its[1]["sub"] == "las fotos y los videos, intercalados por fecha",
              f"sin subcarpetas: en orden (intercalados por fecha): {its[1]['sub']}")

    print("app:<actividad> (para ponerla en un color)")
    await ctl.run_action("app:tidal")
    start = len(log)
    await ctl.run_action("app:videos")
    check(opened(log[start:]) == ["music:quit", "videos:open"], f"app:videos cambia directo: {opened(log[start:])}")

    print("Standby de la TV")
    async def by_tv() -> str:  # el sonido sale por la TV (sin preguntarle a la PipeWire de esta compu)
        return ""
    async def by_bt() -> str:
        return "bluetooth"
    ctl._sound_off_tv = by_tv
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
    ctl._sound_off_tv = by_bt
    log.clear()
    await ctl._pause_if_playing()
    check(log == [], f"con el sonido por Bluetooth, la música sigue aunque se apague la TV: {log}")
    log.clear()
    await ctl._pause_if_playing(everything=True)
    check(log == ["mpris:pause"], f"pero al entrar en reposo se pausa igual: {log}")
    ctl._sound_off_tv = by_tv
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
    out = await press("DOT")
    check(out == ["ov:help:Ayuda · Tidal · controles (después de 0)"],
          f"el punto (•) también: muchas TVs no mandan Info por CEC: {out}")
    await press("EXIT")

    print("YouTube")
    from tele_cec.controller import web as ctl_web
    from tele_cec.ui import help as help_mod_nav
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
    check(out == ["wtype:Down", "wtype:Right", "wtype:Return", "wtype:Escape", "firefox:play-pause", "firefox:seek+60",
                  "ov:toast:Para ir al inicio con el azul, cerrá YouTube y abrilo de nuevo"],
          f"flechas, OK y Atrás van tal cual a YouTube; ⏯ y ⏩ (1 min) por MPRIS; azul = inicio (sin canal: avisa): {out}")
    out = await press("CHANNEL_UP", "CHANNEL_DOWN", "STOP")
    check(out == ["firefox:next", "firefox:previous", "wtype:Escape"],
          f"Canal ± = video siguiente / anterior (sin canal de control, por MPRIS); ■ = salir del video: {out}")
    rows = dict((d, k) for k, d in help_rows(ctl, "youtube"))
    check("Elegir / OK" in rows and "Volver" in rows, f"la ayuda tiene página de YouTube: {list(rows)[:5]}")
    await ctl.run_action("app:tidal")
    check(not yt.on, "abrir otra app cierra YouTube")
    del ctl.activities["youtube"]

    print("Navegador")
    from tele_cec.apps import navegador as nav_mod
    from tele_cec.apps.navegador import favicons as fav_mod
    real_fav = (fav_mod.LOG_DIR, fav_mod.ensure, fav_mod._fetch)
    fav_mod.LOG_DIR = tmp / "cache-fav"
    fav_mod.ensure = lambda urls: None  # type: ignore[assignment]  (sin salir a internet en las pruebas)
    from tele_cec.apps.navegador.t9 import T9
    t9 = T9(1000)
    seq = [t9.press(k, now=1.0 + i * 0.2) for i, k in enumerate(("2", "2", "2", "6", "0"))]
    check([(c, r) for c, r, _l, _i in seq] == [("a", False), ("b", True), ("c", True), ("m", False), (" ", False)]
          and seq[1][2] == "abc2" and seq[1][3] == 1,
          f"T9: el mismo número seguido pasa a la letra siguiente (reemplaza); otro número, letra nueva; 0 espacio: {seq}")
    check(t9.press("2", now=1.9)[0] == "a" and t9.press("2", now=5.0)[0] == "a",
          "después de un rato (t9_ms) la misma tecla empieza de nuevo")
    check(t9.next_mode() == "ABC" and t9.press("3", now=6.0)[0] == "D" and t9.next_mode() == "123"
          and t9.press("3", now=6.1) == ("3", False, "", 0) and t9.next_mode() == "abc", "modos: abc → ABC → 123 → abc")
    check(t9.press("x") == ("", False, "", 0) and t9.press("1", now=9.0)[0] == ".", "una tecla que no es número: nada; 1 = signos")
    check(nav_mod.target_url("clarin.com") == "https://clarin.com" and nav_mod.target_url("http://x.y/z?a=1") == "http://x.y/z?a=1"
          and nav_mod.target_url("clima mañana") == "https://www.google.com/search?q=clima+ma%C3%B1ana"
          and nav_mod.target_url("hola", "https://duckduckgo.com/?q={q}") == "https://duckduckgo.com/?q=hola"
          and nav_mod.target_url("  ") == "",
          "«Ir a»: una dirección (con o sin https://) o una búsqueda en el buscador configurado")
    check(nav_mod.allowed_url("https://es.wikipedia.org/wiki/Pi") and not nav_mod.allowed_url("file:///etc/passwd")
          and not nav_mod.allowed_url("javascript:alert(1)") and not nav_mod.allowed_url("about:config"),
          "desde el celular solo páginas web (http, https)")
    bms = nav_mod.bookmarks_from({"bookmarks": ["infobae.com", {"name": "Wiki", "url": "https://es.wikipedia.org"}, {"name": "sin"}]})
    check(bms == [{"name": "infobae.com", "url": "https://infobae.com"}, {"name": "Wiki", "url": "https://es.wikipedia.org"}]
          and [b["name"] for b in nav_mod.bookmarks_from({})] == ["Google", "DuckDuckGo", "Wikipedia"],
          f"los sitios: una dirección sola sirve; sin url se saltea; sin la lista, unos de muestra: {bms}")
    wa = nav_mod.WebActivity({"profile": str(tmp / "nav-perfil"), "bookmarks": [{"name": "Wiki", "url": "https://es.wikipedia.org"}]},
                             {"engine": "firefox", "reduce_effects": True})
    wargs = wa.browser.args(wa.home_url())
    check(wa.browser.port == 9223 and wa.browser.unit == "navegador-firefox" and "--kiosk" in wargs
          and "--no-remote" in wargs and str(tmp / "nav-perfil") in wargs and "9223" in wargs,
          f"Firefox aparte: perfil propio, puerto de control 9223 (no el 9222 de Netflix), scope propio: {wargs}")
    wprefs = wa.browser.firefox_prefs()
    check(wprefs.get("layout.css.devPixelsPerPx") == "1.5" and wprefs.get("browser.link.open_newwindow") == 1
          and wprefs.get("ui.prefersReducedMotion") == 1 and wprefs.get("app.normandy.enabled") is False
          and wprefs.get("extensions.autoDisableScopes") == 0 and wprefs.get("permissions.default.geo") == 2,
          "sus preferencias: todo al 150 %, sin ventanas nuevas, sin animaciones ni trabajo de fondo (como Netflix), "
          "extensiones copiadas habilitadas, sin preguntar por ubicación")
    check("layout.css.devPixelsPerPx" not in nav_mod.WebActivity({"scale": 1.0}, {}).prefs(), "scale = 1.0: como una PC")
    icons_dir = Path(nav_mod.__file__).resolve().parent.parent.parent / "ui" / "icons"
    check(nav_mod.WebActivity({}, {}).icon == "firefox.svg" and nav_mod.WebActivity({}, {"engine": "chromium"}).icon
          == "chromium.svg" and nav_mod.WebActivity({"icon": "x.svg"}, {"engine": "chromium"}).icon == "x.svg"
          and (icons_dir / "firefox.svg").is_file() and (icons_dir / "chromium.svg").is_file(),
          "el ícono del Navegador es el del navegador configurado (Firefox o Chromium)")
    page_html = wa.start_page_html()
    check(wa.home_url() == (tmp / "nav-perfil" / "inicio.html").as_uri() and (tmp / "nav-perfil" / "inicio.html").is_file()
          and 'href="https://es.wikipedia.org"' in page_html and "<small>1</small>" in page_html and "0 = escribir" in page_html,
          "la página de inicio: un archivo propio con los sitios en cuadros grandes y el número de cada uno")
    check(nav_mod.WebActivity({"home": "https://www.google.com"}, {}).home_url() == "https://www.google.com"
          and nav_mod.WebActivity({}, {"engine": "chromium"}).profile.name == "chromium-navegador"
          and any(a.startswith("--force-device-scale-factor=1.5") for a in
                  nav_mod.WebActivity({"profile": str(tmp / "nav-ch")}, {"engine": "chromium"}).browser.args("https://x")),
          "home propia; con Chromium, perfil chromium-navegador y la escala como flag")
    check(wa.source() == ("Wiki", True) and "window.__tcnav" in wa.script and wa.version in wa.script, "en el menú, los sitios; el script con su versión")

    # el ícono de cada sitio (favicon)
    import io as io_mod
    from PIL import Image as PILImage

    def png_bytes(w, h, color=(255, 0, 0, 255), fmt="PNG"):
        b = io_mod.BytesIO()
        PILImage.new("RGBA", (w, h), color).save(b, fmt)
        return b.getvalue()
    page_links = ('<link rel="icon" href="/a.svg"><link rel="shortcut icon" href="/f.ico">'
                  '<link rel="apple-touch-icon" sizes="180x180" href="t.png"><link rel="mask-icon" href="/m.png">'
                  '<meta itemprop="image" content="/logo128.png">')
    check(fav_mod.icon_links(page_links, "https://x.example/p/") == ["https://x.example/p/t.png", "https://x.example/f.ico"]
          and fav_mod.meta_images(page_links, "https://x.example/") == ["https://x.example/logo128.png"],
          "los íconos que declara la página: el más grande primero, sin SVG ni mask-icon; y el logo de <meta itemprop>")
    small, side = fav_mod.to_png(png_bytes(32, 32))
    check(side == 32 and PILImage.open(io_mod.BytesIO(small)).size == (128, 128) and fav_mod.to_png(b"no") is None
          and fav_mod.to_png(png_bytes(8, 8)) is None and fav_mod.to_png(png_bytes(300, 100), square=True) is None
          and fav_mod.to_png(png_bytes(64, 32))[1] == 64, "a PNG de 128 px (las chicas se agrandan); una imagen que no es "
          "cuadrada no vale como logo; una diminuta o algo que no es imagen, no")
    ico = io_mod.BytesIO()
    PILImage.new("RGBA", (48, 48), (0, 0, 255, 255)).save(ico, "ICO", sizes=[(16, 16), (48, 48)])
    check(fav_mod.to_png(ico.getvalue())[1] == 48, "de un .ico, el tamaño más grande")
    fetched_fav: list = []
    site_files = {"https://sitio.example/": (page_links.encode(), "https://sitio.example/"),
                  "https://sitio.example/p/t.png": (png_bytes(40, 40), ""), "https://sitio.example/f.ico": (png_bytes(32, 32), ""),
                  "https://sitio.example/apple-touch-icon.png": (png_bytes(120, 120), "")}

    def fake_fav_fetch(url):
        fetched_fav.append(url)
        if url not in site_files:
            raise OSError("404")
        return site_files[url]
    fav_mod._fetch = fake_fav_fetch  # type: ignore[assignment]
    got_fav = fav_mod.fetch("https://sitio.example/algo")
    check(got_fav == tmp / "cache-fav" / "favicons" / "sitio.example.png" and fav_mod.path_for("sitio.example") == got_fav
          and fetched_fav[-1] == "https://sitio.example/apple-touch-icon.png",
          f"se queda con el más grande (y deja de buscar al encontrar uno de 96 px o más): {fetched_fav}")
    fetched_fav.clear()
    check(fav_mod.fetch("https://sitio.example/") == got_fav and not fetched_fav, "la segunda vez, de la caché")
    check(fav_mod.fetch("https://nada.example/") is None and fav_mod._failed_recently("nada.example")
          and fav_mod.fetch("https://nada.example/") is None and fetched_fav.count("https://nada.example/") == 1,
          "sin ícono: se recuerda y no se vuelve a pedir enseguida")
    check(fav_mod.path_for("../etc") is None and fav_mod.host_of("javascript:x") == "", "solo nombres de sitio")
    wfav = nav_mod.WebActivity({"profile": str(tmp / "nav-perfil"), "bookmarks": [{"name": "S", "url": "https://sitio.example/x"}]}, {})
    check(wfav.favicon("https://sitio.example/x") == got_fav and got_fav.as_uri() in wfav.start_page_html(),
          "en la página de inicio, cada sitio con su ícono")
    # uBlock: el del Firefox de siempre
    real_profiles = nav_mod.USER_PROFILES
    (tmp / "ffuser" / "abc.default-release" / "extensions").mkdir(parents=True)
    (tmp / "ffuser" / "abc.default-release" / "extensions" / nav_mod.UBLOCK_XPI).write_bytes(b"xpi")
    nav_mod.USER_PROFILES = (str(tmp / "ffuser" / "*" / "extensions"),)
    try:
        wub = nav_mod.WebActivity({"profile": str(tmp / "nav-ub")}, {})
        check(wub.user_ublock() == tmp / "ffuser" / "abc.default-release" / "extensions" / nav_mod.UBLOCK_XPI,
              "encuentra el uBlock del Firefox de siempre")
        wub._copy_extensions()
        check((tmp / "nav-ub" / "extensions" / nav_mod.UBLOCK_XPI).read_bytes() == b"xpi"
              and wub.prefs().get("extensions.startupScanScopes") == 1, "y lo copia al perfil del Navegador (habilitado solo)")
        wno = nav_mod.WebActivity({"profile": str(tmp / "nav-noub"), "ublock": False}, {})
        wno._copy_extensions()
        check(not (tmp / "nav-noub" / "extensions").exists(), "ublock = false: no")
    finally:
        nav_mod.USER_PROFILES = real_profiles

    class FakeNavPage:
        """La página con nav.js simulada: anota los comandos y se comporta como un campo de texto."""

        def __init__(self, log):
            self.log, self.ctx, self.value, self.text_next, self.gobox = log, "page", "", False, False
            self.url, self.mxy = "https://x.example/p", None

        async def evaluate(self, expr, timeout=10.0):
            m = re.search(r'cmd\("([a-zA-Z]+)", (\[.*\])\)', expr)
            name, args = m.group(1), json.loads(m.group(2))
            self.log.append("web:" + name + ("" if not args else ":" + ":".join(map(str, args))))
            if name == "ctx":
                return {"ctx": self.ctx, "gobox": self.gobox, "url": self.url, "title": "Una página - El sitio"}
            if name == "mouse":
                if args[0] == "show":
                    self.mxy = (args[1] if args[1] is not None else 640, args[2] if args[2] is not None else 360)
                    return {"on": True, "x": self.mxy[0], "y": self.mxy[1]}
                if args[0] == "move":
                    self.mxy = (max(0, min(1279, self.mxy[0] + args[1])), max(0, min(719, self.mxy[1] + args[2])))
                    return {"x": self.mxy[0], "y": self.mxy[1]}
                if args[0] == "where":
                    return {"on": self.mxy is not None, "x": self.mxy[0], "y": self.mxy[1], "text": False} if self.mxy else {"on": False}
                if args[0] == "hide":
                    self.mxy = None
                    return {"on": False}
            if name == "activate":
                if self.text_next:
                    self.ctx, self.text_next = "text", False
                    return {"text": True, "label": "buscar"}
                return {"x": 10, "y": 20, "label": "Link"}
            if name == "type":
                self.value = (self.value[:-1] if args[1] else self.value) + args[0]
                return {"ok": True}
            if name == "submit":
                if self.gobox:
                    self.ctx, self.gobox, v = "page", False, self.value
                    return {"go": v}
                return {"enter": True}
            if name == "back":
                self.ctx, self.gobox = "page", False
                return {"done": getattr(self, "back_done", "blur")}
            if name == "go":
                self.ctx, self.gobox, self.value = "text", True, ""
                return {"ok": True}
            if name == "clear":
                self.value = ""
                return {"ok": True}
            if name == "playPause":
                return {"none": True}
            return {}

        async def navigate(self, url):
            self.log.append("web:navigate:" + url)

        async def key_press(self, name):
            self.log.append("web:key:" + name)

        async def mouse_click(self, x, y):
            self.log.append(f"web:click:{x},{y}")

        async def mouse_move(self, x, y):
            self.log.append(f"web:move:{x},{y}")

        async def reload(self):
            self.log.append("web:reload")

        async def bring_to_front(self):
            pass

        async def close(self):
            pass

    class FakeWeb(nav_mod.WebActivity):
        def __init__(self, log):
            super().__init__({"profile": str(tmp / "nav-perfil"), "bookmarks": [
                {"name": "Wiki", "url": "https://es.wikipedia.org"}, {"name": "Clarín", "url": "https://www.clarin.com"}]},
                {"engine": "firefox"})
            self.log, self.on, self.fake = log, False, FakeNavPage(log)

        def installed(self):
            return True

        def running(self):
            return self.on

        async def active(self):
            return self.on

        async def open(self, url=None):
            if url is None:
                url, self.want_url = self.want_url, None
            self.log.append("navegador:open" + (f":{url}" if url and url != self.home_url() else ""))
            self.on, self.page = True, self.fake
            return True

        async def close(self):
            self.log.append("navegador:close")
            self.on, self.page = False, None

        async def _ensure_page(self):
            return self.on

    wf = FakeWeb(log)
    ctl.activities["navegador"] = wf
    await ctl.run_action("app:navegador")
    check(ctl.current_app() == "navegador" and await ctl.context() == "navegador" and wf.page is wf.fake,
          "app:navegador abre el Navegador (contexto 'navegador')")
    out = await press("DOWN", "RIGHT", "SELECT", "EXIT", "F1_BLUE", "CHANNEL_DOWN", "NUMBER2", "PAUSE_PLAY_FUNCTION")
    check(out == ["web:ctx", "web:nav:down", "web:ctx", "web:nav:right", "web:ctx", "web:activate", "web:click:10,20",
                  "web:clicked", "web:ctx",
                  "web:back", "web:ctx", "web:navigate:" + wf.home_url(), "web:ctx", "web:scroll:down", "web:ctx",
                  "ov:toast:Clarín", "web:navigate:https://www.clarin.com", "web:ctx", "web:playPause:",
                  "ov:toast:No hay ningún video en esta página"],
          f"flechas = moverse, OK = clic de verdad (y pregunta si le llegó), Atrás, azul = inicio, Canal− = página "
          f"abajo, 2 = sitio 2, ⏯: {out}")
    closed_web = []
    real_close_web = ctl.a_close

    async def fake_close_web(ident: str) -> None:
        closed_web.append(ident)
    ctl.a_close = fake_close_web  # type: ignore[method-assign]
    wf.fake.back_done = "none"
    await press("EXIT")
    check(closed_web == ["navegador"], f"en la primera página (sin a dónde volver), Atrás cierra el Navegador: {closed_web}")
    del wf.fake.back_done
    ctl.a_close = real_close_web  # type: ignore[method-assign]

    async def hold(name: str, times: int, gap: float = 0.0) -> list[str]:
        """Un botón mantenido: la tecla repetida sin «soltado» en el medio."""
        start = len(log)
        for _ in range(times):
            await ctl.handle_key(parse_key(name))
            if gap:
                await asyncio.sleep(gap)
        return log[start:]
    out = await hold("DOWN", 3)
    ctl._released = True
    check(out.count("web:nav:down") == 3, f"▼ mantenido sigue bajando (web:nav se repite): {out}")
    out = await hold("SELECT", 3)
    ctl._released = True
    check(out.count("web:activate") == 1, f"OK mantenido abre una sola vez: {out}")
    wf.fake.text_next = True
    out = await press("SELECT")
    check(out[-2:] == ["web:activate", "ov:toast:" + ctl_web.TEXT_HINT] and await ctl.context() == "navegador_text",
          f"OK en un campo de texto: entra a escribir (contexto 'navegador_text'): {out}")
    out = await press("NUMBER2", "NUMBER2", "NUMBER6", "NUMBER0", "F2_RED", "NUMBER4")
    check(wf.fake.value == "bm g" and "web:type:a:False:abc2:0" in out and "web:type:b:True:abc2:1" in out
          and "web:key:Backspace" in out, f"los números escriben (T9), rojo borra: «{wf.fake.value}» {out}")
    out = await press("F4_YELLOW", "NUMBER2", "F3_GREEN")
    check("ov:toast:ABC · MAYÚSCULAS" in out and "web:type:A:False:ABC2:0" in out and "web:clear" in out and wf.fake.value == "",
          f"amarillo = mayúsculas, verde = borrar todo: {out}")
    wf.t9.next_mode(); wf.t9.next_mode()  # de vuelta a abc
    out = await press("SELECT", "EXIT")
    check(out[-3:] == ["web:submit", "web:key:Enter", "web:ctx"] or "web:key:Enter" in out, f"OK escribiendo = Enter: {out}")
    check(await ctl.context() == "navegador" and "web:back" in out, "Atrás sale del campo")
    out = await press("NUMBER0", "NUMBER3", "NUMBER3", "SELECT")
    check("web:go:" in out and not wf.fake.gobox and out[-1] == "web:navigate:https://www.google.com/search?q=e",
          f"0 abre «Ir a», se escribe con los números y OK busca (o abre la dirección): {out}")
    rows = dict((d, k) for k, d in help_rows(ctl, "navegador_text"))
    check(("navegador", "Navegador · recorriendo la página") in help_mod_nav.pages(ctl.cfg, {"navegador"})
          and not any(c == "navegador" for c, _t in help_mod_nav.pages(ctl.cfg, {"netflix"}))
          and rows.get("Escribir: cada número, sus letras (seguido = la siguiente)", "").startswith("0"),
          f"la ayuda: dos páginas del Navegador, con los números juntos: {list(rows)[:4]}")

    # el menú: el botoncito «Sitios», la lista, y el amarillo desde el Navegador
    items_before, cols_before = ctl.cfg.menu.get("items"), ctl._menu_columns
    ctl.cfg.menu["items"] = ["netflix", "navegador", "fotos"]
    ctl._menu_columns = 2
    try:
        await ctl.menu_open()
        row = next(i for i in ctl.overlay.items if i["id"] == "navegador")
        check(row.get("action") == "Sitios" and row["current"], f"en el menú, Navegador con el botoncito «Sitios»: {row}")
        (tmp / "cache-fav" / "favicons" / "es.wikipedia.org.png").write_bytes(png_bytes(128, 128))
        await ctl._menu_options(ctl.overlay.items.index(row))
        check(ctl._menu_mode == "sites" and [i["name"] for i in ctl.overlay.items]
              == ["Wiki", "Clarín", "Guardar esta página", "Escribir una dirección o buscar"]
              and ctl.overlay.items[2]["sub"] == "Una página - El sitio" and ctl._menu_sel == 0,
              f"la lista de sitios (con «Guardar esta página», que no está guardada): {[i['name'] for i in ctl.overlay.items]}")
        check(ctl.overlay.items[0]["icon"] == str(tmp / "cache-fav" / "favicons" / "es.wikipedia.org.png")
              and ctl.overlay.items[1]["icon"] == "navegador.svg",
              f"cada sitio con su ícono (si ya se bajó; si no, el genérico): {[i['icon'] for i in ctl.overlay.items]}")
        start = len(log)
        await ctl.a_menu("pick:2")
        check(log[start:] == ["ov:select:1", "web:navigate:https://www.clarin.com", "ov:hide"] and not ctl._menu_open,
              f"elegir uno lo abre y cierra el menú: {log[start:]}")
        await ctl.a_web("sites")
        check(ctl._menu_open and ctl._menu_mode == "sites" and ctl._sites_back_closes, "amarillo en el Navegador: la lista encima")
        out = await press("EXIT")
        check(out == ["ov:hide"] and not ctl._menu_open, f"Atrás vuelve a la página: {out}")
        await ctl.a_web("sites")
        check(ctl._menu_sel == 2, "desde el Navegador, con una página sin guardar: «Guardar esta página» ya resaltado")
        await ctl.a_menu("pick:4")
        check(not ctl._menu_open and log[-1] == "web:go:", f"«Escribir una dirección» abre el cuadro «Ir a»: {log[-3:]}")

        # guardar la página que se ve y borrar sitios (rojo dos veces)
        sites_file = tmp / "datos" / "navegador-sitios.json"
        wf.fake.ctx, wf.fake.gobox = "page", False  # el cuadro «Ir a», cerrado
        check(not sites_file.exists(), "sin cambios desde la TV, los sitios son los de la configuración (sin archivo)")
        await ctl.a_web("sites")
        await press("SELECT")
        check([b["name"] for b in wf.bookmarks] == ["Wiki", "Clarín", "Una página - El sitio"] and ctl._menu_open
              and ctl._menu_sel == 2 and "Guardar esta página" not in [i["name"] for i in ctl.overlay.items]
              and json.loads(sites_file.read_text("utf-8"))[2] == {"name": "Una página - El sitio", "url": "https://x.example/p"},
              f"OK en «Guardar esta página»: queda al final (con el título corto) y se guarda en el archivo: {wf.bookmarks}")
        check(nav_mod.site_name("Raspberry Pi 400 - Wikipedia, la enciclopedia libre", "https://es.wikipedia.org/x")
              == "Raspberry Pi 400" and nav_mod.site_name("", "https://www.clarin.com/") == "clarin.com",
              "el nombre de un sitio nuevo: el título (sin la cola larga) o el nombre del sitio")
        check("Una página" in wf.start_page_html() and ctl.overlay.items[2]["current"], "y aparece en la página de inicio")
        await press("EXIT")
        out = await press("NUMBER3")
        check("web:navigate:https://x.example/p" in out, f"3 lo abre desde el Navegador: {out}")
        await ctl.a_web("save")
        check(len(wf.bookmarks) == 3 and log[-1].startswith("ov:toast:Ya estaba guardado"), f"web:save otra vez: ya estaba: {log[-1]}")
        await ctl.a_web("sites")
        await press("UP", "F2_RED")  # abre en la página que se ve (la 3.ª): ▲ = Clarín
        check(len(wf.bookmarks) == 3 and ctl._sites_delete is not None and ctl._sites_delete[0] == 1,
              "rojo una vez sobre un sitio: pregunta (no lo borra)")
        await press("DOWN", "F2_RED")
        check(len(wf.bookmarks) == 3 and ctl._sites_delete == (2, ctl._sites_delete[1]),
              "otra tecla cancela; rojo sobre otro sitio vuelve a preguntar")
        await press("F2_RED")
        check([b["name"] for b in wf.bookmarks] == ["Wiki", "Clarín"] and ctl._menu_open and ctl._menu_mode == "sites"
              and [i["name"] for i in ctl.overlay.items][:3] == ["Wiki", "Clarín", "Guardar esta página"]
              and len(json.loads(sites_file.read_text("utf-8"))) == 2,
              f"rojo otra vez: lo borra (y la lista se rearma): {[i['name'] for i in ctl.overlay.items]}")
        await press("UP", "UP", "F2_RED", "F2_RED")
        check([b["name"] for b in wf.bookmarks] == ["Clarín"], f"y se puede borrar cualquiera: {wf.bookmarks}")
        wf2 = nav_mod.WebActivity({"profile": str(tmp / "nav-perfil"), "bookmarks": [{"name": "Otro", "url": "https://otro.example"}]}, {})
        check([b["name"] for b in wf2.bookmarks] == ["Clarín"], "el archivo manda sobre [apps.navegador] bookmarks (al reabrir)")
        await press("EXIT")

        # modo mouse (CC)
        start = len(log)
        out = await press("SUB_PICTURE")
        check(wf.mouse and wf.mouse_xy == (640, 360) and await ctl.context() == "navegador_mouse"
              and any(x.startswith("ov:toast:Modo mouse") for x in out), f"CC prende el modo mouse: {out}")
        nav_mod.MOUSE_REPEAT_S = 10.0
        out = await press("RIGHT", "RIGHT", "DOWN")
        check(wf.mouse_xy == (640 + 8 + 8 * 1.3, 360 + 8) and "web:move:640.0,368.0" not in out
              and f"web:move:{640 + 8 + 8 * 1.3},368.0" in out,
              f"las flechas mueven la flecha (apretando seguido, más rápido) y la mueven de verdad (hover): {wf.mouse_xy} {out}")
        out = await press("SELECT")
        check(f"web:click:{640 + 8 + 8 * 1.3},368" in out, f"OK = clic de verdad donde está la flecha: {out}")
        xy0 = wf.mouse_xy
        out = await hold("LEFT", 6, 0.1)  # mantenido ~0,6 s: se desliza sola
        check(ctl._web_glide is not None and not ctl._web_glide.done() and len(out) > 0
              and wf.mouse_xy[0] < xy0[0] - 40, f"◀ mantenido: la flecha se desliza sola y acelera: {xy0} → {wf.mouse_xy}")
        ctl._released = True  # se soltó
        await asyncio.sleep(0.15)
        x_stop = wf.mouse_xy
        await asyncio.sleep(0.15)
        check(ctl._web_glide.done() and wf.mouse_xy == x_stop and log[-1].startswith("web:move:"),
              f"al soltar se frena (y el puntero de verdad va ahí): {log[-2:]}")
        check(not ctl._release_works, "sin avisos de «soltado» (0x45): un toque nunca se desliza solo")

        # Una tele que no repite la tecla mantenida pero sí avisa al soltarla.
        async def tap(name: str, release_after: float | None) -> None:
            await ctl.handle_key(parse_key(name))
            if release_after is not None:
                await asyncio.sleep(release_after)
                ctl._released = True
                ctl._releases += 1
        await tap("RIGHT", 0.05)
        await asyncio.sleep(0.2)
        nav_mod.MOUSE_REPEAT_S = 0.0  # cada toque, un paso chico
        xy0 = wf.mouse_xy
        await tap("RIGHT", 0.05)
        await asyncio.sleep(0.6)
        check(ctl._release_works and wf.mouse_xy[0] == xy0[0] + nav_mod.MOUSE_STEP,
              f"esa tele, un toque (soltado enseguida): un paso, sin deslizarse: {xy0} → {wf.mouse_xy}")
        xy0 = wf.mouse_xy
        await tap("LEFT", None)  # apretado y sin soltar
        await asyncio.sleep(0.8)
        check(ctl._web_glide is not None and not ctl._web_glide.done() and wf.mouse_xy[0] < xy0[0] - 30,
              f"esa tele, apretado sin soltar: pasado un ratito se desliza: {xy0} → {wf.mouse_xy}")
        ctl._released = True
        ctl._releases += 1
        await asyncio.sleep(0.15)
        x_stop = wf.mouse_xy
        await asyncio.sleep(0.15)
        check(ctl._web_glide.done() and wf.mouse_xy == x_stop, "y al soltarlo se frena")
        nav_mod.MOUSE_REPEAT_S = 10.0
        wf.fake.mxy = wf.mouse_xy = (678.4, 384.0)  # de vuelta, para lo que sigue
        wf.fake.ctx = "text"  # un campo con el foco (Google lo enfoca solo al abrir)
        out = await press("RIGHT", "NUMBER2", "SELECT")
        check(any(x.startswith("web:mouse:move:") for x in out) and "web:type:a:False:abc2:0" in out
              and "web:submit" in out and not any(x.startswith("web:click") for x in out),
              f"escribiendo con el modo mouse: ◀ ▶ mueven la flecha, los números escriben, OK (sin mover) busca: {out}")
        out = await press("LEFT", "SELECT")
        check(any(x.startswith("web:click:") for x in out) and "web:submit" not in out,
              f"y si la flecha se movió después de escribir, OK es un clic: {out}")
        wf.fake.ctx = "page"
        out = await press("NUMBER1")
        check("web:ctx:{'x': 678.4, 'y': 384.0}" in out or any(x.startswith("web:ctx:") for x in out),
              f"con cada tecla la página se entera de dónde va la flecha (por si cambió de página): {out}")
        out = await press("SUB_PICTURE")
        check(not wf.mouse and "web:mouse:hide" in out and await ctl.context() == "navegador", f"CC otra vez: lo apaga: {out}")
        nav_mod.MOUSE_REPEAT_S = 0.6
        await ctl.run_action("app:tidal")
        check(not wf.on and "navegador:close" in log[-6:], "abrir otra app cierra el Navegador")
        switched_w: list = []

        async def fake_switch_w(item):
            switched_w.append(item)
            wf.on = True  # como si abriera
        real_menu_switch_w = ctl._menu_switch
        ctl._menu_switch = fake_switch_w  # type: ignore[method-assign]
        try:
            await ctl.menu_open(select_id="navegador")
            await ctl._menu_options(ctl._menu_sel)
            check([i["name"] for i in ctl.overlay.items] == ["Clarín"], "cerrado: la lista sin «Guardar» ni «Escribir una dirección»")
            await ctl.a_menu("pick:1")
            await ctl._macro_task
            check(switched_w and switched_w[0]["id"] == "navegador" and wf.want_url == "https://www.clarin.com",
                  f"cerrado: elegir un sitio abre el Navegador en él: {switched_w} {wf.want_url}")
        finally:
            ctl._menu_switch = real_menu_switch_w  # type: ignore[method-assign]
            ctl._switching = False
            wf.want_url, wf.on = None, False
    finally:
        ctl.cfg.menu["items"], ctl._menu_columns = items_before, cols_before
        ctl._menu_open = False
        del ctl.activities["navegador"]
        fav_mod.LOG_DIR, fav_mod.ensure, fav_mod._fetch = real_fav
    await ctl.run_action("app:tidal")

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
    yfx_prefs = activities_mod.YouTubeActivity({}, {}).prefs()
    check(yfx_prefs.get("ui.prefersReducedMotion") == 1 and yfx_prefs.get("accessibility.force_disabled") == 1
          and "--force-prefers-reduced-motion" in yargs,
          "YouTube también va sin animaciones (browser.reduce_effects, como Netflix)")
    yfx_plain = activities_mod.YouTubeActivity({}, {"reduce_effects": False}).prefs()
    check("ui.prefersReducedMotion" not in yfx_plain and yfx_plain.get("browser.newtab.preload") is False,
          "con reduce_effects = false vuelven las animaciones, pero sigue sin el trabajo en segundo plano")
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

    print("YouTube: CC = subtítulos (por el canal de control de la página)")
    yfx = activities_mod.YouTubeActivity({}, {})
    fargs = yfx.args("/usr/bin/firefox")
    check(fargs[fargs.index("--remote-debugging-port") + 1] == "9224" and "--remote-debugging-port=9224" in yargs
          and "--remote-debugging-port" not in activities_mod.YouTubeActivity({"debug_port": 0}, {}).args("/f"),
          f"abre con su canal de control (9224, otro que Netflix y el Navegador; 0 lo apaga): {fargs}")
    yt_answers = [json.dumps({"on": True, "name": "Español"}), json.dumps({"on": False}),
                  json.dumps({"error": "none"}), json.dumps({"error": "novideo"})]
    yt_exprs: list = []

    async def fake_yt_js(expression, timeout=8.0):
        yt_exprs.append(expression)
        return yt_answers.pop(0)
    yfx.js = fake_yt_js  # type: ignore[method-assign]
    texts = [await yfx.toggle_subtitles() for _ in range(4)]
    check(texts == ["Subtítulos: Español", "Sin subtítulos", "Este video no tiene subtítulos",
                    "Los subtítulos, con un video puesto"] and "toggleSubtitlesOn" in yt_exprs[0] and "setOption('captions', 'track', {})" in yt_exprs[0],
          f"prende o apaga con toggleSubtitlesOn (y apaga con setOption) y dice cuál quedó: {texts}")
    yold = activities_mod.YouTubeActivity({"debug_port": 0}, {})
    check(await yold.toggle_subtitles() == "Para los subtítulos con CC, cerrá YouTube y abrilo de nuevo",
          "un YouTube abierto sin el canal: avisa que hay que reabrirlo")

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
    out = await press("EXIT", "F1_BLUE", "F2_RED", "PAUSE_PLAY_FUNCTION", "FORWARD", "FAST_FORWARD", "STOP",
                      "CHANNEL_UP", "CHANNEL_DOWN")
    check(out == ["adb:back", "adb:home", "adb:apps", "adb:play_pause", "adb:next", "adb:fast_forward", "adb:stop",
                  "adb:next", "adb:previous"],
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
    andr.net_devices = [android_mod.Device("XY123", "unauthorized", "moto_e7_plus")]
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
    andr.net_devices = [android_mod.Device("XY123", "device", "moto_e7_plus")]  # se tocó «Permitir»
    await wait_for(lambda: ctl._macro_task is not None or "android:open:XY123" in log[start:])
    check("android:open:XY123" in log[start:] and ctl.current_app() == "android" and not ctl._menu_open,
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
    andr.net_devices = [android_mod.Device("XY123", "device", "moto_e7_plus")]
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
        mk("1-1.1", manufacturer="motorola", product="moto e(7) plus", serial="XY12345678")
        mk("1-1.1:1.0", bInterfaceClass="ff", bInterfaceSubClass="ff", bInterfaceProtocol="00", interface="MTP")
        mk("1-1.1:1.1", bInterfaceClass="ff", bInterfaceSubClass="42", bInterfaceProtocol="01", interface="ADB Interface")
        mk("1-1.2", manufacturer="Samsung", product="Galaxy", serial="R5CR")
        mk("1-1.2:1.0", bInterfaceClass="06", bInterfaceSubClass="01", bInterfaceProtocol="01")
        mk("1-1.3", product="Raspberry Pi Internal Keyboard")
        mk("1-1.3:1.0", bInterfaceClass="03", bInterfaceSubClass="01", bInterfaceProtocol="01")
        found = android_mod.usb_phones(sysfs)
    check([(p.name, p.serial, p.adb) for p in found]
          == [("motorola moto e(7) plus", "XY12345678", True), ("Samsung Galaxy", "R5CR", False)],
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
    fake_usb.append(android_mod.UsbPhone("motorola moto e(7) plus", "XY12345678", False))
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
    check("ov:toast:Cancelado: sigue andando" in log[start:]
          and all(x.startswith("ov:toast") for x in log[start:]),
          f"y avisa; esa tecla no le llega a la app (un OK no pausa la película): {log[start:]}")
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
    await ctl.switch_to("fotos", tmp / "pick" / "media" / "usuario" / "USB")
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
    await ctl.switch_to("fotos", tmp / "pick" / "media" / "usuario" / "USB")
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
    await ctl.switch_to("fotos", tmp / "pick" / "media" / "usuario" / "USB")
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
        out = await press("SELECT", "RIGHT", "FORWARD", "F2_RED", "F3_GREEN", "F1_BLUE")
        check(out[:5] == ["mpv:ok", "mpv:seek+10", "mpv:next", "mpv:shuffle", "ov:toast:Mezclado"] and out[5] == "mpv:info",
              f"en Música: OK, ▶ (buscar), ⏭ (tema siguiente), rojo (mezclar, como en Tidal), azul (qué suena); verde nada: {out}")
        out = await press("F4_YELLOW", "F4_YELLOW", "F4_YELLOW")
        check(out == ["mpv:loop", "ov:toast:Repetir toda la lista", "mpv:loop", "ov:toast:Repetir este tema",
                      "mpv:loop", "ov:toast:Sin repetir"], f"amarillo: repetir la lista, el tema, nada: {out}")
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
        out = await press("STOP")
        check(not fotos.on and musica.on and ctl._menu_open and "ov:busy:Cerrando Fotos…" in out,
              f"■ cierra Fotos (la música de fondo sigue) y muestra el menú: {out}")
        await press("EXIT")
        check(await ctl.context() == "musica", "Atrás cierra el menú y queda la Música")
        out = await press("STOP")
        check(not musica.on and ctl._menu_open and ctl._foreground is None and "ov:busy:Cerrando Música…" in out,
              f"■ en Música la cierra y muestra el menú (no parece que se cerró sola): {out}")
        await press("EXIT")
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
    qr = {"path": "/tmp/q.svg", "url": "192.168.1.50:8765", "title": "Manejá la TV desde el celular", "text": "…"}
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
    vol = {"level": 40, "muted": False}
    vol_calls: list[str] = []

    async def fake_change_volume(what, step=5):
        vol_calls.append(what)
        if what == "mute":
            vol["muted"] = not vol["muted"]
        else:
            vol["level"] = max(0, min(100, vol["level"] + (step if what == "+" else -step))) if what in "+-" else int(what[1:])
            vol["muted"] = False
        return True

    async def fake_get_volume():
        return vol["level"], vol["muted"]
    saved_audio = (audio_mod.available, audio_mod.list_outputs, audio_mod.current_name, audio_mod.select,
                   audio_mod.bt_nearby_audio, menu_mod.BT_SCAN_SECONDS, menu_mod.BT_POLL_SECONDS, ctl._bt_scan,
                   audio_mod.change_volume, audio_mod.get_volume, audio_mod.volume_now)
    audio_mod.change_volume = fake_change_volume  # type: ignore[assignment]
    audio_mod.get_volume = fake_get_volume  # type: ignore[assignment]
    audio_mod.volume_now = lambda: (vol["level"], vol["muted"])  # type: ignore[assignment]
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
        check(menu_names()[-2:] == ["Sonido", "Ayuda"] and ctl.overlay.items[-2]["sub"] == "TV (HDMI) · volumen 40 %",
              f"«Sonido» antes de la ayuda, con la salida actual: {menu_names()} {ctl.overlay.items[-2]['sub']}")
        snd = menu_names().index("Sonido") + 1
        nearby_backup = list(nearby)
        nearby.clear()  # primero, sin nada nuevo cerca
        await press(f"NUMBER{snd}")
        await asyncio.sleep(0.01)  # que arranque la búsqueda (va en segundo plano)
        check(ctl._menu_mode == "sound"
              and menu_names() == ["Volumen", "Temporizador para apagar", "TV (HDMI)", "Parlante JBL",
                                   "Buscando parlantes y auriculares…"]
              and ctl.overlay.sel == 2 and scan.starts == 1,
              f"OK abre el volumen y las salidas al instante, con la actual elegida, y busca Bluetooth: {menu_names()}")
        check(ctl.overlay.items[0]["level"] == 40 and not ctl.overlay.items[0]["muted"],
              f"la fila «Volumen» con su barra: {ctl.overlay.items[0]}")
        check("desconectado" in ctl.overlay.items[3]["sub"], f"el Bluetooth desconectado lo dice: {ctl.overlay.items[3]['sub']}")
        start = len(log)
        await press("UP", "UP", "LEFT", "LEFT", "RIGHT")
        check(vol_calls == ["-", "-", "+"] and vol["level"] == 35 and ctl.overlay.items[0]["level"] == 35
              and ctl._menu_mode == "sound" and "ov:level:0:35" in log[start:],
              f"en «Volumen», ◀ ▶ bajan y suben (la barra se actualiza sin rearmar la lista): {vol_calls} {log[start:]}")
        await press("SELECT")
        check(vol["muted"] and ctl.overlay.items[0]["badge"] == "en silencio" and ctl._menu_mode == "sound",
              f"OK pone el silencio: {ctl.overlay.items[0]}")
        await press("RIGHT")
        check(not vol["muted"] and vol["level"] == 40, f"subir el volumen saca el silencio: {vol}")
        await press("DOWN", "RIGHT")
        check(ctl.sleep_timer.active() and ctl.overlay.items[1]["sub"].startswith("Se apaga en 15 min")
              and ctl._menu_mode == "sound" and ctl.overlay.sel == 1,
              f"«Temporizador»: ▶ lo pone en 15 min: {ctl.overlay.items[1]}")
        await press("RIGHT", "SELECT")
        check(ctl.sleep_timer.choice == 45, f"▶ u OK: la opción siguiente (30, 45…): {ctl.sleep_timer.choice}")
        await press("LEFT", "LEFT", "LEFT")
        check(not ctl.sleep_timer.active() and ctl.overlay.items[1]["sub"].startswith("Sin temporizador"),
              f"◀ vuelve hasta «sin temporizador»: {ctl.overlay.items[1]['sub']}")
        await press("RIGHT", "EXIT")
        check(ctl._menu_mode == "activities" and ctl.overlay.items[snd - 1]["sub"].endswith("se apaga en 15 min"),
              f"el menú dice que está el temporizador: {ctl.overlay.items[snd - 1]['sub']}")
        ctl.sleep_timer.cancel()
        nearby.clear()
        await press(f"NUMBER{snd}")
        await asyncio.sleep(0.01)
        check(ctl._menu_mode == "sound" and ctl.overlay.sel == 2, f"y se vuelve a Sonido: {ctl.overlay.sel}")
        vol_calls.clear()
        start = len(log)
        await press("DOWN", "SELECT")
        out = log[start:]
        check(chosen == ["Parlante JBL"] and "ov:busy:Conectando Parlante JBL…" in out and not scan.on,
              f"elegir el Bluetooth corta la búsqueda y lo conecta: {chosen} {out}")
        check(ctl._menu_mode == "activities" and "ov:toast:Sonido por Parlante JBL" in out
              and ctl.overlay.items[snd - 1]["sub"] == "Parlante JBL · volumen 40 %",
              f"vuelve al menú con la salida nueva: {out} {ctl.overlay.items[snd - 1]['sub']}")

        nearby[:] = nearby_backup
        await press("RIGHT")
        check(ctl._menu_mode == "sound", "▶ sobre Sonido también abre las salidas")
        await press("DOWN")  # de JBL (la actual) a la fila de búsqueda
        await ctl._sound_scan_task
        check(menu_names() == ["Volumen", "Temporizador para apagar", "TV (HDMI)", "Parlante JBL", "Auriculares Sony",
                               "Buscar Bluetooth otra vez"]
              and "nuevo" in ctl.overlay.items[4]["sub"] and ctl.overlay.sel == 5,
              f"los auriculares que aparecen cerca se agregan al final (la selección no salta): {menu_names()} sel={ctl.overlay.sel}")
        await press("SELECT")
        await asyncio.sleep(0.01)
        check(scan.starts == 4 and menu_names()[-1] == "Buscando parlantes y auriculares…",
              f"«Buscar Bluetooth otra vez» vuelve a buscar: {menu_names()[-1]}")
        start = len(log)
        await press("UP", "SELECT")
        out = log[start:]
        check(chosen[-1] == "Auriculares Sony" and "ov:busy:Emparejando Auriculares Sony…" in out
              and ctl.overlay.items[snd - 1]["sub"].startswith("Auriculares Sony"),
              f"elegir uno nuevo lo empareja y pasa el sonido ahí: {out}")
        await press("RIGHT")
        out = await press("LEFT")
        check(ctl._menu_mode == "activities" and ctl._menu_open and not scan.on and not vol_calls,
              f"sobre una salida, ◀ vuelve al menú (no toca el volumen) y corta la búsqueda: {out} {vol_calls}")
        await press("RIGHT")
        out = await press("EXIT")
        check(ctl._menu_mode == "activities" and ctl._menu_open and not scan.on,
              f"Atrás vuelve al menú y corta la búsqueda: {out}")
        await press("EXIT")
    finally:
        (audio_mod.available, audio_mod.list_outputs, audio_mod.current_name, audio_mod.select,
         audio_mod.bt_nearby_audio, menu_mod.BT_SCAN_SECONDS, menu_mod.BT_POLL_SECONDS, ctl._bt_scan,
         audio_mod.change_volume, audio_mod.get_volume, audio_mod.volume_now) = saved_audio
        ctl._sound_on = False

    def dump(default: str) -> list[dict]:
        node = lambda i, name, **p: {"id": i, "type": "PipeWire:Interface:Node",  # noqa: E731
                                     "info": {"state": "idle", "props": {"media.class": "Audio/Sink",
                                                                         "node.name": name, **p}}}
        return [node(84, "alsa_output.platform-hdmi.stereo", **{"alsa.card_name": "vc4-hdmi-0"}),
                node(69, "bluez_output.8B_D9.1", **{"device.api": "bluez5"}),
                {"type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
                 "metadata": [{"key": "default.audio.sink", "value": {"name": default}}]}]
    saved_dump, saved_avail = audio_mod._pw_dump, audio_mod.available
    audio_mod.available = lambda: True  # type: ignore[assignment]
    try:
        audio_mod._pw_dump = lambda: dump("bluez_output.8B_D9.1")  # type: ignore[assignment]
        check(audio_mod.default_kind() == "bluetooth", "la salida actual: Bluetooth")
        del ctl._sound_off_tv  # la de verdad
        check(await ctl._sound_off_tv() == "bluetooth", "con Bluetooth, el sonido no sale por la TV")
        audio_mod._pw_dump = lambda: dump("alsa_output.platform-hdmi.stereo")  # type: ignore[assignment]
        check(await ctl._sound_off_tv() == "", "por HDMI, el sonido sale por la TV")
        audio_mod._pw_dump = lambda: []  # type: ignore[assignment]
        check(await ctl._sound_off_tv() == "", "sin saber (sin PipeWire), como si saliera por la TV: se pausa")
    finally:
        audio_mod._pw_dump, audio_mod.available = saved_dump, saved_avail
        ctl._sound_off_tv = by_tv

    cmds: list[str] = []

    async def fake_run(cmd, timeout=None):
        cmds.append(" ".join(cmd[1:]))
        return 0, "Volume: 0.35 [MUTED]\n"
    saved_run = audio_mod.run_cmd
    audio_mod.run_cmd = fake_run  # type: ignore[assignment]
    try:
        S = audio_mod.SINK
        await audio_mod.change_volume("+", 5)
        await audio_mod.change_volume("=70")
        await audio_mod.change_volume("mute")
        bad = await audio_mod.change_volume("=1000;rm")
        check(cmds == [f"set-volume -l 1.0 {S} 5%+", f"set-mute {S} 0", f"set-volume -l 1.0 {S} 70%",
                       f"set-mute {S} 0", f"set-mute {S} toggle"] and not bad,
              f"volumen: subir y fijar sacan el silencio; OK cambia el silencio; lo raro no se manda: {cmds}")
        check(await audio_mod.get_volume() == (35, True), "leer el volumen y el silencio")
    finally:
        audio_mod.run_cmd = saved_run  # type: ignore[assignment]

    print("Carpetas de contenido")
    usb = tmp / "media" / "usuario" / "KINGSTON"
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
    check(found == usb and activities_mod.describe_folder(Path("/media/usuario/KINGSTON")) == "Pendrive KINGSTON",
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
    print("Un día como hoy (Fotos)")
    import datetime as dt_mod
    from tele_cec.apps import photos as photos_mod
    import struct

    def jpeg_exif(when: str, bo: str = "MM") -> bytes:
        """Un JPEG mínimo con la fecha en que se sacó (DateTimeOriginal), como los de una cámara."""
        e = ">" if bo == "MM" else "<"
        val = (when + "\0").encode()
        exif_off, data_off = 8 + 18, 8 + 18 + 18
        tiff = (bo.encode() + struct.pack(e + "HI", 42, 8)
                + struct.pack(e + "H", 1) + struct.pack(e + "HHII", 0x8769, 4, 1, exif_off) + struct.pack(e + "I", 0)
                + struct.pack(e + "H", 1) + struct.pack(e + "HHII", 0x9003, 2, len(val), data_off)
                + struct.pack(e + "I", 0) + val)
        app1 = b"Exif\0\0" + tiff
        return (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"
                + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xda\0\x08" + b"\0" * 6)
    hoy = tmp / "hoy"
    for rel in ("2014/11/IMG_20141115_100000.jpg", "2017/11/IMG-20171115-WA0003.jpg", "2017/11/VID_20171115_200000.mp4",
                "2020/11/IMG_20201113_090000.jpg", "2025/11/IMG_20251115_080000.jpg", "2018/03/IMG_20180321_120000.jpg",
                "2008/12/20-12-2008 presentacion/DSC00001.JPG"):
        (hoy / rel).parent.mkdir(parents=True, exist_ok=True)
        (hoy / rel).write_bytes(b"")
    (hoy / "2009" / "11").mkdir(parents=True)
    (hoy / "2009" / "11" / "DSC02815.JPG").write_bytes(jpeg_exif("2009:11:15 18:30:00", "MM"))
    (hoy / "2009" / "11" / "100_4558.JPG").write_bytes(jpeg_exif("2009:11:15 10:00:00", "II"))
    (hoy / "2009" / "11" / "sin_fecha.jpg").write_bytes(b"\xff\xd8\xff\xda\0\x02")
    check([photodates_mod.name_date(str(hoy / r)) for r in ("2014/11/IMG_20141115_100000.jpg", "x/IMG-20171115-WA0003.jpg",
                                                            "x/2017-11-15 10.00.00.jpg", "2009/11/DSC02815.JPG",
                                                            "2008/12/20-12-2008 presentacion/DSC00001.JPG",
                                                            "x/IMG_20211345_000000.jpg")]
          == ["20141115", "20171115", "20171115", None, "20081220", None],
          "la fecha del nombre (o de una carpeta «20-12-2008 …»); un número que no es fecha, no")
    check(photodates_mod.exif_date(str(hoy / "2009/11/DSC02815.JPG")) == "20091115"
          and photodates_mod.exif_date(str(hoy / "2009/11/100_4558.JPG")) == "20091115"
          and photodates_mod.exif_date(str(hoy / "2009/11/sin_fecha.jpg")) is None,
          "la fecha de la foto (EXIF), en los dos órdenes de bytes de las cámaras")
    ph = activities_mod.PhotosActivity({"folders": [str(hoy)]})
    ph.list_file = lambda folder: tmp / "cache" / "hoy.lst"  # type: ignore[method-assign]
    ph.running = lambda: False  # type: ignore[method-assign]
    # Un índice propio: el de otras pruebas puede tener su hilo esperando (sus Fotos «parecen abiertas»).
    photodates_mod._dates = photodates_mod.PhotoDates(tmp / "fotos-fechas.json")
    photodates_mod._dates.pause_s = 0.0
    ph.write_list(hoy)
    d = photodates_mod.dates()
    if d._thread is not None:
        d._thread.join(5)
    files_hoy = ph.mode_files(hoy, "mixed")
    check(not d.missing(files_hoy) and json.loads((tmp / "fotos-fechas.json").read_text())["files"]
          [str(hoy / "2009/11/DSC02815.JPG")] == "20091115",
          "al actualizar la lista, las fechas se leen de fondo y se guardan")
    check(d.update(files_hoy) == 0, "la segunda vez no se vuelve a leer nada")
    held = photodates_mod.PhotoDates(tmp / "fechas-pausa.json")
    held.pause_s, waits = 0.0, []
    real_hold_s = photodates_mod.HOLD_CHECK_S
    photodates_mod.HOLD_CHECK_S = 0.01
    held.hold = lambda: (waits.append(1) or len(waits) < 3)  # «Fotos abierta» las dos primeras veces
    try:
        held.update([str(hoy / "2009/11/DSC02815.JPG")])
    finally:
        photodates_mod.HOLD_CHECK_S = real_hold_s
    check(len(waits) == 3 and not held.missing([str(hoy / "2009/11/DSC02815.JPG")]),
          "mientras se miran las fotos, espera; después sigue leyendo")
    got, spread = ph.today_files(hoy, dt_mod.date(2025, 11, 15))
    check([Path(f).name for f in got] == ["100_4558.JPG", "DSC02815.JPG", "IMG_20141115_100000.jpg",
                                          "IMG-20171115-WA0003.jpg", "VID_20171115_200000.mp4"] and spread == 0,
          f"un día como hoy: las de un 15 de noviembre de otros años (no las de este año), de la más vieja a la más "
          f"nueva, con los videos: {[Path(f).name for f in got]}")
    got, spread = ph.today_files(hoy, dt_mod.date(2025, 11, 11))
    check(spread == 3 and [Path(f).name for f in got] == ["IMG_20201113_090000.jpg"],
          f"si no hay de ese día: las de esa semana (± 3 días): {[Path(f).name for f in got]}")
    check(ph.today_files(hoy, dt_mod.date(2025, 6, 1)) == ([], 0), "sin nada cerca: nada")

    class FakeDate(dt_mod.date):
        @classmethod
        def today(cls):
            return cls(2025, 11, 15)
    real_dt_menu, real_dt_pd = menu_ctl.datetime, photodates_mod.dt
    menu_ctl.datetime = SimpleNamespace(date=FakeDate, timedelta=dt_mod.timedelta)  # type: ignore[assignment]
    photodates_mod.dt = SimpleNamespace(date=FakeDate, timedelta=dt_mod.timedelta)  # type: ignore[assignment]
    real_fotos = ctl.activities["fotos"]
    ctl.activities["fotos"] = ph
    try:
        _, its = ctl._picker_entries("fotos", hoy)
        row = next((i for i in its if i.get("mode") == "today"), None)
        check(row is not None and row["name"] == "▶ Un día como hoy" and row["enabled"]
              and row["sub"] == "15 de noviembre: 4 fotos y 1 video de 2009, 2014 y 2017"
              and its.index(row) == 3 and its[4].get("mode") == "favorites" and its[5]["kind"] == "dir",
              f"en la carpeta de Fotos, después de los modos: «Un día como hoy» (y «Favoritas») con cuántas y de qué "
              f"años: {row}")
        _, its = ctl._picker_entries("fotos", hoy / "2014")
        check(not any(i.get("mode") == "today" for i in its), "en las subcarpetas no aparece")
        opened_files: list = []

        async def fake_open_mpv(files, start):
            opened_files.extend(files)
            return True
        ph._open_mpv = fake_open_mpv  # type: ignore[method-assign]
        real_which_hoy = photos_mod.shutil.which
        photos_mod.shutil.which = lambda name: "/usr/bin/" + name
        try:
            ok = await ph.open(hoy, mode="today")
        finally:
            photos_mod.shutil.which = real_which_hoy
        check(ok and ph.mode == "today" and ph.random_order and sorted(Path(f).name for f in opened_files)
              == sorted(["100_4558.JPG", "DSC02815.JPG", "IMG_20141115_100000.jpg", "IMG-20171115-WA0003.jpg",
                         "VID_20171115_200000.mp4"]),
              f"abrirlo: esas fotos y videos, al azar: {[Path(f).name for f in opened_files]}")
        check(ph._show_year and any(a.endswith("tele_cec_corner.lua") for a in ph.mpv_args(tmp / "p.lst", 0)),
              "con el script que escribe el año en la esquina")
        ordered: list = []

        async def fake_open_mpv_ordered(files, start):
            ordered.extend(files)
            return True
        ph._open_mpv = fake_open_mpv_ordered  # type: ignore[method-assign]
        ph.cfg["today_shuffle"] = False
        photos_mod.shutil.which = lambda name: "/usr/bin/" + name
        try:
            await ph.open(hoy, mode="today")
        finally:
            photos_mod.shutil.which = real_which_hoy
            ph.cfg.pop("today_shuffle")
        check(not ph.random_order and [Path(f).name for f in ordered][:2] == ["100_4558.JPG", "DSC02815.JPG"],
              f"con today_shuffle = false: año por año, del más viejo al más nuevo: {[Path(f).name for f in ordered]}")
        corner: list = []

        async def corner_cmd(*a, timeout=3.0):
            corner.append(a)
            return True, None
        ph.mpv.command = corner_cmd  # type: ignore[method-assign]
        ph.show_date = False
        await ph._mpv_file(str(hoy / "2009" / "11" / "DSC02815.JPG"))
        check(("script-message-to", "tele_cec_corner", "show", "2009", "hace 16 años") in corner,
              f"en cada foto, el año (de la foto misma, EXIF) y hace cuánto, en una esquina: {corner}")
        corner.clear()
        (hoy / "sueltas").mkdir()
        (hoy / "sueltas" / "sin_fecha.jpg").write_bytes(b"\xff\xd8\xff\xda\0\x02")
        await ph._mpv_file(str(hoy / "sueltas" / "sin_fecha.jpg"))
        check(("script-message-to", "tele_cec_corner", "clear") in corner, "sin fecha: no muestra nada")
        corner.clear()
        ph._show_year = False
        await ph._mpv_file(str(hoy / "2009" / "11" / "DSC02815.JPG"))
        check(corner == [("script-message-to", "tele_cec_corner", "clear")]
              and any("corner" in a for a in ph.mpv_args(tmp / "p.lst", 0)),
              f"en los otros modos, sin nada en la esquina (pero con el script, para prenderla con 1): {corner}")

        print("Fotos: la fecha en la esquina (1)")
        ph._show_date_file = lambda: tmp / "cache" / "fotos-fecha-en-pantalla"  # type: ignore[method-assign]
        ph.mode = "photos"

        async def mpv_path(prop):
            return str(hoy / "2009" / "11" / "DSC02815.JPG") if prop == "path" else None
        ph.mpv.get = mpv_path  # type: ignore[method-assign]
        real_use_mpv = ph.use_mpv
        ph.use_mpv = lambda mode: True  # type: ignore[method-assign]
        corner.clear()
        on = await ph.toggle_date()
        check(on is True and ("script-message-to", "tele_cec_corner", "show", "domingo 15 de noviembre de 2009",
                              "hace 16 años") in corner,
              f"1 la prende: el día exacto (de la foto) y hace cuánto: {corner}")
        check((tmp / "cache" / "fotos-fecha-en-pantalla").read_text().strip() == "si"
              and ph._load_show_date() is True, "y se acuerda para la próxima vez")
        corner.clear()
        (hoy / "2003" / "Diciembre").mkdir(parents=True)
        (hoy / "2003" / "Diciembre" / "DSC0001.JPG").write_bytes(b"\xff\xd8\xff\xda\0\x02")
        await ph._mpv_file(str(hoy / "2003" / "Diciembre" / "DSC0001.JPG"))
        await ph._mpv_file(str(hoy / "2009" / "11" / "sin_fecha.jpg"))
        await ph._mpv_file(str(hoy / "2008" / "12" / "20-12-2008 presentacion" / "DSC00001.JPG"))
        shown = [c[3] for c in corner if c[2] == "show"]
        check(shown == ["diciembre de 2003", "noviembre de 2009", "sábado 20 de diciembre de 2008"],
              f"sin el día en el nombre ni en la foto: el mes y el año de las carpetas: {shown}")
        corner.clear()
        ph._show_year = True  # en «Un día como hoy», con la fecha prendida: el día entero, no solo el año
        await ph._mpv_file(str(hoy / "2009" / "11" / "DSC02815.JPG"))
        check(corner and corner[-1][3] == "domingo 15 de noviembre de 2009", f"un día como hoy + fecha: {corner}")
        ph._show_year = False
        print("Fotos: un globo junto a la fecha si la foto o el video tiene la ubicación")

        def box(kind: bytes, body: bytes) -> bytes:
            return (8 + len(body)).to_bytes(4, "big") + kind + body
        xyz = box(b"udta", box(b"\xa9xyz", (18).to_bytes(2, "big") + b"\x15\xc7-34.6037-058.3816/"))
        mp4 = box(b"ftyp", b"isom\0\0\2\0isom") + box(b"mdat", b"\0" * 4000) + box(b"moov", box(b"mvhd", b"\0" * 20) + xyz)
        loci = box(b"loci", b"\0\0\0\0\x15\xc7\0\0" + int(-58.3816 * 65536).to_bytes(4, "big", signed=True)
                   + int(-34.6037 * 65536).to_bytes(4, "big", signed=True) + b"\0\0\0\0earth\0\0")
        apple = (box(b"keys", b"\0" * 8 + box(b"mdta", b"com.apple.quicktime.location.ISO6709"))
                 + box(b"ilst", box(b"\0\0\0\1", box(b"data", b"\0\0\0\1\0\0\0\0+40.7128-074.0060+010.000/"))))
        vdir = hoy / "2017" / "11"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "VID_20171115_200000.mp4").write_bytes(mp4)
        (vdir / "VID_20171115_200001.mp4").write_bytes(box(b"moov", box(b"udta", loci)) + box(b"mdat", b"\0" * 100))
        (vdir / "IMG_2001.MOV").write_bytes(box(b"ftyp", b"qt  ") + box(b"moov", box(b"meta", apple)))
        (vdir / "VID_20171115_200002.mp4").write_bytes(box(b"ftyp", b"isom") + box(b"moov", box(b"mvhd", b"\0" * 20)))
        gps = photodates_mod.video_gps
        check(gps(str(vdir / "VID_20171115_200000.mp4")) == (-34.6037, -58.3816)
              and gps(str(vdir / "VID_20171115_200001.mp4")) == (-34.603699, -58.381592)
              and gps(str(vdir / "IMG_2001.MOV")) == (40.7128, -74.006)
              and gps(str(vdir / "VID_20171115_200002.mp4")) is None and gps(str(hoy / "2009" / "11" / "DSC02815.JPG")) is None,
              "la ubicación de un video: ©xyz (Android, con el índice al final), loci (3GPP) e ISO6709 (iPhone)")
        corner.clear()
        await ph._mpv_file(str(vdir / "VID_20171115_200000.mp4"))
        check(corner and corner[-1][2] == "show" and corner[-1][3] == "miércoles 15 de noviembre de 2017"
              and corner[-1][-1] == "globe",
              f"con la fecha prendida y ubicación: el globo al lado: {corner}")
        corner.clear()
        await ph._mpv_file(str(vdir / "VID_20171115_200002.mp4"))
        check(corner and corner[-1][-1] != "globe" and corner[-1][3].endswith("2017"), f"sin ubicación, sin globo: {corner}")
        ph.cfg["show_location"] = False
        corner.clear()
        await ph._mpv_file(str(vdir / "VID_20171115_200000.mp4"))
        check(corner and corner[-1][-1] != "globe", f"[apps.fotos] show_location = false lo apaga: {corner}")
        ph.cfg.pop("show_location")
        ph.show_date = False
        corner.clear()
        await ph._mpv_file(str(vdir / "VID_20171115_200000.mp4"))
        check(not any(c[-1] == "globe" for c in corner), f"con la fecha apagada, tampoco: {corner}")
        ph.show_date = True
        for f in vdir.glob("VID_20171115_20000[12].mp4"):
            f.unlink()
        (vdir / "IMG_2001.MOV").unlink()
        corner.clear()
        off = await ph.toggle_date()
        check(off is False and corner == [("script-message-to", "tele_cec_corner", "clear")]
              and (tmp / "cache" / "fotos-fecha-en-pantalla").read_text().strip() == "no",
              "otra vez 1: la saca (y se acuerda)")
        ph.use_mpv = lambda mode: False  # type: ignore[method-assign]
        check(await ph.toggle_date() is None and ph.show_date is False, "con feh no se puede (lo avisa el controlador)")
        ph.use_mpv = real_use_mpv  # type: ignore[method-assign]
        check(photodates_mod.date_text((2026, 11, 14), dt_mod.date(2026, 11, 15))[1] == "ayer"
              and photodates_mod.date_text((2026, 8, 1), dt_mod.date(2026, 11, 15))[1] == "hace 3 meses"
              and photodates_mod.date_parts("/f/2005/Mar del Plata/a.jpg") == (2005, None, None, None)
              and photodates_mod.date_parts("/f/2019/Viaje/1/a.jpg") == (2019, None, None, None)
              and photodates_mod.date_parts("/f/sin fecha/a.jpg") is None,
              "hace cuánto (ayer, meses); «Mar del Plata» no es marzo; una subcarpeta «1» no es enero")
        dp = photodates_mod.date_parts
        check(dp("/f/2007/10/dcao0424.jpg", "20020101") == (2007, 10, None, None)
              and dp("/f/2008/IMG_20071231_2300.jpg", "20071231") == (2007, 12, 31, None)
              and dp("/f/2007/bajadas 10-12-07/DCAO0281.JPG", "20071121") == (2007, 11, 21, None)
              and dp("/f/2007/10/dcao0001.jpg", "20070101") == (2007, 10, None, None)
              and dp("/f/2018/01/IMG_20180101_0010.jpg", "20180101") == (2018, 1, 1, None),
              "una fecha que contradice la carpeta (el reloj de la cámara sin poner) no vale: manda la carpeta "
              "(salvo el 31 de diciembre en la carpeta del año siguiente)")
        check(dp("/f/1980-2000/IMG-20200113-WA0005.jpg", "20200113") == (1980, None, None, 2000)
              and photodates_mod.date_text((1980, None, None, 2000)) == ("entre 1980 y 2000", ""),
              "una carpeta «1980-2000»: entre esos años (no la fecha en que se escaneó o se mandó)")
        check(dp("/f/Varias/casa/108 (09-08).jpg") == (2008, 9, None, None)
              and dp("/f/Varias/casa/015 (10-97).jpg") == (1997, 10, None, None),
              "«108 (09-08).jpg»: septiembre de 2008")
        hits = photodates_mod.PhotoDates(tmp / "fechas-plaus.json")
        hits._data = {"/f/2007/10/a.jpg": "20020101", "/f/2002/01/b.jpg": "20020101"}
        check(hits.on_this_day(list(hits._data), dt_mod.date(2025, 1, 1)) == ["/f/2002/01/b.jpg"],
              "y en «Un día como hoy» tampoco aparece (si no, cada 1.º de enero se llenaba de esas)")
        FakeDate.today = classmethod(lambda cls: cls(2025, 6, 1))  # type: ignore[method-assign]
        _, its = ctl._picker_entries("fotos", hoy)
        row = next(i for i in its if i.get("mode") == "today")
        check(not row["enabled"] and row["sub"] == "No hay fotos de un 1 de junio en otros años",
              f"sin fotos de esa fecha: lo dice: {row['sub']}")
        check(menu_ctl._years_text([2009, 2010, 2012, 2014, 2023]) == "de 5 años, entre 2009 y 2023"
              and menu_ctl._years_text([2014]) == "de 2014", "los años")

        print("Fotos: favoritas (2)")
        fav = str(hoy / "2009" / "11" / "DSC02815.JPG")
        vid = str(hoy / "2017" / "11" / "VID_20171115_200000.mp4")
        check(ph.favorites() == [] and not ph.is_favorite(fav), "de entrada no hay ninguna")
        check(ph.toggle_favorite(fav) is True and ph.is_favorite(fav)
              and (tmp / "datos" / "fotos-favoritas.txt").read_text() == fav + "\n",
              "2 guarda la foto que se ve en ~/.local/share/tele-cec/fotos-favoritas.txt (no en la caché)")
        ph.show_date = False
        corner.clear()
        await ph._mpv_file(fav)
        check(corner == [("script-message-to", "tele_cec_corner", "show", "♥", "")],
              f"en la esquina se ve un ♥ (aunque la fecha esté apagada): {corner}")
        ph.show_date = True
        corner.clear()
        await ph.refresh_corner()
        check(corner and corner[-1][3] == "♥ domingo 15 de noviembre de 2009", f"con la fecha prendida: ♥ y la fecha: {corner}")
        ph.show_date = False
        corner.clear()
        await ph._mpv_file(str(hoy / "2009" / "11" / "sin_fecha.jpg"))
        check(corner == [("script-message-to", "tele_cec_corner", "clear")], "una que no es favorita: nada")
        ph.toggle_favorite(vid)
        ph.toggle_favorite("/no/existe.jpg")
        check(ph.favorite_files() == [fav, vid], "para verlas: las que existen, en el orden en que se guardaron")
        _, its = ctl._picker_entries("fotos", None)
        row = its[-1]
        check(row.get("mode") == "favorites" and row["name"] == "▶ ♥ Favoritas" and row["enabled"]
              and row["sub"] == "1 foto y 1 video guardadas con 1 · otra vez 1 las saca" and row["path"] == its[0]["path"]
              and its[0]["kind"] == "dir",
              f"en Carpetas ▸ (la lista de carpetas), al final: «♥ Favoritas» con cuántas hay: {row}")
        _, its = ctl._picker_entries("fotos", hoy)
        check([i.get("mode") for i in its if i.get("mode") in ("today", "favorites")] == ["today", "favorites"],
              "y adentro de la carpeta, después de «Un día como hoy»")
        favs_opened: list = []

        async def fake_open_mpv_favs(files, start):
            favs_opened.extend(files)
            return True
        ph._open_mpv = fake_open_mpv_favs  # type: ignore[method-assign]
        photos_mod.shutil.which = lambda name: "/usr/bin/" + name
        try:
            ok = await ph.open(hoy, mode="favorites")
            check(ok and favs_opened == [fav, vid] and ph.mode == "favorites" and not ph.random_order
                  and not ph._show_year, f"abrirlas: las favoritas en el orden guardado, con mpv: {favs_opened}")
            ph._favorites = None  # se vuelven a leer del archivo
            check(ph.toggle_favorite(fav) is False and not ph.is_favorite(fav)
                  and ph.favorites() == [vid, "/no/existe.jpg"], "otra vez 2: la saca")
            ph._favorites = []
            ph._save_favorites()
            ok = await ph.open(hoy, mode="favorites")
            check(not ok and ph.fail_text.startswith("Todavía no hay favoritas"), f"sin ninguna: avisa: {ph.fail_text}")
            row = ctl._picker_entries("fotos", None)[1][-1]
            check(not row["enabled"] and row["sub"].startswith("Todavía no hay"), f"y en Carpetas ▸ queda en gris: {row}")
        finally:
            photos_mod.shutil.which = real_which_hoy
        check(photos_mod.FAVORITES in photos_mod.ALL_MODES and photos_mod.MODE_NAMES[photos_mod.FAVORITES] == "favoritas",
              "es un modo más (el celular también puede pedirlo)")
    finally:
        ctl.activities["fotos"] = real_fotos
        menu_ctl.datetime, photodates_mod.dt = real_dt_menu, real_dt_pd

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
    check("--prefetch-playlist=yes" in margs, "mpv lee la foto siguiente mientras se ve esta")
    check("--sub-auto=no" in margs and "--audio-file-auto=no" in margs,
          "Fotos no recorre la carpeta buscando subtítulos ni audio aparte (por la red tardaba segundos cada video)")
    from tele_cec.apps import mpv as mpv_mod
    real_pi, real_env = mpv_mod.pi_decoder, os.environ.get("WAYLAND_DISPLAY")
    try:
        mpv_mod.pi_decoder = lambda: True
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        pi_args = mpv_mod.video_args()
        mpv_mod.pi_decoder = lambda: False
        other = mpv_mod.video_args()
        # Al prender la Pi el servicio arranca antes que el escritorio: sin WAYLAND_DISPLAY en su entorno (las fotos
        # salían azules). Se encuentra igual, por el socket de Wayland.
        mpv_mod.pi_decoder = lambda: True
        os.environ.pop("WAYLAND_DISPLAY", None)
        run_dir = tmp / "run-user"
        run_dir.mkdir()
        (run_dir / "wayland-0").write_text("")
        real_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = str(run_dir)
        try:
            boot_args = mpv_mod.video_args()
        finally:
            if real_xdg is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = real_xdg
    finally:
        mpv_mod.pi_decoder = real_pi
        if real_env is None:
            os.environ.pop("WAYLAND_DISPLAY", None)
        else:
            os.environ["WAYLAND_DISPLAY"] = real_env
    check(boot_args[:3] == ["--vo=gpu", "--gpu-api=opengl", "--hwdec=no"],
          f"recién prendida la Pi (el servicio sin WAYLAND_DISPLAY): igual con OpenGL, no azul: {boot_args}")
    check(pi_args[:3] == ["--vo=gpu", "--gpu-api=opengl", "--hwdec=no"] and pi_args[3].endswith("tele_cec_vo.lua")
          and Path(pi_args[3].split("=", 1)[1]).is_file(),
          f"en la Pi: fotos con OpenGL (8 bits), sin hardware, y el script elige la salida de cada archivo: {pi_args}")
    check(other == ["--hwdec=auto-safe"], f"fuera de la Pi: lo que elija mpv: {other}")
    real_which = photos_mod.shutil.which
    try:
        photos_mod.shutil.which = lambda name: "/usr/bin/" + name
        check(activities_mod.PhotosActivity({}).use_mpv("photos")
              and not activities_mod.PhotosActivity({"viewer": "feh"}).use_mpv("photos")
              and activities_mod.PhotosActivity({"viewer": "feh"}).use_mpv("videos"),
              "«solo fotos» va con mpv (la placa de video); con viewer = \"feh\", el de antes (los videos, siempre mpv)")
        photos_mod.shutil.which = lambda name: None if name == "mpv" else "/usr/bin/" + name
        check(not activities_mod.PhotosActivity({}).use_mpv("photos"), "sin mpv instalado, «solo fotos» sigue con feh")
    finally:
        photos_mod.shutil.which = real_which
    one = [str(mix / f"IMG_2020110{i}_000000.jpg") for i in range(6)]
    many = one + [str(mix / "2019" / f"IMG_2019010{i}_000000.jpg") for i in range(6)]
    check(not pm.shuffled_order(one) and pm.shuffled_order(many),
          "al azar si hay subcarpetas con fotos; una carpeta sola, en orden")
    pm.shuffle = True
    check(pm.shuffled_order(one), "con shuffle = true, siempre al azar")
    pm.shuffle = False
    import random as _random
    _random.seed(7)
    mixed_up, first = pm.shuffle_from(many, many[8])
    check(first == many[8] and mixed_up[0] == many[8] and sorted(mixed_up) == sorted(many) and mixed_up != many,
          f"mezclada, empezando por la foto que se veía (al cambiar de modo con ▼): {[Path(x).name for x in mixed_up[:3]]}")
    mixed_up, first = pm.shuffle_from(many, None)
    check(first is None and sorted(mixed_up) == sorted(many), "al abrir: toda mezclada")
    pm.random_order = True
    fargs = pm.default_args(tmp / "mix", tmp / "cache" / "x.lst")
    check("--randomize" not in fargs and "--sort=filename" not in fargs,
          f"feh con la lista ya mezclada: la deja como viene (sin ordenarla por nombre): {fargs[-4:]}")
    pm.random_order = False
    check("--sort=filename" in pm.default_args(tmp / "mix", tmp / "cache" / "x.lst"), "en orden: por nombre")

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
    await pm.photo_changed(new_file=False)
    check("video-rotate=0" not in fm.sent and "video-zoom=0" in fm.sent and pm.rotation == 90,
          f"girar con zoom: la misma foto, entera y girada (el giro queda): {fm.sent}")
    pm._mpv_dirty, pm.zoom = True, 1
    fm.sent.clear()
    await pm.photo_changed()
    check("video-rotate=0" in fm.sent and not pm._mpv_dirty,
          f"pasar a otra foto: entera y derecha enseguida, sin esperar el aviso de mpv: {fm.sent}")
    pm._mpv_dirty, pm.zoom, pm.paused, pm._zoom_paused = True, 1, True, True  # con zoom (que pausó la presentación)
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

    print("Música: temas elegidos desde el celular")
    sargs2 = activities_mod.MusicActivity({"shuffle": True}).default_args(usb, tmp / "m.lst", 2)
    check("--playlist-start=2" in sargs2 and "--shuffle" not in sargs2,
          f"temas elegidos: en ese orden (aunque Música mezcle), desde el elegido: {sargs2}")
    from tele_cec.apps import player as player_mod
    mu = activities_mod.MusicActivity({"folders": [str(tmp / "musica")]})
    mu.mpv.socket_path = tmp / "cache" / "musica-mpv.sock"   # no tocar el de verdad
    spawned: list = []

    async def fake_spawn(argv):
        spawned.append(argv)
        return True

    async def fake_ready(timeout=10.0):
        return True
    mu.spawn, mu.mpv.ready = fake_spawn, fake_ready
    orig_log_dir = player_mod.LOG_DIR
    player_mod.LOG_DIR = tmp / "cache"
    try:
        chosen = [str(mdir / "2 - Dos.flac"), str(mdir / "1 - Uno.ogg")]
        mu.want_files = (chosen, 1)
        ok = await mu.open()
        lst = Path(next(a for a in spawned[-1] if a.startswith("--playlist=")).split("=", 1)[1])
        check(ok and lst.read_text().splitlines() == chosen and "--playlist-start=1" in spawned[-1]
              and mu.want_files is None and mu.current_folder == mdir,
              f"abre Música con esos temas (no la carpeta entera), desde el segundo: {spawned[-1]}")
        vi = activities_mod.VideosActivity({"folders": [str(mdir)]})
        vi.mpv.socket_path = tmp / "cache" / "videos-mpv.sock"
        vi.spawn, vi.mpv.ready = fake_spawn, fake_ready
        (mdir / "peli.mkv").write_bytes(b"")
        ok = await vi.open(mdir / "peli.mkv")
        lst = Path(next(a for a in spawned[-1] if a.startswith("--playlist=")).split("=", 1)[1])
        check(ok and lst.read_text().splitlines() == [str(mdir / "peli.mkv")] and vi.current_folder == mdir,
              f"Videos con un archivo elegido: solo ese (no la carpeta): {lst.read_text().splitlines()}")
        (mdir / "peli.mkv").unlink()
        mu2 = activities_mod.MusicActivity({"folders": [str(mdir)]})
        mu2.mpv.socket_path = tmp / "cache" / "musica2-mpv.sock"
        mu2.spawn, mu2.mpv.ready = fake_spawn, fake_ready
        ok = await mu2.open(mdir / "2 - Dos.flac")
        lst = Path(next(a for a in spawned[-1] if a.startswith("--playlist=")).split("=", 1)[1])
        names_ = [Path(x).name for x in lst.read_text().splitlines()]
        check(ok and names_[:3] == ["1 - Uno.ogg", "2 - Dos.flac", "10 - Diez.mp3"]
              and f"--playlist-start={names_.index('2 - Dos.flac')}" in spawned[-1],
              f"Música con un tema elegido: desde ese, con los demás de la carpeta: {names_} {spawned[-1][-2:]}")
        sent: list = []

        async def fake_cmd(*args, timeout=3.0):
            sent.append(args)
            return True, None
        mu.mpv.command = fake_cmd
        ok = await mu.play_files([str(mdir / "10 - Diez.mp3"), str(mdir / "1 - Uno.ogg")], 5)
        check(ok and ("set_property", "options/playlist-start", 1) in sent
              and sent.index(("set_property", "options/playlist-start", 1)) < next(
                  i for i, c in enumerate(sent) if c[0] == "loadlist")
              and ("set_property", "options/playlist-start", "auto") in sent
              and ("set_property", "pause", False) in sent and ("set_property", "options/shuffle", False) in sent,
              f"ya abierta: cambia la lista y arranca en el elegido (sin pasarse del último), sin pausa: {sent}")
        props: dict = {"shuffle": False, "loop-file": False, "loop-playlist": False, "playlist-count": 2}

        async def fake_cmd2(*args, timeout=3.0):
            sent.append(args)
            if args[0] == "get_property":
                return True, props.get(args[1])
            if args[0] == "set_property":
                props[args[1]] = args[2]
            return True, None
        mu.mpv.command = fake_cmd2
        mu.osd = lambda *a: asyncio.sleep(0)  # type: ignore[method-assign]
        sent.clear()
        ok = await mu.play_files([str(mdir / "10 - Diez.mp3"), str(mdir / "1 - Uno.ogg")], 1, shuffle=True)
        loads = [i for i, c in enumerate(sent) if c[0] == "loadlist"]
        mixes = [i for i, c in enumerate(sent) if c == ("playlist-shuffle",)]
        check(ok and loads and mixes and loads[0] < mixes[0] and props["shuffle"] is True,
              f"«Mezclar» desde el celular: la lista en orden y después mpv mezcla los demás (aleatorio prendido): {sent}")
        sent.clear()
        on = await mu.toggle_shuffle()
        check(on is False and ("playlist-unshuffle",) in sent and props["shuffle"] is False,
              f"aleatorio otra vez: vuelve al orden (y lo anota para la barra y el celular): {sent}")
        on = await mu.toggle_shuffle()
        check(on and ("playlist-shuffle",) in sent and props["shuffle"] is True, "y de nuevo mezcla")
        got = [await mu.cycle_loop() for _ in range(3)]
        check(got == ["playlist", "track", "none"] and props["loop-file"] == "no" and props["loop-playlist"] == "no",
              f"repetir: la lista → el tema → nada: {got} {props}")
        props["loop-playlist"] = "inf"
        check(await mu.cycle_loop() == "track" and props["loop-file"] == "inf" and props["loop-playlist"] == "no",
              "si el celular puso «repetir la lista», el verde sigue desde ahí: el tema")
        mu.mpv.command = fake_cmd
        sent.clear()
        await mu.enqueue("/x/tema.mp3", True)
        await mu.enqueue("/x/otro.mp3", False)
        check(sent == [("loadfile", "/x/tema.mp3", "insert-next"), ("loadfile", "/x/otro.mp3", "append")],
              f"a la cola: después del actual o al final: {sent}")
    finally:
        player_mod.LOG_DIR = orig_log_dir

    print("Música: la biblioteca (artistas, discos, temas)")
    from tele_cec.apps import library as library_mod
    check(library_mod.from_path("/m/Charly García/Clics Modernos/1-02 Nos Siguen Pegando Abajo.m4a", "/m")
          == {"title": "Nos Siguen Pegando Abajo", "artist": "Charly García", "album": "Clics Modernos",
              "album_artist": "Charly García", "track": 2, "disc": 1}, "sin etiquetas: de las carpetas (disco-número)")
    check(library_mod.from_path("/m/suelto.mp3", "/m")["title"] == "suelto", "un tema suelto: el nombre del archivo")
    check(library_mod.fold("Andrés CALAMARO") == "andres calamaro", "buscar sin tildes ni mayúsculas")
    lroot = tmp / "biblio"
    tags = {"Comp/Rock 90/01 Uno.mp3": {"title": "Uno", "artist": "Banda A", "album": "Rock 90", "track": 1},
            "Comp/Rock 90/02 Dos.mp3": {"title": "Dos", "artist": "Banda B", "album": "Rock 90", "track": 2},
            "The Beatles/Help/03 Help.mp3": {"title": "Help!", "artist": "The Beatles", "album": "Help!",
                                             "album_artist": "The Beatles", "track": 3, "dur": 140.2},
            "Abel Pintos/Sueño Dorado/01 Aquí te espero.mp3": {}}
    for rel in tags:
        (lroot / rel).parent.mkdir(parents=True, exist_ok=True)
        (lroot / rel).write_bytes(b"")
    reads: list = []
    orig_read = library_mod.read_tags

    def fake_read(path):
        reads.append(path)
        return dict(tags[os.path.relpath(path, lroot)])
    library_mod.read_tags = fake_read
    try:
        lib = library_mod.MusicLibrary(lambda: [lroot], tmp / "cache" / "indice.json")
        lib.refresh()
        check([a.name for a in lib.artists.values()] == ["Abel Pintos", "The Beatles", "Varios"],
              f"artistas en orden («The Beatles» con la B), un disco de varios al final: {list(lib.artists.values())}")
        beatles = next(a for a in lib.artists.values() if a.name == "The Beatles")
        check(beatles.albums[0].title == "Help!" and beatles.songs[0].dur == 140.2, "las etiquetas mandan")
        n = len(reads)
        lib.refresh()
        check(len(reads) == n, "otra vez enseguida: no vuelve a recorrer la carpeta")
        lib2 = library_mod.MusicLibrary(lambda: [lroot], tmp / "cache" / "indice.json")
        lib2.refresh()
        check(len(reads) == n and len(lib2.songs) == 4, "con el índice guardado: no vuelve a leer las etiquetas")
        (lroot / "Abel Pintos/Sueño Dorado/01 Aquí te espero.mp3").write_bytes(b"x")
        lib2.refresh(force=True)
        check(len(reads) == n + 1, "un archivo que cambió: solo ese se lee de nuevo")
    finally:
        library_mod.read_tags = orig_read

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
        r = await audio_mod2.select(audio_mod2.Output("Auriculares BT", "bluetooth", mac="AA:BB:CC:DD:EE:01", paired=False))
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
        audio_mod2.list_outputs = lambda: [tv, audio_mod2.Output("Auriculares BT", "bluetooth", 77 if speaker_on[0] else None,
                                                                 mac="AA:BB:CC:DD:EE:01")]
        r = await audio_mod2.select(tv)
        check(r[0] and cmds == ["wpctl set-default 55", "bluetoothctl disconnect AA:BB:CC:DD:EE:01"],
              f"elegir la TV: el sonido pasa a la TV y el parlante se desconecta: {cmds}")
        cmds.clear()
        r = await audio_mod2.select(tv)
        check(r[0] and cmds == ["wpctl set-default 55"], f"con el parlante ya desconectado, no hace nada más: {cmds}")
        cmds.clear()
        speaker_on[0] = True
        r = await audio_mod2.select(audio_mod2.Output("Auriculares BT", "bluetooth", 77, mac="AA:BB:CC:DD:EE:01"))
        check(r[0] and cmds == ["wpctl set-default 77"], f"elegir el parlante no desconecta nada: {cmds}")
    finally:
        audio_mod2.list_outputs, audio_mod2.run_cmd = real

    print("Radio")
    from tele_cec.apps import radio as radio_mod
    real_radio_dir = radio_mod.LOG_DIR
    radio_mod.LOG_DIR = tmp / "cache-radio"   # la última radio y la lista: no tocar las de verdad
    try:
        r0 = ctl.activities.get("radio")
        check(isinstance(r0, activities_mod.RadioActivity) and r0.stations == []
              and r0.context == "radio" and r0.audio_only,
              f"no viene con ninguna radio (se ponen en config.toml): {getattr(r0, 'stations', None)}")
        check(r0.source() == ("Sin radios: agregalas en [apps.radio] stations", False)
              and activities_mod.RadioActivity({"stations": [{"name": "Radio Uno", "url": "https://a.example/x"}]})
              .source() == ("Radio Uno", True), "en el menú: las radios (o que no hay)")
        two = {"stations": [{"name": "Radio Uno", "url": "https://a.example/320.mp3"},
                            "https://otra.example:8000/stream", {"name": "sin url"}]}
        radio = activities_mod.RadioActivity(two)
        check([s["name"] for s in radio.stations] == ["Radio Uno", "otra.example:8000"],
              f"una dirección sola sirve (el nombre sale de la dirección); sin url, se saltea: {radio.stations}")
        check(radio.playlist_text() == "#EXTM3U\n#EXTINF:-1,Radio Uno\nhttps://a.example/320.mp3\n"
              "#EXTINF:-1,otra.example:8000\nhttps://otra.example:8000/stream\n", "la lista .m3u con los nombres")
        spawned: list = []

        async def radio_spawn(args, env=None):
            spawned.append(args)
            return True

        async def radio_ready(timeout=10.0):
            return True
        radio.spawn = radio_spawn  # type: ignore[method-assign]
        radio.mpv.ready = radio_ready  # type: ignore[method-assign]
        check(await radio.open() and "--playlist-start=0" in spawned[-1]
              and any(a.endswith("tele_cec_radio.lua") for a in spawned[-1])
              and "--cache-pause-initial=yes" in spawned[-1] and "--loop-playlist=inf" in spawned[-1]
              and any(a.startswith("--stream-lavf-o=reconnect=1") for a in spawned[-1])
              and any(a.startswith("--input-ipc-server=") for a in spawned[-1]),
              f"abre mpv con la lista, la pantalla de la radio, un colchón y reconexión: {spawned[-1]}")
        rcmds: list = []
        radio_state = {"pos": 0, "pause": False}

        async def radio_cmd(*a, timeout=3.0):
            rcmds.append(a)
            if a[0] == "playlist-play-index":
                radio_state["pos"] = a[1]
            return True, None

        async def radio_get(prop):
            return {"playlist-pos": radio_state["pos"], "pause": radio_state["pause"]}.get(prop)

        async def radio_set(prop, value):
            rcmds.append(("set", prop, value))
            if prop == "pause":
                radio_state["pause"] = value
            return True
        radio.mpv.command, radio.mpv.get, radio.mpv.set = radio_cmd, radio_get, radio_set  # type: ignore[method-assign]
        check(await radio.step(1) == "otra.example:8000" and radio_state["pos"] == 1
              and await radio.step(1) == "Radio Uno" and radio_state["pos"] == 0,
              "⏭: la radio siguiente (y da la vuelta)")
        await radio.step(1)
        check(radio.last_index() == 1, "se acuerda de la última que sonó")
        await radio.close()
        check(await radio.open() and "--playlist-start=1" in spawned[-1], "y la próxima vez arranca con esa")
        rcmds.clear()
        await radio.play_pause()
        check(radio_state["pause"] is True and radio._paused_at is not None, "OK: pausa")
        radio._paused_at -= radio_mod.LIVE_AGAIN_S + 1
        rcmds.clear()
        await radio.play_pause()
        check(("playlist-play-index", 1) in rcmds and radio_state["pause"] is False and radio._paused_at is None,
              f"después de una pausa larga, OK vuelve a conectar: lo que sale ahora, no lo de hace un rato: {rcmds}")
        await radio.play_pause()
        rcmds.clear()
        await radio.play_pause()
        check(not any(c[0] == "playlist-play-index" for c in rcmds) and radio_state["pause"] is False,
              "una pausa corta: sigue donde estaba")
        check(activities_mod.RadioActivity({"stations": ["https://x.example/a"]}).playlist_text().count("#EXTINF") == 1
              and await activities_mod.RadioActivity({"stations": ["https://x.example/a"]}).step(1) == "",
              "con una sola radio, ⏭ no hace nada")

        print("Radio: la tapa del disco de lo que suena (iTunes)")
        from tele_cec.apps import covers as covers_mod
        real_cover_dir, covers_mod.LOG_DIR = covers_mod.LOG_DIR, tmp / "cache-radio"
        real_fetch, real_find = covers_mod._fetch, covers_mod.find
        fetched: list[str] = []
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, "PNG")
        png = buf.getvalue()

        def fake_fetch(url):
            fetched.append(url)
            if "itunes.apple.com/search" in url:
                return json.dumps({"results": [
                    {"artistName": "Charly García", "trackName": "Demoliendo hoteles", "collectionName": "Piano bar",
                     "artworkUrl100": "https://a.example/100x100bb.jpg"}]}).encode()
            return png
        covers_mod._fetch = fake_fetch  # type: ignore[assignment]
        try:
            check(covers_mod.song_parts("Charly Garcia - Demoliendo Hoteles") == ("Charly Garcia", "Demoliendo Hoteles")
                  and covers_mod.song_parts("Soda Stereo – De música ligera") == ("Soda Stereo", "De música ligera")
                  and covers_mod.song_parts("Radio Dos FM") is None and covers_mod.song_parts("") is None,
                  "«Artista - Tema» (también con guion largo); sin guion no se busca (saldría cualquier disco)")
            check(covers_mod._similar("Charly García", "charly garcia") and covers_mod._similar("Pericos", "Los Pericos")
                  and covers_mod._similar("Charly García", "Charly Garcia feat. Fito") and not covers_mod._similar("Soda Stereo", "Gustavo Cerati"),
                  "el artista de iTunes tiene que parecerse al que mandó la emisora")
            path = covers_mod.find("Charly Garcia", "Demoliendo Hoteles")
            check(path is not None and path.is_file() and path.read_bytes() == png and path.suffix == ".png"
                  and fetched == ["https://itunes.apple.com/search?media=music&entity=song&limit=5"
                                  "&term=Charly+Garcia+Demoliendo+Hoteles", "https://a.example/600x600bb.jpg"],
                  f"busca en iTunes y baja la tapa grande a ~/.cache/tele-cec/radio-tapas: {fetched}")
            fetched.clear()
            check(covers_mod.find("charly garcía", "demoliendo hoteles") == path and not fetched,
                  "la segunda vez (sin tildes ni mayúsculas) sale de la caché, sin red")
            check(covers_mod.find("Los Pericos", "Demoliendo Hoteles") is None and len(fetched) == 1,
                  "otro artista (iTunes devuelve a Charly): no se acepta, mejor sin tapa que una equivocada")
            fetched.clear()
            check(covers_mod.find("Los Pericos", "Demoliendo Hoteles") is None and not fetched,
                  "y eso también se recuerda (no se vuelve a preguntar)")
            raw = covers_mod.raw_bgra(path, 48)
            px = raw.read_bytes() if raw else b""
            mid = (24 * 48 + 24) * 4
            check(raw is not None and len(px) == 48 * 48 * 4 and px[mid:mid + 4] == b"\x00\x00\xff\xff" and px[3] == 0,
                  f"los píxeles BGRA para mpv (overlay-add), con las puntas redondeadas: {len(px)}")
            check(covers_mod.raw_bgra(path, 48) == raw, "convertida una vez, queda")

            # la actividad: cuando cambia lo que manda la emisora, saca la tapa vieja y busca la nueva
            covers_mod.find = lambda a, t: path if a == "Charly Garcia" else None  # type: ignore[assignment]
            rcmds.clear()
            await radio._on_metadata({"icy-title": "Charly Garcia - Demoliendo Hoteles"})
            await radio._cover_task
            raw480 = covers_mod.raw_bgra(path, 480)
            check(radio.cover_path == path and raw480 is not None
                  and ("script-message-to", "tele_cec_radio", "cover", str(raw480), "480", "480") in rcmds,
                  f"otro tema: la tapa va a la pantalla de la radio (480 px): {rcmds}")
            rcmds.clear()
            await radio._on_metadata({"icy-title": "Charly Garcia - Demoliendo Hoteles"})
            check(not rcmds and radio.cover_path == path, "el mismo tema otra vez: nada")
            await radio._on_metadata({"icy-title": "Los Pericos - Runaway"})
            await radio._cover_task
            check(rcmds == [("script-message-to", "tele_cec_radio", "nocover")] and radio.cover_path is None,
                  f"un tema sin tapa: se saca la de antes y queda sin tapa: {rcmds}")
            rcmds.clear()
            await radio._on_metadata({"icy-title": "Tanda publicitaria"})
            await radio._cover_task
            check(not rcmds and radio.cover_path is None, "sin «Artista - Tema» no se busca")
            check(radio.song_of({"ICY-TITLE": " x - y "}) == "x - y" and radio.song_of(None) == "", "icy-title, como venga")
            check(not activities_mod.RadioActivity({"stations": ["https://x.example/a"], "covers": False}).covers
                  and radio.covers, "[apps.radio] covers = false lo apaga")
        finally:
            covers_mod._fetch, covers_mod.find, covers_mod.LOG_DIR = real_fetch, real_find, real_cover_dir

        # en el controlador: teclas, menú, ayuda, de fondo con las fotos
        real_radio_act = ctl.activities["radio"]
        ctl.activities["radio"] = radio
        radio.running = lambda: True  # type: ignore[method-assign]
        ctl._foreground = "radio"
        try:
            act = ctl.cfg.action_for("radio", parse_key("FORWARD"))
            check(act == ("radio:next", "radio") and ctl.cfg.action_for("radio", parse_key("NUMBER2"))[0]
                  == "radio:station:2" and ctl.cfg.action_for("radio", parse_key("UP"))[0] == "radio:volume:+",
                  f"teclas: ⏭ otra radio, 1 – 9 una de la lista, ▲ volumen: {act}")
            radio_state["pos"] = 0
            start = len(log)
            await ctl.a_radio("next")
            await ctl.a_radio("station:1")
            check("ov:toast:Radio: otra.example:8000" in log[start:] and "ov:toast:Radio: Radio Uno" in log[start:]
                  and radio_state["pos"] == 0, f"radio:next y radio:station:N, con aviso: {log[start:]}")
            radio._song = "Fito - Mariposa"
            start = len(log)
            await ctl.a_radio("info")
            check(any(x.startswith("ov:toast:Radio Uno\nFito - Mariposa") for x in log[start:]),
                  f"azul: la radio y el tema que suena: {log[start:]}")
            ctx_before = ctl._ctx
            await ctl.a_radio("list")
            check(ctl._menu_open and ctl._menu_mode == "radio" and ctl._radio_back_closes,
                  "amarillo: la lista de radios encima de la Radio")
            await ctl.run_action("menu:back")
            check(not ctl._menu_open and ctl._ctx == ctx_before, "Atrás vuelve a la Radio (no al menú de actividades)")
            rows = help_rows(ctl, "radio")
            check(any(d == "Radio siguiente" and "Canal +" in k for k, d in rows)
                  and any(d == "Esa radio de la lista" and "1 – 9" in k for k, d in rows)
                  and not any("⏭" in k or "⏯" in k for k, _d in rows),
                  f"la ayuda tiene la página de la Radio (sin ⏭ ⏯, que el control no tiene): {rows}")
            from tele_cec.ui import help as help_mod2
            check(("radio", "Radio") in help_mod2.pages(ctl.cfg, {"radio"})
                  and not any(c == "radio" for c, _t in help_mod2.pages(ctl.cfg, {"netflix"})),
                  "la página de la ayuda, solo si está la Radio")
            check(ctl_activities.KEEP_PLAYING["fotos"] >= {"radio"} and ctl._local_background() is radio,
                  "con las fotos sigue sonando de fondo (como la música)")
            radio_state["pause"] = False
            ctl.music.running = lambda: False  # type: ignore[method-assign]
            await ctl._fotos_video(True)
            paused_by_video = radio_state["pause"]
            await ctl._fotos_video(False)
            check(paused_by_video is True and radio_state["pause"] is False,
                  "un video de las fotos la pausa, y al terminar sigue")

            # el menú: elegir Radio muestra la lista de emisoras
            radio_state["pos"] = 1
            ctl._menu_open = True
            await ctl._radio_open()
            items = ctl.overlay.items
            check(ctl._menu_mode == "radio" and [i["name"] for i in items] == ["Radio Uno", "otra.example:8000"]
                  and ctl._menu_sel == 1 and items[1]["current"] and items[1].get("badge") == "sonando",
                  f"Radio en el menú: la lista, con la que suena resaltada: {[(i['name'], i.get('badge')) for i in items]}")
            await ctl.a_menu("pick:1")
            check(radio_state["pos"] == 0 and not ctl._menu_open, "elegir otra emisora la cambia y cierra el menú")
            switched: list = []

            async def fake_switch(item):
                switched.append(item)
            real_menu_switch = ctl._menu_switch
            ctl._menu_switch = fake_switch  # type: ignore[method-assign]
            radio.running = lambda: False  # type: ignore[method-assign]
            ctl._foreground = None
            try:
                ctl._menu_open = True
                await ctl._radio_open()
                check(ctl.overlay.items[radio.last_index()].get("sub") == "la última que sonó"
                      and not any(i["current"] for i in ctl.overlay.items), "cerrada: resalta la última que sonó")
                await ctl.a_menu("pick:2")
                await ctl._macro_task
                check(radio.want_station == 1 and switched and switched[0]["id"] == "radio",
                      f"cerrada: elegir una la abre con esa: {radio.want_station} {switched}")
            finally:
                ctl._menu_switch = real_menu_switch  # type: ignore[method-assign]
                ctl._switching = False
                radio.want_station = None

            # OK sobre Radio en el menú: pone la última que sonó de una; el botoncito «Radios» (o ▶), la lista
            radio.running = lambda: False  # type: ignore[method-assign]
            radio.installed = lambda: True  # type: ignore[method-assign]
            ctl._foreground = None
            items_before, cols_before = ctl.cfg.menu.get("items"), ctl._menu_columns
            ctl.cfg.menu["items"] = ["netflix", "radio", "fotos"]
            ctl._menu_columns = 2
            ctl._menu_switch = fake_switch  # type: ignore[method-assign]
            switched.clear()
            try:
                check(radio.has_last() and radio.source() == ("Radio Uno (la última) · 2 radios", True),
                      f"en el menú, abajo de Radio, cuál es la última que sonó: {radio.source()}")
                await ctl.menu_open()
                row = next(i for i in ctl.overlay.items if i["id"] == "radio")
                check(row.get("action") == "Radios", f"con el botoncito «Radios»: {row}")
                await ctl._menu_choose(ctl.overlay.items.index(row))
                await ctl._macro_task
                check(switched and switched[0]["id"] == "radio" and ctl._menu_mode == "activities"
                      and radio.want_station is None, f"OK la abre de una, con la última (sin preguntar): {switched}")
                ctl._switching = False
                await ctl.menu_open(select_id="radio")
                ctl._menu_chip = True
                await ctl._menu_choose(ctl._menu_sel)
                check(ctl._menu_mode == "radio" and [i["name"] for i in ctl.overlay.items] == ["Radio Uno", "otra.example:8000"]
                      and ctl.overlay.items[0]["sub"] == "la última que sonó",
                      f"OK sobre el botoncito: la lista para elegir otra: {[i['name'] for i in ctl.overlay.items]}")
                out = await press("ROOT_MENU")
                check(ctl._menu_mode == "activities" and ctl._menu_open and out[-1].startswith("ov:show:"),
                      f"la tecla Menú vuelve al menú: {out}")
                radio._last_file().unlink()
                switched.clear()
                check(not radio.has_last() and radio.source()[0] == "Radio Uno · otra.example:8000",
                      "sin una última (nunca sonó): abajo de Radio, la lista")
                await ctl.menu_open(select_id="radio")
                await ctl._menu_choose(ctl._menu_sel)
                check(ctl._menu_mode == "radio" and not switched, "y OK abre la lista para elegir (no hay una «última»)")
                await ctl.menu_close()
            finally:
                ctl._menu_switch = real_menu_switch  # type: ignore[method-assign]
                ctl._switching = False
                ctl.cfg.menu["items"], ctl._menu_columns = items_before, cols_before
                radio.want_station = None
        finally:
            ctl.activities["radio"] = real_radio_act
            ctl._foreground = None
            del ctl.music.running
    finally:
        radio_mod.LOG_DIR = real_radio_dir

    bad = [t for ok, t in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} verificaciones OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
