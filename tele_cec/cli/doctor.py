"""tele-cec diagnostico: revisa CEC, la alimentación, Tidal, el menú, el reposo, el navegador y la página."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .. import __version__
from .. import apps as activities_mod
from .. import config as config_mod
from ..apps.netflix.browser import Browser
from ..apps.tidal.hightide import HighTide
from ..apps.tidal.hightide import tools_status as music_tools
from ..apps.webprofile import profile_extensions
from ..controller import Controller
from ..system.procs import graphical_env, has_display
from ..ui import overlay as overlay_mod
from . import ipc
from .commands import _connect_direct
from .service import _service_active


# ---------------------------------------------------------------- diagnostico
def _check(ok: bool | None, text: str) -> None:
    mark = {True: "OK ", False: "MAL", None: " ? "}[ok]
    print(f"  [{mark}] {text}")


def _check_power() -> None:
    """Raspberry Pi: baja tensión o temperatura frenan la CPU (p. ej. de 1800 a 600 MHz) y todo anda lento."""
    vcgencmd = shutil.which("vcgencmd")
    if not vcgencmd:
        return
    try:
        out = subprocess.run([vcgencmd, "get_throttled"], capture_output=True, text=True, timeout=5).stdout
        flags = int(out.strip().split("=", 1)[1], 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return
    clock = ""
    try:
        # La frecuencia real la da el firmware: la del kernel (scaling_cur_freq) no refleja el freno.
        out = subprocess.run([vcgencmd, "measure_clock", "arm"], capture_output=True, text=True, timeout=5).stdout
        cur = int(out.strip().split("=", 1)[1]) // 1_000_000
        top = int(Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq").read_text()) // 1000
        clock = f" (CPU a {cur} de {top} MHz ahora)"
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    print("\nAlimentación (Raspberry Pi)")
    now = [txt for bit, txt in ((0, "baja tensión"), (2, "CPU frenada"), (3, "límite de temperatura")) if flags & (1 << bit)]
    past = [txt for bit, txt in ((16, "baja tensión"), (18, "CPU frenada"), (19, "límite de temperatura")) if flags & (1 << bit)]
    if now:
        _check(False, f"AHORA: {', '.join(now)}{clock}. Todo va a andar lento: usá la fuente oficial "
                      "(5,1 V / 3 A USB-C) y un cable corto y grueso")
    elif past:
        _check(None, f"desde que arrancó hubo: {', '.join(past)} (ahora no){clock}")
    else:
        _check(True, f"sin baja tensión ni CPU frenada{clock}")


def _out(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=graphical_env())  # noqa: S603
        return p.returncode, p.stdout + p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


async def _check_tidal(cfg: config_mod.Config) -> None:
    """Tidal con High Tide: lo que tiene que estar para abrirlo y manejarlo con el control."""
    music = HighTide(cfg.apps.get("tidal"))
    tools = music_tools()
    if not tools["flatpak"] and not music.installed():
        return  # sin Flatpak no hay Tidal: no lo menciono
    print("\nTidal (High Tide)")
    if not music.installed():
        _check(None, f"High Tide no está instalado ({music.app_id}): ./install.sh")
        return
    _check(True, f"High Tide instalado ({music.app_id})")
    missing = [n for n in ("playerctl", "wtype", "gdbus") if not tools[n]]
    if not tools["atspi"]:
        missing.append("gir1.2-atspi-2.0 (accesibilidad)")
    _check(not missing, "herramientas para manejarlo: " + ("OK" if not missing else "faltan " + ", ".join(missing)))
    rc, out = _out(["gdbus", "introspect", "--session", "--dest", "org.freedesktop.portal.Desktop",
                    "--object-path", "/org/freedesktop/portal/desktop"])
    secret = rc == 0 and "org.freedesktop.portal.Secret" in out
    _check(secret, "portal de secretos (guarda la sesión de Tidal)" + ("" if secret else
           ": no está. En labwc: ~/.config/xdg-desktop-portal/labwc-portals.conf con "
           "org.freedesktop.impl.portal.Secret=gnome-keyring (lo crea ./install.sh)"))
    rc, out = _out(["busctl", "--user", "call", "org.freedesktop.secrets", "/org/freedesktop/secrets",
                    "org.freedesktop.Secret.Service", "ReadAlias", "s", "default"])
    path = out.split('"')[1] if rc == 0 and '"' in out else ""
    if path and path != "/":
        rc, out = _out(["busctl", "--user", "get-property", "org.freedesktop.secrets", path,
                        "org.freedesktop.Secret.Collection", "Locked"])
        unlocked = rc == 0 and out.strip().endswith("false")
        _check(unlocked, "llavero predeterminado abierto" + ("" if unlocked else
               ": está bloqueado y High Tide se va a quedar esperando la contraseña. Creá uno sin "
               "contraseña en «Contraseñas y claves» y ponelo como predeterminado"))
    else:
        _check(None, "no hay llavero predeterminado (se crea al iniciar sesión en High Tide)")
    if music.running():
        st = await music.status()
        _check(bool(st), f"abierto: {st.get('status', '?')}" + (f" · {st['title']} - {st.get('artist', '')}"
                                                                 if st.get("title") else ""))
    else:
        _check(True, "cerrado (se abre desde el menú)")


def _check_sound(cfg: config_mod.Config) -> None:
    """Salida de sonido (opción «Sonido» del menú): qué salidas hay y cuál se usa."""
    from ..system import audio  # noqa: PLC0415
    print("\nSonido")
    if not audio.available():
        _check(None, "sin PipeWire (pw-dump / wpctl): el menú no muestra la opción «Sonido»")
        return
    outs = audio.list_outputs()
    if not outs:
        _check(False, "PipeWire no tiene ninguna salida de sonido")
    for o in outs:
        _check(True, f"{'▶ ' if o.default else ''}{o.name} — {o.detail or o.kind}{' (la que se usa)' if o.default else ''}")
    if not shutil.which("bluetoothctl"):
        _check(None, "sin bluetoothctl: no se listan los Bluetooth emparejados y desconectados")


def _check_gamepads(cfg: config_mod.Config) -> None:
    """Joysticks conectados y si el servicio los puede leer."""
    from ..system import gamepad  # noqa: PLC0415
    print("\nJoystick")
    if not (cfg.raw.get("gamepad") or {}).get("enabled", True):
        _check(None, "desactivado ([gamepad] enabled = false)")
        return
    found = gamepad.find_joysticks()
    if not found:
        _check(None, "ninguno conectado (se detectan solos al conectarlos)")
    for path, name in found.items():
        ok = os.access(path, os.R_OK)
        _check(ok, f"{name} ({path})" + ("" if ok else ": sin permiso para leerlo (sudo adduser $USER input)"))


def _check_power_off(cfg: config_mod.Config) -> None:
    """Apagado automático ([power]): si está activo, si va a poder apagar y si ahora sale sonido."""
    from ..system.power import audio_playing  # noqa: PLC0415
    pw = cfg.raw.get("power") or {}
    print("\nReposo / apagado automático")
    if not pw.get("enabled"):
        _check(None, "desactivado ([power] enabled = false; ./install.sh --apagado-automatico lo activa)")
        return
    mode = "apagar" if str(pw.get("mode", "reposo")).lower() in ("apagar", "poweroff") else "reposo"
    _check(True, f"activo ({mode}): a las {float(pw.get('idle_minutes', 180)) / 60:g} h sin sonido ni teclas, "
                 f"{float(pw.get('tv_off_minutes', 15)):g} min con la TV apagada"
                 + (f", {float(menu):g} min con solo el menú" if (menu := pw.get('menu_idle_minutes', 10)) else ""))
    if mode == "apagar":
        rule = Path("/etc/polkit-1/rules.d/50-tele-cec-apagado.rules")
        try:
            os.stat(rule)
            present: bool | None = True
        except PermissionError:
            present = None  # la carpeta de reglas solo la lee root: no se puede saber desde acá
        except OSError:
            present = False
        _check(present, "permiso para apagar sin contraseña (regla de polkit)" + {
            True: "", None: ": no puedo verificarlo (la carpeta solo la lee root)",
            False: ": falta (./install.sh --apagado-automatico); sin eso el apagado va a fallar"}[present])
    else:
        _check(True, "vuelve con una tecla del control" + (" o al prenderse la TV" if pw.get("wake_on_tv", True) else ""))
        off = float(pw.get("off_after_hours", 72) or 0)
        _check(True, f"tras {off:g} h seguidas en reposo se apaga de verdad" if off else "en reposo no se apaga nunca")
    _check(True, f"ahora: {'sale sonido' if audio_playing() else 'no sale sonido'}")


def _check_activities(cfg: config_mod.Config) -> None:
    """Menú de actividades: el panel en pantalla, mpv, feh y de dónde saca cada una el contenido."""
    print("\nMenú de actividades")
    gtk, layer, detail = overlay_mod.available()
    env = graphical_env()
    wayland = bool(env.get("WAYLAND_DISPLAY"))
    if not gtk:
        _check(False, f"panel en pantalla: {detail}")
    elif layer or not wayland:
        _check(True, f"panel en pantalla: {detail}")
    else:
        _check(None, f"panel en pantalla: {detail} (sin eso puede quedar detrás de Netflix en pantalla completa)")
    keys = [k for k in ("ROOT_MENU", "CONTENTS_MENU", "SETUP_MENU")
            if (cfg.action_for("global", config_mod.parse_key(k)) or ("",))[0] == "menu"]
    _check(bool(keys), "tecla Menú: " + (" / ".join(keys) if keys else "ninguna en [keys.global] (poné ROOT_MENU = \"menu\")")
           + " — confirmá con «tele-cec aprender» qué manda tu control")
    acts = activities_mod.build(_NoCtl(cfg), cfg)  # type: ignore[arg-type]
    groups = cfg.menu.get("groups") or {}
    order = []
    for i in (str(i) for i in (cfg.menu.get("items") or [])):  # un grupo («Videos y música») trae las suyas
        order += [str(x) for x in (groups[i].get("items") or [])] if isinstance(groups.get(i), dict) else [i]
    order += [k for k in acts if k not in order and k not in ("netflix", "tidal", "videos", "musica", "fotos")]
    shown = []
    for ident in order:
        act = acts.get(ident)
        if act is None:
            _check(False, f"menu.items: no hay ninguna actividad «{ident}»")
            continue
        if ident in ("netflix", "tidal"):  # tienen su propia sección
            if act.installed():
                shown.append(act.name)
            continue
        if not act.installed():
            hint = {"videos": " (./install.sh o sudo apt install mpv mpv-mpris)",
                    "musica": " (./install.sh o sudo apt install mpv mpv-mpris)",
                    "fotos": " (./install.sh o sudo apt install feh)",
                    "android": " (./install.sh: instala adb y compila scrcpy)"}.get(ident, "")
            _check(None, f"{act.name}: no está instalado{hint}: no aparece en el menú")
            continue
        shown.append(act.name)
        if isinstance(act, activities_mod.MediaActivity):
            folders = activities_mod.expand_folders(act.folders)
            folder = act.folder()
            if folder:
                n = activities_mod.count_media(folder, act.exts, stop_at=10000)
                _check(True, f"{act.name}: {activities_mod.describe_folder(folder)} ({n}{'+' if n >= 10000 else ''} archivos)")
            else:
                where = ", ".join(map(str, folders)) or "ninguna carpeta existe"
                _check(None, f"{act.name}: sin contenido en {where} (aparece en gris)")
            if isinstance(act, activities_mod.MpvMediaActivity) and not act.command:
                from ..apps.mpv import mpris_plugin  # noqa: PLC0415
                _check(bool(mpris_plugin()) or None, f"{act.name}: lo que suena en la página del celular (mpv-mpris)" +
                       ("" if mpris_plugin() else ": falta (sudo apt install mpv-mpris)"))
        elif isinstance(act, activities_mod.AndroidActivity):
            devs = act.devices()
            dev = act._pick(devs)
            if dev and dev.ready:
                _check(True, f"{act.name}: {dev.label()} ({act.binary()})")
            elif dev:
                _check(None, f"{act.name}: {dev.serial}: {activities_mod.android.STATE_TEXT.get(dev.state, dev.state)}")
            else:
                _check(None, f"{act.name}: ningún celular conectado por adb"
                             + (f" (Wi‑Fi {act._target()}: se conecta al abrirlo)" if act._target() else ""))
        elif isinstance(act, activities_mod.YouTubeActivity):
            _check(True, f"{act.name}: en {act.binary()} (perfil {act.profile}"
                         f"{', H.264' if act.cfg.get('force_h264', True) else ''})")
        else:
            cmd = getattr(act, "command", [])
            _check(True, f"{act.name}: {cmd if isinstance(cmd, str) else ' '.join(map(str, cmd))}")
    _check(bool(shutil.which("wtype")) or not wayland, "teclas a las apps Wayland, como Tidal (wtype)" +
           ("" if shutil.which("wtype") else ": falta (sudo apt install wtype)"))
    if shutil.which("feh"):
        _check(bool(shutil.which("xdotool")), "teclas a feh (xdotool)" + ("" if shutil.which("xdotool") else
               ": falta (sudo apt install xdotool); sin eso no andan pausa, zoom ni girar en Fotos"))
    print(f"         en el menú: {' · '.join(shown) or '(nada)'}")


class _NoCtl:
    """Para armar las actividades en el diagnóstico sin un controller."""

    def __init__(self, cfg: config_mod.Config):
        self.browser = Browser(cfg.browser)
        self.music = HighTide(cfg.apps.get("tidal"))
        self.page = None


async def cmd_doctor(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    print(f"tele-cec {__version__}\nConfiguración: {cfg.path or 'valores por defecto (no hay config.toml)'}\n")
    print("CEC")
    cec_bin = cfg.cec.get("cec_client") or "cec-client"
    _check(bool(shutil.which(cec_bin) or os.path.isfile(cec_bin)), f"cec-client: {shutil.which(cec_bin) or 'no encontrado (sudo apt install cec-utils)'}")
    devs = sorted(str(p) for p in Path("/dev").glob("cec*"))
    _check(bool(devs), f"dispositivos CEC: {', '.join(devs) or 'ninguno (¿dtoverlay vc4-kms-v3d?)'}")
    for d in devs:
        _check(os.access(d, os.R_OK | os.W_OK), f"permiso de lectura/escritura en {d}" + ("" if os.access(d, os.R_OK | os.W_OK) else " (agregá tu usuario al grupo video)"))
    _check(_service_active(), "servicio tele-cec activo" if _service_active() else "servicio tele-cec no está activo")
    _check_power()
    await _check_tidal(cfg)
    _check_activities(cfg)
    _check_sound(cfg)
    _check_gamepads(cfg)
    _check_power_off(cfg)

    browser = Browser(cfg.browser)
    print(f"\nNavegador: {browser.name}")
    binary = browser.binary()
    _check(bool(binary), f"binario: {binary or 'no encontrado'}")
    env = graphical_env()
    _check(has_display(env), f"sesión gráfica: WAYLAND_DISPLAY={env.get('WAYLAND_DISPLAY', '-')} DISPLAY={env.get('DISPLAY', '-')}")
    _check(True, f"perfil: {browser.user_data_dir} ({'existe' if browser.user_data_dir.exists() else 'se crea al abrir'})")
    if browser.firefox:
        exts = profile_extensions(browser.user_data_dir)
        if exts:
            desc = ", ".join(e["name"] + ("" if e["active"] else " (apagada por tele-cec)" if e["ours"] else " (apagada)")
                             for e in exts)
            _check(True, f"extensiones del perfil: {desc}")
            wanted = [str(x).strip().lower() for x in (browser.fx.get("disable_extensions") or [])]
            pending = [e["name"] for e in exts if e["active"] and {e["id"].lower(), e["name"].lower()} & set(wanted)]
            if pending:
                _check(None, f"{', '.join(pending)}: se apaga{'n' if len(pending) > 1 else ''} la próxima vez que se abra Firefox")
    alive = await browser.alive()
    proto = "WebDriver BiDi" if browser.firefox else "DevTools"
    _check(alive, f"{proto} en 127.0.0.1:{browser.port}" + ("" if alive else " — abrilo con: tele-cec navegador"))
    if not alive:
        return 1

    # Con el servicio corriendo, se le pregunta a él (Firefox admite una sola sesión de control).
    info = await ipc.request({"op": "info"})
    ctl: Controller | None = None
    if info is not None:
        _check(info.get("connected"), "el servicio está conectado al navegador" if info.get("connected")
               else "el servicio todavía no se conectó al navegador")
        if not info.get("connected"):
            return 1
        _check(True, f"versión: {info.get('version')}")

        async def js(name: str, *a: object) -> dict:
            r = await ipc.request({"op": "js", "name": name, "args": list(a)})
            return (r or {}).get("result") or {}
    else:
        ctl = Controller(cfg, browser)
        connected = await _connect_direct(ctl)
        if not connected:
            if connected is False:
                _check(False, "no pude conectarme a la pestaña")
            return 1
        _check(True, f"versión: {ctl.page.version}")
        js = ctl.js
    try:
        dump = await js("dump")
        print("\nNetflix (página actual)")
        _check(True, f"URL: {dump.get('url')}")
        _check(True, f"contexto: {dump.get('ctx')}   modal abierto: {dump.get('modal')}")
        _check(dump.get("candidates", 0) > 0, f"elementos navegables: {dump.get('candidates')} (en pantalla: {dump.get('inView')})")
        _check(True, f"filas: {dump.get('rows')}   fila Seguir viendo: {'sí' if dump.get('cwRow') else 'no'}")
        if dump.get("profiles"):
            _check(True, f"perfiles: {', '.join(dump['profiles'])}")
        if dump.get("video"):
            _check(dump.get("playerApi"), "API interna del reproductor de Netflix" + ("" if dump.get("playerApi") else " no disponible (se usan atajos de teclado)"))
        pl = dump.get("player")
        if pl:
            _check(True, f"reproductor: {pl.get('time')} / {pl.get('duration')}   "
                         f"{'en pausa' if pl.get('paused') else 'reproduciendo'}   volumen {round((pl.get('volume') or 0) * 100)} %"
                         f"{' (silencio)' if pl.get('muted') else ''}   audio: {pl.get('audio')}   subtítulos: {pl.get('subtitles')}")
        # Ida y vuelta con la página (cada tecla hace al menos dos: contexto + acción). A través del
        # servicio se le descuenta lo que cuesta el socket solo (una consulta que no toca la página),
        # porque las teclas no pasan por ahí.
        async def median_ms(fn) -> float:
            out = []
            for _ in range(9):
                t0 = time.perf_counter()
                await fn()
                out.append((time.perf_counter() - t0) * 1000)
            return sorted(out)[len(out) // 2]
        page_ms = await median_ms(lambda: js("viewport"))
        if info is not None:
            sock_ms = await median_ms(lambda: ipc.request({"op": "ping"}))
            page_ms = max(0.0, page_ms - sock_ms)
            extra = f" (sin contar {sock_ms:.0f} ms del socket del servicio)"
        else:
            extra = ""
        _check(page_ms < 30, f"ida y vuelta con la página: {page_ms:.0f} ms{extra}")
        pf = dump.get("perf")
        if pf and pf.get("reduceEffects"):
            _check(pf.get("css") and pf.get("animatePatched"),
                   f"reduce_effects: CSS {'sí' if pf.get('css') else 'NO'} · animate() interceptado "
                   f"{'sí' if pf.get('animatePatched') else 'NO'} · prefers-reduced-motion "
                   f"{'sí' if pf.get('reducedMotion') else 'no'} · animaciones corriendo ahora: {pf.get('running')}")
            # Animaciones por JavaScript (cuadro a cuadro) no aparecen arriba: se ven en los cuadros que pide.
            p1 = await js("perf")
            await asyncio.sleep(3)
            p2 = await js("perf")
            fps = (p2.get("pageFrames", 0) - p1.get("pageFrames", 0)) / 3
            _check(fps < 5, f"cuadros pedidos por la página (quieta): {fps:.0f}/s"
                            + ("" if fps < 5 else " — algo se anima por JavaScript; ver selectors.no_motion"))
            for a in (pf.get("sample") or [])[:8]:
                print(f"         {a.get('kind')} {a.get('name') or ''} en {a.get('target') or '?'}: "
                      f"{a.get('ms')} ms × {a.get('iterations')}")
        if args.detalle:
            print("\nDetalle (JSON):")
            print(json.dumps(dump, indent=2, ensure_ascii=False))
        else:
            print("\n  (usá --detalle para ver los data-uia visibles y los métodos del reproductor)")
        await js("toast", "tele-cec: diagnóstico OK", 2500)
    finally:
        if ctl:
            await ctl.disconnect()
    return 0
