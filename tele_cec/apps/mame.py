"""Juegos: las ROMs de MAME, cada una con su nombre de verdad, para abrirlas directo desde el menú.

- Dónde busca: las carpetas de [apps.juegos] rompath o, si está vacío, el «rompath» de ~/.mame/mame.ini.
- Qué lista: cada .zip / .7z (o carpeta) que MAME conoce como juego que se puede jugar. Los BIOS, los
  dispositivos y los archivos que no son de MAME no aparecen. El nombre, el año y el fabricante salen de
  `mame -listxml` y quedan guardados en ~/.cache/tele-cec/mame-juegos.json (así la lista aparece al instante).
- Abrir: `mame <rom>` en pantalla completa, con la configuración de MAME de siempre (~/.mame).
- Control: MAME arranca con un script Lua propio (-autoboot_script) que cada 10 cuadros mira un archivo de
  órdenes (pause, resume, exit) y contesta en otro. Así se pausa y se cierra con la API de MAME, sin
  depender del foco ni del teclado (la P mandada con xdotool no lo pausaba).
- Cerrar: MAME ignora SIGTERM, y matarlo pierde lo que guarda al salir (los controles que se configuraron en
  su menú, los récords). Por eso: «exit» por el script; si no contesta, Esc a su ventana (X11, por Xwayland,
  con xdotool); y al final SIGTERM y SIGKILL.
- El joystick es del juego, salvo el botón del medio: es el de nuestro menú, como en las demás apps. Para eso
  MAME arranca con un archivo de controles propio (-ctrlr) donde «mostrar el menú de MAME» queda solo en Tab;
  el menú de MAME se abre desde nuestra lista («Opciones de MAME»).
- Mientras nuestro menú está encima, el juego queda en pausa (P de MAME): los botones no mueven nada abajo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..system.procs import run_cmd
from .base import ProcActivity

log = logging.getLogger("actividades")

ROM_EXT = (".zip", ".7z")
CACHE_NAME = "mame-juegos.json"
CTRLR_NAME = "tele-cec"
LUA_SCRIPT = """-- Lo escribe tele-cec: órdenes desde el servicio (pause, resume, exit) por un archivo.
local cmd, ack = %(cmd)s, %(ack)s
local n = 0
_tele_cec = emu.add_machine_frame_notifier(function()
  n = n + 1
  if n %% 10 ~= 0 then return end
  local f = io.open(cmd, "r")
  if not f then return end
  local c = f:read("*l") or ""
  f:close()
  os.remove(cmd)
  if c == "pause" then emu.pause() elseif c == "resume" then emu.unpause()
  elseif c == "exit" then manager.machine:exit() end
  local a = io.open(ack, "w")
  if a then a:write(c .. "\\n"); a:close() end
end)
"""
# El botón del medio (Guide) deja de abrir el menú de MAME: «mostrar el menú» queda solo en Tab.
CTRLR_XML = """<?xml version="1.0"?>
<!-- Lo escribe tele-cec: el botón del medio del joystick abre el menú de tele-cec, no el de MAME. -->
<mameconfig version="10">
    <system name="default">
        <input>
            <port type="UI_MENU">
                <newseq type="standard">KEYCODE_TAB</newseq>
            </port>
        </input>
    </system>
</mameconfig>
"""
LISTXML_BATCH = 200


@dataclass
class Game:
    rom: str          # nombre corto de MAME: «bublbobl»
    title: str        # «Bubble Bobble»
    detail: str       # «Japan, Ver 0.1»
    year: str = ""
    maker: str = ""

    @property
    def sub(self) -> str:
        return " · ".join(x for x in (self.year, self.maker, self.detail) if x)


def split_title(description: str) -> tuple[str, str]:
    """«Bubble Bobble (Japan, Ver 0.1)» → («Bubble Bobble», «Japan, Ver 0.1»)."""
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", description)
    return (m.group(1).strip(), m.group(2).strip()) if m and m.group(1).strip() else (description.strip(), "")


def parse_listxml(xml_text: str) -> dict[str, Game | None]:
    """Las máquinas de `mame -listxml`: rom → Game, o None si no es un juego (BIOS, dispositivo, no se juega)."""
    out: dict[str, Game | None] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("MAME: no pude leer -listxml: %s", exc)
        return out
    for m in root.iter("machine"):
        rom = m.get("name") or ""
        if not rom:
            continue
        if m.get("isbios") == "yes" or m.get("isdevice") == "yes" or m.get("runnable") == "no":
            out[rom] = None
            continue
        title, detail = split_title(m.findtext("description") or rom)
        out[rom] = Game(rom, title, detail, (m.findtext("year") or "").strip(),
                        (m.findtext("manufacturer") or "").strip())
    return out


def mame_ini_rompath(ini: Path | None = None) -> list[str]:
    """El rompath de ~/.mame/mame.ini (separado por «;», con $HOME)."""
    ini = ini or Path.home() / ".mame" / "mame.ini"
    try:
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] == "rompath":
                return [p.strip() for p in parts[1].strip().strip('"').split(";") if p.strip()]
    except OSError:
        pass
    return []


class MameActivity(ProcActivity):
    id = "juegos"
    name = "Juegos"
    icon = "juegos.svg"
    context = "juegos"
    start_timeout = 30.0

    def __init__(self, cfg: dict[str, Any] | None, cache_dir: Path | None = None):
        cfg = cfg or {}
        self.name = str(cfg.get("name") or "Juegos")
        self.binary = str(cfg.get("binary") or "mame")
        self.rompath_cfg = [str(p) for p in (cfg.get("rompath") or [])]
        extra = cfg.get("extra_args") or []
        self.extra_args = shlex.split(extra) if isinstance(extra, str) else [str(a) for a in extra]
        self.procs = {os.path.basename(self.binary)}
        self.cache_dir = cache_dir or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "tele-cec"
        self.guide_menu = bool(cfg.get("guide_menu", True))
        self.current_game = ""
        self.fail_text = ""
        self.frozen = False   # en pausa porque nuestro menú está encima
        self.control = bool(cfg.get("control", True))   # el script Lua de órdenes (pausa, salir)
        run = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        self.cmd_file = run / "tele-cec-mame.cmd"
        self.ack_file = run / "tele-cec-mame.ack"
        self._games: list[Game] | None = None
        self._games_key: tuple = ()

    # ------------------------------------------------------------ dónde y qué
    def exe(self) -> str | None:
        return shutil.which(self.binary) or shutil.which("/usr/games/" + self.binary)

    def installed(self) -> bool:
        return self.exe() is not None

    def rompaths(self) -> list[Path]:
        raw = self.rompath_cfg or mame_ini_rompath() or ["~/mame/roms"]
        out: list[Path] = []
        for p in raw:
            path = Path(os.path.expandvars(os.path.expanduser(p)))
            if path.is_dir() and path not in out:
                out.append(path)
        return out

    def rom_files(self) -> dict[str, Path]:
        """rom → archivo (o carpeta) en el rompath; la primera carpeta gana, como en MAME."""
        found: dict[str, Path] = {}
        for folder in self.rompaths():
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for e in entries:
                if e.name.startswith("."):
                    continue
                rom = e.stem.lower() if e.suffix.lower() in ROM_EXT else (e.name.lower() if e.is_dir() else "")
                if rom and rom not in found:
                    found[rom] = e
        return found

    def _load_cache(self) -> dict[str, Any]:
        try:
            data = json.loads((self.cache_dir / CACHE_NAME).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self, data: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_dir / (CACHE_NAME + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
            tmp.replace(self.cache_dir / CACHE_NAME)
        except OSError as exc:
            log.debug("MAME: no pude guardar la lista de juegos: %s", exc)

    def _listxml(self, roms: list[str]) -> dict[str, Game | None]:
        exe = self.exe()
        if not exe:
            return {}
        out: dict[str, Game | None] = {}
        for i in range(0, len(roms), LISTXML_BATCH):
            batch = roms[i:i + LISTXML_BATCH]
            try:
                res = subprocess.run([exe, "-listxml", *batch], capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("MAME: -listxml falló: %s", exc)
                continue
            got = parse_listxml(res.stdout) if res.stdout.strip() else {}
            out.update(got)
            for rom in batch:
                out.setdefault(rom, None)  # MAME no la conoce: no es un juego (p. ej. drivedroid.img)
        return out

    def games(self) -> list[Game]:
        """Los juegos que hay, ordenados por nombre. Se llama en un hilo aparte: la primera vez tarda."""
        files = self.rom_files()
        key = tuple(sorted((r, int(p.stat().st_mtime)) for r, p in files.items() if p.exists()))
        if self._games is not None and key == self._games_key:
            return self._games
        exe = self.exe() or ""
        cache = self._load_cache()
        known: dict[str, Any] = (cache.get("roms") or {}) if cache.get("mame") == self._mame_id(exe) else {}
        missing = [r for r in files if r not in known]
        if missing:
            t0 = time.monotonic()
            for rom, game in self._listxml(missing).items():
                known[rom] = asdict(game) if game else None
            log.info("MAME: %d ROMs nuevas leídas en %.1f s", len(missing), time.monotonic() - t0)
            self._save_cache({"mame": self._mame_id(exe), "roms": known})
        games = [Game(**known[r]) for r in files if isinstance(known.get(r), dict)]
        games.sort(key=lambda g: (g.title.lower(), g.detail.lower()))
        self._games, self._games_key = games, key
        return games

    @staticmethod
    def _mame_id(exe: str) -> str:
        """Si se actualiza MAME, se vuelve a preguntar (los nombres y lo que se puede jugar cambian)."""
        try:
            st = os.stat(exe)
            return f"{exe}:{int(st.st_mtime)}:{st.st_size}"
        except OSError:
            return exe

    def game(self, rom: str) -> Game | None:
        return next((g for g in (self._games or []) if g.rom == rom), None)

    def source(self) -> tuple[str, bool]:
        games = self.games()
        if not games:
            where = ", ".join(str(p).replace(str(Path.home()), "~") for p in self.rompaths()) or "~/mame/roms"
            return f"Sin juegos en {where}", False
        if self.running() and self.current_game:
            g = self.game(self.current_game)
            return f"Jugando {g.title if g else self.current_game}", True
        return f"{len(games)} juego{'s' if len(games) != 1 else ''}", True

    def unavailable_text(self) -> str:
        return "No hay juegos: poné las ROMs de MAME (.zip) en " + (
            ", ".join(str(p).replace(str(Path.home()), "~") for p in self.rompaths()) or "~/mame/roms")

    # ------------------------------------------------------------ abrir y cerrar
    def args(self, rom: str) -> list[str]:
        exe = self.exe() or self.binary
        paths = self.rompaths()
        rp = ["-rompath", ";".join(str(p) for p in paths)] if self.rompath_cfg and paths else []
        ctrlr = ["-ctrlrpath", str(self.cache_dir / "mame-ctrlr"), "-ctrlr", CTRLR_NAME] if self.guide_menu else []
        lua = (["-autoboot_script", str(self.cache_dir / "mame-tele-cec.lua"), "-autoboot_delay", "0"]
               if self.control else [])
        return [exe, *rp, *ctrlr, *lua, *self.extra_args, rom]

    def write_lua(self) -> None:
        text = LUA_SCRIPT % {"cmd": json.dumps(str(self.cmd_file)), "ack": json.dumps(str(self.ack_file))}
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self.cache_dir / "mame-tele-cec.lua"
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
            for f in (self.cmd_file, self.ack_file):
                f.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("MAME: no pude escribir el script de control: %s", exc)

    async def command(self, what: str, timeout: float = 1.5) -> bool:
        """Una orden al script Lua de MAME; True si contestó."""
        if not self.control or not self.running():
            return False
        try:
            self.ack_file.unlink(missing_ok=True)
            tmp = self.cmd_file.with_suffix(".tmp")
            tmp.write_text(what + "\n", encoding="utf-8")
            tmp.replace(self.cmd_file)  # entero de una vez: el script no lee media orden
        except OSError as exc:
            log.warning("MAME: no pude mandar «%s»: %s", what, exc)
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.ack_file.read_text(encoding="utf-8").strip() == what:
                    return True
            except OSError:
                pass
            await asyncio.sleep(0.05)
        log.debug("MAME no contestó «%s»", what)
        return False

    def write_ctrlr(self) -> None:
        folder = self.cache_dir / "mame-ctrlr"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{CTRLR_NAME}.cfg"
            if not path.exists() or path.read_text(encoding="utf-8") != CTRLR_XML:
                path.write_text(CTRLR_XML, encoding="utf-8")
        except OSError as exc:
            log.warning("MAME: no pude escribir el archivo de controles: %s", exc)

    async def open(self, rom: str | None = None) -> bool:
        self.fail_text = ""
        self.frozen = False
        if self.guide_menu:
            await asyncio.to_thread(self.write_ctrlr)
        if self.control:
            await asyncio.to_thread(self.write_lua)
        rom = rom or self.current_game
        if not rom:
            self.fail_text = "Elegí un juego"
            return False
        ok = await self.spawn(self.args(rom))
        if ok:
            self.current_game = rom
            g = self.game(rom)
            log.info("Jugando %s (%s)", g.title if g else rom, rom)
        else:
            self.fail_text = "MAME no pudo abrir el juego (ver ~/.cache/tele-cec/juegos.log)"
        return ok

    # ------------------------------------------------------------ teclas a la ventana de MAME (X11, Xwayland)
    async def _window(self) -> str:
        if not shutil.which("xdotool"):
            return ""
        for query in [["--pid", str(pid)] for pid in self.pids(fresh=True)] + [["--class", "mame"]]:
            rc, out = await run_cmd(["xdotool", "search", *query], 5)
            wids = [w for w in out.split() if w.isdigit()] if rc == 0 else []
            if wids:
                return wids[0]
        return ""

    async def key(self, name: str) -> bool:
        """Una tecla a MAME, apretada un rato: mira el teclado una vez por cuadro, y una tecla instantánea
        (xdotool key) a veces pasa entre dos cuadros sin que la vea."""
        wid = await self._window()
        if not wid:
            return False
        await run_cmd(["xdotool", "windowactivate", "--sync", wid], 3)
        await run_cmd(["xdotool", "keydown", name], 3)
        await asyncio.sleep(0.15)
        await run_cmd(["xdotool", "keyup", name], 3)
        return True

    async def freeze(self) -> None:
        """Nuestro menú se abre encima del juego: en pausa, si no estaba ya."""
        if self.running() and not self.frozen and await self.command("pause"):
            self.frozen = True
            log.info("MAME en pausa (menú encima)")

    async def unfreeze(self) -> None:
        if self.frozen:
            self.frozen = False
            if await self.command("resume"):
                log.info("MAME sigue (sin pausa)")

    async def mame_menu(self) -> None:
        """El menú de MAME (Tab): opciones, controles, trucos…"""
        await self.unfreeze()
        await asyncio.sleep(0.2)
        await self.key("Tab")

    async def close(self) -> None:
        self.frozen = False
        pids = self.pids(fresh=True)
        if pids and await self.command("exit"):
            for _ in range(20):
                if not self.pids(fresh=True):
                    log.info("MAME cerrado")
                    return
                await asyncio.sleep(0.25)
        if self.pids(fresh=True) and shutil.which("xdotool"):
            for _attempt in range(2):
                if not await self.key("Escape"):
                    break
                for _ in range(10):
                    if not self.pids(fresh=True):
                        log.info("MAME cerrado (Esc)")
                        return
                    await asyncio.sleep(0.25)
            log.info("MAME no respondió a Esc: lo cierro con SIGTERM")
        await self.terminate(timeout=5.0)
