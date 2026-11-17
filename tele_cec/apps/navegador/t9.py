"""Escribir con los números del control remoto, como en los celulares viejos (T9 multitap).

Cada número tiene sus letras (2 = a b c, 3 = d e f…, y la ñ en el 6); apretarlo seguido pasa a la
letra siguiente y reemplaza la anterior; después de un rato (o con otro número) la letra queda. 0 es el espacio y 1
los signos. Tres modos: abc, ABC y 123 (los números directos).
"""

from __future__ import annotations

import time

KEYS: dict[str, str] = {
    "1": ".,?!'-@/:_1",
    "2": "abc2",
    "3": "def3",
    "4": "ghi4",
    "5": "jkl5",
    "6": "mnño6",
    "7": "pqrs7",
    "8": "tuv8",
    "9": "wxyz9",
    "0": " 0",
}
MODES = ("abc", "ABC", "123")
MODE_TEXT = {"abc": "abc · minúsculas", "ABC": "ABC · MAYÚSCULAS", "123": "123 · números"}
DEFAULT_WINDOW_MS = 1200


class T9:
    def __init__(self, window_ms: int = DEFAULT_WINDOW_MS):
        self.window_s = max(0.3, window_ms / 1000)
        self.mode = "abc"
        self._key = ""
        self._idx = 0
        self._at = 0.0

    def reset(self) -> None:
        self._key, self._idx, self._at = "", 0, 0.0

    def letters(self, key: str) -> str:
        letters = KEYS.get(key, "")
        return letters.upper() if self.mode == "ABC" else letters

    def press(self, key: str, now: float | None = None) -> tuple[str, bool, str, int]:
        """Un número apretado: (carácter, reemplaza al anterior, las letras de esa tecla, cuál es)."""
        now = time.monotonic() if now is None else now
        key = str(key).strip()
        if key not in KEYS:
            return "", False, "", 0
        if self.mode == "123":
            self.reset()
            return key, False, "", 0
        letters = self.letters(key)
        if key == self._key and now - self._at < self.window_s:
            idx, replace = (self._idx + 1) % len(letters), True
        else:
            idx, replace = 0, False
        self._key, self._idx, self._at = key, idx, now
        return letters[idx], replace, letters, idx

    def next_mode(self) -> str:
        self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
        self.reset()
        return self.mode
