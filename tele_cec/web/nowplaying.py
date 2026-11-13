"""Lo que está sonando (MPRIS, con playerctl) y el volumen de la salida actual (wpctl), para la página del celular.

Todos los reproductores que usa tele-cec publican MPRIS: High Tide (Tidal), VLC (Videos) y Firefox o Chromium
(Netflix, YouTube). playerctl -a los recorre a todos de una vez.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ..system.procs import run_cmd

SEP = "\x1f"  # separador que no aparece en un título
FIELDS = ("playerInstance", "status", "title", "artist", "album", "mpris:artUrl", "position", "mpris:length")
FORMAT = SEP.join("{{" + f + "}}" for f in FIELDS)
SINK = "@DEFAULT_AUDIO_SINK@"


@dataclass
class Track:
    player: str          # instancia de playerctl (p. ej. "vlc", "firefox.instance_1_42")
    status: str          # Playing / Paused / Stopped
    title: str = ""
    artist: str = ""
    album: str = ""
    art: str = ""        # mpris:artUrl (file://… o https://…)
    position: float = 0.0   # segundos
    length: float = 0.0     # segundos (0 = no se sabe, p. ej. en vivo)

    def to_dict(self) -> dict:
        return asdict(self)


def _seconds(us: str) -> float:
    try:
        return max(0.0, int(us) / 1_000_000)
    except ValueError:
        return 0.0


def parse_players(text: str) -> list[Track]:
    """Salida de `playerctl -a metadata --format FORMAT` → un Track por reproductor."""
    out = []
    for line in text.splitlines():
        f = line.split(SEP)
        if len(f) != len(FIELDS) or not f[0]:
            continue
        out.append(Track(player=f[0], status=f[1], title=f[2], artist=f[3], album=f[4], art=f[5],
                         position=_seconds(f[6]), length=_seconds(f[7])))
    return out


async def players() -> list[Track]:
    rc, out = await run_cmd(["playerctl", "-a", "metadata", "--format", FORMAT], 4)
    return parse_players(out) if rc == 0 or out else []


def pick(tracks: list[Track], prefer: list[str]) -> Track | None:
    """El que conviene mostrar: el que está sonando (si hay varios, el de la app en pantalla); si no, el de la
    app en pantalla aunque esté en pausa; si no, cualquiera en pausa con un tema cargado."""
    def mine(t: Track) -> bool:
        return any(t.player == p or t.player.startswith(p + ".") for p in prefer if p)
    playing = [t for t in tracks if t.status == "Playing"]
    for group in (
        [t for t in playing if mine(t)], playing,
        [t for t in tracks if mine(t) and t.title], [t for t in tracks if t.status == "Paused" and t.title],
    ):
        if group:
            return group[0]
    return None


async def control(player: str, command: str) -> bool:
    if command not in ("play-pause", "next", "previous", "play", "pause"):
        return False
    rc, _ = await run_cmd(["playerctl", "-p", player, command], 5)
    return rc == 0


def parse_volume(text: str) -> tuple[int | None, bool]:
    """`wpctl get-volume` → (porcentaje, silenciado). «Volume: 0.40 [MUTED]» → (40, True)."""
    m = re.search(r"Volume:\s*([\d.]+)", text)
    if not m:
        return None, False
    return round(float(m.group(1)) * 100), "MUTED" in text


async def volume() -> tuple[int | None, bool]:
    rc, out = await run_cmd(["wpctl", "get-volume", SINK], 3)
    return parse_volume(out) if rc == 0 else (None, False)


async def change_volume(what: str, step: int = 5) -> bool:
    if what == "mute":
        cmd = ["wpctl", "set-mute", SINK, "toggle"]
    elif what in ("+", "-"):
        cmd = ["wpctl", "set-volume", "-l", "1.0", SINK, f"{step}%{what}"]
    else:
        return False
    rc, _ = await run_cmd(cmd, 3)
    return rc == 0
