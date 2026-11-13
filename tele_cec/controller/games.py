"""Juegos (MAME) en el menú: la lista de ROMs para elegir una y jugarla.

OK sobre «Juegos» (sin un juego abierto) o ▶ sobre «Juegos» muestra la lista. Mientras se juega, Atrás del
control, o el botón del medio del joystick, la muestran encima del juego (en pausa), con «Seguir jugando»,
«Salir del juego», «Opciones de MAME» (su propio menú) y «Otras actividades» arriba de los juegos.
"""

from __future__ import annotations

import asyncio
import logging

from .. import apps as activities_mod

log = logging.getLogger("tele")

GAMES_HINT = "▲ ▼ elegir · OK jugar · Atrás volver"
QUIT_ID = "_quit_game"
RESUME_ID = "_resume_game"
MAME_MENU_ID = "_mame_menu"
MAIN_MENU_ID = "_main_menu"


class GamesMenuMixin:
    def _mame(self) -> activities_mod.MameActivity | None:
        act = self.activities.get("juegos")
        return act if isinstance(act, activities_mod.MameActivity) else None

    def _games_entries(self) -> tuple[list[dict], int]:
        act = self._mame()
        if act is None:
            return [], 0
        games = act.games()
        playing = act.current_game if act.running() else ""
        items: list[dict] = []
        if playing:
            g = act.game(playing)
            items += [
                {"id": RESUME_ID, "name": "Seguir jugando", "icon": "media-playback-start",
                 "sub": g.title if g else playing, "enabled": True, "current": False},
                {"id": QUIT_ID, "name": "Salir del juego", "icon": "application-exit",
                 "sub": "Cierra MAME", "enabled": True, "current": False},
                {"id": MAME_MENU_ID, "name": "Opciones de MAME", "icon": "preferences-system",
                 "sub": "Su propio menú: controles, video, trucos", "enabled": True, "current": False},
                {"id": MAIN_MENU_ID, "name": "Otras actividades", "icon": "view-app-grid",
                 "sub": "El menú principal", "enabled": True, "current": False},
            ]
        for g in games:
            items.append({"id": "_game", "rom": g.rom, "name": g.title, "icon": "juegos.svg", "sub": g.sub,
                          "enabled": True, "current": g.rom == playing})
        sel = 0 if playing else next((i for i, it in enumerate(items) if it["current"]), 0)
        return items, sel

    async def games_open(self) -> None:
        """La lista de juegos en el panel (desde el menú, desde el juego o al terminar uno)."""
        act = self._mame()
        if act is None or not await asyncio.to_thread(act.installed):
            await self.toast("MAME no está instalado", 2500)
            return
        if not (self._menu_open and self._menu_mode == "games"):
            self._games_back_closes = not self._menu_open and act.running()
        if not self._menu_open or self._menu_mode == "activities":
            self._menu_act_sel = next((i for i, it in enumerate(self._menu_items) if it.get("id") == "juegos"),
                                      self._menu_sel if self._menu_open else 0)
        first = act._games is None
        if first:
            await self.overlay.busy("Buscando juegos…")
        items, sel = await asyncio.to_thread(self._games_entries)
        if not any(it["id"] == "_game" for it in items):
            await self.overlay.toast(act.unavailable_text(), 4000)
            if self._menu_open:
                await self.menu_open(self._menu_act_sel)
            return
        self._sound_scan_stop()
        self._android_stop()
        if self._menu_mode == "activities" and not self._menu_open and self._ctx != "menu":
            self._menu_from_ctx = self._ctx
        if not self._menu_open:
            await act.freeze()  # encima del juego: en pausa (antes de que el panel tome el teclado)
        self._menu_mode, self._menu_items, self._menu_sel = "games", items, sel
        n = sum(1 for it in items if it["id"] == "_game")
        if not await self.overlay.show(items, sel, title=f"{act.name} · {n} juego{'s' if n != 1 else ''}",
                                       hint=GAMES_HINT, numbers=False):
            await self.toast("No pude mostrar la lista de juegos")
            return
        self._menu_open = True
        log.info("Menú: juegos (%d)", n)
        self._menu_restart_timer()

    async def _games_choose(self, index: int) -> None:
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        item = self._menu_items[index]
        act = self._mame()
        assert act is not None
        if item["id"] == QUIT_ID:
            log.info("Menú: salgo del juego")
            self._switching = True
            await self.overlay.busy("Saliendo del juego…")
            try:
                self._foreground = None  # se cierra a propósito: que no parezca que se cerró solo
                await act.close()
            finally:
                self._switching = False
            await self.menu_open()
            return
        if item["id"] == RESUME_ID or item.get("current"):
            log.info("Menú: sigo jugando %s", item.get("sub") if item["id"] == RESUME_ID else item["name"])
            await self.menu_close()
            return
        if item["id"] == MAME_MENU_ID:
            log.info("Menú: abro el menú de MAME")
            await self.menu_close()
            await act.mame_menu()
            return
        if item["id"] == MAIN_MENU_ID:
            await self.menu_open(self._menu_act_sel)
            return
        log.info("Menú: juego %s (%s)", item["name"], item["rom"])
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True
        await self.overlay.busy(f"Abriendo {item['name']}…")
        self._macro_task = asyncio.create_task(
            self._menu_switch({"id": "juegos", "name": item["name"], "folder": item["rom"]}))

    async def a_juegos(self, arg: str) -> None:
        """juegos:list (la lista de juegos, también mientras se juega) · juegos:quit (cerrar MAME)."""
        sub = arg.strip().lower()
        act = self._mame()
        if act is None:
            return
        if sub in ("", "list", "lista"):
            await self.games_open()
        elif sub in ("quit", "salir"):
            if act.running():
                self._foreground = None
                await act.close()
            await self.games_open()
        else:
            log.warning("juegos:%s desconocida (list, quit)", arg)
