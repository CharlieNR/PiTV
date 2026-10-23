#!/usr/bin/env python3
"""Central, device-only persistent state for PiTV.

All durable PiTV data belongs in ~/.pitv/pitv-state.json. Runtime files such
as logs, FIFOs and /tmp status markers are deliberately separate.

The first call to ensure_state() imports the older split files only when the
central file does not exist. Once the central file exists, deployment never
recreates, merges over, or resets it.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from typing import Any

STATE_FILE = os.path.abspath(os.path.expanduser(
    os.environ.get("PITV_STATE_FILE", "~/.pitv/pitv-state.json")
))
STATE_DIR = os.path.dirname(STATE_FILE)
_LOCK_ID = hashlib.sha256(STATE_FILE.encode("utf-8")).hexdigest()[:20]
LOCK_FILE = f"/tmp/pitv-state-{_LOCK_ID}.lock"

# Compatibility mappings used while migrating older versions and by callers
# being transitioned to the central store.
_LEGACY_SECTIONS = {
    "guests.json": "GUEST_ACCOUNTS",
    "admin.json": "ADMIN_ACCOUNTS",
    "pins.json": "PLAYER_PINS",
    "sky.json": "SKY_SETTINGS",
    "network.json": "NETWORK_SETTINGS",
    "pairing.json": "PAIRING",
    "lock-state.json": "LOCK_STATE",
}
_LEGACY_VALUE_FILES = {
    "emergency-code": "EMERGENCY_PIN",
    "control-key": "CONTROL_KEY",
}
_LEGACY_SECRET_FILES = {
    "portal-secret": "GUEST_PORTAL_SECRET",
    "admin-secret": "ADMIN_PORTAL_SECRET",
}
_LEGACY_FILES = tuple(_LEGACY_SECTIONS) + tuple(_LEGACY_VALUE_FILES) + tuple(_LEGACY_SECRET_FILES)


@contextmanager
def _locked():
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_json_file(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except Exception as exc:
        # Never silently drop account data because an old file is malformed.
        raise RuntimeError(f"Cannot migrate {path}: {exc}") from exc
    return value


def _read_text_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
        return value or None
    except OSError:
        return None


def _read_bytes_file(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            value = handle.read().strip()
        return value or None
    except OSError:
        return None


def _atomic_write(state: dict[str, Any]) -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    fd, temp_path = tempfile.mkstemp(prefix=".pitv-state-", suffix=".tmp", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, STATE_FILE)
        try:
            dir_fd = os.open(STATE_DIR, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def ensure_state() -> bool:
    """Create the state file once, importing old files only if it is absent.

    Returns True when newly created and False if an existing file was left
    completely untouched.
    """
    with _locked():
        if os.path.exists(STATE_FILE):
            return False

        legacy_dir = os.path.expanduser("~/.pitv")
        state: dict[str, Any] = {"_schema_version": 1}

        for filename, section in _LEGACY_SECTIONS.items():
            value = _read_json_file(os.path.join(legacy_dir, filename), {})
            if not isinstance(value, dict):
                raise RuntimeError(f"Cannot migrate {filename}: expected a JSON object")
            state[section] = value

        emergency = _read_text_file(os.path.join(legacy_dir, "emergency-code"))
        if emergency and emergency.isdigit() and len(emergency) == 6:
            state["EMERGENCY_PIN"] = emergency
        else:
            state["EMERGENCY_PIN"] = "000000"

        # The installer passes the old system key implicitly by making it
        # readable to the PiTV user. Existing system key takes priority over
        # the old per-user fallback, matching the previous implementation.
        old_control = (
            os.environ.get("PITV_LEGACY_CONTROL_KEY", "").strip()
            or _read_text_file("/etc/pitv/control.key")
            or _read_text_file(os.path.join(legacy_dir, "control-key"))
        )
        state["CONTROL_KEY"] = old_control or secrets.token_hex(32)

        for filename, key in _LEGACY_SECRET_FILES.items():
            old = _read_bytes_file(os.path.join(legacy_dir, filename))
            state[key] = old.hex() if old else secrets.token_hex(32)

        # Keep valid device data, accounts, password hashes, tokens and settings
        # as-is. Empty defaults are only used for files that never existed.
        _atomic_write(state)

        # Now that the central file has been written successfully, remove the
        # old live copies. The update script has already backed up ~/.pitv.
        for filename in _LEGACY_FILES:
            try:
                os.unlink(os.path.join(legacy_dir, filename))
            except FileNotFoundError:
                pass
            except OSError:
                # Data is safely in the central file; deployment can report any
                # remaining legacy copy without making the migration fail.
                pass
        return True


def read_state() -> dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        ensure_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception as exc:
        raise RuntimeError(f"PiTV state file cannot be read: {STATE_FILE}: {exc}") from exc
    if not isinstance(state, dict):
        raise RuntimeError(f"PiTV state file must contain a JSON object: {STATE_FILE}")
    return state


def read_value(name: str, default: Any = None) -> Any:
    return read_state().get(name, default)


def write_value(name: str, value: Any) -> None:
    ensure_state()
    with _locked():
        state = read_state()
        state[name] = value
        _atomic_write(state)


def load_section(name: str, default: Any = None) -> Any:
    return read_state().get(name, default)


def save_section(name: str, value: Any) -> None:
    write_value(name, value)


def load_named(path: str, default: Any = None) -> Any:
    """Compatibility read for callers still passing an old data-file path."""
    section = _LEGACY_SECTIONS.get(os.path.basename(path))
    if section is None:
        return default
    return load_section(section, default)


def save_named(path: str, value: Any) -> None:
    """Compatibility write for callers still passing an old data-file path."""
    section = _LEGACY_SECTIONS.get(os.path.basename(path))
    if section is None:
        raise ValueError(f"Not a persistent PiTV state path: {path}")
    save_section(section, value)


def get_control_key_for_helper() -> int:
    """Print only the control key for the restricted Homebridge reader helper."""
    value = read_value("CONTROL_KEY", "")
    if not isinstance(value, str) or not value:
        return 1
    print(value)
    return 0


def main() -> int:
    import sys

    args = sys.argv[1:]
    command = args[0] if args else ""
    if command == "ensure":
        print("CREATED" if ensure_state() else "EXISTS")
        return 0
    if command == "get-control-key":
        return get_control_key_for_helper()
    print("Usage: pitv_state.py ensure|get-control-key", file=__import__("sys").stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
