#!/usr/bin/env python3
"""
/opt/pitv/pitv-config.py — local settings the `screen` CLI edits directly.

  screen sky status                  Sky Q control: on/off, device, routing
  screen control status              the signed-command channel
  screen control key show|rotate     the shared control key
  screen network status              who may reach the web portals
  screen network allow <cidr>        add an allowed client range
  screen network clear               allow any address again

Sky Q switching ON/OFF goes through the FIFO instead (tv_menu owns that state
while it's running); this is the read-only/system half.
"""

import grp
import json
import os
import pwd
import stat
import sys

import pitv_secrets
import pitv_state

PITV_DIR     = os.path.expanduser("~/.pitv")
SKY_FILE     = "SKY_SETTINGS"
NETWORK_FILE = "NETWORK_SETTINGS"
TV_INPUT     = "/tmp/pitv-tv-input"
SKY_PHYS     = "10:00"


def _load(path, default):
    try:
        return pitv_state.load_named(path, default)
    except Exception:
        return default


def _save(path, data):
    pitv_state.save_named(path, data)


# ── sky ──────────────────────────────────────────────────────────────
def cmd_sky(args):
    d = _load(SKY_FILE, {})
    on = bool(d.get("enabled"))
    print(f"Sky Q control: {'ON' if on else 'OFF (default)'}")
    if not on:
        print("Turn it on with:  screen sky on")
        return
    print(f"  CEC device number : {d.get('logical', 3)}")
    print(f"  Route remote keys : {'always' if d.get('always') else 'only while the TV is on the Sky input'}")
    try:
        with open(TV_INPUT) as f:
            phys = f.read().strip()
    except OSError:
        phys = ""
    where = ("Sky" if (d.get("always") or phys == SKY_PHYS) else "the PiTV menu")
    seen  = phys or "unknown"
    print(f"  TV input now      : {seen}")
    print(f"  Nav keys go to    : {where}")


# ── control key ──────────────────────────────────────────────────────
def _readers_of(path):
    """Which users can read the key file, in plain words."""
    try:
        st = os.stat(path)
    except OSError:
        return "the file does not exist yet"
    mode = stat.S_IMODE(st.st_mode)
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    who = [f"{owner} (owner)"]
    if mode & stat.S_IRGRP:
        try:
            members = grp.getgrnam(group).gr_mem
        except KeyError:
            members = []
        who.append(f"group {group}" + (f" [{', '.join(members)}]" if members else ""))
    if mode & stat.S_IROTH:
        who.append("EVERYONE ON THE BOX — tighten this")
    return f"{oct(mode)} {owner}:{group} — readable by " + ", ".join(who)


def cmd_control(args):
    sub = args[0] if args else "status"
    path = pitv_secrets.control_key_path()
    if sub == "status":
        key = pitv_secrets.control_key()
        print("Signed control channel (UDP 127.0.0.1:8129)")
        print(f"  Central state: {path}")
        print("  Key storage : private to the PiTV user; only the control key is")
        print("                exposed to Homebridge by the restricted reader helper.")
        print(f"  Fingerprint : {pitv_secrets.key_fingerprint(key)}")
        if not key:
            print("  !! No readable key: tv_menu refuses every command. "
                  "Run ./deploy.sh")
            sys.exit(1)
        hb = _homebridge_user()
        if hb:
            ready = (os.path.exists("/usr/local/bin/pitv-read-control-key")
                     and os.path.exists("/etc/sudoers.d/pitv-control-key"))
            print(f"  Homebridge  : user '{hb}' "
                  + ("restricted key reader is installed" if ready else
                     "key reader is not installed — run ./deploy.sh"))
        print("  Rejections  : see `screen log` for 'Control UDP: rejected'")
        return
    if sub == "key":
        what = args[1] if len(args) > 1 else "show"
        if what == "show":
            key = pitv_secrets.control_key()
            if not key:
                print(f"No readable key at {path}."); sys.exit(1)
            print(key.decode())
            return
        if what == "rotate":
            import secrets as _secrets
            new = _secrets.token_hex(32)
            try:
                pitv_state.write_value("CONTROL_KEY", new)
            except Exception as e:
                print(f"Could not update central state file {path}: {e}")
                print("Try:  sudo ./deploy.sh"); sys.exit(1)
            print(f"New key written to the central state file {path} "
                  f"(fingerprint {pitv_secrets.key_fingerprint(new.encode())}).")
            print("tv_menu picks it up on its own. Restart Homebridge so the "
                  "plugin re-reads it:  sudo hb-service restart")
            return
    print("Usage: screen control <status|key show|key rotate>")
    sys.exit(1)


def _homebridge_user():
    """Best guess at the user Homebridge runs as, for the readability check."""
    for unit in ("/etc/systemd/system/homebridge.service",
                 "/lib/systemd/system/homebridge.service"):
        try:
            for line in open(unit):
                if line.strip().startswith("User="):
                    return line.strip().split("=", 1)[1].strip()
        except OSError:
            continue
    try:
        pwd.getpwnam("homebridge")
        return "homebridge"
    except KeyError:
        return ""


def _can_read(path, user):
    try:
        st = os.stat(path)
        pw = pwd.getpwnam(user)
    except (OSError, KeyError):
        return False
    mode = stat.S_IMODE(st.st_mode)
    if pw.pw_uid == st.st_uid:
        return bool(mode & stat.S_IRUSR)
    groups = {g.gr_gid for g in grp.getgrall() if user in g.gr_mem}
    groups.add(pw.pw_gid)
    if st.st_gid in groups:
        return bool(mode & stat.S_IRGRP)
    return bool(mode & stat.S_IROTH)


# ── network allowlist ────────────────────────────────────────────────
def cmd_network(args):
    import ipaddress
    sub = args[0] if args else "status"
    data = _load(NETWORK_FILE, {})
    allow = data.get("allow", [])
    if sub == "status":
        if not allow:
            print("Web portals: reachable from ANY address that can route to "
                  "the Pi (the default).")
            print("Restrict to your LAN with:  screen network allow "
                  "192.168.1.0/24")
        else:
            print("Web portals: only these client addresses are accepted —")
            for a in allow:
                print(f"  {a}")
            print("Everything else is dropped before the login page is served.")
        return
    if sub == "allow":
        cidr = args[1] if len(args) > 1 else ""
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            print("Usage: screen network allow <cidr>   e.g. 192.168.1.0/24")
            sys.exit(1)
        if str(net) not in allow:
            allow.append(str(net))
        data["allow"] = allow
        _save(NETWORK_FILE, data)
        print(f"Allowed {net}. Now: " + ", ".join(allow))
        print("Restart the portals to apply immediately:  "
              "sudo systemctl restart pitv-guest pitv-admin")
        return
    if sub == "clear":
        data["allow"] = []
        _save(NETWORK_FILE, data)
        print("Allowlist cleared — any address may reach the portals again.")
        return
    print("Usage: screen network <status|allow <cidr>|clear>")
    sys.exit(1)


def cmd_interface(args):
    """Read or persist the menu interface version (takes effect after restart)."""
    choice = args[0] if args else "status"
    current = str(pitv_state.read_value("UI_VERSION", "2")).strip()
    if current not in ("1", "2"):
        current = "2"

    if choice in ("status", "show"):
        name = "Classic" if current == "1" else "Modern TV"
        print(f"PiTV interface version: {current} ({name})")
        print("Change it with: screen version 1 or screen version 2")
        return

    if choice not in ("1", "2") or len(args) != 1:
        print("Usage: screen version [1|2|status]")
        sys.exit(1)

    try:
        pitv_state.write_value("UI_VERSION", choice)
    except Exception as exc:
        print(f"Could not save the interface choice: {exc}", file=sys.stderr)
        sys.exit(1)

    name = "Classic" if choice == "1" else "Modern TV"
    print(f"Saved PiTV interface version {choice} ({name}).")
    if current == choice:
        print("That version was already selected.")
    print("Restart the menu to apply it:")
    print("  sudo systemctl restart pitv-menu.service")
    print("This only restarts the PiTV menu service; it does not reboot the Pi.")



def cmd_carousel(args):
    """Read or change the persisted home-carousel autoplay setting."""
    if args[:1] != ["autoplay"] or len(args) > 2:
        print("Usage: screen settings carousel autoplay <on|off|status>", file=sys.stderr)
        sys.exit(2)
    choice = args[1].lower() if len(args) == 2 else "status"
    if choice in ("status", "show"):
        enabled = bool(pitv_state.read_value("CAROUSEL_AUTOPLAY", True))
        print("Carousel autoplay: " + ("ON" if enabled else "OFF"))
        return
    if choice not in ("on", "off"):
        print("Usage: screen settings carousel autoplay <on|off|status>", file=sys.stderr)
        sys.exit(2)
    try:
        pitv_state.write_value("CAROUSEL_AUTOPLAY", choice == "on")
    except Exception as exc:
        print(f"Could not save carousel autoplay: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Carousel autoplay " + ("enabled." if choice == "on" else "disabled."))



def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "sky":
        cmd_sky(args[1:])
    elif cmd == "control":
        cmd_control(args[1:])
    elif cmd == "network":
        cmd_network(args[1:])
    elif cmd == "interface":
        cmd_interface(args[1:])
    elif cmd == "carousel":
        cmd_carousel(args[1:])
    else:
        print("Usage: pitv-config.py <sky|control|network|interface> ...")
        sys.exit(1)


if __name__ == "__main__":
    main()
