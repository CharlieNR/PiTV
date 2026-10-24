#!/usr/bin/env python3
"""Shared permanent and 24-hour temporary PiTV player PINs."""
from __future__ import annotations

import fcntl
import os
import secrets
import time
from contextlib import contextmanager

import pitv_secrets
import pitv_state

PERMANENT_SECTION = "PLAYER_PINS"
TEMPORARY_SECTION = "TEMP_PLAYER_PINS"
TEMPORARY_TTL = 24 * 60 * 60
TEMP_LOCK = "/tmp/pitv-temporary-pins.lock"


@contextmanager
def _guard():
    fd = os.open(TEMP_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _permanent() -> dict[str, str]:
    raw = pitv_state.load_section(PERMANENT_SECTION, {})
    return {str(name): str(pin) for name, pin in raw.items()} if isinstance(raw, dict) else {}


def _clean_temporary(raw, now: float) -> dict[str, dict]:
    clean: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return clean
    for raw_name, raw_entry in raw.items():
        name = str(raw_name).strip()
        if not name or not isinstance(raw_entry, dict):
            continue
        pin = str(raw_entry.get("pin", ""))
        try:
            expires_at = int(raw_entry.get("expires_at", 0))
        except (TypeError, ValueError):
            continue
        if (expires_at <= now or not pin.isdigit() or len(pin) != 6
                or any(ord(ch) < 32 for ch in name) or len(name) > 40):
            continue
        clean[name] = {"pin": pin, "expires_at": expires_at}
    return clean


def temporary_records() -> list[dict]:
    """Return valid, unexpired temporary PINs and prune expired records."""
    with _guard():
        raw = pitv_state.load_section(TEMPORARY_SECTION, {})
        clean = _clean_temporary(raw, time.time())
        if clean != raw:
            pitv_state.save_section(TEMPORARY_SECTION, clean)
        return [
            {"name": name, "pin": entry["pin"], "temporary": True,
             "expires_at": entry["expires_at"]}
            for name, entry in sorted(clean.items(), key=lambda pair: pair[0].casefold())
        ]


def all_pin_records() -> list[dict]:
    """Return all player PINs for the administrator UI, without emergency PIN."""
    permanent = _permanent()
    records = [
        {"name": name, "pin": pin, "temporary": False, "expires_at": None}
        for name, pin in permanent.items()
    ]
    # Permanent entries take precedence if old data ever contains a name clash.
    seen = set(permanent)
    for item in temporary_records():
        if item["name"] not in seen:
            records.append(item)
    return sorted(records, key=lambda item: item["name"].casefold())


def load_all_pins() -> dict[str, str]:
    """Return PINs that can currently unlock a game, including live temporary PINs."""
    permanent = _permanent()
    for item in temporary_records():
        if item["name"] not in permanent:
            permanent[item["name"]] = item["pin"]
    return permanent


def create_temporary_pin(name: str) -> dict:
    """Generate a new non-custom PIN valid for exactly 24 hours."""
    name = " ".join(str(name).split())
    if not name or len(name) > 40 or any(ord(ch) < 32 for ch in name):
        raise ValueError("Enter a name of 1–40 characters.")
    with _guard():
        permanent = _permanent()
        raw = pitv_state.load_section(TEMPORARY_SECTION, {})
        temp = _clean_temporary(raw, time.time())
        if temp != raw:
            pitv_state.save_section(TEMPORARY_SECTION, temp)
        if name in permanent or name in temp:
            raise ValueError(f"A PIN already exists for '{name}'. Choose a different name.")
        used = set(permanent.values())
        used.update(entry["pin"] for entry in temp.values())
        used.add(pitv_secrets.emergency_code())
        while True:
            pin = f"{secrets.randbelow(1000000):06d}"
            if pin not in used:
                break
        expires_at = int(time.time()) + TEMPORARY_TTL
        temp[name] = {"pin": pin, "expires_at": expires_at}
        pitv_state.save_section(TEMPORARY_SECTION, temp)
    return {"name": name, "pin": pin, "temporary": True, "expires_at": expires_at}


def remove_temporary_pin(name: str) -> bool:
    """Remove a temporary PIN by name; never removes a permanent PIN."""
    with _guard():
        raw = pitv_state.load_section(TEMPORARY_SECTION, {})
        temp = _clean_temporary(raw, time.time())
        removed = temp.pop(str(name), None) is not None
        if temp != raw or removed:
            pitv_state.save_section(TEMPORARY_SECTION, temp)
        return removed
