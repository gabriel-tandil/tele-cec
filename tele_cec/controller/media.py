"""Acciones de las apps que no son Netflix: Videos (VLC), YouTube, Fotos (feh), Android (scrcpy) y Tidal (High Tide)."""

from __future__ import annotations

import asyncio
import logging

from .. import apps as activities_mod
from ..system import keyboard

log = logging.getLogger("tele")


class MediaMixin:
    def _init_media(self) -> None:
        self._tidal_nav = self.music.start_in_nav  # flechas = recorrer la interfaz (0 cambia)
        # Qué pantalla de High Tide dejó el último amarillo («my_tracks», «inicio» o "" si no se sabe), y en qué
        # proceso de High Tide (si se reinició, ya no vale).
        self._tidal_view = ""
        self._tidal_pids: tuple[int, ...] = ()

    # ---------------------------------------------------------------- Videos (VLC) y Fotos (feh)
    async def a_video(self, arg: str) -> None:
        """Videos (VLC): video:play_pause, play, pause, next, previous, stop, seek:+10, volume:+, subtitles, audio,
        aspect (proporción de la imagen)."""
        v = self.activities.get("videos")
        if not isinstance(v, activities_mod.VideosActivity) or not v.running():
            log.info("Videos no está abierto: no ejecuto video:%s", arg)
            return
        cmd, _, rest = arg.strip().partition(":")
        mpris = {"play_pause": "play-pause", "play": "play", "pause": "pause", "next": "next",
                 "previous": "previous", "stop": "stop"}
        if cmd in mpris:
            await v.mpris.ctl(mpris[cmd])
        elif cmd == "volume":
            await v.mpris.volume(rest or "+")
        elif cmd == "seek":
            try:
                await v.mpris.seek(int(rest or 10))
            except ValueError:
                log.warning("video:seek:%s inválido (segundos, p. ej. -10)", rest)
        elif cmd in ("subtitles", "audio", "aspect"):
            kind, label = {"subtitles": ("strack", "Subtítulos"), "audio": ("atrack", "Audio"),
                           "aspect": ("vratio", "Proporción")}[cmd]
            name = await v.cycle(kind)
            log.info("Videos: %s %s", label.lower(), name.strip("\0") or "(sin opciones)")
            if name == v.PAUSED:
                await self.toast(f"Poné play para cambiar {label.lower()}", 1800)
            else:
                await self.toast(f"{label}: {name}" if name else f"{label}: este video no tiene opciones", 1800)
        else:
            log.warning("video:%s desconocida", arg)

    async def a_yt(self, arg: str) -> None:
        """YouTube para TV: yt:key:<Tecla> (flechas, Return, Escape: la interfaz las entiende de fábrica),
        yt:play_pause|play|pause (MPRIS), yt:seek:+10|-10."""
        y = self.activities.get("youtube")
        if not isinstance(y, activities_mod.YouTubeActivity) or not y.running():
            log.info("YouTube no está abierto: no ejecuto yt:%s", arg)
            return
        cmd, _, rest = arg.strip().partition(":")
        mpris = {"play_pause": "play-pause", "play": "play", "pause": "pause"}
        if cmd == "key":
            if rest:
                await keyboard.wtype_key(rest)
        elif cmd in mpris:
            await y.mpris.ctl(mpris[cmd])
        elif cmd == "seek":
            try:
                await y.mpris.seek(int(rest or 10))
            except ValueError:
                log.warning("yt:seek:%s inválido (segundos, p. ej. -10)", rest)
        else:
            log.warning("yt:%s desconocida", arg)

    async def a_android(self, arg: str) -> None:
        """Android (scrcpy): android:key:<Tecla> (flechas, Return, números: van a la ventana de scrcpy, que se las
        pasa al celular), android:back|home|apps|notifications (botones de Android), android:play_pause|play|pause|
        stop|next|previous|fast_forward|rewind (teclas multimedia), android:keyevent:<N> (cualquier KEYCODE)."""
        a = self.activities.get("android")
        if not isinstance(a, activities_mod.AndroidActivity) or not a.running():
            log.info("Android no está abierto: no ejecuto android:%s", arg)
            return
        cmd, _, rest = arg.strip().partition(":")
        cmd = cmd.lower()
        if cmd == "key":
            if rest:
                await a.key(rest)
        elif cmd == "keyevent":
            await a.keyevent(rest)
        elif cmd == "notifications":
            # Abre la cortina de notificaciones; otra vez la cierra (KEYCODE_NOTIFICATION no anda en todos).
            await a.shell("cmd statusbar expand-notifications" if not a.notif_open else "cmd statusbar collapse")
            a.notif_open = not a.notif_open
        elif cmd in activities_mod.android.KEYCODES:
            a.notif_open = False
            await a.keyevent(cmd)
        else:
            log.warning("android:%s desconocida", arg)

    async def a_fotos(self, arg: str) -> None:
        """Fotos (feh): fotos:next, previous, pause, rotate, random, info, zoom_in, zoom_out, fit (sin zoom),
        scroll:left|right|up|down (mover la foto con zoom)."""
        f = self.activities.get("fotos")
        if not isinstance(f, activities_mod.PhotosActivity) or not f.running():
            log.info("Fotos no está abierto: no ejecuto fotos:%s", arg)
            return
        cmd, _, rest = arg.strip().lower().partition(":")
        # Atajos de feh: > girar, z foto al azar, d el nombre en letra chica de feh (filename).
        keys = {"rotate": "greater", "random": "z", "filename": "d"}
        if cmd == "next":
            await f.next()
            await f.photo_changed()
        elif cmd in ("previous", "prev"):
            await f.previous()
            await f.photo_changed()
        elif cmd == "pause":
            await f.toggle_pause()
        elif cmd in ("media_play_pause", "media_next", "media_previous"):
            if self.music.running():  # Tidal sonando de fondo: estas teclas son de la música
                mpris = {"media_play_pause": "play-pause", "media_next": "next", "media_previous": "previous"}[cmd]
                await self.music.ctl(mpris)
                await asyncio.sleep(0.4)
                st = await self.music.status()
                what = {"Playing": "♪", "Paused": "♪ en pausa"}.get(st.get("status", ""), "♪")
                title = " — ".join(x for x in (st.get("title"), st.get("artist")) if x)
                await self.toast(f"{what} {title}".strip(), 2500)
            elif cmd == "media_play_pause":
                await f.toggle_pause()
            else:
                await (f.next() if cmd == "media_next" else f.previous())
                await f.photo_changed()
        elif cmd == "info":
            info = await f.photo_info()
            if info:
                text = f.describe_photo(info)
                log.info("Fotos: %s", text.replace("\n", " · "))
                await self.toast(text, 6000)
            else:
                await f.key("d")  # sin el título de la ventana: el texto chico de feh
        elif cmd in keys:
            await f.key(keys[cmd])
            if cmd != "filename":
                await f.photo_changed()
        elif cmd == "zoom_in":
            entering = not f.zoom
            if await f.zoom_in() and entering:
                log.info("Fotos: zoom (flechas mueven la foto)")
                await self.toast("Zoom: flechas mueven · OK acerca · Atrás aleja", 2500)
        elif cmd == "zoom_out":
            await f.zoom_out()
        elif cmd in ("fit", "zoom_fit"):
            await f.fit()
        elif cmd == "scroll":
            if not await f.scroll(rest or "down"):
                log.debug("fotos:scroll:%s sin zoom o dirección inválida (left, right, up, down)", rest)
        else:
            log.warning("fotos:%s desconocida", arg)

    async def a_music(self, arg: str) -> None:
        """Controles de Tidal (High Tide): music:play_pause, next, previous, volume:+, seek:-10, like, shuffle,
        my_tracks (Mis canciones), nav (flechas para recorrer la interfaz), move:<Flecha> (mover el foco),
        page:inicio|explorar|coleccion, key:<Tecla>, back."""
        cmd, _, rest = arg.strip().partition(":")
        m = self.music
        if not m.running():
            log.info("Tidal no está abierto: no ejecuto music:%s", arg)
            return
        mpris = {"play_pause": "play-pause", "play": "play", "pause": "pause", "next": "next",
                 "previous": "previous", "stop": "stop"}
        # Lo que cambia la pantalla de High Tide: después de eso ya no se sabe si sigue en «Mis canciones».
        if cmd in ("back", "nav", "move", "page", "key", "tab"):
            self._tidal_view = ""
        if cmd in mpris:
            await m.ctl(mpris[cmd])
        elif cmd == "volume":
            await m.volume(rest or "+")
        elif cmd == "seek":
            try:
                await m.seek(int(rest or 10))
            except ValueError:
                log.warning("music:seek:%s inválido (segundos, p. ej. -10)", rest)
        elif cmd == "like":
            await m.like()
        elif cmd == "shuffle":
            await m.shuffle()
        elif cmd == "back":
            await m.go("back")
        elif cmd == "my_tracks":
            # En ciclo: Mis canciones → Colección → Inicio → Mis canciones. Con el canal de control del parche,
            # High Tide decide según la pantalla que muestra.
            went = await m.cycle()
            if went is not None:
                log.info("Amarillo: %s", {"my_tracks": "Mis canciones", "collection": "Colección",
                                         "home": "Inicio"}.get(went, "no pude cambiar de pantalla"))
                return
            # Sin canal (parche desactivado): se lleva la cuenta acá, mientras no se mueva por otro lado,
            # porque preguntarle por accesibilidad qué pantalla muestra puede cerrarlo (error de GTK 4).
            pids = tuple(m.pids())
            view = self._tidal_view if pids == self._tidal_pids else ""
            if view == "my_tracks":
                # «Mis canciones» es una página abierta desde Colección: Atrás vuelve justo ahí.
                if await m.go("back"):
                    self._tidal_view = "coleccion"
                    log.info("Amarillo: Colección")
                return
            if view == "coleccion":
                if await m.go("home"):
                    self._tidal_view = "inicio"
                    log.info("Amarillo: Inicio")
                return
            ok = await m.my_tracks()
            self._tidal_view, self._tidal_pids = ("my_tracks" if ok else ""), pids
            log.info("Mis canciones: %s", "reproduciendo" if ok else "no pude abrirlas")
        elif cmd == "nav":
            self._tidal_nav = not self._tidal_nav
            log.info("Tidal: %s", "flechas recorren la interfaz" if self._tidal_nav else "flechas = controles")
            if not self._tidal_nav:
                await m.stop_nav()
        elif cmd == "move":
            await m.move(rest.capitalize() or "Down")
        elif cmd == "tab":
            await m.tab(back=rest.lower() in ("back", "atras", "atrás", "prev"))
        elif cmd == "lyrics":
            await m.toggle_lyrics()
        elif cmd == "page":
            pages = {"inicio": "home", "home": "home", "explorar": "explore", "explore": "explore",
                     "coleccion": "collection", "colección": "collection", "collection": "collection"}
            page = pages.get(rest.lower())
            if page is None:
                log.warning("music:page:%s desconocida (inicio, explorar, coleccion)", rest)
            else:
                await m.go(page)
        elif cmd == "key":
            if rest:
                await m.key(rest)
        else:
            log.warning("music:%s desconocida", arg)
