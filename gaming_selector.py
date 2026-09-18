import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests


CONFIG_PATH = Path("config.json")
SOURCES_PATH = Path("gaming_sources.json")
HISTORY_PATH = Path("history.json")
OUTPUT_PATH = Path("work/production/gaming_selection.json")

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"
TWITCH_CLIPS_URL = "https://api.twitch.tv/helix/clips"


GAMBLING_TERMS = {
    "casino", "gambling", "slots", "slot", "roulette",
    "blackjack", "sportsbook", "sports betting", "betting",
    "jackpot", "stake", "stake.com", "wager", "wagering"
}

GAMING_TERMS = {
    "game", "gaming", "ranked", "clutch", "kill", "kills",
    "win", "wins", "boss", "speedrun", "record", "rage",
    "fail", "fails", "glitch", "challenge", "1v1", "ace",
    "headshot", "round", "match", "squad", "duo", "trio",
    "controller", "keyboard", "console", "pc"
}


def load_json(path, default):
    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def normalize(text):
    text = str(text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def contains_any(text, terms):
    text = normalize(text)
    return any(normalize(term) in text for term in terms)


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except Exception:
        return None


def clip_fingerprint(clip):
    platform = str(clip.get("platform", "")).lower()
    clip_id = str(clip.get("id", "")).strip()

    if clip_id:
        raw = f"{platform}:{clip_id}"
    else:
        raw = "|".join([
            platform,
            str(clip.get("creator_name", "")),
            str(clip.get("title", "")),
            str(clip.get("created_at", "")),
            str(clip.get("url", "")),
        ])

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_history():
    data = load_json(HISTORY_PATH, {"processed": []})

    if isinstance(data, list):
        data = {"processed": data}

    data.setdefault("processed", [])
    return data


def processed_fingerprints(history):
    result = set()

    for item in history.get("processed", []):
        if isinstance(item, dict) and item.get("fingerprint"):
            result.add(item["fingerprint"])

    return result


# ---------------------------------------------------------
# TWITCH DISCOVERY
# ---------------------------------------------------------

def twitch_app_token():
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError("Twitch credentials are missing.")

    response = requests.post(
        TWITCH_TOKEN_URL,
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json()["access_token"], client_id


def twitch_headers(token, client_id):
    return {
        "Authorization": f"Bearer {token}",
        "Client-Id": client_id,
    }


def get_twitch_user(login, headers):
    response = requests.get(
        TWITCH_USERS_URL,
        headers=headers,
        params={"login": login},
        timeout=30,
    )

    response.raise_for_status()
    data = response.json().get("data", [])

    return data[0] if data else None


def get_twitch_clips(user_id, headers):
    response = requests.get(
        TWITCH_CLIPS_URL,
        headers=headers,
        params={
            "broadcaster_id": user_id,
            "first": 100,
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.json().get("data", [])


def discover_twitch(config):
    streamers = config.get("discovery_streamers", [])

    token, client_id = twitch_app_token()
    headers = twitch_headers(token, client_id)

    candidates = []

    for streamer in streamers:
        print(f"Discovering: {streamer}")

        try:
            user = get_twitch_user(streamer, headers)

            if not user:
                print("  Twitch user not found.")
                continue

            raw_clips = get_twitch_clips(user["id"], headers)

            print(f"  Found {len(raw_clips)} clips.")

            for raw in raw_clips:
                candidates.append({
                    "platform": "twitch",
                    "id": raw.get("id"),
                    "url": raw.get("url"),
                    "title": raw.get("title", ""),
                    "creator_name": raw.get("creator_name", ""),
                    "broadcaster_name": raw.get(
                        "broadcaster_name",
                        streamer
                    ),
                    "game_id": raw.get("game_id"),
                    "views": int(raw.get("view_count", 0)),
                    "created_at": raw.get("created_at"),
                    "duration": float(raw.get("duration", 0)),
                    "thumbnail_url": raw.get("thumbnail_url"),

                    # Discovery is NOT production permission.
                    "production_authorized": False,
                    "discovery_only": True,
                })

        except Exception as exc:
            print(f"  Error: {exc}")

    return candidates


# ---------------------------------------------------------
# GAMING SCORING
# ---------------------------------------------------------

def view_score(views):
    views = max(int(views or 0), 0)

    if not views:
        return 0

    return min(
        40,
        math.log10(views + 1) * 8
    )


def recency_score(created_at):
    created = parse_time(created_at)

    if not created:
        return 0

    age_days = max(
        0,
        (
            datetime.now(timezone.utc) - created
        ).total_seconds() / 86400
    )

    return max(0, 30 - min(age_days, 30))


def title_score(title):
    title = normalize(title)
    score = 0

    signals = {
        "clutch": 8,
        "insane": 7,
        "crazy": 6,
        "rage": 6,
        "fail": 6,
        "record": 8,
        "world record": 10,
        "speedrun": 8,
        "1v1": 5,
        "ace": 7,
        "glitch": 7,
        "boss": 5,
        "no way": 6,
    }

    for term, points in signals.items():
        if term in title:
            score += points

    if contains_any(title, GAMING_TERMS):
        score += 5

    return min(score, 20)


def duration_score(duration):
    duration = float(duration or 0)

    if 20 <= duration <= 60:
        return 10
    if 12 <= duration < 20:
        return 7
    if 60 < duration <= 90:
        return 7
    if 8 <= duration < 12:
        return 4
    if 90 < duration <= 180:
        return 3

    return 0


def score_clip(clip):
    return round(
        view_score(clip.get("views"))
        + recency_score(clip.get("created_at"))
        + title_score(clip.get("title"))
        + duration_score(clip.get("duration")),
        2,
    )


# ---------------------------------------------------------
# RIGHTS ENGINE
# ---------------------------------------------------------

def evaluate_source(source):
    reasons = []

    if not source.get("creator_clipping_allowed"):
        reasons.append("creator_clipping_not_allowed")

    if not source.get("platform_monetization_allowed"):
        reasons.append("platform_monetization_not_allowed")

    if not source.get("commercial_use_allowed"):
        reasons.append("commercial_use_not_allowed")

    if source.get("third_party_game_rights_check"):
        reasons.append("game_rights_must_be_checked_per_clip")

    if not source.get("authorized_acquisition_method"):
        reasons.append("no_authorized_acquisition_method")

    if not source.get("production_enabled"):
        reasons.append("production_disabled")

    return {
        "source_id": source.get("id"),
        "creator": source.get("creator"),
        "organization": source.get("organization"),
        "permission_url": source.get("permission_url"),
        "waiting_period_hours": int(
            source.get("waiting_period_hours", 0)
        ),
        "authorized_acquisition_method":
            source.get("authorized_acquisition_method"),
        "production_enabled":
            bool(source.get("production_enabled")),
        "eligible_for_unattended_production":
            len(reasons) == 0,
        "blocking_reasons": reasons,
    }


def build_rights_report(source_db):
    report = []

    for source in source_db.get("sources", []):
        report.append(evaluate_source(source))

    return report


def source_wait_period_passed(source, clip_created_at):
    hours = int(source.get("waiting_period_hours", 0))

    if hours <= 0:
        return True

    created = parse_time(clip_created_at)

    if not created:
        return False

    age_hours = (
        datetime.now(timezone.utc) - created
    ).total_seconds() / 3600

    return age_hours >= hours


def source_can_produce(source, clip=None):
    if not source.get("creator_clipping_allowed"):
        return False

    if not source.get("platform_monetization_allowed"):
        return False

    if not source.get("commercial_use_allowed"):
        return False

    if not source.get("authorized_acquisition_method"):
        return False

    if not source.get("production_enabled"):
        return False

    # We deliberately do NOT automatically pass the
    # third-party game-rights requirement.
    if source.get("third_party_game_rights_check"):
        if not clip:
            return False

        if not clip.get("game_rights_verified"):
            return False

    if clip and not source_wait_period_passed(
        source,
        clip.get("created_at")
    ):
        return False

    return True


# ---------------------------------------------------------
# CONTENT FILTERS / DUPLICATES
# ---------------------------------------------------------

def hard_rejection_reason(clip, fingerprints):
    fp = clip_fingerprint(clip)

    if fp in fingerprints:
        return "duplicate"

    text = " ".join([
        str(clip.get("title", "")),
        str(clip.get("creator_name", "")),
        str(clip.get("broadcaster_name", "")),
    ])

    if contains_any(text, GAMBLING_TERMS):
        return "gambling_or_casino"

    duration = float(clip.get("duration", 0))

    if duration and duration < 8:
        return "too_short"

    if duration > 180:
        return "too_long"

    return None


def evaluate_discovery(clips, history):
    fingerprints = processed_fingerprints(history)

    accepted = []
    rejected = []

    for clip in clips:
        clip = dict(clip)
        clip["fingerprint"] = clip_fingerprint(clip)

        reason = hard_rejection_reason(
            clip,
            fingerprints
        )

        if reason:
            clip["rejected_reason"] = reason
            rejected.append(clip)
            continue

        clip["gaming_score"] = score_clip(clip)
        accepted.append(clip)

    accepted.sort(
        key=lambda item: item["gaming_score"],
        reverse=True
    )

    return accepted, rejected


# ---------------------------------------------------------
# ATTRIBUTION
# ---------------------------------------------------------

def build_attribution(
    creator,
    original_title,
    source_url,
):
    return {
        "featured_creator": creator,
        "original_title": original_title,
        "source_url": source_url,
        "youtube_description_block": (
            "Original commentary and editing by ViralSpawnTV.\n\n"
            f"Featured creator: {creator}\n"
            f"Original stream/video: {original_title}\n"
            f"Source: {source_url}\n\n"
            "ViralSpawnTV transforms gaming moments with "
            "original commentary, narration, captions and editing."
        ),
    }


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    print()
    print("=======================================")
    print("ViralSpawnTV Gaming Rights Selector V2")
    print("=======================================")
    print("Gaming only: YES")
    print("Gambling: BLOCKED")
    print("Duplicate protection: ON")
    print("Creator rights gate: ON")
    print("Acquisition rights gate: ON")
    print("Game-rights gate: ON")
    print()

    config = load_json(CONFIG_PATH, {})
    source_db = load_json(SOURCES_PATH, {})
    history = load_history()

    if not config:
        raise RuntimeError("config.json missing or invalid.")

    if not source_db:
        raise RuntimeError(
            "gaming_sources.json missing or invalid."
        )

    rights_report = build_rights_report(source_db)

    print("SOURCE RIGHTS STATUS")
    print("---------------------------------------")

    for item in rights_report:
        name = (
            item.get("creator")
            or item.get("organization")
            or item.get("source_id")
        )

        print()
        print(name)

        if item["eligible_for_unattended_production"]:
            print("  STATUS: PRODUCTION READY")
        else:
            print("  STATUS: NOT PRODUCTION READY")

            for reason in item["blocking_reasons"]:
                print(f"   - {reason}")

    print()
    print("TWITCH GAMING DISCOVERY")
    print("---------------------------------------")

    discovery = discover_twitch(config)

    accepted, rejected = evaluate_discovery(
        discovery,
        history
    )

    print()
    print("TOP GAMING DISCOVERY")
    print("---------------------------------------")

    for index, clip in enumerate(
        accepted[:15],
        start=1
    ):
        print()
        print(
            f"{index}. "
            f"{clip.get('broadcaster_name', 'Unknown')}"
        )
        print(f"   Score: {clip['gaming_score']}")
        print(f"   Views: {clip.get('views', 0):,}")
        print(f"   Title: {clip.get('title', '')}")
        print("   Production: DISCOVERY ONLY")

    production_ready_sources = []

    for source in source_db.get("sources", []):
        # No clip supplied yet, therefore any source requiring
        # per-game verification correctly remains blocked here.
        if source_can_produce(source):
            production_ready_sources.append(source)

    result = {
        "generated_at":
            datetime.now(timezone.utc).isoformat(),

        "channel": "ViralSpawnTV",
        "niche": "gaming",

        "discovery": {
            "total": len(discovery),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "top_candidates": accepted[:25],
        },

        "rights_sources": rights_report,

        "production_ready_source_count":
            len(production_ready_sources),

        "production_candidate": None,

        "next_required_step": (
            "Configure an authorized acquisition adapter "
            "and verify underlying game rights before enabling "
            "a source for production."
        ),
    }

    save_json(OUTPUT_PATH, result)

    print()
    print("=======================================")

    if production_ready_sources:
        print("PRODUCTION SOURCES AVAILABLE")
    else:
        print("NO SOURCE CLEARED FOR PRODUCTION")

    print("=======================================")

    print(
        f"\nReport saved to {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
