"""Acciones de las apps que no son Netflix: Videos, Música y Radio (mpv), YouTube, Fotos, Android (scrcpy) y Tidal."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .. import apps as activities_mod
from ..apps import photodates, photomap
from ..system import keyboard

log = logging.getLogger("tele")


class MediaMixin:
    def _init_media(self) -> None:
        self._tidal_nav = self.music.start_in_nav  # flechas = recorrer la interfaz (0 cambia)
        # Qué pantalla de High Tide dejó el último amarillo («my_tracks», «inicio» o "" si no se sabe), y en qué
        # proceso de High Tide (si se reinició, ya no vale).
        self._tidal_view = ""
        self._tidal_pids: tuple[int, ...] = ()

    # ---------------------------------------------------------------- Videos, Música (mpv) y Fotos
    async def a_video(self, arg: str) -> None:
        """Videos (mpv): video:play_pause (OK: si se está buscando, salta ahí), play, pause, next, previous, stop,
        seek:+10 (buscar con la barra), volume:+, subtitles, audio, aspect (proporción), back (deja de buscar)."""
        await self._player_action(self.activities.get("videos"), "video", arg)

    async def a_musica(self, arg: str) -> None:
        """Música local (mpv): las mismas que video: (play_pause, next, previous, seek:+10, volume:+, audio…), y
        shuffle (mezclar / en orden) y loop (repetir: la lista, el tema, nada)."""
        await self._player_action(self.activities.get("musica"), "musica", arg)

    async def a_radio(self, arg: str) -> None:
        """Radio: radio:play_pause (OK), play, pause, stop, next / previous (otra radio), station:N (la N.ª, desde 1),
        volume:+ / volume:-."""
        r = self.activities.get("radio")
        if not isinstance(r, activities_mod.RadioActivity) or not r.running():
            log.info("La Radio no está abierta: no ejecuto radio:%s", arg)
            return
        cmd, _, rest = arg.strip().lower().partition(":")
        if cmd in ("play_pause", "ok"):
            await r.play_pause()
        elif cmd in ("play", "pause", "stop"):
            await r.set_pause(cmd != "play")
        elif cmd in ("next", "previous"):
            name = await r.step(1 if cmd == "next" else -1)
            await self.toast(f"Radio: {name}" if name else "Hay una sola radio (agregá más en [apps.radio])", 1800)
        elif cmd == "station":
            try:
                index = int(rest) - 1
            except ValueError:
                log.warning("radio:station:%s inválida (el número de la radio, desde 1)", rest)
                return
            if await r.play_station(index):
                await self.toast(f"Radio: {r.stations[index]['name']}", 1800)
        elif cmd == "volume":
            vol = await r.volume(rest or "+")
            if vol is not None:
                await self.toast(f"Volumen {vol} %", 1200)
        else:
            log.warning("radio:%s desconocida", arg)

    async def _player_action(self, v, prefix: str, arg: str) -> None:
        if not isinstance(v, activities_mod.MpvMediaActivity) or not v.running():
            log.info("%s no está abierto: no ejecuto %s:%s", getattr(v, "name", prefix), prefix, arg)
            return
        cmd, _, rest = arg.strip().partition(":")
        if cmd in ("play_pause", "ok"):
            await v.play_pause()
        elif cmd in ("play", "pause", "stop"):
            await v.set_pause(cmd != "play")
        elif cmd == "next":
            await v.next()
        elif cmd == "previous":
            await v.previous()
        elif cmd == "back":
            await v.osd("cancel")
        elif cmd == "shuffle":
            await self.toast("Mezclado" if await v.toggle_shuffle() else "En orden", 1500)
        elif cmd == "loop":
            loop = await v.cycle_loop()
            await self.toast({"playlist": "Repetir toda la lista", "track": "Repetir este tema"}.get(loop, "Sin repetir"),
                             1500)
        elif cmd == "volume":
            vol = await v.volume(rest or "+")
            if vol is not None:
                await self.toast(f"Volumen {vol} %", 1200)
        elif cmd == "seek":
            try:
                await v.seek(int(rest or 10))
            except ValueError:
                log.warning("%s:seek:%s inválido (segundos, p. ej. -10)", prefix, rest)
        elif cmd in ("subtitles", "audio", "aspect"):
            kind, label = {"subtitles": ("sub", "Subtítulos"), "audio": ("audio", "Audio"),
                           "aspect": ("aspect", "Proporción")}[cmd]
            name = await v.cycle(kind)
            log.info("%s: %s %s", v.name, label.lower(), name or "(sin opciones)")
            await self.toast(f"{label}: {name}" if name else f"{label}: este archivo no tiene opciones", 1800)
        else:
            log.warning("%s:%s desconocida", prefix, arg)

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
        stop|next|previous|fast_forward|rewind (teclas multimedia), android:keyevent:<N> (cualquier KEYCODE),
        android:list (la lista de celulares, con «Dejar de compartir la pantalla»), android:disconnect (dejar de
        compartir y mostrar esa lista)."""
        a = self.activities.get("android")
        cmd = arg.strip().partition(":")[0].lower()
        if isinstance(a, activities_mod.AndroidActivity) and cmd in ("list", "lista", "disconnect", "desconectar"):
            if cmd in ("disconnect", "desconectar") and a.running():
                await self._android_disconnect()
            else:
                await self.android_connect_open(autostart=False)
            return
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
        """Fotos: fotos:next, previous, pause, rotate, random, info, zoom_in, zoom_out, fit (sin zoom),
        scroll:left|right|up|down (mover la foto con zoom), mode (solo fotos → fotos y videos → solo videos),
        date (la fecha de cada foto en una esquina: prender / apagar), favorite (guardar la foto en Favoritas, o
        sacarla)."""
        f = self.activities.get("fotos")
        if not isinstance(f, activities_mod.PhotosActivity) or not f.running():
            log.info("Fotos no está abierto: no ejecuto fotos:%s", arg)
            return
        cmd, _, rest = arg.strip().lower().partition(":")
        if cmd == "next":
            await f.next()
            await f.photo_changed()
        elif cmd in ("previous", "prev"):
            await f.previous()
            await f.photo_changed()
        elif cmd == "pause":
            await f.toggle_pause()
        elif cmd in ("media_next", "media_previous"):
            # Antes pasaban los temas de Tidal de fondo; ahora siempre las fotos (con zoom son las cómodas).
            await (f.next() if cmd == "media_next" else f.previous())
            await f.photo_changed()
        elif cmd == "media_play_pause":
            if f._video_now:  # suena un video (la música ya está en pausa): ⏯ es del video
                await f.toggle_pause()
            elif not self.music.running() and self._local_background() is not None:
                await self._local_background().play_pause()  # la música local o la Radio de fondo: ⏯ es de ella
            elif self.music.running():  # Tidal de fondo: ⏯ es de la música
                await self.music.ctl("play-pause")
                await asyncio.sleep(0.4)
                st = await self.music.status()
                what = {"Playing": "♪", "Paused": "♪ en pausa"}.get(st.get("status", ""), "♪")
                title = " — ".join(x for x in (st.get("title"), st.get("artist")) if x)
                await self.toast(f"{what} {title}".strip(), 2500)
            else:
                await f.toggle_pause()
        elif cmd == "info":
            info = await f.photo_info()
            if info:
                text = f.describe_photo(info)
                log.info("Fotos: %s", text.replace("\n", " · "))
                if f.cfg.get("map", True) and await asyncio.to_thread(photodates.gps_of, info["path"]):
                    text += "\nVerde: dónde se sacó (mapa)"
                await self.toast(text, 6000)
            else:
                await f.filename()  # sin el título de la ventana: el texto chico del visor
        elif cmd == "map":
            await self._fotos_map(f)
        elif cmd in ("date", "fecha"):
            on = await f.toggle_date()
            if on is None:
                await self.toast("La fecha en la esquina necesita mpv ([apps.fotos] viewer = \"mpv\")", 3000)
            else:
                await self.toast("Fecha en la esquina · CC o 1 la saca" if on else "Sin la fecha · CC o 1 la muestra", 1800)
        elif cmd in ("favorite", "favorita", "fav"):
            await self._fotos_favorite(f)
        elif cmd == "filename":
            await f.filename()
        elif cmd == "rotate":
            await f.rotate()
            await f.photo_changed(new_file=False)  # la misma foto, girada: sin zoom
        elif cmd == "random":
            await f.random()
            await f.photo_changed()
        elif cmd == "mode":
            await self._fotos_cycle(f, rest)
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

    async def _fotos_favorite(self, f: activities_mod.PhotosActivity) -> None:
        """2 en Fotos: guarda la foto que se ve en Favoritas (otra vez la saca). Se ven desde Carpetas ▸."""
        info = await f.photo_info()
        if not info:
            await self.toast("No sé qué foto se está viendo", 2500)
            return
        added = await asyncio.to_thread(f.toggle_favorite, info["path"])
        if added is None:
            await self.toast("No pude guardar la lista de favoritas", 3000)
            return
        n = len(f.favorites())
        await f.refresh_corner()
        await self.toast(f"♥ Guardada en Favoritas ({n}) · 2 otra vez la saca" if added
                         else f"Sacada de Favoritas ({n})", 2500)

    async def _fotos_map(self, f: activities_mod.PhotosActivity) -> None:
        """Verde en Fotos: el mapa de dónde se sacó la foto que se ve (si tiene GPS)."""
        if not f.cfg.get("map", True):
            return
        if not photomap.available():
            await self.toast("Para el mapa falta Pillow (sudo apt install python3-pil)", 3500)
            return
        info = await f.photo_info()
        gps = await asyncio.to_thread(photodates.gps_of, info["path"]) if info else None
        if not gps:
            await self.toast("Esta foto no tiene la ubicación (GPS)", 2500)
            return
        log.info("Fotos: mapa de %s (%.5f, %.5f)", os.path.basename(info["path"]), *gps)
        await self.map_open(f, info["path"], gps)

    async def _fotos_cycle(self, f: activities_mod.PhotosActivity, want: str = "") -> None:
        """▼ en Fotos: solo fotos → fotos y videos → solo videos → solo fotos, en la misma carpeta y siguiendo
        desde la foto que se estaba viendo (fotos:mode:mixed elige uno en particular)."""
        from ..apps.photos import MODE_NAMES, MODES
        if f.command or f.current_folder is None:
            await self.toast("Con un visor propio ([apps.fotos] command) no hay otros modos", 3000)
            return
        old = f.mode
        if old not in MODES:  # desde «Un día como hoy»: a la carpeta entera, siguiendo desde la foto que se veía
            new = want if want in MODES else "photos"
        else:
            new = want if want in MODES else MODES[(MODES.index(old) + 1) % len(MODES)]
        if new == old:
            return
        info = await f.photo_info()
        start = info["path"] if info else None
        log.info("Fotos: paso de «%s» a «%s»", MODE_NAMES[old], MODE_NAMES[new])
        self._switching = True
        self._switch_until = time.monotonic() + 60
        try:
            await self.overlay.busy(f"Fotos: {MODE_NAMES[new]}…")
            await f.close()
            ok = await f.open(f.current_folder, mode=new, start=start)
            await self.overlay.hide()  # el panel no muestra avisos encima de «Fotos: …»: sin esto quedaba ahí
            if not ok:
                why = f.fail_text or f"No pude pasar a {MODE_NAMES[new]}"
                await f.open(f.current_folder, mode=old, start=start)  # se queda como estaba
                await self.overlay.toast(why, 3500)
                return
            nxt = MODES[(MODES.index(new) + 1) % len(MODES)]
            await self.overlay.toast(f"Fotos: {MODE_NAMES[new]} · ▼ {MODE_NAMES[nxt]}", 3000)
        except BaseException:
            await self.overlay.hide()
            raise
        finally:
            self._switching = False
            self._switch_until = time.monotonic() + 3

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
