"""Acciones sobre Netflix: navegar, abrir, reproducir, perfiles, «Seguir viendo»…"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

log = logging.getLogger("tele")

# Tope de la espera automática a que una fila termine de pasar de página (netflix.page_delay_ms = 0).
PAGE_WAIT_MAX_MS = 1200


def loading_text(title: str) -> str:
    return f"Cargando «{title}»…" if title else "Cargando el video…"


def is_home(path: str) -> bool:
    """El inicio de Netflix: /browse, o /Kids en los perfiles infantiles."""
    return path.rstrip("/").lower() in ("", "/browse", "/kids")


class NetflixMixin:
    def _init_netflix(self) -> None:
        self._smart_none_at = 0.0
        self._last_profile = ""        # perfil de Netflix elegido último (para volver a él después del reposo)

    async def a_nav(self, arg: str) -> None:
        direction = arg.lower() or "down"
        if direction not in ("up", "down", "left", "right"):
            log.warning("nav:%s inválido (up/down/left/right)", arg)
            return
        if self._ctx == "player_menu":
            await self.wake_controls()
        r = await self.js("nav", direction)
        if r.get("paged"):
            delay = int(self.cfg.netflix.get("page_delay_ms", 0) or 0)
            if delay > 0:
                await asyncio.sleep(delay / 1000)
                r = await self.js("afterPage", direction)
            else:
                # La página espera sola a que la fila termine de moverse (sin animaciones, unos 50-100 ms).
                r = await self.js("afterPage", direction, PAGE_WAIT_MAX_MS)
                log.debug("Fila lista en %s ms", r.get("waited"))
        if r.get("label"):
            log.debug("Foco: %s", r["label"])

    async def a_ok(self, arg: str) -> None:
        if self._ctx == "player_menu":
            await self.wake_controls()
        r = await self.js("activate")
        if r.get("focused") is not None:
            return  # primera pulsación: solo muestra el resaltado
        await self.apply_click(r)
        if r.get("label"):
            log.info("Clic en: %s", r["label"])
            if self._ctx == "profiles":
                self._last_profile = str(r["label"]).strip()

    async def a_back(self, arg: str) -> None:
        if self._ctx in ("player", "player_menu"):
            # El botón "volver" del reproductor solo existe mientras se ven los controles; sin él,
            # back() recurre a history.back(), que puede llevar al episodio anterior.
            await self.wake_controls()
        r = await self.js("back")
        if r.get("done") == "none" and self._ctx in ("browse", "profiles"):
            # El inicio de Netflix, arriba de todo: no hay a dónde volver, cierra Netflix y vuelve al menú.
            await self.a_close("netflix")
            return
        if r.get("key"):
            await self.key(r["key"])
        if r.get("restore"):
            # La página espera sola a que el modal termine de cerrarse (tope 800 ms).
            r = await self.js("restore", 800)
            log.debug("Modal cerrado en %s ms", r.get("waited"))

    async def a_smart_skip(self, arg: str) -> None:
        r = await self.js("smart")
        if not r.get("none"):
            await self.apply_click(r)
            log.info("Botón inteligente: %s", r.get("label", "?"))
            await self.toast(r.get("label") or "Listo", 1200)
            self._smart_none_at = 0.0
            return
        if r.get("player"):
            now = time.monotonic()
            if now - self._smart_none_at < self.cfg.netflix.get("smart_double_press_ms", 4000) / 1000:
                self._smart_none_at = 0.0
                await self.a_next_episode("")
            else:
                self._smart_none_at = now
                await self.toast("Nada para saltar · otra vez: siguiente episodio", 2500)
        else:
            await self.toast("Nada para saltar")

    async def a_next_episode(self, arg: str) -> None:
        r = await self.js("nextEpisode")
        if r.get("none"):
            await self.wiggle()  # mostrar los controles
            await asyncio.sleep(0.6)
            r = await self.js("nextEpisode")
        if r.get("none"):
            await self.toast("No encontré «Siguiente episodio»")
        else:
            log.info("Siguiente episodio")
            await self.toast("Siguiente episodio", 1200)

    async def a_play_pause(self, arg: str) -> None:
        if self._ctx == "browse":
            await self.play_selected()
            return
        await self._fallback(await self.js("playPause"))

    async def a_play(self, arg: str) -> None:
        if self._ctx == "browse":
            await self.play_selected()
            return
        await self._fallback(await self.js("play"))

    async def play_selected(self) -> bool:
        """En la lista o en una ficha: reproducir el título resaltado (o el de la ficha)."""
        r = await self.js("playSelected")
        if r.get("none"):
            await self.toast("No encontré «Reproducir» en la ficha" if r.get("modal")
                             else "Elegí un título con las flechas")
            return False
        title = r.get("title") or ""
        log.info("Reproducir: %s", title or r.get("label") or "?")
        if r.get("href"):
            await self.busy(loading_text(title), until="playing", ttl=20)
            await self.navigate(r["href"])
            return True
        if r.get("opened"):
            return await self.open_and_play(r)
        await self.busy(loading_text(title), until="playing", ttl=20)
        await self.apply_click(r)
        return True

    async def open_and_play(self, r: dict, timeout: float = 5.0) -> bool:
        """Abre la ficha de una tarjeta y aprieta su botón «Reproducir» apenas aparece."""
        title = r.get("title") or ""
        await self.busy(f"Abriendo «{title}»…" if title else "Abriendo la ficha…")
        await self.apply_click(r)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            pb = await self.js("playButton")
            if not pb.get("none"):
                await asyncio.sleep(0.2)  # que termine la animación de apertura
                pb = await self.js("playButton")
                if not pb.get("none"):
                    await self.busy(loading_text(title), until="playing", ttl=20)
                    await self.apply_click(pb)
                    return True
        log.warning("La ficha no mostró el botón «Reproducir»")
        await self.toast("No encontré «Reproducir» en la ficha")
        return False

    async def a_pause(self, arg: str) -> None:
        await self._fallback(await self.js("pause"))

    async def a_seek(self, arg: str) -> None:
        default = float(self.cfg.netflix.get("seek_seconds", 10))
        if arg in ("+", ""):
            sec = default
        elif arg == "-":
            sec = -default
        else:
            sec = float(arg)
        await self._fallback(await self.js("seek", sec))

    async def a_volume(self, arg: str) -> None:
        step = float(self.cfg.netflix.get("volume_step", 0.1))
        delta = step if arg in ("+", "") else -step if arg == "-" else float(arg)
        await self._fallback(await self.js("volume", delta))

    async def a_mute(self, arg: str) -> None:
        await self._fallback(await self.js("mute"))

    async def a_subtitles(self, arg: str) -> None:
        r = await self.js("subtitles")
        if r.get("name"):
            log.info("Subtítulos: %s", r["name"])

    async def a_audio(self, arg: str) -> None:
        r = await self.js("audio")
        if r.get("name"):
            log.info("Audio: %s", r["name"])

    async def a_fullscreen(self, arg: str) -> None:
        await self.key("f")

    async def a_nav_mode(self, arg: str) -> None:
        r = await self.js("navMode")
        if r.get("navMode"):
            await self.wiggle()

    async def a_type(self, arg: str) -> None:
        for ch in arg:
            await self.key(ch)

    async def a_key(self, arg: str) -> None:
        try:
            await self.key(arg)
        except ValueError as exc:
            log.warning("%s", exc)

    async def a_reload(self, arg: str) -> None:
        await self.busy("Recargando…", until="page", nav=True)
        await self._page().reload()

    async def a_my_list(self, arg: str) -> None:
        await self.busy("Abriendo Mi lista…", until="page", nav=True)
        await self.navigate(self.cfg.netflix.get("my_list_url") or "https://www.netflix.com/browse/my-list")

    async def a_my_list_toggle(self, arg: str) -> None:
        """♥ (1): agrega o quita de Mi lista el título resaltado, el de la ficha abierta o el que se está viendo."""
        r = await self.js("myList")
        if r.get("none"):
            await self.toast("No encontré «Mi lista» en la ficha" if r.get("modal")
                             else "Elegí un título con las flechas", 2000)
            return
        title = str(r.get("title") or "")
        if r.get("frame"):
            await self._my_list_frame(str(r.get("id") or ""), title)
            return
        opened = False
        if r.get("open"):
            # Una tarjeta de la fila: se abre su ficha, que es la que tiene el botón.
            await self.apply_click(r)
            opened = True
            deadline = time.monotonic() + 5
            while True:
                await asyncio.sleep(0.3)
                r = await self.js("myList")
                if r.get("modal") and not r.get("none"):
                    break
                if time.monotonic() > deadline:
                    log.warning("Mi lista: la ficha de «%s» no mostró el botón", title or "?")
                    await self.toast("No encontré «Mi lista» en la ficha", 2000)
                    return
            if r.get("frame"):
                await self._my_list_frame(str(r.get("id") or ""), title)
                return
        was = bool(r.get("inList"))
        await self.apply_click(r)
        now = was
        for _ in range(8):  # Netflix cambia el botón enseguida (antes de que conteste el servidor)
            await asyncio.sleep(0.25)
            st = await self.js("myListState")
            if st.get("none"):
                break
            now = bool(st.get("inList"))
            if now != was:
                break
        await self._my_list_done(title, now, changed=now != was)
        if opened:
            await self.key("Escape")
            await self.js("restore", 800)

    async def _my_list_frame(self, video_id: str, title: str) -> None:
        """En el reproductor (o una tarjeta que lleva directo al video): la ficha en un marco invisible."""
        await self.toast("Mi lista…", 6000)
        r = await self.js("myListFrame", video_id, timeout=30)
        log.info("Mi lista (marco invisible, título %s): %s", video_id or "?", r)
        if r.get("done"):
            await self._my_list_done(title, bool(r.get("inList")), changed=not r.get("unchanged"))
        else:
            await self.toast("No pude cambiar Mi lista desde acá: probá desde la ficha", 2500)

    async def _my_list_done(self, title: str, in_list: bool, changed: bool) -> None:
        if not changed:
            await self.toast("No pude cambiar Mi lista", 2000)
            return
        name = f"«{title}» " if title and len(title) <= 40 else ""
        log.info("Mi lista: %s%s", name, "agregado" if in_list else "quitado")
        await self.toast(f"♥ {name}en Mi lista" if in_list else f"{name}fuera de Mi lista" if name
                         else "Fuera de Mi lista", 2000)

    async def a_home(self, arg: str) -> None:
        await self.busy("Cargando el inicio…", until="page", nav=True)
        await self.navigate(self.cfg.browser.get("start_url"))

    async def a_launch(self, arg: str) -> None:
        """Abrir Netflix. Si hay otra actividad abierta, es lo mismo que elegir Netflix en el menú."""
        if not self._switching and any(a.running() for k, a in self.activities.items() if k != "netflix"):
            await self.switch_to("netflix")
            return
        await self.launch_browser()

    async def a_profiles(self, arg: str) -> None:
        if await self.context() == "profiles":
            return
        await self.navigate(self.cfg.netflix.get("profiles_url"))
        if not await self.wait_ctx({"profiles"}, timeout=10):
            log.warning("No apareció la pantalla de perfiles (revisá netflix.profiles_url)")
            await self.toast("No encontré la pantalla de perfiles")

    async def a_profile(self, arg: str) -> None:
        try:
            n = int(arg)
        except ValueError:
            log.warning("profile:%s inválido (número de perfil)", arg)
            return
        if await self.context() != "profiles":
            await self.a_profiles("")
        r = await self.js("profile", n)
        if r.get("none"):
            log.warning("No hay perfil %d (encontré %s)", n, r.get("count", 0))
            return
        name = r.get("name") or ""
        await self.busy(f"Entrando como {name}…" if name else "Entrando al perfil…")
        await self.apply_click(r)
        log.info("Perfil elegido: %d (%s)", n, name or "?")
        if name:
            self._last_profile = name
        if await self.wait_ctx(unwanted={"profiles"}, timeout=10):
            await self.busy("Cargando Netflix…", until="page")

    async def a_continue_watching(self, arg: str) -> None:
        """Rojo: lo último de «Seguir viendo» (en los perfiles infantiles, «¿Seguimos, …?»)."""
        start_url = self.cfg.browser.get("start_url")
        default_profile = int(self.cfg.netflix.get("default_profile", 0) or 0)
        ctx = await self.context()
        if ctx == "profiles" and not default_profile:
            await self.toast("Elegí un perfil (números o flechas + OK)", 2500)
            return
        if ctx != "profiles" and (ctx in ("player", "player_menu")
                                  or not is_home(urlparse((await self.js("ctx")).get("url", "")).path)):
            await self.navigate(start_url)

        deadline = time.monotonic() + 20
        attempt = 0
        last: dict = {}
        while time.monotonic() < deadline:
            ctx = await self.context()
            if ctx == "profiles":
                if not default_profile:
                    await self.toast("Elegí un perfil (números o flechas + OK)", 2500)
                    return
                r = await self.js("profile", default_profile)
                if not r.get("none"):
                    await self.busy(f"Entrando como {r.get('name')}…" if r.get("name") else "Entrando al perfil…")
                    await self.apply_click(r)
                    log.info("Perfil por defecto: %s", r.get("name", default_profile))
                    await self.wait_ctx(unwanted={"profiles"}, timeout=10)
                    continue
            r = await self.js("cw", attempt >= 2)  # las filas de más abajo aparecen al bajar
            if r.get("href"):
                title = r.get("title") or ""
                log.info("Seguir viendo: %s → %s (fila por %s)", title or "?", r["href"], r.get("how", "?"))
                if self._busy_on:
                    await self.busy(loading_text(title), until="playing", ttl=20)
                else:
                    await self.toast(f"Seguir viendo: {title}" if title else "Seguir viendo…", 2000)
                    await asyncio.sleep(0.3)
                await self.navigate(r["href"])
                return
            if r.get("opened"):
                log.info("Seguir viendo: %s (fila por %s; sin número de título, abro la ficha)", r.get("title", "?"),
                         r.get("how", "?"))
                if await self.open_and_play(r):
                    return
            last = r
            attempt += 1
            await asyncio.sleep(0.8)
        # Para saber por qué (la fila se llama distinto en ese perfil, no está, no cargó…): qué filas había.
        seen = last.get("seen") if isinstance(last, dict) else None
        log.warning("No encontré la fila «Seguir viendo» (perfil %s, %s intentos, bajé hasta y=%s). Filas: %s",
                    self._last_profile or "?", attempt, last.get("y", "?") if isinstance(last, dict) else "?",
                    " | ".join(seen) if seen else "ninguna")
        await self.toast("No encontré «Seguir viendo»")
