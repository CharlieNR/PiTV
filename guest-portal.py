#!/usr/bin/env python3
"""
/opt/pitv/guest-portal.py — PiTV web portals (Python stdlib only).

Runs in one of two modes from the same code:

  guest  (default, port 8080)  — password OR NFC login; TV power, HDMI input,
         menu D-pad; shows the guest's player PIN. ONLY works while the Apple
         Home "Guest Mode" switch is ON.
  admin  (--admin, port 80 -> http://<hostname>.local) — username + password
         login; the same controls PLUS a Guest Mode on/off toggle; never gated.

Passwords are PBKDF2-HMAC-SHA256 (see pitv_secrets.py); records written by
older versions were a single round of SHA-256 and are re-hashed in place the
next time that person signs in. Login failures are counted per client IP and
locked out with a doubling backoff.

Persistent data lives together in ~/.pitv/pitv-state.json (device-only,
never committed): guest/admin accounts and password hashes, player PINs,
portal-signing secrets, EMERGENCY_PIN, CONTROL_KEY, pairing, Sky/network
settings and persistent lockout state. pitv_state.py is the shared reader/writer.
State:
  /tmp/pitv-guest-mode   "on"/"off" (Home switch, or the admin toggle)
Actuation:
  TV power / HDMI / Guest Mode -> localhost UDP 127.0.0.1:8129 (tv_menu)
  menu navigation              -> the FIFO /tmp/tv_menu.fifo
"""

import base64
import fcntl
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pitv_secrets
import pitv_state
import pitv_imposter

ADMIN           = "--admin" in sys.argv[1:]
PORT            = 80 if ADMIN else 8080
COOKIE_NAME     = "pitv_admin" if ADMIN else "pitv_session"

PITV_DIR        = os.path.expanduser("~/.pitv")
GUEST_FILE      = "GUEST_ACCOUNTS"
ADMIN_FILE      = "ADMIN_ACCOUNTS"
PIN_FILE        = "PLAYER_PINS"
# Separate signing keys per portal so `screen guest kick all` (which rotates
# the guest key) can't log admins out. Rotating a key invalidates every
# session cookie signed with it.
GUEST_SECRET    = "GUEST_PORTAL_SECRET"
ADMIN_SECRET    = "ADMIN_PORTAL_SECRET"
SECRET_FILE     = ADMIN_SECRET if ADMIN else GUEST_SECRET
GUEST_MODE_FILE = "/tmp/pitv-guest-mode"
SKY_FILE        = "SKY_SETTINGS"
# tv_menu writes the pairing code here; we only ever read it.
PAIR_FILE       = "PAIRING"
# Which client addresses may reach the portals at all (`screen network ...`).
# Empty list = anyone who can route to the Pi, which is the historical
# behaviour; add a CIDR to shut out, say, a VPN range.
NETWORK_FILE    = "NETWORK_SETTINGS"
FIFO            = "/tmp/tv_menu.fifo"
GAME_STATE_FILE = "/tmp/pitv-game-state"
UDP_ADDR        = ("127.0.0.1", 8129)
# Admin sessions effectively never expire; guest sessions last a week.
SESSION_MAX_AGE = (3650 if ADMIN else 7) * 24 * 3600
# Signing in as a guest has two steps: the account password, then the
# "connection password" shown on the TV. Between the two the guest holds a
# half-session — it proves they got the password right and lets them ask for
# the code, and it grants no control of anything.
HALF_PREFIX     = "half:"
HALF_AGE        = 600
# Don't let anyone flash the code onto the TV over and over.
PAIR_SHOW_COOL  = 20
# The admin "Lock" switch: guests keep their session but every control is
# greyed out until it is lifted. In /tmp so a reboot always clears it.
GUEST_LOCK_FILE = "/tmp/pitv-guest-lock"
# How often a signed-in page re-checks state, so a kick, a lock or Guest Mode
# going off lands in seconds instead of whenever someone happens to reload.
POLL_SECONDS    = 3
ROTATE_PERIOD   = 7 * 24 * 3600

# Login throttling. The guest portal takes a password with no username, so it
# is the one credential worth guessing on this box; without a limiter a phone
# on the Wi-Fi could try thousands a minute. Failures are counted per client IP
# and the lockout doubles, to a ceiling.
MAX_FAILS       = 5
FAIL_WINDOW     = 15 * 60
LOCK_BASE       = 30
LOCK_CEILING    = 15 * 60
MAX_BODY        = 8192        # a login form is a few hundred bytes

# A single local keyboard lease shared by the admin and guest portal processes.
# The lock lives in ~/.pitv (mode 0700); its data contains only a random lease
# token and an opaque hash of the signed-in session cookie.
KEYBOARD_LEASE_PATH = os.path.join(PITV_DIR, "remote-keyboard-lease.json")
KEYBOARD_GUARD_PATH = os.path.join(PITV_DIR, "remote-keyboard-lease.lock")
KEYBOARD_LEASE_SECONDS = 90
KEYBOARD_MAX_CHARS = 120

# PIN-keypad prompts are published by tv_menu and share a first-claimant lease
# across the guest and admin portal processes.
PIN_ENTRY_STATE_FILE = "/tmp/pitv-pin-entry-state"
PIN_LEASE_PATH = os.path.join(PITV_DIR, "pin-entry-lease.json")
PIN_GUARD_PATH = os.path.join(PITV_DIR, "pin-entry-lease.lock")
PIN_LEASE_SECONDS = 90
PIN_PROMPT_SECONDS = 10

NAV = {"up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
       "ok": "SELECT", "back": "BACK"}
UDP = {"tv_on": "TV_ON", "tv_off": "TV_OFF",
       "input_sky": "CEC tx 1f:82:10:00", "input_pitv": "CEC tx 1f:82:20:00"}
ADMIN_UDP = {"guest_on": "GUEST_ON", "guest_off": "GUEST_OFF"}

# Sky Q buttons the portal may press. The payload is a fixed token, never
# anything the browser supplies, and tv_menu ignores the lot unless Sky mode is
# on (`screen sky on`).
SKY_BUTTONS = [
    ("sky_sky",     "SKY",     "Sky"),
    ("sky_guide",   "GUIDE",   "TV Guide"),
    ("sky_info",    "INFO",    "Info"),
    ("sky_ch_up",   "CH_UP",   "CH +"),
    ("sky_ch_down", "CH_DOWN", "CH −"),
    ("sky_rewind",  "REWIND",  "&#9194;"),
    ("sky_play",    "PLAY",    "&#9654;"),
    ("sky_pause",   "PAUSE",   "&#10074;&#10074;"),
    ("sky_forward", "FORWARD", "&#9193;"),
    ("sky_record",  "RECORD",  "&#9679; Rec"),
    ("sky_red",     "RED",     "Red"),
    ("sky_green",   "GREEN",   "Green"),
    ("sky_yellow",  "YELLOW",  "Yellow"),
    ("sky_blue",    "BLUE",    "Blue"),
]
SKY_ACTIONS = {a: tok for a, tok, _ in SKY_BUTTONS}


# ── storage helpers ──────────────────────────────────────────────────
def _load(path, default):
    try:
        return pitv_state.load_named(path, default)
    except Exception:
        return default


def _save(path, data):
    pitv_state.save_named(path, data)


def load_guests():
    d = _load(GUEST_FILE, {})
    d.setdefault("meta", {})
    d.setdefault("guests", {})
    return d


def get_secret():
    key_name = "ADMIN_PORTAL_SECRET" if ADMIN else "GUEST_PORTAL_SECRET"
    try:
        saved = pitv_state.read_value(key_name, "")
        if saved:
            return bytes.fromhex(saved)
    except (ValueError, TypeError):
        pass
    secret = secrets.token_bytes(32)
    pitv_state.write_value(key_name, secret.hex())
    return secret


def guest_mode_on():
    try:
        with open(GUEST_MODE_FILE) as f:
            return f.read().strip().lower() == "on"
    except Exception:
        return False


def guest_pin(username):
    return _load(PIN_FILE, {}).get(username, "—")


# ── PIN rotation (weekly, guest mode only) ───────────────────────────
def _new_pin(used):
    used = set(used) | {pitv_secrets.emergency_code()}
    while True:
        p = f"{secrets.randbelow(1000000):06d}"
        if p not in used:
            return p


_last_rotate_check = 0.0
_rotate_lock       = threading.Lock()


def maybe_rotate():
    """Rotate every guest's player PIN once a week. Cheap to call often — it
    only touches disk once a minute unless a rotation is actually due. The lock
    stops two simultaneous requests from rotating twice and handing one guest a
    PIN that changes again a moment later."""
    global _last_rotate_check
    now = time.time()
    with _rotate_lock:
        if now - _last_rotate_check < 60:
            return
        _last_rotate_check = now
        _rotate_if_due()


def _rotate_if_due():
    data = load_guests()
    last = data["meta"].get("pin_rotated", 0)
    if data["guests"] and time.time() - last > ROTATE_PERIOD:
        pins = _load(PIN_FILE, {})
        for user in data["guests"]:
            pins[user] = _new_pin(pins.values())
        _save(PIN_FILE, pins)
        data["meta"]["pin_rotated"] = int(time.time())
        _save(GUEST_FILE, data)


# ── auth ─────────────────────────────────────────────────────────────
def _upgrade_guest(user, password):
    """Re-hash a legacy guest record with PBKDF2 now that we hold the
    plaintext. Keeps the salt-and-token structure; nobody has to re-register."""
    data = load_guests()
    rec  = data["guests"].get(user)
    if not rec:
        return
    rec.update(pitv_secrets.hash_password(password))
    data["guests"][user] = rec
    _save(GUEST_FILE, data)


def _upgrade_admin(user, password):
    admins = _load(ADMIN_FILE, {})
    rec = admins.get(user)
    if not rec:
        return
    rec.update(pitv_secrets.hash_password(password))
    admins[user] = rec
    _save(ADMIN_FILE, admins)


def check_password(password):
    """Guest login: password only -> matching guest username, or None.

    Every stored guest is checked even after a match so the reply doesn't leak,
    by how long it took, how far down the list the matching guest sits."""
    if not password:
        return None
    match = None
    for user, g in load_guests()["guests"].items():
        ok, stale = pitv_secrets.verify_password(g, password)
        if ok and match is None:
            match = (user, stale)
    if not match:
        return None
    user, stale = match
    if stale:
        _upgrade_guest(user, password)
    return user


def check_admin(username, password):
    """Admin login: username + password -> username, or None."""
    if not username or not password:
        return None
    rec = _load(ADMIN_FILE, {}).get(username)
    if not rec:
        return None
    ok, stale = pitv_secrets.verify_password(rec, password)
    if not ok:
        return None
    if stale:
        _upgrade_admin(username, password)
    return username


# ── login throttling ─────────────────────────────────────────────────
_fail_state = {}                  # ip -> {"fails": n, "until": ts, "seen": ts}
_fail_lock  = threading.Lock()
_last_pair_show = 0.0             # global cooldown on "show it on the TV"


def _throttle_until(ip):
    """Seconds the caller must wait, or 0 if they may try now."""
    now = time.time()
    with _fail_lock:
        st = _fail_state.get(ip)
        if not st:
            return 0
        if now - st["seen"] > FAIL_WINDOW:
            _fail_state.pop(ip, None)
            return 0
        return max(0, int(st["until"] - now))


def _note_failure(ip):
    now = time.time()
    with _fail_lock:
        st = _fail_state.get(ip)
        if not st or now - st["seen"] > FAIL_WINDOW:
            st = {"fails": 0, "until": 0.0, "seen": now}
        st["fails"] += 1
        st["seen"]   = now
        if st["fails"] >= MAX_FAILS:
            backoff = min(LOCK_BASE * 2 ** (st["fails"] - MAX_FAILS), LOCK_CEILING)
            st["until"] = now + backoff
        _fail_state[ip] = st
        # Don't let a busy network grow this without bound.
        if len(_fail_state) > 512:
            for old, v in list(_fail_state.items()):
                if now - v["seen"] > FAIL_WINDOW:
                    _fail_state.pop(old, None)


def _note_success(ip):
    with _fail_lock:
        _fail_state.pop(ip, None)


def valid_users():
    return (set(_load(ADMIN_FILE, {})) if ADMIN
            else set(load_guests()["guests"]))


def user_for_token(token):
    if not token:
        return None
    for user, g in load_guests()["guests"].items():
        if hmac.compare_digest(g.get("token", ""), token):
            return user
    return None


def sign_session(username, max_age=None):
    exp = int(time.time()) + (max_age or SESSION_MAX_AGE)
    payload = f"{username}|{exp}".encode()
    sig = hmac.new(get_secret(), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload).decode() + "." + sig


def verify_half(cookie_val):
    """The user who has passed step one and is waiting to type the code."""
    try:
        b64, sig = cookie_val.split(".", 1)
        payload = base64.urlsafe_b64decode(b64.encode())
        good = hmac.new(get_secret(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        user, exp = payload.decode().split("|")
        if int(exp) < time.time() or not user.startswith(HALF_PREFIX):
            return None
        user = user[len(HALF_PREFIX):]
        return user if user in valid_users() else None
    except Exception:
        return None


def verify_session(cookie_val):
    try:
        b64, sig = cookie_val.split(".", 1)
        payload = base64.urlsafe_b64decode(b64.encode())
        good = hmac.new(get_secret(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        user, exp = payload.decode().split("|")
        if int(exp) < time.time():
            return None
        if user.startswith(HALF_PREFIX):
            return None            # half-way through signing in: no control
        return user if user in valid_users() else None
    except Exception:
        return None


# ── actuation ────────────────────────────────────────────────────────
def send_udp(msg):
    """Send a SIGNED command to tv_menu. Unsigned datagrams are dropped at the
    far end, so a process without the shared key can't drive the TV even from
    on the Pi itself."""
    try:
        signed = pitv_secrets.sign_command(msg)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(signed.encode(), UDP_ADDR)
        s.close()
    except Exception:
        pass


# ── the connection password (a code shown on the TV) ─────────────────
def check_pairing(code):
    """True if this matches the code currently live on the TV. Whoever typed it
    could see the telly, which is the whole point — someone who has merely
    reached the network cannot. Guest Mode is the switch: tv_menu stops issuing
    a code when it is off."""
    if not code or not code.isdigit() or len(code) != 6:
        return False
    if not guest_mode_on():
        return False
    d = _load(PAIR_FILE, {})
    live = str(d.get("code", ""))
    if not live or d.get("expires", 0) < time.time():
        return False
    return hmac.compare_digest(code, live)


def guest_locked():
    """The admin Lock switch — guests stay signed in but can't press anything."""
    try:
        with open(GUEST_LOCK_FILE) as f:
            return f.read().strip().lower() == "on"
    except OSError:
        return False


def set_guest_lock(on):
    try:
        with open(GUEST_LOCK_FILE, "w") as f:
            f.write("on" if on else "off")
    except OSError:
        pass


def sky_on():
    """Is Sky control switched on? Only then does the portal show its panel."""
    try:
        return bool(pitv_state.load_section("SKY_SETTINGS", {}).get("enabled"))
    except Exception:
        return False


# ── network allowlist ────────────────────────────────────────────────
_allow_cache = (0.0, [])


def allowed_networks():
    """Parsed allowlist, re-read at most once a minute so `screen network`
    changes apply without restarting the portals."""
    global _allow_cache
    now = time.time()
    if now - _allow_cache[0] < 60:
        return _allow_cache[1]
    nets = []
    for entry in _load(NETWORK_FILE, {}).get("allow", []):
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            pass
    _allow_cache = (now, nets)
    return nets


def address_allowed(ip):
    nets = allowed_networks()
    if not nets:
        return True                       # no allowlist configured
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def write_fifo(token):
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (token + "\n").encode())
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


@contextmanager
def _keyboard_lease_guard():
    """Serialise lease access across the separate guest/admin HTTP processes."""
    os.makedirs(PITV_DIR, mode=0o700, exist_ok=True)
    fd = os.open(KEYBOARD_GUARD_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_keyboard_lease_locked(lease):
    """Write only while _keyboard_lease_guard() is held."""
    if lease is None:
        try:
            os.unlink(KEYBOARD_LEASE_PATH)
        except FileNotFoundError:
            pass
        return
    tmp = KEYBOARD_LEASE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lease, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, KEYBOARD_LEASE_PATH)
    os.chmod(KEYBOARD_LEASE_PATH, 0o600)


def game_keyboard_available():
    """True only while tv_menu has an external game running for keyboard input."""
    try:
        with open(GAME_STATE_FILE, "r", encoding="ascii") as f:
            return f.read().strip() == "on"
    except OSError:
        return False


def _read_keyboard_lease_locked():
    """Read/expire a lease only while _keyboard_lease_guard() is held."""
    try:
        with open(KEYBOARD_LEASE_PATH, encoding="utf-8") as f:
            lease = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(lease, dict):
        _write_keyboard_lease_locked(None)
        return None
    now = time.time()
    try:
        expired = float(lease.get("expires_at", 0)) <= now
    except (TypeError, ValueError):
        expired = True
    # If Guest Mode is turned off or guests are locked, a guest's lease should
    # not hold the shared keyboard until the timeout.
    guest_blocked = (not bool(lease.get("owner_admin"))
                     and (not guest_mode_on() or guest_locked()))
    game_ended = not game_keyboard_available()
    if expired or guest_blocked or game_ended:
        _write_keyboard_lease_locked(None)
        return None
    return lease


def keyboard_lease_status(session_id):
    with _keyboard_lease_guard():
        lease = _read_keyboard_lease_locked()
        if not lease:
            return {"keyboard_busy": False, "keyboard_mine": False,
                    "keyboard_expires_in": 0,
                    "keyboard_game_available": game_keyboard_available()}
        mine = bool(session_id and hmac.compare_digest(
            str(lease.get("session_hash", "")), str(session_id)))
        return {
            "keyboard_busy": True,
            "keyboard_mine": mine,
            "keyboard_expires_in": max(
                0, int(float(lease.get("expires_at", 0)) - time.time())),
            "keyboard_game_available": game_keyboard_available(),
        }


def pin_entry_state():
    """Read the current on-TV player-PIN prompt; never reads or stores the PIN."""
    try:
        with open(PIN_ENTRY_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {"active": False}
    except (OSError, ValueError, TypeError):
        return {"active": False}


@contextmanager
def _pin_lease_guard():
    """Serialise PIN-keypad claims across the separate guest/admin processes."""
    os.makedirs(PITV_DIR, mode=0o700, exist_ok=True)
    fd = os.open(PIN_GUARD_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_pin_lease_locked(lease):
    """Write only while _pin_lease_guard() is held."""
    if lease is None:
        try:
            os.unlink(PIN_LEASE_PATH)
        except FileNotFoundError:
            pass
        return
    tmp = PIN_LEASE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lease, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, PIN_LEASE_PATH)
    os.chmod(PIN_LEASE_PATH, 0o600)


def _read_pin_lease_locked(state=None):
    """Read and expire the exclusive PIN lease while the lease lock is held."""
    if state is None:
        state = pin_entry_state()
    try:
        with open(PIN_LEASE_PATH, "r", encoding="utf-8") as f:
            lease = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(lease, dict):
        _write_pin_lease_locked(None)
        return None
    try:
        expired = float(lease.get("expires_at", 0)) <= time.time()
    except (TypeError, ValueError):
        expired = True
    owner_admin = bool(lease.get("owner_admin"))
    guest_blocked = not owner_admin and (not guest_mode_on() or guest_locked())
    prompt_changed = (not state.get("active")
                      or not hmac.compare_digest(
                          str(lease.get("event_id", "")),
                          str(state.get("event_id", ""))))
    if expired or guest_blocked or prompt_changed:
        _write_pin_lease_locked(None)
        return None
    return lease


def pin_entry_status(session_id):
    """Public-safe prompt state plus whether this browser owns the keypad."""
    state = pin_entry_state()
    with _pin_lease_guard():
        lease = _read_pin_lease_locked(state)
    mine = bool(lease and session_id and hmac.compare_digest(
        str(lease.get("session_hash", "")), str(session_id)))
    try:
        started_at = float(state.get("started_at", 0) or 0)
    except (TypeError, ValueError):
        started_at = 0.0
    try:
        count = max(0, min(6, int(state.get("count", 0) or 0)))
    except (TypeError, ValueError):
        count = 0
    expires_in = 0
    if lease:
        try:
            expires_in = max(0, int(float(lease.get("expires_at", 0)) - time.time()))
        except (TypeError, ValueError):
            expires_in = 0
    return {
        "pin_entry_active": bool(state.get("active") and state.get("event_id")),
        "pin_prompt_id": str(state.get("event_id", "")),
        "pin_prompt_started_at": started_at,
        "pin_prompt_title": str(state.get("title", "PiTV")),
        "pin_prompt_player": int(state.get("player", 0) or 0),
        "pin_entry_count": count,
        "pin_entry_error": str(state.get("error", "")),
        "pin_busy": bool(lease),
        "pin_mine": mine,
        "pin_expires_in": expires_in,
    }


def set_guest_mode(on):
    """Admin toggle: write the state file and notify tv_menu (which keeps the
    Home switch in sync)."""
    try:
        with open(GUEST_MODE_FILE, "w") as f:
            f.write("on" if on else "off")
    except OSError:
        pass
    send_udp("GUEST_ON" if on else "GUEST_OFF")


# ── HTML ─────────────────────────────────────────────────────────────
PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>PiTV {who}</title><style>
*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,system-ui,sans-serif;
background:#0b0f1a;color:#e8ecf4;text-align:center}}
.wrap{{max-width:440px;margin:0 auto;padding:22px}}
h1{{font-size:22px;margin:.4em 0}}.muted{{color:#8b93a7;font-size:14px}}
.card{{background:#151b2b;border-radius:16px;padding:18px;margin:14px 0}}
button{{font-size:17px;padding:14px;border:0;border-radius:12px;background:#2a3350;
color:#fff;width:100%;margin:6px 0;cursor:pointer}}button:active{{background:#3a466e}}
input{{font-size:17px;padding:12px;width:100%;border-radius:10px;border:1px solid #2a3350;
background:#0b0f1a;color:#fff;margin:6px 0}}
.row{{display:flex;gap:8px}}.row button{{margin:0}}
.pad{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-width:260px;margin:0 auto}}
.skygrid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}
.skygrid button{{font-size:15px;padding:12px 4px;margin:0}}
.pad button{{aspect-ratio:1;font-size:20px}}.pad .sp{{visibility:hidden}}
.pin{{font-size:30px;letter-spacing:4px;font-weight:700;color:#7fd1ff}}
.on{{background:#1f7a3f}}.off{{background:#7a2a2a}}
.lockbar{{position:sticky;bottom:0;background:#7a2a2a;color:#fff;padding:10px;
border-radius:10px;margin:10px 0;font-size:15px}}
.modal{{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.82);display:flex;
align-items:center;justify-content:center;padding:16px}}
.modal[hidden]{{display:none!important}}
.modal-card{{width:100%;max-width:420px;max-height:90vh;overflow:auto;background:#151b2b;
border:1px solid #37415c;border-radius:16px;padding:18px;text-align:left;
box-shadow:0 20px 80px #000}}
.modal-card h2{{margin-top:0;text-align:center}}
.modal-card .muted{{line-height:1.45}}
.modal-card .row{{margin-top:8px}}
.pin-prompt{{position:fixed;z-index:90;left:12px;right:12px;bottom:12px;margin:auto;
max-width:440px;display:flex;align-items:center;gap:12px;background:#1b2942;
border:1px solid #7fd1ff;border-radius:16px;padding:12px;box-shadow:0 10px 35px #000b;
text-align:left}}
.pin-prompt[hidden]{{display:none!important}}
.pin-prompt-copy{{flex:1;min-width:0}}
.pin-prompt-copy strong{{display:block;font-size:16px}}
.pin-prompt-copy p{{margin:5px 0 0;font-size:13px}}
.pin-prompt button{{width:auto;min-width:105px;margin:0;padding:11px;font-size:14px}}
.pin-phone-pad{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-width:300px;margin:14px auto}}
.pin-phone-pad button{{aspect-ratio:1;font-size:23px;padding:8px;margin:0}}
.pin-entry-display{{font-size:28px;letter-spacing:7px;text-align:center;padding:12px;color:#7fd1ff;
background:#0b0f1a;border-radius:10px;min-height:54px}}
.pin-cancel{{background:#7a2a2a}}
@media(max-width:360px){{.pin-prompt{{align-items:stretch;flex-direction:column}}.pin-prompt button{{width:100%}}}}
body.locked .card button{{opacity:.35;pointer-events:none;filter:grayscale(1)}}
a{{color:#7fd1ff}}
</style></head><body><div class=wrap>{body}</div></body></html>"""


def page(body):
    return PAGE.format(body=body, who="Admin" if ADMIN else "Guest")


def login_body(err=""):
    e = f'<p style="color:#ff8a8a">{html.escape(err)}</p>' if err else ""
    userfield = ('<input name=username placeholder="Username" autofocus>'
                 if ADMIN else "")
    title = "PiTV Admin" if ADMIN else "PiTV Guest"
    hint = "Sign in." if ADMIN else "Enter your guest password."
    return (f"<h1>{title}</h1><p class=muted>{hint}</p>"
            f'<div class=card><form method=post action="/login">{e}{userfield}'
            f'<input type=password name=password placeholder="Password"'
            f'{"" if ADMIN else " autofocus"}>'
            f'<button type=submit>Sign in</button></form></div>')


def connect_body(username, err=""):
    """Step two: the password was right, now prove you can see the TV. The
    button turns into the box you type the answer into, so there is only ever
    one thing on screen to do next."""
    e = f'<p style="color:#ff8a8a">{html.escape(err)}</p>' if err else ""
    return (f"<h1>PiTV Guest</h1>"
            f"<p class=muted>Hello {html.escape(username)} — one more step.</p>"
            f"<div class=card>{e}"
            f'<p class=muted>The connection password appears on the TV.</p>'
            f'<button id=showbtn onclick="showcode()">'
            f'Show me the connection password</button>'
            f'<form id=codeform method=post action="/connect" '
            f'style="display:none">'
            f'<input name=code inputmode=numeric autocomplete=off maxlength=6 '
            f'placeholder="Connection password">'
            f'<button type=submit>Connect</button></form>'
            f'<noscript><style>#codeform{{display:block!important}}'
            f'#showbtn{{display:none}}</style>'
            f'<p class=muted>Ask the host to run: screen guest code show</p>'
            f'</noscript></div>'
            f'<p class=muted><a href="/logout">Start again</a></p>'
            f"""
<script>
function showcode(){{
  var b=document.getElementById('showbtn');
  b.disabled=true; b.textContent='Look at the TV…';
  fetch('/pair/show',{{method:'POST',headers:{{'Content-Type':
    'application/x-www-form-urlencoded'}},body:''}}).then(function(){{
    b.style.display='none';
    var f=document.getElementById('codeform');
    f.style.display='block'; f.querySelector('input').focus();
  }}).catch(function(){{ b.disabled=false;
    b.textContent='Show me the connection password'; }});
}}
</script>""")


def imposter_join_body(game_id):
    """The QR destination lets players re-enter their personal PIN."""
    state = pitv_imposter.read_state()
    if not state.get("active") or state.get("game_id") != game_id or not game_id:
        return ("<h1>Imposter</h1><div class=card><p>This game link has expired "
                "or the game has ended. Ask the host to start a new game.</p></div>")
    body = r"""<style>
.imposter-brand{font-weight:800;letter-spacing:2px;color:#7fd1ff;text-transform:uppercase}
.imposter-role{font-size:22px;font-weight:800;text-align:center;padding:18px 10px;border-radius:14px}
.imposter-role.bad{background:#542626;color:#ffd4d4}
.imposter-role.good{background:#123c2b;color:#c5ffe0}
.imposter-role.pick{background:#24395c;color:#d6e8ff}
.imposter-word{font-size:28px;font-weight:800;padding:22px 12px;border-radius:16px;background:#0b0f1a;text-align:center;word-break:break-word}
.imposter-list{display:grid;gap:8px;margin-top:12px}
.imposter-list button{text-align:left;margin:0;padding:12px 14px}
.imposter-list button.selected{outline:2px solid #7fd1ff;background:#234168}
.imposter-note{background:#1b2942;border-radius:12px;padding:12px;margin:12px 0}
.imposter-countdown{font-size:24px;font-weight:800;color:#7fd1ff;text-align:center}
</style>
<div class=imposter-brand>PiTV · Imposter</div>
<div class=card id=imposterAuth><h1>Join the game</h1>
  <p class=muted>Enter your personal six-digit PiTV player PIN to confirm who you are.</p>
  <form id=imposterPinForm>
    <input id=imposterPin type=password inputmode=numeric pattern="[0-9]{6}" maxlength=6 autocomplete=one-time-code placeholder="Six-digit player PIN" required>
    <button type=submit>Join game</button>
  </form><p class=muted id=imposterAuthError aria-live=polite></p>
</div>
<div id=imposterPlay hidden><div class=card><h1 id=imposterHeading>Imposter</h1>
<p class=muted id=imposterSubheading></p><div id=imposterContent></div></div></div>
<script>
(function(){
 var gameId=__GAME_ID__, storageKey='pitv-imposter-'+gameId;
 var token=sessionStorage.getItem(storageKey)||'';
 var authBox=document.getElementById('imposterAuth'),playBox=document.getElementById('imposterPlay');
 var content=document.getElementById('imposterContent'),heading=document.getElementById('imposterHeading');
 var sub=document.getElementById('imposterSubheading'),authError=document.getElementById('imposterAuthError');
 var lastRenderKey='',voteMessage='',wordSubmitting=false,turnSubmitting=false,polling=false,stateRevision=0;
 function el(tag,text,cls){var n=document.createElement(tag);if(text!==undefined&&text!==null)n.textContent=text;if(cls)n.className=cls;return n;}
 function api(path,fields){return fetch(path,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
  body:new URLSearchParams(Object.assign({game_id:gameId,token:token},fields||{}))});}
 function showAuth(msg){authBox.hidden=false;playBox.hidden=true;authError.textContent=msg||'';}
 function authenticate(e){
  e.preventDefault();var pin=document.getElementById('imposterPin').value;
  if(!/^[0-9]{6}$/.test(pin)){showAuth('Enter all six digits.');return;}
  authError.textContent='Checking PIN…';
  fetch('/imposter/auth',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
   body:new URLSearchParams({game_id:gameId,pin:pin})})
   .then(function(r){return r.json().then(function(j){return {r:r,j:j};});})
   .then(function(x){
    if(!x.r.ok){
     var msg={invalid_pin:'That PIN is not valid.',not_a_player:'That PIN is not one of the players selected for this game.',
      joining_closed:'The game has started and this player has not joined.',game_unavailable:'This game is no longer available.'};
     showAuth(msg[x.j.error]||'Could not join. Check your PIN and try again.');return;
    }
    token=x.j.token;sessionStorage.setItem(storageKey,token);lastRenderKey='';poll();
   }).catch(function(){showAuth('Could not contact PiTV. Check that you are on the same Wi-Fi.');});
 }
 function render(s){
  authBox.hidden=true;playBox.hidden=false;
  var phase=s.phase||'ended',role=s.role||'';
  heading.textContent='Imposter · Round '+(s.round_number||1)+'/'+(s.rounds_per_game||3);
  var signature=phase+'|'+role+'|'+(s.round_number||1)+'|'+(s.current_turn||'');
  if(signature!==lastRenderKey){
   lastRenderKey=signature;content.replaceChildren();sub.textContent='';
   if(phase==='join'){
    sub.textContent='Waiting for everyone to scan the QR code.';
    var p=el('p','Players connected: '+s.joined_count+' / '+s.player_count,'imposter-countdown');
    p.id='imposterJoinProgress';content.appendChild(p);
    var list=el('div',null,'imposter-list');
    s.players.forEach(function(n){list.appendChild(el('div',n,'imposter-note'));});content.appendChild(list);
   }else if(phase==='choose_word'){
    if(role==='imposter'){
     content.appendChild(el('div','YOU ARE THE IMPOSTER','imposter-role bad'));
     content.appendChild(el('p','You will not receive the word. Listen carefully and try to blend in.','imposter-note'));
    }else if(role==='chooser'){
     content.appendChild(el('div','YOU CHOOSE THE WORD','imposter-role pick'));
     content.appendChild(el('p','Choose one word, then tap Enter to show it to every non-imposter.','imposter-note'));
     var form=el('form');form.id='imposterWordForm';var inp=el('input');inp.id='imposterWord';inp.maxLength=40;inp.placeholder='Choose a word';inp.required=true;
     var btn=el('button','Enter');btn.type='submit';form.appendChild(inp);form.appendChild(btn);
     var wordStatus=el('p','','muted');wordStatus.id='imposterWordStatus';wordStatus.setAttribute('aria-live','polite');
     form.addEventListener('submit',function(e){
      e.preventDefault();
      if(wordSubmitting)return;
      var word=inp.value.trim();
      if(!word){wordStatus.textContent='Enter a word first.';inp.focus();return;}
      wordSubmitting=true;stateRevision++;btn.disabled=true;
      wordStatus.textContent='Sending word to PiTV…';
      api('/action',{action:'imposter_word',word:word})
       .then(function(r){return r.json().catch(function(){return {};}).then(function(j){return {r:r,j:j};});})
       .then(function(x){
        if(!x.r.ok||!x.j.ok){
         var messages={invalid_word:'Enter a word of 1–40 characters.',
          not_word_chooser:'Only the selected word chooser can submit the word.',
          invalid_game_session:'Your session expired. Reload this page and join again.',
          server_error:'PiTV received the word but encountered a server error. Check the PiTV guest service log.'};
         wordStatus.textContent=messages[x.j.error]||('PiTV could not accept the word (HTTP '+x.r.status+'). Try again.');
         wordSubmitting=false;btn.disabled=false;inp.focus();return;
        }
        wordStatus.textContent='Word accepted. Moving to the round…';
        wordSubmitting=false;stateRevision++;poll();
       })
       .catch(function(error){
        console.error('PiTV Imposter word POST failed:',error);
        wordStatus.textContent='The word request failed before PiTV returned a response: '+(error&&error.message?error.message:'network error')+'. Check the PiTV guest service log.';
        wordSubmitting=false;btn.disabled=false;
       });
     });
     content.appendChild(form);content.appendChild(wordStatus);
    }else{
     content.appendChild(el('div','YOU ARE A PLAYER','imposter-role good'));
     content.appendChild(el('p','Waiting for the word to be chosen. Keep your role secret.','imposter-note'));
    }
   }else if(phase==='round'){
    if(role==='imposter'){
     content.appendChild(el('div','YOU ARE THE IMPOSTER','imposter-role bad'));
     content.appendChild(el('p','You do not know the word. Try to blend in.','imposter-note'));
    }else{
     content.appendChild(el('p','YOUR WORD','muted'));
     content.appendChild(el('div',s.word||'Waiting for word…','imposter-word'));
    }
    if(s.current_turn && s.current_turn===s.you){
     content.appendChild(el('div',"IT'S YOUR TURN",'imposter-role pick'));
     content.appendChild(el('p','Give a clue without saying the exact word, then tap I’m done.','imposter-note'));
     var done=el('button','I’m done');done.type='button';done.id='imposterDone';
     var doneStatus=el('p','','muted');doneStatus.id='imposterDoneStatus';doneStatus.setAttribute('aria-live','polite');
     done.addEventListener('click',function(){
      if(turnSubmitting)return;
      turnSubmitting=true;stateRevision++;done.disabled=true;
      doneStatus.textContent='Moving to the next player…';
      api('/imposter/advance',{}).then(function(r){
       return r.json().catch(function(){return {};}).then(function(j){return {r:r,j:j};});
      }).then(function(x){
       if(!x.r.ok||!x.j.ok){
        var message={not_your_turn:'It is no longer your turn. Refreshing the game…',
         turn_not_active:'This turn has already ended. Refreshing the game…',
         invalid_game_session:'Your session expired. Reload this page and join again.'};
        doneStatus.textContent=message[x.j.error]||'PiTV could not advance the turn. Refreshing the game…';
        turnSubmitting=false;done.disabled=false;stateRevision++;poll();return;
       }
       doneStatus.textContent='Done. Moving on…';
       turnSubmitting=false;stateRevision++;poll();
      }).catch(function(error){
       console.error('PiTV Imposter advance POST failed:',error);
       doneStatus.textContent='Could not contact PiTV. Check your Wi-Fi and try again.';
       turnSubmitting=false;done.disabled=false;stateRevision++;
      });
     });
     content.appendChild(done);content.appendChild(doneStatus);
    }else{
     content.appendChild(el('p',(s.current_turn||'The next player')+' is giving a clue. Wait for your turn.','imposter-note'));
    }
   }else if(phase==='round_complete'){
    content.appendChild(el('div','ROUND COMPLETE','imposter-role pick'));
    content.appendChild(el('p','The host can start the vote now or continue for another clue round with the same roles and word.','imposter-note'));
   }else if(phase==='vote'){
    content.appendChild(el('p','Who do you think is the imposter? You may change your vote until time runs out.','imposter-note'));
    var cd=el('p','15 seconds left','imposter-countdown');cd.id='voteCountdown';content.appendChild(cd);
    var list=el('div',null,'imposter-list');list.id='imposterVoteList';
    s.players.forEach(function(n){
     var b=el('button',n);b.type='button';b.dataset.target=n;
     b.addEventListener('click',function(){
      api('/imposter/vote',{target:b.dataset.target}).then(function(r){return r.json().then(function(j){return {r:r,j:j};});})
       .then(function(x){if(!x.r.ok){voteMessage='Voting has closed.';poll();return;}
        voteMessage='Vote saved. You can change it before time runs out.';s.my_vote=b.dataset.target;updateVoteButtons(s);})
       .catch(function(){voteMessage='Could not submit vote.';});
     });list.appendChild(b);
    });content.appendChild(list);
    var status=el('p','','muted');status.id='imposterVoteStatus';content.appendChild(status);
   }else if(phase==='results'){
    content.appendChild(el('div','VOTE CLOSED','imposter-role pick'));
    content.appendChild(el('p','The results are being revealed on the TV.','imposter-note'));
   }else if(phase==='revealed'){
    content.appendChild(el('div','THE IMPOSTER WAS','imposter-role bad'));
    content.appendChild(el('div',s.imposter||'Unknown','imposter-word'));
    if(s.word)content.appendChild(el('p','The word was: '+s.word,'imposter-note'));
   }else content.appendChild(el('p','The game has ended.','imposter-note'));
  }
  if(phase==='join'){var p=document.getElementById('imposterJoinProgress');if(p)p.textContent='Players connected: '+s.joined_count+' / '+s.player_count;}
  updateVoteButtons(s);
 }
 function updateVoteButtons(s){
  var list=document.getElementById('imposterVoteList');
  if(list){
   Array.prototype.forEach.call(list.querySelectorAll('button'),function(b){b.classList.toggle('selected',b.dataset.target===s.my_vote);});
   var status=document.getElementById('imposterVoteStatus');
   if(status)status.textContent=voteMessage||(s.my_vote?'Your vote: '+s.my_vote+'. You can change it.':'Choose a player to submit your vote.');
   var cd=document.getElementById('voteCountdown');
   if(cd)cd.textContent=Math.max(0,Math.ceil((Number(s.vote_ends_at)||0)-Date.now()/1000))+' seconds left';
  }
 }
 function poll(){
  if(!token||polling||wordSubmitting||turnSubmitting)return;
  polling=true;
  var revision=stateRevision;
  api('/imposter/state',{}).then(function(r){
   if(!r.ok){token='';sessionStorage.removeItem(storageKey);showAuth('Your game session expired. Enter your PIN again.');return null;}
   return r.json();
  }).then(function(s){
   // Drop stale responses, especially polls that began before word submission.
   if(s&&!wordSubmitting&&revision===stateRevision)render(s);
  }).catch(function(){}).then(function(){polling=false;});
 }
 document.getElementById('imposterPinForm').addEventListener('submit',authenticate);
 if(token)poll();else showAuth('');
 setInterval(poll,1000);
})();
</script>""";
    return body.replace("__GAME_ID__", json.dumps(game_id))


def _imposter_players_for_pin(pin):
    """Require one unique six-digit player PIN; never accept the emergency PIN."""
    if not pin or not pin.isdigit() or len(pin) != 6 or pitv_secrets.check_emergency_code(pin):
        return []
    try: pin_map = pitv_state.load_section("PLAYER_PINS", {})
    except Exception: pin_map = {}
    matches = []
    if isinstance(pin_map, dict):
        for name, saved in pin_map.items():
            try: good = hmac.compare_digest(str(saved), pin)
            except (TypeError, ValueError): good = False
            if good: matches.append(str(name))
    return matches


def _imposter_api_post(self, path, body=None):
    if body is None:
        body = self._body()
    if body is None:
        self._json({"ok": False, "error": "bad_request"}, code=413)
        return
    game_id = body.get("game_id", "")
    if path == "/imposter/auth":
        ip = self._client_ip()
        wait = _throttle_until(ip)
        if wait:
            self._json({"ok": False, "error": "rate_limited", "retry_after": wait}, code=429)
            return
        matches = _imposter_players_for_pin(body.get("pin", ""))
        if len(matches) != 1:
            _note_failure(ip)
            self._json({"ok": False, "error": "invalid_pin"}, code=401)
            return
        result = pitv_imposter.authenticate_player(game_id, matches[0])
        if not result.get("ok"):
            self._json(result, code=403 if result.get("error") == "not_a_player" else 409)
            return
        _note_success(ip)
        self._json(result)
        return
    token = body.get("token", "")
    view = pitv_imposter.player_view(game_id, token)
    if view is None:
        self._json({"ok": False, "error": "invalid_game_session"}, code=401)
    elif path == "/imposter/state":
        self._json(view)
    elif path == "/imposter/word":
        raw = body.get("word", "")
        word = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", raw).split())
        if not word or len(word) > 40:
            self._json({"ok": False, "error": "invalid_word"}, code=400)
        else:
            try:
                result = pitv_imposter.choose_word(game_id, token, word)
            except Exception as exc:
                # Always return JSON instead of dropping the HTTP connection;
                # the phone can then show a server error rather than a network failure.
                print(
                    f"Imposter word submission failed: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                self._json({"ok": False, "error": "server_error"}, code=500)
                return
            if not result.get("ok"):
                print(
                    f"Imposter word submission refused: {result.get('error', 'unknown')}",
                    flush=True,
                )
            self._json(result, code=200 if result.get("ok") else 409)
    elif path == "/imposter/advance":
        result = pitv_imposter.advance_turn(game_id, token)
        self._json(result, code=200 if result.get("ok") else 409)
    elif path == "/imposter/vote":
        result = pitv_imposter.set_vote(game_id, token, body.get("target", ""))
        self._json(result, code=200 if result.get("ok") else 409)
    else:
        self._json({"ok": False, "error": "not_found"}, code=404)


def gate_body():
    return ("<h1>PiTV Guest</h1><div class=card>"
            "<p>Please ask the administrator to turn on <b>Guest Mode</b>.</p>"
            "</div>")


def controls_body(username):
    # Look the PIN up with the real name; escape only for display, so a name
    # with an & or < in it still resolves AND can't inject markup.
    pin  = html.escape(str(guest_pin(username)))
    name = html.escape(username)
    pin_card = (f'<div class=card><p class=muted>Your game PIN</p>'
                f'<div class="pin" id=pin>{pin}</div></div>') if pin != "—" else ""
    if ADMIN:
        gm = guest_mode_on()
        lk = guest_locked()
        head = (f"<h1>PiTV Admin</h1><p class=muted>Signed in as {name}</p>"
                + pin_card +
                f'<div class=card><p class=muted>Guest Mode is '
                f'<b id=gmstate>{"ON" if gm else "OFF"}</b></p><div class=row>'
                f'<button class=on onclick="act(\'guest_on\')">Guest Mode On</button>'
                f'<button class=off onclick="act(\'guest_off\')">Guest Mode Off</button>'
                f'</div>'
                f'<p class=muted style="margin-top:14px">Guest controls are '
                f'<b id=lkstate>{"LOCKED" if lk else "unlocked"}</b></p>'
                f'<div class=row>'
                f'<button class=off onclick="act(\'guest_lock\')">Lock</button>'
                f'<button class=on onclick="act(\'guest_unlock\')">Unlock</button>'
                f'</div></div>')
    else:
        head = (f"<h1>PiTV Guest</h1><p class=muted>Signed in as {name}</p>"
                + pin_card)
    sky = ""
    if sky_on():
        rows = "".join(
            f'<button onclick="act(\'{a}\')">{label}</button>'
            for a, _tok, label in SKY_BUTTONS)
        sky = ('<div class=card><p class=muted>Sky Q</p>'
               '<div class=skygrid>' + rows + '</div>'
               '<div class=row style="margin-top:8px">'
               '<button onclick="act(\'sky_power_on\')">Sky On</button>'
               '<button onclick="act(\'sky_power_off\')">Sky Standby</button>'
               '</div></div>')
    return (head + sky + """
<div class=card><div class=row>
  <button onclick="act('tv_on')">TV On</button>
  <button onclick="act('tv_off')">TV Off</button></div>
<div class=row style="margin-top:8px">
  <button onclick="act('input_sky')">Sky</button>
  <button onclick="act('input_pitv')">PiTV</button></div></div>
<div class=card><p class=muted>Navigate</p><div class=pad>
  <span class="sp"></span><button onclick="act('up')">&#9650;</button><span class="sp"></span>
  <button onclick="act('left')">&#9664;</button><button onclick="act('ok')">OK</button>
  <button onclick="act('right')">&#9654;</button>
  <button onclick="act('back')">Back</button><button onclick="act('down')">&#9660;</button>
  <span class="sp"></span></div></div>
<div class=card id=keyboardCard hidden><p class=muted>Type into a running PiTV game</p>
  <p class=muted>Use your phone's keyboard. Only one signed-in browser can type at a time.</p>
  <button onclick="openKeyboard()">Use phone keyboard</button></div>
<p class=muted><a href="/logout">Sign out</a></p>
<div id=lockbar class=lockbar hidden>The host has paused the controls.</div>
<div id=keyboardbar class=lockbar hidden>Someone else has exclusive TV input access. Navigation is temporarily paused.</div>
<div id=pinPrompt class=pin-prompt hidden role=status aria-live=polite>
  <div class=pin-prompt-copy><strong>PiTV is waiting for a PIN</strong>
    <p class=muted id=pinPromptText>Tap to enter the PIN on your phone.</p></div>
  <button id=pinPromptButton onclick="claimPinEntry()">Enter PIN</button>
</div>
<div class=modal id=pinModal hidden>
  <div class=modal-card role=dialog aria-modal=true aria-labelledby=pinTitle>
    <h2 id=pinTitle>Enter PIN on your phone</h2>
    <p class=muted id=pinStatus>Each digit is sent directly to the TV.</p>
    <div class=pin-entry-display id=pinEntryDisplay>_ _ _ _ _ _</div>
    <div class=pin-phone-pad>
      <button onclick="sendPinKey('1')">1</button><button onclick="sendPinKey('2')">2</button><button onclick="sendPinKey('3')">3</button>
      <button onclick="sendPinKey('4')">4</button><button onclick="sendPinKey('5')">5</button><button onclick="sendPinKey('6')">6</button>
      <button onclick="sendPinKey('7')">7</button><button onclick="sendPinKey('8')">8</button><button onclick="sendPinKey('9')">9</button>
      <button onclick="sendPinKey('DEL')">⌫</button><button onclick="sendPinKey('0')">0</button><button onclick="sendPinKey('OK')">OK</button>
    </div>
    <button class=pin-cancel onclick="releasePinEntry()">Cancel and release keypad</button>
  </div>
</div>
<div class=modal id=keyboardModal hidden>
  <div class=modal-card role=dialog aria-modal=true aria-labelledby=keyboardTitle>
    <h2 id=keyboardTitle>Type into PiTV</h2>
    <p class=muted id=keyboardStatus>Would you like to use your phone's keyboard to type into the currently running PiTV game?</p>
    <div id=keyboardConfirm>
      <p class=muted>Only one person can hold keyboard access. Other users' keyboard and navigation input will be rejected until it is released or expires after inactivity.</p>
      <button id=keyboardClaim onclick="claimKeyboard()">Yes, use keyboard</button>
    </div>
    <div id=keyboardEntry hidden>
      <input id=keyboardText type=text maxlength=120 autocomplete=off autocapitalize=off spellcheck=false
        placeholder="Type letters, numbers or punctuation"
        onkeydown="if(event.key==='Enter'){event.preventDefault();sendKeyboardText()}">
      <div class=row><button onclick="sendKeyboardText()">Send text</button><button onclick="sendKeyboardKey('SPACE')">Space</button></div>
      <div class=row><button onclick="sendKeyboardKey('ENTER')">Enter</button><button onclick="sendKeyboardKey('BKSP')">Backspace</button></div>
      <div class=row><button onclick="sendKeyboardKey('TAB')">Tab</button><button onclick="sendKeyboardKey('ESC')">Escape</button></div>
      <button onclick="releaseKeyboard()">Done — release access</button>
    </div>
    <button onclick="closeKeyboard()">Close</button>
  </div>
</div>
<script>
var ADMIN = %ADMIN%, POLL = %POLL%;
var keyboardToken = "";
var pinToken = "", pinEventId = "";
var pinSendQueue = Promise.resolve();
var pinEntryCount = 0, pinLastError = "";
var pinPromptSeen = "", pinPromptDismissed = "";
var pinPromptTimer = null;
function act(a){
  if(document.body.classList.contains('locked') && !ADMIN) return;
  fetch('/action',{method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams({action:a})}).then(function(r){
      if(r.status===423){sync();}
    });
}
function pinLabel(s){
  var title = s.pin_prompt_title || 'PiTV';
  return s.pin_prompt_player ? title + ' — Player ' + s.pin_prompt_player : title;
}
function dismissPinPrompt(){
  pinPromptDismissed = pinPromptSeen;
  var prompt = document.getElementById('pinPrompt');
  if(prompt) prompt.hidden = true;
}
function renderPinPrompt(s){
  var prompt = document.getElementById('pinPrompt');
  var message = document.getElementById('pinPromptText');
  var button = document.getElementById('pinPromptButton');
  if(!prompt || !message || !button) return;
  var now = Date.now() / 1000;
  if(s.pin_prompt_id && s.pin_prompt_id !== pinPromptSeen){
    pinPromptSeen = s.pin_prompt_id;
    pinPromptDismissed = "";
    if(pinPromptTimer) clearTimeout(pinPromptTimer);
    var eventId = pinPromptSeen;
    var deadline = (Number(s.pin_prompt_started_at) || now) + 10;
    pinPromptTimer = setTimeout(function(){
      if(pinPromptSeen === eventId && !pinToken) prompt.hidden = true;
    }, Math.max(0, (deadline - now) * 1000));
  }
  var deadline = (Number(s.pin_prompt_started_at) || 0) + 10;
  var inWindow = !!s.pin_entry_active && !!s.pin_prompt_id
      && now >= (Number(s.pin_prompt_started_at) || 0) - 1 && now < deadline;
  var visible = inWindow && pinPromptDismissed !== s.pin_prompt_id && !pinToken;
  prompt.hidden = !visible;
  if(!s.pin_entry_active){
    prompt.hidden = true;
    if(pinToken){
      pinToken = ""; pinEventId = ""; pinEntryCount = 0; pinLastError = "";
      document.getElementById('pinModal').hidden = true;
    }
    return;
  }
  if(s.pin_busy && !s.pin_mine){
    message.textContent = 'Someone else claimed the keypad first. Their PIN entry has priority.';
    button.textContent = 'Keypad in use';
    button.disabled = true;
  } else {
    message.textContent = 'Enter the PIN for ' + pinLabel(s) + '. First tap claims the keypad for you.';
    button.textContent = 'Enter PIN';
    button.disabled = false;
  }
  if(pinToken && pinEventId !== s.pin_prompt_id){
    pinToken = ""; pinEventId = ""; pinEntryCount = 0; pinLastError = "";
    document.getElementById('pinModal').hidden = true;
  }
}
function renderPinEntry(){
  var display = document.getElementById('pinEntryDisplay');
  if(display) display.textContent = Array.from({length:6},function(_,i){
    return i < pinEntryCount ? '•' : '_';
  }).join(' ');
}
function claimPinEntry(){
  if(!state || !state.pin_entry_active || !state.pin_prompt_id) return;
  var button = document.getElementById('pinPromptButton');
  button.disabled = true;
  fetch('/pin/claim',{method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams({event_id:state.pin_prompt_id})})
    .then(function(r){return r.json().catch(function(){return {};}).then(function(j){return {r:r,j:j};});})
    .then(function(result){
      if(!result.r.ok){
        if(state){state.pin_busy=!!result.j.pin_busy;state.pin_mine=false;}
        renderPinPrompt(state || {});
        var msg = result.r.status===409
          ? 'Someone else claimed the keypad first, or the PIN prompt has ended.'
          : result.r.status===423
            ? 'Guest controls are locked by the administrator.'
            : 'Could not claim the TV keypad. Try again.';
        document.getElementById('pinPromptText').textContent=msg;
        sync();
        return;
      }
      pinToken=result.j.token||'';
      pinEventId=result.j.event_id||state.pin_prompt_id;
      pinEntryCount=Number(result.j.count)||0;
      pinLastError=(state && state.pin_entry_error)||'';
      pinPromptDismissed=pinEventId;
      document.getElementById('pinPrompt').hidden=true;
      document.getElementById('pinModal').hidden=false;
      document.getElementById('pinTitle').textContent='Enter PIN — '+pinLabel(state);
      document.getElementById('pinStatus').textContent='Each digit is sent immediately to the TV.';
      renderPinEntry();
    }).catch(function(){
      button.disabled=false;
      document.getElementById('pinPromptText').textContent='Could not contact PiTV. Try again.';
    });
}
function sendPinKey(key){
  if(!pinToken) return;
  if(key.length===1 && key >= '0' && key <= '9'){
    if(pinEntryCount >= 6) return;
    pinEntryCount=Math.min(6,pinEntryCount+1);
  } else if(key==='DEL'){
    pinEntryCount=Math.max(0,pinEntryCount-1);
  }
  renderPinEntry();
  document.getElementById('pinStatus').textContent='Sending input to PiTV…';
  var token=pinToken, eventId=pinEventId;
  pinSendQueue=pinSendQueue.then(function(){
    if(!token || token!==pinToken || eventId!==pinEventId) return;
    return fetch('/pin/input',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams({token:token,event_id:eventId,key:key})})
      .then(function(r){
        if(!r.ok){
          pinToken=''; pinEventId=''; document.getElementById('pinModal').hidden=true;
          document.getElementById('pinStatus').textContent='PIN entry ended or keypad access was lost.';
          sync(); return;
        }
        document.getElementById('pinStatus').textContent='Sent to TV.';
      });
  }).catch(function(){
    document.getElementById('pinStatus').textContent='Could not contact PiTV.';
  });
}
function releasePinEntry(){
  var token=pinToken, eventId=pinEventId;
  pinToken=''; pinEventId=''; pinEntryCount=0; pinLastError='';
  document.getElementById('pinModal').hidden=true;
  pinPromptDismissed="";
  if(token){
    pinSendQueue=pinSendQueue.catch(function(){}).then(function(){
      return fetch('/pin/release',{method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},
        body:new URLSearchParams({token:token,event_id:eventId})});
    }).then(function(){sync();}).catch(function(){sync();});
  } else sync();
}
function updatePinEntry(s){
  if(pinToken && (!s.pin_entry_active || s.pin_prompt_id!==pinEventId)){
    pinToken=''; pinEventId=''; pinEntryCount=0; pinLastError='';
    document.getElementById('pinModal').hidden=true;
    pinSendQueue=Promise.resolve();
  } else if(pinToken && s.pin_prompt_id===pinEventId
            && s.pin_entry_error && s.pin_entry_error!==pinLastError){
    pinLastError=s.pin_entry_error;
    pinEntryCount=Number(s.pin_entry_count)||0;
    document.getElementById('pinStatus').textContent=s.pin_entry_error;
    renderPinEntry();
  }
  renderPinPrompt(s);
}
(function enablePinPromptSwipe(){
  var p=document.getElementById('pinPrompt'),x=0,y=0;
  if(!p)return;
  p.addEventListener('touchstart',function(e){if(e.changedTouches&&e.changedTouches.length){x=e.changedTouches[0].clientX;y=e.changedTouches[0].clientY;}},{passive:true});
  p.addEventListener('touchend',function(e){
    if(!e.changedTouches||!e.changedTouches.length)return;
    var dx=e.changedTouches[0].clientX-x,dy=e.changedTouches[0].clientY-y;
    if(dx < -70 && Math.abs(dx)>Math.abs(dy)*1.25)dismissPinPrompt();
  },{passive:true});
  p.addEventListener('pointerdown',function(e){x=e.clientX;y=e.clientY;});
  p.addEventListener('pointerup',function(e){var dx=e.clientX-x,dy=e.clientY-y;if(dx < -70 && Math.abs(dx)>Math.abs(dy)*1.25)dismissPinPrompt();});
})();
function renderKeyboardModal(){
  var modal=document.getElementById('keyboardModal');
  if(!modal || modal.hidden) return;
  var status=document.getElementById('keyboardStatus');
  var confirmBox=document.getElementById('keyboardConfirm');
  var entryBox=document.getElementById('keyboardEntry');
  var claim=document.getElementById('keyboardClaim');
  if(keyboardToken){
    status.textContent='You have exclusive access. Make sure a PiTV game that accepts keyboard input is running, then type and send your text.';
    confirmBox.hidden=true; entryBox.hidden=false; claim.disabled=false;
  } else if(state && state.keyboard_busy && !state.keyboard_mine){
    status.textContent='Someone else is using the TV keyboard. You cannot type or navigate while they hold it. Try again when they finish.';
    confirmBox.hidden=false; entryBox.hidden=true; claim.disabled=true;
  } else if(state && !state.keyboard_game_available){
    status.textContent='Start a PiTV game that accepts keyboard input first. The phone keyboard is for game text entry, not menu navigation.';
    confirmBox.hidden=false; entryBox.hidden=true; claim.disabled=true;
  } else {
    status.textContent='Would you like to use your phone keyboard to type into the currently running PiTV game?';
    confirmBox.hidden=false; entryBox.hidden=true; claim.disabled=false;
  }
}
function openKeyboard(){
  document.getElementById('keyboardModal').hidden=false;
  renderKeyboardModal();
  sync();
}
function claimKeyboard(){
  var claim=document.getElementById('keyboardClaim');
  claim.disabled=true;
  document.getElementById('keyboardStatus').textContent='Requesting exclusive keyboard access…';
  fetch('/keyboard/claim',{method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},body:''})
    .then(function(r){return r.json().catch(function(){return {}}).then(function(j){return {r:r,j:j};});})
    .then(function(result){
      if(!result.r.ok){
        if(state){
          state.keyboard_busy=!!result.j.keyboard_busy;
          state.keyboard_mine=false;
          if(typeof result.j.keyboard_game_available==='boolean'){
            state.keyboard_game_available=result.j.keyboard_game_available;
          }
        }
        renderKeyboardModal();
        if(result.r.status===423){
          document.getElementById('keyboardStatus').textContent='Guest controls are currently locked by the administrator.';
        } else if(result.j.error==='no_running_external_game'){
          document.getElementById('keyboardStatus').textContent='Start a PiTV game that accepts keyboard input, then claim the keyboard.';
        } else if(result.r.status===409){
          document.getElementById('keyboardStatus').textContent='Someone else claimed the keyboard first. Their input has priority; try again when it is free.';
        } else {
          document.getElementById('keyboardStatus').textContent='Could not claim keyboard access. Please try again.';
        }
        return;
      }
      keyboardToken=result.j.token||'';
      if(state){state.keyboard_busy=true;state.keyboard_mine=true;}
      renderKeyboardModal();
      var field=document.getElementById('keyboardText');
      field.value='';
      field.focus(); // opens the phone's normal on-screen keyboard
    }).catch(function(){
      claim.disabled=false;
      document.getElementById('keyboardStatus').textContent='Could not contact PiTV. Please try again.';
    });
}
function keyboardRequest(fields){
  fields.token=keyboardToken;
  return fetch('/keyboard/type',{method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams(fields)});
}
function keyboardRequestFailed(r){
  if(r.status===409 || r.status===423){
    keyboardToken='';
    sync();
    document.getElementById('keyboardStatus').textContent=
      r.status===423 ? 'Guest controls are locked.' :
      'Keyboard access expired, another user owns it, or the game has ended.';
    renderKeyboardModal();
  } else {
    document.getElementById('keyboardStatus').textContent='Could not send input. Check that the game is still running.';
  }
}
function sendKeyboardText(){
  var field=document.getElementById('keyboardText');
  var value=field.value;
  if(!value) return;
  keyboardRequest({text:value}).then(function(r){
    if(!r.ok){keyboardRequestFailed(r);return;}
    field.value='';field.focus();
  }).catch(function(){
    document.getElementById('keyboardStatus').textContent='Could not contact PiTV.';
  });
}
function sendKeyboardKey(key){
  keyboardRequest({key:key}).then(function(r){
    if(!r.ok){keyboardRequestFailed(r);return;}
    document.getElementById('keyboardText').focus();
  }).catch(function(){
    document.getElementById('keyboardStatus').textContent='Could not contact PiTV.';
  });
}
function releaseKeyboard(){
  var token=keyboardToken;
  keyboardToken='';
  if(state){state.keyboard_busy=false;state.keyboard_mine=false;}
  document.getElementById('keyboardModal').hidden=true;
  if(token){
    fetch('/keyboard/release',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams({token:token})}).then(function(){sync();}).catch(function(){sync();});
  } else {
    sync();
  }
}
function closeKeyboard(){releaseKeyboard();}
// Poll so a kick, a lock, Guest Mode going off or a rotated PIN lands within
// seconds — nobody has to know to pull-to-refresh.
var state = null;
function apply(s){
  if(state && (s.sky !== state.sky || s.guest_mode !== state.guest_mode)){
    location.reload(); return;              // the page shape changed
  }
  state = s;
  document.body.classList.toggle('locked', !!s.locked && !ADMIN);
  var bar = document.getElementById('lockbar');
  if(bar) bar.hidden = !(s.locked && !ADMIN);
  var pin = document.getElementById('pin');
  if(pin && s.pin && pin.textContent !== s.pin) pin.textContent = s.pin;
  var gm = document.getElementById('gmstate');
  if(gm) gm.textContent = s.guest_mode ? 'ON' : 'OFF';
  var lk = document.getElementById('lkstate');
  if(lk) lk.textContent = s.locked ? 'LOCKED' : 'unlocked';
  var kb = document.getElementById('keyboardbar');
  if(kb) kb.hidden = !(s.keyboard_busy && !s.keyboard_mine);
  var kbCard = document.getElementById('keyboardCard');
  if(kbCard) kbCard.hidden = !s.keyboard_game_available;
  updatePinEntry(s);
  renderKeyboardModal();
}
function sync(){
  fetch('/state',{headers:{'Accept':'application/json'}}).then(function(r){
    if(r.status === 401){ location.href = '/'; return null; }   // kicked
    return r.json();
  }).then(function(s){ if(s) apply(s); }).catch(function(){});
}
sync(); setInterval(sync, POLL*1000);
document.addEventListener('visibilitychange', function(){
  if(!document.hidden) sync();              // straight back on re-open
});
</script>"""
            .replace("%ADMIN%", "true" if ADMIN else "false")
            .replace("%POLL%", str(POLL_SECONDS)))


# ── HTTP handler ─────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "PiTV"

    def _security_headers(self):
        # The pages use inline styles/handlers, so the CSP allows inline but
        # nothing external and no framing.
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _html(self, body, code=200, cookie=None):
        data = page(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location, cookie=None):
        # Use POST/Redirect/GET and provide a real HTML fallback for browsers
        # that do not handle an empty redirect response correctly.
        status = 303 if self.command == "POST" else 302
        safe_location = html.escape(location, quote=True)
        data = (
            "<!doctype html><html><head><meta charset=utf-8>"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="0;url={safe_location}">'
            "</head><body>"
            f'<p>Continuing to <a href="{safe_location}">PiTV</a>.</p>'
            "</body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Location", location)
        self._security_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _client_ip(self):
        # Direct LAN connections only — no proxy, so no XFF to trust.
        try:
            return self.client_address[0]
        except Exception:
            return "?"

    def _same_origin(self):
        """Check the request origin when the browser provides one.

        Some browsers or embedded web views send Origin: null. Treat that
        like an omitted Origin, which this local portal already permits for
        browser compatibility. Normal Origin values must still match Host.
        """
        origin = self.headers.get("Origin")
        if not origin or origin.strip().lower() == "null":
            return True

        host = (self.headers.get("Host") or "").strip()
        parsed = urlparse(origin)
        return bool(host and parsed.netloc.lower() == host.lower())

    def _session_user(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE_NAME in c:
            return verify_session(c[COOKIE_NAME].value)
        return None

    def _pending_user(self):
        """Someone who has passed step one and owes us the code off the TV."""
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE_NAME in c:
            return verify_half(c[COOKIE_NAME].value)
        return None

    def _keyboard_session_id(self):
        """Opaque identity for this browser session; never store the cookie itself."""
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE_NAME not in c:
            return None
        return hashlib.sha256(
            (COOKIE_NAME + ":" + c[COOKIE_NAME].value).encode("utf-8")
        ).hexdigest()

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _cookie(value, age):
        return (f"{COOKIE_NAME}={value}; Path=/; Max-Age={age}; "
                f"HttpOnly; SameSite=Lax")

    def _body(self):
        """Parse a form body, refusing anything oversized so a bogus
        Content-Length can't make us allocate arbitrary memory."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/imposter/join":
            game_id = parse_qs(urlparse(self.path).query).get("g", [""])[0]
            self._html(imposter_join_body(game_id))
            return
        if path == "/nfc" and not ADMIN:
            # An NFC tag is a bearer credential, so guessing it is throttled the
            # same way a password is.
            ip = self._client_ip()
            if _throttle_until(ip):
                self._redirect("/")
                return
            qs = parse_qs(urlparse(self.path).query)
            user = user_for_token(qs.get("t", [""])[0])
            if user and guest_mode_on():
                _note_success(ip)
                self._redirect("/", self._cookie(sign_session(user), SESSION_MAX_AGE))
            else:
                _note_failure(ip)
                self._redirect("/")
            return
        if path == "/logout":
            self._redirect("/", f"{COOKIE_NAME}=; Path=/; Max-Age=0")
            return
        if path == "/login":
            # A GET to /login can happen when a browser repeats a form URL.
            # Return the browser to the actual page rather than a blank 404.
            self._redirect("/")
            return
        if path == "/state":
            # What every open page polls. 401 is the signal to go back to the
            # login screen, which is how a kick lands in seconds.
            user = self._session_user()
            if not user or (not ADMIN and not guest_mode_on()):
                self._json({"ok": False}, code=401)
                return
            keyboard = keyboard_lease_status(self._keyboard_session_id())
            self._json({
                "ok":         True,
                "guest_mode": guest_mode_on(),
                "locked":     guest_locked(),
                "sky":        sky_on(),
                "pin":        str(guest_pin(user)),
                **keyboard,
                **pin_entry_status(self._keyboard_session_id()),
            })
            return
        if path != "/":
            self.send_response(404); self.end_headers(); return

        if not ADMIN and not guest_mode_on():
            self._html(gate_body()); return
        if not ADMIN:
            # Weekly PIN rotation used to be checked only at startup, so on a Pi
            # that never restarts it never actually happened. Check it here, at
            # most once a minute, so the schedule is real.
            maybe_rotate()
        user = self._session_user()
        if user:
            self._html(controls_body(user))
        elif not ADMIN and self._pending_user():
            self._html(connect_body(self._pending_user()))
        else:
            self._html(login_body())

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._same_origin():
            print(
                f"Rejected cross-origin POST to {path}: "
                f"Host={self.headers.get('Host')!r}, "
                f"Origin={self.headers.get('Origin')!r}",
                flush=True,
            )
            self._html(
                login_body(
                    "This request was rejected because its origin did not "
                    "match the page address. Reload PiTV using the same "
                    "address and try again."
                ),
                code=403,
            )
            return
        # Parse /action exactly once. Imposter's QR join page uses the same
        # form-encoded POST mechanism as guest controls, but authenticates with
        # its game-scoped bearer token rather than a guest-login cookie.
        action_body = None
        if path == "/action":
            action_body = self._body()
            if action_body is None:
                self._json({"ok": False, "error": "bad_request"}, code=413)
                return
            if action_body.get("action", "") == "imposter_word":
                _imposter_api_post(self, "/imposter/word", action_body)
                return

        # Let PIN endpoints return authenticated JSON errors if Guest Mode
        # has just been switched off, rather than a misleading HTML 200.
        if (not ADMIN and not guest_mode_on()
                and not path.startswith("/pin/")
                and not path.startswith("/imposter/")):
            self._html(gate_body()); return

        if path.startswith("/imposter/"):
            _imposter_api_post(self, path)
            return

        if path == "/login":
            ip = self._client_ip()
            wait = _throttle_until(ip)
            if wait:
                self._html(login_body(
                    f"Too many attempts — try again in {wait} s."), code=429)
                return
            b = self._body()
            if b is None:
                self.send_response(413); self.end_headers(); return
            secret = b.get("password", "")
            if ADMIN:
                user = check_admin(b.get("username", ""), secret)
                if user:
                    _note_success(ip)
                    self._redirect("/", self._cookie(
                        sign_session(user), SESSION_MAX_AGE))
                    return
            else:
                user = check_password(secret)
                if user:
                    # Step one done. The half-session controls nothing; it only
                    # lets them ask the TV for the connection password.
                    _note_success(ip)
                    self._html(connect_body(user), cookie=self._cookie(
                        sign_session(HALF_PREFIX + user, HALF_AGE), HALF_AGE))
                    return
            _note_failure(ip)
            self._html(login_body("Wrong login." if ADMIN
                                  else "That password wasn't right."), code=401)
            return

        if path == "/connect" and not ADMIN:
            # Step two: the connection password from the TV screen.
            pending = self._pending_user()
            if not pending:
                self._html(login_body("Please sign in again."), code=401)
                return
            ip   = self._client_ip()
            wait = _throttle_until(ip)
            if wait:
                self._html(connect_body(pending,
                    f"Too many tries — wait {wait} s."), code=429)
                return
            b = self._body()
            if b is None:
                self.send_response(413); self.end_headers(); return
            if check_pairing(b.get("code", "")):
                _note_success(ip)
                print(f"connected {pending} from {ip}", flush=True)
                self._redirect("/", self._cookie(
                    sign_session(pending), SESSION_MAX_AGE))
            else:
                _note_failure(ip)
                self._html(connect_body(pending,
                    "That wasn't the connection password on the TV."), code=401)
            return

        if path == "/pair/show" and not ADMIN:
            # Only for someone who has already given the right guest password,
            # so nobody on the network can sit there flashing the TV.
            if not self._pending_user():
                self.send_response(403); self.end_headers(); return
            global _last_pair_show
            now = time.time()
            if now - _last_pair_show >= PAIR_SHOW_COOL:
                _last_pair_show = now
                write_fifo("PAIR_SHOW")
            self.send_response(204); self.end_headers()
            return

        if path.startswith("/pin/"):
            user = self._session_user()
            if not user or (not ADMIN and not guest_mode_on()):
                self._json({"ok": False, "error": "not_signed_in"}, code=401)
                return
            if not ADMIN and guest_locked():
                self._json({"ok": False, "error": "guest_controls_locked"}, code=423)
                return
            session_id = self._keyboard_session_id()
            if not session_id:
                self._json({"ok": False, "error": "no_session"}, code=401)
                return

            if path == "/pin/claim":
                b = self._body()
                if b is None:
                    self._json({"ok": False, "error": "bad_request"}, code=413)
                    return
                event_id = b.get("event_id", "")
                state = pin_entry_state()
                if not state.get("active") or not hmac.compare_digest(
                        str(state.get("event_id", "")), event_id):
                    self._json({"ok": False, "error": "pin_prompt_ended",
                                "pin_busy": False}, code=409)
                    return
                with _pin_lease_guard():
                    state = pin_entry_state()
                    if not state.get("active") or not hmac.compare_digest(
                            str(state.get("event_id", "")), event_id):
                        self._json({"ok": False, "error": "pin_prompt_ended",
                                    "pin_busy": False}, code=409)
                        return
                    lease = _read_pin_lease_locked(state)
                    if lease and not hmac.compare_digest(
                            str(lease.get("session_hash", "")), session_id):
                        self._json({"ok": False, "error": "pin_keypad_in_use",
                                    "pin_busy": True}, code=409)
                        return
                    token = (lease.get("token") if lease else None) or secrets.token_urlsafe(24)
                    lease = {
                        "session_hash": session_id,
                        "event_id": event_id,
                        "token": token,
                        "owner_admin": bool(ADMIN),
                        "expires_at": time.time() + PIN_LEASE_SECONDS,
                    }
                    _write_pin_lease_locked(lease)
                try:
                    count = max(0, min(6, int(state.get("count", 0) or 0)))
                except (TypeError, ValueError):
                    count = 0
                self._json({"ok": True, "token": token, "event_id": event_id,
                            "expires_in": PIN_LEASE_SECONDS, "count": count})
                return

            b = self._body()
            if b is None:
                self._json({"ok": False, "error": "bad_request"}, code=413)
                return
            token = b.get("token", "")
            event_id = b.get("event_id", "")
            if path == "/pin/release":
                with _pin_lease_guard():
                    lease = _read_pin_lease_locked()
                    if (lease
                            and hmac.compare_digest(str(lease.get("session_hash", "")), session_id)
                            and hmac.compare_digest(str(lease.get("token", "")), token)
                            and hmac.compare_digest(str(lease.get("event_id", "")), event_id)):
                        _write_pin_lease_locked(None)
                self.send_response(204); self.end_headers()
                return

            if path == "/pin/input":
                key = b.get("key", "").upper()
                if not (key in {"DEL", "OK"} or (len(key) == 1 and key in "0123456789")):
                    self._json({"ok": False, "error": "unsupported_key"}, code=400)
                    return
                with _pin_lease_guard():
                    state = pin_entry_state()
                    lease = _read_pin_lease_locked(state)
                    valid = bool(
                        lease and state.get("active")
                        and hmac.compare_digest(str(lease.get("session_hash", "")), session_id)
                        and hmac.compare_digest(str(lease.get("token", "")), token)
                        and hmac.compare_digest(str(lease.get("event_id", "")), event_id)
                        and hmac.compare_digest(str(state.get("event_id", "")), event_id)
                    )
                    if not valid:
                        self._json({"ok": False, "error": "pin_lease_expired_or_prompt_ended",
                                    "pin_busy": bool(lease)}, code=409)
                        return
                    lease["expires_at"] = time.time() + PIN_LEASE_SECONDS
                    _write_pin_lease_locked(lease)
                    sent = write_fifo(f"PINKEY {token} {event_id} {key}")
                if not sent:
                    self._json({"ok": False, "error": "pitv_menu_unavailable"}, code=503)
                else:
                    self.send_response(204); self.end_headers()
                return

            self._json({"ok": False, "error": "not_found"}, code=404)
            return

        if path.startswith("/keyboard/"):
            user = self._session_user()
            if not user or (not ADMIN and not guest_mode_on()):
                self._json({"ok": False, "error": "not_signed_in"}, code=401)
                return
            if not ADMIN and guest_locked():
                self._json({"ok": False, "error": "guest_controls_locked"}, code=423)
                return
            session_id = self._keyboard_session_id()
            if not session_id:
                self._json({"ok": False, "error": "no_session"}, code=401)
                return

            if path == "/keyboard/claim":
                if not game_keyboard_available():
                    self._json({
                        "ok": False, "error": "no_running_external_game",
                        "keyboard_busy": False, "keyboard_mine": False,
                        "keyboard_game_available": False,
                    }, code=409)
                    return
                with _keyboard_lease_guard():
                    lease = _read_keyboard_lease_locked()
                    if lease and not hmac.compare_digest(
                            str(lease.get("session_hash", "")), session_id):
                        remaining = max(0, int(float(lease.get("expires_at", 0)) - time.time()))
                        self._json({
                            "ok": False, "keyboard_busy": True,
                            "keyboard_mine": False, "keyboard_expires_in": remaining,
                        }, code=409)
                        return
                    token = (lease.get("token") if lease else None) or secrets.token_urlsafe(24)
                    lease = {
                        "session_hash": session_id,
                        "token": token,
                        "owner_admin": bool(ADMIN),
                        "expires_at": time.time() + KEYBOARD_LEASE_SECONDS,
                    }
                    _write_keyboard_lease_locked(lease)
                self._json({"ok": True, "token": token,
                            "expires_in": KEYBOARD_LEASE_SECONDS})
                return

            b = self._body()
            if b is None:
                self._json({"ok": False, "error": "bad_request"}, code=413)
                return
            token = b.get("token", "")
            if path == "/keyboard/release":
                with _keyboard_lease_guard():
                    lease = _read_keyboard_lease_locked()
                    if lease and hmac.compare_digest(
                            str(lease.get("session_hash", "")), session_id) and hmac.compare_digest(
                            str(lease.get("token", "")), token):
                        _write_keyboard_lease_locked(None)
                self.send_response(204); self.end_headers()
                return

            if path == "/keyboard/type":
                text_value = b.get("text")
                key_value = b.get("key")
                if (text_value is None) == (key_value is None):
                    self._json({"ok": False, "error": "provide_text_or_key"}, code=400)
                    return
                if text_value is not None:
                    if (not text_value or len(text_value) > KEYBOARD_MAX_CHARS
                            or any(ord(ch) < 32 or ord(ch) > 126 for ch in text_value)):
                        self._json({"ok": False,
                                    "error": "text_must_be_printable_ascii_under_121_chars"},
                                   code=400)
                        return
                    fifo_command = "WEBTYPE " + token + " " + text_value
                else:
                    special = {"SPACE", "ENTER", "BKSP", "TAB", "ESC"}
                    if key_value not in special:
                        self._json({"ok": False, "error": "unsupported_key"}, code=400)
                        return
                    fifo_command = "WEBTYPE_KEY " + token + " " + key_value

                with _keyboard_lease_guard():
                    lease = _read_keyboard_lease_locked()
                    valid = bool(
                        lease
                        and hmac.compare_digest(str(lease.get("session_hash", "")), session_id)
                        and hmac.compare_digest(str(lease.get("token", "")), token)
                    )
                    if not valid:
                        self._json({
                            "ok": False,
                            "error": ("no_running_external_game" if not game_keyboard_available()
                                      else "keyboard_lease_expired_or_owned_by_another_user"),
                            "keyboard_busy": bool(lease),
                            "keyboard_mine": False,
                            "keyboard_game_available": game_keyboard_available(),
                        }, code=409)
                        return
                    lease["expires_at"] = time.time() + KEYBOARD_LEASE_SECONDS
                    _write_keyboard_lease_locked(lease)
                    sent = write_fifo(fifo_command)
                if not sent:
                    self._json({"ok": False, "error": "pitv_menu_unavailable"}, code=503)
                else:
                    self.send_response(204); self.end_headers()
                return

            self._json({"ok": False, "error": "not_found"}, code=404)
            return

        if path == "/action":
            user = self._session_user()
            if not user:
                self.send_response(403); self.end_headers(); return
            if not ADMIN and guest_locked():
                # 423 Locked — the page greys itself out, but the server is
                # what actually refuses, so a stale tab can't get through.
                self.send_response(423); self.end_headers(); return
            b = action_body
            action = b.get("action", "")
            # A claimed phone PIN keypad has exclusive input priority over web
            # D-pad commands, including commands from the owner page.
            if action in NAV and pin_entry_status(self._keyboard_session_id())["pin_busy"]:
                self._json({"ok": False, "error": "pin_keypad_in_use"}, code=423)
                return
            # While somebody owns the shared TV keyboard, reject competing web
            # navigation. Power and administrator safety controls remain usable.
            if action in (set(NAV) | set(SKY_ACTIONS) | {"input_sky", "input_pitv"}):
                keyboard = keyboard_lease_status(self._keyboard_session_id())
                if keyboard["keyboard_busy"] and not keyboard["keyboard_mine"]:
                    self._json({"ok": False, "error": "keyboard_in_use"}, code=423)
                    return
            if action in NAV:
                write_fifo(NAV[action])
            elif action in UDP:
                send_udp(UDP[action])
            elif action in SKY_ACTIONS:
                # Fixed token from our own table — the browser can't name an
                # arbitrary CEC frame. tv_menu drops it unless Sky mode is on.
                send_udp(f"SKY {SKY_ACTIONS[action]}")
            elif action in ("sky_power_on", "sky_power_off"):
                send_udp("SKY_POWER_ON" if action.endswith("_on")
                         else "SKY_POWER_OFF")
            elif ADMIN and action == "guest_on":
                set_guest_mode(True)
            elif ADMIN and action == "guest_off":
                set_guest_mode(False)
            elif ADMIN and action in ("guest_lock", "guest_unlock"):
                set_guest_lock(action == "guest_lock")
            self.send_response(204); self.end_headers()
            return

        self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


class PiTVServer(ThreadingHTTPServer):
    """Drops connections from outside the allowlist before a single byte of
    request is parsed — the login page isn't even reachable from an address
    that isn't allowed."""

    def verify_request(self, request, client_address):
        if address_allowed(client_address[0]):
            return True
        print(f"refused connection from {client_address[0]}", flush=True)
        return False


def main():
    if not ADMIN:
        maybe_rotate()
    get_secret()
    if not pitv_secrets.control_key():
        print("WARNING: no control key readable — TV commands will be refused "
              "by tv_menu. Run ./deploy.sh", flush=True)
    nets = allowed_networks()
    if nets:
        print("Allowing only: " + ", ".join(str(n) for n in nets), flush=True)
    httpd = PiTVServer(("0.0.0.0", PORT), Handler)
    print(f"PiTV {'admin' if ADMIN else 'guest'} portal on :{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
