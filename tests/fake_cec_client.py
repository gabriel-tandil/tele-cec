#!/usr/bin/env python3
"""cec-client simulado para probar tele-cec sin TV.

Imita la salida real de cec-client -d 15 y manda las teclas de FAKE_KEYS
(códigos decimales separados por comas, ej. "1,1,4,0") cada FAKE_DELAY segundos.
FAKE_CRASH_AFTER=N hace que el proceso "se caiga" tras N teclas (prueba de reinicio).
"""

import os
import select
import sys
import time

keys = [int(k) for k in os.environ.get("FAKE_KEYS", "1,2,3,4,0,13").split(",") if k.strip()]
delay = float(os.environ.get("FAKE_DELAY", "0.5"))
crash_after = int(os.environ.get("FAKE_CRASH_AFTER", "0"))
t0 = time.monotonic()


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def log(level: str, msg: str) -> None:
    out(f"{level:<9}[{int((time.monotonic() - t0) * 1000):16d}]\t{msg}")


out("log level set to 15")
out("== using device type 'recording device'")
out("CEC Parser created - libCEC version 6.0.2")
out("no serial port given. trying autodetect: ")
out(" path:     Linux")
out(" com port: /dev/null")
out("")
out("opening a connection to the CEC adapter...")
log("NOTICE:", "connection opened")
log("TRAFFIC:", "<< 10:47:4e:65:74:66:6c:69:78")
out("waiting for input")

sent = 0
next_at = time.monotonic() + delay
while True:
    r, _, _ = select.select([sys.stdin], [], [], 0.05)
    if r:
        line = sys.stdin.readline()
        if not line:
            pass
        elif line.strip() == "q":
            log("NOTICE:", "closing")
            sys.exit(0)
        elif line.strip() == "as":
            log("TRAFFIC:", "<< 1f:82:10:00")
            out("waiting for input")
    if sent < len(keys) and time.monotonic() >= next_at:
        code = keys[sent]
        log("TRAFFIC:", f">> 01:44:{code:02x}")
        log("TRAFFIC:", ">> 01:45")
        sent += 1
        next_at = time.monotonic() + delay
        if crash_after and sent >= crash_after:
            log("ERROR:", "simulated adapter crash")
            sys.exit(3)
