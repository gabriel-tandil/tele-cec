"""Reposo: qué está abierto (para restaurarlo), cerrar todo al entrar y qué hacer al volver."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

from .. import apps as activities_mod
from ..apps.netflix.cdp import CDPError
from .activities import KEEP_PLAYING

log = logging.getLogger("tele")


class SleepMixin:
    async def snapshot(self) -> dict[str, Any]:
        """Qué está abierto, para restaurarlo al volver del reposo."""
        cur = self.current_app()
        if not cur:
            return {}
        state: dict[str, Any] = {"app": cur}
        if cur == "netflix" and self.page:
            try:
                state["url"] = (await self.js("ctx")).get("url") or ""
            except CDPError:
                pass
            if self._last_profile:
                state["profile"] = self._last_profile
        act = self.activities.get(cur)
        if isinstance(act, activities_mod.MediaActivity) and act.current_folder:
            state["folder"] = str(act.current_folder)
        if isinstance(act, activities_mod.VideosActivity):
            state.update(await act.where())
        if cur in KEEP_PLAYING and self.music.running():
            state["music"] = True  # Tidal sonando de fondo con las fotos
        return state

    async def sleep_apps(self) -> None:
        """Al entrar en reposo: pausar lo que suena y cerrar todo (se restaura al volver)."""
        if self._menu_open:
            await self.menu_close()
        await self._pause_if_playing()
        for ident, act in self.activities.items():
            if await act.active():
                log.info("Reposo: cierro %s", act.name)
                await act.close()
        self._foreground = None  # se cerraron a propósito: que no parezca que se cerraron solas
        await self.overlay.hide()

    async def restore(self, state: dict[str, Any], activate: bool = False) -> None:
        """Al volver del reposo: el menú de actividades o, si se guardó qué había ([power] resume_apps), abrirlo
        en pausa (listo para seguir con ⏯).

        activate: anunciarse como fuente activa (la TV cambia a la Pi). Solo si se despertó con el control:
        si se prendió la TV para ver otra cosa, no se le cambia la entrada.
        """
        if activate and self.source:
            await self.source.activate_source()
        app = state.get("app")
        if not app or app not in self.activities:
            self.power.last_activity = time.monotonic()
            await self.menu_open()
            return
        name = self.activities[app].name
        self._switching = True
        self._no_activate = not activate
        await self.overlay.busy(f"Volviendo a {name}…")
        try:
            if app == "netflix":
                await self._restore_netflix(state.get("url") or "", state.get("profile") or "")
            elif app == "tidal":
                if await self.activities["tidal"].open(resume=False):
                    self._foreground = "tidal"
            elif app in ("videos", "fotos"):
                if state.get("music"):
                    await self.activities["tidal"].open(resume=False)
                ok = await self.switch_to(app, state.get("folder"))
                if ok and app == "videos":
                    await self._restore_video(state)
            else:
                await self.switch_to(app)
        except Exception:  # noqa: BLE001
            log.exception("No pude restaurar %s", name)
        finally:
            self._switching = False
            self._no_activate = False
            self.power.last_activity = time.monotonic()  # la cuenta empieza cuando terminó de volver
            await self.overlay.hide()
        await self.toast(f"{name} en pausa · ⏯ para seguir" if app in ("netflix", "videos") else f"Volviste a {name}",
                         3500)

    async def _page_ctx(self, timeout: float = 15.0, unwanted: str = "") -> str:
        """Contexto que informa la página (sin pasar por context(): durante la restauración dice «menu»)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.page:
            try:
                c = (await self.js("ctx")).get("ctx") or ""
            except CDPError:
                c = ""
            if c and c != unwanted:
                return c
            await asyncio.sleep(0.5)
        return ""

    async def _restore_profile(self, profile: str) -> None:
        """En «¿Quién está viendo?»: elegir el perfil que se estaba usando (o netflix.default_profile)."""
        names = [str(n).strip() for n in (await self.js("profiles")).get("names") or []]
        idx = next((i + 1 for i, n in enumerate(names) if profile and n.lower() == profile.lower()), 0)
        if not idx:
            idx = int(self.cfg.netflix.get("default_profile", 0) or 0)
        if not idx:
            log.info("Reposo: Netflix pide perfil y no sé cuál era: queda para elegir")
            return
        r = await self.js("profile", idx)
        if r.get("none"):
            return
        log.info("Reposo: vuelvo al perfil %s", r.get("name") or idx)
        await self.apply_click(r)
        await self._page_ctx(15, unwanted="profiles")

    async def _restore_netflix(self, url: str, profile: str = "") -> None:
        if not await self.switch_to("netflix"):
            return
        if await self._page_ctx(20) == "profiles":
            await self._restore_profile(profile)
        if url and "netflix.com" in url and urlparse(url).path.rstrip("/") not in ("", "/browse"):
            await self.navigate(url)
        if "/watch/" in url:
            # El reproductor arranca solo: se pausa apenas empieza (Netflix recuerda el minuto). Se le pregunta a
            # la página directamente: durante la restauración context() responde «menu».
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and self.page:
                try:
                    r = await self.js("ctx")
                except CDPError:
                    r = {}
                if r.get("ctx") == "player":
                    await asyncio.sleep(1.0)  # que el video arranque, si no la pausa no toma
                    await self.js("pause")
                    break
                await asyncio.sleep(0.5)

    async def _restore_video(self, state: dict[str, Any]) -> None:
        v = self.activities["videos"]
        assert isinstance(v, activities_mod.VideosActivity)
        await asyncio.sleep(2)  # que VLC termine de abrir y publique su interfaz de comandos
        if state.get("file"):
            await v.go_to(state["file"], int(state.get("seconds") or 0))
        await asyncio.sleep(0.5)
        await v.mpris.ctl("pause")
