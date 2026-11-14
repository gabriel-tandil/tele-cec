#!/usr/bin/env python3
"""Prueba de punta a punta contra una página que imita a Netflix.

Levanta un servidor HTTP local con tests/mock_netflix.html, abre Chromium
headless con DevTools y simula teclas CEC a través del Controller real.
Por defecto prueba los dos diseños del mock (el sitio actual, "nuevo", y el
anterior, "viejo", para cubrir los selectores de respaldo) con los dos navegadores
(Chromium por DevTools y Firefox por WebDriver BiDi) que estén instalados.

    python3 tests/e2e_mock.py [--layout nuevo|viejo|ambos] [--motor chromium|firefox|ambos] [--screens DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tele_cec import config as config_mod  # noqa: E402
from tele_cec.apps.netflix.browser import Browser
from tele_cec.apps.webprofile import find_binary  # noqa: E402
from tele_cec.controller import Controller  # noqa: E402
from tele_cec.keys import parse_key  # noqa: E402

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


def launch_cmd(browser: Browser, binary: str, profile: Path, url: str) -> list[str]:
    if browser.firefox:
        # Igual que en producción (user.js, arranque en about:blank, kiosko) más --headless.
        browser.prepare_profile()
        return browser.args(url)
    return [binary, "--headless=new", f"--remote-debugging-port={DEBUG_PORT}", f"--user-data-dir={profile}",
            "--window-size=1280,720", "--no-first-run", "--disable-gpu", *browser.perf_args(), url]


async def run(layout: str, engine: str, screens: Path | None) -> None:
    new = layout == "nuevo"
    print(f"\n===== {engine} · diseño {layout} =====")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="nfx-e2e-"))
    cfg_file = tmp / "config.toml"
    cfg_file.write_text(f"""
log_level = "info"
[browser]
engine = "{engine}"
debug_port = {DEBUG_PORT}
start_url = "{HOST}/browse"
launch = "never"
[browser.firefox]
profile = "{tmp / 'profile'}"
extra_args = ["--headless", "--window-size=1280,720"]
[netflix]
profiles_url = "{HOST}/ProfilesGate"
page_delay_ms = 0
[feedback]
key_echo = true
""")
    cfg = config_mod.load(cfg_file)
    browser = Browser(cfg.browser)
    cmd = launch_cmd(browser, browser.binary(), tmp / "profile", f"{HOST}/ProfilesGate?layout={layout}")
    chrome = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ctl = Controller(cfg, browser)
    try:
        for _ in range(60):
            if await browser.alive():
                break
            await asyncio.sleep(0.25)
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
                "(() => { const f = document.querySelector('.nfx-focused');"
                " return {log: window.__log || [], path: location.pathname,"
                " cur: f ? (f.getAttribute('aria-label') || f.textContent) : null,"
                " uia: f ? f.getAttribute('data-uia') : null}; })()")

        async def shot(name: str) -> None:
            if screens:
                (screens / f"{engine}-{layout}-{name}.png").write_bytes(await ctl.page.screenshot())

        async def last_log(prefix: str) -> list[str]:
            return [x for x in (await st())["log"] if x.startswith(prefix)]

        async def log_since(n: int) -> list[str]:
            return (await st())["log"][n:]

        async def wait_busy_off(n: int, timeout: float = 5.0) -> list[str]:
            """Espera a que el indicador se apague (después de haberse encendido) y devuelve el log desde n."""
            deadline = time.monotonic() + timeout
            while True:
                lg = await log_since(n)
                ons = [i for i, x in enumerate(lg) if x.startswith("busy-on")]
                if (ons and "busy-off" in lg[ons[-1]:]) or time.monotonic() > deadline:
                    return lg
                await asyncio.sleep(0.1)

        def busy_texts(lg: list[str]) -> list[str]:
            return [x[len("busy-on:"):] for x in lg if x.startswith("busy-on:")]

        if browser.firefox:
            check(browser.is_blank(cmd[-1]), f"Firefox abre primero la página negra: {cmd[-1][:40]}…")

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
            dur: getComputedStyle(document.querySelector('.sliderContent, .track')).transitionDuration,
            preview: getComputedStyle(document.querySelector('.billboard-preview')).visibility,
        })""")
        check(perf["reduced"], "prefers-reduced-motion activo (flag de Chromium / pref de Firefox)")
        check(perf["css"] and perf["dur"] == "0.001s", f"transiciones de Netflix acortadas: {perf['dur']}")
        check(perf["preview"] == "hidden", "avance automático del inicio oculto")
        if browser.firefox:
            seen = [x for x in (await st())["log"] if x.startswith("webdriver:")]
            check(bool(seen) and all(x == "webdriver:false" for x in seen),
                  f"navigator.webdriver oculto desde antes de los scripts de la página: {sorted(set(seen))}")

        print("Navegación espacial")
        n_nav = len((await st())["log"])
        await press("DOWN")
        s = await st()
        check(s["cur"] is not None and "Reproducir" in s["cur"], f"1.ª flecha muestra el foco inicial (billboard): {s['cur']!r}")
        echo = await ctl.cdp.evaluate("(document.getElementById('nfx-echo') || {}).textContent || ''")
        check(echo == "▼", f"eco de teclas (feedback.key_echo): {echo!r}")
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
        check(s["cur"] is not None and ("Reproducir" in s["cur"] or "Más info" in s["cur"]),
              f"arriba x2 vuelve al billboard (no a la cabecera fija): {s['cur']!r}")
        await press("UP")
        s = await st()
        check(s["cur"] in ("Inicio", "NETFLIX", "Netflix", "Series", "Películas"), f"arriba otra vez -> cabecera: {s['cur']!r}")
        await press("DOWN", "DOWN", "DOWN", "DOWN", "DOWN", "DOWN")
        await shot("3-scroll")
        s = await st()
        check(s["cur"] is not None and s["cur"].startswith(("Populares", "Nuevos", "Top")), f"bajando varias filas hace scroll: {s['cur']!r}")
        check(not busy_texts(await log_since(n_nav)), "las acciones rápidas (flechas, paginado) no muestran el indicador")
        hid = await ctl.cdp.evaluate("(() => { const f = document.querySelector('.nfx-focused'); "
                                     "f.style.visibility = 'hidden'; f.dataset.nfxTest = '1'; return f.getAttribute('aria-label'); })()")
        await asyncio.sleep(0.5)
        s = await st()
        check(s["cur"] != hid, f"si el elemento resaltado se oculta, el marco no queda colgado (sin tocar teclas): {hid} -> {s['cur']!r}")
        await ctl.cdp.evaluate("document.querySelector('[data-nfx-test]').style.visibility = ''")
        await asyncio.sleep(0.3)  # con reduce_effects hasta visibility tiene una transición (1 ms) que avanza por cuadros
        await press("LEFT")
        s = await st()
        name, _, num = hid.rpartition(" ")
        check(s["cur"] == f"{name} {int(num) - 1}",
              f"y la próxima tecla sigue desde donde estaba (◀ desde {hid}): {s['cur']!r}")

        print("Modal")
        await press("SELECT", pause=0.8)
        s = await st()
        check(any(x.startswith("card:") and x.endswith(":trusted") for x in s["log"]), "OK abre la ficha con clic real")
        focused_before = [x for x in s["log"] if x.startswith("card:")][-1].split(":")[1]
        in_modal = await ctl.cdp.evaluate("!!(document.querySelector('.nfx-focused') || document.body).closest('[role=dialog]')")
        check(s["cur"] is not None and "Reproducir" in s["cur"] and in_modal,
              f"al abrirse la ficha, el marco pasa solo a su «Reproducir» (no queda sobre la tarjeta tapada): {s['cur']!r}")
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

        print("Tecla PLAY en la lista y en la ficha")
        focused_href = "(document.querySelector('.nfx-focused') || document.body).getAttribute('href') || ''"
        href = await ctl.cdp.evaluate(focused_href)
        wid = href.split("=")[-1].split("/")[-1]
        n0 = len((await st())["log"])
        await press("PLAY", pause=0.3)
        lg = await wait_busy_off(n0)
        s = await st()
        check(bool(wid) and s["path"] == f"/watch/{wid}", f"PLAY sobre una tarjeta reproduce ese título: {href} -> {s['path']}")
        if new:
            check(any(x.startswith("card:") for x in lg), "(abriendo la ficha y apretando «Reproducir»)")
        texts = busy_texts(lg)
        check(any(t.startswith("Cargando «") for t in texts), f"indicador «Cargando «título»…» mientras carga: {texts}")
        check("video-playing" in lg and "busy-off" in lg[lg.index("video-playing"):],
              f"y se apaga recién cuando arranca el video: {[x for x in lg if x.startswith(('busy', 'video', 'watch'))]}")
        n0 = len((await st())["log"])
        await ctl.run_action("home")
        lg = await wait_busy_off(n0)
        check("Cargando el inicio…" in busy_texts(lg) and "busy-off" in lg,
              f"«inicio» muestra el indicador hasta que carga la página: {[x for x in lg if x.startswith('busy')]}")
        await press("DOWN")
        s = await st()
        check(s["cur"] is not None and "Reproducir" in s["cur"], f"el primer foco es «Reproducir» de la portada: {s['cur']!r}")
        await press("PLAY", pause=1.2)
        s = await st()
        check(s["path"].startswith("/watch/9"), f"PLAY sobre la portada la reproduce: {s['path']}")
        await ctl.navigate(f"{HOST}/browse")
        await press("DOWN", "DOWN")
        href = await ctl.cdp.evaluate(focused_href)
        wid = href.split("=")[-1].split("/")[-1]
        await press("SELECT", pause=0.8)
        check("modal-open" in " ".join((await st())["log"][-2:]), "OK abre la ficha")
        await press("PAUSE_PLAY_FUNCTION", pause=1.2)
        s = await st()
        check(bool(wid) and s["path"] == f"/watch/{wid}", f"Play/Pausa con la ficha abierta reproduce ese título: {s['path']}")
        await ctl.navigate(f"{HOST}/browse")
        await press("PLAY", pause=0.6)
        s = await st()
        check(s["path"] == "/browse", "PLAY sin nada resaltado no hace nada (solo avisa)")
        check(not await ctl.cdp.evaluate("!!document.querySelector('#nfx-busy.on')"), "no queda ningún indicador encendido")

        print("Seguir viendo + reproductor")
        await press("F2_RED", pause=1.5)
        s = await st()
        check(s["path"] == "/watch/80001", f"rojo (Netflix) con Netflix abierto = lo último de «Seguir viendo»: {s['path']}")
        check(await ctl.context() == "player", "contexto 'player'")
        await press("FORWARD", pause=0.6)
        check(bool(await last_log("skip-intro")), "⏭ salta la intro")
        await press("FORWARD", pause=0.3)
        await shot("5-nada-para-saltar")
        await press("FORWARD", pause=1.5)
        s = await st()
        check("next-episode" in s["log"] and s["path"] == "/watch/80002", f"⏭ x2 sin nada para saltar -> siguiente episodio: {s['path']}")
        await press("RIGHT", "RIGHT", pause=0.2)
        seeks = await last_log("seek:")
        check(seeks[-2:] == ["seek:70000", "seek:80000"], f"derecha x2 = +10 s acumulado: {seeks[-2:]}")
        await press("LEFT", pause=0.2)
        check((await last_log("seek:"))[-1] == "seek:70000", "izquierda = -10 s")
        await press("UP", "UP", pause=0.2)
        check((await last_log("vol:"))[-1] == "vol:0.7", "arriba x2 = volumen 70 %")
        if new:
            # Sin pista "none": "Desactivados" es la pista forced narrative y tiene que estar en el ciclo.
            await press("F1_BLUE", "F1_BLUE", "F1_BLUE", "F1_BLUE", pause=0.2)
            subs = await last_log("subs:")
            check(subs == ["subs:Inglés", "subs:Español", "subs:Portugués (Brasil)", "subs:Desactivados"],
                  f"azul cicla subtítulos y vuelve a «Desactivados» (forced): {subs}")
        else:
            await press("F1_BLUE", "F1_BLUE", "F1_BLUE", pause=0.2)
            subs = await last_log("subs:")
            check(subs == ["subs:Español", "subs:English", "subs:Off"], f"azul cicla subtítulos (sin forzados): {subs}")
        await press("F2_RED", pause=0.2)
        want = "audio:Inglés [Original]" if new else "audio:Español"
        check((await last_log("audio:"))[-1] == want, f"rojo cicla audio: {want}")
        await shot("6-player")
        await press("SELECT", pause=0.2)
        check((await st())["log"][-1] == "pause", "OK = pausa")
        await press("SELECT", pause=0.2)
        check((await st())["log"][-1] == "play", "OK otra vez = play")
        if new:
            # Los controles salen del DOM al ocultarse: el modo navegación tiene que despertarlos.
            await press("NUMBER0", pause=0.3)
            check(await ctl.context() == "player_menu", "0 activa el modo navegación")
            await asyncio.sleep(2.0)  # que se oculten los controles
            gone = await ctl.cdp.evaluate("!document.querySelector('[data-uia=control-nav-back]')")
            await press("RIGHT", pause=0.3)
            s = await st()
            first = s["uia"]
            check(gone and bool(first) and first.startswith("control-"),
                  f"con los controles ocultos, una flecha los muestra y enfoca uno: {first}")
            await press("RIGHT", pause=0.3)
            s = await st()
            check(bool(s["uia"]) and s["uia"].startswith("control-") and s["uia"] != first,
                  f"y otra flecha se mueve entre ellos: {first} -> {s['uia']}")
            await shot("6b-modo-navegacion")
            await press("EXIT", pause=0.3)
            check(await ctl.context() == "player", "Atrás sale del modo navegación")
        await ctl.cdp.evaluate("delete window.netflix")
        await press("RIGHT", pause=0.3)
        check((await st())["log"][-1] == "key:ArrowRight:trusted", "sin la API interna usa la tecla de Netflix (fallback)")
        await asyncio.sleep(2.0)  # controles ocultos: Atrás igual tiene que usar «volver», no el historial
        await press("EXIT", pause=1.2)
        s = await st()
        check(s["path"] == "/browse" and "exit-player" in s["log"], f"Atrás sale del reproductor con «volver»: {s['path']}")

        print("Perfiles desde el inicio + macro")
        await press("NUMBER0", pause=1.2)
        check(await ctl.context() == "profiles", "0 en el inicio abre ¿Quién está viendo?")
        await ctl.run_action(["profile:1", "wait:800", "continue_watching"])
        await asyncio.sleep(1.5)
        s = await st()
        check("profile:Gaby:trusted" in s["log"] and s["path"] == "/watch/80001", f"macro perfil 1 + seguir viendo: {s['path']}")
    finally:
        await ctl.disconnect()
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


async def main(layouts: list[str], engines: list[str], screens: Path | None) -> int:
    for engine in engines:
        for layout in layouts:
            start = len(results)
            try:
                await run(layout, engine, screens)
            except Exception as exc:  # noqa: BLE001
                check(False, f"la prueba se cortó: {type(exc).__name__}: {exc}")
            for i in range(start, len(results)):
                results[i] = (results[i][0], f"[{engine}/{layout}] {results[i][1]}")
    bad = [t for ok, t in results if not ok]
    for t in bad:
        print(f"  MAL: {t}")
    print(f"\n{len(results) - len(bad)}/{len(results)} verificaciones OK")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", choices=("nuevo", "viejo", "ambos"), default="ambos",
                    help="diseño del Netflix simulado (por defecto: los dos)")
    ap.add_argument("--motor", choices=("chromium", "firefox", "ambos"), default="ambos",
                    help="navegador (por defecto: los que estén instalados)")
    ap.add_argument("--screens", type=Path, help="guardar capturas en este directorio")
    a = ap.parse_args()
    if a.screens:
        a.screens.mkdir(parents=True, exist_ok=True)
    os.environ.pop("JOURNAL_STREAM", None)
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    layouts = ["nuevo", "viejo"] if a.layout == "ambos" else [a.layout]
    engines = ["chromium", "firefox"] if a.motor == "ambos" else [a.motor]
    for e in list(engines):
        if not find_binary("", e):
            print(f"(no encontré {e}: no lo pruebo)")
            if a.motor == "ambos":
                engines.remove(e)
    sys.exit(asyncio.run(main(layouts, engines, a.screens)))
