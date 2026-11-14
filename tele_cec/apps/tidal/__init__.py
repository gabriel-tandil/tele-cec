"""Tidal con High Tide (Flatpak). La actividad envuelve a HighTide (hightide.py), que vive en ctl.music."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..base import Activity

if TYPE_CHECKING:
    from ...controller import Controller

log = logging.getLogger("actividades")


class TidalActivity(Activity):
    id = "tidal"
    name = "Tidal"
    icon = "tidal.svg"
    context = "tidal"

    def __init__(self, ctl: Controller, cfg: dict[str, Any] | None = None):
        self.ctl = ctl  # ctl.music se lee cada vez (las pruebas lo reemplazan)
        cfg = cfg or {}
        self.name = str(cfg.get("name") or self.name)
        self.icon = str(cfg.get("icon") or self.icon)

    def installed(self) -> bool:
        return self.ctl.music.installed()

    def running(self) -> bool:
        return self.ctl.music.running()

    async def open(self, resume: bool = True) -> bool:
        """Abre High Tide; con resume=False no pone música (al volver del reposo queda en pausa)."""
        m = self.ctl.music
        if not await m.launch():
            return False
        if resume and m.resume_on_open and not await m.resume():
            # High Tide no retoma "Mis canciones" por sí solo (solo guarda el número de tema).
            start = m.saved_index()
            log.info("Retomo «Mis canciones» desde el tema %d", start + 1)
            await m.my_tracks(start=start)
        return True

    async def close(self) -> None:
        await self.ctl.music.quit()

    async def bring_front(self) -> None:
        """Estaba sonando de fondo (detrás de Fotos): que High Tide quede adelante aunque su ventana se haya
        escondido (High Tide puede seguir sonando con la ventana cerrada)."""
        await self.ctl.music.command("show", timeout=5)

    async def playback(self) -> str:
        st = await self.ctl.music.status()
        return st.get("status", "") if st.get("status") != "Paused" or st.get("title") else "Stopped"

    async def pause(self) -> None:
        m = self.ctl.music
        if (await m.status()).get("status") == "Playing":
            await m.ctl("pause")
            log.info("Música pausada por standby de la TV")
