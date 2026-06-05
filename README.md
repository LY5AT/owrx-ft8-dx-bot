# OpenWebRX 2 m FT8 / MSK144 DX Telegram bot

Sends a Telegram message whenever your OpenWebRX receiver hears a 2 m FT8 (or
MSK144 meteor-scatter) station more than 200 km away. It reads decodes locally
from OpenWebRX over MQTT - no internet scraping, near real-time.

Features:
- DX alerts with distance, bearing and SNR
- **Es opening detector** - one "band is open" alert on a burst, with direction
- **Grid / DXCC / ODX tracking** - tags new grids, new countries, distance records
- **False-decode filtering** - drops impossible distances and malformed calls
- **Interactive commands** - `/status` `/spots` `/feed` `/last` `/best` `/dxcc`
  `/mute` `/threshold` `/map`
- Survives reboots (systemd), auto-reconnects

## Requirements
- A Linux box running **OpenWebRX+** with background **services** enabled and
  **FT8** (and optionally MSK144) in the decoders list, covering the 2 m band.
- `sudo` access. Python 3 is already present (OpenWebRX needs it).

## Install (about 2 minutes)

First, **create a Telegram bot:** open Telegram, message **@BotFather**, send
`/newbot`, follow the prompts, and copy the token (looks like `8123456789:AAH...`).

Then, **SSH into your OpenWebRX machine** (Raspberry Pi or whatever runs it) and
paste this one line:

```bash
curl -fsSLO https://raw.githubusercontent.com/LY5AT/owrx-ft8-dx-bot/main/install.sh && sudo bash install.sh
```

It downloads everything it needs, then asks you to paste the bot token and to
press **Start** in your bot (so it can message you - your chat id is detected
automatically). That's it: you get a "bot online" message and the `≡` command
menu appears in the chat.

> Prefer not to pipe from the web? Clone the repo and run it locally instead:
> ```bash
> git clone https://github.com/LY5AT/owrx-ft8-dx-bot.git
> cd owrx-ft8-dx-bot && sudo ./install.sh
> ```
> Or, if you used the graphical `setup.html` page, drop the `config.env` it gave
> you next to `install.sh` first and the installer skips all the questions.

### What the installer changes
- Installs `mosquitto` (a tiny local MQTT broker, bound to localhost only).
- Sets `mqtt_enabled` + `mqtt_host=127.0.0.1` in OpenWebRX's `settings.json`
  (a timestamped backup is saved) and **restarts OpenWebRX once** (~20 s).
- Installs the bot to `~/ft8-dx-bot/` and a systemd service `ft8-dx-bot`.

If your OpenWebRX `settings.json` is not at `/var/lib/openwebrx/settings.json`:
```bash
sudo OWRX_SETTINGS=/path/to/settings.json ./install.sh
```

## Tuning
Edit `~/ft8-dx-bot/config.env`, then `sudo systemctl restart ft8-dx-bot`.
Common knobs: `MIN_KM`, `MODES` (add `FT4`), `MAX_KM`, `CONFIRM_DECODES` (set
`2` if junk slips through), `MIN_SNR`. See the comments in the file.

The receiver's location is read automatically from OpenWebRX; override with
`RX_LAT` / `RX_LON` / `RX_CALL` in `config.env` if needed.

## Commands
Text these to the bot (or use the `≡` menu):

| command | what |
|---|---|
| `/status` | health, uptime, today's tally, ODX, active filters |
| `/spots [n]` | every recent decode, any band/distance (incl. local) |
| `/feed [min] / off` | live-stream every decode, batched (great during an opening) |
| `/last [n]` | recent DX alerts |
| `/best` | all-time + today's distance records |
| `/dxcc` | countries heard on 2 m |
| `/mute 2h / 30m / off` | snooze alerts |
| `/threshold 500 / off` | change the km filter on the fly |
| `/map` | live pskreporter map link |

## Manage / remove
```bash
systemctl status ft8-dx-bot
journalctl -u ft8-dx-bot -f
sudo ./uninstall.sh          # remove the bot (add --all to also drop mosquitto cfg)
```

## Note on coverage
A single SDR can only decode one slice of spectrum at a time, and OpenWebRX
rotates its background services across bands/modes. So the bot catches 2 m DX
only while the radio is actually on 2 m FT8. For 24/7 2 m coverage, dedicate a
receiver/SDR to 144.174 MHz.
