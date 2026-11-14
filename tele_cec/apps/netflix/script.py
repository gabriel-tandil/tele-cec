"""El script que se inyecta en la pestaña de Netflix (nfx.js) con su configuración."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...config import Config

NFX_JS = Path(__file__).resolve().parent / "nfx.js"

# Con el control remoto activo Firefox pone navigator.webdriver = true (Chromium no); esto lo oculta.
HIDE_WEBDRIVER_JS = (
    "try { if (navigator.webdriver) Object.defineProperty(Navigator.prototype, 'webdriver', "
    "{ get: () => false, configurable: true, enumerable: true }); } catch (e) { /* nada */ }\n"
)


def build_script(cfg: Config) -> tuple[str, str]:
    src = NFX_JS.read_text(encoding="utf-8")
    fx = cfg.browser.get("firefox") or {}
    if str(cfg.browser.get("engine", "firefox")).lower() == "firefox" and fx.get("hide_webdriver", True):
        src = HIDE_WEBDRIVER_JS + src
    js_cfg = dict(cfg.selectors)
    js_cfg["trusted_click"] = bool(cfg.netflix.get("trusted_click", True))
    js_cfg["reduce_effects"] = bool(cfg.browser.get("reduce_effects", True))
    js_cfg["stop_previews"] = bool(cfg.browser.get("stop_previews", True))
    if js_cfg["reduce_effects"]:
        js_cfg["smooth_scroll"] = False
    cfg_json = json.dumps(js_cfg, ensure_ascii=False)
    version = hashlib.sha1((src + cfg_json).encode("utf-8")).hexdigest()[:12]
    script = src.replace("__NFX_VERSION__", version).replace("__NFX_CONFIG__", cfg_json)
    return script, version
