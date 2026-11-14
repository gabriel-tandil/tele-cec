#!/usr/bin/env bash
# Desinstala tele-cec. Conserva tu configuración y los perfiles del navegador.
set -euo pipefail

UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/tele-cec.service"

systemctl --user disable --now tele-cec.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload || true
rm -f "$HOME/.local/bin/tele-cec"
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/netflix-tv.desktop"
rm -rf "$HOME/.local/share/tele-cec"

echo "tele-cec desinstalado."
echo "Se conservan (borralos a mano si querés):"
echo "  ${XDG_CONFIG_HOME:-$HOME/.config}/tele-cec/      (configuración)"
echo "  $HOME/.config/chromium-netflix/   (perfil de Chromium con la sesión de Netflix)"
echo "  $HOME/.config/firefox-netflix/    (perfil de Firefox con la sesión de Netflix)"
echo "  $HOME/.config/firefox-youtube/    (perfil de Firefox de YouTube para TV)"
if [ -f /etc/polkit-1/rules.d/50-tele-cec-apagado.rules ]; then
  echo "  /etc/polkit-1/rules.d/50-tele-cec-apagado.rules   (permiso de apagado: sudo rm para sacarlo)"
fi
