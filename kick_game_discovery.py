import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright
from firefight_cache import load as load_firefight_cache, current as cached_firefight_verdict


HISTORY_PATH = Path("history.json")
SHORTS_REJECTED_HISTORY_PATH = Path("shorts_rejected_history.json")
LONGFORM_HISTORY_PATH = Path("longform_history.json")
LONGFORM_REJECTED_HISTORY_PATH = Path("longform_rejected_history.json")
MANIFEST_PATH = Path("work/v11_candidate_manifest.json")

MAX_PUBLIC_UPLOADS_PER_CREATOR_24H = 2
MAX_PUBLIC_UPLOADS_PER_GAME_24H = 4
DIVERSITY_WINDOW_HOURS = 24

# V12.14.6 ADAPTIVE DISCOVERY
#
# Stop based on ACTUAL usable Shorts sources, not raw links.
TARGET_ELIGIBLE_CANDIDATES = int(
    os.getenv(
        "DISCOVERY_TARGET_ELIGIBLE",
        "100",
    )
)

MIN_ACCEPTABLE_ELIGIBLE = int(
    os.getenv(
        "DISCOVERY_MIN_ELIGIBLE",
        "80",
    )
)

MIN_SOURCE_SECONDS = 40.0

MAX_ELIGIBLE_PER_GAME = int(
    os.getenv(
        "DISCOVERY_MAX_ELIGIBLE_PER_GAME",
        "24",
    )
)

MAX_ELIGIBLE_PER_CREATOR = int(
    os.getenv(
        "DISCOVERY_MAX_ELIGIBLE_PER_CREATOR",
        "3",
    )
)

API_WORKERS = int(
    os.getenv(
        "DISCOVERY_API_WORKERS",
        "16",
    )
)

API_TIMEOUT_SECONDS = int(
    os.getenv(
        "DISCOVERY_API_TIMEOUT_SECONDS",
        "10",
    )
)

# API listing pass first; browser scraping is fallback.
API_PAGES_PER_QUERY = int(
    os.getenv(
        "DISCOVERY_API_PAGES_PER_QUERY",
        "6",
    )
)

DISCOVERY_SCROLL_ROUNDS = int(
    os.getenv(
        "DISCOVERY_SCROLL_ROUNDS",
        "12",
    )
)

DISCOVERY_SCROLL_WAIT_MS = int(
    os.getenv(
        "DISCOVERY_SCROLL_WAIT_MS",
        "550",
    )
)

DISCOVERY_INITIAL_WAIT_MS = int(
    os.getenv(
        "DISCOVERY_INITIAL_WAIT_MS",
        "1800",
    )
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

API_HEADERS = {
    "User-Agent":
        USER_AGENT,
    "Accept":
        "application/json, text/plain, */*",
    "Referer":
        "https://kick.com/",
    "Origin":
        "https://kick.com",
    "X-Requested-With":
        "XMLHttpRequest",
}

GAME_CATEGORIES = [
    ("Call of Duty: Warzone", "call-of-duty-warzone"),
    ("Valorant", "valorant"),
    ("Counter-Strike 2", "counter-strike-2"),
    ("Fortnite", "fortnite"),
    ("Apex Legends", "apex-legends"),
    ("Marvel Rivals", "marvel-rivals"),
    ("Overwatch 2", "overwatch-2"),
    ("Call of Duty: Black Ops 7", "call-of-duty-black-ops-7"),
    ("Escape from Tarkov", "escape-from-tarkov"),
    ("Rust", "rust"),
    ("Grand Theft Auto V (GTA)", "grand-theft-auto-v"),
]

GAME_NAMES = {
    name.casefold():
        name
    for name, _ in GAME_CATEGORIES
}

GAME_SLUGS = {
    slug.casefold():
        name
    for name, slug in GAME_CATEGORIES
}

BLOCKED_TERMS = {
    "casino",
    "slots",
    "slot machine",
    "gambling",
    "roulette",
    "blackjack",
    "sportsbook",
    "sports betting",
    "betting",
    "stake",
    "crypto casino",
}

MUSIC_TERMS = {
    "music video",
    "official audio",
    "karaoke",
    "lyrics",
    "singing",
    "remix",
    "soundtrack",
}


def load_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return default


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
        parts = urlsplit(raw)

        netloc = parts.netloc.lower()

        if netloc.startswith("www."):
            netloc = netloc[4:]

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
        return raw.rstrip("/").casefold()


def clip_id_from_value(value):
    match = re.search(
        r"(clip_[A-Za-z0-9_-]+)",
        str(value or ""),
    )

    return (
        match.group(1)
        if match
        else None
    )


def parse_utc(value):
    try:
        return datetime.fromisoformat(
            str(value).replace(
                "Z",
                "+00:00",
            )
        ).astimezone(
            timezone.utc
        )
    except Exception:
        return None


def history_state(history):
    cutoff = (
        datetime.now(
            timezone.utc
        )
        -
        timedelta(
            hours=DIVERSITY_WINDOW_HOURS
        )
    )

    used = set()
    creator_counts = {}
    game_counts = {}

    rows = (
        history.get(
            "used_clips",
            [],
        )
        if isinstance(
            history,
            dict,
        )
        else []
    )

    for item in rows:
        if isinstance(item, str):
            clip_id = clip_id_from_value(
                item
            )

            if clip_id:
                used.add(
                    normalize_clip_id(
                        clip_id
                    )
                )
            elif item.strip():
                used.add(
                    normalize_clip_id(
                        item
                    )
                )

            continue

        if not isinstance(
            item,
            dict,
        ):
            continue

        clip_id = item.get(
            "clip_id"
        )

        if clip_id:
            used.add(
                normalize_clip_id(
                    clip_id
                )
            )

        if str(
            item.get(
                "privacy_status",
                "",
            )
        ).lower() != "public":
            continue

        when = parse_utc(
            item.get(
                "processed_at_utc"
            )
        )

        if (
            when is None
            or
            when < cutoff
        ):
            continue

        channel = str(
            item.get(
                "channel",
                "",
            )
        ).strip().lower()

        game = str(
            item.get(
                "game",
                "",
            )
        ).strip().lower()

        if channel:
            creator_counts[
                channel
            ] = (
                creator_counts.get(
                    channel,
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

    return (
        used,
        creator_counts,
        game_counts,
    )


def identity_sets(data, keys):
    ids = set()
    urls = set()

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = []

        for key in keys:
            value = data.get(key)

            if isinstance(
                value,
                list,
            ):
                rows.extend(value)
    else:
        rows = []

    for item in rows:
        if isinstance(item, str):
            raw = item.strip()

            clip_id = clip_id_from_value(
                raw
            )

            if clip_id:
                ids.add(
                    normalize_clip_id(
                        clip_id
                    )
                )

            if raw.startswith(
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

            continue

        if not isinstance(
            item,
            dict,
        ):
            continue

        clip_id = (
            item.get("clip_id")
            or
            item.get("id")
        )

        if clip_id:
            ids.add(
                normalize_clip_id(
                    clip_id
                )
            )

        url = (
            item.get("clip_url")
            or
            item.get("source_url")
            or
            item.get("url")
            or
            item.get("source")
        )

        if url:
            urls.add(
                normalize_clip_url(
                    url
                )
            )

    return (
        ids,
        urls,
    )


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
        f"User-Agent: {USER_AGENT}",
        "-H",
        "Accept: application/json, text/plain, */*",
        "-H",
        "Referer: https://kick.com/",
        "-H",
        "Origin: https://kick.com",
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


def fetch_json(url):
    errors = []

    for attempt in range(
        1,
        3,
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

        if attempt < 2:
            time.sleep(
                0.25
                *
                attempt
            )

    raise RuntimeError(
        " | ".join(
            errors[-4:]
        )
    )


def nested_slug(value):
    if isinstance(
        value,
        dict,
    ):
        for key in [
            "slug",
            "username",
            "name",
        ]:
            result = str(
                value.get(
                    key,
                    "",
                )
            ).strip()

            if result:
                return result

    return ""


def nested_name(value):
    if isinstance(
        value,
        dict,
    ):
        for key in [
            "name",
            "slug",
        ]:
            result = str(
                value.get(
                    key,
                    "",
                )
            ).strip()

            if result:
                return result

    return ""


def extract_listing_clips(payload):
    if not isinstance(
        payload,
        dict,
    ):
        return []

    for key in [
        "clips",
        "data",
        "results",
    ]:
        value = payload.get(
            key
        )

        if isinstance(
            value,
            list,
        ):
            return value

        if isinstance(
            value,
            dict,
        ):
            nested = value.get(
                "data"
            )

            if isinstance(
                nested,
                list,
            ):
                return nested

    return []


def next_cursor(payload):
    if not isinstance(
        payload,
        dict,
    ):
        return None

    for key in [
        "nextCursor",
        "next_cursor",
        "cursor",
    ]:
        value = payload.get(
            key
        )

        if isinstance(
            value,
            dict,
        ):
            value = (
                value.get("id")
                or
                value.get("cursor")
            )

        if value:
            return str(value)

    results = payload.get(
        "results"
    )

    if isinstance(
        results,
        dict,
    ):
        value = results.get(
            "cursor"
        )

        if isinstance(
            value,
            dict,
        ):
            value = (
                value.get("id")
                or
                value.get("cursor")
            )

        if value:
            return str(value)

    return None


def to_float(value):
    try:
        return float(value)
    except Exception:
        return None


def to_int(value):
    try:
        return max(
            0,
            int(value),
        )
    except Exception:
        return 0


def blocked_text(text):
    lowered = str(
        text or ""
    ).casefold()

    return any(
        term in lowered
        for term in (
            BLOCKED_TERMS
            |
            MUSIC_TERMS
        )
    )


def category_name_matches(
    category_name,
    expected_game,
):
    category = str(
        category_name or ""
    ).strip().casefold()

    if not category:
        return True

    expected = str(
        expected_game or ""
    ).strip().casefold()

    # Kick labels can differ slightly from our local display name.
    if (
        category == expected
        or
        category in expected
        or
        expected in category
    ):
        return True

    aliases = {
        "grand theft auto v":
            "grand theft auto v (gta)",
        "call of duty warzone":
            "call of duty: warzone",
        "counter-strike":
            "counter-strike 2",
        "overwatch":
            "overwatch 2",
    }

    mapped = aliases.get(
        category,
        category,
    )

    return (
        mapped == expected
        or
        mapped in expected
        or
        expected in mapped
    )


def candidate_from_clip(
    clip,
    expected_game,
    category_slug,
    category_position,
):
    if not isinstance(
        clip,
        dict,
    ):
        return None

    clip_id = (
        clip.get("id")
        or
        clip.get("clip_id")
    )

    if not clip_id:
        for value in [
            clip.get("url"),
            clip.get("clip_url"),
            clip.get("thumbnail_url"),
        ]:
            clip_id = clip_id_from_value(
                value
            )

            if clip_id:
                break

    clip_id = str(
        clip_id or ""
    ).strip()

    if not clip_id:
        return None

    channel = (
        nested_slug(
            clip.get("channel")
        )
        or
        nested_slug(
            clip.get("broadcaster")
        )
        or
        str(
            clip.get(
                "channel_slug",
                "",
            )
        ).strip()
        or
        nested_slug(
            clip.get("creator")
        )
    )

    channel = channel.lower()

    category_name = (
        nested_name(
            clip.get("category")
        )
        or
        nested_name(
            clip.get("subcategory")
        )
        or
        str(
            clip.get(
                "category_name",
                "",
            )
        ).strip()
    )

    if not category_name_matches(
        category_name,
        expected_game,
    ):
        return None

    title = str(
        clip.get(
            "title",
            "",
        )
    ).strip()

    duration = to_float(
        clip.get(
            "duration"
        )
        or
        clip.get(
            "duration_sec"
        )
        or
        clip.get(
            "duration_seconds"
        )
    )

    media_url = str(
        clip.get(
            "clip_url",
            "",
        )
        or
        clip.get(
            "media_url",
            "",
        )
        or
        clip.get(
            "video_url",
            "",
        )
    ).strip()

    page_url = str(
        clip.get(
            "url",
            "",
        )
        or
        clip.get(
            "page_url",
            "",
        )
    ).strip()

    if (
        not page_url
        or
        "clips.kick.com" in page_url
        or
        page_url.endswith(
            ".m3u8"
        )
    ):
        page_channel = (
            channel
            or
            "clip"
        )

        page_url = (
            f"https://kick.com/"
            f"{page_channel}/clips/"
            f"{clip_id}"
        )

    return {
        "platform":
            "kick",
        "game":
            expected_game,
        "category_slug":
            category_slug,
        "channel":
            channel,
        "clip_id":
            clip_id,
        "clip_url":
            page_url,
        "category_position":
            category_position,
        "prevalidated_title":
            title,
        "prevalidated_duration_seconds":
            duration,
        "prevalidated_media_url":
            media_url,
        "prevalidated_views":
            to_int(
                clip.get(
                    "view_count"
                )
                or
                clip.get(
                    "views"
                )
            ),
        "prevalidated_likes":
            to_int(
                clip.get(
                    "likes_count"
                )
                or
                clip.get(
                    "likes"
                )
            ),
        "prevalidated_created_at":
            clip.get(
                "created_at"
            ),
    }


def detail_candidate(candidate):
    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    ).strip()

    if not clip_id:
        return candidate

    duration = candidate.get(
        "prevalidated_duration_seconds"
    )

    media_url = str(
        candidate.get(
            "prevalidated_media_url",
            "",
        )
    ).strip()

    title = str(
        candidate.get(
            "prevalidated_title",
            "",
        )
    ).strip()

    if (
        duration is not None
        and
        media_url
    ):
        return candidate

    payload = fetch_json(
        "https://kick.com/api/v2/"
        f"clips/{clip_id}/play"
    )

    clip = payload.get(
        "clip"
    )

    if not isinstance(
        clip,
        dict,
    ):
        return candidate

    row = dict(
        candidate
    )

    row[
        "prevalidated_duration_seconds"
    ] = to_float(
        clip.get(
            "duration"
        )
    )

    row[
        "prevalidated_media_url"
    ] = str(
        clip.get(
            "clip_url",
            "",
        )
    ).strip()

    if not title:
        row[
            "prevalidated_title"
        ] = str(
            clip.get(
                "title",
                "",
            )
        ).strip()

    if not row.get(
        "channel"
    ):
        row[
            "channel"
        ] = nested_slug(
            clip.get(
                "channel"
            )
        ).lower()

    row[
        "prevalidated_views"
    ] = to_int(
        clip.get(
            "views"
        )
    )

    row[
        "prevalidated_likes"
    ] = to_int(
        clip.get(
            "likes"
        )
    )

    row[
        "prevalidated_created_at"
    ] = clip.get(
        "created_at"
    )

    return row


def is_eligible(candidate):
    cached = cached_firefight_verdict(candidate, load_firefight_cache())
    if cached and cached.get("passed") is False:
        return False

    duration = to_float(
        candidate.get(
            "prevalidated_duration_seconds"
        )
    )

    if (
        duration is None
        or
        duration < MIN_SOURCE_SECONDS
    ):
        return False

    if not str(
        candidate.get(
            "prevalidated_media_url",
            "",
        )
    ).strip():
        return False

    metadata = " ".join(
        [
            str(
                candidate.get(
                    "prevalidated_title",
                    "",
                )
            ),
            str(
                candidate.get(
                    "game",
                    "",
                )
            ),
        ]
    )

    if blocked_text(
        metadata
    ):
        return False

    return True


def extract_clip_links(html):
    patterns = [
        r'href=["\'](/[^"\']+/clips/clip_[A-Za-z0-9_-]+)["\']',
        r'https://kick\.com/[^"\'\\\s]+/clips/clip_[A-Za-z0-9_-]+',
    ]

    links = []

    for pattern in patterns:
        for match in re.findall(
            pattern,
            str(
                html or ""
            ),
        ):
            full = (
                match
                if match.startswith(
                    "http"
                )
                else
                f"https://kick.com{match}"
            )

            if full not in links:
                links.append(
                    full
                )

    return links


def channel_from_url(url):
    match = re.search(
        r"kick\.com/([^/]+)/clips/clip_",
        str(
            url or ""
        ),
        re.I,
    )

    return (
        match.group(1).lower()
        if match
        else ""
    )


def browser_candidates_for_category(
    page,
    game,
    slug,
):
    url = (
        f"https://kick.com/"
        f"category/{slug}/clips"
    )

    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=45000,
    )

    page.wait_for_timeout(
        DISCOVERY_INITIAL_WAIT_MS
    )

    links = extract_clip_links(
        page.content()
    )

    previous_count = len(
        links
    )

    stagnant = 0

    for _ in range(
        DISCOVERY_SCROLL_ROUNDS
    ):
        page.evaluate(
            "window.scrollTo("
            "0, document.body.scrollHeight)"
        )

        page.wait_for_timeout(
            DISCOVERY_SCROLL_WAIT_MS
        )

        links = extract_clip_links(
            page.content()
        )

        if len(
            links
        ) <= previous_count:
            stagnant += 1
        else:
            previous_count = len(
                links
            )
            stagnant = 0

        if stagnant >= 4:
            break

    rows = []

    for position, url in enumerate(
        links,
        1,
    ):
        clip_id = clip_id_from_value(
            url
        )

        channel = channel_from_url(
            url
        )

        if not clip_id:
            continue

        rows.append(
            {
                "platform":
                    "kick",
                "game":
                    game,
                "category_slug":
                    slug,
                "channel":
                    channel,
                "clip_id":
                    clip_id,
                "clip_url":
                    url,
                "category_position":
                    position,
                "prevalidated_title":
                    "",
                "prevalidated_duration_seconds":
                    None,
                "prevalidated_media_url":
                    "",
                "prevalidated_views":
                    0,
                "prevalidated_likes":
                    0,
                "prevalidated_created_at":
                    None,
            }
        )

    return rows


def api_candidates_for_category(
    game,
    slug,
):
    rows = []
    seen = set()

    queries = [("date", "day"), ("date", "month")] + [
        (
            "view",
            "week",
        ),
        (
            "view",
            "month",
        ),
        (
            "date",
            "week",
        ),
        (
            "view",
            "all",
        ),
    ]

    position = 0

    for sort_name, time_name in queries:
        cursor = None

        for _ in range(
            API_PAGES_PER_QUERY
        ):
            params = {
                "sort":
                    sort_name,
                "time":
                    time_name,
            }

            if cursor:
                params[
                    "cursor"
                ] = cursor

            url = (
                "https://kick.com/api/v2/"
                f"categories/{slug}/clips?"
                +
                urlencode(
                    params
                )
            )

            try:
                payload = fetch_json(
                    url
                )
            except Exception:
                break

            clips = extract_listing_clips(
                payload
            )

            if not clips:
                break

            for clip in clips:
                position += 1

                row = candidate_from_clip(
                    clip,
                    game,
                    slug,
                    position,
                )

                if not row:
                    continue

                clip_id = normalize_clip_id(
                    row.get(
                        "clip_id"
                    )
                )

                if (
                    not clip_id
                    or
                    clip_id in seen
                ):
                    continue

                seen.add(
                    clip_id
                )

                rows.append(
                    row
                )

            cursor = next_cursor(
                payload
            )

            if not cursor:
                break

    return rows


def enrich_batch(rows):
    results = []

    with ThreadPoolExecutor(
        max_workers=API_WORKERS
    ) as executor:
        futures = {
            executor.submit(
                detail_candidate,
                row,
            ): row
            for row in rows
        }

        for future in as_completed(
            futures
        ):
            try:
                results.append(
                    future.result()
                )
            except Exception:
                results.append(
                    futures[
                        future
                    ]
                )

    return results


def main():
    history = load_json(
        HISTORY_PATH,
        {
            "version": 1,
            "used_clips": [],
        },
    )

    (
        used,
        creator_upload_counts,
        game_upload_counts,
    ) = history_state(
        history
    )

    (
        published_ids,
        published_urls,
    ) = identity_sets(
        history,
        [
            "used_clips",
        ],
    )

    rejected = load_json(
        SHORTS_REJECTED_HISTORY_PATH,
        {
            "version": 1,
            "clip_ids": [],
        },
    )

    (
        rejected_ids,
        rejected_urls,
    ) = identity_sets(
        rejected,
        [
            "clip_ids",
        ],
    )

    used |= published_ids

    print(
        "================================================"
    )
    print(
        "ViralSpawnTV V12.14.6 "
        "Adaptive Eligible Discovery"
    )
    print(
        "================================================"
    )

    print(
        f"Target: "
        f"{TARGET_ELIGIBLE_CANDIDATES} "
        f"actual {MIN_SOURCE_SECONDS:.0f}s+ "
        f"gaming clips."
    )

    eligible = []
    seen_ids = set()
    seen_urls = set()
    per_game_counts = {}
    per_creator_counts = {}

    browser = None
    playwright = None
    page = None

    try:
        # Two passes:
        # pass 1 uses category API where possible;
        # pass 2 invokes browser fallback/deeper collection for categories
        # that still have room if the eligible target was not reached.
        for pass_no in [
            1,
            2,
        ]:
            if (
                len(
                    eligible
                )
                >=
                TARGET_ELIGIBLE_CANDIDATES
            ):
                break

            print(
                f"Adaptive discovery pass "
                f"{pass_no}: "
                f"{len(eligible)}/"
                f"{TARGET_ELIGIBLE_CANDIDATES} "
                f"eligible collected."
            )

            for game, slug in GAME_CATEGORIES:
                if (
                    len(
                        eligible
                    )
                    >=
                    TARGET_ELIGIBLE_CANDIDATES
                ):
                    break

                if (
                    game_upload_counts.get(
                        game.lower(),
                        0,
                    )
                    >=
                    MAX_PUBLIC_UPLOADS_PER_GAME_24H
                ):
                    continue

                room = (
                    MAX_ELIGIBLE_PER_GAME
                    -
                    per_game_counts.get(
                        game,
                        0,
                    )
                )

                if room <= 0:
                    continue

                candidates = []

                if pass_no == 1:
                    candidates = (
                        api_candidates_for_category(
                            game,
                            slug,
                        )
                    )

                if (
                    pass_no == 2
                    or
                    not candidates
                ):
                    if playwright is None:
                        playwright = (
                            sync_playwright()
                            .start()
                        )

                        browser = (
                            playwright
                            .chromium
                            .launch(
                                headless=True
                            )
                        )

                        page = browser.new_page(
                            user_agent=
                                USER_AGENT,
                            viewport={
                                "width":
                                    1440,
                                "height":
                                    1000,
                            },
                        )

                    try:
                        browser_rows = (
                            browser_candidates_for_category(
                                page,
                                game,
                                slug,
                            )
                        )

                        candidates.extend(
                            browser_rows
                        )

                    except Exception as exc:
                        print(
                            f"Browser fallback skipped "
                            f"{game}: {exc}"
                        )

                # Filter identity/history before API detail work.
                to_enrich = []

                for candidate in candidates:
                    clip_id = normalize_clip_id(
                        candidate.get(
                            "clip_id"
                        )
                    )

                    clip_url = normalize_clip_url(
                        candidate.get(
                            "clip_url"
                        )
                    )

                    if (
                        not clip_id
                        or
                        clip_id in seen_ids
                        or
                        clip_url in seen_urls
                    ):
                        continue

                    if (
                        clip_id in used
                        or
                        clip_id in rejected_ids
                        or
                        clip_url in published_urls
                        or
                        clip_url in rejected_urls
                    ):
                        continue

                    channel = str(
                        candidate.get(
                            "channel",
                            "",
                        )
                    ).strip().lower()

                    if (
                        channel
                        and
                        creator_upload_counts.get(
                            channel,
                            0,
                        )
                        >=
                        MAX_PUBLIC_UPLOADS_PER_CREATOR_24H
                    ):
                        continue

                    if (
                        channel
                        and
                        per_creator_counts.get(
                            channel,
                            0,
                        )
                        >=
                        MAX_ELIGIBLE_PER_CREATOR
                    ):
                        continue

                    seen_ids.add(
                        clip_id
                    )

                    if clip_url:
                        seen_urls.add(
                            clip_url
                        )

                    to_enrich.append(
                        candidate
                    )

                if not to_enrich:
                    continue

                enriched = enrich_batch(
                    to_enrich
                )

                game_added = 0

                # Prefer clips with stronger native traction inside each game,
                # while still allowing unknown/low-view creators.
                enriched.sort(
                    key=lambda row: (
                        int(
                            row.get(
                                "prevalidated_views",
                                0,
                            )
                            or
                            0
                        ),
                        int(
                            row.get(
                                "prevalidated_likes",
                                0,
                            )
                            or
                            0
                        ),
                    ),
                    reverse=True,
                )

                for candidate in enriched:
                    if (
                        len(
                            eligible
                        )
                        >=
                        TARGET_ELIGIBLE_CANDIDATES
                    ):
                        break

                    if game_added >= room:
                        break

                    if not is_eligible(
                        candidate
                    ):
                        continue

                    channel = str(
                        candidate.get(
                            "channel",
                            "",
                        )
                    ).strip().lower()

                    if (
                        channel
                        and
                        per_creator_counts.get(
                            channel,
                            0,
                        )
                        >=
                        MAX_ELIGIBLE_PER_CREATOR
                    ):
                        continue

                    eligible.append(
                        candidate
                    )

                    game_added += 1

                    per_game_counts[
                        game
                    ] = (
                        per_game_counts.get(
                            game,
                            0,
                        )
                        +
                        1
                    )

                    if channel:
                        per_creator_counts[
                            channel
                        ] = (
                            per_creator_counts.get(
                                channel,
                                0,
                            )
                            +
                            1
                        )

                if game_added:
                    print(
                        f"{game}: +{game_added} "
                        f"eligible | total="
                        f"{len(eligible)}/"
                        f"{TARGET_ELIGIBLE_CANDIDATES}"
                    )

        if (
            len(
                eligible
            )
            <
            MIN_ACCEPTABLE_ELIGIBLE
        ):
            print(
                f"WARNING: adaptive target not met; "
                f"found only {len(eligible)} "
                f"eligible clips. Minimum preferred is "
                f"{MIN_ACCEPTABLE_ELIGIBLE}."
            )

        if not eligible:
            raise RuntimeError(
                "Adaptive discovery found no "
                "40s+ eligible gaming clips."
            )

        manifest = {
            "version":
                "12.14.6-100-eligible-active-firefight-pool",
            "strategy":
                "100_eligible_then_active_firefight_filter",
            "created_at_utc":
                datetime.now(
                    timezone.utc
                ).isoformat(),
            "target_eligible_candidates":
                TARGET_ELIGIBLE_CANDIDATES,
            "minimum_acceptable_eligible":
                MIN_ACCEPTABLE_ELIGIBLE,
            "minimum_source_seconds":
                MIN_SOURCE_SECONDS,
            "candidate_count":
                len(
                    eligible
                ),
            "eligible_target_reached":
                len(
                    eligible
                )
                >=
                TARGET_ELIGIBLE_CANDIDATES,
            "games_with_candidates":
                sorted(
                    {
                        row.get(
                            "game"
                        )
                        for row in eligible
                        if row.get(
                            "game"
                        )
                    }
                ),
            "candidates":
                eligible,
        }

        MANIFEST_PATH.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        MANIFEST_PATH.write_text(
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print()
        print(
            f"V12.14.6 ADAPTIVE DISCOVERY COMPLETE: "
            f"{len(eligible)} actual "
            f"{MIN_SOURCE_SECONDS:.0f}s+ "
            f"eligible gaming clips."
        )

        print(
            f"Target reached: "
            f"{manifest['eligible_target_reached']}"
        )

    finally:
        if browser is not None:
            browser.close()

        if playwright is not None:
            playwright.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"V12.14.6 DISCOVERY FAILED: "
            f"{exc}"
        )
        sys.exit(1)
