import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import sync_playwright


HISTORY_PATH = Path("history.json")
SHORTS_REJECTED_HISTORY_PATH = Path("shorts_rejected_history.json")
LONGFORM_HISTORY_PATH = Path("longform_history.json")
LONGFORM_REJECTED_HISTORY_PATH = Path("longform_rejected_history.json")
MANIFEST_PATH = Path("work/v11_candidate_manifest.json")

MAX_PUBLIC_UPLOADS_PER_CREATOR_24H = 2
MAX_PUBLIC_UPLOADS_PER_GAME_24H = 4
DIVERSITY_WINDOW_HOURS = 24

MAX_CLIPS_PER_GAME = int(os.getenv("DISCOVERY_MAX_CLIPS_PER_GAME", "18"))
MAX_TOTAL_CANDIDATES = int(os.getenv("DISCOVERY_MAX_TOTAL_CANDIDATES", "80"))

LONGFORM_MODE = MAX_TOTAL_CANDIDATES > 80
DISCOVERY_SCROLL_ROUNDS = int(os.getenv("DISCOVERY_SCROLL_ROUNDS", "14" if LONGFORM_MODE else "0"))
DISCOVERY_SCROLL_WAIT_MS = int(os.getenv("DISCOVERY_SCROLL_WAIT_MS", "900"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

GAME_CATEGORIES = [
    ("Grand Theft Auto V (GTA)", "grand-theft-auto-v"),
    ("Call of Duty: Warzone", "call-of-duty-warzone"),
    ("Minecraft", "minecraft"),
    ("Fortnite", "fortnite"),
    ("Valorant", "valorant"),
    ("Counter-Strike 2", "counter-strike-2"),
    ("Rocket League", "rocket-league"),
    ("League of Legends", "league-of-legends"),
    ("Apex Legends", "apex-legends"),
    ("Overwatch 2", "overwatch-2"),
    ("Marvel Rivals", "marvel-rivals"),
    ("Roblox", "roblox"),
    ("Rust", "rust"),
    ("Dead by Daylight", "dead-by-daylight"),
    ("Escape from Tarkov", "escape-from-tarkov"),
    ("Call of Duty: Black Ops 7", "call-of-duty-black-ops-7"),
]

GAMBLING_TERMS = {
    "casino", "slots", "slot machine", "gambling", "roulette",
    "blackjack", "sportsbook", "sports betting", "betting",
}


def load_history():
    if not HISTORY_PATH.exists():
        return {"version": 1, "used_clips": []}
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "used_clips": []}
    if isinstance(data, list):
        return {"version": 1, "used_clips": data}
    if not isinstance(data, dict):
        return {"version": 1, "used_clips": []}
    data.setdefault("used_clips", [])
    return data


def load_aux_history(path, key):
    if not path.exists():
        return {"version": 1, key: []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, key: []}
    if isinstance(data, list):
        return {"version": 1, key: data}
    if not isinstance(data, dict):
        return {"version": 1, key: []}
    data.setdefault(key, [])
    if not isinstance(data.get(key), list):
        data[key] = []
    return data


def normalize_clip_id(value):
    return str(value or "").strip().casefold()


def normalize_clip_url(value):
