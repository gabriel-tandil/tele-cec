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


def app(name, icon="", mpris=None, installed=True):
    return SimpleNamespace(name=name, icon=icon, mpris=mpris, installed=lambda: installed, fail_text="")


class FakeMusic:
    """High Tide simulado: el canal de control (data/command) con una biblioteca chica."""

    def __init__(self, log):
        self.log = log
        self.on = True
        self.logged = True
        self.mpris = SimpleNamespace(player="io.github.nokse22.high-tide", ctl=self._mpris_ctl)
        self.liked = False

    async def _mpris_ctl(self, *args):
        self.log.append(("mpris", *args))
        return True

    def running(self):
        return self.on

    async def command(self, line, timeout=10.0, wait=0.0):
        self.log.append(("cmd", line))
        if line == "like":
            self.liked = not self.liked
        return "ok"

    async def data(self, line, timeout=25.0):
        self.log.append(("data", line))
        if not self.on:
            return {"error": "cerrado"}
        if not self.logged:
            return {"error": "login"}
        name, _, rest = line.partition(" ")
        if name == "liked":
            return {"liked": self.liked, "id": "1"}
        if name == "ready":
            return {"ok": True, "restored": True}
        if name == "lib":
            return {"items": [{"type": "track", "id": "11", "title": "Uno"}], "total": 1, "start": 0}
        if name == "list":
            return {"title": "Mi lista", "items": [{"type": "track", "id": "21", "title": "A"}], "total": 1}
        if name == "search":
            return {"tracks": [{"type": "track", "id": "31", "title": rest}], "albums": [], "playlists": [],
                    "artists": [], "top": None}
        if name == "queue":
            return {"current": {"type": "track", "id": "1", "title": "Ahora"}, "queue": [], "next": [], "total_next": 0}
        if name in ("play", "enqueue", "jump"):
            return {"ok": True, "title": "Tema"}
        return {"error": "?"}


class FakeCtl:
    def __init__(self) -> None:
        self.keys: list = []
        self.toasts: list = []
        self.log: list = []
        # como la real: la actividad Tidal no tiene MPRIS propio, está en ctl.music (High Tide)
        self.music = FakeMusic(self.log)
        self.activities = {"netflix": app("Netflix", "netflix.svg"), "tidal": app("Tidal", "tidal.svg"),
                           "videos": app("Videos", "videos.svg", SimpleNamespace(player="vlc")),
                           "youtube": app("YouTube", "youtube.svg", SimpleNamespace(player="firefox")),
                           "juegos": app("Juegos", "juegos.svg", installed=False)}
        self.cfg = SimpleNamespace(menu={"items": ["netflix", "youtube", "tidal", "videos", "juegos"]})
        self.browser = SimpleNamespace(engine="firefox")
        self.power = SimpleNamespace(asleep=False, last_activity=0.0, note_key=lambda: False, wake=self._wake,
                                     _wake_task=None)
        self._menu_open = False
        self._switching = False
        self._no_activate = False
        self.page = None
        self.cur = "tidal"
        self.ctx = "browse"
        self.url = "https://www.netflix.com/browse"
        self._switch_until = 0.0
        self.overlay = SimpleNamespace(toast=self._toast, busy=self._busy, hide=self._hide)

    async def _toast(self, text, ms=2000):
        self.toasts.append(text)

    async def _busy(self, text):
        self.log.append(("busy", text))

    async def _hide(self):
        self.log.append(("hide",))

    def _wake(self, by_key):
        self.log.append(("wake", by_key))
        self.power.asleep = False

    async def menu_close(self):
        self._menu_open = False
        self.log.append(("menu_close",))

    async def switch_to(self, ident, folder=None):
        self.log.append(("switch", ident, self._no_activate))
        self.cur = ident
        if ident == "tidal":
            self.music.on = True
        return True

    async def key(self, name):
        self.log.append(("key", name))

    async def context(self):
        return self.ctx

    async def js(self, name, *args):
        return {"ctx": self.ctx, "url": self.url}

    async def navigate(self, url):
        self.log.append(("navigate", url))

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
    out = (f"vlc{S}Paused{S}Peli{S}{S}{S}file:///tmp/x.jpg{S}5000000{S}60000000{S}{S}\n"
           f"io.github.nokse22.high-tide{S}Playing{S}Tema{S}Artista{S}Disco{S}https://img/x.jpg{S}1500000{S}200000000"
           f"{S}true{S}Playlist\n"
           f"basura sin campos\n")
    tracks = nowplaying.parse_players(out)
    check([(t.player, t.status, t.title, t.position, t.length, t.shuffle, t.loop) for t in tracks]
          == [("vlc", "Paused", "Peli", 5.0, 60.0, None, ""),
              ("io.github.nokse22.high-tide", "Playing", "Tema", 1.5, 200.0, True, "Playlist")],
          f"lee cada reproductor (segundos, no microsegundos; aleatorio y repetir si los informa): {tracks}")
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

    async def fake_control(player, command, value=None):
        calls.append(("media", player, command) if value is None else ("media", player, command, value))
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

        print("Lo que suena: ir a otra parte, aleatorio, repetir y «Me gusta»")
        H = {"X-Tele-CEC": "1"}
        players[:] = [tracks[1]]  # Tidal sonando, aleatorio sí, repetir la lista
        web._state = None
        _, _, body = await http(port, "GET", "/api/state")
        s = json.loads(body)
        check(s["now"]["tidal"] and s["now"]["shuffle"] is True and s["now"]["loop"] == "Playlist"
              and s["now"]["liked"] is False, f"el estado dice si es Tidal, aleatorio, repetir y me gusta: {s['now']}")
        P = tracks[1].player
        await http(port, "POST", "/api/media", {"command": "seek", "player": P, "value": 95.5}, H)
        check(calls[-1] == ("media", P, "seek", 95.5), f"tocar la barra va a ese segundo: {calls[-1]}")
        await http(port, "POST", "/api/media", {"command": "loop", "player": P}, H)
        check(calls[-1] == ("media", P, "loop", "Playlist"), f"repetir pasa al siguiente modo desde el actual: {calls[-1]}")
        _, _, body = await http(port, "POST", "/api/media", {"command": "like", "player": P}, H)
        r = json.loads(body)
        check(r["ok"] and ("cmd", "like") in ctl.log and r["message"] == "Agregado a tus favoritos",
              f"«Me gusta» en Tidal: {r}")
        players[:] = [tracks[0]]
        _, _, body = await http(port, "POST", "/api/media", {"command": "like", "player": "vlc"}, H)
        check(not json.loads(body)["ok"], "«Me gusta» en VLC: no")

        print("Apps")
        web._state = None
        _, _, body = await http(port, "GET", "/api/state")
        s = json.loads(body)
        check([a["id"] for a in s["apps"]] == ["netflix", "youtube", "tidal", "videos"]
              and s["apps"][0]["icon"] == "/icons/netflix.svg",
              f"las apps instaladas, en el orden del menú, con su ícono: {[a['id'] for a in s['apps']]}")
        st, h, body = await http(port, "GET", "/icons/netflix.svg")
        check(st == 200 and h["Content-Type"] == "image/svg+xml", "sirve los íconos del menú")
        st, _, _ = await http(port, "GET", "/icons/..%2Fmenu_ui.py")
        check(st == 404, "y nada más que los íconos")
        ctl.cur, ctl._menu_open = None, True
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/app", {"id": "youtube"}, H)
        check(json.loads(body)["ok"] and ("menu_close",) in ctl.log and ("switch", "youtube", False) in ctl.log
              and ctl.log[-1] == ("hide",), f"abrir YouTube: cierra el menú y la abre, como el menú: {ctl.log}")
        _, _, body = await http(port, "POST", "/api/app", {"id": "juegos"}, H)
        check(not json.loads(body)["ok"], "una app no instalada: no")

        print("Abrir Tidal con el sonido en Bluetooth y la Pi en reposo: sin prender la TV")
        outs[0].default, outs[1].default, outs[1].sink_id = False, True, 79
        web._outputs_at = 0.0
        ctl.power.asleep = True
        ctl.music.on = False
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/app", {"id": "tidal"}, H)
        check(json.loads(body)["ok"] and ("wake", False) in ctl.log and ("switch", "tidal", True) in ctl.log
              and not ctl._no_activate,
              f"despierta como si se prendiera la TV (sin cambiarle la entrada) y abre Tidal sin anunciarse: {ctl.log}")
        outs[0].default, outs[1].default, outs[1].sink_id = True, False, None
        web._outputs_at = 0.0

        print("Escribir en la TV")
        ctl.cur, ctl._menu_open, ctl.page, ctl.ctx = "netflix", False, object(), "text"
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/text", {"text": "Ñandú\n", "key": "Enter"}, H)
        check(json.loads(body)["ok"] and [x for x in ctl.log if x[0] == "key"]
              == [("key", c) for c in "Ñandú"] + [("key", "Enter")],
              f"en Netflix con un campo de texto (PIN, búsqueda): letra por letra, sin caracteres raros, y Enter: {ctl.log}")
        ctl.ctx = "browse"
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/text", {"text": "la casa de papel", "key": "Enter"}, H)
        check(json.loads(body)["ok"] and ctl.log == [("navigate", "https://www.netflix.com/search?q=la%20casa%20de%20papel")],
              f"en Netflix sin campo de texto: va directo a la búsqueda: {ctl.log}")
        ctl.ctx, ctl.url = "text", "https://www.netflix.com/search?q=otra"
        ctl.log.clear()
        await http(port, "POST", "/api/text", {"text": "dark"}, H)
        check(ctl.log == [("navigate", "https://www.netflix.com/search?q=dark")],
              f"con el buscador de Netflix enfocado (con la búsqueda anterior): la reemplaza, no la alarga: {ctl.log}")
        ctl.ctx, ctl.url = "profiles", "https://www.netflix.com/browse"
        _, _, body = await http(port, "POST", "/api/text", {"text": "dark"}, H)
        r = json.loads(body)
        check(not r["ok"] and "perfil" in r["error"], f"en «¿Quién está viendo?»: pide elegir el perfil: {r}")
        ctl.ctx = "browse"
        yt = ctl.activities["youtube"]
        yt.search_url = lambda t: "https://www.youtube.com/tv#/search?q=" + t
        yt_log: list = []

        async def yt_close():
            yt_log.append("close")

        async def yt_open(url=None):
            yt_log.append(("open", url))
            return True
        yt.close, yt.open = yt_close, yt_open
        ctl.cur, ctl.page = "youtube", None
        _, _, body = await http(port, "POST", "/api/text", {"text": "gatitos", "key": "Enter"}, H)
        check(json.loads(body)["ok"] and yt_log == ["close", ("open", "https://www.youtube.com/tv#/search?q=gatitos")]
              and not ctl._switching and ctl._switch_until > 0,
              f"en YouTube: se reabre en la búsqueda con el texto (escribir con el teclado no sirve): {yt_log}")
        del yt.search_url
        typed = []

        async def fake_wtype_text(text):
            typed.append(("text", text))
            return True

        async def fake_wtype_key(name):
            typed.append(("key", name))
            return True
        server.keyboard.wtype_text = fake_wtype_text  # type: ignore[assignment]
        server.keyboard.wtype_key = fake_wtype_key  # type: ignore[assignment]
        ctl.cur, ctl.page = "android", None
        _, _, body = await http(port, "POST", "/api/text", {"text": "hola", "key": "Enter"}, H)
        check(json.loads(body)["ok"] and typed == [("text", "hola"), ("key", "Return")],
              f"en Android (y Tidal): con wtype a la ventana: {typed}")
        ctl.cur = "videos"
        _, _, body = await http(port, "POST", "/api/text", {"text": "x"}, H)
        check(not json.loads(body)["ok"], "en Videos no hay dónde escribir: lo dice")
        ctl._menu_open, ctl.cur = True, "youtube"
        _, _, body = await http(port, "POST", "/api/text", {"text": "x"}, H)
        r = json.loads(body)
        check(not r["ok"] and "menú" in r["error"], f"con el menú abierto (tiene el teclado) avisa: {r}")
        ctl._menu_open = False

        print("Tidal: biblioteca, búsqueda, cola y poner a sonar")
        ctl.music.on, ctl.cur = True, "tidal"
        _, _, body = await http(port, "GET", "/api/tidal/library?what=tracks&start=0")
        check(json.loads(body)["items"][0]["title"] == "Uno" and ("data", "lib tracks 0 100") in ctl.log,
              "Mis canciones")
        _, _, body = await http(port, "GET", "/api/tidal/library?what=secretos")
        check("error" in json.loads(body), "una biblioteca que no existe: no")
        _, _, body = await http(port, "GET", "/api/tidal/list?type=playlist&id=abc-123&start=100")
        check(json.loads(body)["title"] == "Mi lista" and ("data", "list playlist abc-123 100 100") in ctl.log,
              "los temas de una playlist, de a páginas")
        _, _, body = await http(port, "GET", "/api/tidal/list?type=playlist&id=a%0Aquit")
        check("error" in json.loads(body), "un id con un salto de línea (otro comando escondido): no")
        _, _, body = await http(port, "GET", "/api/tidal/search?q=soda%0Astereo%20%20")
        check(json.loads(body)["tracks"][0]["title"] == "soda stereo",
              "buscar: el texto llega en una sola línea (sin saltos que agreguen otro comando)")
        _, _, body = await http(port, "GET", "/api/tidal/queue")
        check(json.loads(body)["current"]["title"] == "Ahora", "la cola")
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/tidal/play", {"type": "fav", "index": 7, "shuffle": True}, H)
        check(json.loads(body)["ok"] and ("data", "play fav - 7") in ctl.log
              and ("mpris", "shuffle", "On") in ctl.log, f"Mis canciones desde el tema 8, mezcladas: {ctl.log}")
        ctl.music.on, ctl.cur = False, None
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/tidal/play", {"type": "album", "id": "999"}, H)
        check(json.loads(body)["ok"] and ("switch", "tidal", False) in ctl.log
              and ctl.log.index(("switch", "tidal", False)) < ctl.log.index(("data", "play album 999 0")),
              f"con Tidal cerrado: lo abre primero y después pone el disco: {ctl.log}")
        _, _, body = await http(port, "POST", "/api/tidal/enqueue", {"id": "31", "mode": "next"}, H)
        r = json.loads(body)
        check(r["ok"] and r["message"].startswith("Suena después de este"), f"agregar a la cola: {r}")
        _, _, body = await http(port, "POST", "/api/tidal/jump", {"where": "next", "n": 3}, H)
        check(json.loads(body)["ok"] and ("data", "jump next 3") in ctl.log, "saltar a un tema de lo que sigue")
        ctl.music.logged = False
        _, _, body = await http(port, "GET", "/api/tidal/library?what=tracks")
        r = json.loads(body)
        check(r.get("login") and "cuenta" in r["error"], f"sin la sesión de Tidal iniciada lo dice: {r}")
    finally:
        await web.close()

    print(f"\n{OK}/{OK + FAIL} verificaciones OK")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
