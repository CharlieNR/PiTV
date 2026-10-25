#!/usr/bin/env python3
"""Local shared state for PiTV's Imposter party game. No PINs are stored here."""
from __future__ import annotations
import copy, fcntl, hmac, json, os, secrets, time
from contextlib import contextmanager

STATE_PATH = "/tmp/pitv-imposter-state.json"
LOCK_PATH = "/tmp/pitv-imposter-state.lock"

WORD_CATEGORIES = {
    "Animals": (
        "Dolphin", "Elephant", "Penguin", "Octopus", "Crocodile",
        "Butterfly", "Giraffe", "Shark", "Panda", "Squirrel",
        "Eagle", "Kangaroo", "Owl", "Hedgehog", "Chameleon",
    ),
    "Food & Drink": (
        "Pizza", "Sushi", "Pancakes", "Chocolate", "Popcorn",
        "Curry", "Lemonade", "Sandwich", "Ice cream", "Spaghetti",
        "Watermelon", "Coffee", "Cheeseburger", "Doughnut", "Lasagne",
    ),
    "Countries": (
        "Australia", "Brazil", "Canada", "Egypt", "France",
        "Germany", "Greece", "India", "Italy", "Japan",
        "Kenya", "Mexico", "New Zealand", "Spain", "Sweden",
    ),
    "Nature & Weather": (
        "Volcano", "Tornado", "Rainbow", "Glacier", "Waterfall",
        "Desert", "Jungle", "Coral reef", "Earthquake", "Aurora",
        "Canyon", "Avalanche", "Tsunami", "Thunderstorm", "Forest",
    ),
    "Space": (
        "Mars", "Saturn", "Moon", "Comet", "Black hole",
        "Nebula", "Astronaut", "Telescope", "Spacesuit", "Meteor",
        "Galaxy", "Rocket", "Solar eclipse", "Space station", "Satellite",
    ),
    "Sports": (
        "Football", "Tennis", "Cricket", "Basketball", "Rugby",
        "Swimming", "Skiing", "Cycling", "Fencing", "Golf",
        "Badminton", "Skateboarding", "Volleyball", "Gymnastics", "Table tennis",
    ),
    "Technology": (
        "Smartphone", "Robot", "Drone", "Keyboard", "Computer",
        "Headphones", "3D printer", "Game controller", "Microchip", "Camera",
        "Printer", "Wi-Fi router", "Electric car", "VR headset", "Smartwatch",
    ),
    "Transport": (
        "Bicycle", "Submarine", "Helicopter", "Train", "Sailboat",
        "Tram", "Motorbike", "Taxi", "Hot air balloon", "Tractor",
        "Ferry", "Skateboard", "Ambulance", "Cable car", "Steam train",
    ),
    "Mythical Creatures": (
        "Dragon", "Unicorn", "Phoenix", "Mermaid", "Griffin",
        "Werewolf", "Ghost", "Wizard", "Goblin", "Centaur",
        "Fairy", "Troll", "Cyclops", "Kraken", "Vampire",
    ),
    "Everyday Objects": (
        "Compass", "Microscope", "Calculator", "Ruler", "Backpack",
        "Whiteboard", "Glue stick", "Pencil sharpener", "Stopwatch", "Textbook",
        "Globe", "Scissors", "Protractor", "Lunchbox", "Umbrella",
    ),
}


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
    s["imposter"], s["word_picker"] = imposter, chooser
    s["category"], s["word"] = "", ""
    s["votes"], s["results"], s["vote_ends_at"], s["reveal_at"] = {}, [], 0, 0
    s["turn_index"] = 0
    for p in s["players"]:
        p["role"] = "imposter" if p["name"] == imposter else "chooser" if p["name"] == chooser else "player"
        p["vote"] = ""
    s["phase"] = "choose_category"

def create_game(game_id, names, join_url):
    names = list(dict.fromkeys(str(n).strip() for n in names if str(n).strip()))
    if len(names) < 3:
        raise ValueError("Imposter needs at least three distinct players.")
    if len(names) > 8:
        raise ValueError("Imposter supports at most eight players.")
    state = {
        "active": True, "game_id": game_id, "join_url": join_url, "phase": "join",
        "round_number": 1, "created_at": time.time(),
        "players": [{"name": n, "joined": False, "token": "", "role": "", "vote": ""} for n in names],
        "imposter": "", "word_picker": "", "category": "", "word": "", "turn_index": 0,
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
    role = str(p.get("role", ""))
    word = ""
    category = ""
    categories = []
    words = []

    # Send each role only the clue information it is entitled to see.
    # The imposter receives the category, never the selected word, until reveal.
    if phase == "revealed":
        word = str(s.get("word", ""))
        category = str(s.get("category", ""))
    elif role == "imposter":
        if phase in ("choose_word", "round", "round_complete", "vote", "results"):
            category = str(s.get("category", ""))
    elif role == "chooser" and phase == "choose_category":
        categories = list(WORD_CATEGORIES.keys())
    elif role == "chooser" and phase == "choose_word":
        category = str(s.get("category", ""))
        words = list(WORD_CATEGORIES.get(category, ()))
    elif phase in ("round", "round_complete", "vote", "results") and role != "imposter":
        word = str(s.get("word", ""))

    return {
        "ok": True, "active": bool(s.get("active")), "phase": phase,
        "you": str(p.get("name", "")), "role": role, "word": word,
        "category": category, "categories": categories, "words": words,
        "players": names, "joined_count": joined, "player_count": len(names),
        "round_number": int(s.get("round_number", 1) or 1),
        "current_turn": names[idx] if 0 <= idx < len(names) else "",
        "vote_ends_at": float(s.get("vote_ends_at", 0) or 0),
        "my_vote": str(s.get("votes", {}).get(p.get("name", ""), "")),
        "results": copy.deepcopy(s.get("results", [])) if phase in ("results", "revealed") else [],
        "imposter": str(s.get("imposter", "")) if phase == "revealed" else "",
    }
def choose_category(game_id, token, category):
    category = " ".join(str(category).split())
    canonical = next(
        (name for name in WORD_CATEGORIES if name.casefold() == category.casefold()),
        None,
    )
    if canonical is None:
        return {"ok": False, "error": "invalid_category"}
    with _transaction(True) as s:
        p = _player_for_token(s, token)
        if (not s.get("active") or s.get("game_id") != game_id
                or s.get("phase") != "choose_category" or not p
                or p.get("role") != "chooser"):
            return {"ok": False, "error": "not_word_chooser"}
        s["category"], s["word"] = canonical, ""
        s["phase"] = "choose_word"
        return {"ok": True, "category": canonical}


def choose_word(game_id, token, word):
    word = " ".join(str(word).split())
    if not word or len(word) > 40 or any(ord(c) < 32 for c in word):
        return {"ok": False, "error": "invalid_word"}
    with _transaction(True) as s:
        p = _player_for_token(s, token)
        if (not s.get("active") or s.get("game_id") != game_id
                or s.get("phase") != "choose_word" or not p
                or p.get("role") != "chooser"):
            return {"ok": False, "error": "not_word_chooser"}
        category = str(s.get("category", ""))
        allowed = WORD_CATEGORIES.get(category, ())
        canonical = next(
            (candidate for candidate in allowed if candidate.casefold() == word.casefold()),
            None,
        )
        if canonical is None:
            return {"ok": False, "error": "invalid_word"}
        s["word"], s["phase"], s["turn_index"] = canonical, "round", 0
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

def _begin_vote(s, now=None):
    """Enter the standard timed vote, clearing any votes from an earlier phase."""
    now = time.time() if now is None else now
    s["votes"], s["results"] = {}, []
    for player in s.get("players", []):
        player["vote"] = ""
    s["vote_ends_at"] = now + 15
    s["reveal_at"] = 0
    s["phase"] = "vote"


def advance_turn(game_id, token=None):
    """Advance the host's turn, or advance only for the authenticated current player."""
    with _transaction(True) as s:
        if not s.get("active") or s.get("game_id") != game_id or s.get("phase") != "round":
            return {"ok": False, "error": "turn_not_active"}
        players = s.get("players", [])
        if not players:
            return {"ok": False, "error": "no_players"}
        i = int(s.get("turn_index", 0) or 0)
        i = max(0, min(i, len(players) - 1))
        if token is not None:
            player = _player_for_token(s, token)
            if not player or player.get("name") != players[i].get("name"):
                return {"ok": False, "error": "not_your_turn"}
        if i + 1 < len(players):
            s["turn_index"] = i + 1
        else:
            # Clue rounds are unlimited; the host chooses when the group is
            # ready to vote using the explicit VOTE NOW option.
            s["phase"] = "round_complete"
        return {"ok": True, "phase": s["phase"]}


def rewind_turn(game_id):
    """Return to the player before the current turn; also undo round completion."""
    with _transaction(True) as s:
        if not s.get("active") or s.get("game_id") != game_id:
            return {"ok": False, "error": "game_unavailable"}
        players = s.get("players", [])
        if not players:
            return {"ok": False, "error": "no_players"}
        if s.get("phase") in ("round_complete", "vote"):
            # Back can undo the final player's advance even after the host has
            # opened voting, returning the group to the last player's clue turn.
            if s.get("phase") == "vote":
                s["votes"], s["results"] = {}, []
                for player in players:
                    player["vote"] = ""
                s["vote_ends_at"], s["reveal_at"] = 0, 0
            s["phase"] = "round"
            s["turn_index"] = len(players) - 1
            return {"ok": True, "current_turn": players[-1].get("name", "")}
        if s.get("phase") != "round":
            return {"ok": False, "error": "turn_not_active"}
        i = max(0, min(int(s.get("turn_index", 0) or 0), len(players) - 1))
        if i == 0:
            return {"ok": False, "error": "no_previous_turn"}
        s["turn_index"] = i - 1
        return {"ok": True, "current_turn": players[i - 1].get("name", "")}


def repeat_round(game_id):
    """Start another clue pass with the same roles and secret word."""
    with _transaction(True) as s:
        if (not s.get("active") or s.get("game_id") != game_id
                or s.get("phase") != "round_complete"):
            return False
        # Another clue pass can be started for as long as the host wants.
        s["round_number"] = int(s.get("round_number", 1) or 1) + 1
        s["turn_index"] = 0
        s["phase"] = "round"
        s["votes"], s["results"] = {}, []
        s["vote_ends_at"], s["reveal_at"] = 0, 0
        return True


def next_round(game_id):
    """Start a fresh match after the reveal, dealing new roles and a new word."""
    with _transaction(True) as s:
        if not s.get("active") or s.get("game_id") != game_id or s.get("phase") != "revealed":
            return False
        s["round_number"] = 1
        _assign_roles(s)
        return s.get("phase") == "choose_word"

def start_vote(game_id):
    with _transaction(True) as s:
        if not s.get("active") or s.get("game_id") != game_id or s.get("phase") != "round_complete":
            return False
        _begin_vote(s)
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
            # Rank every player, including those with zero votes, for a stable podium.
            names = [str(p.get("name", "")) for p in s.get("players", [])]
            s["results"] = [
                {"name": name, "count": int(counts.get(name, 0))}
                for name in sorted(names, key=lambda n: (-int(counts.get(n, 0)), n.casefold()))
            ]
            # Reveal the podium one place at a time before revealing the actual imposter.
            s["phase"], s["reveal_at"] = "results", now + 9
        elif s.get("phase") == "results" and now >= float(s.get("reveal_at", 0) or 0):
            s["phase"], s["reveal_at"] = "revealed", 0
