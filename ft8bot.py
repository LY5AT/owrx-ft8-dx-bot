#!/usr/bin/env python3
"""
LY5AT 2 m FT8 / MSK144 DX notifier  (v2).

Sources decodes from OpenWebRX's MQTT stream (topic openwebrx/<MODE>) on the
local broker and, for 2 m spots farther than the distance threshold, sends
Telegram alerts. On top of the basic per-station alert it adds:

  * sporadic-E OPENING detection (one "band is open" alert on a burst, plus a
    closing summary)
  * lifetime grid / DXCC tracking with NEW-grid / NEW-country / ODX tags and a
    nightly digest
  * interactive Telegram commands (/status /last /best /mute /threshold ...)

Config comes from the environment (see config.env, loaded by systemd):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID            (required)
  MIN_KM=200            distance threshold (km)
  MODES=FT8,MSK144      modes to watch
  BAND_MIN_HZ=144000000 / BAND_MAX_HZ=146000000
  EXPIRY_HOURS=24       re-alert a (call,grid) only after N hours
  OPENING_DX_KM=500     a spot must beat this to count toward an opening
  OPENING_MIN_STATIONS=4 distinct DX in the window to declare an opening
  OPENING_WINDOW_MIN=5  sliding window (minutes)
  OPENING_CLOSE_MIN=12  minutes of no DX before the opening is "closed"
  MQTT_HOST=127.0.0.1 / MQTT_PORT=1883 / MQTT_TOPIC=openwebrx/#
  STATE_FILE, OWRX_SETTINGS, STARTUP_PING=1
"""

import html
import json
import logging
import math
import os
import sys
import threading
import re
import time
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ft8bot")
START_TIME = time.time()


def env(key, default=None):
    return os.environ.get(key, default)


TOKEN = env("TELEGRAM_TOKEN")
CHAT_ID = env("TELEGRAM_CHAT_ID")
MIN_KM = float(env("MIN_KM", "200"))
MAX_KM = float(env("MAX_KM", "3000"))   # ceiling: >this on 2 m is a false decode (impossible distance)
MODES = {m.strip().upper() for m in env("MODES", "FT8,MSK144").split(",") if m.strip()}
BAND_MIN = int(env("BAND_MIN_HZ", "144000000"))
BAND_MAX = int(env("BAND_MAX_HZ", "146000000"))
EXPIRY = float(env("EXPIRY_HOURS", "24")) * 3600.0
# False-decode filtering: require a (call,grid) to be heard CONFIRM_DECODES times
# within CONFIRM_WINDOW_MIN before alerting. Applies only to CONFIRM_MODES (FT8/FT4
# stations repeat every cycle; MSK144 meteor pings are sparse so are left at 1).
CONFIRM_DECODES = int(env("CONFIRM_DECODES", "1"))
CONFIRM_WINDOW = float(env("CONFIRM_WINDOW_MIN", "10")) * 60.0
CONFIRM_MODES = {m.strip().upper() for m in env("CONFIRM_MODES", "FT8,FT4").split(",") if m.strip()}
_min_snr = env("MIN_SNR", "").strip()
MIN_SNR = float(_min_snr) if _min_snr not in ("", "off", "none") else None
OPENING_DX_KM = float(env("OPENING_DX_KM", "500"))
OPENING_MIN_STATIONS = int(env("OPENING_MIN_STATIONS", "4"))
OPENING_WINDOW = float(env("OPENING_WINDOW_MIN", "5")) * 60.0
OPENING_CLOSE = float(env("OPENING_CLOSE_MIN", "12")) * 60.0
MQTT_HOST = env("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(env("MQTT_PORT", "1883"))
MQTT_TOPIC = env("MQTT_TOPIC", "openwebrx/#")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = env("STATE_FILE", os.path.join(HERE, "state.json"))
OWRX_SETTINGS = env("OWRX_SETTINGS", "/var/lib/openwebrx/settings.json")
STARTUP_PING = env("STARTUP_PING", "1") == "1"

if not TOKEN or not CHAT_ID:
    log.error("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in the environment")
    sys.exit(1)

MODE_TAGS = {
    "FT8": ("\U0001F4E1", "FT8"),
    "FT4": ("\U0001F4E1", "FT4"),
    "MSK144": ("☄", "MSK144 meteor scatter"),
    "Q65": ("☄", "Q65"),
    "JT65": ("\U0001F4E1", "JT65"),
}

# --- receiver location: env override -> OpenWebRX settings.json ---------------
RX_LAT = RX_LON = None
RX_CALL = env("RX_CALL")
try:
    _s = json.load(open(OWRX_SETTINGS))
    _gps = _s.get("receiver_gps") or {}
    RX_LAT = float(_gps.get("lat"))
    RX_LON = float(_gps.get("lon"))
    RX_CALL = RX_CALL or _s.get("pskreporter_callsign") or _s.get("receiver_name")
except Exception as exc:  # noqa: BLE001
    log.warning("could not read receiver location from %s (%s)", OWRX_SETTINGS, exc)
if env("RX_LAT"):
    RX_LAT = float(env("RX_LAT"))
if env("RX_LON"):
    RX_LON = float(env("RX_LON"))
RX_CALL = RX_CALL or "OWRX"
if RX_LAT is None or RX_LON is None:
    log.error("No receiver coordinates - set RX_LAT/RX_LON in config.env "
              "or receiver_gps in OpenWebRX, then restart.")
    sys.exit(1)


# --- geo helpers -------------------------------------------------------------
def grid_to_latlon(grid):
    if not grid:
        return None
    g = grid.strip()[:8]
    if len(g) < 4:
        return None
    try:
        lon = (ord(g[0].upper()) - 65) * 20 - 180
        lat = (ord(g[1].upper()) - 65) * 10 - 90
        lon += int(g[2]) * 2
        lat += int(g[3]) * 1
        if len(g) >= 6:
            lon += (ord(g[4].lower()) - 97) * (2.0 / 24)
            lat += (ord(g[5].lower()) - 97) * (1.0 / 24)
            if len(g) >= 8:
                lon += (ord(g[6]) - 48) * (2.0 / 24 / 10) + (2.0 / 24 / 10) / 2
                lat += (ord(g[7]) - 48) * (1.0 / 24 / 10) + (1.0 / 24 / 10) / 2
            else:
                lon += (2.0 / 24) / 2
                lat += (1.0 / 24) / 2
        else:
            lon += 1.0
            lat += 0.5
        return lat, lon
    except (ValueError, IndexError):
        return None


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(brg):
    return _COMPASS[int((brg + 11.25) // 22.5) % 16]


def circular_mean(angles):
    if not angles:
        return 0.0
    sx = sum(math.sin(math.radians(a)) for a in angles)
    sy = sum(math.cos(math.radians(a)) for a in angles)
    return (math.degrees(math.atan2(sx, sy)) + 360) % 360


_BANDS = [
    (1810000, 2000000, "160m"), (3500000, 3800000, "80m"), (5250000, 5450000, "60m"),
    (7000000, 7300000, "40m"), (10100000, 10150000, "30m"), (14000000, 14350000, "20m"),
    (18068000, 18168000, "17m"), (21000000, 21450000, "15m"), (24890000, 24990000, "12m"),
    (28000000, 29700000, "10m"), (50000000, 54000000, "6m"), (70000000, 70500000, "4m"),
    (144000000, 148000000, "2m"), (430000000, 440000000, "70cm"), (1240000000, 1300000000, "23cm"),
]


def freq_to_band(freq):
    for lo, hi, name in _BANDS:
        if lo <= freq <= hi:
            return name
    return "%.0f MHz" % (freq / 1e6)


# Structural sanity check for a callsign (catches obvious junk, not all false decodes).
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z]{1,4}(/[A-Z0-9]{1,4})?$")


def valid_callsign(call):
    return bool(_CALLSIGN_RE.match(call.upper()))


# --- state -------------------------------------------------------------------
state_lock = threading.Lock()
state = {}
recent = []          # in-memory ring of recent DX alerts (also persisted)
all_recent = []      # in-memory ring of EVERY decode, any band/distance (persisted)
feed_buffer = []     # in-memory: decodes awaiting a batched live-feed flush
last_feed_flush = [0.0]
confirm_seen = {}    # in-memory: (call|grid4) -> [timestamps] for false-decode filtering
dx_window = []       # in-memory: recent DX (>= OPENING_DX_KM) for opening detection
opening = None       # in-memory: current opening accumulator or None
LAST_DECODE_TS = [0.0]


def default_state():
    return {
        "grids": {}, "alerts": {},
        "heard_grids": {}, "heard_dxcc": {},
        "odx": {"km": 0.0, "call": "", "grid": "", "ts": 0},
        "daily": new_daily(today_str()),
        "mute_until": 0, "min_km_override": None,
        "tg_offset": 0, "recent": [], "all_recent": [], "feed_until": 0,
    }


def today_str():
    return time.strftime("%Y-%m-%d", time.localtime())


def new_daily(date):
    return {"date": date, "count": 0, "calls": [], "ccodes": {}, "grids": [],
            "best": {"km": 0.0, "call": "", "grid": ""}, "new_grids": 0, "new_dxcc": []}


def load_state():
    global state, recent
    try:
        loaded = json.load(open(STATE_FILE))
    except Exception:  # noqa: BLE001
        loaded = {}
    base = default_state()
    base.update(loaded if isinstance(loaded, dict) else {})
    # make sure nested keys exist after a partial/old file
    for k, v in default_state().items():
        base.setdefault(k, v)
    if not isinstance(base.get("daily"), dict) or "date" not in base["daily"]:
        base["daily"] = new_daily(today_str())
    state = base
    recent = list(state.get("recent", []))
    # scrub any previously-logged garbage (impossible 2 m distances) from history
    all_recent[:] = [e for e in state.get("all_recent", [])
                     if not (e.get("band") == "2m" and e.get("km") is not None
                             and e["km"] > MAX_KM)]


def save_state():
    try:
        state["recent"] = recent[-25:]
        state["all_recent"] = all_recent[-60:]
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save state: %s", exc)


def effective_min_km():
    ov = state.get("min_km_override")
    return float(ov) if ov else MIN_KM


def is_muted():
    return time.time() < state.get("mute_until", 0)


def record_recent(now, call, grid, freq, mode, snr, km):
    """Log EVERY decode (any band / distance) to the in-memory firehose buffer."""
    all_recent.append({"t": now, "call": call, "grid": grid or "", "freq": freq,
                       "band": freq_to_band(freq), "mode": mode,
                       "snr": (int(snr) if snr is not None else None),
                       "km": (round(km) if km is not None else None)})
    del all_recent[:-200]


# --- telegram ----------------------------------------------------------------
def tg_call(method, params, timeout=15):
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def send(text, force=False):
    """Send to the owner. Suppressed while muted unless force=True."""
    if is_muted() and not force:
        return False
    for attempt in range(3):
        try:
            tg_call("sendMessage", {"chat_id": CHAT_ID, "text": text,
                                    "parse_mode": "HTML", "disable_web_page_preview": "true"})
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram send failed (try %d/3): %s", attempt + 1, exc)
            time.sleep(2)
    return False


# --- stats / records (call under state_lock) ---------------------------------
def ensure_today():
    """Roll the daily bucket over at local midnight. Returns a summary string
    for the *previous* day if there was activity, else None."""
    today = today_str()
    if state["daily"]["date"] == today:
        return None
    prev = state["daily"]
    summary = format_daily(prev) if prev["count"] > 0 else None
    state["daily"] = new_daily(today)
    return summary


def record_stats(call, grid, dist, ccode, country, ts):
    """Update lifetime + daily stats. Returns list of tag strings."""
    tags = []
    g4 = (grid or "")[:4].upper()
    d = state["daily"]
    d["count"] += 1
    if call not in d["calls"]:
        d["calls"].append(call)
    if g4 and g4 not in d["grids"]:
        d["grids"].append(g4)
    if dist > d["best"]["km"]:
        d["best"] = {"km": dist, "call": call, "grid": grid}
    if g4 and g4 not in state["heard_grids"]:
        state["heard_grids"][g4] = ts
        d["new_grids"] += 1
        tags.append("\U0001F195 NEW GRID")
    if ccode and ccode not in state["heard_dxcc"]:
        state["heard_dxcc"][ccode] = {"name": country or ccode, "first": ts}
        if (country or ccode) not in d["new_dxcc"]:
            d["new_dxcc"].append(country or ccode)
        tags.append("\U0001F195 NEW DXCC")
    if ccode:
        d["ccodes"][ccode] = country or ccode
    if dist > state["odx"]["km"]:
        state["odx"] = {"km": dist, "call": call, "grid": grid, "ts": ts}
        tags.append("\U0001F3C6 ODX")
    return tags


def format_daily(d):
    countries = ", ".join(sorted(set(d["ccodes"].values()))) or "-"
    best = d["best"]
    txt = ("\U0001F4CA <b>2 m daily — %s</b>\n"
           "%d DX spots · %d countries · %d grids\n"
           "Best: %.0f km %s (%s)") % (
        d["date"], d["count"], len(set(d["ccodes"].values())), len(d["grids"]),
        best["km"], best["call"], best["grid"])
    extra = []
    if d["new_grids"]:
        extra.append("%d new grids" % d["new_grids"])
    if d["new_dxcc"]:
        extra.append("new DXCC: " + ", ".join(d["new_dxcc"]))
    if extra:
        txt += "\n" + " · ".join(extra)
    txt += "\nCountries: " + countries
    return txt


# --- opening detection (call under state_lock) -------------------------------
def feed_opening(call, grid, dist, brg, ccode, country, now):
    """Returns ('open', text) / ('close', text) / (None, None)."""
    global opening
    if dist < OPENING_DX_KM:
        return None, None
    LAST_DECODE_TS[0] = now
    dx_window.append({"ts": now, "call": call, "brg": brg, "dist": dist,
                      "grid": grid, "ccode": ccode, "country": country})
    cutoff = now - max(OPENING_WINDOW, OPENING_CLOSE)
    while dx_window and dx_window[0]["ts"] < cutoff:
        dx_window.pop(0)

    if opening is None:
        recent_dx = [s for s in dx_window if s["ts"] >= now - OPENING_WINDOW]
        calls = {s["call"] for s in recent_dx}
        if len(calls) >= OPENING_MIN_STATIONS:
            opening = {"start": now, "last": now, "calls": set(), "ccodes": {},
                       "best": {"km": 0.0, "call": "", "grid": ""}, "brgs": []}
            for s in recent_dx:
                _acc_opening(s)
            return "open", format_opening_open(recent_dx)
    else:
        opening["last"] = now
        _acc_opening({"call": call, "brg": brg, "dist": dist, "grid": grid,
                      "ccode": ccode, "country": country})
    return None, None


def _acc_opening(s):
    opening["calls"].add(s["call"])
    if s.get("ccode"):
        opening["ccodes"][s["ccode"]] = s.get("country") or s["ccode"]
    opening["brgs"].append(s["brg"])
    if s["dist"] > opening["best"]["km"]:
        opening["best"] = {"km": s["dist"], "call": s["call"], "grid": s["grid"]}


def format_opening_open(recent_dx):
    brg = circular_mean([s["brg"] for s in recent_dx])
    best = opening["best"]
    calls = ", ".join(sorted({s["call"] for s in recent_dx}))
    mins = max(1, round((recent_dx[-1]["ts"] - recent_dx[0]["ts"]) / 60.0))
    return ("\U0001F525 <b>2 m Es OPENING</b>\n"
            "%d DX in ~%d min, heading <b>%s</b> (%.0f°)\n"
            "Furthest: %s %s %.0f km\n"
            "Calls: %s") % (len({s["call"] for s in recent_dx}), mins,
                            compass(brg), brg, best["call"], best["grid"], best["km"], calls)


def maybe_close_opening(now):
    """Called from the ticker. Returns a closing-summary string or None."""
    global opening
    if opening is None:
        return None
    if now - opening["last"] < OPENING_CLOSE:
        return None
    dur = max(1, round((opening["last"] - opening["start"]) / 60.0))
    best = opening["best"]
    countries = sorted(set(opening["ccodes"].values()))
    txt = ("\U0001F4C9 <b>2 m opening closed</b> (lasted %d min)\n"
           "%d stations · %d countries · best %.0f km (%s)") % (
        dur, len(opening["calls"]), len(countries), best["km"], best["call"])
    if countries:
        txt += "\n" + ", ".join(countries)
    opening = None
    dx_window.clear()
    return txt


# --- decode handling ---------------------------------------------------------
def handle_spot(spot):
    mode = str(spot.get("mode", "")).upper()
    call = spot.get("callsign")
    if not call:
        return
    try:
        freq = int(spot.get("freq", 0))
    except (TypeError, ValueError):
        return
    if not freq:
        return

    loc = spot.get("locator")
    ccode = spot.get("ccode")
    country = spot.get("country")
    snr = spot.get("db")
    ts = spot.get("timestamp")
    now = time.time()

    with state_lock:
        if loc:
            state["grids"][call] = loc
        grid = loc or state["grids"].get(call)

    coords = grid_to_latlon(grid) if grid else None
    dist = haversine_km(RX_LAT, RX_LON, coords[0], coords[1]) if coords else None
    brg = bearing_deg(RX_LAT, RX_LON, coords[0], coords[1]) if coords else None
    is_2m = BAND_MIN <= freq <= BAND_MAX

    # Drop garbage outright, even from the raw feed/spots: on 2 m an impossible
    # distance or a malformed callsign can only be a false decode. (HF long-haul
    # is legitimate, so the distance ceiling is applied to 2 m only.)
    if is_2m and ((dist is not None and dist > MAX_KM) or not valid_callsign(call)):
        log.info("DROP garbage %s %s %s km", call, grid, round(dist) if dist is not None else "?")
        return

    # firehose: log every (plausible) decode + feed the live buffer
    with state_lock:
        record_recent(now, call, grid, freq, mode, snr, dist)
        if now < state.get("feed_until", 0):
            feed_buffer.append({"t": now, "call": call, "grid": grid or "",
                                "band": freq_to_band(freq), "mode": mode,
                                "snr": (int(snr) if snr is not None else None),
                                "km": (round(dist) if dist is not None else None)})

    # DX alert path: watched mode, 2 m band, has a locator, and in range
    if (mode not in MODES or not is_2m
            or grid is None or dist is None or dist < effective_min_km()):
        return
    if MIN_SNR is not None and snr is not None and float(snr) < MIN_SNR:
        return

    key = "%s|%s" % (call, grid[:6].upper())
    needed = CONFIRM_DECODES if mode in CONFIRM_MODES else 1
    rollover_summary = None
    open_evt = open_txt = None
    do_alert = False
    tags = []
    with state_lock:
        # confirmation: count decodes of this (call,grid) in the window
        seen = [t for t in confirm_seen.get(key, []) if now - t < CONFIRM_WINDOW]
        seen.append(now)
        confirm_seen[key] = seen
        for k in [k for k, v in confirm_seen.items() if not v or now - v[-1] > CONFIRM_WINDOW]:
            confirm_seen.pop(k, None)
        if len(seen) < needed:
            log.info("HOLD %s %s %.0fkm (%d/%d confirmations)", call, grid, dist, len(seen), needed)
            save_state()
            return
        rollover_summary = ensure_today()
        last = state["alerts"].get(key, 0)
        if now - last >= EXPIRY:
            state["alerts"][key] = now
            state["alerts"] = {k: v for k, v in state["alerts"].items() if now - v < EXPIRY}
            tags = record_stats(call, grid, dist, ccode, country, ts or int(now * 1000))
            do_alert = True
        open_evt, open_txt = feed_opening(call, grid, dist, brg, ccode, country, now)
        if do_alert:
            entry = {"call": call, "grid": grid, "km": round(dist), "brg": round(brg),
                     "mode": mode, "t": now}
            recent.append(entry)
            del recent[:-25]
        save_state()

    if rollover_summary:
        send(rollover_summary)
    if do_alert:
        emoji, label = MODE_TAGS.get(mode, ("\U0001F4E1", mode))
        when = time.strftime("%H:%M", time.gmtime(ts / 1000.0)) if ts else time.strftime("%H:%M", time.gmtime())
        snr_str = ("%+d" % int(snr)) if snr is not None else "?"
        head = "%s <b>2 m %s DX</b>" % (emoji, label)
        if tags:
            head += "  " + "  ".join(tags)
        text = ("%s\n<b>%s</b>  ·  %s\n%.0f km  @  %.0f° (%s)\n%.3f MHz  ·  %s dB  ·  %s UTC") % (
            head, call, grid, dist, brg, compass(brg), freq / 1e6, snr_str, when)
        raw = spot.get("msg")
        if raw:
            text += "\n<code>%s</code>" % html.escape(str(raw))
        sent = send(text)
        log.info("ALERT %s %s %.0f km %.0f deg %s sent=%s muted=%s",
                 call, grid, dist, brg, ",".join(t.split()[-1] for t in tags) or "-",
                 sent, is_muted())
    if open_evt == "open":
        send(open_txt)
        log.info("OPENING start")


# --- interactive commands ----------------------------------------------------
def fmt_age(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % (seconds // 60)
    if seconds < 172800:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def cmd_status():
    now = time.time()
    with state_lock:
        d = state["daily"]
        odx = state["odx"]
        muted = is_muted()
        mute_left = state.get("mute_until", 0) - now
        thr = effective_min_km()
        ov = state.get("min_km_override")
        open_now = opening is not None
        last_dec = LAST_DECODE_TS[0]
    lines = [
        "✅ <b>%s 2 m DX bot</b>" % RX_CALL,
        "up %s · modes %s" % (fmt_age(now - START_TIME), "/".join(sorted(MODES))),
        "range %.0f-%.0f km%s" % (thr, MAX_KM, " (override)" if ov else ""),
        "garbage filter: callsign + distance%s%s" % (
            (" + confirm %dx" % CONFIRM_DECODES) if CONFIRM_DECODES > 1 else "",
            (" + SNR>=%g" % MIN_SNR) if MIN_SNR is not None else ""),
        "today: %d DX · %d countries · best %.0f km" % (
            d["count"], len(set(d["ccodes"].values())), d["best"]["km"]),
        "all-time ODX: %.0f km %s (%s)" % (odx["km"], odx["call"] or "-", odx["grid"] or "-"),
        "grids heard: %d · DXCC: %d" % (len(state["heard_grids"]), len(state["heard_dxcc"])),
        "opening: %s" % ("OPEN now" if open_now else "no"),
        "last 2 m DX: %s" % (fmt_age(now - last_dec) + " ago" if last_dec else "never"),
        "muted: %s" % (("yes, %s left" % fmt_age(mute_left)) if muted else "no"),
    ]
    return "\n".join(lines)


def cmd_last(n):
    with state_lock:
        items = list(recent[-n:])
    if not items:
        return "No DX logged yet."
    now = time.time()
    rows = ["<b>Last %d DX:</b>" % len(items)]
    for e in reversed(items):
        rows.append("%s %s · %d km @ %d° · %s ago" % (
            e["call"], e["grid"], e["km"], e["brg"], fmt_age(now - e["t"])))
    return "\n".join(rows)


def cmd_best():
    with state_lock:
        odx = dict(state["odx"])
        d = dict(state["daily"])
    txt = ["\U0001F3C6 <b>Records</b>",
           "All-time ODX: %.0f km — %s %s" % (odx["km"], odx["call"] or "-", odx["grid"] or "-"),
           "Today best: %.0f km — %s %s" % (d["best"]["km"], d["best"]["call"] or "-", d["best"]["grid"] or "-"),
           "Today: %d DX · %d countries · %d grids" % (
               d["count"], len(set(d["ccodes"].values())), len(d["grids"]))]
    return "\n".join(txt)


def cmd_mute(arg):
    secs = 0
    arg = (arg or "").strip().lower()
    if arg in ("off", "0", "none"):
        with state_lock:
            state["mute_until"] = 0
            save_state()
        return "\U0001F514 Unmuted."
    if not arg:
        secs = 8 * 3600
    else:
        try:
            num = float(arg[:-1]) if arg[-1] in "hmd" else float(arg)
            unit = arg[-1] if arg[-1] in "hmd" else "h"
            secs = num * {"m": 60, "h": 3600, "d": 86400}[unit]
        except (ValueError, KeyError):
            return "Usage: /mute 2h | 30m | off"
    with state_lock:
        state["mute_until"] = time.time() + secs
        save_state()
    return "\U0001F507 Muted for %s." % fmt_age(secs)


def cmd_threshold(arg):
    arg = (arg or "").strip().lower()
    if not arg:
        return "Threshold is %.0f km. Use /threshold 500 or /threshold off." % effective_min_km()
    if arg in ("off", "reset", "default"):
        with state_lock:
            state["min_km_override"] = None
            save_state()
        return "Threshold reset to %.0f km (config)." % MIN_KM
    try:
        km = float(arg)
    except ValueError:
        return "Usage: /threshold 500 | off"
    with state_lock:
        state["min_km_override"] = km
        save_state()
    return "Threshold set to %.0f km." % km


def cmd_dxcc():
    with state_lock:
        names = sorted({v["name"] for v in state["heard_dxcc"].values()})
    if not names:
        return "No DXCC logged yet."
    return "<b>%d DXCC heard on 2 m:</b>\n%s" % (len(names), ", ".join(names))


def cmd_map():
    return ("Live 2 m map for %s:\nhttps://pskreporter.info/pskmap.html?preset=&callsign=%s"
            "&timerange=3600&mode=FT8&band=2m" % (RX_CALL, RX_CALL))


def _fmt_spot_line(e, now=None):
    snr = ("%+d" % e["snr"]) if e.get("snr") is not None else "?"
    dist = ("%d km" % e["km"]) if e.get("km") is not None else "no-grid"
    t = time.strftime("%H:%M", time.gmtime(e["t"]))
    return "%s  <b>%s</b> %s · %s · %s · %s · %sdB" % (
        t, e["call"], e.get("grid") or "-", e.get("band", "?"), e.get("mode", "?"), dist, snr)


def cmd_spots(n, band_filter=None):
    with state_lock:
        items = list(all_recent)
    if band_filter:
        items = [e for e in items if e.get("band") == band_filter]
    items = items[-n:]
    if not items:
        return ("No spots logged yet - the radio may be off this band right now "
                "(single SDR rotates bands/modes).")
    title = "Last %d spots%s" % (len(items), (" on " + band_filter) if band_filter else " (all bands)")
    return "<b>%s:</b>\n" % title + "\n".join(_fmt_spot_line(e) for e in reversed(items))


def cmd_feed(arg):
    arg = (arg or "").strip().lower()
    if arg in ("off", "0", "stop", "none"):
        with state_lock:
            state["feed_until"] = 0
            feed_buffer.clear()
            save_state()
        return "\U0001F4F4 Live feed off."
    try:
        mins = int(float(arg[:-1]) * (60 if arg[-1] == "h" else 1)) if arg and arg[-1] in "hm" else (int(arg) if arg else 30)
    except (ValueError, TypeError):
        mins = 30
    mins = max(1, min(720, mins))
    with state_lock:
        state["feed_until"] = time.time() + mins * 60
        save_state()
    return ("\U0001F4E5 Live feed ON for %dm - every decode (all bands), batched ~20 s.\n"
            "/feed off to stop." % mins)


def format_feed(items):
    head = "\U0001F4E5 <b>live feed</b> · %d spots" % len(items)
    shown = items[-40:]
    body = "\n".join(_fmt_spot_line(e) for e in shown)
    if len(items) > len(shown):
        body += "\n…(+%d more)" % (len(items) - len(shown))
    return head + "\n" + body


HELP = ("<b>Commands</b>\n"
        "/status - health, records, mute\n"
        "/spots [n] - every recent decode, any distance\n"
        "/feed [min] | off - live stream every decode\n"
        "/last [n] - recent DX alerts (default 10)\n"
        "/best - ODX + today's records\n"
        "/dxcc - countries heard on 2 m\n"
        "/mute 2h | 30m | off - snooze alerts\n"
        "/threshold 500 | off - change km filter\n"
        "/map - pskreporter link\n"
        "/help - this")


def handle_command(text):
    parts = text.split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "status":
        reply = cmd_status()
    elif cmd in ("spots", "spot", "all"):
        n, band_filter = 12, None
        for p in parts[1:]:
            if p.isdigit():
                n = max(1, min(40, int(p)))
            elif p.lower().endswith("m") or p.lower().endswith("cm"):
                band_filter = p.lower()
        reply = cmd_spots(n, band_filter)
    elif cmd == "feed":
        reply = cmd_feed(arg)
    elif cmd == "last":
        try:
            n = max(1, min(25, int(arg)))
        except (ValueError, TypeError):
            n = 10
        reply = cmd_last(n)
    elif cmd == "best":
        reply = cmd_best()
    elif cmd in ("mute", "snooze"):
        reply = cmd_mute(arg)
    elif cmd in ("threshold", "dist", "km"):
        reply = cmd_threshold(arg)
    elif cmd == "dxcc":
        reply = cmd_dxcc()
    elif cmd == "map":
        reply = cmd_map()
    else:
        reply = HELP
    send(reply, force=True)


def telegram_poll():
    with state_lock:
        offset = state.get("tg_offset", 0)
    while True:
        try:
            resp = tg_call("getUpdates", {"offset": offset, "timeout": 50}, timeout=60)
            for u in resp.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message") or {}
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue
                txt = (msg.get("text") or "").strip()
                if txt.startswith("/"):
                    log.info("command: %s", txt)
                    handle_command(txt)
            with state_lock:
                state["tg_offset"] = offset
                save_state()
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram poll: %s", exc)
            time.sleep(5)


# --- ticker (time-based housekeeping) ----------------------------------------
def ticker():
    while True:
        time.sleep(20)
        now = time.time()
        try:
            feed_flush = None
            with state_lock:
                summary = ensure_today()
                close_txt = maybe_close_opening(now)
                if feed_buffer and now - last_feed_flush[0] >= 18:
                    feed_flush = list(feed_buffer)
                    feed_buffer.clear()
                    last_feed_flush[0] = now
                save_state()  # also persists the firehose log
            if close_txt:
                send(close_txt)
                log.info("OPENING closed")
            if summary:
                send(summary)
            if feed_flush:
                send(format_feed(feed_flush))
        except Exception:  # noqa: BLE001
            log.exception("ticker error")


# --- mqtt --------------------------------------------------------------------
def on_connect(client, userdata, flags, rc, *args):
    if rc == 0:
        log.info("connected to mqtt %s:%d, subscribing %s", MQTT_HOST, MQTT_PORT, MQTT_TOPIC)
        client.subscribe(MQTT_TOPIC)
    else:
        log.warning("mqtt connect returned rc=%s", rc)


def on_message(client, userdata, msg):
    try:
        spot = json.loads(msg.payload.decode())
    except Exception:  # noqa: BLE001
        return
    try:
        handle_spot(spot)
    except Exception:  # noqa: BLE001
        log.exception("error handling spot from %s", msg.topic)


def main():
    load_state()
    log.info("%s 2 m DX bot | RX %.4f,%.4f | %.0f-%.0f km | modes=%s | opening>=%d in %dm",
             RX_CALL, RX_LAT, RX_LON, MIN_KM, MAX_KM, ",".join(sorted(MODES)),
             OPENING_MIN_STATIONS, int(OPENING_WINDOW / 60))
    threading.Thread(target=telegram_poll, name="tg-poll", daemon=True).start()
    threading.Thread(target=ticker, name="ticker", daemon=True).start()
    if STARTUP_PING:
        send("✅ <b>%s 2 m DX bot online</b>\n>%.0f km on 2 m %s · opening detect + records + /help"
             % (RX_CALL, MIN_KM, "/".join(sorted(MODES))), force=True)

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as exc:  # noqa: BLE001
            log.warning("mqtt loop error: %s; retrying in 5 s", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
