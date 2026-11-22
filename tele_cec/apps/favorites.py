"""Favoritos (1 = ★ en el control): una lista de rutas guardada en ~/.local/share/tele-cec, una por línea, en el orden
en que se guardaron. La usan Fotos («★ Favoritas») y Videos («★ Favoritos»)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..system.procs import DATA_DIR

log = logging.getLogger("actividades")


class FavoritesMixin:
    FAVORITES_FILE = "favoritos.txt"   # cada app pone el suyo
    name = ""

    _favorites: list[str] | None = None   # se leen la primera vez que hacen falta

    def favorites_file(self) -> Path:
        return DATA_DIR / self.FAVORITES_FILE

    def favorites(self) -> list[str]:
        """Las rutas guardadas, en el orden en que se guardaron (sin repetidas)."""
        if self._favorites is None:
            try:
                lines = self.favorites_file().read_text("utf-8").splitlines()
            except OSError:
                lines = []
            self._favorites = list(dict.fromkeys(ln.strip() for ln in lines if ln.strip()))
        return list(self._favorites)

    def _save_favorites(self) -> bool:
        f = self.favorites_file()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text("".join(p + "\n" for p in self._favorites or []), "utf-8")
            tmp.replace(f)
        except OSError as exc:
            log.warning("%s: no pude guardar los favoritos (%s)", self.name, exc)
            return False
        return True

    def is_favorite(self, path: str) -> bool:
        return path in self.favorites()

    def toggle_favorite(self, path: str) -> bool | None:
        """Lo guarda en favoritos (True) o lo saca si ya estaba (False); None si no se pudo guardar."""
        favs = self.favorites()
        if path in favs:
            favs.remove(path)
            added = False
        else:
            favs.append(path)
            added = True
        self._favorites = favs
        if not self._save_favorites():
            return None
        log.info("%s: %s %s (%d favoritos)", self.name, "guardo" if added else "saco de favoritos",
                 os.path.basename(path), len(favs))
        return added

    def favorite_files(self, folder: Path | None = None) -> list[str]:
        """Los favoritos que todavía existen (de todas las carpetas, no solo de esta: son pocos y van juntos)."""
        return [p for p in self.favorites() if os.path.isfile(p)]
