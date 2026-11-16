"""Procesos y sesión gráfica: buscar programas abiertos, lanzarlos en su scope de systemd y correr comandos."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("procs")

# Lo que escriben los programas que abre tele-cec (mpv, feh, High Tide): sirve para ver por qué se cerraron.
LOG_DIR = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "tele-cec"
# Lo que no es caché (las fotos favoritas): si se borra ~/.cache no se pierde.
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "tele-cec"

# Variables que inyecta un snap (p. ej. la terminal de VS Code instalado como snap) y que
# rompen la parte gráfica de otros programas: el navegador se lanza sin ellas.
SNAP_LEAKS = ("GTK_PATH", "GTK_EXE_PREFIX", "GIO_MODULE_DIR", "LOCPATH", "GDK_PIXBUF_MODULE_FILE",
              "GDK_PIXBUF_MODULEDIR", "GTK_IM_MODULE_FILE", "GSETTINGS_SCHEMA_DIR")


def graphical_env() -> dict[str, str]:
    """Entorno para abrir ventanas aunque el servicio no haya heredado WAYLAND_DISPLAY/DISPLAY.

    Raspberry Pi OS Bookworm usa Wayland (labwc/wayfire). Un servicio de usuario no
    siempre recibe esas variables, así que se buscan los sockets en XDG_RUNTIME_DIR.
    """
    env = dict(os.environ)
    if env.get("SNAP_NAME"):
        for k in SNAP_LEAKS:
            env.pop(k, None)
        for k in [k for k in env if k.endswith("_VSCODE_SNAP_ORIG")]:
            env[k.removesuffix("_VSCODE_SNAP_ORIG")] = env.pop(k)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    if not env.get("WAYLAND_DISPLAY"):
        sockets = sorted(p for p in glob.glob(os.path.join(runtime, "wayland-*")) if not p.endswith(".lock"))
        if sockets:
            env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
    if not env.get("DISPLAY"):
        xsockets = sorted(glob.glob("/tmp/.X11-unix/X*"))
        if xsockets:
            env["DISPLAY"] = ":" + os.path.basename(xsockets[0])[1:]
    if not env.get("XAUTHORITY"):
        xauth = Path.home() / ".Xauthority"
        if xauth.exists():
            env["XAUTHORITY"] = str(xauth)
    return env


def has_display(env: dict[str, str]) -> bool:
    return bool(env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"))


def _popen(cmd: list[str], env: dict[str, str], out: Any = subprocess.DEVNULL) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603
        cmd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT if out is not subprocess.DEVNULL
        else subprocess.DEVNULL, start_new_session=True,
    )


def log_file(name: str) -> Any:
    """~/.cache/tele-cec/<name>.log, vacío, para la salida de un programa (o DEVNULL si no se puede)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return open(LOG_DIR / f"{name}.log", "wb")  # noqa: SIM115  (lo hereda el programa)
    except OSError:
        return subprocess.DEVNULL


async def spawn_scoped(args: list[str], unit: str, env: dict[str, str] | None = None,
                       check: float = 1.5, log: str | None = None) -> subprocess.Popen:
    """Abre un programa en un scope propio de systemd: reiniciar o detener el servicio no lo cierra.

    Si systemd-run no puede crear el scope (sin bus de usuario) sale enseguida con error: entonces se
    lanza directamente. Con --scope, systemd-run se convierte en el programa (mismo PID).
    Con `log`, lo que escribe el programa va a ~/.cache/tele-cec/<log>.log.
    Lanza OSError si no se puede ejecutar.
    """
    env = env if env is not None else graphical_env()
    out = log_file(log) if log else subprocess.DEVNULL
    try:
        proc = await _spawn_scoped(args, unit, env, check, out)
    finally:
        if out is not subprocess.DEVNULL:
            out.close()  # el programa ya tiene su copia
    # Alguien tiene que recoger su salida cuando termina: si no, queda zombi, sigue figurando en /proc y
    # parece abierto (pgrep y kill -0 lo cuentan).
    threading.Thread(target=proc.wait, name=f"reap-{unit}", daemon=True).start()
    return proc


async def _spawn_scoped(args: list[str], unit: str, env: dict[str, str], check: float, out: Any) -> subprocess.Popen:
    if not (shutil.which("systemd-run") and env.get("XDG_RUNTIME_DIR")):
        return _popen(args, env, out)
    cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
           f"--unit={unit}-{int(time.time())}", "--"] + args
    proc = _popen(cmd, env, out)
    deadline = time.monotonic() + check
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            if rc != 0:
                log.warning("systemd-run terminó con código %s; lanzo %s directamente", rc, os.path.basename(args[0]))
                return _popen(args, env, out)
            break
        await asyncio.sleep(0.1)
    return proc


# ---------------------------------------------------------------- procesos abiertos
# ---------------------------------------------------------------- procesos
_PROC_CACHE: tuple[float, list[tuple[int, list[str]]]] = (0.0, [])
_PROC_TTL = 0.3


def proc_table(fresh: bool = False) -> list[tuple[int, list[str]]]:
    """(PID, argv) de los procesos del sistema. Se guarda un instante: cada tecla pregunta qué app está
    abierta, y con varias actividades serían varios recorridos de /proc por tecla."""
    global _PROC_CACHE
    now = time.monotonic()
    if not fresh and now - _PROC_CACHE[0] < _PROC_TTL:
        return _PROC_CACHE[1]
    out = []
    for d in Path("/proc").glob("[0-9]*"):
        try:
            raw = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if raw:
            out.append((int(d.name), [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]))
            continue
    _PROC_CACHE = (now, out)
    return out


def find_pids(pred: Callable[[list[str]], bool], fresh: bool = False) -> list[int]:
    own = os.getpid()
    return [pid for pid, argv in proc_table(fresh) if pid != own and argv and pred(argv)]


def invalidate_procs() -> None:
    global _PROC_CACHE
    _PROC_CACHE = (0.0, [])


async def run_cmd(cmd: list[str], timeout: float) -> tuple[int, str]:
    env = graphical_env()
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        return 127, str(exc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "sin respuesta"
    return proc.returncode or 0, out.decode("utf-8", "replace")
