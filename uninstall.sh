#!/usr/bin/env bash
# Remove the FT8 DX bot. Leaves mosquitto and OpenWebRX's MQTT setting in place
# (pass --all to also remove those).
set -euo pipefail
SERVICE=ft8-dx-bot
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo ./uninstall.sh"; exit 1; }

RUN_USER="${SUDO_USER:-root}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
APP_DIR="${APP_DIR:-$RUN_HOME/$SERVICE}"

systemctl disable --now $SERVICE 2>/dev/null || true
rm -f /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
echo "Removed service $SERVICE. Bot files left in $APP_DIR (delete manually if you want)."

if [ "${1:-}" = "--all" ]; then
  rm -f /etc/mosquitto/conf.d/ft8-dx-bot.conf
  systemctl restart mosquitto 2>/dev/null || true
  echo "Removed mosquitto config snippet. (OpenWebRX mqtt_enabled left unchanged - turn it off in admin if desired.)"
fi
