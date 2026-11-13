"""«Android · conectar» en el panel: elegir el celular y vincular uno nuevo por Wi‑Fi, todo con el control.

Se abre con OK sobre Android cuando no hay ningún celular listo, o con ▶ sobre Android. Muestra:

- los celulares que ve adb (USB o Wi‑Fi): OK muestra su pantalla;
- «Vincular con código QR»: el panel muestra un QR que se escanea desde el celular (Depuración inalámbrica ›
  Vincular dispositivo con código QR). Cuando el celular lo lee anuncia un servicio de vinculación con el nombre
  del QR: tele-cec lo encuentra (avahi-browse), hace `adb pair` y `adb connect`, y abre su pantalla;
- «Vincular con código de 6 dígitos»: el celular muestra el código, se escribe con los números del control y
  tele-cec vincula con el servicio de vinculación que encuentre en la red.

Mientras tanto se buscan en la red los celulares ya vinculados (con la depuración inalámbrica activa) y se conectan
solos: aparecen en la lista. Si se entró con OK y queda uno solo, se abre directo.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .. import apps as activities_mod
from ..apps.android import CONNECT, PAIRING, STATE_TEXT, AndroidActivity, Device, Service
from ..system.procs import LOG_DIR

log = logging.getLogger("tele")

ANDROID_HINT = "▲ ▼ elegir · OK abrir · ◀ volver"
CODE_HINT = "0 – 9 escribir el código · ◀ borrar · Atrás volver"
QR_HINT = "Atrás volver"
PAIR_WAIT_S = 180      # cuánto se espera a que el celular lea el QR o muestre el código
SCAN_EVERY_S = 2.0
CODE_LEN = 6


class AndroidMenuMixin:
    def _init_android_menu(self) -> None:
        self._and_devices: list[Device] = []
        self._and_task: asyncio.Task | None = None     # búsqueda en la red / espera de la vinculación
        self._and_scanning = False
        self._and_autostart = False                    # se entró con OK: si aparece un solo celular, abrirlo
        self._and_code = ""
        self._and_pairing: Service | None = None       # el servicio de vinculación encontrado (código)
        self._and_status = ""

    def _android(self) -> AndroidActivity | None:
        act = self.activities.get("android")
        return act if isinstance(act, AndroidActivity) else None

    def _android_stop(self) -> None:
        if self._and_task and not self._and_task.done() and self._and_task is not asyncio.current_task():
            self._and_task.cancel()
        self._and_task = None
        self._and_scanning = False

    def _in_android(self, mode: str = "android") -> bool:
        return self._menu_open and self._menu_mode == mode and not self._switching

    # ---------------------------------------------------------------- lista de celulares
    async def android_connect_open(self, autostart: bool = False) -> None:
        act = self._android()
        if act is None:
            return
        self._android_stop()
        self._and_autostart = autostart
        self._and_devices = await asyncio.to_thread(act.devices)
        if autostart and self._open_if_single():
            return
        self._and_scanning = act.discovery_available()
        await self._android_render(0)
        log.info("Menú: Android · conectar (%s)", ", ".join(d.label() for d in self._and_devices) or "sin celulares")
        if self._and_scanning:
            self._and_task = asyncio.get_running_loop().create_task(self._android_scan())

    def _android_items(self) -> list[dict]:
        act = self._android()
        items = []
        for d in self._and_devices:
            if d.ready:
                items.append({"id": "_dev", "serial": d.serial, "name": d.nice_name(), "icon": "android.svg",
                              "sub": f"{'Wi‑Fi' if d.wifi else 'USB'} · OK para ver su pantalla", "enabled": True,
                              "current": bool(act and act.running() and act.serial == d.serial)})
            else:
                items.append({"id": "_bad", "serial": d.serial, "name": d.nice_name(), "icon": "android.svg",
                              "sub": STATE_TEXT.get(d.state, f"Celular: {d.state}"), "enabled": False,
                              "current": False})
        if self._and_scanning:
            items.append({"id": "_scan", "name": "Buscando celulares en la red…", "icon": "network-wireless",
                          "sub": "Los vinculados con la depuración inalámbrica activa", "enabled": False,
                          "current": False})
        wifi = bool(act and act.discovery_available())
        if wifi:
            items.append({"id": "_qr", "name": "Vincular por Wi‑Fi con código QR", "icon": "network-wireless",
                          "sub": "Celular: Depuración inalámbrica › Vincular con QR", "enabled": True,
                          "current": False})
            items.append({"id": "_code", "name": "Vincular por Wi‑Fi con código", "icon": "dialog-password",
                          "sub": "Celular: Depuración inalámbrica › Vincular con código", "enabled": True,
                          "current": False})
        items.append({"id": "_usb", "name": "Por USB: enchufalo a la Pi", "icon": "drive-removable-media",
                      "sub": "y aceptá «Permitir depuración USB» en el celular", "enabled": False, "current": False})
        if not self._and_scanning and wifi:
            items.append({"id": "_rescan", "name": "Buscar otra vez", "icon": "view-refresh",
                          "sub": "Celulares conectados y vinculados", "enabled": True, "current": False})
        return items

    async def _android_render(self, sel: int) -> None:
        items = self._android_items()
        sel = max(0, min(sel, len(items) - 1))
        self._menu_mode, self._menu_items, self._menu_sel = "android", items, sel
        await self.overlay.show(items, sel, title="Android · conectar el celular", hint=ANDROID_HINT, numbers=False)
        self._menu_restart_timer()

    def _open_if_single(self) -> bool:
        """Con un solo celular listo (y entrando con OK): abrirlo directo."""
        ready = [d for d in self._and_devices if d.ready]
        if len(ready) != 1:
            return False
        self._android_start(ready[0].serial)
        return True

    async def _android_scan(self) -> None:
        """Conecta los celulares ya vinculados que anuncian la depuración inalámbrica."""
        act = self._android()
        try:
            if act is None:
                return
            services = await asyncio.to_thread(act.browse, CONNECT)
            if services:
                log.info("Android: en la red: %s", ", ".join(f"{s.name} ({s.addr})" for s in services))
            devices = await asyncio.to_thread(act.auto_connect, services)
            devices = await asyncio.to_thread(act.devices) if devices or services else self._and_devices
            if not self._in_android():
                return
            self._and_devices = devices
            self._and_scanning = False
            if self._and_autostart and self._open_if_single():
                return
            await self._android_render(self._menu_sel)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Android: error buscando celulares en la red")
        finally:
            self._and_scanning = False

    async def _android_choose(self, index: int) -> None:
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        item = self._menu_items[index]
        kind = item["id"]
        if kind == "_dev":
            self._android_start(item["serial"])
        elif kind == "_qr":
            await self._android_qr_open()
        elif kind == "_code":
            await self._android_code_open()
        elif kind == "_rescan":
            await self.android_connect_open(autostart=False)
        else:
            await self.overlay.toast(item.get("sub") or "", 2500)
            self._menu_restart_timer()

    def _android_start(self, serial: str) -> None:
        """Abrir la pantalla de ese celular (como elegir Android en el menú)."""
        act = self._android()
        if act is None:
            return
        self._android_stop()
        act.want_serial = serial
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True
        loop = asyncio.get_running_loop()
        loop.create_task(self.overlay.busy(f"Abriendo {act.name}…"))
        self._macro_task = loop.create_task(self._android_switch(act))

    async def _android_switch(self, act: AndroidActivity) -> None:
        if act.running():  # otro celular: se cierra y se abre con el elegido
            await act.close()
        await self._menu_switch({"id": "android", "name": act.name})

    # ---------------------------------------------------------------- vincular con QR
    async def _android_qr_open(self) -> None:
        act = self._android()
        if act is None:
            return
        self._android_stop()
        name, password = act.new_pairing()
        path = LOG_DIR / "android-qr.svg"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not await asyncio.to_thread(act.write_qr, str(path), activities_mod.android.qr_payload(name, password)):
            await self.overlay.toast("Falta qrencode (sudo apt install qrencode)", 3000)
            self._menu_restart_timer()
            return
        self._menu_mode = "android_qr"
        await self.overlay.image(
            "Android · vincular con código QR", str(path),
            ["1. En el celular: Opciones de desarrollador › Depuración inalámbrica (activala y entrá)",
             "2. Tocá «Vincular dispositivo con código QR»",
             "3. Apuntá la cámara a este código"],
            status="Esperando al celular…", hint=QR_HINT)
        log.info("Android: QR de vinculación (%s)", name)
        self._menu_restart_timer()
        self._and_task = asyncio.get_running_loop().create_task(self._android_qr_wait(act, name, password))

    async def _android_qr_wait(self, act: AndroidActivity, name: str, password: str) -> None:
        try:
            deadline = time.monotonic() + PAIR_WAIT_S
            while time.monotonic() < deadline:
                services = await asyncio.to_thread(act.browse, PAIRING, 4)
                if not self._in_android("android_qr"):
                    return
                svc = next((s for s in services if s.name == name), None)
                if svc is None:
                    await asyncio.sleep(SCAN_EVERY_S)
                    continue
                await self.overlay.status("Vinculando…")
                ok, text = await asyncio.to_thread(act.pair, svc.addr, password)
                if not self._in_android("android_qr"):
                    return
                if not ok:
                    await self.overlay.status(f"No se pudo vincular ({text[:60]}): probá otra vez")
                    return
                await self._android_after_pair(act, svc)
                return
            if self._in_android("android_qr"):
                await self.overlay.status("El celular no leyó el código: Atrás y probá otra vez")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Android: error vinculando con QR")

    async def _android_after_pair(self, act: AndroidActivity, svc: Service) -> None:
        """Vinculado: conectar (el celular anuncia otro puerto para eso) y abrir su pantalla."""
        if self._menu_mode == "android_qr":
            await self.overlay.status("Vinculado ✓ · conectando…")
        dev = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and dev is None:
            services = [s for s in await asyncio.to_thread(act.browse, CONNECT, 4) if s.ip == svc.ip]
            for s in services:
                if await asyncio.to_thread(act.connect, s.addr):
                    dev = next((d for d in await asyncio.to_thread(act.devices) if d.serial == s.addr and d.ready),
                               None)
                    if dev:
                        break
            if dev is None:
                await asyncio.sleep(1.5)
            if not self._menu_open or not self._menu_mode.startswith("android"):
                return
        if dev is None:
            await self.android_connect_open()
            await self.overlay.toast("Vinculado, pero no pude conectarlo: «Buscar otra vez»", 4000)
            return
        log.info("Android: vinculado y conectado: %s", dev.label())
        self._android_start(dev.serial)

    # ---------------------------------------------------------------- vincular con código de 6 dígitos
    async def _android_code_open(self) -> None:
        act = self._android()
        if act is None:
            return
        self._android_stop()
        self._and_code = ""
        self._and_pairing = None
        self._and_status = "Buscando el celular en la red…"
        await self._android_code_render()
        self._and_task = asyncio.get_running_loop().create_task(self._android_code_wait(act))

    async def _android_code_render(self) -> None:
        shown = "  ".join(list(self._and_code) + ["_"] * (CODE_LEN - len(self._and_code)))
        items = [
            {"id": "_code", "name": f"Código:  {shown}", "icon": "dialog-password", "sub": self._and_status,
             "enabled": True, "current": False},
            {"id": "_help1", "name": "En el celular", "icon": "android.svg",
             "sub": "Depuración inalámbrica › Vincular con código", "enabled": False, "current": False},
        ]
        self._menu_mode, self._menu_items, self._menu_sel = "android_code", items, 0
        await self.overlay.show(items, 0, title="Android · vincular con código", hint=CODE_HINT, numbers=False)
        self._menu_restart_timer()

    async def _android_code_wait(self, act: AndroidActivity) -> None:
        """Busca el servicio de vinculación (aparece mientras el celular muestra el código)."""
        try:
            deadline = time.monotonic() + PAIR_WAIT_S
            while time.monotonic() < deadline and self._and_pairing is None:
                services = await asyncio.to_thread(act.browse, PAIRING, 4)
                if not self._in_android("android_code"):
                    return
                services = [s for s in services if not s.name.startswith("tele-cec-")]  # los QR viejos, no
                if services:
                    self._and_pairing = services[0]
                    log.info("Android: vinculación ofrecida por %s (%s)", services[0].name, services[0].addr)
                    self._and_status = f"Celular encontrado ({services[0].ip}): escribí el código"
                    await self._android_code_render()
                    if len(self._and_code) == CODE_LEN:
                        await self._android_code_pair()
                    return
                await asyncio.sleep(SCAN_EVERY_S)
            if self._in_android("android_code"):
                self._and_status = "No encontré el celular: ¿muestra el código? Atrás y probá otra vez"
                await self._android_code_render()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Android: error buscando la vinculación")

    async def _android_code_digit(self, digit: str) -> None:
        if len(self._and_code) >= CODE_LEN:
            self._menu_restart_timer()
            return
        self._and_code += digit
        if len(self._and_code) == CODE_LEN and self._and_pairing is None:
            self._and_status = "Esperando al celular… (que muestre el código)"
        await self._android_code_render()
        if len(self._and_code) == CODE_LEN and self._and_pairing is not None:
            self._android_code_pair_soon()

    def _android_code_pair_soon(self) -> None:
        """Vincular en segundo plano (tarda unos segundos): Atrás lo corta."""
        self._android_stop()
        self._and_task = asyncio.get_running_loop().create_task(self._android_code_pair())

    async def _android_code_pair(self) -> None:
        act, svc = self._android(), self._and_pairing
        if act is None or svc is None:
            return
        self._and_status = "Vinculando…"
        await self._android_code_render()
        ok, text = await asyncio.to_thread(act.pair, svc.addr, self._and_code)
        if not self._in_android("android_code"):
            return
        if not ok:
            # El celular cambia de código si falla: hay que volver a buscar su servicio.
            self._and_code = ""
            self._and_pairing = None
            self._and_status = "Código incorrecto o vencido: tocá «Vincular» otra vez en el celular"
            await self._android_code_render()
            self._and_task = asyncio.get_running_loop().create_task(self._android_code_wait(act))
            return
        self._and_status = "Vinculado ✓ · conectando…"
        await self._android_code_render()
        await self._android_after_pair(act, svc)

    # ---------------------------------------------------------------- teclas
    async def _android_key(self, sub: str, rest: str) -> None:
        mode = self._menu_mode
        if mode == "android":
            if sub in ("up", "prev", "previous"):
                await self._menu_move(-1)
            elif sub in ("down", "", "next"):
                await self._menu_move(+1)
            elif sub in ("ok", "right"):
                await self._android_choose(self._menu_sel)
            elif sub in ("left", "back"):
                self._android_stop()
                await self.menu_open(self._menu_act_sel)
            elif sub == "close":
                await self.menu_close()
            else:
                self._menu_restart_timer()
            return
        if mode == "android_code" and sub == "pick" and rest.isdigit() and len(rest) == 1:
            await self._android_code_digit(rest)
            return
        if mode == "android_code" and sub == "left" and self._and_code:
            self._and_code = self._and_code[:-1]
            await self._android_code_render()
            return
        if mode == "android_code" and sub == "ok" and len(self._and_code) == CODE_LEN and self._and_pairing:
            self._android_code_pair_soon()
            return
        if sub in ("left", "back"):
            await self.android_connect_open(autostart=False)
        elif sub == "close":
            await self.menu_close()
        else:
            self._menu_restart_timer()
