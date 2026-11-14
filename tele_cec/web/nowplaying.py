"""Lo que está sonando (MPRIS, con playerctl) y el volumen de la salida actual (wpctl), para la página del celular.

Todos los reproductores que usa tele-cec publican MPRIS: High Tide (Tidal), mpv (Videos y Música, con mpv-mpris) y Firefox o Chromium
(Netflix, YouTube). playerctl -a los recorre a todos de una vez.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ..system.procs import run_cmd

SEP = "\x1f"  # separador que no aparece en un título
FIELDS = ("playerInstance", "status", "title", "artist", "album", "mpris:artUrl", "position", "mpris:length",
          "shuffle", "loop", "xesam:url")
FORMAT = SEP.join("{{" + f + "}}" for f in FIELDS)
SINK = "@DEFAULT_AUDIO_SINK@"


@dataclass
class Track:
    player: str          # instancia de playerctl (p. ej. "mpv", "firefox.instance_1_42")
    status: str          # Playing / Paused / Stopped
    title: str = ""
    artist: str = ""
    album: str = ""
    art: str = ""        # mpris:artUrl (file://… o https://…)
    position: float = 0.0   # segundos
    length: float = 0.0     # segundos (0 = no se sabe, p. ej. en vivo)
    shuffle: bool | None = None   # None = el reproductor no lo informa
    loop: str = ""          # None / Playlist / Track ("" = no lo informa)
    url: str = ""           # xesam:url: el archivo (file://…) en la Música local, para buscarle la tapa

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
        if len(f) not in (len(FIELDS), len(FIELDS) - 1) or not f[0]:  # (sin xesam:url: un playerctl viejo)
            continue
        shuffle = {"true": True, "false": False}.get(f[8].strip().lower())
        loop = f[9].strip() if f[9].strip() in ("None", "Playlist", "Track") else ""
        out.append(Track(player=f[0], status=f[1], title=f[2], artist=f[3], album=f[4], art=f[5],
                         position=_seconds(f[6]), length=_seconds(f[7]), shuffle=shuffle, loop=loop,
                         url=f[10] if len(f) > 10 else ""))
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


LOOP_NEXT = {"None": "Playlist", "Playlist": "Track", "Track": "None", "": "Playlist"}


async def control(player: str, command: str, value: float | str | None = None) -> bool:
    """play-pause, next, previous, play, pause · seek (value = segundos desde el principio) · shuffle (alterna) ·
    loop (value = el estado actual: pasa al siguiente, None → Playlist → Track → None)."""
    if command in ("play-pause", "next", "previous", "play", "pause"):
        args = [command]
    elif command == "seek":
        try:
            args = ["position", f"{max(0.0, float(value or 0)):.1f}"]
        except (TypeError, ValueError):
            return False
    elif command == "shuffle":
        args = ["shuffle", "Toggle"]
    elif command == "loop":
        args = ["loop", LOOP_NEXT.get(str(value or ""), "Playlist")]
    else:
        return False
    rc, _ = await run_cmd(["playerctl", "-p", player, *args], 5)
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
