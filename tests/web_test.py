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
    return SimpleNamespace(name=name, icon=icon, mpris=mpris, installed=lambda: installed, fail_text="",
                           running=lambda: False)


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


class FakeTimer:
    """El temporizador para apagar (controller/sleeptimer.py), simulado."""

    def __init__(self) -> None:
        self.choice: int | str = 0

    def status(self):
        return None if not self.choice else ({"mode": "track", "text": "al terminar este tema"}
                                             if self.choice == "track" else {"mode": "time", "left": self.choice * 60,
                                                                             "text": f"en {self.choice} min"})

    async def set(self, choice):
        self.choice = choice
        return True, f"Se apaga en {choice} min" if choice else "Sin temporizador"


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
        self.sleep_timer = FakeTimer()
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
        self.log.append(("switch", ident, self._no_activate) if folder is None
                        else ("switch", ident, self._no_activate, folder))
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


def phone_qr_test() -> None:
    """El QR de la página del celular para el menú de la TV: con la IP de la red de la casa y el puerto."""
    import shutil
    import tempfile
    print("QR del celular para el menú de la TV")
    tmp = Path(tempfile.mkdtemp(prefix="nfx-qr-"))
    real = (server.LOG_DIR, server._lan_ip, server.shutil.which, server.subprocess.run)
    runs = []

    def fake_run(cmd, **_kw):
        runs.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text("<svg/>")
    try:
        server.LOG_DIR, server._lan_ip = tmp, lambda: "192.168.0.111"
        server.shutil.which = lambda name: "/usr/bin/" + name
        server.subprocess.run = fake_run
        web = server.WebRemote(SimpleNamespace(), {"port": 8765})
        check(web.phone_qr() is None, "sin el servidor andando: sin QR")
        web.server = object()
        qr = web.phone_qr()
        check(qr and qr["url"] == "192.168.0.111:8765" and runs and runs[0][-1] == "http://192.168.0.111:8765/"
              and Path(qr["path"]).is_file(), f"QR con la IP y el puerto: {qr} {runs}")
        web.phone_qr()
        check(len(runs) == 1, "la segunda vez usa el mismo archivo (no lo regenera)")
        server._lan_ip = lambda: "192.168.0.120"
        check(web.phone_qr()["url"] == "192.168.0.120:8765" and len(runs) == 2, "si cambia la IP, lo regenera")
        server.shutil.which = lambda name: None
        server._lan_ip = lambda: "192.168.0.5"
        check(web.phone_qr() is None, "sin qrencode: sin QR (el menú se abre igual)")
    finally:
        server.LOG_DIR, server._lan_ip, server.shutil.which, server.subprocess.run = real
        shutil.rmtree(tmp, ignore_errors=True)


async def local_music_test(ctl, port) -> None:
    """La música local desde el celular: biblioteca, búsqueda, tapas, poner a sonar, cola."""
    import tempfile
    from tele_cec import apps as activities_mod
    from tele_cec.apps import library as library_mod
    print("Música local: biblioteca, búsqueda, poner a sonar y la cola")
    H = {"X-Tele-CEC": "1"}
    tmp = Path(tempfile.mkdtemp(prefix="nfx-musica-"))
    root = tmp / "Música"
    files = ["Soda Stereo/Canción Animal/01 Un Millón de Años Luz.mp3", "Soda Stereo/Canción Animal/02 De Música Ligera.mp3",
             "Soda Stereo/Canción Animal/10 Cae el Sol.mp3", "Soda Stereo/Signos/01 Signos.mp3",
             "Los Pericos/Desde Cero/05 Casi Nunca Lo Ves.mp3", "suelto.mp3", "Soda Stereo/Signos/notas.txt"]
    for f in files:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_bytes(b"")   # sin etiquetas: salen de las carpetas
    (root / "Los Pericos/Desde Cero/cover.jpg").write_bytes(b"\xff\xd8\xff\xe0tapa")
    old_log_dir = library_mod.LOG_DIR
    library_mod.LOG_DIR = tmp

    class FakeMusicAct(activities_mod.MusicActivity):
        def __init__(self):
            super().__init__({"folders": [str(root)]})
            self.on = False
            self.list: list[str] = []
            self.pos = -1
            self.calls: list = []

        def installed(self):
            return True

        def running(self):
            return self.on

        async def play_files(self, files, start=0, shuffle=False):
            self.calls.append(("play_files", [Path(f).name for f in files], start) + (("mezclados",) if shuffle else ()))
            self.list, self.pos = list(files), start
            return True

        async def enqueue(self, path, next_):
            self.calls.append(("enqueue", Path(path).name, next_))
            self.list.insert(self.pos + 1, path) if next_ else self.list.append(path)
            return True

        async def jump(self, index):
            self.calls.append(("jump", index))
            if not 0 <= index < len(self.list):
                return False
            self.pos = index
            return True

        async def playlist(self):
            return list(self.list), self.pos

        async def toggle_shuffle(self):
            self.calls.append(("toggle_shuffle",))
            return True

    act = FakeMusicAct()
    ctl.activities["musica"] = act
    real_switch = ctl.switch_to

    async def switch_to(ident, folder=None):
        if ident == "musica":
            ctl.log.append(("abre musica", [Path(f).name for f in act.want_files[0]], act.want_files[1]))
            act.list, act.pos = act.want_files
            act.want_files = None
            act.on = True
        return await real_switch(ident, folder)
    ctl.switch_to = switch_to
    try:
        _, _, body = await http(port, "GET", "/api/music/library?what=artists")
        r = json.loads(body)
        names = [a["title"] for a in r["items"]]
        check(names == ["Los Pericos", "Soda Stereo", "Sin artista"] and r["total"] == 3,
              f"artistas en orden («Los Pericos» con la P) y lo suelto aparte: {names}")
        soda = r["items"][1]
        check("2 discos · 4 temas" in soda["sub"] and soda["img"].startswith("/api/music/art?id="), f"{soda}")
        _, _, body = await http(port, "GET", "/api/music/library?what=songs&start=1&count=2")
        r = json.loads(body)
        check([s["title"] for s in r["items"]] == ["Un Millón de Años Luz", "De Música Ligera"] and r["total"] == 6,
              f"temas en el orden de la biblioteca (artista, disco, número), de a páginas: {r['items']}")
        _, _, body = await http(port, "GET", "/api/music/library?what=albums")
        albums = {a["title"]: a for a in json.loads(body)["items"]}
        check(set(albums) == {"Desde Cero", "Canción Animal", "Signos", "Música"}, f"discos: {list(albums)}")
        _, _, body = await http(port, "GET", f"/api/music/list?type=artist&id={soda['id']}")
        r = json.loads(body)
        check(r["title"] == "Soda Stereo" and [a["title"] for a in r["albums"]] == ["Canción Animal", "Signos"]
              and [s["title"] for s in r["items"]][2:] == ["Cae el Sol", "Signos"],
              f"un artista: sus discos y sus temas (el 10 después del 2): {r['items']}")
        _, _, body = await http(port, "GET", "/api/music/list?type=album&id=../../etc")
        check("error" in json.loads(body), "un id que no es un id: no")
        _, _, body = await http(port, "GET", "/api/music/search?q=MUSICA%20ligera")
        r = json.loads(body)
        check([s["title"] for s in r["songs"]] == ["De Música Ligera"], f"buscar sin tildes ni mayúsculas: {r}")
        st, hdrs, body = await http(port, "GET", f"/api/music/art?id={albums['Desde Cero']['id']}&s=240")
        check(st == 200 and hdrs.get("Content-Type") == "image/jpeg" and body.startswith(b"\xff\xd8"),
              f"la tapa del disco (cover.jpg): {st} {hdrs.get('Content-Type')}")
        from tele_cec.web.music import LocalMusicWeb
        lm = LocalMusicWeb(ctl, None)
        got = await lm.art_for(str(root / "Los Pericos/Desde Cero/05 Casi Nunca Lo Ves.mp3"))
        check(got == f"/api/music/art?id={albums['Desde Cero']['id']}&s=640" and await lm.art_for("/otro/x.mp3") == "",
              f"lo que suena en Música: la tapa de su disco para el reproductor del celular: {got}")
        st, _, _ = await http(port, "GET", f"/api/music/art?id={albums['Signos']['id']}")
        check(st == 404, "un disco sin tapa: 404 (la página pone la nota)")

        ctl.cur, ctl.log[:] = None, []
        _, _, body = await http(port, "POST", "/api/music/play", {"type": "album", "id": albums["Canción Animal"]["id"],
                                                                  "index": 1}, H)
        check(json.loads(body)["ok"] and ("abre musica", ["01 Un Millón de Años Luz.mp3", "02 De Música Ligera.mp3",
                                                          "10 Cae el Sol.mp3"], 1) in ctl.log
              and act.want_files is None, f"con Música cerrada: la abre con el disco, desde el tema 2: {ctl.log}")
        song = next(s for s in json.loads((await http(port, "GET", "/api/music/search?q=signos"))[2])["songs"])
        _, _, body = await http(port, "POST", "/api/music/play", {"type": "song", "id": song["id"]}, H)
        check(json.loads(body)["ok"] and act.calls[-1] == ("play_files", ["01 Signos.mp3"], 0),
              f"abierta: le cambia la lista sin cerrarla (un tema: su disco desde ese): {act.calls}")
        _, _, body = await http(port, "POST", "/api/music/play", {"type": "all", "index": 3, "shuffle": True}, H)
        check(json.loads(body)["ok"] and act.calls[-1][2] == 3 and act.calls[-1][1][3] == "10 Cae el Sol.mp3"
              and len(act.calls[-1][1]) == 6 and act.calls[-1][3] == "mezclados",
              f"todo mezclado: en orden, desde el elegido, y que mpv mezcle los demás (así se puede volver al orden): "
              f"{act.calls[-1]}")
        lst = json.loads((await http(port, "GET", "/api/music/library?what=songs"))[2])["items"]
        _, _, body = await http(port, "POST", "/api/music/enqueue", {"id": lst[0]["id"], "mode": "next"}, H)
        r = json.loads(body)
        check(r["ok"] and r["message"].startswith("Suena después de este") and act.list[act.pos + 1].endswith("Ves.mp3"),
              f"que suene después de este: {r}")
        _, _, body = await http(port, "GET", "/api/music/queue")
        q = json.loads(body)
        check(q["current"]["title"] == "Cae el Sol" and q["next"][0]["title"] == "Casi Nunca Lo Ves"
              and q["next"][0]["n"] == act.pos + 1 and q["total_next"] == len(act.list) - act.pos - 1,
              f"la cola: lo que suena y lo que sigue: {q}")
        _, _, body = await http(port, "POST", "/api/music/jump", {"n": 2}, H)
        check(json.loads(body)["ok"] and act.pos == 2, "saltar a un tema de la cola")
        _, _, body = await http(port, "POST", "/api/music/jump", {"n": 99}, H)
        check(not json.loads(body)["ok"], "saltar a uno que no está: lo dice")
        st, _, _ = await http(port, "POST", "/api/music/play", {"type": "all"}, {})
        check(st == 403, "poner a sonar sin el encabezado: no")
        saved_players = nowplaying.players

        async def mpv_playing():
            return [nowplaying.Track("mpv", "Playing", "Cae el Sol", shuffle=False, loop="None")]
        nowplaying.players = mpv_playing  # type: ignore[assignment]
        try:
            _, _, body = await http(port, "POST", "/api/media", {"command": "shuffle", "player": "mpv"}, H)
            check(json.loads(body)["ok"] and act.calls[-1] == ("toggle_shuffle",),
                  f"aleatorio desde el celular en Música: como el amarillo de la TV (para poder volver al orden): "
                  f"{act.calls[-1]}")
        finally:
            nowplaying.players = saved_players  # type: ignore[assignment]
        act.on = False
        _, _, body = await http(port, "GET", "/api/music/queue")
        check(json.loads(body).get("closed"), "la cola con Música cerrada")
    finally:
        library_mod.LOG_DIR = old_log_dir
        ctl.switch_to = real_switch
        del ctl.activities["musica"]


def collection_playlists_test() -> None:
    import types
    """Parche de High Tide: «Mis playlists» y «Favoritas», también las guardadas en carpetas (y sin repetir)."""
    import importlib
    print("Tidal: playlists de la colección (con carpetas)")
    fake_pl = types.ModuleType("tidalapi.playlist")

    class Playlist:
        def __init__(self, ident, name, creator="tidal", n=10):
            self.id, self.name, self.num_tracks = ident, name, n
            self.creator = SimpleNamespace(id=creator, name="TIDAL" if creator == "tidal" else "me")

    class UserPlaylist(Playlist):
        pass

    class Folder:
        def __init__(self, ident, name, items):
            self.id, self.name, self._items = ident, name, items

        def items(self, offset=0, limit=50):
            return self._items[offset:offset + limit]
    fake_pl.UserPlaylist, fake_pl.Playlist = UserPlaylist, Playlist
    mine = [UserPlaylist(f"m{i}", f"Mía {i}", "u1") for i in range(3)]
    root_favs = [Playlist(f"f{i}", f"Favorita {i}") for i in range(60)]  # más de una página de 50
    sub = Folder("c2", "Sub", [Playlist("s1", "En subcarpeta")])
    folders = {"root": [Folder("c1", "Para correr", [Playlist("c1a", "En carpeta"), root_favs[0]])], "c1": [sub]}
    calls = []

    def page(items, offset, limit):
        calls.append(offset)
        return items[offset:offset + limit]
    user = SimpleNamespace(
        id="u1", playlists=lambda: list(mine),
        playlist_and_favorite_playlists=lambda offset=0, limit=50: page(mine[:1] + root_favs[:5], offset, limit),
        favorites=SimpleNamespace(
            playlists=lambda limit=50, offset=0: page(mine[:2] + root_favs, offset, limit),
            playlist_folders=lambda limit=50, offset=0, parent_folder_id="root":
                folders.get(parent_folder_id, [])[offset:offset + limit]))
    fake_utils = types.ModuleType("high_tide.lib.utils")
    fake_utils.session = SimpleNamespace(user=user)
    saved = {k: sys.modules.get(k) for k in ("tidalapi", "tidalapi.playlist", "high_tide", "high_tide.lib",
                                             "high_tide.lib.utils")}
    sys.modules.update({"tidalapi": types.ModuleType("tidalapi"), "tidalapi.playlist": fake_pl,
                        "high_tide": types.ModuleType("high_tide"), "high_tide.lib": types.ModuleType("high_tide.lib"),
                        "high_tide.lib.utils": fake_utils})
    try:
        patch = importlib.import_module("tele_cec.apps.tidal.hightide_patch")
        patch._my_playlists[:] = [0.0, [], []]
        m, f = patch._collection_playlists()
        names = {pl.name for pl in f}
        check([pl.id for pl in m] == ["m0", "m1", "m2"], f"Mis playlists: las tres mías, sin repetir: {[p.id for p in m]}")
        check(len(f) == 62 and {"En carpeta", "En subcarpeta", "Favorita 59"} <= names,
              f"Favoritas: las 60 de la raíz (dos páginas) y las de carpetas y subcarpetas, sin repetir: {len(f)}")
        n = len(calls)
        patch._collection_playlists()
        check(len(calls) == n, "la segunda vez sale de lo guardado (no recorre todo de nuevo)")

        print("Tidal: Recientes (del Inicio de Tidal)")
        home = {"items": [
            {"moduleId": "SHORTCUT_LIST", "type": "SHORTCUT_LIST", "title": "Shortcuts", "items": ["x"]},
            {"moduleId": "HISTORY_MIXES", "type": "HORIZONTAL_LIST", "title": "Your listening history", "items": ["h"]},
            {"moduleId": "CONTINUE_LISTEN_TO", "type": "HORIZONTAL_LIST", "title": "Recently played",
             "items": ["a", "b"], "viewAll": "home/pages/CONTINUE_LISTEN_TO/view-all"}]}
        asked = []

        class Resp:
            def __init__(self, j):
                self._j = j

            def json(self):
                return self._j

        def request(method, path, base_url=None, params=None):
            asked.append(path)
            return Resp(home if path == "home/feed/static" else {"items": [{"n": "a"}, {"n": "b"}, {"n": "c"}]})

        class Cat:
            def __init__(self, module):
                self.items = [SimpleNamespace(name=x) for x in module["items"]]

            def get_item(self, it):
                return SimpleNamespace(name=it["n"])
        fake_utils.session = SimpleNamespace(
            user=user, locale="es_AR", config=SimpleNamespace(api_v2_location="https://api.tidal.com/v2/"),
            request=SimpleNamespace(request=request), page=SimpleNamespace(page_category_v2=SimpleNamespace(
                parse_item=Cat)))
        patch._recent_cache[:] = [0.0, "", [], []]
        title, items, mods = patch._recent()
        check(title == "Recently played" and [x.name for x in items] == ["a", "b", "c"] and len(mods) == 3,
              f"«Recently played» (no el historial de mixes), con «Ver todo» completo: {title} {[x.name for x in items]}")
        home["items"][2]["moduleId"] = "OTRO_ID"
        patch._recent_cache[:] = [0.0, "", [], []]
        title, _items, _ = patch._recent()
        check(title == "Recently played", f"si Tidal le cambia el id, lo encuentra por el título: {title}")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


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
    t = nowplaying.parse_players(f"mpv{S}Playing{S}Tema{S}{S}{S}{S}0{S}0{S}false{S}None{S}file:///m/a%20b.m4a\n")
    check(t and t[0].url == "file:///m/a%20b.m4a", f"y el archivo (para la tapa de la Música local): {t}")
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
        from tele_cec.web import server as server_mod
        check(s.get("ui") and s["ui"] == server_mod._ui_version(),
              f"y la versión de la página (si cambia, la página abierta en el celular se recarga sola): {s.get('ui')}")
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
        _, _, body = await http(port, "POST", "/api/volume", {"level": 73}, {"X-Tele-CEC": "1"})
        check(json.loads(body)["ok"] and calls[-1] == ("vol", "=73", 5), f"la barra: fijar el volumen en 73 %: {calls[-1]}")
        await http(port, "POST", "/api/volume", {"level": 250}, {"X-Tele-CEC": "1"})
        check(calls[-1] == ("vol", "=100", 5), f"como mucho 100 %: {calls[-1]}")
        _, _, body = await http(port, "POST", "/api/sleep", {"choice": 30}, {"X-Tele-CEC": "1"})
        r = json.loads(body)
        web._state = None
        _, _, sbody = await http(port, "GET", "/api/state")
        check(r["ok"] and r["message"] == "Se apaga en 30 min" and json.loads(sbody)["sleep"]["left"] == 1800,
              f"temporizador desde el celular, y el estado dice cuánto falta: {r}")
        for bad in ("30", True, -5, 1000, "mañana"):
            _, _, body = await http(port, "POST", "/api/sleep", {"choice": bad}, {"X-Tele-CEC": "1"})
            check(not json.loads(body)["ok"], f"temporizador: {bad!r} no")
        await http(port, "POST", "/api/sleep", {"choice": 0}, {"X-Tele-CEC": "1"})
        web._state = None
        _, _, sbody = await http(port, "GET", "/api/state")
        check(json.loads(sbody)["sleep"] is None, "sacarlo")
        st, _, body = await http(port, "POST", "/api/client-log", {"msg": "pantalla bloqueada: sonando <b>"},
                                 {"X-Tele-CEC": "1"})
        check(st == 200 and json.loads(body)["ok"], "el celular puede dejar una línea en el registro de la Pi")

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
        from tele_cec.apps.mame import MameActivity
        mame = MameActivity.__new__(MameActivity)
        real_juegos, ctl.activities["juegos"] = ctl.activities["juegos"], mame
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/app", {"id": "juegos"}, H)
        r = json.loads(body)
        check(not r["ok"] and "juego" in r["error"] and not ctl.log,
              f"Juegos sin elegir juego: no abre uno por defecto: {r} {ctl.log}")
        ctl.activities["juegos"] = real_juegos

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
        ctl.music.logged = True

        print("El menú de actividades, como en la TV")
        import tempfile
        import types
        from tele_cec import apps as activities_mod
        from tele_cec.controller.menu import MenuMixin

        class FakeVideos(activities_mod.VideosActivity):
            on = False

            def installed(self):
                return True

            def running(self):
                return self.on

            async def close(self):
                ctl.log.append(("close", "videos"))
                self.on = False

        class FakeAndroid(activities_mod.AndroidActivity):
            devs: list = []
            on = False

            def installed(self):
                return True

            def running(self):
                return self.on

            def devices(self):
                return list(self.devs)

        tmp = Path(tempfile.mkdtemp(prefix="tele-web-"))
        from tele_cec.apps import fileinfo as fileinfo_mod
        fileinfo_mod._video_info = fileinfo_mod.VideoInfo(tmp / ".videos-info.json")  # no tocar el de verdad
        from tele_cec.apps import resume as resume_mod
        resume_mod.WATCH_DIR = Path(tempfile.mkdtemp(prefix="tele-seguir-"))  # Seguir viendo: tampoco
        (tmp / "Vacaciones" / "2024").mkdir(parents=True)
        (tmp / "Vacaciones" / "peli.mp4").write_bytes(b"x")
        outside = Path(tempfile.mkdtemp(prefix="tele-fuera-"))
        vids = FakeVideos({"folders": [str(tmp)]})
        andr = FakeAndroid({})
        andr.devs = [activities_mod.android.Device("ZY327", "device", "moto_e7_plus"),
                     activities_mod.android.Device("192.168.0.9:5555", "unauthorized", "")]
        ctl.activities["videos"] = vids
        ctl.activities["android"] = andr
        ctl._picker_entries = types.MethodType(MenuMixin._picker_entries, ctl)
        ctl._picker_title = types.MethodType(MenuMixin._picker_title, ctl)

        def menu_entries():
            out = []
            for ident in ("netflix", "youtube", "tidal", "videos", "android"):
                act = ctl.activities[ident]
                out.append({"id": ident, "name": act.name, "icon": getattr(act, "icon", ""), "sub": "algo",
                            "enabled": True, "current": ident == ctl.cur})
            out.append({"id": "_sound", "name": "Sonido", "icon": "sonido.svg", "sub": "TV (HDMI) · volumen 40 %",
                        "output": "TV (HDMI)", "enabled": True, "current": False})
            out.append({"id": "_help", "name": "Ayuda", "icon": "ayuda.svg", "sub": "", "enabled": True,
                        "current": False})
            return out
        ctl._menu_entries = menu_entries
        ctl.cur = "tidal"
        web.menu.invalidate()
        _, _, body = await http(port, "GET", "/api/menu")
        r = json.loads(body)
        rows = {it["id"]: it for it in r["items"]}
        check(list(rows) == ["netflix", "youtube", "tidal", "videos", "android", "_sound"]
              and rows["videos"]["option"] == "folders" and rows["videos"]["chip"] == "Carpetas"
              and rows["android"]["chip"] == "Conectar" and rows["tidal"]["chip"] == "Explorar"
              and rows["netflix"]["option"] == "search" and rows["tidal"]["current"]
              and rows["netflix"]["icon"] == "/icons/netflix.svg" and rows["_sound"]["sub"] == "TV (HDMI)",
              f"las filas del menú de la TV (sin la Ayuda), con su botoncito y la actual marcada: "
              f"{[(i['id'], i['chip'], i['current']) for i in r['items']]}")

        # El grupo «Videos y música»: una fila con las de adentro (para la página del grupo).
        plain_entries = menu_entries

        def grouped(group=None):
            if group == "local":
                return [{"id": "videos", "name": "Videos", "icon": "", "sub": "Pendrive", "enabled": True,
                         "current": ctl.cur == "videos"}]
            out = [e for e in plain_entries() if e["id"] != "videos"]
            out.insert(2, {"id": "_group:local", "group": "local", "name": "Videos y música", "icon": "local.svg",
                           "sub": "Videos", "enabled": True, "current": ctl.cur == "videos"})
            return out
        ctl._menu_entries = grouped
        ctl.cur = "videos"
        web.menu.invalidate()
        _, _, body = await http(port, "GET", "/api/menu")
        rows = {it["id"]: it for it in json.loads(body)["items"]}
        g = rows.get("_group:local") or {}
        check(g.get("option") == "group" and [c["id"] for c in g.get("children", [])] == ["videos"]
              and g["children"][0]["chip"] == "Carpetas" and g["current"] and "videos" not in rows,
              f"el grupo «Videos y música» va con sus actividades adentro (y la abierta marcada): {g}")
        ctl._menu_entries = plain_entries
        ctl.cur = "tidal"
        web.menu.invalidate()

        # Tidal de fondo (detrás de Fotos): «sonando» solo si suena de verdad.
        ctl.cur = "videos"
        bg_state = ["Paused"]

        async def playback():
            return bg_state[0]
        ctl.activities["tidal"].playback = playback
        ctl._menu_playback = types.MethodType(MenuMixin._menu_playback, ctl)
        plain = menu_entries

        def menu_entries_bg():
            out = plain()
            for it in out:
                if it["id"] == "tidal":
                    it.update(badge="sonando", background=True)
            return out
        ctl._menu_entries = menu_entries_bg
        web.menu.invalidate()
        _, _, body = await http(port, "GET", "/api/menu")
        tid = next(it for it in json.loads(body)["items"] if it["id"] == "tidal")
        check(tid["badge"] == "en pausa", f"Tidal de fondo en pausa: el celular dice «en pausa»: {tid['badge']}")
        bg_state[0] = "Playing"
        _, _, body = await http(port, "GET", "/api/menu")
        tid = next(it for it in json.loads(body)["items"] if it["id"] == "tidal")
        check(tid["badge"] == "sonando", f"y «sonando» en cuanto vuelve a sonar (sin esperar): {tid['badge']}")
        ctl._menu_entries = plain
        ctl.cur = "tidal"
        web.menu.invalidate()

        _, _, body = await http(port, "GET", "/api/folders?app=videos")
        r = json.loads(body)
        check(r["items"] and r["items"][0]["kind"] == "dir" and r["items"][0]["path"] == str(tmp),
              f"Carpetas: primero las configuradas: {r}")
        _, _, body = await http(port, "GET", f"/api/folders?app=videos&path={tmp / 'Vacaciones'}")
        r = json.loads(body)
        kinds = [(i["kind"], i["name"]) for i in r["items"]]
        check(kinds == [("dir", "2024"), ("file", "peli")],
              f"adentro: sus subcarpetas y los videos para elegir uno (no «Reproducir esta carpeta»): {kinds}")
        _, _, body = await http(port, "GET", "/api/folders?app=videos&default=1")
        r = json.loads(body)
        check(r["path"] == str(tmp) and ("dir", "Vacaciones") in [(i["kind"], i["name"]) for i in r["items"]],
              f"default=1: directo en la carpeta predeterminada (tocar Videos en el celular): {r['path']}")
        _, _, body = await http(port, "GET", "/api/menu")
        vrow = next(it for it in json.loads(body)["items"] if it["id"] == "videos")
        check(vrow["browse"] is True, f"la fila de Videos avisa que se elige qué ver: {vrow}")
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/app", {"id": "videos", "folder": str(tmp / "Vacaciones" / "peli.mp4")}, H)
        check(json.loads(body)["ok"]
              and ("switch", "videos", False, str((tmp / "Vacaciones" / "peli.mp4").resolve())) in ctl.log,
              f"elegir un video lo abre: {ctl.log}")
        peli = str((tmp / "Vacaciones" / "peli.mp4").resolve())
        (resume_mod.WATCH_DIR / "X1").write_text(f"# {peli}\nstart=754\n")
        _, _, body = await http(port, "GET", f"/api/folders?app=videos&path={tmp / 'Vacaciones'}")
        f = next(i for i in json.loads(body)["items"] if i["kind"] == "file")
        check(f["resume"] == 754 and not f["direct"] and "quedó en 12:34" in f["sub"],
              f"Seguir viendo: el celular sabe dónde quedó (para preguntar): {f}")
        _, _, body = await http(port, "POST", "/api/app", {"id": "videos", "folder": peli, "restart": True}, H)
        check(json.loads(body)["ok"] and not (resume_mod.WATCH_DIR / "X1").exists(),
              "«Desde el principio» desde el celular: se borra dónde quedó")
        (tmp / "Vacaciones" / "nota.txt").write_text("")
        (outside / "otra.mp4").write_bytes(b"x")
        for bad in (tmp / "Vacaciones" / "nota.txt", outside / "otra.mp4"):
            _, _, body = await http(port, "POST", "/api/app", {"id": "videos", "folder": str(bad)}, H)
            check(not json.loads(body)["ok"], f"un archivo que no es video o está afuera: no ({bad.name})")
        _, _, body = await http(port, "GET", f"/api/folders?app=videos&path={tmp / 'Vacaciones' / 'peli.mp4'}")
        check("error" in json.loads(body), "un archivo no se lista como carpeta")
        _, _, body = await http(port, "GET", f"/api/folders?app=videos&path={outside}")
        check("error" in json.loads(body), "una carpeta fuera de las configuradas: no se lista")
        _, _, body = await http(port, "GET", "/api/folders?app=videos&path=/etc")
        check("error" in json.loads(body), "ni /etc")
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/app", {"id": "videos", "folder": str(tmp / "Vacaciones")}, H)
        check(json.loads(body)["ok"] and ("switch", "videos", False, str((tmp / "Vacaciones").resolve())) in ctl.log,
              f"elegir una carpeta la abre: {ctl.log}")
        _, _, body = await http(port, "POST", "/api/app", {"id": "videos", "folder": str(outside)}, H)
        check(not json.loads(body)["ok"], "abrir una carpeta de afuera: no")
        _, _, body = await http(port, "POST", "/api/app", {"id": "videos", "folder": str(tmp / "Vacaciones"),
                                                           "mode": "mixed"}, H)
        check(not json.loads(body)["ok"], "el modo (fotos y videos…) es solo de Fotos")
        _, _, body = await http(port, "POST", "/api/app", {"id": "videos", "mode": "cualquiera"}, H)
        check(not json.loads(body)["ok"], "un modo desconocido: no")

        _, _, body = await http(port, "GET", "/api/android")
        r = json.loads(body)
        check([(d["name"], d["ready"]) for d in r["devices"]] == [("moto e7 plus", True), ("192.168.0.9:5555", False)]
              and "Permitir" in r["devices"][1]["sub"], f"Android: los celulares y su estado: {r['devices']}")
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/app", {"id": "android", "serial": "ZY327"}, H)
        check(json.loads(body)["ok"] and andr.want_serial == "ZY327" and ("switch", "android", False) in ctl.log,
              f"tocar un celular lo comparte: {ctl.log}")
        _, _, body = await http(port, "POST", "/api/app", {"id": "android", "serial": "192.168.0.9:5555"}, H)
        check(not json.loads(body)["ok"], "uno sin autorizar: no")

        async def menu_open(sel=None):
            ctl.log.append(("menu_open",))
        ctl.menu_open = menu_open
        ctl._foreground = "videos"
        vids.on, ctl.cur = True, "videos"
        orig_current = ctl.current_app
        ctl.current_app = lambda: "videos" if vids.on else None
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/close", {"id": "videos"}, H)
        check(json.loads(body)["ok"] and ("close", "videos") in ctl.log and ctl.log[-1] == ("menu_open",)
              and ctl._foreground is None and not ctl._switching,
              f"cerrar la app en uso: se cierra (sin el aviso de «se cerró sola») y vuelve el menú: {ctl.log}")
        ctl.current_app = orig_current

        print("Buscar desde el celular con la app cerrada")
        yt = ctl.activities["youtube"]
        yt.search_url = lambda t: "https://www.youtube.com/tv#/search?q=" + t
        yt.running = lambda: False
        ctl.cur = None
        ctl.log.clear()
        _, _, body = await http(port, "POST", "/api/search", {"app": "youtube", "text": "  gatitos\n "}, H)
        r = json.loads(body)
        check(r["ok"] and ("switch", "youtube", False) in [x[:3] for x in ctl.log]
              and any(x[0] == "switch" and x[1] == "youtube" for x in ctl.log),
              f"YouTube cerrado: se abre directo en la búsqueda: {ctl.log} {r}")
        _, _, body = await http(port, "POST", "/api/search", {"app": "tidal", "text": "x"}, H)
        check(not json.loads(body)["ok"], "buscar solo en Netflix y YouTube")

        await local_music_test(ctl, port)
    finally:
        await web.close()

    phone_qr_test()
    collection_playlists_test()
    print(f"\n{OK}/{OK + FAIL} verificaciones OK")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
