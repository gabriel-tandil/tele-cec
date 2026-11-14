"""Perfiles de navegador que usan Netflix y YouTube: binarios, user.js y extensiones de Firefox, y el aviso de
«no se cerró correctamente» de Chromium."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger("browser")

ENGINES = ("chromium", "firefox")
BINARIES = {
    "chromium": ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable"),
    "firefox": ("firefox", "firefox-esr"),
}
NAMES = {"chromium": "Chromium", "firefox": "Firefox"}

# Preferencias que tele-cec escribe en el user.js del perfil de Firefox.
FIREFOX_PREFS: dict[str, Any] = {
    # Las "preferencias recomendadas" del agente remoto son para pruebas automáticas (apagan
    # Safe Browsing, el guardado de contraseñas, la protección contra rastreo…): no las queremos.
    "remote.prefs.recommended": False,
    # DRM (Widevine) y reproducción automática.
    "media.eme.enabled": True,
    "media.gmp-widevinecdm.enabled": True,
    "media.gmp-widevinecdm.visible": True,
    "media.autoplay.default": 0,
    "media.autoplay.blocking_policy": 0,
    # Sin carteles de primer inicio, de navegador predeterminado ni de restaurar la sesión.
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "browser.sessionstore.resume_from_crash": False,
    # Después de varios cierres bruscos seguidos (cortes de luz, el servicio reiniciando un Firefox colgado)
    # Firefox arranca preguntando si entrar en "modo seguro" y no sigue hasta que alguien conteste: en la
    # TV, con el control remoto, no hay forma de hacerlo.
    "toolkit.startup.max_resumed_crashes": -1,
    "browser.tabs.warnOnClose": False,
    "browser.warnOnQuit": False,
    "browser.translations.automaticallyPopup": False,
    # Nada encima del video: ni el botón de imagen en imagen ni el aviso de pantalla completa.
    "media.videocontrols.picture-in-picture.video-toggle.enabled": False,
    "full-screen-api.warning.timeout": 0,
    # El joystick lo traduce tele-cec a teclas del control: si la página también lo leyera (YouTube para TV
    # entiende joysticks), cada botón haría dos cosas.
    "dom.gamepad.enabled": False,
}
FIREFOX_PERF_PREFS: dict[str, Any] = {
    "ui.prefersReducedMotion": 1,
    "general.smoothScroll": False,
}


def find_binary(configured: str = "", engine: str = "chromium") -> str | None:
    if configured:
        return configured if (os.path.isabs(configured) or shutil.which(configured)) else None
    for name in BINARIES.get(engine, BINARIES["chromium"]):
        path = shutil.which(name)
        if path:
            return path
    return None


def _js_value(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


USER_JS_HEADER = "// Lo genera tele-cec cada vez que abre Firefox: los cambios a mano se pierden."
_PREF_LINE = re.compile(r'^\s*user_pref\(\s*"((?:[^"\\]|\\.)*)"')


def _pref_keys(text: str) -> set[str]:
    keys = set()
    for line in text.splitlines():
        m = _PREF_LINE.match(line)
        if m:
            try:
                keys.add(json.loads(f'"{m.group(1)}"'))
            except ValueError:
                keys.add(m.group(1))
    return keys


def _drop_prefs(prefs_js: Path, keys: set[str]) -> None:
    try:
        lines = prefs_js.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return
    kept = [ln for ln in lines if not (_pref_keys(ln) & keys)]
    if len(kept) != len(lines):
        tmp = prefs_js.with_suffix(".js.tele-cec")
        tmp.write_text("".join(kept), encoding="utf-8")
        tmp.replace(prefs_js)
        log.info("Preferencias de Firefox que tele-cec ya no fija (vuelven a su valor normal): %s",
                 ", ".join(sorted(keys)))


def write_user_js(profile: Path, prefs: dict[str, Any]) -> None:
    """Escribe el user.js del perfil (Firefox tiene que estar cerrado).

    Firefox copia lo del user.js a su prefs.js y ahí queda aunque después se saque del user.js. Por
    eso, lo que tele-cec fijaba antes y ya no fija (p. ej. reduce_effects = false, o una pref
    borrada de [browser.firefox] prefs) también se borra de prefs.js, para que vuelva a su valor normal.
    """
    profile.mkdir(parents=True, exist_ok=True)
    user_js = profile / "user.js"
    try:
        old = user_js.read_text(encoding="utf-8")
    except OSError:
        old = ""
    if old.startswith(USER_JS_HEADER):  # solo si el user.js anterior era nuestro
        removed = _pref_keys(old) - set(prefs)
        if removed:
            _drop_prefs(profile / "prefs.js", removed)
    lines = [USER_JS_HEADER,
             "// Para agregar o cambiar preferencias usá [browser.firefox] prefs en config.toml."]
    lines += [f"user_pref({_js_value(k)}, {_js_value(v)});" for k, v in prefs.items()]
    user_js.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Extensiones que deshabilitó tele-cec (para volver a habilitarlas si se sacan de la lista).
EXT_STATE_FILE = "tele-cec-extensiones.json"


def _ext_name(addon: dict) -> str:
    return (addon.get("defaultLocale") or {}).get("name") or addon.get("id") or "?"


def profile_extensions(profile: Path) -> list[dict]:
    """Extensiones instaladas en el perfil (no las que trae Firefox): id, nombre, activa, nuestra."""
    try:
        data = json.loads((profile / "extensions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    try:
        ours = set(json.loads((profile / EXT_STATE_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        ours = set()
    return [{"id": a.get("id"), "name": _ext_name(a), "active": bool(a.get("active")) and not a.get("userDisabled"),
             "ours": a.get("id") in ours}
            for a in data.get("addons", [])
            if a.get("type") == "extension" and a.get("location") == "app-profile" and a.get("id") != "newtab@mozilla.org"]


def apply_extension_states(profile: Path, disable: list[str]) -> None:
    """Deshabilita en el perfil las extensiones de [browser.firefox] disable_extensions (por id o por nombre).

    Es lo mismo que apagarlas en about:addons: quedan instaladas. Las que tele-cec había deshabilitado y
    ya no están en la lista se vuelven a habilitar; las que apagaste vos a mano no se tocan. Firefox tiene
    que estar cerrado (al salir reescribe extensions.json).
    """
    want = {str(x).strip().lower() for x in (disable or []) if str(x).strip()}
    mark = profile / EXT_STATE_FILE
    try:
        ours = set(json.loads(mark.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        ours = set()
    if not want and not ours:
        return
    db = profile / "extensions.json"
    try:
        data = json.loads(db.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # perfil nuevo: todavía no hay extensiones
    changed: list[str] = []
    found: set[str] = set()
    new_ours: set[str] = set()
    for a in data.get("addons", []):
        aid = a.get("id") or ""
        keys = {aid.lower(), _ext_name(a).lower()}
        if keys & want:
            found |= keys & want
            if not a.get("userDisabled"):
                a["userDisabled"], a["active"] = True, False
                changed.append(f"deshabilito {_ext_name(a)}")
                new_ours.add(aid)
            elif aid in ours:
                new_ours.add(aid)
        elif aid in ours:
            a["userDisabled"], a["active"] = False, True
            changed.append(f"vuelvo a habilitar {_ext_name(a)}")
    for missing in sorted(want - found):
        log.warning("disable_extensions: no hay ninguna extensión «%s» en el perfil", missing)
    if changed:
        tmp = db.with_suffix(".json.tele-cec")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(db)
        # Caché del estado de las extensiones al arrancar: sin ella, Firefox la rearma desde extensions.json.
        (profile / "addonStartup.json.lz4").unlink(missing_ok=True)
        log.info("Extensiones del perfil: %s", ", ".join(changed))
    if new_ours != ours:
        if new_ours:
            mark.write_text(json.dumps(sorted(new_ours)), encoding="utf-8")
        else:
            mark.unlink(missing_ok=True)


def mark_clean_exit(user_data_dir: Path) -> None:
    """Evita el cartel "Chromium no se cerró correctamente" después de un corte."""
    prefs = user_data_dir / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    profile = data.setdefault("profile", {})
    if profile.get("exit_type") != "Normal" or profile.get("exited_cleanly") is not True:
        profile["exit_type"] = "Normal"
        profile["exited_cleanly"] = True
        try:
            prefs.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
