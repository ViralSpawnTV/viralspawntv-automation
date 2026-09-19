import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright


HISTORY_PATH = Path("history.json")
MANIFEST_PATH = Path("work/v11_candidate_manifest.json")

MAX_PUBLIC_UPLOADS_PER_CREATOR_24H = 2
MAX_PUBLIC_UPLOADS_PER_GAME_24H = 4
DIVERSITY_WINDOW_HOURS = 24
MAX_CLIPS_PER_GAME = 18
MAX_TOTAL_CANDIDATES = 80

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# Game-first discovery. A dead/renamed category is simply skipped.
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


def parse_utc(value):
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except Exception:
        return None


def history_state(history):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=DIVERSITY_WINDOW_HOURS)
    used = set()
    creator_counts = {}
    game_counts = {}

    for item in history.get("used_clips", []):
        if not isinstance(item, dict):
            continue

        clip_id = item.get("clip_id")
        if clip_id:
            used.add(str(clip_id))

        if str(item.get("privacy_status", "")).lower() != "public":
            continue

        when = parse_utc(item.get("processed_at_utc"))
        if when is None or when < cutoff:
            continue

        channel = str(item.get("channel", "")).strip().lower()
        game = str(item.get("game", "")).strip().lower()

        if channel:
            creator_counts[channel] = creator_counts.get(channel, 0) + 1
        if game:
            game_counts[game] = game_counts.get(game, 0) + 1

    return used, creator_counts, game_counts


def clip_id_from_url(url):
    m = re.search(r"(clip_[A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def channel_from_url(url):
    m = re.search(r"kick\.com/([^/]+)/clips/clip_", url, re.I)
    return m.group(1).lower() if m else ""


def discover_category(page, game, slug):
    url = f"https://kick.com/category/{slug}/clips"
    print(f"Scanning game category: {game}")
    print(url)

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4500)

    html = page.content()
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception:
        pass

    # Category is known gaming content, but still avoid category pages that
    # unexpectedly expose gambling language prominently.
    lowered = body.lower()
    if any(term in lowered for term in GAMBLING_TERMS):
        print("Category page contains gambling terms; individual clips still filtered downstream.")

    patterns = [
        r'href=["\'](/[^"\']+/clips/clip_[A-Za-z0-9_-]+)["\']',
        r'https://kick\.com/[^"\'\\\s]+/clips/clip_[A-Za-z0-9_-]+',
    ]

    links = []
    for pattern in patterns:
        for match in re.findall(pattern, html):
            full = match if match.startswith("http") else f"https://kick.com{match}"
            if full not in links:
                links.append(full)

    print(f"Found {len(links)} clip links for {game}.")
    return links[:MAX_CLIPS_PER_GAME]


def interleave(per_game):
    # Round-robin by game so Fortnite/GTA cannot monopolize the front
    # of the candidate manifest simply because they have more clips.
    output = []
    max_len = max((len(v) for v in per_game.values()), default=0)

    for position in range(max_len):
        for game, candidates in per_game.items():
            if position < len(candidates):
                output.append(candidates[position])
                if len(output) >= MAX_TOTAL_CANDIDATES:
                    return output
    return output


def main():
    history = load_history()
    used, creator_counts, game_counts = history_state(history)

    print("================================================")
    print("ViralSpawnTV V11 Game-First Batch Discovery")
    print("================================================")
    print(f"Used clips in history: {len(used)}")

    per_game = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1000},
        )

        for game, slug in GAME_CATEGORIES:
            if game_counts.get(game.lower(), 0) >= MAX_PUBLIC_UPLOADS_PER_GAME_24H:
                print(f"Skipping {game}: game diversity cap reached.")
                continue

            try:
                links = discover_category(page, game, slug)
            except Exception as exc:
                print(f"Category skipped: {game}: {exc}")
                continue

            rows = []
            for position, url in enumerate(links, start=1):
                clip_id = clip_id_from_url(url)
                channel = channel_from_url(url)

                if not clip_id or not channel:
                    continue
                if clip_id in used:
                    continue
                if creator_counts.get(channel, 0) >= MAX_PUBLIC_UPLOADS_PER_CREATOR_24H:
                    continue

                rows.append({
                    "platform": "kick",
                    "game": game,
                    "category_slug": slug,
                    "channel": channel,
                    "clip_id": clip_id,
                    "clip_url": url,
                    "category_position": position,
                })

            if rows:
                per_game[game] = rows

        browser.close()

    candidates = interleave(per_game)

    manifest = {
        "version": 11,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "creator_cap_24h": MAX_PUBLIC_UPLOADS_PER_CREATOR_24H,
        "game_cap_24h": MAX_PUBLIC_UPLOADS_PER_GAME_24H,
        "games_with_candidates": list(per_game.keys()),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print(f"Games with candidates: {len(per_game)}")
    print(f"Batch candidate count: {len(candidates)}")

    if not candidates:
        raise RuntimeError("V11 discovery found no eligible game-category clips.")

    print("V11 batch discovery complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"V11 DISCOVERY FAILED: {exc}")
        sys.exit(1)
