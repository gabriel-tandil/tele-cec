"""El menú de actividades en la página del celular: las mismas filas que el panel de la TV y sus opciones.

- las actividades (con su ícono, el texto de abajo, «en uso» / «sonando»), como las arma el menú de la TV;
- tocar una fila hace lo mismo que OK en la TV: abrirla;
- su botoncito, lo mismo que «Carpetas ▸» / «Conectar ▸» en la TV, y algunos más: elegir carpeta (Videos, Fotos),
  elegir juego, elegir celular o dejar de compartir (Android), buscar (Netflix, YouTube), explorar Tidal, salida de
  sonido;
- cerrar la app que está abierta (vuelve el menú en la TV).

Las carpetas y los juegos que se piden desde el celular se validan contra lo configurado (no se puede abrir
cualquier carpeta de la Pi).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import apps as activities_mod

if TYPE_CHECKING:
    from ..controller import Controller

log = logging.getLogger("web")
ICONS = Path(__file__).resolve().parent.parent / "ui" / "icons"
MENU_TTL_S = 6.0          # source() mira carpetas (pueden estar en la red): no en cada consulta


def icon_url(icon: str) -> str:
    icon = str(icon or "")
    return f"/icons/{icon}" if icon.endswith(".svg") and (ICONS / icon).is_file() else ""


class MenuWeb:
    def __init__(self, ctl: Controller, launch):
        self.ctl = ctl
        self.launch = launch   # WebRemote.launch(ident, folder=None)
        self._items: list[dict[str, Any]] = []
        self._at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._at = 0.0

    # ---------------------------------------------------------------- filas
    def _option(self, ident: str, act: Any) -> tuple[str, str]:
        """El botoncito de la fila: (qué abre, cómo se llama)."""
        if isinstance(act, activities_mod.MusicActivity) and not act.command:
            return "music", "Explorar"   # artistas, discos, temas (y las carpetas)
        if isinstance(act, activities_mod.MediaActivity):
            return "folders", "Carpetas"
        if isinstance(act, activities_mod.MameActivity):
            return "games", "Juegos"
        if isinstance(act, activities_mod.AndroidActivity):
            return "android", "Conectar"
        if isinstance(act, activities_mod.TidalActivity) or ident == "tidal":
            return "tidal", "Explorar"
        if ident in ("netflix", "youtube"):
            return "search", "Buscar"
        return "", ""

    def _row(self, e: dict[str, Any]) -> dict[str, Any]:
        ident = e["id"]
        act = self.ctl.activities.get(ident)
        option, chip = self._option(ident, act)
        return {"id": ident, "name": e["name"], "icon": icon_url(e.get("icon", "")),
                "sub": e.get("sub", ""), "enabled": bool(e.get("enabled", True)),
                "current": bool(e.get("current")), "badge": e.get("badge") or "",
                "option": option, "chip": chip, "running": bool(act is not None and act.running()),
                "background": bool(e.get("background"))}

    def _build(self) -> list[dict[str, Any]]:
        ctl = self.ctl
        out = []
        for e in ctl._menu_entries():
            ident = e["id"]
            if ident in ("_help",):
                continue  # la ayuda es de las teclas del control
            if ident == "_sound":
                out.append({"id": "_sound", "name": e["name"], "icon": icon_url(e.get("icon", "")),
                            "sub": e.get("sub", ""), "enabled": True, "current": False, "badge": "",
                            "option": "sound", "chip": ""})
                continue
            if e.get("group"):  # «Videos y música»: una página con las de adentro
                children = [self._row(c) for c in ctl._menu_entries(e["group"])]
                out.append({"id": ident, "group": e["group"], "name": e["name"], "icon": icon_url(e.get("icon", "")),
                            "sub": e.get("sub", ""), "enabled": True, "current": bool(e.get("current")),
                            "badge": e.get("badge") or "", "option": "group", "chip": "",
                            "running": any(c["running"] for c in children), "children": children,
                            "background": bool(e.get("background")), "bg_id": e.get("bg_id", "")})
                continue
            out.append(self._row(e))
        return out

    async def items(self) -> dict[str, Any]:
        async with self._lock:
            if time.monotonic() - self._at > MENU_TTL_S:
                try:
                    self._items = await asyncio.to_thread(self._build)
                except Exception:  # noqa: BLE001
                    log.exception("Página del celular: no pude armar el menú")
                self._at = time.monotonic()
            cur = self.ctl.current_app()
            for it in self._items:  # lo que está en pantalla cambia más seguido que el resto
                for row in it.get("children") or [it]:
                    if row["id"] in self.ctl.activities:
                        row["current"] = row["id"] == cur
                        row["running"] = self.ctl.activities[row["id"]].running()
                if it.get("children"):
                    it["current"] = any(c["current"] for c in it["children"])
                    it["running"] = any(c["running"] for c in it["children"])
            if any(it.get("background") for it in self._items):
                await self.ctl._menu_playback(self._items)  # «sonando» / «en pausa»: cambia seguido
            return {"title": "Actividades", "items": self._items}

    # ---------------------------------------------------------------- carpetas (Videos, Fotos)
    def _media(self, ident: str) -> activities_mod.MediaActivity | None:
        act = self.ctl.activities.get(ident)
        return act if isinstance(act, activities_mod.MediaActivity) else None

    def allowed_folder(self, ident: str, folder: str) -> Path | None:
        """La carpeta, si está dentro de una de las configuradas para esa app (si no, None)."""
        act = self._media(ident)
        if act is None or not folder:
            return None
        try:
            path = Path(folder).resolve()
        except (OSError, RuntimeError):
            return None
        for root in act.roots():
            try:
                path.relative_to(Path(root).resolve())
            except (ValueError, OSError):
                continue
            return path if path.is_dir() else None
        return None

    async def folders(self, query: dict[str, str]) -> dict[str, Any]:
        ident = query.get("app", "")
        act = self._media(ident)
        if act is None:
            return {"error": "esa app no tiene carpetas"}
        path = None
        if query.get("path"):
            path = self.allowed_folder(ident, query["path"])
            if path is None:
                return {"error": "esa carpeta no está entre las configuradas"}
        title, items = await asyncio.to_thread(self.ctl._picker_entries, ident, path)
        if not items:
            return {"error": "No hay carpetas configuradas"}
        return {"title": title, "app": ident, "path": str(path) if path else "",
                "items": [{"kind": it["kind"], "path": it["path"], "name": it["name"], "sub": it.get("sub", ""),
                           "enabled": bool(it.get("enabled", True)), "mode": it.get("mode") or ""} for it in items]}

    # ---------------------------------------------------------------- juegos
    async def games(self) -> dict[str, Any]:
        act = self.ctl.activities.get("juegos")
        if not isinstance(act, activities_mod.MameActivity):
            return {"error": "no hay juegos"}
        items, _sel = await asyncio.to_thread(self.ctl._games_entries)
        playing = act.current_game if act.running() else ""
        games = [{"rom": it["rom"], "name": it["name"], "sub": it.get("sub", ""), "current": bool(it.get("current"))}
                 for it in items if it.get("id") == "_game"]
        return {"title": f"{act.name} · {len(games)} juego{'s' if len(games) != 1 else ''}", "playing": playing,
                "items": games}

    def allowed_rom(self, rom: str) -> bool:
        act = self.ctl.activities.get("juegos")
        return isinstance(act, activities_mod.MameActivity) and any(g.rom == rom for g in act.games())

    # ---------------------------------------------------------------- Android
    async def android(self) -> dict[str, Any]:
        act = self.ctl.activities.get("android")
        if not isinstance(act, activities_mod.AndroidActivity):
            return {"error": "no hay Android"}
        devs = await asyncio.to_thread(act.devices)
        running = act.running()
        return {"title": f"{act.name} · conectar el celular", "running": running,
                "devices": [{"serial": d.serial, "name": d.nice_name(), "wifi": d.wifi, "ready": d.ready,
                             "sub": ("Wi‑Fi" if d.wifi else "USB") if d.ready
                             else activities_mod.android.STATE_TEXT.get(d.state, f"Celular: {d.state}"),
                             "current": running and act.serial == d.serial} for d in devs]}

    # ---------------------------------------------------------------- cerrar
    async def close(self, ident: str) -> dict[str, Any]:
        """Cerrar una app (dejar de compartir el celular, salir del juego…). Si no queda nada, vuelve el menú."""
        ctl = self.ctl
        act = ctl.activities.get(ident)
        if act is None:
            return {"ok": False, "error": "esa app no existe"}
        if not act.running():
            return {"ok": True, "message": f"{act.name} ya estaba cerrado"}
        if ctl._switching:
            return {"ok": False, "error": "Esperá: está cambiando de app"}
        log.info("Celular: cierro %s", act.name)
        ctl.power.note_key()
        ctl._switching = True
        try:
            if ctl._menu_open:
                await ctl.menu_close()
            await ctl.overlay.busy(f"Cerrando {act.name}…")
            if ctl._foreground == ident:
                ctl._foreground = None  # se cierra a propósito: que no parezca que se cerró sola
            await act.close()
        finally:
            ctl._switching = False
            ctl._switch_until = time.monotonic() + 3
            await ctl.overlay.hide()
            self.invalidate()
        if ctl.current_app() is None and not ctl.page:
            await ctl.menu_open()
        return {"ok": True, "message": f"{act.name} cerrado"}
