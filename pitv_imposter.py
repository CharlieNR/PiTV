#!/usr/bin/env python3
"""Local shared state for PiTV's Imposter party game. No PINs are stored here."""
from __future__ import annotations
import copy, fcntl, hmac, json, os, secrets, time
from contextlib import contextmanager

STATE_PATH = "/tmp/pitv-imposter-state.json"
LOCK_PATH = "/tmp/pitv-imposter-state.lock"

def _read():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}

def _write(v):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(v, f, separators=(",", ":"), ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)
    os.chmod(STATE_PATH, 0o600)

@contextmanager
def _transaction(write):
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        state = _read()
        try:
            yield state
        except Exception:
            raise
        else:
            if write:
                _write(state)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

def read_state():
    with _transaction(False) as s:
        return copy.deepcopy(s)

def _player_for_token(state, token):
    if not token:
        return None
    found = None
    for p in state.get("players", []):
        try:
            if hmac.compare_digest(str(p.get("token", "")), str(token)):
                found = p
        except (TypeError, ValueError):
            pass
    return found

def _assign_roles(s):
    names = [p["name"] for p in s.get("players", [])]
    if len(names) < 3:
        s["active"], s["phase"], s["error"] = False, "ended", "At least three players are required."
        return
    imposter = secrets.choice(names)
    chooser = secrets.choice([n for n in names if n != imposter])
    s["imposter"], s["word_picker"], s["word"] = imposter, chooser, ""
    s["votes"], s["results"], s["vote_ends_at"], s["reveal_at"] = {}, [], 0, 0
    s["turn_index"] = 0
    for p in s["players"]:
        p["role"] = "imposter" if p["name"] == imposter else "chooser" if p["name"] == chooser else "player"
        p["vote"] = ""
    s["phase"] = "choose_word"

def create_game(game_id, names, join_url):
    names = list(dict.fromkeys(str(n).strip() for n in names if str(n).strip()))
    if len(names) < 3:
        raise ValueError("Imposter needs at least three distinct players.")
    state = {
        "active": True, "game_id": game_id, "join_url": join_url, "phase": "join",
        "round_number": 1, "created_at": time.time(),
        "players": [{"name": n, "joined": False, "token": "", "role": "", "vote": ""} for n in names],
        "imposter": "", "word_picker": "", "word": "", "turn_index": 0,
        "votes": {}, "results": [], "vote_ends_at": 0, "reveal_at": 0, "error": ""
    }
    with _transaction(True) as s:
        s.clear()
        s.update(state)
    return copy.deepcopy(state)

def end_game(game_id):
    with _transaction(True) as s:
        if s.get("game_id") != game_id:
            return
        s["active"], s["phase"], s["vote_ends_at"], s["reveal_at"] = False, "ended", 0, 0

def authenticate_player(game_id, name):
    with _transaction(True) as s:
        if not s.get("active") or s.get("game_id") != game_id:
            return {"ok": False, "error": "game_unavailable"}
        p = next((p for p in s.get("players", []) if p.get("name") == name), None)
        if not p:
            return {"ok": False, "error": "not_a_player"}
        if not p.get("joined"):
            if s.get("phase") != "join":
                return {"ok": False, "error": "joining_closed"}
            p["joined"] = True
        p["token"] = secrets.token_urlsafe(32)
        if s.get("phase") == "join" and all(p.get("joined") for p in s["players"]):
            _assign_roles(s)
        return {"ok": True, "token": p["token"], "name": p["name"]}

def player_view(game_id, token):
    s = read_state()
    if s.get("game_id") != game_id:
        return None
    p = _player_for_token(s, token)
    if not p:
        return None
    phase = s.get("phase", "ended")
    names = [str(x.get("name", "")) for x in s.get("players", [])]
    joined = sum(1 for x in s.get("players", []) if x.get("joined"))
    idx = int(s.get("turn_index", 0) or 0)
    word = ""
    if phase == "revealed":
        word = str(s.get("word", ""))
    elif phase in ("round", "round_complete", "vote", "results") and p.get("role") != "imposter":
        word = str(s.get("word", ""))
    return {
        "ok": True, "active": bool(s.get("active")), "phase": phase,
        "you": str(p.get("name", "")), "role": str(p.get("role", "")), "word": word,
        "players": names, "joined_count": joined, "player_count": len(names),
        "round_number": int(s.get("round_number", 1) or 1),
        "current_turn": names[idx] if 0 <= idx < len(names) else "",
        "vote_ends_at": float(s.get("vote_ends_at", 0) or 0),
        "my_vote": str(s.get("votes", {}).get(p.get("name", ""), "")),
        "results": copy.deepcopy(s.get("results", [])) if phase in ("results", "revealed") else [],
        "imposter": str(s.get("imposter", "")) if phase == "revealed" else "",
    }

def choose_word(game_id, token, word):
    word = " ".join(str(word).split())
    if not word or len(word) > 40 or any(ord(c) < 32 for c in word):
        return {"ok": False, "error": "invalid_word"}
    with _transaction(True) as s:
        p = _player_for_token(s, token)
        if not s.get("active") or s.get("game_id") != game_id or s.get("phase") != "choose_word" or not p or p.get("role") != "chooser":
            return {"ok": False, "error": "not_word_chooser"}
        s["word"], s["phase"], s["turn_index"] = word, "round", 0
        return {"ok": True}

def set_vote(game_id, token, target):
    with _transaction(True) as s:
        p = _player_for_token(s, token)
        names = [str(x.get("name", "")) for x in s.get("players", [])]
        if not s.get("active") or s.get("game_id") != game_id or s.get("phase") != "vote" or time.time() >= float(s.get("vote_ends_at", 0) or 0):
            return {"ok": False, "error": "voting_closed"}
        if not p or target not in names:
            return {"ok": False, "error": "invalid_player"}
        s.setdefault("votes", {})[p["name"]] = target
        p["vote"] = target
        return {"ok": True, "target": target}

def advance_turn(game_id):
    with _transaction(True) as s:
        if not s.get("active") or s.get("game_id") != game_id or s.get("phase") != "round":
            return
        i = int(s.get("turn_index", 0) or 0)
        if i + 1 >= len(s.get("players", [])):
            s["phase"] = "round_complete"
        else:
            s["turn_index"] = i + 1

def next_round(game_id):
    with _transaction(True) as s:
        if not s.get("active") or s.get("game_id") != game_id or s.get("phase") not in ("round_complete", "revealed"):
            return False
        s["round_number"] = int(s.get("round_number", 1) or 1) + 1
        _assign_roles(s)
        return s.get("phase") == "choose_word"

def start_vote(game_id):
    with _transaction(True) as s:
        if not s.get("active") or s.get("game_id") != game_id or s.get("phase") != "round_complete":
            return False
        s["votes"], s["results"] = {}, []
        for p in s.get("players", []):
            p["vote"] = ""
        s["vote_ends_at"], s["phase"] = time.time() + 15, "vote"
        return True

def advance_deadlines(game_id, now=None):
    now = time.time() if now is None else now
    with _transaction(True) as s:
        if not s.get("active") or s.get("game_id") != game_id:
            return
        if s.get("phase") == "vote" and now >= float(s.get("vote_ends_at", 0) or 0):
            counts = {}
            for target in s.get("votes", {}).values():
                if target:
                    counts[target] = counts.get(target, 0) + 1
            s["results"] = [{"name": n, "count": c} for n, c in
                sorted(counts.items(), key=lambda it: (it[1], it[0].casefold())) if c > 0]
            s["phase"], s["reveal_at"] = "results", now + 5
        elif s.get("phase") == "results" and now >= float(s.get("reveal_at", 0) or 0):
            s["phase"], s["reveal_at"] = "revealed", 0
