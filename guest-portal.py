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
function act(a){
  if(document.body.classList.contains('locked') && !ADMIN) return;
  fetch('/action',{method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams({action:a})}).then(function(r){
      if(r.status===423){sync();}
    });
}
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
        if not ADMIN and not guest_mode_on():
            self._html(gate_body()); return

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
            b = self._body()
            if b is None:
                self.send_response(413); self.end_headers(); return
            action = b.get("action", "")
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
