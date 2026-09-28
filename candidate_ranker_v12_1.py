import bisect
import json
import math
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


MANIFEST = Path("work/v11_candidate_manifest.json")
OUT = Path("work/v12_ranked_candidates.json")
SHORTS_HISTORY = Path("history.json")
SHORTS_REJECTED_HISTORY = Path("shorts_rejected_history.json")

# V12.14.6 QUALITY-FIRST TRACTION RANKER
MAX_INSPECT = 320
MAX_RANKED = 100
MIN_SOURCE_SECONDS = 40.0

API_WORKERS = 16
API_TIMEOUT_SECONDS = 10
API_RETRIES = 2

KICK_API_TEMPLATE = (
    "https://kick.com/api/v2/"
    "clips/{clip_id}/play"
)

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept":
        "application/json, text/plain, */*",
    "Referer":
        "https://kick.com/",
    "Origin":
        "https://kick.com",
    "X-Requested-With":
        "XMLHttpRequest",
}

BLOCKED = {
    "casino",
    "gambling",
    "roulette",
    "blackjack",
    "sportsbook",
    "betting",
    "slots",
    "slot machine",
    "stake",
    "crypto casino",
    "prediction market",
}

MUSIC_BLOCKED = {
    "music video",
    "official audio",
    "karaoke",
    "lyrics",
    "singing",
    "remix",
    "soundtrack",
}

ACTION_TERMS = {
    "clutch",
    "1v1",
    "1v2",
    "1v3",
    "1v4",
    "1v5",
    "ace",
    "kill",
    "kills",
    "elimination",
    "elim",
    "knock",
    "down",
    "wipe",
    "wiped",
    "squad wipe",
    "last alive",
    "solo",
    "win",
    "victory",
    "champion",
    "comeback",
    "overtime",
    "last second",
    "final circle",
    "endgame",
    "headshot",
    "sniper",
    "noscope",
    "no scope",
    "360",
    "air dribble",
    "goal",
    "rage",
    "scream",
    "reaction",
    "funny",
    "hilarious",
    "fail",
    "failed",
    "jumpscare",
    "jump scare",
    "chase",
    "escape",
    "save",
    "rescue",
    "ambush",
    "boss",
    "record",
    "speedrun",
    "1 hp",
    "one hp",
    "quad",
    "triple",
    "double",
    "crazy",
    "insane",
}

STAKE_TERMS = {
    "last",
    "final",
    "overtime",
    "match point",
    "game point",
    "ranked",
    "1 hp",
    "one hp",
    "solo",
    "last alive",
    "final circle",
    "endgame",
    "comeback",
    "record",
    "boss",
}

# Small category prior only. Real audience traction dominates.
GAME_PRIOR = {
    "call of duty: warzone": 100,
    "valorant": 96,
    "counter-strike 2": 96,
    "fortnite": 93,
    "apex legends": 93,
    "marvel rivals": 92,
    "rocket league": 92,
    "overwatch 2": 88,
    "call of duty: black ops 7": 92,
    "dead by daylight": 84,
    "escape from tarkov": 84,
    "rust": 82,
    "grand theft auto v (gta)": 80,
    "league of legends": 82,
    "minecraft": 72,
    "roblox": 70,
}

MAX_PER_CREATOR_IN_RANKED = 5
MAX_PER_GAME_IN_RANKED = 24


def norm(value):
    return re.sub(
        r"\s+",
        " ",
        str(
            value or ""
        ),
    ).strip()


def normalize_clip_id(value):
    return str(
        value or ""
    ).strip().casefold()


def normalize_clip_url(value):
    raw = str(
        value or ""
    ).strip()

    if not raw:
        return ""

    try:
        parts = urlsplit(
            raw
        )

        netloc = (
            parts.netloc.lower()
        )

        if netloc.startswith(
            "www."
        ):
            netloc = netloc[
                4:
            ]

        path = re.sub(
            r"/+",
            "/",
            parts.path,
        ).rstrip("/").casefold()

        return urlunsplit(
            (
                (
                    parts.scheme
                    or
                    "https"
                ).lower(),
                netloc,
                path,
                "",
                "",
            )
        )

    except Exception:
        return (
            raw
            .rstrip("/")
            .casefold()
        )


def load_json(path):
    if not path.exists():
        return {}

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:
        print(
            f"WARNING: could not parse "
            f"{path}: {exc}"
        )

        return {}


def extract_identity_sets(
    data,
    keys,
):
    ids = set()
    urls = set()

    if isinstance(
        data,
        list,
    ):
        rows = data

    elif isinstance(
        data,
        dict,
    ):
        rows = []

        for key in keys:
            value = data.get(
                key
            )

            if isinstance(
                value,
                list,
            ):
                rows.extend(
                    value
                )

    else:
        rows = []

    for item in rows:
        if isinstance(
            item,
            str,
        ):
            raw = item.strip()

            if raw.lower().startswith(
                (
                    "http://",
                    "https://",
                )
            ):
                urls.add(
                    normalize_clip_url(
                        raw
                    )
                )

            else:
                ids.add(
                    normalize_clip_id(
                        raw
                    )
                )

            continue

        if not isinstance(
            item,
            dict,
        ):
            continue

        cid = normalize_clip_id(
            item.get(
                "clip_id"
            )
            or
            item.get(
                "id"
            )
        )

        curl = normalize_clip_url(
            item.get(
                "clip_url"
            )
            or
            item.get(
                "source_url"
            )
            or
            item.get(
                "url"
            )
            or
            item.get(
                "source"
            )
        )

        if cid:
            ids.add(
                cid
            )

        if curl:
            urls.add(
                curl
            )

    return (
        ids,
        urls,
    )


def terms_found(
    text,
    terms,
):
    lowered = str(
        text or ""
    ).casefold()

    return sorted(
        term
        for term in terms
        if term in lowered
    )


def to_int(value):
    try:
        return max(
            0,
            int(
                value
            ),
        )

    except Exception:
        return 0


def to_float(value):
    try:
        return float(
            value
        )

    except Exception:
        return None


def parse_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(
                value
            ).replace(
                "Z",
                "+00:00",
            )
        ).astimezone(
            timezone.utc
        )

    except Exception:
        return None


def fetch_json_urllib(url):
    request = urllib.request.Request(
        url,
        headers=API_HEADERS,
        method="GET",
    )

    with urllib.request.urlopen(
        request,
        timeout=API_TIMEOUT_SECONDS,
    ) as response:
        return json.loads(
            response
            .read()
            .decode(
                "utf-8",
                errors="replace",
            )
        )


def fetch_json_curl(url):
    command = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--max-time",
        str(
            API_TIMEOUT_SECONDS
        ),
        "-H",
        f"User-Agent: "
        f"{API_HEADERS['User-Agent']}",
        "-H",
        f"Accept: "
        f"{API_HEADERS['Accept']}",
        "-H",
        f"Referer: "
        f"{API_HEADERS['Referer']}",
        "-H",
        f"Origin: "
        f"{API_HEADERS['Origin']}",
        "-H",
        "X-Requested-With: XMLHttpRequest",
        url,
    ]

    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )

    return json.loads(
        result.stdout
    )


def fetch_kick_clip_api(candidate):
    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    ).strip()

    if not clip_id:
        raise RuntimeError(
            "missing clip_id"
        )

    url = KICK_API_TEMPLATE.format(
        clip_id=clip_id
    )

    errors = []

    for attempt in range(
        1,
        API_RETRIES + 1,
    ):
        try:
            return fetch_json_urllib(
                url
            )

        except Exception as exc:
            errors.append(
                f"urllib#{attempt}: {exc}"
            )

        try:
            return fetch_json_curl(
                url
            )

        except Exception as exc:
            errors.append(
                f"curl#{attempt}: {exc}"
            )

        if attempt < API_RETRIES:
            time.sleep(
                0.25
                *
                attempt
            )

    raise RuntimeError(
        " | ".join(
            errors[
                -4:
            ]
        )
    )


def inspect_candidate(candidate):
    payload = fetch_kick_clip_api(
        candidate
    )

    clip = payload.get(
        "clip"
    )

    if not isinstance(
        clip,
        dict,
    ):
        raise RuntimeError(
            "Kick response missing clip object"
        )

    media_url = str(
        clip.get(
            "clip_url",
            "",
        )
    ).strip()

    duration = to_float(
        clip.get(
            "duration"
        )
    )

    title = norm(
        clip.get(
            "title",
            "",
        )
    )

    channel_obj = clip.get(
        "channel"
    )

    if not isinstance(
        channel_obj,
        dict,
    ):
        channel_obj = {}

    category_obj = clip.get(
        "category"
    )

    if not isinstance(
        category_obj,
        dict,
    ):
        category_obj = {}

    creator_obj = clip.get(
        "creator"
    )

    if not isinstance(
        creator_obj,
        dict,
    ):
        creator_obj = {}

    api_channel = norm(
        channel_obj.get(
            "slug",
            "",
        )
    ).lower()

    api_category = norm(
        category_obj.get(
            "name",
            "",
        )
    )

    creator_username = norm(
        creator_obj.get(
            "username",
            "",
        )
    )

    views = to_int(
        clip.get(
            "views"
        )
    )

    likes = to_int(
        clip.get(
            "likes"
        )
    )

    like_rate_pct = (
        (
            likes
            /
            max(
                1,
                views,
            )
        )
        *
        100.0
        if views > 0
        else 0.0
    )

    created_at_raw = clip.get(
        "created_at"
    )

    created_at = parse_datetime(
        created_at_raw
    )

    if created_at is None:
        age_hours = None
        views_per_hour = 0.0

    else:
        age_hours = max(
            0.25,
            (
                datetime.now(
                    timezone.utc
                )
                -
                created_at
            ).total_seconds()
            /
            3600.0,
        )

        views_per_hour = (
            views
            /
            age_hours
        )

    metadata_text = norm(
        " ".join(
            [
                title,
                api_category,
                api_channel,
                creator_username,
            ]
        )
    )

    row = dict(
        candidate
    )

    row.update(
        {
            "page_title":
                title,
            "page_description":
                "",
            "metadata_text":
                metadata_text,
            "bad_hits":
                terms_found(
                    metadata_text,
                    BLOCKED,
                ),
            "music_hits":
                terms_found(
                    metadata_text,
                    MUSIC_BLOCKED,
                ),
            "action_hits":
                terms_found(
                    metadata_text,
                    ACTION_TERMS,
                ),
            "stake_hits":
                terms_found(
                    metadata_text,
                    STAKE_TERMS,
                ),
            "media_url":
                media_url,
            "playlist_url":
                media_url,
            "source_duration_seconds":
                (
                    round(
                        duration,
                        3,
                    )
                    if duration is not None
                    else None
                ),
            "kick_api_category":
                api_category,
            "kick_api_channel":
                api_channel,
            "kick_creator_username":
                creator_username,
            "kick_view_count":
                views,
            "kick_like_count":
                likes,
            "kick_like_rate_pct":
                round(
                    like_rate_pct,
                    4,
                ),
            "kick_created_at":
                created_at_raw,
            "kick_age_hours":
                (
                    round(
                        age_hours,
                        3,
                    )
                    if age_hours is not None
                    else None
                ),
            "kick_views_per_hour":
                round(
                    views_per_hour,
                    4,
                ),
            "kick_thumbnail_url":
                clip.get(
                    "thumbnail_url"
                ),
            "kick_is_mature":
                bool(
                    clip.get(
                        "is_mature",
                        False,
                    )
                ),
            "metadata_source":
                "kick_direct_api",
        }
    )

    if api_channel:
        row[
            "channel"
        ] = api_channel

    return row


def percentile_scores(
    rows,
    key,
):
    values = sorted(
        float(
            row.get(
                key,
                0,
            )
            or
            0
        )
        for row in rows
    )

    if not values:
        return {}

    if len(
        values
    ) == 1:
        return {
            id(
                rows[
                    0
                ]
            ):
                100.0
        }

    result = {}

    denominator = max(
        1,
        len(
            values
        )
        -
        1,
    )

    for row in rows:
        value = float(
            row.get(
                key,
                0,
            )
            or
            0
        )

        rank = (
            bisect.bisect_right(
                values,
                value,
            )
            -
            1
        )

        result[
            id(
                row
            )
        ] = (
            rank
            /
            denominator
            *
            100.0
        )

    return result


def freshness_score(
    age_hours,
):
    if age_hours is None:
        return 45.0

    if age_hours <= 6:
        return 100.0

    if age_hours <= 24:
        return 92.0

    if age_hours <= 72:
        return 78.0

    if age_hours <= 168:
        return 62.0

    if age_hours <= 720:
        return 40.0

    return 20.0


def duration_score(
    seconds,
):
    seconds = float(
        seconds
    )

    if 40 <= seconds <= 60:
        return 100.0

    if seconds <= 90:
        return 96.0

    if seconds <= 150:
        return 90.0

    if seconds <= 240:
        return 82.0

    return 68.0


def title_signal_score(row):
    action_count = len(
        row.get(
            "action_hits",
            [],
        )
    )

    stake_count = len(
        row.get(
            "stake_hits",
            [],
        )
    )

    score = (
        action_count
        *
        20.0
        +
        stake_count
        *
        10.0
    )

    if norm(
        row.get(
            "page_title",
            "",
        )
    ):
        score += 5.0

    return min(
        100.0,
        score,
    )


def engagement_score(row):
    views = int(
        row.get(
            "kick_view_count",
            0,
        )
        or
        0
    )

    rate = float(
        row.get(
            "kick_like_rate_pct",
            0,
        )
        or
        0
    )

    # 5%+ like rate is excellent, but tiny-view clips should not get a huge
    # bonus based on one or two likes.
    raw = min(
        100.0,
        (
            rate
            /
            5.0
        )
        *
        100.0,
    )

    reliability = min(
        1.0,
        views
        /
        250.0,
    )

    return (
        raw
        *
        reliability
    )


def score_rows(rows):
    views_pct = percentile_scores(
        rows,
        "kick_view_count",
    )

    likes_pct = percentile_scores(
        rows,
        "kick_like_count",
    )

    vph_pct = percentile_scores(
        rows,
        "kick_views_per_hour",
    )

    for row in rows:
        key = id(
            row
        )

        traction = (
            vph_pct.get(
                key,
                0.0,
            )
            *
            0.45
            +
            views_pct.get(
                key,
                0.0,
            )
            *
            0.35
            +
            likes_pct.get(
                key,
                0.0,
            )
            *
            0.20
        )

        engagement = (
            engagement_score(
                row
            )
        )

        title_signal = (
            title_signal_score(
                row
            )
        )

        freshness = (
            freshness_score(
                row.get(
                    "kick_age_hours"
                )
            )
        )

        duration = (
            duration_score(
                row.get(
                    "source_duration_seconds",
                    0,
                )
            )
        )

        game_name = str(
            row.get(
                "game",
                "",
            )
        ).strip().lower()

        game_prior = GAME_PRIOR.get(
            game_name,
            75.0,
        )

        # Audience evidence deliberately dominates:
        # 50% traction percentile + 15% engagement = 65%.
        score = (
            traction
            *
            0.50
            +
            engagement
            *
            0.15
            +
            title_signal
            *
            0.16
            +
            freshness
            *
            0.08
            +
            duration
            *
            0.06
            +
            game_prior
            *
            0.05
        )

        # Small evidence boosts only.
        if (
            vph_pct.get(
                key,
                0.0,
            )
            >=
            90
        ):
            score += 4.0

        if (
            views_pct.get(
                key,
                0.0,
            )
            >=
            90
        ):
            score += 3.0

        if (
            row.get(
                "kick_view_count",
                0,
            )
            >=
            500
            and
            row.get(
                "kick_like_rate_pct",
                0,
            )
            >=
            3.0
        ):
            score += 4.0

        if len(
            row.get(
                "action_hits",
                [],
            )
        ) >= 2:
            score += 3.0

        score = max(
            0.0,
            min(
                100.0,
                score,
            ),
        )

        row[
            "v12_metadata_score"
        ] = round(
            score,
            2,
        )

        row[
            "v12_metadata_reason"
        ] = (
            "quality-first traction rank: "
            f"views={row.get('kick_view_count', 0)}, "
            f"vph={row.get('kick_views_per_hour', 0):.1f}, "
            f"likes={row.get('kick_like_count', 0)}, "
            f"like_rate={row.get('kick_like_rate_pct', 0):.2f}%, "
            f"action_hits={len(row.get('action_hits', []))}"
        )

        row[
            "v13_traction_score"
        ] = round(
            traction,
            2,
        )

        row[
            "v13_engagement_score"
        ] = round(
            engagement,
            2,
        )

        row[
            "v13_title_signal_score"
        ] = round(
            title_signal,
            2,
        )

        row[
            "v13_freshness_score"
        ] = round(
            freshness,
            2,
        )

        row[
            "v13_views_percentile"
        ] = round(
            views_pct.get(
                key,
                0.0,
            ),
            2,
        )

        row[
            "v13_vph_percentile"
        ] = round(
            vph_pct.get(
                key,
                0.0,
            ),
            2,
        )

    return rows


def select_diverse_ranked(
    rows,
):
    ordered = sorted(
        rows,
        key=lambda row: (
            float(
                row.get(
                    "v12_metadata_score",
                    0,
                )
            ),
            float(
                row.get(
                    "kick_views_per_hour",
                    0,
                )
            ),
            int(
                row.get(
                    "kick_view_count",
                    0,
                )
            ),
        ),
        reverse=True,
    )

    selected = []
    creator_counts = {}
    game_counts = {}

    for row in ordered:
        creator = str(
            row.get(
                "channel",
                "",
            )
        ).strip().lower()

        game = str(
            row.get(
                "game",
                "",
            )
        ).strip().lower()

        if (
            creator
            and
            creator_counts.get(
                creator,
                0,
            )
            >=
            MAX_PER_CREATOR_IN_RANKED
        ):
            continue

        if (
            game
            and
            game_counts.get(
                game,
                0,
            )
            >=
            MAX_PER_GAME_IN_RANKED
        ):
            continue

        selected.append(
            row
        )

        if creator:
            creator_counts[
                creator
            ] = (
                creator_counts.get(
                    creator,
                    0,
                )
                +
                1
            )

        if game:
            game_counts[
                game
            ] = (
                game_counts.get(
                    game,
                    0,
                )
                +
                1
            )

        if len(
            selected
        ) >= MAX_RANKED:
            break

    # If diversity caps left us short, backfill from the raw top order.
    if len(
        selected
    ) < MAX_RANKED:
        selected_ids = {
            str(
                row.get(
                    "clip_id",
                    "",
                )
            )
            for row in selected
        }

        for row in ordered:
            clip_id = str(
                row.get(
                    "clip_id",
                    "",
                )
            )

            if clip_id in selected_ids:
                continue

            selected.append(
                row
            )

            selected_ids.add(
                clip_id
            )

            if len(
                selected
            ) >= MAX_RANKED:
                break

    return selected


def main():
    if not MANIFEST.exists():
        raise RuntimeError(
            "Missing discovery manifest."
        )

    manifest = load_json(
        MANIFEST
    )

    all_candidates = manifest.get(
        "candidates",
        [],
    )

    if not isinstance(
        all_candidates,
        list,
    ):
        all_candidates = []

    history = load_json(
        SHORTS_HISTORY
    )

    rejected = load_json(
        SHORTS_REJECTED_HISTORY
    )

    (
        used_ids,
        used_urls,
    ) = extract_identity_sets(
        history,
        [
            "used_clips",
        ],
    )

    (
        rejected_ids,
        rejected_urls,
    ) = extract_identity_sets(
        rejected,
        [
            "clip_ids",
        ],
    )

    fresh_candidates = []

    seen_ids = set()
    seen_urls = set()

    for candidate in all_candidates:
        if not isinstance(
            candidate,
            dict,
        ):
            continue

        clip_id = normalize_clip_id(
            candidate.get(
                "clip_id"
            )
            or
            candidate.get(
                "id"
            )
        )

        clip_url = normalize_clip_url(
            candidate.get(
                "clip_url"
            )
            or
            candidate.get(
                "url"
            )
        )

        if (
            not clip_id
            or
            not clip_url
        ):
            continue

        if (
            clip_id in used_ids
            or
            clip_url in used_urls
            or
            clip_id in rejected_ids
            or
            clip_url in rejected_urls
        ):
            continue

        if (
            clip_id in seen_ids
            or
            clip_url in seen_urls
        ):
            continue

        seen_ids.add(
            clip_id
        )

        seen_urls.add(
            clip_url
        )

        fresh_candidates.append(
            candidate
        )

    candidates = fresh_candidates[
        :MAX_INSPECT
    ]

    if not candidates:
        raise RuntimeError(
            "No fresh candidates available "
            "for V12.14.6 API ranking."
        )

    print(
        f"V12.14.6 QUALITY-FIRST API: "
        f"querying {len(candidates)} clips "
        f"with {API_WORKERS} workers."
    )

    rows_by_index = {}
    failures = []

    with ThreadPoolExecutor(
        max_workers=API_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                inspect_candidate,
                candidate,
            ): (
                index,
                candidate,
            )
            for index, candidate in enumerate(
                candidates,
                1,
            )
        }

        for future in as_completed(
            future_map
        ):
            index, candidate = (
                future_map[
                    future
                ]
            )

            try:
                rows_by_index[
                    index
                ] = future.result()

            except Exception as exc:
                failures.append(
                    (
                        index,
                        candidate,
                        str(
                            exc
                        ),
                    )
                )

    deterministic = []

    too_short = 0
    blocked = 0
    music = 0
    no_media = 0
    unknown_duration = 0

    for index in sorted(
        rows_by_index
    ):
        row = rows_by_index[
            index
        ]

        if row.get(
            "bad_hits"
        ):
            blocked += 1
            continue

        if row.get(
            "music_hits"
        ):
            music += 1
            continue

        if not row.get(
            "media_url"
        ):
            no_media += 1
            continue

        seconds = row.get(
            "source_duration_seconds"
        )

        if seconds is None:
            unknown_duration += 1
            continue

        if float(
            seconds
        ) < MIN_SOURCE_SECONDS:
            too_short += 1
            continue

        deterministic.append(
            row
        )

    print(
        "V12.14.6 API FILTER: "
        f"{len(rows_by_index)} responses, "
        f"{len(failures)} failures, "
        f"{too_short} under {MIN_SOURCE_SECONDS:.0f}s, "
        f"{blocked} blocked, "
        f"{music} music, "
        f"{no_media} no-media, "
        f"{unknown_duration} unknown-duration."
    )

    if not deterministic:
        raise RuntimeError(
            "No clips survived V12.14.6 "
            "deterministic screening."
        )

    score_rows(
        deterministic
    )

    ranked = select_diverse_ranked(
        deterministic
    )

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "version":
            "12.14.6-100-candidate-traction-rank",
        "strategy":
            "100_candidate_traction_rank_before_active_firefight_scan",
        "source_candidate_count":
            len(
                all_candidates
            ),
        "fresh_candidate_count":
            len(
                fresh_candidates
            ),
        "api_inspected_count":
            len(
                candidates
            ),
        "api_success_count":
            len(
                rows_by_index
            ),
        "api_failure_count":
            len(
                failures
            ),
        "duration_eligible_count":
            len(
                deterministic
            ),
        "ranked_count":
            len(
                ranked
            ),
        "minimum_source_seconds":
            MIN_SOURCE_SECONDS,
        "candidates":
            ranked,
    }

    OUT.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print()
    print(
        f"V12.14.6 QUALITY-FIRST RANK COMPLETE: "
        f"{len(all_candidates)} discovered -> "
        f"{len(deterministic)} duration/content survivors -> "
        f"{len(ranked)} strongest candidates."
    )

    for position, row in enumerate(
        ranked[
            :15
        ],
        1,
    ):
        print(
            f"QUALITY TOP {position}: "
            f"{row.get('clip_id')} | "
            f"game={row.get('game')} | "
            f"score={row.get('v12_metadata_score')} | "
            f"views={row.get('kick_view_count')} | "
            f"vph={row.get('kick_views_per_hour')} | "
            f"likes={row.get('kick_like_count')} | "
            f"like_rate="
            f"{row.get('kick_like_rate_pct'):.2f}% | "
            f"age_h={row.get('kick_age_hours')} | "
            f"action_hits={row.get('action_hits')}"
        )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"V12.14.6 RANKER FAILED: "
            f"{exc}"
        )
        sys.exit(
            1
        )
