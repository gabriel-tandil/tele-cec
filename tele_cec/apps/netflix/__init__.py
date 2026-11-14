"""Netflix en Firefox (WebDriver BiDi) o Chromium (DevTools), manejado con un script inyectado (nfx.js).

La actividad envuelve lo que hace el controller (controller/browser.py y controller/netflix.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..base import Activity

if TYPE_CHECKING:
    from ...controller import Controller


class NetflixActivity(Activity):
    id = "netflix"
    name = "Netflix"
    icon = "netflix.svg"
    context = "browse"

    def __init__(self, ctl: Controller, cfg: dict[str, Any] | None = None):
        self.ctl = ctl
        cfg = cfg or {}
        self.name = str(cfg.get("name") or self.name)
        self.icon = str(cfg.get("icon") or self.icon)

    def installed(self) -> bool:
        binary = getattr(self.ctl.browser, "binary", None)
        return binary() is not None if callable(binary) else True

    def running(self) -> bool:
        return self.ctl.page is not None

    async def active(self) -> bool:
        return self.ctl.page is not None or await self.ctl.browser.alive()

    async def open(self) -> bool:
        return await self.ctl.launch_browser()

    async def close(self) -> None:
        if self.ctl.page:
            await self.ctl.pause_netflix()
        await self.ctl.browser.close()

    async def pause(self) -> None:
        await self.ctl.pause_netflix()
