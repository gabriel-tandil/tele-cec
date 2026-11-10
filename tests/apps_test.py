#!/usr/bin/env python3
"""Prueba de la lógica de apps (colores = apps, Tidal con High Tide) con navegador y High Tide simulados.

No abre nada: verifica qué hace cada tecla según qué app está abierta.

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

from netflix_cec import config as config_mod  # noqa: E402
from netflix_cec.controller import Controller  # noqa: E402
from netflix_cec.keys import parse_key  # noqa: E402

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

    def __init__(self, log: list[str]):
        self.log = log
        self.on = False

    def installed(self) -> bool:
        return True

    def running(self) -> bool:
        return self.on

    async def launch(self) -> bool:
        self.log.append("music:launch")
        self.on = True
        return True

    async def resume(self) -> None:
        self.log.append("music:resume")

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

    async def my_tracks(self) -> bool:
        self.log.append("music:my_tracks")
        return True


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="nfx-apps-"))
    cfg_file = tmp / "config.toml"
    cfg_file.write_text('[browser]\nengine = "firefox"\nlaunch = "on_key"\n[feedback]\nbusy = false\n')
    cfg = config_mod.load(cfg_file)
    log: list[str] = []
    browser = FakeBrowser(log)
    ctl = Controller(cfg, browser)  # type: ignore[arg-type]
    browser.ctl = ctl
    ctl.music = FakeMusic(log)  # type: ignore[assignment]
    cw: list[str] = []

    async def fake_cw(arg: str) -> None:
        cw.append("continue_watching")
    ctl.a_continue_watching = fake_cw  # type: ignore[method-assign]

    async def press(*names: str) -> list[str]:
        start = len(log)
        for n in names:
            await ctl.handle_key(parse_key(n))
            ctl._released = True
            if ctl._macro_task:
                await ctl._macro_task
                ctl._macro_task = None
        return log[start:]

    print("Nada abierto")
    check(await ctl.context() == "offline", "sin navegador ni Tidal: contexto 'offline'")
    out = await press("SELECT")
    check(not any(x.startswith(("browser:", "music:")) for x in out), f"OK no abre nada: {out}")
    out = await press("F4_YELLOW")
    check(out[:2] == ["music:launch", "music:resume"], f"amarillo abre Tidal y sigue lo último: {out}")
    check(await ctl.context() == "tidal", "contexto 'tidal'")

    print("Tidal abierto")
    out = await press("F4_YELLOW")
    check(out == ["music:my_tracks"], f"amarillo otra vez = Mis canciones: {out}")
    out = await press("SELECT", "FORWARD", "BACKWARD", "UP", "DOWN", "F1_BLUE")
    check(out == ["mpris:play-pause", "mpris:next", "mpris:previous", "mpris:volume+", "mpris:volume-",
                  "boton:Agregar/Eliminar de favoritos"], f"OK, ⏭, ⏮, ▲, ▼, azul: {out}")
    out = await press("EXIT", "F3_GREEN")
    check(out == [], f"Atrás y verde no hacen nada en Tidal: {out}")
    out = await press("NUMBER0")
    check(await ctl.context() == "tidal_nav", "0 activa el modo navegación de Tidal")
    out = await press("DOWN", "SELECT", "EXIT")
    check(out == ["wtype:Down", "wtype:Return", "boton:Atrás"], f"en modo navegación: flechas, OK y Atrás van a la app: {out}")
    await press("NUMBER0")
    check(await ctl.context() == "tidal", "0 otra vez vuelve a los controles")
    check(ctl._holding_for_tidal(), "con Tidal abierto el navegador no se reabre solo (launch = always)")

    print("Tidal → Netflix")
    out = await press("F2_RED")
    check(out[:2] == ["music:quit", "browser:launch"], f"rojo cierra Tidal y abre Netflix: {out[:2]}")
    check(await ctl.context() == "browse" and ctl.current_app() == "netflix", "queda Netflix (contexto 'browse')")
    check(not ctl._holding_for_tidal(), "y el navegador vuelve a poder abrirse solo")

    print("Netflix abierto")
    await press("F2_RED")
    check(cw == ["continue_watching"], f"rojo con Netflix abierto = Seguir viendo: {cw}")
    out = await press("NUMBER0")
    check(any(x == "js:profiles" or x == "js:ctx" for x in out), "0 en el inicio va a perfiles")

    print("Netflix → Tidal")
    out = await press("F4_YELLOW")
    check("browser:close" in out and out.index("browser:close") < out.index("music:launch"),
          f"amarillo cierra el navegador y después abre Tidal: {[x for x in out if not x.startswith('js:')]}")
    check(await ctl.context() == "tidal", "queda Tidal")

    print("Standby de la TV con Tidal sonando")
    log.clear()
    await ctl._pause_if_playing()
    check(log == ["mpris:pause"], f"pausa la música: {log}")

    bad = [t for ok, t in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} verificaciones OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
