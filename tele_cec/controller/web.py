"""Acciones del Navegador (apps/navegador): moverse por la página, abrir, volver, escribir con los números (T9), el
cuadro «Ir a», el modo mouse, los sitios guardados y el video de la página."""

from __future__ import annotations

import asyncio
import logging
import time

from .. import apps as activities_mod
from ..apps.navegador.t9 import MODE_TEXT
from ..apps.netflix.cdp import CDPClosed, CDPError

log = logging.getLogger("tele")

TEXT_HINT = "Escribí con los números · OK aceptar · Atrás salir del campo"
MOUSE_HINT = "Modo mouse · las flechas mueven la flecha (seguido: más rápido) · OK = clic · CC = salir"
MOUSE_OFF_HINT = "Modo mouse apagado · las flechas van de enlace en enlace"


# Botón mantenido sin aviso de «soltado» (0x45): si no llega otra repetición en este tiempo, se soltó.
HOLD_GAP_S = 0.55
# Tele que no repite la tecla mantenida pero avisa al soltarla: si sigue apretada pasado este tiempo, se desliza
# (hasta que se suelte, o como mucho GLIDE_MAX_S, por si el aviso se pierde).
GLIDE_AFTER_S = 0.4
GLIDE_MAX_S = 6.0
MOUSE_DIRS = ("up", "down", "left", "right")


class WebMixin:
    def _init_web(self) -> None:
        self._web_glide: asyncio.Task | None = None   # la flecha deslizándose (botón mantenido en modo mouse)
        self._web_glide_dir = ""

    def _web_stop_glide(self) -> None:
        if self._web_glide is not None and not self._web_glide.done():
            self._web_glide.cancel()
        self._web_glide = None

    def _web_still_held(self) -> bool:
        return not self._released and time.monotonic() - self._last_key[1] < HOLD_GAP_S

    async def _web_glide_later(self, w: activities_mod.WebActivity, sub: str, key: tuple) -> None:
        """Un toque de flecha en una tele que avisa al soltar: si pasado un ratito sigue apretada, se desliza."""
        await asyncio.sleep(GLIDE_AFTER_S)
        if self._released or self._last_key[0] != key[0] or not w.mouse:  # (otro toque ya la habría cancelado)
            return
        t0 = time.monotonic()
        log.info("Navegador: flecha mantenida (sin repeticiones de la tele): se desliza hasta que se suelte")

        def held() -> bool:
            if self._last_key[0] != key[0] or time.monotonic() - t0 > GLIDE_MAX_S:
                return False
            return not self._released
        await w.mouse_glide(sub, held)

    def _web(self) -> activities_mod.WebActivity | None:
        w = self.activities.get("navegador")
        return w if isinstance(w, activities_mod.WebActivity) else None

    async def a_web(self, arg: str) -> None:
        """web:nav:up|down|left|right · ok · back · home · reload · go (el cuadro «Ir a») · url:<dirección> ·
        submit (Enter / ir) · t9:0-9 (escribir) · t9:mode (abc → ABC → 123) · key:<Tecla> (Backspace, ArrowLeft…) ·
        clear · scroll:up|down · play_pause|play|pause (el video de la página) · sites (la lista) · site:N ·
        mouse (prender / apagar el modo mouse) · mouse:up|down|left|right · mouse:click · save (guardar la página)."""
        w = self._web()
        if w is None or not w.running():
            log.info("El Navegador no está abierto: no ejecuto web:%s", arg)
            return
        cmd, _, rest = arg.strip().partition(":")
        try:
            await self._web_cmd(w, cmd.lower(), rest)
        except CDPClosed:
            log.warning("Navegador: se perdió la conexión con la página (web:%s)", arg)
        except CDPError as exc:
            log.warning("Navegador: web:%s: %s", arg, exc)

    async def _web_cmd(self, w: activities_mod.WebActivity, cmd: str, rest: str) -> None:
        if w.mouse:
            # Modo mouse, también escribiendo: las flechas siempre mueven la flecha; OK hace clic (escribiendo, si
            # la flecha no se movió desde la última letra, OK busca: Enter).
            if cmd == "nav" or (cmd == "key" and rest in ("ArrowLeft", "ArrowRight")):
                cmd, rest = "mouse", rest if cmd == "nav" else rest.removeprefix("Arrow").lower()
            elif cmd == "ok" or (cmd == "submit" and w.mouse_moved):
                cmd, rest = "mouse", "click"
        if not (cmd == "mouse" and rest.lower() in MOUSE_DIRS):
            self._web_stop_glide()  # cualquier otra tecla frena la flecha
        if cmd == "nav":
            r = await w.js("nav", rest.lower() or "down")
            if r.get("key"):  # las sugerencias del buscador: con flechas de verdad
                w.t9.reset()
                await w.key_press(r["key"])
            if r.get("label"):
                log.debug("Navegador: foco en %s", r["label"])
        elif cmd == "ok":
            r = await w.js("activate")
            if "x" in r and "y" in r:
                await w.click(r["x"], r["y"])
                await w.click_landed()
            if r.get("text"):
                w.t9.reset()
                await self.toast(TEXT_HINT, 2500)
            if r.get("label"):
                log.info("Navegador: %s «%s»", "escribir en" if r.get("text") else "abro", r["label"])
        elif cmd == "back":
            w.t9.reset()
            r = await w.js("back")
            if r.get("done") == "none":
                await self.toast("Es la primera página · azul = inicio · amarillo = sitios", 2500)
        elif cmd == "home":
            await w.navigate(w.home_url())
        elif cmd == "reload":
            if await w._ensure_page():
                await w.page.reload()
        elif cmd == "go":
            w.t9.reset()
            await w.js("go", rest)
        elif cmd == "url":
            await w.navigate(rest)
        elif cmd == "submit":
            w.t9.reset()
            r = await w.js("submit")
            if "go" in r:
                if r["go"]:
                    await w.go(r["go"])
                else:
                    await self.toast("Escribí una dirección o qué buscar (con los números)", 2500)
            elif r.get("enter"):
                await w.key_press("Enter")
        elif cmd == "t9":
            w.mouse_moved = False
            if rest.lower() == "mode":
                await self.toast(MODE_TEXT[w.t9.next_mode()], 1500)
                return
            ch, replace, letters, idx = w.t9.press(rest)
            if ch:
                await w.js("type", ch, replace, letters, idx)
        elif cmd == "key":
            w.t9.reset()
            await w.key_press(rest)
        elif cmd == "clear":
            w.t9.reset()
            await w.js("clear")
        elif cmd == "scroll":
            await w.js("scroll", rest.lower() or "down")
        elif cmd in ("play_pause", "play", "pause"):
            r = await w.js("playPause", "" if cmd == "play_pause" else cmd)
            if r.get("none"):
                await self.toast("No hay ningún video en esta página", 2000)
        elif cmd == "sites":
            await self.sites_open_from_app()
        elif cmd == "site":
            try:
                index = int(rest) - 1
            except ValueError:
                log.warning("web:site:%s inválido (el número del sitio, desde 1)", rest)
                return
            if 0 <= index < len(w.bookmarks):
                await self.toast(w.bookmarks[index]["name"], 1500)
                await w.navigate(w.bookmarks[index]["url"])
            else:
                await self.toast(f"No hay un sitio {index + 1} (ver [apps.navegador] bookmarks)", 2500)
        elif cmd == "mouse":
            await self._web_mouse(w, rest.lower())
        elif cmd == "save":
            await self.web_save_page()
        elif cmd == "toast":
            await self.toast(rest)
        else:
            log.warning("web:%s desconocida", cmd)

    async def _web_mouse(self, w: activities_mod.WebActivity, sub: str) -> None:
        if sub in ("", "toggle", "on", "off"):
            on = (not w.mouse) if sub in ("", "toggle") else sub == "on"
            if on:
                await w.mouse_on()
                await self.toast(MOUSE_HINT, 3000)
            else:
                await w.mouse_off()
                await self.toast(MOUSE_OFF_HINT, 2000)
            log.info("Navegador: modo mouse %s", "prendido" if on else "apagado")
        elif sub in MOUSE_DIRS:
            if not w.mouse:
                await w.mouse_on()
            glide = self._web_glide
            if glide is not None and not glide.done():
                if self._key_held and self._web_glide_dir == sub:
                    return  # otra repetición del botón mantenido: la flecha ya se está deslizando
                self._web_stop_glide()
            loop = asyncio.get_running_loop()
            if self._key_held:
                # Mantenido: se desliza sola (cada vez más rápido) hasta que se suelte el botón.
                self._web_glide_dir = sub
                self._web_glide = loop.create_task(w.mouse_glide(sub, self._web_still_held))
            else:
                await w.mouse_move(sub)
                if self._release_works:  # esta tele avisa al soltar: si no lo suelta, se desliza
                    self._web_glide_dir = sub
                    self._web_glide = loop.create_task(self._web_glide_later(w, sub, self._last_key))
        elif sub == "click":
            r = await w.mouse_click()
            if r.get("text"):
                w.t9.reset()
                await self.toast(TEXT_HINT, 2500)
            log.info("Navegador: clic en %s,%s%s", r.get("x"), r.get("y"), f" «{r['label']}»" if r.get("label") else "")
        else:
            log.warning("web:mouse:%s desconocida (up, down, left, right, click, on, off)", sub)

    async def web_save_page(self) -> int:
        """Guarda la página que se ve en los sitios. Devuelve su lugar en la lista (-1: no se pudo)."""
        w = self._web()
        if w is None or not w.running():
            return -1
        url, title = await w.page_info()
        if not activities_mod.navegador.allowed_url(url):
            await self.toast("Esta página no se puede guardar (solo direcciones web)", 2500)
            return -1
        before = w.saved_index(url)
        index = w.add_bookmark(url, title)
        if index < 0:
            await self.toast("No pude guardar el sitio", 2500)
        elif before >= 0:
            await self.toast(f"Ya estaba guardado: «{w.bookmarks[index]['name']}» (sitio {index + 1})", 2500)
        else:
            num = f" · {index + 1} lo abre" if index < 9 else ""
            await self.toast(f"Guardado: «{w.bookmarks[index]['name']}»{num}", 2500)
        return index
