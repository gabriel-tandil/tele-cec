"""Cambio de actividad: cuál está en primer plano, abrir una cerrando la otra (con lo que puede seguir sonando)."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path

from .. import apps as activities_mod
from ..apps import photodates
from ..apps.photos import MODE_NAMES, MODES, TODAY
from ..system import audio as audio_mod

log = logging.getLogger("tele")

# Apps que pueden quedar sonando de fondo al abrir otra: con Fotos, la música de Tidal (o la local) sigue.
KEEP_PLAYING = {"fotos": {"tidal", "musica"}}
WATCH_EVERY_S = 1.5    # cada cuánto se mira si la app en pantalla se cerró sola
FOTOS_REMINDER_S = 3.0  # cuánto se ve el aviso del modo de Fotos antes de que arranque la presentación
CLOSED_TEXT = {"android": "Se desconectó el celular", "videos": "Terminó el video", "juegos": "Terminó el juego"}


class ActivitiesMixin:
    def _init_activities(self) -> None:
        # Actividades (Netflix, YouTube, Tidal, Videos, Fotos, [activities.*]) y el menú para elegirlas.
        self.activities: dict[str, activities_mod.Activity] = activities_mod.build(self, self.cfg)
        # La app que se eligió última (la que está en pantalla), aunque otra siga abierta de fondo.
        self._foreground: str | None = None
        # Mientras se cambia de actividad las teclas se ignoran (cortar el cambio a mitad dejaría todo
        # cerrado), y con launch = "always" el navegador no se reabre solo.
        self._switching = False
        self._switch_until = 0.0
        self._no_activate = False      # al volver del reposo porque se prendió la TV: no cambiarle la entrada
        self._music_by_video = False   # un video de Fotos pausó la música de Tidal (vuelve al terminar)

    async def _sound_off_tv(self) -> str:
        """El tipo de salida si el sonido no sale por la TV (p. ej. "bluetooth"); "" si sale por la TV o no se sabe."""
        kind = await asyncio.to_thread(audio_mod.default_kind)
        return kind if kind not in ("", "hdmi") else ""

    async def _pause_if_playing(self, everything: bool = False) -> None:
        """La TV pasó a standby: pausar lo que se ve. La música sigue si suena por otro lado (parlantes Bluetooth),
        salvo con everything (al entrar en reposo)."""
        if self.page:
            await self.pause_netflix()
            return
        off_tv: str | None = None  # se pregunta una sola vez, y solo si hace falta
        for ident, act in self.activities.items():  # p. ej. Fotos con música de fondo: pausa las dos
            if ident == "netflix" or not act.running():
                continue
            if act.audio_only and not everything:
                if off_tv is None:
                    off_tv = await self._sound_off_tv()
                if off_tv:
                    log.info("%s sigue sonando: el sonido no sale por la TV (%s)", act.name, off_tv)
                    continue
            await act.pause()

    # ---------------------------------------------------------------- actividades
    def _holding_for_activity(self) -> bool:
        """Con otra actividad abierta (o mientras se cambia, o en reposo) el navegador no se reabre solo."""
        if time.monotonic() < self._switch_until or self.power.asleep:
            return True
        return any(a.running() for k, a in self.activities.items() if k != "netflix")

    def current_app(self) -> str | None:
        """La actividad en primer plano: Netflix si hay pestaña conectada; si no, la última elegida si sigue
        abierta (Fotos, aunque Tidal suene de fondo); si no, la primera abierta."""
        if self.page:
            return "netflix"
        fg = self._foreground
        if fg and fg != "netflix" and fg in self.activities and self.activities[fg].running():
            return fg
        for ident, act in self.activities.items():
            if ident != "netflix" and act.running():
                return ident
        return None

    def nothing_open(self) -> bool:
        """Ninguna actividad abierta (ni abriéndose): en pantalla queda solo el menú (o el escritorio)."""
        if self.page or self._switching or time.monotonic() < self._switch_until:
            return False
        return not any(act.running() for act in self.activities.values())

    async def a_app(self, arg: str) -> None:
        """app:<actividad>: la abre (cerrando la actual) o, si ya está abierta, hace su on_reselect."""
        want = arg.strip().lower()
        if want not in self.activities:
            log.warning("app:%s desconocida (válidas: %s)", arg, ", ".join(self.activities))
            return
        if self.current_app() == want:
            again = (self.cfg.apps.get(want) or {}).get("on_reselect")
            if again:
                log.info("%s ya está abierto: %s", self.activities[want].name, again)
                await self.run_action(again)
            return
        if isinstance(self.activities[want], activities_mod.MameActivity):
            await self.games_open()  # Juegos: la lista para elegir cuál (no abre el último que se jugó)
            return
        await self.switch_to(want)

    async def switch_to(self, want: str, folder: str | Path | None = None, mode: str | None = None) -> bool:
        """Cierra la actividad abierta (pausando antes lo que suena) y abre `want`.

        Con `folder` (Videos o Fotos, elegida en el menú) la abre en esa carpeta; si ya estaba abierta, la reabre.
        """
        act = self.activities[want]
        if not await asyncio.to_thread(act.installed):
            log.error("%s no está instalado (ver README)", act.name)
            await self.toast(f"{act.name} no está instalado", 2500)
            return False
        # Ya estaba abierta (por ejemplo, Tidal sonando de fondo detrás de Fotos, o tras reiniciar el servicio):
        # no se reabre, pero lo que la tapa se cierra igual (si no, la presentación seguiría encima).
        already = folder is None and want != "netflix" and act.running() and self.current_app() != want
        if already:
            log.info("%s ya estaba abierto: pasa a primer plano", act.name)
        else:
            log.info("Cambio a %s", act.name)
        outer = self._switching  # el menú ya lo marcó (y lo desmarca él al ocultar el panel)
        self._switching = True
        self._switch_until = time.monotonic() + 120
        try:
            keep = KEEP_PLAYING.get(want, set())
            for ident, other in self.activities.items():
                if ident in keep and other.running():
                    log.info("%s sigue de fondo", other.name)
                    continue
                if (ident != want or folder is not None) and await other.active():
                    log.info("Cierro %s", other.name)
                    await other.close()
            if already:
                self._foreground = want  # antes de mostrarla: que no parezca que se cerró sola
                await act.bring_front()
                return True
            self._tidal_nav = self.music.start_in_nav
            if (want != "netflix" and self.source and not self._no_activate
                    and self.cfg.cec.get("activate_source") in ("launch", "both")):
                await self.source.activate_source()  # a_launch lo hace para Netflix
            if isinstance(act, activities_mod.PhotosActivity) and not act.command:
                act.on_video = self._fotos_video  # un video pausa la música de fondo
                await self._fotos_reminder(mode or "photos")
                ok = await act.open(Path(folder) if folder is not None else None, mode=mode)
            elif folder is not None and isinstance(act, activities_mod.MediaActivity):
                log.info("%s en %s", act.name, folder)
                ok = await act.open(Path(folder))
            elif folder is not None and isinstance(act, activities_mod.MameActivity):
                ok = await act.open(str(folder))  # el juego elegido
            elif folder is not None and isinstance(act, activities_mod.YouTubeActivity):
                ok = await act.open(str(folder))  # una dirección (la búsqueda, desde el celular)
            else:
                ok = await act.open()
            if ok:
                self._foreground = want
        finally:
            self._switching = outer
            self._switch_until = 0.0 if want == "netflix" else time.monotonic() + 5
        if not ok:
            log.warning("No pude abrir %s", act.name)
        return ok

    # ---------------------------------------------------------------- Fotos: modos y música
    async def _fotos_reminder(self, mode: str) -> None:
        """Antes de la presentación, unos segundos: en qué modo arranca y que ▼ lo cambia."""
        others = " o ".join(MODE_NAMES[m] for m in MODES if m != mode)
        await self.overlay.hide()  # el panel no muestra avisos encima de «Abriendo…»
        if mode == TODAY:
            text = f"Un día como hoy: {photodates.month_day_text(datetime.date.today())}\n▼ toda la carpeta"
        else:
            text = f"Fotos: {MODE_NAMES[mode]}\n▼ cambia a {others}"
        await self.overlay.toast(text, int(FOTOS_REMINDER_S * 1000) + 1500)
        await asyncio.sleep(FOTOS_REMINDER_S)

    async def _fotos_video(self, playing: bool) -> None:
        """Mientras suena un video de Fotos, la música de fondo (Tidal o la local) se pausa; al terminar, sigue."""
        m = self.music
        local = self.activities.get("musica")
        local = local if isinstance(local, activities_mod.MpvMediaActivity) and local.running() else None
        if playing:
            if m.running() and (await m.status()).get("status") == "Playing":
                log.info("Fotos: suena un video, pauso la música")
                await m.ctl("pause")
                self._music_by_video = True
            elif local is not None and await local.playback() == "Playing":
                log.info("Fotos: suena un video, pauso la música")
                await local.set_pause(True)
                self._music_by_video = True
        elif self._music_by_video:
            self._music_by_video = False
            log.info("Fotos: terminó el video, sigue la música")
            if m.running():
                await m.ctl("play")
            elif local is not None:
                await local.set_pause(False)

    # ---------------------------------------------------------------- app que se cierra sola
    def _left_alone(self) -> str | None:
        """La app que estaba en pantalla y se cerró sola (no la cerró tele-cec al cambiar o al entrar en reposo)."""
        fg = self._foreground
        if not fg or fg == "netflix" or fg not in self.activities:
            return None
        if (self._switching or time.monotonic() < self._switch_until or self.power.asleep or self.power.entering
                or self.activities[fg].running()):
            return None
        return fg

    async def watch_apps(self) -> None:
        """Si la app en pantalla se cierra sola (el celular se desconectó, terminó la carpeta de videos, un programa se
        cayó), vuelve el menú de actividades: si no, quedaría el escritorio sin nada que hacer con el control."""
        while True:
            await asyncio.sleep(WATCH_EVERY_S)
            try:
                gone = self._left_alone()
                if gone is None:
                    continue
                await asyncio.sleep(0.5)  # que no sea un instante de un cambio que recién empieza
                if self._left_alone() != gone:
                    continue
                await self._app_gone(gone)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Error vigilando las apps abiertas")

    async def _app_gone(self, ident: str) -> None:
        act = self.activities[ident]
        log.info("%s se cerró sola", act.name)
        self._foreground = None
        try:
            await act.close()  # lo que le quede abierto (p. ej. el shell de adb de Android)
        except Exception:  # noqa: BLE001
            log.exception("No pude terminar de cerrar %s", act.name)
        if self.page or self.current_app() is not None or self._menu_open:
            return  # hay otra cosa en pantalla (o el menú ya está abierto)
        if isinstance(act, activities_mod.MameActivity):
            await self.games_open()  # salió del juego (Esc en MAME): la lista, para elegir otro
            await self.overlay.toast(CLOSED_TEXT["juegos"], 2500)
            return
        await self.menu_open()
        await self.overlay.toast(getattr(act, "fail_text", "") or CLOSED_TEXT.get(ident) or f"{act.name} se cerró",
                                 4000)

    async def open_tidal(self) -> None:
        """Nombre anterior: elegir Tidal."""
        await self.switch_to("tidal")
