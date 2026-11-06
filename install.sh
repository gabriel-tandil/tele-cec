#!/usr/bin/env bash
# Instalador de netflix-cec. Ejecutalo como tu usuario (sin sudo):
#     ./install.sh [--firefox] [--copiar-perfil] [--sin-apt] [--sin-servicio]
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/netflix-cec"
BIN="$HOME/.local/bin"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/netflix-cec"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
PROFILE_SRC="$HOME/.config/chromium"
PROFILE_DST="$HOME/.config/chromium-netflix"
FX_PROFILES="$HOME/.mozilla/firefox"
FX_PROFILE_DST="$HOME/.config/firefox-netflix"

ENGINE=chromium
COPY_PROFILE=0
NO_APT=0
NO_SERVICE=0
RELOGIN=0

usage() {
  cat <<EOF
Uso: ./install.sh [opciones]
  --firefox         usar Firefox en vez de Chromium (deja browser.engine = "firefox" en la configuración)
  --copiar-perfil   copia tu perfil del navegador al perfil propio de netflix-cec para no tener
                    que volver a iniciar sesión en Netflix. Cerrá el navegador antes.
                      Chromium: ~/.config/chromium -> ~/.config/chromium-netflix
                      Firefox:  tu perfil de ~/.mozilla/firefox -> ~/.config/firefox-netflix
  --sin-apt         no instalar paquetes (solo avisar si falta algo)
  --sin-servicio    no habilitar ni iniciar el servicio de systemd
EOF
}

for arg in "$@"; do
  case "$arg" in
    --firefox) ENGINE=firefox ;;
    --copiar-perfil) COPY_PROFILE=1 ;;
    --sin-apt) NO_APT=1 ;;
    --sin-servicio) NO_SERVICE=1 ;;
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
need=()
command -v cec-client >/dev/null 2>&1 || need+=(cec-utils)
python3 -c 'import websockets' 2>/dev/null || need+=(python3-websockets)
python3 -c 'import tomllib' 2>/dev/null || python3 -c 'import tomli' 2>/dev/null || need+=(python3-tomli)
if ((${#need[@]})); then
  if ((NO_APT)); then
    echo "Faltan paquetes: ${need[*]}  ->  sudo apt install ${need[*]}"
    exit 1
  fi
  echo "Instalando: ${need[*]}"
  sudo apt-get update
  sudo apt-get install -y "${need[@]}"
else
  echo "cec-utils, python3-websockets: OK"
fi
if [ "$ENGINE" = firefox ]; then
  if ! command -v firefox >/dev/null 2>&1 && ! command -v firefox-esr >/dev/null 2>&1; then
    warn "no encontré Firefox (firefox / firefox-esr)."
  fi
elif ! command -v chromium-browser >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
  warn "no encontré Chromium (chromium-browser / chromium)."
fi

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

# ---------------------------------------------------------------- archivos
step "Archivos"
mkdir -p "$DEST" "$BIN" "$CONF_DIR" "$UNIT_DIR" "$APPS"
rm -rf "$DEST/netflix_cec" "$DEST/tests"
cp -r "$SRC/netflix_cec" "$SRC/tests" "$DEST/"
cp "$SRC/README.md" "$SRC/config.example.toml" "$DEST/"
find "$DEST" -name __pycache__ -prune -exec rm -rf {} +
echo "Programa: $DEST"

cat > "$BIN/netflix-cec" <<EOF
#!/bin/sh
PYTHONPATH="$DEST\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m netflix_cec "\$@"
EOF
chmod +x "$BIN/netflix-cec"
echo "Comando: $BIN/netflix-cec"
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
if [ "$ENGINE" = firefox ]; then
  # Agrega engine = "firefox" en [browser] sin tocar el resto (y sin duplicar la tabla).
  python3 - "$CONF_DIR/config.toml" <<'PY'
import re, sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
engine = tomllib.loads(text).get("browser", {}).get("engine")
if engine == "firefox":
    print('Navegador: Firefox (ya estaba en la configuración)')
elif engine:
    print(f'AVISO: tu configuración dice browser.engine = "{engine}": cambialo a "firefox" a mano.')
else:
    m = re.search(r"^\[browser\][ \t]*$", text, re.M)
    if m:
        text = text[:m.end()] + '\nengine = "firefox"' + text[m.end():]
    else:
        text = text.rstrip("\n") + '\n\n[browser]\nengine = "firefox"\n'
    open(path, "w", encoding="utf-8").write(text)
    print('Navegador: Firefox (agregué engine = "firefox" en [browser])')
PY
fi

cat > "$APPS/netflix-tv.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Netflix (TV)
Comment=Netflix en pantalla completa, manejable con el control remoto de la TV
Exec=$BIN/netflix-cec navegador
Icon=video-display
Terminal=false
Categories=AudioVideo;Video;Player;
EOF
echo "Acceso en el menú: Netflix (TV)"

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
    warn "Firefox está abierto. Cerralo y corré de nuevo: ./install.sh --firefox --copiar-perfil"
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

# ---------------------------------------------------------------- systemd
step "Servicio de usuario (systemd)"
cp "$SRC/systemd/netflix-cec.service" "$UNIT_DIR/netflix-cec.service"
systemctl --user daemon-reload
if ((NO_SERVICE)); then
  echo "Instalado pero no habilitado (--sin-servicio). Para habilitarlo:"
  echo "  systemctl --user enable --now netflix-cec"
else
  systemctl --user enable netflix-cec.service
  systemctl --user restart netflix-cec.service
  sleep 2
  systemctl --user --no-pager --lines=0 status netflix-cec.service || true
fi

# ---------------------------------------------------------------- listo
step "Listo"
if ((RELOGIN)); then
  warn "te agregué a un grupo nuevo: REINICIÁ la Pi (o cerrá y abrí sesión) para que el servicio pueda usar el adaptador CEC."
fi
cat <<EOF
Próximos pasos:
  1. Probá el control:          netflix-cec aprender      (Ctrl+C para salir)
  2. Abrí Netflix:              apretá OK en el control, o "Netflix (TV)" en el menú
     La primera vez iniciá sesión en Netflix (salvo que hayas usado --copiar-perfil).
  3. Revisá que todo esté bien: netflix-cec diagnostico
  Logs en vivo:                 journalctl --user -u netflix-cec -f
  Configuración:                $CONF_DIR/config.toml
EOF
