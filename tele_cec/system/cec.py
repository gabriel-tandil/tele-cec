"""Lectura del control remoto por HDMI-CEC.

Usa `cec-client` (paquete cec-utils, la misma libcec que Kodi y cec-mini-kb) como
proceso hijo y parsea el tráfico CEC que imprime con -d 15:

    TRAFFIC: [          433]	>> 01:44:01

  ">> 01"  = de la dirección lógica 0 (TV) a la 1 (nosotros)
  "44"     = USER_CONTROL_PRESSED,  "01" = código de tecla (UP)

Igual que cec-mini-kb, la acción se dispara con la pulsación (0x44). La
liberación (0x45) se usa para distinguir la auto-repetición de la TV (0x44
seguidos sin 0x45 = botón mantenido) de dos pulsaciones deliberadas.

Recuperación: si cec-client termina, no logra abrir el adaptador, el dispositivo
/dev/cecN desaparece o acumula errores, se reinicia con espera creciente.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..keys import ALIAS, NAME_TO_CODE, OPCODES, parse_key

log = logging.getLogger("cec")

TRAFFIC_RE = re.compile(r"^TRAFFIC:\s*\[\s*\d+\]\s*>>\s*([0-9a-fA-F]{2})((?::[0-9a-fA-F]{2})*)\s*$")
LEVEL_RE = re.compile(r"^(ERROR|WARNING|NOTICE|TRAFFIC|DEBUG):\s*\[\s*\d+\]\s*(.*)$")
COMPORT_RE = re.compile(r"^\s*com port:\s*(\S+)")

OP_KEY_PRESSED = 0x44
OP_KEY_RELEASED = 0x45
OP_STANDBY = 0x36


@dataclass
class CecFrame:
    initiator: int
    destination: int
    opcode: int | None
    params: list[int] = field(default_factory=list)
    raw: str = ""

    @property
    def opcode_name(self) -> str:
        if self.opcode is None:
            return "POLL"
        return OPCODES.get(self.opcode, f"0x{self.opcode:02X}")


def parse_traffic(line: str) -> CecFrame | None:
    m = TRAFFIC_RE.match(line.strip())
    if not m:
        return None
    addr = int(m.group(1), 16)
    rest = [int(x, 16) for x in m.group(2).split(":") if x]
    return CecFrame(
        initiator=addr >> 4,
        destination=addr & 0xF,
        opcode=rest[0] if rest else None,
        params=rest[1:],
        raw=m.group(1) + m.group(2),
    )


KeyCallback = Callable[[int], Awaitable[None] | None]
FrameCallback = Callable[[CecFrame], Awaitable[None] | None]


async def _call(cb, *args) -> None:
    if cb is None:
        return
    res = cb(*args)
    if asyncio.iscoroutine(res):
        await res


class CecClientSource:
    """Mantiene vivo un cec-client y entrega las teclas recibidas."""

    READY_TIMEOUT = 45.0     # segundos para que cec-client diga "waiting for input"
    ERROR_BURST = 12         # errores en ERROR_WINDOW segundos => reinicio
    ERROR_WINDOW = 60.0

    def __init__(self, cec_cfg: dict, on_key: KeyCallback, on_frame: FrameCallback | None = None,
                 activate_on_start: bool = True, echo_raw: bool = False, on_release=None):
        self.cfg = cec_cfg
        self.on_key = on_key
        self.on_release = on_release
        self.on_frame = on_frame
        self.activate_on_start = activate_on_start
        self.echo_raw = echo_raw
        self.proc: asyncio.subprocess.Process | None = None
        self.ready = asyncio.Event()
        self.port_in_use: str | None = None
        self._activated_once = False
        self._stopping = False
        self._errors: list[float] = []
        self._last_warn: dict[str, float] = {}

    # ------------------------------------------------------------------ API
    async def send(self, command: str) -> bool:
        """Manda un comando de consola a cec-client (por ejemplo "as" = fuente activa)."""
        proc = self.proc
        if not proc or proc.returncode is not None or not proc.stdin or not self.ready.is_set():
            log.debug("cec-client no está listo; no se manda %r", command)
            return False
        try:
            proc.stdin.write((command + "\n").encode())
            await proc.stdin.drain()
            log.debug("cec-client <- %s", command)
            return True
        except (BrokenPipeError, ConnectionResetError) as exc:
            log.warning("No pude escribirle a cec-client: %s", exc)
            return False

    async def activate_source(self) -> None:
        if await self.send("as"):
            log.info("CEC: anunciado como fuente activa")

    async def run(self) -> None:
        delay = 2.0
        while not self._stopping:
            started = time.monotonic()
            try:
                await self._run_once()
            except FileNotFoundError:
                log.error("No encuentro '%s'. Instalalo con: sudo apt install cec-utils", self.cfg.get("cec_client"))
                delay = 30.0
            except asyncio.CancelledError:
                await self.stop()
                raise
            except Exception:  # noqa: BLE001
                log.exception("Error inesperado leyendo CEC")
            if self._stopping:
                break
            if time.monotonic() - started > 120:
                delay = 2.0  # funcionó un buen rato: reintento rápido
            log.warning("CEC: reintento en %.0f s", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def stop(self) -> None:
        self._stopping = True
        await self._kill()

    # ------------------------------------------------------------ interno
    def _command(self) -> list[str]:
        binary = self.cfg.get("cec_client") or "cec-client"
        if not os.path.isabs(binary) and not shutil.which(binary):
            raise FileNotFoundError(binary)
        cmd = [binary, "-t", self.cfg.get("device_type", "r"), "-d", "15"]
        name = (self.cfg.get("osd_name") or "").strip()
        if name:
            cmd += ["-o", name[:13]]
        port = (self.cfg.get("port") or "").strip()
        if port:
            cmd.append(port)
        return cmd

    async def _run_once(self) -> None:
        cmd = self._command()
        log.info("Iniciando: %s", " ".join(cmd))
        self.ready.clear()
        self._errors.clear()
        self.port_in_use = (self.cfg.get("port") or "").strip() or None
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        watchdog = asyncio.create_task(self._watchdog())
        try:
            assert self.proc.stdout
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                await self._handle_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except asyncio.CancelledError:
            self._stopping = True
            raise
        finally:
            watchdog.cancel()
            await self._kill()
            rc = self.proc.returncode if self.proc else None
            self.ready.clear()
            if not self._stopping:
                log.warning("cec-client terminó (código %s)", rc)

    async def _handle_line(self, line: str) -> None:
        if not line.strip():
            return
        if self.echo_raw:
            print(f"    [cec-client] {line}", flush=True)

        frame = parse_traffic(line)
        if frame is not None:
            await self._handle_frame(frame)
            return

        m = LEVEL_RE.match(line)
        if m:
            level, msg = m.groups()
            if level == "ERROR":
                self._note_error(msg)
            elif level == "WARNING":
                self._warn_limited(msg)
            elif level in ("NOTICE", "TRAFFIC"):
                log.debug("cec-client: %s", msg)
            return

        m = COMPORT_RE.match(line)
        if m:
            self.port_in_use = m.group(1)
            log.info("Adaptador CEC detectado: %s", self.port_in_use)
            return

        low = line.lower()
        if "waiting for input" in low:
            if not self.ready.is_set():
                self.ready.set()
                log.info("CEC listo (%s)", self.port_in_use or "autodetectado")
                if self.activate_on_start and not self._activated_once:
                    self._activated_once = True
                    await self.activate_source()
        elif "unable to open" in low or "failed" in low or "cannot load" in low:
            log.error("cec-client: %s", line.strip())
        else:
            log.debug("cec-client: %s", line.strip())

    async def _handle_frame(self, frame: CecFrame) -> None:
        await _call(self.on_frame, frame)
        if frame.opcode == OP_KEY_PRESSED and frame.params:
            await _call(self.on_key, frame.params[0])
        elif frame.opcode == OP_KEY_RELEASED:
            await _call(self.on_release)

    def _note_error(self, msg: str) -> None:
        now = time.monotonic()
        self._errors = [t for t in self._errors if now - t < self.ERROR_WINDOW] + [now]
        self._warn_limited(msg, level=logging.ERROR)
        if len(self._errors) >= self.ERROR_BURST and self.proc and self.proc.returncode is None:
            log.error("Demasiados errores de CEC en poco tiempo: reinicio cec-client")
            self._errors.clear()
            asyncio.get_running_loop().create_task(self._kill())

    def _warn_limited(self, msg: str, level: int = logging.WARNING) -> None:
        """Evita inundar el journal con el mismo mensaje."""
        now = time.monotonic()
        key = re.sub(r"\d+", "#", msg)[:120]
        if now - self._last_warn.get(key, -1e9) > 60:
            self._last_warn[key] = now
            log.log(level, "cec-client: %s", msg)

    async def _watchdog(self) -> None:
        """Reinicia cec-client si no arranca a tiempo o si desaparece el dispositivo."""
        try:
            await asyncio.wait_for(self.ready.wait(), self.READY_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("cec-client no respondió en %.0f s: reinicio", self.READY_TIMEOUT)
            await self._kill()
            return
        while True:
            await asyncio.sleep(5)
            port = self.port_in_use
            if port and port.startswith("/dev/") and not os.path.exists(port):
                log.error("Desapareció el adaptador CEC %s: reinicio cec-client", port)
                await self._kill()
                return

    async def _kill(self) -> None:
        proc = self.proc
        if not proc or proc.returncode is not None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write(b"q\n")
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), 3)
            return
        except asyncio.TimeoutError:
            pass
        for sig in ("terminate", "kill"):
            try:
                getattr(proc, sig)()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), 3)
                return
            except asyncio.TimeoutError:
                continue


class StdinSource:
    """Fuente de teclas para probar sin control: se escriben nombres por consola.

    Acepta nombres libcec (UP, SELECT, F2_RED, NUMBER1), números (1 = NUMBER1),
    códigos (0x71) y alias (u d l r ok b m rojo verde amarillo azul; m = menú).
    """

    def __init__(self, on_key: KeyCallback, on_release=None):
        self.on_key = on_key
        self.on_release = on_release
        self.ready = asyncio.Event()
        self.ready.set()

    async def send(self, command: str) -> bool:  # compatibilidad con CecClientSource
        log.debug("(modo teclado) se ignora el comando CEC %r", command)
        return False

    async def activate_source(self) -> None:
        return None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        print("Modo teclado: escribí teclas (u d l r ok b m=menú rojo verde amarillo azul 1..9, "
              "o nombres libcec como F2_RED). Ctrl+D para salir.", file=sys.stderr, flush=True)
        # Hilo daemon propio (no run_in_executor): readline() bloquea hasta que llega una línea, y
        # asyncio.run espera a los hilos del executor al salir, así que con SIGTERM el proceso
        # quedaba colgado minutos después de "Servicio detenido".
        lines: asyncio.Queue[str] = asyncio.Queue()

        def reader() -> None:
            try:
                for raw in iter(sys.stdin.readline, ""):
                    loop.call_soon_threadsafe(lines.put_nowait, raw)
                loop.call_soon_threadsafe(lines.put_nowait, "")
            except RuntimeError:  # el loop ya se cerró (el servicio terminó)
                pass

        threading.Thread(target=reader, name="stdin", daemon=True).start()
        while True:
            line = await lines.get()
            if not line:
                log.info("Fin de la entrada estándar")
                return
            for token in line.split():
                low = token.lower()
                if low in ALIAS:
                    code = NAME_TO_CODE[ALIAS[low]]
                else:
                    code = parse_key(token)
                if code is None:
                    print(f"  tecla desconocida: {token}", file=sys.stderr, flush=True)
                    continue
                await _call(self.on_key, code)
                await _call(self.on_release)

    async def stop(self) -> None:
        return None
