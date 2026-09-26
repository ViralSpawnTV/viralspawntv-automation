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
DISCOVERY_SCROLL_ROUNDS = int(
    os.getenv(
        "DISCOVERY_SCROLL_ROUNDS",
        "14" if LONGFORM_MODE else "8"
    )
)
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
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        netloc = parts.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = re.sub(r"/+", "/", parts.path).rstrip("/").casefold()
        return urlunsplit(((parts.scheme or "https").lower(), netloc, path, "", ""))
    except Exception:
        return raw.rstrip("/").casefold()


def identity_sets(history, key):
    ids = set()
    urls = set()

    for item in history.get(key, []):
        if isinstance(item, str):
            raw = item.strip()
            if raw.lower().startswith(("http://", "https://")):
                cid = normalize_clip_id(clip_id_from_url(raw))
                curl = normalize_clip_url(raw)
            else:
                cid = normalize_clip_id(raw)
                curl = ""
        elif isinstance(item, dict):
            cid = normalize_clip_id(item.get("clip_id") or item.get("id"))
            curl = normalize_clip_url(
                item.get("clip_url")
                or item.get("source_url")
                or item.get("url")
                or item.get("source")
            )
        else:
            continue

        if cid:
            ids.add(cid)
        if curl:
            urls.add(curl)

    return ids, urls


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
        # IMPORTANT: published history may contain plain clip-ID strings.
        if isinstance(item, str):
            raw = item.strip()
            if raw.lower().startswith(("http://", "https://")):
                cid = clip_id_from_url(raw)
                if cid:
                    used.add(normalize_clip_id(cid))
            elif raw:
                used.add(normalize_clip_id(raw))
            continue

        if not isinstance(item, dict):
            continue

        clip_id = item.get("clip_id")
        if clip_id:
            used.add(normalize_clip_id(clip_id))

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

    lowered = body.lower()
    if any(term in lowered for term in GAMBLING_TERMS):
        print(
            "Category page contains gambling terms; "
            "individual clips still filtered downstream."
        )

    if DISCOVERY_SCROLL_ROUNDS > 0:
        stagnant_rounds = 0
        previous_size = len(html)

        for round_no in range(
            DISCOVERY_SCROLL_ROUNDS
        ):
            page.evaluate(
                "window.scrollTo("
                "0, document.body.scrollHeight)"
            )

            page.wait_for_timeout(
                DISCOVERY_SCROLL_WAIT_MS
            )

            new_html = page.content()

            if len(new_html) <= previous_size:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
                previous_size = len(new_html)

            html = new_html

            print(
                f"Discovery scroll "
                f"{round_no + 1}/"
                f"{DISCOVERY_SCROLL_ROUNDS} "
                f"for {game}"
            )

            if stagnant_rounds >= 3:
                print(
                    f"No additional clips loading "
                    f"for {game}; stopping scroll."
                )
                break

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

    # Return every discovered link.
    # The fresh per-game limit is applied AFTER published/rejected filtering.
    return links


def interleave(per_game):
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

    # Robustly load published identities even when history uses strings/URLs.
    published_ids, published_urls = identity_sets(history, "used_clips")
    used |= published_ids

    # NEW: load permanent Shorts rejections before discovery.
    shorts_rejected_history = load_aux_history(
        SHORTS_REJECTED_HISTORY_PATH,
        "clip_ids",
    )
    shorts_rejected_ids, shorts_rejected_urls = identity_sets(
        shorts_rejected_history,
        "clip_ids",
    )

    longform_history = load_aux_history(LONGFORM_HISTORY_PATH, "used_clips")
    rejected_history = load_aux_history(
        LONGFORM_REJECTED_HISTORY_PATH,
        "rejected_clips",
    )
    longform_used_ids, longform_used_urls = identity_sets(
        longform_history,
        "used_clips",
    )
    rejected_ids, rejected_urls = identity_sets(
        rejected_history,
        "rejected_clips",
    )

    print("================================================")
    print("ViralSpawnTV V11 Game-First Batch Discovery")
    print("================================================")
    print(f"Published Shorts loaded before discovery: {len(published_ids)}")
    print(f"Rejected Shorts loaded before discovery: {len(shorts_rejected_ids)}")
    print(f"Max clips per game: {MAX_CLIPS_PER_GAME}")
    print(f"Max total candidates: {MAX_TOTAL_CANDIDATES}")

    if LONGFORM_MODE:
        print(f"Long-form used clips loaded: {len(longform_used_ids)}")
        print(f"Long-form rejected clips loaded: {len(rejected_ids)}")

    per_game = {}
    skipped_published = 0
    skipped_shorts_rejected = 0
    skipped_longform_used = 0
    skipped_longform_rejected = 0
    seen_ids = set()
    seen_urls = set()

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

                normalized_id = normalize_clip_id(clip_id)
                normalized_url = normalize_clip_url(url)

                if normalized_id in seen_ids or normalized_url in seen_urls:
                    continue

                # Filter published Shorts BEFORE they enter the candidate pool.
                if (
                    normalized_id in used
                    or normalized_id in published_ids
                    or normalized_url in published_urls
                ):
                    skipped_published += 1
                    continue

                # Filter permanently rejected Shorts BEFORE candidate creation.
                if (
                    normalized_id in shorts_rejected_ids
                    or normalized_url in shorts_rejected_urls
                ):
                    skipped_shorts_rejected += 1
                    continue

                if creator_counts.get(channel, 0) >= MAX_PUBLIC_UPLOADS_PER_CREATOR_24H:
                    continue

                if LONGFORM_MODE and (
                    normalized_id in longform_used_ids
                    or normalized_url in longform_used_urls
                ):
                    skipped_longform_used += 1
                    continue

                if LONGFORM_MODE and (
                    normalized_id in rejected_ids
                    or normalized_url in rejected_urls
                ):
                    skipped_longform_rejected += 1
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

                seen_ids.add(normalized_id)
                seen_urls.add(normalized_url)

                # Limit only AFTER filtering old/rejected clips.
                if len(rows) >= MAX_CLIPS_PER_GAME:
                    break

            if rows:
                per_game[game] = rows

        browser.close()

    candidates = interleave(per_game)

    manifest = {
        "version": 11,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "creator_cap_24h": MAX_PUBLIC_UPLOADS_PER_CREATOR_24H,
        "game_cap_24h": MAX_PUBLIC_UPLOADS_PER_GAME_24H,
        "max_clips_per_game": MAX_CLIPS_PER_GAME,
        "max_total_candidates": MAX_TOTAL_CANDIDATES,
        "games_with_candidates": list(per_game.keys()),
        "candidate_count": len(candidates),
        "fresh_candidate_target": MAX_TOTAL_CANDIDATES,
        "fresh_target_reached": len(candidates) >= MAX_TOTAL_CANDIDATES,
        "skipped_published_shorts": skipped_published,
        "skipped_rejected_shorts": skipped_shorts_rejected,
        "skipped_longform_history": skipped_longform_used,
        "skipped_longform_rejected": skipped_longform_rejected,
        "candidates": candidates,
    }

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print(f"Games with candidates: {len(per_game)}")
    print(f"Fresh batch candidate count: {len(candidates)}")
    print(f"Skipped already-published Shorts during discovery: {skipped_published}")
    print(f"Skipped permanently rejected Shorts during discovery: {skipped_shorts_rejected}")

    if LONGFORM_MODE:
        print(f"Skipped previously used long-form clips: {skipped_longform_used}")
        print(f"Skipped permanently rejected long-form clips: {skipped_longform_rejected}")

    if len(candidates) < MAX_TOTAL_CANDIDATES:
        print(
            "NOTICE: Kick exposed fewer fresh eligible clips than the "
            "requested target. Continuing with every fresh clip found."
        )

    if not candidates:
        raise RuntimeError(
            "V11 discovery found no eligible fresh game-category clips."
        )

    print("V11 batch discovery complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"V11 DISCOVERY FAILED: {exc}")
        sys.exit(1)
