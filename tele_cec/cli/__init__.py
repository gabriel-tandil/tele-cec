"""Línea de comandos: tele-cec <comando>.

    service.py   servicio (systemd), aprender, tecla, menu
    commands.py  accion, navegador
    doctor.py    diagnostico
    ipc.py       socket local del servicio (los comandos sueltos le piden las cosas a él)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .. import __version__
from .. import config as config_mod
from .commands import cmd_action, cmd_browser
from .doctor import cmd_doctor
from .service import cmd_key, cmd_learn, cmd_menu, cmd_service

log = logging.getLogger("tele-cec")


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
    log.info("tele-cec %s — configuración: %s", __version__, cfg.path or "valores por defecto")
    return cfg


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tele-cec",
        description="Netflix, YouTube, Tidal, videos y fotos en la TV con su control remoto (HDMI-CEC).",
    )
    p.add_argument("-c", "--config", help=f"archivo de configuración (por defecto {config_mod.USER_CONFIG})")
    p.add_argument("--debug", action="store_true", help="logs detallados")
    p.add_argument("--version", action="version", version=f"tele-cec {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="comando")

    s = sub.add_parser("servicio", aliases=["run"], help="servicio principal (lo usa systemd)")
    s.add_argument("--teclado", action="store_true", help="leer teclas desde la consola en vez de CEC (para probar)")

    s = sub.add_parser("aprender", aliases=["learn"], help="mostrar las teclas CEC que llegan del control")
    s.add_argument("--todo", action="store_true", help="mostrar también el resto del tráfico CEC")
    s.add_argument("--crudo", action="store_true", help="mostrar la salida completa de cec-client")

    s = sub.add_parser("accion", aliases=["action"], help="ejecutar una o varias acciones ahora (ej.: nav:down ok)")
    s.add_argument("accion", nargs="+", help="acciones, p. ej. smart_skip  o  profile:1 continue_watching")
    s.add_argument("--esperar", type=float, default=0.5, help="segundos a esperar antes de salir")
    s.add_argument("--directo", action="store_true",
                   help="conectarse al navegador aunque el servicio esté corriendo (no sirve con Firefox)")

    s = sub.add_parser("navegador", aliases=["browser"], help="abrir el navegador listo para el control (o traerlo al frente)")
    s.add_argument("url", nargs="?", help="URL inicial (por defecto browser.start_url)")

    s = sub.add_parser("diagnostico", aliases=["doctor"], help="revisar CEC, el navegador y la página de Netflix")
    s.add_argument("--detalle", action="store_true", help="volcar el estado de la página en JSON")

    sub.add_parser("menu", help="mostrar el menú de actividades de ejemplo (para ver cómo queda en la TV)")

    s = sub.add_parser("tecla", aliases=["key"], help="mandar teclas al servicio como si vinieran del control")
    s.add_argument("teclas", nargs="+", help="nombres libcec o alias: ok, u, d, l, r, b, m (menú), rojo, 1…")
    s.add_argument("--esperar", type=float, default=0.5, help="segundos a esperar después de cada tecla")
    return p


COMMANDS = {
    "servicio": cmd_service, "run": cmd_service,
    "aprender": cmd_learn, "learn": cmd_learn,
    "accion": cmd_action, "action": cmd_action,
    "navegador": cmd_browser, "browser": cmd_browser,
    "diagnostico": cmd_doctor, "doctor": cmd_doctor,
    "menu": cmd_menu,
    "tecla": cmd_key, "key": cmd_key,
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
