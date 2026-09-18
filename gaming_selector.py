import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests


CONFIG_PATH = Path("config.json")
HISTORY_PATH = Path("history.json")
OUTPUT_PATH = Path("work/production/gaming_selection.json")

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"
TWITCH_CLIPS_URL = "https://api.twitch.tv/helix/clips"


GAMBLING_TERMS = {
    "casino",
    "gambling",
    "slots",
    "slot",
    "roulette",
    "blackjack",
    "sportsbook",
    "sports betting",
    "betting",
    "jackpot",
    "stake",
    "stake.com",
    "wager",
    "wagering",
}

GAMING_TERMS = {
    "game",
    "gaming",
    "ranked",
    "clutch",
    "kill",
    "kills",
    "win",
    "wins",
    "boss",
    "speedrun",
    "record",
    "rage",
    "fail",
    "fails",
    "glitch",
    "challenge",
    "1v1",
    "ace",
    "headshot",
    "round",
    "match",
    "squad",
    "duo",
    "trio",
    "controller",
    "keyboard",
    "console",
    "pc",
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
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )


def load_config():
    config = load_json(CONFIG_PATH, {})

    if not config:
        raise RuntimeError("config.json could not be loaded.")

    return config


def load_history():
    history = load_json(
        HISTORY_PATH,
        {
            "processed": [],
            "updated_at": None,
        },
    )

    if isinstance(history, list):
        history = {
            "processed": history,
            "updated_at": None,
        }

    history.setdefault("processed", [])

    return history


def normalize(text):
    text = str(text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def contains_any(text, terms):
    normalized = normalize(text)

    for term in terms:
        if normalize(term) in normalized:
            return True

    return False


def clip_fingerprint(clip):
    """
    Creates our own stable duplicate identifier.

    Twitch clip ID is preferred, but the fallback protects us
    if another platform/source uses a different identifier.
    """

    platform = str(clip.get("platform", "")).lower()
    clip_id = str(clip.get("id", "")).strip()

    if clip_id:
        raw = f"{platform}:{clip_id}"
    else:
        raw = "|".join(
            [
                platform,
                str(clip.get("creator_name", "")),
                str(clip.get("title", "")),
                str(clip.get("created_at", "")),
                str(clip.get("url", "")),
            ]
        )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def history_fingerprints(history):
    fingerprints = set()

    for item in history.get("processed", []):
        if isinstance(item, dict):
            fp = item.get("fingerprint")

            if fp:
                fingerprints.add(fp)

    return fingerprints


def twitch_app_token():
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET is missing."
        )

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

    return (
        response.json()["access_token"],
        client_id,
    )


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

    if not data:
        return None

    return data[0]


def get_twitch_clips(user_id, headers, first=100):
    response = requests.get(
        TWITCH_CLIPS_URL,
        headers=headers,
        params={
            "broadcaster_id": user_id,
            "first": first,
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json().get("data", [])


def convert_twitch_clip(raw, streamer):
    return {
        "platform": "twitch",
        "id": raw.get("id"),
        "url": raw.get("url"),
        "embed_url": raw.get("embed_url"),
        "title": raw.get("title", ""),
        "creator_name": raw.get("creator_name", ""),
        "broadcaster_name": raw.get(
            "broadcaster_name",
            streamer,
        ),
        "game_id": raw.get("game_id"),
        "language": raw.get("language"),
        "views": int(raw.get("view_count", 0)),
        "created_at": raw.get("created_at"),
        "duration": float(raw.get("duration", 0)),
        "thumbnail_url": raw.get("thumbnail_url"),
        "reuse_allowed": False,
        "download_allowed": False,
        "permission_url": None,
        "production_authorized": False,
        "discovery_only": True,
    }


def reject_reason(clip, config, fingerprints):
    """
    Hard filters.

    Returning None means the candidate survives.
    """

    fp = clip_fingerprint(clip)

    if fp in fingerprints:
        return "duplicate"

    combined_text = " ".join(
        [
            str(clip.get("title", "")),
            str(clip.get("creator_name", "")),
            str(clip.get("broadcaster_name", "")),
        ]
    )

    filters = config.get("content_filters", {})

    if filters.get("reject_gambling", True):
        if contains_any(combined_text, GAMBLING_TERMS):
            return "gambling_or_casino"

    duration = float(clip.get("duration", 0))

    # Extremely tiny clips generally do not give V4 enough story.
    if duration and duration < 8:
        return "too_short"

    # Avoid absurdly long discovery candidates.
    if duration > 180:
        return "too_long"

    return None


def recency_score(created_at):
    if not created_at:
        return 0.0

    try:
        created = datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        )

        now = datetime.now(timezone.utc)
        age_hours = max(
            0,
            (now - created).total_seconds() / 3600,
        )

        # Recent clips get a meaningful advantage,
        # but older viral clips can still compete.
        return max(
            0.0,
            30.0 - min(age_hours / 24.0, 30.0),
        )

    except Exception:
        return 0.0


def view_score(views):
    views = max(int(views or 0), 0)

    if views == 0:
        return 0.0

    # Log scale prevents one giant clip from completely
    # overwhelming every other signal.
    return min(
        40.0,
        math.log10(views + 1) * 8.0,
    )


def title_score(title):
    title = normalize(title)

    score = 0.0

    high_value_terms = {
        "clutch": 8,
        "insane": 7,
        "crazy": 6,
        "rage": 6,
        "fail": 6,
        "fails": 6,
        "record": 8,
        "world record": 10,
        "speedrun": 8,
        "1v1": 5,
        "ace": 7,
        "glitch": 7,
        "boss": 5,
        "win": 4,
        "wins": 4,
        "no way": 6,
        "wtf": 3,
    }

    for term, points in high_value_terms.items():
        if term in title:
            score += points

    if contains_any(title, GAMING_TERMS):
        score += 5

    return min(score, 20.0)


def duration_score(duration):
    duration = float(duration or 0)

    if 20 <= duration <= 60:
        return 10.0

    if 12 <= duration < 20:
        return 7.0

    if 60 < duration <= 90:
        return 7.0

    if 8 <= duration < 12:
        return 4.0

    if 90 < duration <= 180:
        return 3.0

    return 0.0


def score_clip(clip):
    score = 0.0

    score += view_score(
        clip.get("views", 0)
    )

    score += recency_score(
        clip.get("created_at")
    )

    score += title_score(
        clip.get("title", "")
    )

    score += duration_score(
        clip.get("duration", 0)
    )

    return round(score, 2)


def discover_twitch(config):
    streamers = config.get(
        "discovery_streamers",
        [],
    )

    if not streamers:
        print("No Twitch discovery streamers configured.")
        return []

    token, client_id = twitch_app_token()

    headers = twitch_headers(
        token,
        client_id,
    )

    candidates = []

    for streamer in streamers:
        print(f"Discovering gaming clips: {streamer}")

        try:
            user = get_twitch_user(
                streamer,
                headers,
            )

            if not user:
                print(
                    f"  Could not find Twitch user: {streamer}"
                )
                continue

            clips = get_twitch_clips(
                user["id"],
                headers,
                first=100,
            )

            print(
                f"  Found {len(clips)} public clips."
            )

            for raw in clips:
                candidates.append(
                    convert_twitch_clip(
                        raw,
                        streamer,
                    )
                )

        except Exception as exc:
            print(
                f"  Discovery error for {streamer}: {exc}"
            )

    return candidates


def production_sources_from_config(config):
    """
    Future authorized gaming sources go here.

    This deliberately does NOT turn Twitch discovery clips
    into production clips.
    """

    sources = config.get(
        "production_sources",
        [],
    )

    candidates = []

    for source in sources:
        if not isinstance(source, dict):
            continue

        candidate = dict(source)

        candidate.setdefault(
            "platform",
            "unknown",
        )

        candidate.setdefault(
            "reuse_allowed",
            False,
        )

        candidate.setdefault(
            "download_allowed",
            False,
        )

        candidate["production_authorized"] = bool(
            candidate.get("reuse_allowed")
            and candidate.get("download_allowed")
        )

        candidate["discovery_only"] = False

        candidates.append(candidate)

    return candidates


def evaluate_candidates(
    candidates,
    config,
    history,
):
    fingerprints = history_fingerprints(
        history
    )

    accepted = []
    rejected = []

    for clip in candidates:
        clip = dict(clip)

        clip["fingerprint"] = (
            clip_fingerprint(clip)
        )

        reason = reject_reason(
            clip,
            config,
            fingerprints,
        )

        if reason:
            clip["rejected_reason"] = reason
            rejected.append(clip)
            continue

        clip["gaming_score"] = (
            score_clip(clip)
        )

        accepted.append(clip)

    accepted.sort(
        key=lambda x: x.get(
            "gaming_score",
            0,
        ),
        reverse=True,
    )

    return accepted, rejected


def choose_production_candidate(
    accepted,
):
    """
    Critical rights gate.

    A high gaming score NEVER overrides rights.
    """

    for clip in accepted:
        if (
            clip.get("production_authorized")
            and clip.get("reuse_allowed")
            and clip.get("download_allowed")
        ):
            return clip

    return None


def print_leaderboard(accepted):
    print()
    print("======================================")
    print("ViralSpawnTV Gaming Discovery Rankings")
    print("======================================")

    if not accepted:
        print("No usable discovery candidates.")
        return

    for index, clip in enumerate(
        accepted[:15],
        start=1,
    ):
        rights = (
            "AUTHORIZED"
            if clip.get("production_authorized")
            else "DISCOVERY ONLY"
        )

        print()
        print(
            f"{index}. "
            f"{clip.get('broadcaster_name', 'Unknown')}"
        )

        print(
            f"   Score: {clip.get('gaming_score')}"
        )

        print(
            f"   Views: {clip.get('views', 0):,}"
        )

        print(
            f"   Title: {clip.get('title', '')}"
        )

        print(
            f"   Rights: {rights}"
        )

        print(
            f"   URL: {clip.get('url', '')}"
        )


def main():
    print()
    print("======================================")
    print("ViralSpawnTV Gaming Selector")
    print("======================================")
    print("Gaming-only mode: ON")
    print("Gambling rejection: ON")
    print("Duplicate protection: ON")
    print("Rights gate: ON")
    print()

    config = load_config()
    history = load_history()

    twitch_candidates = discover_twitch(
        config
    )

    authorized_candidates = (
        production_sources_from_config(
            config
        )
    )

    all_candidates = (
        twitch_candidates
        + authorized_candidates
    )

    accepted, rejected = (
        evaluate_candidates(
            all_candidates,
            config,
            history,
        )
    )

    print_leaderboard(accepted)

    production_candidate = (
        choose_production_candidate(
            accepted
        )
    )

    result = {
        "generated_at": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
        "niche": "gaming",
        "candidate_count": len(
            all_candidates
        ),
        "accepted_count": len(
            accepted
        ),
        "rejected_count": len(
            rejected
        ),
        "production_candidate": (
            production_candidate
        ),
        "top_discovery_candidates": (
            accepted[:25]
        ),
        "rejected": rejected[:50],
    }

    save_json(
        OUTPUT_PATH,
        result,
    )

    print()
    print(
        f"Selection report saved to: "
        f"{OUTPUT_PATH}"
    )

    if production_candidate:
        print()
        print("======================================")
        print("AUTHORIZED GAMING CLIP SELECTED")
        print("======================================")
        print(
            production_candidate.get(
                "title",
                "",
            )
        )
        print(
            production_candidate.get(
                "url",
                "",
            )
        )

    else:
        print()
        print("======================================")
        print("NO AUTHORIZED PRODUCTION CLIP")
        print("======================================")
        print(
            "Gaming clips were discovered and ranked, "
            "but none passed the production-rights gate."
        )
        print()
        print(
            "This is intentional. ViralSpawnTV will "
            "not turn discovery-only footage into a "
            "production video."
        )


if __name__ == "__main__":
    main()
