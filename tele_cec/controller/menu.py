"""El menú de actividades en el panel: elegir actividad, el selector de carpetas (Videos, Fotos) y la ayuda."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import apps as activities_mod
from ..ui import help as help_mod
from .activities import KEEP_PLAYING

log = logging.getLogger("tele")

# Ayuda al pie del menú.
MENU_HINT = "▲ ▼ elegir · OK abrir · ▶ otra carpeta (Videos, Fotos) · Atrás cerrar"
FOLDER_HINT = "▲ ▼ elegir · OK abrir · ◀ volver"
HELP_HINT = "◀ ▶ otra app · ▲ ▼ más filas · Atrás volver"


class MenuMixin:
    def _init_menu(self) -> None:
        self._menu_open = False
        self._menu_items: list[dict] = []
        self._menu_sel = 0
        self._menu_timer: asyncio.Task | None = None
        self._menu_timeout = float(self.cfg.menu.get("timeout_s", 10) or 0)
        # Selector de carpetas (▶ sobre Videos o Fotos): qué actividad, en qué carpeta (None = las configuradas)
        # y por dónde se vino (para volver con ◀ a la carpeta de arriba y a la misma fila).
        self._menu_mode = "activities"
        self._menu_act_sel = 0
        self._picker_act = ""
        self._picker_path: Path | None = None
        self._picker_stack: list[tuple[Path | None, int]] = []
        # Ayuda (desde el menú o con la tecla Info/Ayuda): páginas por app y desde dónde se abrió.
        self._menu_from_ctx = "offline"
        self._help_pages: list[tuple[str, str]] = []
        self._help_idx = 0
        self._help_top = 0
        self._help_from_menu = False

    # ---------------------------------------------------------------- menú de actividades
    def _menu_order(self) -> list[str]:
        items = [str(i).strip().lower() for i in (self.cfg.menu.get("items") or [])]
        out = [i for i in items if i in self.activities]
        for unknown in (i for i in items if i not in self.activities):
            log.warning("menu.items: no hay ninguna actividad «%s»", unknown)
        out += [k for k in self.activities if k not in out and k not in ("netflix", "youtube", "tidal", "videos", "fotos")]
        return out

    def _menu_entries(self) -> list[dict]:
        """Las opciones del menú (en un hilo aparte: mira qué está instalado y busca contenido en carpetas)."""
        cur = self.current_app()
        out = []
        for ident in self._menu_order():
            act = self.activities[ident]
            try:
                if not act.installed():
                    continue
                sub, enabled = act.source()
            except Exception:  # noqa: BLE001
                log.exception("Menú: no pude revisar %s", ident)
                continue
            item = {"id": ident, "name": act.name, "icon": act.icon, "sub": sub, "enabled": enabled,
                    "current": ident == cur}
            if ident != cur and ident in KEEP_PLAYING.get(cur or "", set()) and act.running():
                item["badge"] = "sonando"  # p. ej. Tidal de fondo mientras pasan las fotos
            out.append(item)
        out.append({"id": "_help", "name": "Ayuda", "icon": "ayuda.svg", "sub": "Qué hace cada tecla del control",
                    "enabled": True, "current": False})
        return out

    def _menu_restart_timer(self) -> None:
        if self._menu_timer:
            self._menu_timer.cancel()
        self._menu_timer = None
        if self._menu_timeout > 0:
            self._menu_timer = asyncio.get_running_loop().create_task(self._menu_autoclose())

    def _menu_wait(self) -> float:
        """Cuánto espera el menú sin teclas antes de cerrarse (la ayuda se lee con más calma)."""
        return max(self._menu_timeout, 60.0) if self._menu_mode == "help" else self._menu_timeout

    async def _menu_autoclose(self) -> None:
        wait = self._menu_wait()
        await asyncio.sleep(wait)
        if self._menu_open and not self._switching:
            log.info("Menú cerrado solo (sin teclas en %.0f s)", wait)
            await self.menu_close()

    async def menu_open(self, sel: int | None = None) -> None:
        items = await asyncio.to_thread(self._menu_entries)
        if not items:
            await self.toast("No hay actividades para elegir")
            return
        if sel is None:
            sel = next((i for i, it in enumerate(items) if it["current"]), 0)
        sel = max(0, min(sel, len(items) - 1))
        if self._menu_mode == "activities" and not self._menu_open and self._ctx != "menu":
            self._menu_from_ctx = self._ctx  # para que la ayuda empiece por la app que se estaba usando
        self._menu_mode = "activities"
        self._menu_items, self._menu_sel = items, sel
        if not await self.overlay.show(items, sel, hint=MENU_HINT):
            log.warning("No pude mostrar el menú: %s", self.overlay.error or "?")
            await self.toast("No pude mostrar el menú")
            return
        self._menu_open = True
        log.info("Menú: %s", " · ".join(("[" + it["name"] + "]") if i == sel else it["name"]
                                        for i, it in enumerate(items)))
        self._menu_restart_timer()

    async def menu_close(self) -> None:
        self._menu_open = False
        self._menu_mode = "activities"
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        await self.overlay.hide()

    async def _menu_move(self, delta: int) -> None:
        n = len(self._menu_items)
        if not n:
            return
        self._menu_sel = (self._menu_sel + delta) % n
        await self.overlay.select(self._menu_sel)
        log.debug("Menú: %s", self._menu_items[self._menu_sel]["name"])
        self._menu_restart_timer()

    async def _menu_choose(self, index: int) -> None:
        if not 0 <= index < len(self._menu_items):
            self._menu_restart_timer()
            return
        if self._menu_mode == "folders":
            await self._picker_choose(index)
            return
        if self._menu_mode == "help":
            self._menu_restart_timer()
            return
        item = self._menu_items[index]
        if item["id"] == "_help":
            self._menu_act_sel = index
            await self.help_open(from_menu=True)
            return
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        if not item["enabled"]:
            await self.overlay.toast(self.activities[item["id"]].unavailable_text(), 2500)
            self._menu_restart_timer()
            return
        if item["id"] == self.current_app():
            log.info("Menú: sigo en %s", item["name"])
            await self.menu_close()
            return
        log.info("Menú: elijo %s", item["name"])
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True  # desde ya: que ninguna tecla corte el cambio
        await self.overlay.busy(f"Abriendo {item['name']}…")
        self._macro_task = asyncio.create_task(self._menu_switch(item))

    async def _menu_switch(self, item: dict) -> None:
        ok = False
        try:
            ok = await self.switch_to(item["id"], item.get("folder"))
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Error cambiando a %s", item["name"])
        finally:
            self._switching = False
            self._menu_open = False
            await self.overlay.hide()
        if not ok:
            await self.overlay.toast(f"No pude abrir {item['name']}", 3000)

    async def a_menu(self, arg: str) -> None:
        """menu (abrir; abierto = siguiente opción), menu:up|down|left|right|ok|back|close|open|pick:N.

        ▶ (right) sobre Videos o Fotos abre el selector de carpetas; ahí ◀ (left) o Atrás (back) sube.
        """
        sub, _, rest = arg.strip().lower().partition(":")
        if self._switching:
            return
        if self._menu_open and self._menu_mode == "help":
            await self._help_key(sub)
            return
        if sub == "help":
            self._menu_act_sel = self._menu_sel if self._menu_open else 0
            await self.help_open(from_menu=self._menu_open)
            return
        if sub in ("", "next", "toggle"):
            if self._menu_open:
                await self._menu_move(+1)
            else:
                await self.menu_open()
            return
        if sub == "open":
            if not self._menu_open:
                await self.menu_open()
            return
        if not self._menu_open:
            log.debug("menu:%s con el menú cerrado: lo abro", arg)
            await self.menu_open()
            return
        if sub in ("up", "prev", "previous"):
            await self._menu_move(-1)
        elif sub == "down":
            await self._menu_move(+1)
        elif sub == "close":
            await self.menu_close()
        elif sub == "right":
            if self._menu_mode == "folders":
                await self._picker_enter(self._menu_sel)
            elif self._is_media(self._menu_items[self._menu_sel]["id"]):
                await self._picker_open(self._menu_items[self._menu_sel]["id"])
            else:
                self._menu_restart_timer()
        elif sub == "left":
            if self._menu_mode == "folders":
                await self._picker_back()
            else:
                self._menu_restart_timer()
        elif sub == "back":
            if self._menu_mode == "folders":
                await self._picker_back()
            else:
                await self.menu_close()
        elif sub == "ok":
            await self._menu_choose(self._menu_sel)
        elif sub == "pick":
            try:
                await self._menu_choose(int(rest) - 1)
            except ValueError:
                log.warning("menu:pick:%s inválido (número de opción)", rest)
        else:
            log.warning("menu:%s desconocida (up, down, left, right, ok, back, close, pick:N)", arg)

    # ---------------------------------------------------------------- selector de carpetas (Videos, Fotos)
    def _is_media(self, ident: str) -> bool:
        return isinstance(self.activities.get(ident), activities_mod.MediaActivity)

    def _picker_title(self, act: activities_mod.MediaActivity, path: Path) -> str:
        """«Fotos · En red: fotosRemotas › 2017 › 07» (las últimas partes, si es muy largo)."""
        for root in act.roots():
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            parts = list(rel.parts)
            if len(parts) > 3:
                parts = ["…"] + parts[-3:]
            return " › ".join([activities_mod.describe_folder(root), *parts])
        return str(path)

    def _picker_entries(self, ident: str, path: Path | None) -> tuple[str, list[dict]]:
        """Título y opciones del selector (en un hilo aparte: lista carpetas, que pueden estar en la red)."""
        act = self.activities[ident]
        assert isinstance(act, activities_mod.MediaActivity)
        items: list[dict] = []
        if path is None:
            default = act.folder()
            for root in act.roots():
                items.append({"kind": "dir", "path": str(root), "name": activities_mod.describe_folder(root),
                              "icon": "folder", "sub": "predeterminada" if root == default else "",
                              "enabled": True, "current": False})
            return f"{act.name} · elegir carpeta", items
        has = activities_mod.count_media(path, act.exts) > 0  # corta en el primero (y tiene tope de tiempo)
        items.append({"kind": "play", "path": str(path), "name": act.play_label, "icon": "media-playback-start",
                      "sub": "con sus subcarpetas" if has else act.empty_in_folder, "enabled": has, "current": False})
        for d in activities_mod.list_subfolders(path):
            items.append({"kind": "dir", "path": str(d), "name": d.name, "icon": "folder", "sub": "",
                          "enabled": True, "current": False})
        return f"{act.name} · {self._picker_title(act, path)}", items

    async def _picker_show(self, ident: str, path: Path | None, sel: int = 0) -> bool:
        title, items = await asyncio.to_thread(self._picker_entries, ident, path)
        if not items:
            await self.overlay.toast("No hay carpetas configuradas (ver [apps.videos] / [apps.fotos] folders)", 3000)
            self._menu_restart_timer()
            return False
        self._menu_mode, self._picker_act, self._picker_path = "folders", ident, path
        self._menu_items, self._menu_sel = items, max(0, min(sel, len(items) - 1))
        await self.overlay.show(items, self._menu_sel, title=title, hint=FOLDER_HINT, numbers=False)
        log.info("Menú: %s", title)
        self._menu_restart_timer()
        return True

    async def _picker_open(self, ident: str) -> None:
        self._menu_act_sel = self._menu_sel
        self._picker_stack = []
        await self._picker_show(ident, None)

    async def _picker_enter(self, index: int) -> None:
        item = self._menu_items[index]
        if item.get("kind") != "dir":
            self._menu_restart_timer()
            return
        self._picker_stack.append((self._picker_path, index))
        if not await self._picker_show(self._picker_act, Path(item["path"])):
            self._picker_stack.pop()

    async def _picker_back(self) -> None:
        if self._picker_stack:
            path, sel = self._picker_stack.pop()
            await self._picker_show(self._picker_act, path, sel)
        else:
            await self.menu_open(self._menu_act_sel)

    async def _picker_choose(self, index: int) -> None:
        if index != self._menu_sel:
            self._menu_sel = index
            await self.overlay.select(index)
        item = self._menu_items[index]
        if item.get("kind") == "dir":
            await self._picker_enter(index)
            return
        act = self.activities[self._picker_act]
        if not item.get("enabled", True):
            await self.overlay.toast(item.get("sub") or act.unavailable_text(), 2500)
            self._menu_restart_timer()
            return
        log.info("Menú: %s en %s", act.name, item["path"])
        if self._menu_timer:
            self._menu_timer.cancel()
            self._menu_timer = None
        self._switching = True
        await self.overlay.busy(f"Abriendo {act.name}…")
        self._macro_task = asyncio.create_task(
            self._menu_switch({"id": self._picker_act, "name": act.name, "folder": item["path"]}))

    # ---------------------------------------------------------------- ayuda
    async def help_open(self, from_menu: bool = False) -> None:
        installed = await asyncio.to_thread(
            lambda: {i for i, a in self.activities.items() if a.installed()})
        self._help_pages = help_mod.pages(self.cfg, installed)
        if not from_menu:
            self._menu_from_ctx = self._ctx if self._ctx != "menu" else self._menu_from_ctx
        start = help_mod.page_for(self._menu_from_ctx or "menu")
        self._help_idx = next((i for i, (ctx, _t) in enumerate(self._help_pages) if ctx == start), 0)
        self._help_top = 0
        self._help_from_menu = from_menu
        self._menu_mode = "help"
        self._menu_open = True
        await self._help_render()
        log.info("Ayuda: %s", self._help_pages[self._help_idx][1])

    async def _help_render(self) -> None:
        ctx, title = self._help_pages[self._help_idx]
        rows = help_mod.rows(self.cfg, ctx)
        self._help_top = max(0, min(self._help_top, max(0, len(rows) - 10)))
        page = f"{self._help_idx + 1} / {len(self._help_pages)}"
        await self.overlay.help(f"Ayuda · {title}", rows, self._help_top, page, HELP_HINT)
        self._menu_restart_timer()

    async def _help_key(self, sub: str) -> None:
        if sub in ("left", "right"):
            n = len(self._help_pages)
            self._help_idx = (self._help_idx + (1 if sub == "right" else -1)) % n
            self._help_top = 0
            await self._help_render()
        elif sub in ("up", "down"):
            self._help_top += 1 if sub == "down" else -1
            self._help_top = max(0, self._help_top)
            await self._help_render()
        elif sub in ("ok", "back", "", "next", "help", "close"):
            if self._help_from_menu and sub != "close":
                await self.menu_open(self._menu_act_sel)
            else:
                await self.menu_close()
        else:
            self._menu_restart_timer()

    async def a_help(self, arg: str) -> None:
        """Ayuda: qué hace cada tecla del control en cada app (empieza por la que se está usando)."""
        if self._switching:
            return
        await self.help_open(from_menu=False)
