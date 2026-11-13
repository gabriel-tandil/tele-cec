#!/usr/bin/env python3
"""Tests de la página del celular (tele_cec/web): HTTP real en un puerto libre, con un controlador falso."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tele_cec.system import audio  # noqa: E402
from tele_cec.web import nowplaying, server  # noqa: E402

OK = 0
FAIL = 0


def check(cond: bool, what: str) -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [OK ] {what}")
    else:
        FAIL += 1
        print(f"  [FAIL] {what}")


class FakeCtl:
    def __init__(self) -> None:
        self.keys: list = []
        self.toasts: list = []
        tidal = SimpleNamespace(name="Tidal", mpris=SimpleNamespace(player="io.github.nokse22.high-tide"))
        videos = SimpleNamespace(name="Videos", mpris=SimpleNamespace(player="vlc"))
        self.activities = {"netflix": SimpleNamespace(name="Netflix"), "tidal": tidal, "videos": videos,
                           "youtube": SimpleNamespace(name="YouTube", mpris=SimpleNamespace(player="firefox"))}
        self.browser = SimpleNamespace(engine="firefox")
        self.power = SimpleNamespace(asleep=False, last_activity=0.0)
        self._menu_open = False
        self.cur = "tidal"
        self.overlay = SimpleNamespace(toast=self._toast)

    async def _toast(self, text, ms=2000):
        self.toasts.append(text)

    def current_app(self):
        return self.cur

    def on_key(self, code):
        self.keys.append(code)

    def on_release(self):
        self.keys.append(None)


async def http(port: int, method: str, path: str, body: dict | None = None, headers: dict | None = None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    data = json.dumps(body).encode() if body is not None else b""
    head = [f"{method} {path} HTTP/1.1", f"Content-Length: {len(data)}"]
    headers = {"Host": "raspberrypi.local:8765", **(headers or {})}
    head += [f"{k}: {v}" for k, v in (headers or {}).items()]
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    hdrs = dict(line.split(": ", 1) for line in head.decode().split("\r\n")[1:])
    return status, hdrs, payload


async def main() -> int:
    print("Lo que suena (playerctl) y el volumen (wpctl)")
    S = nowplaying.SEP
    out = (f"vlc{S}Paused{S}Peli{S}{S}{S}file:///tmp/x.jpg{S}5000000{S}60000000\n"
           f"io.github.nokse22.high-tide{S}Playing{S}Tema{S}Artista{S}Disco{S}https://img/x.jpg{S}1500000{S}200000000\n"
           f"basura sin campos\n")
    tracks = nowplaying.parse_players(out)
    check([(t.player, t.status, t.title, t.position, t.length) for t in tracks]
          == [("vlc", "Paused", "Peli", 5.0, 60.0), ("io.github.nokse22.high-tide", "Playing", "Tema", 1.5, 200.0)],
          f"lee cada reproductor (segundos, no microsegundos): {tracks}")
    check(nowplaying.pick(tracks, ["vlc"]).player == "io.github.nokse22.high-tide",
          "el que está sonando gana aunque la app en pantalla sea otra")
    paused = [t for t in tracks if t.player == "vlc"]
    check(nowplaying.pick(paused, []).player == "vlc", "si nada suena: el que está en pausa con un tema")
    ff = nowplaying.Track("firefox.instance_1_5", "Playing", "Video")
    check(nowplaying.pick([tracks[1], ff], ["firefox"]).player == "firefox.instance_1_5",
          "con dos sonando, el de la app en pantalla (firefox.instance_… cuenta como firefox)")
    check(nowplaying.pick([], ["vlc"]) is None, "sin reproductores: nada")
    check(nowplaying.parse_volume("Volume: 0.40 [MUTED]") == (40, True) and nowplaying.parse_volume("Volume: 1.00")
          == (100, False) and nowplaying.parse_volume("") == (None, False), "volumen y silencio de wpctl")

    print("Quién puede usar la página")
    check(all(server.allowed_client(h) for h in ("192.168.0.50", "10.1.2.3", "127.0.0.1", "::1", "fe80::1%wlan0",
                                                 "::ffff:192.168.0.9")),
          "la red de la casa (privadas, loopback, link-local)")
    check(not any(server.allowed_client(h) for h in ("8.8.8.8", "2800:810::1", "basura")), "internet no")
    check(all(server.allowed_host(h) for h in ("raspberrypi.local:8765", "192.168.0.111:8765", "raspberrypi",
                                               "[fe80::1]:8765", "localhost:8765")),
          "nombres de la casa: .local, IP, el nombre de la Pi")
    check(not any(server.allowed_host(h) for h in ("evil.example.com:8765", "", "x.attacker.net")),
          "un dominio de internet que apunte a la Pi (DNS rebinding): no")

    print("Servidor HTTP")
    ctl = FakeCtl()
    calls: list = []
    players = list(tracks)

    async def fake_players():
        return list(players)

    async def fake_volume():
        return 35, False

    async def fake_control(player, command):
        calls.append(("media", player, command))
        return True

    async def fake_change(what, step=5):
        calls.append(("vol", what, step))
        return True

    outs = [audio.Output("TV (HDMI)", "hdmi", 50, "alsa_output.hdmi", default=True),
            audio.Output("KN320", "bluetooth", None, mac="AA:BB:CC:DD:EE:FF"),
            audio.Output("Otro", "bluetooth", None, mac="11:22:33:44:55:66", paired=False)]

    async def fake_select(out, connect_timeout=25.0):
        calls.append(("sound", out.name))
        return True, f"Sonido por {out.name}"

    nowplaying.players = fake_players  # type: ignore[assignment]
    nowplaying.volume = fake_volume  # type: ignore[assignment]
    nowplaying.control = fake_control  # type: ignore[assignment]
    nowplaying.change_volume = fake_change  # type: ignore[assignment]
    audio.list_outputs = lambda: list(outs)  # type: ignore[assignment]
    audio.available = lambda: True  # type: ignore[assignment]
    audio.select = fake_select  # type: ignore[assignment]

    web = server.WebRemote(ctl, {"port": 0, "bind": "127.0.0.1"})
    check(await web.start(), "arranca")
    port = web.server.sockets[0].getsockname()[1]
    try:
        st, h, body = await http(port, "GET", "/")
        check(st == 200 and h["Content-Type"].startswith("text/html") and b"Tele-CEC" in body,
              "/ devuelve la página")
        st, h, body = await http(port, "GET", "/manifest.webmanifest")
        check(st == 200 and json.loads(body)["display"] == "standalone", "manifiesto para la pantalla de inicio")
        st, _, _ = await http(port, "GET", "/../server.py")
        check(st == 404, "no sirve otros archivos")
        st, _, _ = await http(port, "GET", "/api/state", headers={"Host": "evil.example.com:8765"})
        check(st == 403, "con el nombre de otro sitio (DNS rebinding): rechazado")

        st, _, body = await http(port, "GET", "/api/state")
        s = json.loads(body)
        check(st == 200 and s["app"] == {"id": "tidal", "name": "Tidal"} and s["now"]["title"] == "Tema"
              and s["now"]["app"] == "Tidal" and s["now"]["art"] == "https://img/x.jpg"
              and s["volume"] == {"level": 35, "muted": False},
              f"estado: app en pantalla, lo que suena (con su app y tapa) y volumen: {s['app']} {s['now']}")
        check([o["name"] for o in s["outputs"]] == ["TV (HDMI)", "KN320"] and s["outputs"][0]["current"]
              and "tocá" in s["outputs"][1]["detail"],
              f"salidas emparejadas, con la actual marcada (las nuevas sin emparejar no): {s['outputs']}")

        players[:] = [tracks[0]]  # solo VLC, en pausa, con la tapa en un archivo
        art = Path("/tmp") / f"tele-cec-web-test-{time.time_ns()}.jpg"
        art.write_bytes(b"\xff\xd8\xff fake jpeg")
        players[0] = nowplaying.Track("vlc", "Paused", "Peli", art=f"file://{art}")
        web._state = None
        _, _, body = await http(port, "GET", "/api/state")
        s = json.loads(body)
        check(s["now"]["app"] == "Videos" and s["now"]["art"].startswith("/api/art?"),
              f"tapa en un archivo local: la sirve la Pi: {s['now']['art']}")
        st, h, body = await http(port, "GET", s["now"]["art"])
        check(st == 200 and h["Content-Type"] == "image/jpeg" and body.startswith(b"\xff\xd8"),
              "/api/art devuelve esa tapa (y solo esa)")
        art.unlink()

        st, _, _ = await http(port, "POST", "/api/key", {"key": "UP"})
        check(st == 403 and not ctl.keys, "sin el encabezado X-Tele-CEC no acepta teclas (otra página no puede)")
        st, _, body = await http(port, "POST", "/api/key", {"key": "UP"}, {"X-Tele-CEC": "1"})
        check(st == 200 and json.loads(body)["ok"] and ctl.keys == [1, None],
              f"una tecla entra a la cola como la del control (con su «soltar»): {ctl.keys}")
        _, _, body = await http(port, "POST", "/api/key", {"key": "POWER"}, {"X-Tele-CEC": "1"})
        check(not json.loads(body)["ok"] and ctl.keys == [1, None], "teclas que no están en la página: no")

        _, _, body = await http(port, "POST", "/api/media", {"command": "play-pause", "player": "vlc"},
                                {"X-Tele-CEC": "1"})
        check(json.loads(body)["ok"] and calls[-1] == ("media", "vlc", "play-pause") and ctl.power.last_activity > 0,
              "play/pausa va al reproductor que se muestra (y cuenta como uso para el reposo)")
        _, _, body = await http(port, "POST", "/api/media", {"command": "next", "player": "spotify"},
                                {"X-Tele-CEC": "1"})
        check(not json.loads(body)["ok"] and calls[-1][1] == "vlc", "un reproductor que no existe: no")

        _, _, body = await http(port, "POST", "/api/volume", {"change": "+"}, {"X-Tele-CEC": "1"})
        check(json.loads(body)["ok"] and calls[-1] == ("vol", "+", 5), "volumen +")

        _, _, body = await http(port, "POST", "/api/sound", {"id": "AA:BB:CC:DD:EE:FF"}, {"X-Tele-CEC": "1"})
        r = json.loads(body)
        check(r["ok"] and calls[-1] == ("sound", "KN320") and ctl.toasts == ["Sonido por KN320"],
              f"elegir el parlante Bluetooth: lo conecta y la TV lo avisa: {r}")
        _, _, body = await http(port, "POST", "/api/sound", {"id": "nada"}, {"X-Tele-CEC": "1"})
        check(not json.loads(body)["ok"], "una salida que no existe: no")

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /api/key HTTP/1.1\r\nHost: raspberrypi.local\r\nX-Tele-CEC: 1\r\nContent-Length: 999999\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        check(raw == b"" and ctl.keys == [1, None], "un cuerpo enorme se corta sin leerlo")
        st, _, _ = await http(port, "GET", "/api/state")
        check(st == 200, "sigue atendiendo después de un pedido inválido")
    finally:
        await web.close()

    print(f"\n{OK}/{OK + FAIL} verificaciones OK")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
