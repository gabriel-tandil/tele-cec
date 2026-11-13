"""Avisos del escritorio de Raspberry Pi OS que se cruzan con lo que hace tele-cec.

El Bluetooth del panel (wf-panel-pi) vigila los emparejamientos aunque los haga otro programa: cuando tele-cec
empareja un parlante, el panel abre su ventana «Pairing...» con «Connection successful» y un botón OK, que con el
control no se puede cerrar y queda ahí para siempre (detrás del menú, y después encima de lo que se esté
mirando). Se cierra apretando su OK por accesibilidad (AT-SPI), igual que se manejan los botones de High Tide.

Se llama en un hilo aparte (asyncio.to_thread): las consultas por AT-SPI bloquean.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

log = logging.getLogger("sonido")

PANEL_APP = "wf-panel-pi"
# Título de la ventana de emparejamiento del panel (sin traducir al español por ahora; por las dudas, también
# como quedaría traducido).
PAIRING_TITLES = ("pairing", "empareja", "vinculando")
_BUTTON_ROLES = {"push button", "button"}
_ENTRY_ROLES = {"text", "entry", "password text"}


def _atspi():
    try:
        import gi  # noqa: PLC0415
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: PLC0415
    except (ImportError, ValueError):
        return None
    return Atspi


def _contents(o, showing, out: dict[str, list], depth: int = 0) -> None:
    """Botones, campos de texto y etiquetas visibles dentro de una ventana."""
    if depth > 8:
        return
    for j in range(o.get_child_count()):
        c = o.get_child_at_index(j)
        if c is None or not c.get_state_set().contains(showing):
            continue
        role = c.get_role_name()
        if role in _BUTTON_ROLES:
            out["buttons"].append(c)
        elif role in _ENTRY_ROLES:
            out["entries"].append(c)
        elif role == "label":
            out["labels"].append(c.get_name() or "")
        else:
            _contents(c, showing, out, depth + 1)


def close_pairing_notices() -> list[str]:
    """Cierra las ventanas de emparejamiento del panel que solo informan (un único botón: OK). Las que piden algo
    (confirmar un código, escribir un PIN, aceptar o cancelar) se dejan. Devuelve los textos de las que cerró."""
    Atspi = _atspi()
    if Atspi is None:
        return []
    showing = Atspi.StateType.SHOWING
    closed: list[str] = []
    try:
        desk = Atspi.get_desktop(0)
        for i in range(desk.get_child_count()):
            app = desk.get_child_at_index(i)
            if app is None or app.get_name() != PANEL_APP:
                continue
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is None or not win.get_state_set().contains(showing):
                    continue
                title = (win.get_name() or "").lower()
                if not any(t in title for t in PAIRING_TITLES):
                    continue
                parts: dict[str, list] = {"buttons": [], "entries": [], "labels": []}
                _contents(win, showing, parts)
                if len(parts["buttons"]) != 1 or parts["entries"]:
                    continue
                act = parts["buttons"][0].get_action_iface()
                if act and act.get_n_actions() and act.do_action(0):
                    text = " ".join(t for t in parts["labels"] if t and t.lower() not in title) or win.get_name()
                    closed.append(text)
    except Exception as exc:  # noqa: BLE001  (sin accesibilidad o el panel cambió: no es grave)
        log.debug("No pude revisar los avisos del panel: %s", exc)
    return closed


class PairingNoticeCloser:
    """Mientras tele-cec empareja algo, va cerrando los avisos del panel (cada medio segundo, en un hilo).

    start() al empezar; finish(grace) al terminar: sigue mirando `grace` segundos más en segundo plano (el aviso
    puede llegar un poco después) y se detiene solo.
    """

    def __init__(self, interval: float = 0.5, max_s: float = 120.0) -> None:
        self.interval = interval
        self.max_s = max_s
        self._until = 0.0
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self.closed: list[str] = []

    def start(self) -> None:
        with self._lock:
            self._until = time.monotonic() + self.max_s
        self._task = asyncio.create_task(asyncio.to_thread(self._loop))

    def _loop(self) -> None:
        while True:
            with self._lock:
                if time.monotonic() >= self._until:
                    return
            for text in close_pairing_notices():
                log.info("Cerré el aviso del panel: %s", text)
                self.closed.append(text)
            time.sleep(self.interval)

    def finish(self, grace: float = 8.0) -> None:
        with self._lock:
            self._until = min(self._until, time.monotonic() + grace)
