#!/usr/bin/env python3
"""Prueba de punta a punta contra una página que imita a Netflix.

Levanta un servidor HTTP local con tests/mock_netflix.html, abre Chromium
headless con DevTools y simula teclas CEC a través del Controller real.

    python3 tests/e2e_mock.py [--screens DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from netflix_cec import config as config_mod  # noqa: E402
from netflix_cec.browser import Browser, find_binary  # noqa: E402
from netflix_cec.cdp import endpoint_alive  # noqa: E402
from netflix_cec.controller import Controller  # noqa: E402
from netflix_cec.keys import parse_key  # noqa: E402

HTTP_PORT = 8765
DEBUG_PORT = 9333
HOST = f"http://www.netflix.com.localhost:{HTTP_PORT}"
MOCK = (Path(__file__).parent / "mock_netflix.html").read_bytes()

results: list[tuple[bool, str]] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/favicon"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(MOCK)

    def log_message(self, *args):
        pass


def check(cond: bool, text: str) -> None:
    results.append((bool(cond), text))
    print(f"  [{'OK ' if cond else 'MAL'}] {text}", flush=True)


async def main(screens: Path | None) -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="nfx-e2e-"))
    cfg_file = tmp / "config.toml"
    cfg_file.write_text(f"""
log_level = "info"
[browser]
debug_port = {DEBUG_PORT}
start_url = "{HOST}/browse"
launch = "never"
[netflix]
profiles_url = "{HOST}/ProfilesGate"
page_delay_ms = 650
""")
    cfg = config_mod.load(cfg_file)
    binary = find_binary()
    chrome = subprocess.Popen(
        [binary, "--headless=new", f"--remote-debugging-port={DEBUG_PORT}", f"--user-data-dir={tmp / 'profile'}",
         "--window-size=1280,720", "--no-first-run", "--disable-gpu", *Browser(cfg.browser).perf_args(),
         f"{HOST}/ProfilesGate"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            if await endpoint_alive(DEBUG_PORT):
                break
            await asyncio.sleep(0.25)
        ctl = Controller(cfg, Browser(cfg.browser))
        assert await ctl.connect_once(), "no conecta"
        await ctl.wait_ctx({"profiles"}, timeout=10)

        async def press(*names: str, pause: float = 0.45) -> None:
            for n in names:
                await ctl.handle_key(parse_key(n))
                ctl._released = True  # simula el 0x45 (botón soltado)
                if ctl._macro_task:
                    await ctl._macro_task
                    ctl._macro_task = None
                await asyncio.sleep(pause)

        async def st() -> dict:
            return await ctl.cdp.evaluate(
                "({log: window.__log || [], path: location.pathname,"
                " cur: (document.querySelector('.nfx-focused') || {}).textContent || null})")

        async def shot(name: str) -> None:
            if screens:
                r = await ctl.send("Page.captureScreenshot", {"format": "png"})
                (screens / f"{name}.png").write_bytes(base64.b64decode(r["data"]))

        async def last_log(prefix: str) -> list[str]:
            return [x for x in (await st())["log"] if x.startswith(prefix)]

        print("Perfiles")
        check(await ctl.context() == "profiles", "contexto 'profiles' en ¿Quién está viendo?")
        await press("NUMBER2", pause=1.5)
        s = await st()
        check("profile:Chicos:trusted" in s["log"], "número 2 elige el 2.º perfil con clic real")
        check(await ctl.context() == "browse", "después pasa a 'browse'")

        print("Rendimiento")
        perf = await ctl.cdp.evaluate("""({
            reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
            css: !!document.getElementById('nfx-perf'),
            dur: getComputedStyle(document.querySelector('.sliderContent')).transitionDuration,
            preview: getComputedStyle(document.querySelector('.billboard-preview')).visibility,
        })""")
        check(perf["reduced"], "Chromium con --force-prefers-reduced-motion")
        check(perf["css"] and perf["dur"] == "0.001s", f"transiciones de Netflix acortadas: {perf['dur']}")
        check(perf["preview"] == "hidden", "avance automático del inicio oculto")

        print("Navegación espacial")
        await press("DOWN")
        s = await st()
        check(s["cur"] is not None and "Reproducir" in s["cur"], f"1.ª flecha muestra el foco inicial (billboard): {s['cur']!r}")
        await shot("1-billboard")
        await press("DOWN")
        s = await st()
        check(s["cur"] == "Seguir 1", f"abajo -> primera tarjeta de la primera fila: {s['cur']!r}")
        await press("RIGHT", "RIGHT", "RIGHT", "RIGHT", "RIGHT")
        check((await st())["cur"] == "Seguir 6", "derecha x5 -> Seguir 6")
        await press("RIGHT", pause=1.0)
        s = await st()
        check("page:continueWatching:1" in s["log"] and s["cur"] == "Seguir 7",
              f"en el borde pasa de página y enfoca la siguiente: {s['cur']!r}")
        await shot("2-paginado")
        await press("LEFT", pause=1.0)
        s = await st()
        check("page:continueWatching:0" in s["log"] and s["cur"] == "Seguir 6", f"izquierda vuelve de página: {s['cur']!r}")
        await press("DOWN")
        s = await st()
        check(s["cur"] == "Tendencias 6", f"abajo mantiene la columna: {s['cur']!r}")
        await press("UP", "UP")
        s = await st()
        check(s["cur"] is not None and ("Reproducir" in s["cur"] or "Más información" in s["cur"]),
              f"arriba x2 vuelve al billboard (no a la cabecera fija): {s['cur']!r}")
        await press("UP")
        s = await st()
        check(s["cur"] in ("Inicio", "NETFLIX", "Series", "Películas"), f"arriba otra vez -> cabecera: {s['cur']!r}")
        await press("DOWN", "DOWN", "DOWN", "DOWN", "DOWN", "DOWN")
        await shot("3-scroll")
        s = await st()
        check(s["cur"] is not None and s["cur"].startswith(("Populares", "Nuevos", "Top")), f"bajando varias filas hace scroll: {s['cur']!r}")

        print("Modal")
        await press("SELECT", pause=0.8)
        s = await st()
        check(any(x.startswith("card:") and x.endswith(":trusted") for x in s["log"]), "OK abre la ficha con clic real")
        focused_before = [x for x in s["log"] if x.startswith("card:")][-1].split(":")[1]
        await press("DOWN")
        s = await st()
        check(s["cur"] is not None and "Reproducir" in s["cur"], f"el foco entra al modal por «Reproducir»: {s['cur']!r}")
        await press("DOWN")
        s = await st()
        check(s["cur"] is not None and ("Episodio" in s["cur"] or "Mi lista" in s["cur"]), f"y navega dentro del modal: {s['cur']!r}")
        await shot("4-modal")
        await press("EXIT", pause=0.6)
        s = await st()
        check("modal-close" in s["log"], "Atrás cierra el modal (Escape)")
        check(s["cur"] == focused_before, f"y el resaltado vuelve a la tarjeta: {s['cur']!r}")
        await press("RIGHT")
        s = await st()
        check(s["cur"] is not None and s["cur"] != focused_before and s["cur"].split()[0] == focused_before.split()[0],
              f"el foco vuelve a la fila de la tarjeta ({focused_before} -> {s['cur']!r})")

        print("Seguir viendo + reproductor")
        await press("F3_GREEN", pause=1.5)
        s = await st()
        check(s["path"] == "/watch/80001", f"verde abre lo último de «Seguir viendo»: {s['path']}")
        check(await ctl.context() == "player", "contexto 'player'")
        await press("F4_YELLOW", pause=0.6)
        check(bool(await last_log("skip-intro")), "amarillo salta la intro")
        await press("F4_YELLOW", pause=0.3)
        await shot("5-nada-para-saltar")
        await press("F4_YELLOW", pause=1.5)
        s = await st()
        check("next-episode" in s["log"] and s["path"] == "/watch/80002", f"amarillo x2 sin nada para saltar -> siguiente episodio: {s['path']}")
        await press("RIGHT", "RIGHT", pause=0.2)
        seeks = await last_log("seek:")
        check(seeks[-2:] == ["seek:70000", "seek:80000"], f"derecha x2 = +10 s acumulado: {seeks[-2:]}")
        await press("LEFT", pause=0.2)
        check((await last_log("seek:"))[-1] == "seek:70000", "izquierda = -10 s")
        await press("UP", "UP", pause=0.2)
        check((await last_log("vol:"))[-1] == "vol:0.7", "arriba x2 = volumen 70 %")
        await press("F1_BLUE", "F1_BLUE", "F1_BLUE", pause=0.2)
        check((await last_log("subs:")) == ["subs:Español", "subs:English", "subs:Off"], f"azul cicla subtítulos (sin forzados): {await last_log('subs:')}")
        await press("F2_RED", pause=0.2)
        check((await last_log("audio:"))[-1] == "audio:Español", "rojo cicla audio")
        await shot("6-player")
        await press("SELECT", pause=0.2)
        check((await st())["log"][-1] == "pause", "OK = pausa")
        await press("SELECT", pause=0.2)
        check((await st())["log"][-1] == "play", "OK otra vez = play")
        await ctl.cdp.evaluate("delete window.netflix")
        await press("RIGHT", pause=0.3)
        check((await st())["log"][-1] == "key:ArrowRight:trusted", "sin la API interna usa la tecla de Netflix (fallback)")
        await press("EXIT", pause=1.2)
        s = await st()
        check(s["path"] == "/browse", f"Atrás sale del reproductor: {s['path']}")

        print("Perfiles desde el inicio + macro")
        await press("F2_RED", pause=1.2)
        check(await ctl.context() == "profiles", "rojo en el inicio abre ¿Quién está viendo?")
        await ctl.run_action(["profile:1", "wait:800", "continue_watching"])
        await asyncio.sleep(1.5)
        s = await st()
        check("profile:Gaby:trusted" in s["log"] and s["path"] == "/watch/80001", f"macro perfil 1 + seguir viendo: {s['path']}")
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [t for ok, t in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} verificaciones OK")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--screens", type=Path, help="guardar capturas en este directorio")
    a = ap.parse_args()
    if a.screens:
        a.screens.mkdir(parents=True, exist_ok=True)
    os.environ.pop("JOURNAL_STREAM", None)
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main(a.screens)))
