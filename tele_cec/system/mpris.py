"""Controles de un reproductor por MPRIS (D-Bus) con playerctl."""

from __future__ import annotations

from .procs import run_cmd


class Mpris:
    """Controles de un reproductor por MPRIS (D-Bus) con playerctl."""

    def __init__(self, player: str, volume_step: float = 0.05):
        self.player = player  # nombre para playerctl -p
        self.volume_step = volume_step

    async def ctl(self, *args: str) -> bool:
        rc, _ = await run_cmd(["playerctl", "-p", self.player, *args], 5)
        return rc == 0

    async def status(self) -> dict[str, str]:
        rc, out = await run_cmd(["playerctl", "-p", self.player, "metadata", "--format",
                                 "{{status}}\t{{title}}\t{{artist}}"], 5)
        if rc != 0:
            # Sin tema cargado, "metadata" falla aunque el reproductor esté abierto: el estado sí responde.
            rc, st = await run_cmd(["playerctl", "-p", self.player, "status"], 5)
            return {"status": st.strip()} if rc == 0 else {}
        parts = (out.strip().split("\t") + ["", "", ""])[:3]
        return {"status": parts[0], "title": parts[1], "artist": parts[2]}

    async def volume(self, sign: str) -> bool:
        return await self.ctl("volume", f"{self.volume_step:.2f}{'+' if sign != '-' else '-'}")

    async def seek(self, seconds: int) -> bool:
        return await self.ctl("position", f"{abs(seconds)}{'+' if seconds >= 0 else '-'}")

    async def quit(self) -> bool:
        """Pide al reproductor que se cierre (método Quit de MPRIS)."""
        rc, _ = await run_cmd(["gdbus", "call", "--session", "--dest", f"org.mpris.MediaPlayer2.{self.player}",
                               "--object-path", "/org/mpris/MediaPlayer2", "--method",
                               "org.mpris.MediaPlayer2.Quit"], 2)  # High Tide 1.4 no contesta: no esperar de más
        return rc == 0
