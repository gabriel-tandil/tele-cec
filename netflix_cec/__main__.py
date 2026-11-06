"""Punto de entrada: netflix-cec <comando>."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import config as config_mod
from .browser import Browser, find_binary, graphical_env, has_display
from .cdp import endpoint_alive, http_json, list_pages
from .cec import CecClientSource, CecFrame, StdinSource
from .controller import Controller
from .keys import key_label

log = logging.getLogger("netflix-cec")
SERVICE = "netflix-cec.service"


# ---------------------------------------------------------------- logging
class JournalFormatter(logging.Formatter):
    """Prefijo <N> para que journald registre la prioridad (journalctl -p warning)."""

    PRIO = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}

    def format(self, record: logging.LogRecord) -> str:
        prio = self.PRIO.get(record.levelno, 6)
        text = super().format(record)
        return "\n".join(f"<{prio}>{line}" for line in text.splitlines())


def _stderr_is_journal() -> bool:
    """systemd define JOURNAL_STREAM=dev:inode; solo vale si coincide con nuestro stderr."""
    value = os.environ.get("JOURNAL_STREAM", "")
    try:
        dev, ino = (int(x) for x in value.split(":"))
        st = os.fstat(sys.stderr.fileno())
    except (ValueError, OSError):
        return False
    return st.st_dev == dev and st.st_ino == ino


def setup_logging(level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    if _stderr_is_journal():
        handler.setFormatter(JournalFormatter("%(name)s: %(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def load_config(args: argparse.Namespace) -> config_mod.Config:
    cfg = config_mod.load(args.config)
    level = "debug" if args.debug else cfg.raw.get("log_level", "info")
    setup_logging(level)
    log.info("netflix-cec %s — configuración: %s", __version__, cfg.path or "valores por defecto")
    return cfg


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
async def cmd_service(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    browser = Browser(cfg.browser)
    ctl = Controller(cfg, browser)
    if args.teclado:
        source = StdinSource(ctl.on_key, on_release=ctl.on_release)
    else:
        act = cfg.cec.get("activate_source") in ("start", "both")
        source = CecClientSource(cfg.cec, on_key=ctl.on_key, on_frame=ctl.on_frame,
                                 on_release=ctl.on_release, activate_on_start=act)
    ctl.source = source

    async def main() -> None:
        tasks = [asyncio.create_task(ctl.run()), asyncio.create_task(source.run())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    raise t.exception()  # type: ignore[misc]
        finally:
            for t in tasks:
                t.cancel()
            await source.stop()
            await ctl.disconnect()

    log.info("Servicio iniciado (%s)", "modo teclado" if args.teclado else "HDMI-CEC")
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
        print("El servicio netflix-cec está corriendo y usa el adaptador CEC: lo detengo mientras aprendés.")
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
            print("\nVuelvo a iniciar el servicio netflix-cec.")
            subprocess.run(["systemctl", "--user", "start", SERVICE], check=False)
    return 0


# ---------------------------------------------------------------- accion
async def cmd_action(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    browser = Browser(cfg.browser)
    ctl = Controller(cfg, browser)
    action = args.accion if len(args.accion) == 1 else list(args.accion)
    first = action if isinstance(action, str) else action[0]
    if not await ctl.connect_once():
        if str(first).startswith("launch"):
            ok = await browser.launch()
            if ok:
                await ctl.connect_once()
        if not ctl.cdp:
            print(f"No hay Chromium escuchando en el puerto {browser.port}. Abrilo con:  netflix-cec navegador")
            return 1
    try:
        ctl._ctx = await ctl.context()
        print(f"Contexto: {ctl._ctx}")
        await ctl.run_action(action)
        await asyncio.sleep(args.esperar)
        print(f"Contexto después: {await ctl.context()}")
    finally:
        await ctl.disconnect()
    return 0


# ---------------------------------------------------------------- navegador
async def cmd_browser(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    browser = Browser(cfg.browser)
    if await endpoint_alive(browser.port):
        ctl = Controller(cfg, browser)
        if await ctl.connect_once():
            try:
                await ctl.send("Page.bringToFront")
            finally:
                await ctl.disconnect()
        print("Chromium ya estaba abierto: lo traje al frente.")
        return 0
    ok = await browser.launch(args.url)
    print("Chromium abierto." if ok else "No pude abrir Chromium (mirá el log de arriba).")
    return 0 if ok else 1


# ---------------------------------------------------------------- diagnostico
def _check(ok: bool | None, text: str) -> None:
    mark = {True: "OK ", False: "MAL", None: " ? "}[ok]
    print(f"  [{mark}] {text}")


async def cmd_doctor(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    print(f"netflix-cec {__version__}\nConfiguración: {cfg.path or 'valores por defecto (no hay config.toml)'}\n")
    print("CEC")
    cec_bin = cfg.cec.get("cec_client") or "cec-client"
    _check(bool(shutil.which(cec_bin) or os.path.isfile(cec_bin)), f"cec-client: {shutil.which(cec_bin) or 'no encontrado (sudo apt install cec-utils)'}")
    devs = sorted(str(p) for p in Path("/dev").glob("cec*"))
    _check(bool(devs), f"dispositivos CEC: {', '.join(devs) or 'ninguno (¿dtoverlay vc4-kms-v3d?)'}")
    for d in devs:
        _check(os.access(d, os.R_OK | os.W_OK), f"permiso de lectura/escritura en {d}" + ("" if os.access(d, os.R_OK | os.W_OK) else " (agregá tu usuario al grupo video)"))
    _check(_service_active(), "servicio netflix-cec activo" if _service_active() else "servicio netflix-cec no está activo")

    print("\nChromium")
    browser = Browser(cfg.browser)
    binary = find_binary(cfg.browser.get("binary", ""))
    _check(bool(binary), f"binario: {binary or 'no encontrado'}")
    env = graphical_env()
    _check(has_display(env), f"sesión gráfica: WAYLAND_DISPLAY={env.get('WAYLAND_DISPLAY', '-')} DISPLAY={env.get('DISPLAY', '-')}")
    _check(True, f"perfil: {browser.user_data_dir} ({'existe' if browser.user_data_dir.exists() else 'se crea al abrir'})")
    alive = await endpoint_alive(browser.port)
    _check(alive, f"DevTools en 127.0.0.1:{browser.port}" + ("" if alive else " — abrilo con: netflix-cec navegador"))
    if not alive:
        return 1
    ver = await http_json(browser.port, "/json/version")
    _check(True, f"versión: {ver.get('Browser')}")
    pages = await list_pages(browser.port)
    for p in pages:
        print(f"         pestaña: {p.get('url')}")
    ctl = Controller(cfg, browser)
    if not await ctl.connect_once():
        _check(False, "no pude conectarme a la pestaña")
        return 1
    try:
        dump = await ctl.js("dump")
        print("\nNetflix (página actual)")
        _check(True, f"URL: {dump.get('url')}")
        _check(True, f"contexto: {dump.get('ctx')}   modal abierto: {dump.get('modal')}")
        _check(dump.get("candidates", 0) > 0, f"elementos navegables: {dump.get('candidates')} (en pantalla: {dump.get('inView')})")
        _check(True, f"filas: {dump.get('rows')}   fila Seguir viendo: {'sí' if dump.get('cwRow') else 'no'}")
        if dump.get("profiles"):
            _check(True, f"perfiles: {', '.join(dump['profiles'])}")
        if dump.get("video"):
            _check(dump.get("playerApi"), "API interna del reproductor de Netflix" + ("" if dump.get("playerApi") else " no disponible (se usan atajos de teclado)"))
        if args.detalle:
            print("\nDetalle (JSON):")
            print(json.dumps(dump, indent=2, ensure_ascii=False))
        else:
            print("\n  (usá --detalle para ver los data-uia visibles y los métodos del reproductor)")
        await ctl.toast("netflix-cec: diagnóstico OK", 2500)
    finally:
        await ctl.disconnect()
    return 0


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="netflix-cec",
        description="Controla Netflix en Chromium con el control remoto de la TV (HDMI-CEC).",
    )
    p.add_argument("-c", "--config", help=f"archivo de configuración (por defecto {config_mod.USER_CONFIG})")
    p.add_argument("--debug", action="store_true", help="logs detallados")
    p.add_argument("--version", action="version", version=f"netflix-cec {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="comando")

    s = sub.add_parser("servicio", aliases=["run"], help="servicio principal (lo usa systemd)")
    s.add_argument("--teclado", action="store_true", help="leer teclas desde la consola en vez de CEC (para probar)")

    s = sub.add_parser("aprender", aliases=["learn"], help="mostrar las teclas CEC que llegan del control")
    s.add_argument("--todo", action="store_true", help="mostrar también el resto del tráfico CEC")
    s.add_argument("--crudo", action="store_true", help="mostrar la salida completa de cec-client")

    s = sub.add_parser("accion", aliases=["action"], help="ejecutar una o varias acciones ahora (ej.: nav:down ok)")
    s.add_argument("accion", nargs="+", help="acciones, p. ej. smart_skip  o  profile:1 continue_watching")
    s.add_argument("--esperar", type=float, default=0.5, help="segundos a esperar antes de salir")

    s = sub.add_parser("navegador", aliases=["browser"], help="abrir Chromium listo para el control (o traerlo al frente)")
    s.add_argument("url", nargs="?", help="URL inicial (por defecto browser.start_url)")

    s = sub.add_parser("diagnostico", aliases=["doctor"], help="revisar CEC, Chromium y la página de Netflix")
    s.add_argument("--detalle", action="store_true", help="volcar el estado de la página en JSON")
    return p


COMMANDS = {
    "servicio": cmd_service, "run": cmd_service,
    "aprender": cmd_learn, "learn": cmd_learn,
    "accion": cmd_action, "action": cmd_action,
    "navegador": cmd_browser, "browser": cmd_browser,
    "diagnostico": cmd_doctor, "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args)
    except (OSError, ValueError) as exc:
        setup_logging("info")
        log.error("Error en la configuración: %s", exc)
        return 2
    try:
        return asyncio.run(COMMANDS[args.cmd](args, cfg))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
