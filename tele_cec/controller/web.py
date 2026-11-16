"""Acciones del Navegador (apps/navegador): moverse por la página, abrir, volver, escribir con los números (T9), el
cuadro «Ir a», los sitios guardados y el video de la página."""

from __future__ import annotations

import logging

from .. import apps as activities_mod
from ..apps.navegador.t9 import MODE_TEXT
from ..apps.netflix.cdp import CDPClosed, CDPError

log = logging.getLogger("tele")

TEXT_HINT = "Escribí con los números · OK aceptar · Atrás salir del campo"


class WebMixin:
    def _web(self) -> activities_mod.WebActivity | None:
        w = self.activities.get("navegador")
        return w if isinstance(w, activities_mod.WebActivity) else None

    async def a_web(self, arg: str) -> None:
        """web:nav:up|down|left|right · ok · back · home · reload · go (el cuadro «Ir a») · url:<dirección> ·
        submit (Enter / ir) · t9:0-9 (escribir) · t9:mode (abc → ABC → 123) · key:<Tecla> (Backspace, ArrowLeft…) ·
        clear · scroll:up|down · play_pause|play|pause (el video de la página) · sites (la lista) · site:N."""
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
        if cmd == "nav":
            r = await w.js("nav", rest.lower() or "down")
            if r.get("label"):
                log.debug("Navegador: foco en %s", r["label"])
        elif cmd == "ok":
            r = await w.js("activate")
            if "x" in r and "y" in r:
                await w.click(r["x"], r["y"])
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
        elif cmd == "toast":
            await self.toast(rest)
        else:
            log.warning("web:%s desconocida", cmd)
