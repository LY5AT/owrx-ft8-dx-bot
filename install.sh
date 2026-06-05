#!/usr/bin/env bash
#
# Installer for the OpenWebRX 2 m FT8/MSK144 DX Telegram bot.
#
# Easiest (on the OpenWebRX machine, e.g. a Raspberry Pi over SSH):
#   curl -fsSLO https://raw.githubusercontent.com/LY5AT/owrx-ft8-dx-bot/main/install.sh && sudo bash install.sh
#
# Or from a cloned/extracted folder:  sudo ./install.sh
#
set -euo pipefail

SERVICE=ft8-dx-bot
REPO_RAW="https://raw.githubusercontent.com/LY5AT/owrx-ft8-dx-bot/main"
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd || pwd)"

if [ "$(id -u)" != 0 ]; then
  echo "Please run with sudo, e.g.:  sudo bash install.sh"
  exit 1
fi

RUN_USER="${SUDO_USER:-root}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
APP_DIR="${APP_DIR:-$RUN_HOME/$SERVICE}"
OWRX_SETTINGS="${OWRX_SETTINGS:-/var/lib/openwebrx/settings.json}"

# If run standalone (downloaded just install.sh), fetch the rest from GitHub.
# Preserve any config.env sitting next to it (e.g. written by the setup page).
if [ ! -f "$HERE/ft8bot.py" ]; then
  echo "==> Fetching bot files from GitHub"
  command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; }
  SRC0="$HERE"; HERE="$(mktemp -d)"
  curl -fsSL "$REPO_RAW/ft8bot.py"          -o "$HERE/ft8bot.py"
  curl -fsSL "$REPO_RAW/config.env.example" -o "$HERE/config.env.example"
  [ -f "$SRC0/config.env" ] && cp "$SRC0/config.env" "$HERE/config.env"
fi

echo "==> Installing $SERVICE for user '$RUN_USER' into $APP_DIR"

echo "==> 1/7 Installing dependencies (mosquitto, paho-mqtt)"
apt-get update -qq
apt-get install -y -qq mosquitto mosquitto-clients python3-paho-mqtt curl

echo "==> 2/7 Configuring local MQTT broker (localhost only)"
cat > /etc/mosquitto/conf.d/ft8-dx-bot.conf <<'EOF'
listener 1883 127.0.0.1
allow_anonymous true
EOF
systemctl enable --now mosquitto >/dev/null 2>&1 || true
systemctl restart mosquitto

echo "==> 3/7 Enabling MQTT publishing in OpenWebRX"
if [ -f "$OWRX_SETTINGS" ]; then
  python3 - "$OWRX_SETTINGS" <<'PY'
import json, sys, shutil, time
p = sys.argv[1]
shutil.copy(p, p + ".bak-ft8bot-" + time.strftime("%Y%m%d-%H%M%S"))
d = json.load(open(p))
d["mqtt_enabled"] = True
d["mqtt_host"] = "127.0.0.1"
d["mqtt_use_ssl"] = False
json.dump(d, open(p, "w"), indent=4)
print("    settings.json updated (backup saved)")
PY
  echo "    restarting OpenWebRX (brief downtime)..."
  systemctl restart openwebrx 2>/dev/null || systemctl restart openwebrx.service 2>/dev/null || \
    echo "    !! could not restart openwebrx automatically - restart it yourself"
else
  echo "    !! $OWRX_SETTINGS not found."
  echo "       If OpenWebRX lives elsewhere, re-run as: sudo OWRX_SETTINGS=/path/settings.json ./install.sh"
  echo "       Or enable MQTT manually in the OpenWebRX admin (host 127.0.0.1) and restart it."
fi

echo "==> 4/7 Installing bot files"
install -d -o "$RUN_USER" -g "$RUN_USER" "$APP_DIR"
install -o "$RUN_USER" -g "$RUN_USER" -m 0755 "$HERE/ft8bot.py" "$APP_DIR/ft8bot.py"

echo "==> 5/7 Telegram configuration"
if [ -f "$HERE/config.env" ] && grep -q '^TELEGRAM_TOKEN=[0-9]' "$HERE/config.env"; then
  install -o "$RUN_USER" -g "$RUN_USER" -m 600 "$HERE/config.env" "$APP_DIR/config.env"
  echo "    using the config.env you prepared (from the setup page) - no questions needed."
elif [ -f "$APP_DIR/config.env" ] && grep -q '^TELEGRAM_TOKEN=[0-9]' "$APP_DIR/config.env"; then
  echo "    config.env already has a token - keeping it."
else
  cp "$HERE/config.env.example" "$APP_DIR/config.env"
  echo
  echo "    Create a bot first: open Telegram, message @BotFather, /newbot, copy the token."
  read -rp "    Paste the bot token: " TG_TOKEN </dev/tty
  echo "    Now open YOUR new bot in Telegram and press Start (or send it 'hi')."
  read -rp "    Press Enter once you've messaged the bot... " _ </dev/tty || true
  TG_CHAT="$(curl -s "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" | python3 -c 'import sys,json
r=json.load(sys.stdin).get("result",[])
print(next((str(u.get("message",{}).get("chat",{}).get("id")) for u in reversed(r) if u.get("message")), ""))' 2>/dev/null || true)"
  if [ -z "$TG_CHAT" ]; then
    echo "    Could not auto-detect your chat id (message @userinfobot to get it)."
    read -rp "    Enter your numeric chat id: " TG_CHAT </dev/tty
  else
    echo "    Detected chat id: $TG_CHAT"
  fi
  python3 - "$APP_DIR/config.env" "$TG_TOKEN" "$TG_CHAT" <<'PY'
import sys
path, tok, chat = sys.argv[1], sys.argv[2], sys.argv[3]
out = []
for line in open(path):
    if line.startswith("TELEGRAM_TOKEN="):
        out.append("TELEGRAM_TOKEN=%s\n" % tok)
    elif line.startswith("TELEGRAM_CHAT_ID="):
        out.append("TELEGRAM_CHAT_ID=%s\n" % chat)
    else:
        out.append(line)
open(path, "w").writelines(out)
PY
  chown "$RUN_USER:$RUN_USER" "$APP_DIR/config.env"
  chmod 600 "$APP_DIR/config.env"
fi

echo "==> 6/7 Installing systemd service"
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=OpenWebRX 2 m FT8/MSK144 DX Telegram notifier
After=network-online.target mosquitto.service openwebrx.service
Wants=network-online.target mosquitto.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/config.env
ExecStart=/usr/bin/python3 $APP_DIR/ft8bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now $SERVICE

echo "==> 7/7 Registering Telegram command menu"
TG_TOKEN="$(grep '^TELEGRAM_TOKEN=' "$APP_DIR/config.env" | cut -d= -f2-)"
python3 - "$TG_TOKEN" <<'PY' || echo "    (menu registration skipped)"
import sys, json, urllib.request, urllib.parse
tok = sys.argv[1]
cmds = [
    {"command": "status",    "description": "Health, uptime, today's DX, ODX"},
    {"command": "spots",     "description": "Every recent decode, any band/distance"},
    {"command": "feed",      "description": "Live-stream every decode (/feed 10, /feed off)"},
    {"command": "last",      "description": "Recent DX alerts (e.g. /last 5)"},
    {"command": "best",      "description": "All-time + today's distance records"},
    {"command": "dxcc",      "description": "Countries heard on 2 m"},
    {"command": "mute",      "description": "Snooze alerts (e.g. /mute 30m, off)"},
    {"command": "threshold", "description": "Change km filter (e.g. /threshold 500, off)"},
    {"command": "map",       "description": "Live pskreporter map link"},
    {"command": "help",      "description": "Show all commands"},
]
data = urllib.parse.urlencode({"commands": json.dumps(cmds)}).encode()
urllib.request.urlopen("https://api.telegram.org/bot%s/setMyCommands" % tok, data=data, timeout=15).read()
print("    command menu registered")
PY

echo
echo "============================================================"
echo " Done.  Service: $SERVICE  (running as $RUN_USER)"
echo "   status:  systemctl status $SERVICE"
echo "   logs:    journalctl -u $SERVICE -f"
echo "   config:  $APP_DIR/config.env   (then: systemctl restart $SERVICE)"
echo " You should have a 'bot online' message in Telegram now."
echo "============================================================"
