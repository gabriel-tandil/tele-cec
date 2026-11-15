#!/usr/bin/env bash
# Instalador de tele-cec. Ejecutalo como tu usuario (sin sudo):
#     ./install.sh [--chromium] [--sin-tidal] [--sin-android] [--apagado-automatico] [--copiar-perfil] [--sin-apt] …
# Instala lo que falte: los paquetes, las apps del menú (navegador, mpv, feh, High Tide, scrcpy) y el servicio.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/tele-cec"
BIN="$HOME/.local/bin"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/tele-cec"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
PROFILE_SRC="$HOME/.config/chromium"
PROFILE_DST="$HOME/.config/chromium-netflix"
FX_PROFILES="$HOME/.mozilla/firefox"
FX_PROFILE_DST="$HOME/.config/firefox-netflix"

ENGINE=firefox      # Firefox por defecto; --chromium para usar Chromium
ENGINE_SET=0
COPY_PROFILE=0
NO_APT=0
NO_SERVICE=0
NO_ANNOUNCE=0
# Apps del menú: se instalan las que falten (--sin-<app> para saltear una).
VIDEOS=1
FOTOS=1
TIDAL=1
ANDROID=1
AUTO_OFF=0
HIGHTIDE=io.github.nokse22.high-tide
RELOGIN=0
# scrcpy (Android): no está en los repositorios de Debian trixie; se compila esta versión. El servidor (lo que
# corre en el celular) es el que publica scrcpy, verificado con su SHA-256 (el mismo que usa su install_release.sh).
SCRCPY_VERSION=5.0.1
SCRCPY_SERVER_SHA256=764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536

usage() {
  cat <<EOF
Uso: ./install.sh [opciones]
  --chromium        usar Chromium en vez de Firefox (deja browser.engine = "chromium" en la configuración)
  --firefox         usar Firefox (es el que se usa por defecto; lo deja escrito en la configuración)

  Instala lo que falte de cada app del menú. Para no instalar alguna:
  --sin-videos      sin mpv (Videos y Música; Fotos sigue con feh)
  --sin-fotos       sin feh (Fotos)
  --sin-tidal       sin High Tide (Tidal; Flatpak para tu usuario, con playerctl, wtype y accesibilidad)
  --sin-android     sin scrcpy ni adb (Android: la pantalla del celular). scrcpy no está en los repositorios:
                    se compila la versión $SCRCPY_VERSION (unos minutos en una Pi)
  --apagado-automatico
                    la Pi se apaga sola tras un rato sin sonido ni teclas del control ([power] en la
                    configuración): instala una regla de polkit para que el servicio pueda apagarla
  --copiar-perfil   copia tu perfil del navegador al perfil propio de tele-cec para no tener
                    que volver a iniciar sesión en Netflix. Cerrá el navegador antes.
                      Chromium: ~/.config/chromium -> ~/.config/chromium-netflix
                      Firefox:  tu perfil de ~/.mozilla/firefox -> ~/.config/firefox-netflix
  --sin-apt         no instalar paquetes (solo avisar si falta algo; las apps que falten no aparecen en el menú)
  --sin-servicio    no habilitar ni iniciar el servicio de systemd
  --sin-anunciar    al reiniciar el servicio no anunciarse a la TV (no la prende ni le cambia la entrada); solo
                    ese arranque
EOF
}

for arg in "$@"; do
  case "$arg" in
    --firefox) ENGINE=firefox; ENGINE_SET=1 ;;
    --chromium) ENGINE=chromium; ENGINE_SET=1 ;;
    --tidal) TIDAL=1 ;;          # se aceptan por compatibilidad: ahora se instalan siempre
    --android) ANDROID=1 ;;
    --sin-videos) VIDEOS=0 ;;
    --sin-fotos) FOTOS=0 ;;
    --sin-tidal) TIDAL=0 ;;
    --sin-android) ANDROID=0 ;;
    --apagado-automatico) AUTO_OFF=1 ;;
    --copiar-perfil) COPY_PROFILE=1 ;;
    --sin-apt) NO_APT=1 ;;
    --sin-servicio) NO_SERVICE=1 ;;
    --sin-anunciar) NO_ANNOUNCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Opción desconocida: $arg"; usage; exit 1 ;;
  esac
done

if [ "$(id -u)" -eq 0 ]; then
  echo "Ejecutalo como tu usuario, sin sudo (te va a pedir la clave cuando haga falta)."
  exit 1
fi

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mAVISO:\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- dependencias
step "Dependencias"
# ¿Hay un paquete con ese nombre en los repositorios? (los nombres cambian entre Raspberry Pi OS y Debian)
apt_has() { LC_ALL=C apt-cache policy "$1" 2>/dev/null | grep -q 'Candidate: [^(]'; }
# El primero que exista de una lista de nombres de paquete.
first_pkg() { for p in "$@"; do if apt_has "$p"; then echo "$p"; return 0; fi; done; return 1; }
scrcpy_ok() { command -v scrcpy >/dev/null 2>&1 && scrcpy --version 2>/dev/null | head -1 | grep -q "scrcpy $SCRCPY_VERSION"; }

need=()        # imprescindibles para el servicio
apps=()        # para las apps del menú (sin ellos, esa app no aparece)
add_app() { for p in "$@"; do [[ " ${apps[*]} " == *" $p "* ]] || apps+=("$p"); done; }

command -v cec-client >/dev/null 2>&1 || need+=(cec-utils)
python3 -c 'import websockets' 2>/dev/null || need+=(python3-websockets)
python3 -c 'import tomllib' 2>/dev/null || python3 -c 'import tomli' 2>/dev/null || need+=(python3-tomli)
# Menú de actividades: panel en pantalla con GTK 3 y gtk-layer-shell (encima de las apps en pantalla completa).
python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null || need+=(python3-gi gir1.2-gtk-3.0)
python3 -c 'import gi; gi.require_version("GtkLayerShell", "0.1")' 2>/dev/null || need+=(gir1.2-gtklayershell-0.1)
# Controles por MPRIS (Videos, YouTube, Tidal) y teclas a las ventanas: wtype (Wayland) y xdotool (feh, por Xwayland).
command -v playerctl >/dev/null 2>&1 || need+=(playerctl)
command -v wtype >/dev/null 2>&1 || need+=(wtype)
command -v xdotool >/dev/null 2>&1 || need+=(xdotool)

# Navegador (Netflix y YouTube): Firefox o Chromium, con el nombre de paquete que tenga esta distribución.
if [ "$ENGINE" = firefox ]; then
  if ! command -v firefox >/dev/null 2>&1 && ! command -v firefox-esr >/dev/null 2>&1; then
    if pkg="$(first_pkg firefox firefox-esr)"; then add_app "$pkg"; else warn "no encontré un paquete de Firefox: instalalo a mano."; fi
  fi
elif ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  if pkg="$(first_pkg chromium chromium-browser)"; then add_app "$pkg"; else warn "no encontré un paquete de Chromium: instalalo a mano."; fi
fi
# Videos, Música y «fotos y videos» con mpv; mpv-mpris: lo que suena se ve en la página del celular;
# python3-mutagen: las etiquetas de la música (artista, disco…) para elegirla desde el celular y el menú;
# ffprobe (ffmpeg): duración, calidad y título de cada video en la lista del menú.
((VIDEOS)) && { command -v mpv >/dev/null 2>&1 || add_app mpv; apt_has mpv-mpris && ! dpkg -s mpv-mpris >/dev/null 2>&1 && add_app mpv-mpris
               python3 -c 'import mutagen' 2>/dev/null || add_app python3-mutagen
               command -v ffprobe >/dev/null 2>&1 || add_app ffmpeg; }
# python3-pil (Pillow): el mapa de dónde se sacó cada foto (verde en Fotos).
((FOTOS)) && { command -v feh >/dev/null 2>&1 || add_app feh
               python3 -c 'import PIL' 2>/dev/null || add_app python3-pil; }
if ((TIDAL)); then
  command -v flatpak >/dev/null 2>&1 || add_app flatpak
  command -v gdbus >/dev/null 2>&1 || add_app libglib2.0-bin
  python3 -c 'import gi; gi.require_version("Atspi", "2.0")' 2>/dev/null || add_app gir1.2-atspi-2.0 at-spi2-core
fi
if ((ANDROID)); then
  command -v adb >/dev/null 2>&1 || add_app adb
  # Vincular por Wi‑Fi desde el menú: el adb de Debian no busca celulares en la red (mDNS) y el QR hay que dibujarlo.
  command -v avahi-browse >/dev/null 2>&1 || add_app avahi-utils
  command -v qrencode >/dev/null 2>&1 || add_app qrencode
  if ! scrcpy_ok; then  # para compilar scrcpy y para usarlo (SDL3, FFmpeg, libusb)
    for pkg in wget ca-certificates gcc pkg-config meson ninja-build libsdl3-0 libsdl3-dev libavcodec-dev \
               libavdevice-dev libavformat-dev libavutil-dev libswresample-dev libusb-1.0-0 libusb-1.0-0-dev \
               libv4l-dev libdrm-dev; do
      dpkg -s "$pkg" >/dev/null 2>&1 || add_app "$pkg"
    done
  fi
fi

APT_OK=1   # ¿están los paquetes de las apps? (con --sin-apt puede que no)
if ((${#need[@]} + ${#apps[@]})); then
  if ((NO_APT)); then
    if ((${#need[@]})); then
      echo "Faltan paquetes: ${need[*]}  ->  sudo apt install ${need[*]}"
      exit 1
    fi
    warn "faltan paquetes para algunas apps del menú (no van a aparecer): sudo apt install ${apps[*]}"
    APT_OK=0
  else
    echo "Instalando: ${need[*]} ${apps[*]}"
    sudo apt-get update
    sudo apt-get install -y "${need[@]}" "${apps[@]}"
  fi
else
  echo "Paquetes: OK (CEC, GTK y gtk-layer-shell, playerctl, wtype, xdotool y las apps del menú)"
fi

if ((ANDROID)); then
  step "scrcpy (Android)"
  if scrcpy_ok; then
    echo "scrcpy $SCRCPY_VERSION: OK"
  elif ! ((APT_OK)); then
    warn "no compilo scrcpy (--sin-apt y faltan paquetes): «Android» no va a aparecer en el menú."
  else
    build="${XDG_CACHE_HOME:-$HOME/.cache}/tele-cec/scrcpy-$SCRCPY_VERSION"
    rm -rf "$build" && mkdir -p "$build"
    echo "Bajando scrcpy $SCRCPY_VERSION…"
    wget -q "https://github.com/Genymobile/scrcpy/archive/refs/tags/v$SCRCPY_VERSION.tar.gz" -O "$build/src.tar.gz"
    tar xzf "$build/src.tar.gz" -C "$build" --strip-components=1
    wget -q "https://github.com/Genymobile/scrcpy/releases/download/v$SCRCPY_VERSION/scrcpy-server-v$SCRCPY_VERSION" \
      -O "$build/scrcpy-server"
    echo "$SCRCPY_SERVER_SHA256  $build/scrcpy-server" | sha256sum --check --quiet
    echo "Compilando (unos minutos)…"
    (cd "$build" && meson setup build-auto --buildtype=release --strip -Db_lto=true \
       -Dprebuilt_server=scrcpy-server >/dev/null && ninja -C build-auto >/dev/null)
    sudo ninja -C "$build/build-auto" install >/dev/null
    scrcpy --version | head -1
    rm -rf "$build"
  fi
fi
((VIDEOS)) || echo "Videos: salteado (--sin-videos)"
((FOTOS)) || echo "Fotos: salteado (--sin-fotos)"
((TIDAL)) || echo "Tidal: salteado (--sin-tidal)"
((ANDROID)) || echo "Android: salteado (--sin-android)"

# ---------------------------------------------------------------- permisos CEC
step "Adaptador CEC"
found=0
for dev in /dev/cec*; do
  [ -e "$dev" ] || continue
  found=1
  if [ -r "$dev" ] && [ -w "$dev" ]; then
    echo "$dev: OK"
  else
    grp="$(stat -c %G "$dev")"
    echo "$dev pertenece al grupo '$grp': agrego a $USER a ese grupo"
    sudo usermod -aG "$grp" "$USER"
    RELOGIN=1
  fi
done
((found)) || warn "no hay /dev/cec* (en Raspberry Pi OS Bookworm lo crea el driver vc4-kms-v3d)."

# ---------------------------------------------------------------- migración desde netflix-cec
# El proyecto antes se llamaba netflix-cec: se pasa la configuración, la caché y la regla de apagado al
# nombre nuevo y se saca el programa viejo. Los perfiles del navegador (firefox-netflix, chromium-netflix)
# no cambian de nombre: ahí está tu sesión de Netflix.
OLD_NAME=netflix-cec
OLD_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/$OLD_NAME"
OLD_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/$OLD_NAME"
NEW_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/tele-cec"
OLD_DEST="$HOME/.local/share/$OLD_NAME"
OLD_UNIT="$UNIT_DIR/$OLD_NAME.service"
OLD_RULE=/etc/polkit-1/rules.d/50-$OLD_NAME-apagado.rules
if [ -e "$OLD_UNIT" ] || [ -d "$OLD_DEST" ] || [ -d "$OLD_CONF" ] || [ -e "$BIN/$OLD_NAME" ]; then
  step "Migración desde $OLD_NAME"
  systemctl --user disable --now "$OLD_NAME.service" 2>/dev/null && echo "Servicio $OLD_NAME detenido y deshabilitado" || true
  if [ -d "$OLD_CONF" ] && [ ! -e "$CONF_DIR" ]; then
    mv "$OLD_CONF" "$CONF_DIR" && echo "Configuración: $OLD_CONF -> $CONF_DIR"
  elif [ -d "$OLD_CONF" ]; then
    warn "existen $OLD_CONF y $CONF_DIR: uso la nueva y dejo la vieja sin tocar"
  fi
  if [ -d "$OLD_CACHE" ] && [ ! -e "$NEW_CACHE" ]; then
    mv "$OLD_CACHE" "$NEW_CACHE" && echo "Caché: $OLD_CACHE -> $NEW_CACHE"
  fi
  if sudo test -f "$OLD_RULE"; then  # /etc/polkit-1/rules.d no es legible por el usuario
    sudo sed -i "s/$OLD_NAME/tele-cec/g" "$OLD_RULE"
    sudo mv "$OLD_RULE" /etc/polkit-1/rules.d/50-tele-cec-apagado.rules && echo "Regla de polkit renombrada"
  fi
  rm -f "$OLD_UNIT" "$BIN/$OLD_NAME"
  if [ -d "$OLD_DEST" ] && [ -f "$OLD_DEST/netflix_cec/__main__.py" ]; then
    rm -r "$OLD_DEST" && echo "Programa viejo borrado: $OLD_DEST"
  fi
  systemctl --user daemon-reload
fi

# ---------------------------------------------------------------- archivos
step "Archivos"
mkdir -p "$DEST" "$BIN" "$CONF_DIR" "$UNIT_DIR" "$APPS"
rm -rf "$DEST/tele_cec" "$DEST/tests"
cp -r "$SRC/tele_cec" "$SRC/tests" "$DEST/"
cp "$SRC/README.md" "$SRC/config.example.toml" "$DEST/"
find "$DEST" -name __pycache__ -prune -exec rm -rf {} +
echo "Programa: $DEST"

cat > "$BIN/tele-cec" <<EOF
#!/bin/sh
PYTHONPATH="$DEST\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m tele_cec "\$@"
EOF
chmod +x "$BIN/tele-cec"
echo "Comando: $BIN/tele-cec"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "$BIN no está en tu PATH (se agrega solo al volver a iniciar sesión si la carpeta existe)." ;;
esac

if [ -f "$CONF_DIR/config.toml" ]; then
  echo "Configuración: $CONF_DIR/config.toml (se conserva la tuya)"
else
  cp "$SRC/config.example.toml" "$CONF_DIR/config.toml"
  echo "Configuración: $CONF_DIR/config.toml (nueva)"
fi
if ((ENGINE_SET)); then
  # Deja engine = "<motor>" en [browser] sin tocar el resto (y sin duplicar la tabla).
  python3 - "$CONF_DIR/config.toml" "$ENGINE" <<'PY'
import re, sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
path, want = sys.argv[1], sys.argv[2]
name = {"firefox": "Firefox", "chromium": "Chromium"}[want]
text = open(path, encoding="utf-8").read()
engine = tomllib.loads(text).get("browser", {}).get("engine")
if engine == want:
    print(f'Navegador: {name} (ya estaba en la configuración)')
elif engine:
    print(f'AVISO: tu configuración dice browser.engine = "{engine}": cambialo a "{want}" a mano.')
else:
    m = re.search(r"^\[browser\][ \t]*$", text, re.M)
    if m:
        text = text[:m.end()] + f'\nengine = "{want}"' + text[m.end():]
    else:
        text = text.rstrip("\n") + f'\n\n[browser]\nengine = "{want}"\n'
    open(path, "w", encoding="utf-8").write(text)
    print(f'Navegador: {name} (agregué engine = "{want}" en [browser])')
PY
else
  echo "Navegador: Firefox (por defecto; --chromium para usar Chromium)"
fi

cat > "$APPS/netflix-tv.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Netflix (TV)
Comment=Netflix en pantalla completa, manejable con el control remoto de la TV
Exec=$BIN/tele-cec navegador
Icon=video-display
Terminal=false
Categories=AudioVideo;Video;Player;
EOF
echo "Acceso en el menú: Netflix (TV)"

# ---------------------------------------------------------------- Tidal (High Tide)
if ((TIDAL)) && ! command -v flatpak >/dev/null 2>&1; then
  warn "no está flatpak (--sin-apt): no instalo High Tide; «Tidal» no va a aparecer en el menú."
  TIDAL=0
fi
if ((TIDAL)); then
  step "Tidal (High Tide)"
  if ! flatpak remotes --columns=name 2>/dev/null | grep -qx flathub; then
    flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
  fi
  if flatpak info "$HIGHTIDE" >/dev/null 2>&1; then
    echo "High Tide: ya instalado"
  else
    flatpak install --user -y --noninteractive flathub "$HIGHTIDE"
  fi
  # Las apps Flatpak guardan su sesión por el "portal de secretos". En labwc (Raspberry Pi OS) el
  # portal no lo asigna solo: sin esto High Tide se queda en "Cargando…" y no guarda la sesión.
  PORTAL_SYS=/usr/share/xdg-desktop-portal/labwc-portals.conf
  PORTAL_USR="${XDG_CONFIG_HOME:-$HOME/.config}/xdg-desktop-portal/labwc-portals.conf"
  if [ -f "$PORTAL_SYS" ] && ! grep -qs "impl.portal.Secret" "$PORTAL_SYS" "$PORTAL_USR"; then
    mkdir -p "$(dirname "$PORTAL_USR")"
    if [ -f "$PORTAL_USR" ]; then cp "$PORTAL_USR" "$PORTAL_USR.tmp"; else cp "$PORTAL_SYS" "$PORTAL_USR.tmp"; fi
    printf '%s\n' '# Secretos (sesiones guardadas de apps Flatpak como High Tide) en gnome-keyring' \
      'org.freedesktop.impl.portal.Secret=gnome-keyring' >> "$PORTAL_USR.tmp"
    mv "$PORTAL_USR.tmp" "$PORTAL_USR"
    systemctl --user try-restart xdg-desktop-portal.service 2>/dev/null || true
    echo "Portal de secretos: $PORTAL_USR"
  fi
  # labwc: que High Tide se abra maximizado (regla de ventana en tu rc.xml; labwc -m la combina con la del sistema).
  if command -v labwc >/dev/null 2>&1; then
    LABWC_RC="${XDG_CONFIG_HOME:-$HOME/.config}/labwc/rc.xml"
    mkdir -p "$(dirname "$LABWC_RC")"
    [ -f "$LABWC_RC" ] || printf '<?xml version="1.0"?>\n<openbox_config xmlns="http://openbox.org/3.4/rc"/>\n' > "$LABWC_RC"
    python3 - "$LABWC_RC" "$HIGHTIDE" <<'PY'
import sys, xml.etree.ElementTree as ET
path, app = sys.argv[1], sys.argv[2]
NS = "http://openbox.org/3.4/rc"
ET.register_namespace("", NS)
q = lambda t: f"{{{NS}}}{t}"
try:
    tree = ET.parse(path)
except ET.ParseError as exc:
    print(f"AVISO: no pude leer {path} ({exc}): agregá a mano una windowRule que maximice {app}")
    sys.exit()
root = tree.getroot()
rules = root.find(q("windowRules"))
if rules is None:
    rules = ET.SubElement(root, q("windowRules"))
if any(r.get("identifier") == app for r in rules.findall(q("windowRule"))):
    print("labwc: High Tide ya se abre maximizado")
else:
    ET.SubElement(ET.SubElement(rules, q("windowRule"), identifier=app), q("action"), name="Maximize")
    ET.indent(tree, "  ")
    tree.write(path, xml_declaration=True, encoding="utf-8")
    print(f"labwc: High Tide se abre maximizado ({path})")
PY
    pkill -HUP -x labwc 2>/dev/null || true
  fi
  echo "Para que High Tide no pida una contraseña en cada arranque (con el control no se puede contestar):"
  echo "  abrí «Contraseñas y claves» (sudo apt install seahorse), creá un llavero de contraseñas SIN"
  echo "  contraseña y ponelo como predeterminado. Después iniciá sesión una vez en High Tide."
  echo "  Revisalo con: tele-cec diagnostico   (sección Tidal)"
fi

# ---------------------------------------------------------------- perfil del navegador
if ((COPY_PROFILE)) && [ "$ENGINE" = firefox ]; then
  step "Perfil de Firefox"
  # El perfil en uso es el Default= de la sección [Install…] de profiles.ini (o el marcado Default=1).
  FX_SRC="$(python3 - "$FX_PROFILES" <<'PY'
import configparser, os, sys
base = sys.argv[1]
ini = configparser.ConfigParser()
if not ini.read(os.path.join(base, "profiles.ini")):
    sys.exit()
path = next((ini[s]["Default"] for s in ini.sections() if s.startswith("Install") and "Default" in ini[s]), None)
if path is None:
    prof = [ini[s] for s in ini.sections() if s.startswith("Profile")]
    chosen = next((p for p in prof if p.get("Default") == "1"), prof[0] if prof else None)
    if chosen is not None:
        path = chosen["Path"]  # relativo a base o absoluto (os.path.join respeta los absolutos)
if path:
    print(path if os.path.isabs(path) else os.path.join(base, path))
PY
)"
  if [ -e "$FX_PROFILE_DST/prefs.js" ]; then
    echo "Ya existe $FX_PROFILE_DST: no lo toco."
  elif pgrep -u "$USER" -x 'firefox|firefox-esr|firefox-bin' >/dev/null 2>&1; then
    warn "Firefox está abierto. Cerralo y corré de nuevo: ./install.sh --copiar-perfil"
  elif [ -n "$FX_SRC" ] && [ -d "$FX_SRC" ]; then
    mkdir -p "$FX_PROFILE_DST"
    cp -a "$FX_SRC/." "$FX_PROFILE_DST/"
    rm -f "$FX_PROFILE_DST/lock" "$FX_PROFILE_DST/.parentlock" "$FX_PROFILE_DST/user.js"
    rm -rf "$FX_PROFILE_DST/cache2" "$FX_PROFILE_DST/startupCache"
    echo "Copiado $FX_SRC -> $FX_PROFILE_DST (con tu sesión de Netflix)."
  else
    warn "no encontré tu perfil de Firefox en $FX_PROFILES; vas a iniciar sesión en Netflix la primera vez."
  fi
elif ((COPY_PROFILE)); then
  step "Perfil de Chromium"
  if [ -e "$PROFILE_DST" ]; then
    echo "Ya existe $PROFILE_DST: no lo toco."
  elif pgrep -u "$USER" '^chromium' >/dev/null 2>&1; then
    warn "Chromium está abierto. Cerralo y corré de nuevo: ./install.sh --copiar-perfil"
  elif [ -d "$PROFILE_SRC" ]; then
    cp -a "$PROFILE_SRC" "$PROFILE_DST"
    rm -f "$PROFILE_DST"/Singleton*
    rm -rf "$PROFILE_DST/Default/Cache" "$PROFILE_DST/Default/Code Cache" "$PROFILE_DST/Default/GPUCache"
    echo "Copiado $PROFILE_SRC -> $PROFILE_DST (con tu sesión de Netflix)."
  else
    warn "no existe $PROFILE_SRC; vas a iniciar sesión en Netflix la primera vez."
  fi
fi

# ---------------------------------------------------------------- apagado automático
if ((AUTO_OFF)); then
  step "Apagado automático"
  # El servicio de usuario no tiene una sesión activa propia: sin esta regla, apagar pide contraseña.
  RULE=/etc/polkit-1/rules.d/50-tele-cec-apagado.rules
  sudo tee "$RULE" >/dev/null <<EOF
// tele-cec: el apagado automático (servicio de usuario de $USER) puede apagar la Raspberry Pi sin contraseña.
polkit.addRule(function(action, subject) {
    if ((action.id == "org.freedesktop.login1.power-off" ||
         action.id == "org.freedesktop.login1.power-off-multiple-sessions") &&
        subject.user == "$USER") {
        return polkit.Result.YES;
    }
});
EOF
  echo "Regla de polkit: $RULE"
  python3 - "$CONF_DIR/config.toml" <<'PY2'
import re, sys, tomllib
from pathlib import Path
p = Path(sys.argv[1])
text = p.read_text(encoding="utf-8") if p.exists() else ""
power = tomllib.loads(text).get("power", {})
if power.get("enabled") is True:
    print("Apagado automático: ya estaba activado en la configuración")
elif "enabled" in power or re.search(r"(?m)^\[power\]", text):
    print("AVISO: tu configuración ya tiene [power]: poné enabled = true a mano")
else:
    text = text.rstrip("\n") + "\n\n[power]                       # apagado automático (ver defaults.toml)\nenabled = true\n"
    p.write_text(text, encoding="utf-8")
    print("Apagado automático: activado ([power] enabled = true)")
PY2
  echo "  Ojo: apagada, el control no la prende. En una Pi 400: Fn+F10; si no, desenchufar y enchufar."
fi

# ---------------------------------------------------------------- systemd
step "Servicio de usuario (systemd)"
cp "$SRC/systemd/tele-cec.service" "$UNIT_DIR/tele-cec.service"
systemctl --user daemon-reload
if ((NO_SERVICE)); then
  echo "Instalado pero no habilitado (--sin-servicio). Para habilitarlo:"
  echo "  systemctl --user enable --now tele-cec"
else
  systemctl --user enable tele-cec.service
  if ((NO_ANNOUNCE)); then
    mkdir -p "${XDG_CACHE_HOME:-$HOME/.cache}/tele-cec"
    touch "${XDG_CACHE_HOME:-$HOME/.cache}/tele-cec/no-anunciar-al-arrancar"
  fi
  systemctl --user restart tele-cec.service
  sleep 2
  systemctl --user --no-pager --lines=0 status tele-cec.service || true
fi

# ---------------------------------------------------------------- listo
step "Listo"
if ((RELOGIN)); then
  warn "te agregué a un grupo nuevo: REINICIÁ la Pi (o cerrá y abrí sesión) para que el servicio pueda usar el adaptador CEC."
fi
cat <<EOF
Próximos pasos:
  1. Probá el control:          tele-cec aprender      (Ctrl+C para salir)
  2. Elegí una actividad:       apretá Menú en el control (o OK con todo cerrado) y elegí Netflix,
                                YouTube, Tidal, Videos, Fotos o Android. También: "Netflix (TV)" en el
                                menú del escritorio.
     La primera vez iniciá sesión en Netflix (salvo que hayas usado --copiar-perfil).
  3. Revisá que todo esté bien: tele-cec diagnostico
  Logs en vivo:                 journalctl --user -u tele-cec -f
  Configuración:                $CONF_DIR/config.toml
EOF
