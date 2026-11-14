"""Comandos del servicio: servicio (lo usa systemd), aprender, tecla y la demostración del menú."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .. import config as config_mod
from ..apps.netflix.browser import Browser
from ..controller import Controller
from ..keys import key_label
from ..system.cec import CecClientSource, CecFrame, StdinSource
from ..system.procs import LOG_DIR, graphical_env, has_display
from . import ipc
from ..web.server import WebRemote

log = logging.getLogger("tele-cec")
SERVICE = "tele-cec.service"


async def _run_until_signal(main_coro) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(main_coro)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:  # pragma: no cover
            pass
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------- servicio
NO_ANNOUNCE_FLAG = LOG_DIR / "no-anunciar-al-arrancar"


def _skip_announce_once() -> bool:
    """`./install.sh --sin-anunciar` deja este archivo: el próximo arranque no se anuncia como fuente activa (la TV
    apagada no se prende ni cambia de entrada por una actualización). Vale una sola vez; abrir una app o despertar
    con el control sí cambian la entrada, como siempre."""
    try:
        NO_ANNOUNCE_FLAG.unlink()
    except OSError:
        return False
    log.info("Arranque sin anunciarme a la TV (actualización con --sin-anunciar)")
    return True


async def cmd_service(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    browser = Browser(cfg.browser)
    ctl = Controller(cfg, browser)
    if args.teclado:
        source = StdinSource(ctl.on_key, on_release=ctl.on_release)
    else:
        act = cfg.cec.get("activate_source") in ("start", "both") and not _skip_announce_once()
        source = CecClientSource(cfg.cec, on_key=ctl.on_key, on_frame=ctl.on_frame,
                                 on_release=ctl.on_release, activate_on_start=act)
    ctl.source = source

    async def main() -> None:
        server = await ipc.serve(ctl)
        web = WebRemote(ctl, cfg.raw.get("web"))
        await web.start()
        tasks = [asyncio.create_task(ctl.run()), asyncio.create_task(source.run())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    raise t.exception()  # type: ignore[misc]
        finally:
            for t in tasks:
                t.cancel()
            await ipc.close(server)
            await web.close()
            await source.stop()
            await ctl.overlay.close()
            await ctl.disconnect()  # con Firefox, termina la sesión de control (si no, queda tomada)

    log.info("Servicio iniciado (%s, %s)", "modo teclado" if args.teclado else "HDMI-CEC", browser.name)
    await _run_until_signal(main())
    log.info("Servicio detenido")
    return 0


# ---------------------------------------------------------------- aprender
def _service_active() -> bool:
    if not shutil.which("systemctl"):
        return False
    r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", SERVICE], check=False)
    return r.returncode == 0


async def cmd_learn(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    restart = False
    if _service_active():
        print("El servicio tele-cec está corriendo y usa el adaptador CEC: lo detengo mientras aprendés.")
        subprocess.run(["systemctl", "--user", "stop", SERVICE], check=False)
        restart = True

    print("\nModo aprendizaje: apretá los botones del control remoto (Ctrl+C para salir).")
    print("Si no aparece nada: elegí en la TV la entrada HDMI de la Pi y cerrá Kodi.\n")

    def on_key(code: int) -> None:
        print(f"  Tecla  {key_label(code):<32} código {code:>3} (0x{code:02X})   -> "
              f"{config_mod.describe_bindings(cfg, code)}", flush=True)

    def on_frame(frame: CecFrame) -> None:
        if args.todo and frame.opcode != 0x44:
            print(f"  CEC    {frame.raw:<20} {frame.opcode_name}  "
                  f"(de {frame.initiator:X} a {frame.destination:X})", flush=True)
        elif frame.opcode in (0x8A, 0x8B, 0x89, 0xA0):
            print(f"  Vendor {frame.raw:<20} {frame.opcode_name}: esta tecla la manda la TV con un "
                  "comando propio del fabricante (no estándar)", flush=True)

    source = CecClientSource(cfg.cec, on_key=on_key, on_frame=on_frame,
                             activate_on_start=cfg.cec.get("activate_source") != "never", echo_raw=args.crudo)

    async def main() -> None:
        await source.run()

    try:
        await _run_until_signal(main())
    finally:
        await source.stop()
        if restart:
            print("\nVuelvo a iniciar el servicio tele-cec.")
            subprocess.run(["systemctl", "--user", "start", SERVICE], check=False)
    return 0


# ---------------------------------------------------------------- tecla
async def cmd_key(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    """Manda teclas al servicio como si vinieran del control remoto (para probar por SSH)."""
    for name in args.teclas:
        resp = await ipc.request({"op": "key", "key": name, "wait": args.esperar})
        if resp is None:
            print("El servicio tele-cec no está corriendo.")
            return 1
        if not resp.get("ok"):
            print(resp.get("error"))
            return 1
        print(f"{name} → contexto después: {resp.get('after')}")
    return 0


# ---------------------------------------------------------------- menu (demostración)
async def cmd_menu(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    """Muestra el panel del menú con opciones de ejemplo, para ver cómo queda en la TV."""
    env = graphical_env()
    if not has_display(env):
        print("No hay sesión gráfica (WAYLAND_DISPLAY/DISPLAY): corrélo en la Pi con el escritorio abierto.")
        return 1
    pkg_root = str(Path(__file__).resolve().parent.parent.parent)
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "tele_cec.ui.menu_ui", "--demo", env=env)
    return await proc.wait()
