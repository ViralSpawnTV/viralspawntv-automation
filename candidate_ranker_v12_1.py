import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from openai import OpenAI
from playwright.sync_api import sync_playwright


MANIFEST = Path("work/v11_candidate_manifest.json")
OUT = Path("work/v12_ranked_candidates.json")
SHORTS_HISTORY = Path("history.json")
SHORTS_REJECTED_HISTORY = Path("shorts_rejected_history.json")

# V12.10 DIRECT-API PASS:
# Normal path: no individual Kick clip page loads at all.
MAX_INSPECT = 100
MAX_RANKED = 40
MIN_SCORE = 0
MIN_SOURCE_SECONDS = 49.0

API_WORKERS = 12
API_TIMEOUT_SECONDS = 10
API_RETRIES = 2

# If direct API access is temporarily blocked, fall back to Playwright for
# only a small number of clips instead of loading all 100 pages.
MIN_DIRECT_SURVIVORS_BEFORE_SKIP_BROWSER = 6
MAX_BROWSER_FALLBACKS = 16

KICK_API_TEMPLATE = "https://kick.com/api/v2/clips/{clip_id}/play"

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://kick.com/",
    "Origin": "https://kick.com",
    "X-Requested-With": "XMLHttpRequest",
}

BLOCKED = {
    "casino", "gambling", "roulette", "blackjack", "sportsbook",
    "betting", "slots", "slot machine", "stake", "crypto",
    "prediction market",
}

MUSIC_BLOCKED = {
    "song", "music", "remix", "lyrics", "singing", "karaoke",
    "official audio", "music video", "soundtrack",
}

ACTION = {
    "clutch", "1v", "kill", "kills", "win", "ace", "rage",
    "insane", "crazy", "sniper", "headshot", "fight", "final",
    "boss", "record", "speedrun", "comeback", "fail", "funny",
    "reaction", "elim", "wiped", "squad", "ranked", "overtime",
    "last second", "1 hp", "quad", "triple", "double", "ambush",
    "rocket", "movement",
}


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


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
        return urlunsplit(
            (
                (parts.scheme or "https").lower(),
                netloc,
                path,
                "",
                "",
            )
        )
    except Exception:
        return raw.rstrip("/").casefold()


def load_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: could not parse {path}: {exc}")
        return {}


def extract_identity_sets(data, keys):
    ids, urls = set(), set()

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = []
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                rows.extend(value)
    else:
        rows = []

    for item in rows:
        if isinstance(item, str):
            if item.strip().lower().startswith(("http://", "https://")):
                curl = normalize_clip_url(item)
                if curl:
                    urls.add(curl)
            else:
                cid = normalize_clip_id(item)
                if cid:
                    ids.add(cid)
            continue

        if not isinstance(item, dict):
            continue

        cid = normalize_clip_id(
            item.get("clip_id")
            or item.get("id")
        )

        curl = normalize_clip_url(
            item.get("clip_url")
            or item.get("source_url")
            or item.get("url")
            or item.get("source")
        )

        if cid:
            ids.add(cid)

        if curl:
            urls.add(curl)

    return ids, urls


def load_blocked_short_identities():
    used_ids, used_urls = extract_identity_sets(
        load_json(SHORTS_HISTORY),
        ["used_clips", "clips", "history"],
    )

    rejected_ids, rejected_urls = extract_identity_sets(
        load_json(SHORTS_REJECTED_HISTORY),
        [
            "clip_ids",
            "rejected_clips",
            "rejected",
            "clips",
            "used_clips",
        ],
    )

    print(
        f"Previously published Shorts IDs loaded: "
        f"{len(used_ids)}"
    )

    print(
        f"Permanently rejected Shorts IDs loaded: "
        f"{len(rejected_ids)}"
    )

    return (
        used_ids | rejected_ids,
        used_urls | rejected_urls,
    )


def candidate_is_blocked(
    candidate,
    blocked_ids,
    blocked_urls,
):
    cid = normalize_clip_id(
        candidate.get("clip_id")
    )

    curl = normalize_clip_url(
        candidate.get("clip_url")
        or candidate.get("source_url")
        or candidate.get("url")
        or candidate.get("source")
    )

    return (
        (cid and cid in blocked_ids)
        or
        (curl and curl in blocked_urls)
    )


def hits(text, words):
    lowered = text.lower()
    return sorted(
        word
        for word in words
        if word in lowered
    )




def _json_via_urllib(url):
    request = urllib.request.Request(
        url,
        headers=API_HEADERS,
        method="GET",
    )

    with urllib.request.urlopen(
        request,
        timeout=API_TIMEOUT_SECONDS,
    ) as response:
        raw = response.read()

    return json.loads(
        raw.decode(
            "utf-8",
            errors="replace",
        )
    )


def _json_via_curl(url):
    """
    Second direct-HTTP route. This is still dramatically cheaper than
    launching a browser and lets the runner survive occasional urllib/TLS
    differences.
    """
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
        f"User-Agent: {API_HEADERS['User-Agent']}",
        "-H",
        f"Accept: {API_HEADERS['Accept']}",
        "-H",
        f"Referer: {API_HEADERS['Referer']}",
        "-H",
        f"Origin: {API_HEADERS['Origin']}",
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
    """
    Fetch the same public clip payload used by Kick's clip player and by
    current yt-dlp Kick extraction.

    Expected response:
        {"clip": {
            "clip_url": ...,
            "duration": ...,
            "title": ...,
            "views": ...,
            "likes": ...,
            "channel": {"slug": ...},
            "category": {"name": ...},
            ...
        }}
    """
    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    ).strip()

    if not clip_id:
        raise RuntimeError(
            "candidate missing clip_id"
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
            payload = _json_via_urllib(
                url
            )
            return payload

        except Exception as exc:
            errors.append(
                f"urllib#{attempt}: {exc}"
            )

        try:
            payload = _json_via_curl(
                url
            )
            return payload

        except Exception as exc:
            errors.append(
                f"curl#{attempt}: {exc}"
            )

        if attempt < API_RETRIES:
            time.sleep(
                0.35 * attempt
            )

    raise RuntimeError(
        " | ".join(
            errors[-4:]
        )
    )


def to_float(value):
    try:
        return float(
            value
        )
    except Exception:
        return None


def to_int(value):
    try:
        return int(
            value
        )
    except Exception:
        return 0


def inspect_metadata_api(candidate):
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
            "Kick API response missing clip object"
        )

    media_url = str(
        clip.get(
            "clip_url",
            "",
        )
    ).strip()

    duration_seconds = to_float(
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
    )

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
        round(
            likes
            /
            max(
                1,
                views,
            )
            *
            100,
            3,
        )
        if views > 0
        else 0.0
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

    # Keep original discovery game for diversity bookkeeping, but attach
    # Kick's current category separately for quality/content checks.
    row.update(
        {
            "metadata_source":
                "playwright_fallback",
            "page_title":
                title,
            "page_description":
                "",
            "metadata_text":
                metadata_text,
            "bad_hits":
                hits(
                    metadata_text,
                    BLOCKED,
                ),
            "music_hits":
                hits(
                    metadata_text,
                    MUSIC_BLOCKED,
                ),
            "action_hits":
                hits(
                    metadata_text,
                    ACTION,
                ),
            "media_url":
                media_url,
            # Backward-compatible alias for V12.9 consumers.
            "playlist_url":
                media_url,
            "source_duration_seconds":
                (
                    round(
                        duration_seconds,
                        3,
                    )
                    if duration_seconds
                    is not None
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
                like_rate_pct,
            "kick_created_at":
                clip.get(
                    "created_at"
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


def probe_stream_duration(playlist_url):
    if not playlist_url:
        return None

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                playlist_url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )

        value = result.stdout.strip()

        if not value:
            return None

        seconds = float(value)

        if seconds <= 0:
            return None

        return seconds

    except Exception:
        return None


def inspect_metadata_browser_fallback(page, candidate):
    """
    Emergency fallback only. V12.10 normally uses the direct Kick clip
    API and never opens individual clip pages.
    """
    playlist_urls = []

    def capture(response):
        url = response.url

        if ".m3u8" in url:
            playlist_urls.append(
                url
            )

    page.on(
        "response",
        capture,
    )

    try:
        page.goto(
            candidate["clip_url"],
            wait_until="domcontentloaded",
            timeout=30000,
        )

        # Short wait only; this replaces the second page visit that the
        # old visual prescreener used to perform.
        page.wait_for_timeout(
            450
        )

        try:
            page.locator(
                "video"
            ).first.click(
                timeout=1000
            )

            page.wait_for_timeout(
                350
            )

        except Exception:
            pass

        # Give slow HLS responses one small extra chance.
        if not playlist_urls:
            page.wait_for_timeout(
                450
            )

        title = norm(
            page.title()
        )

        desc = ""

        for selector in [
            'meta[name="description"]',
            'meta[property="og:description"]',
            'meta[name="twitter:description"]',
        ]:
            try:
                value = (
                    page.locator(
                        selector
                    )
                    .first
                    .get_attribute(
                        "content"
                    )
                )

                if value:
                    desc = norm(
                        value
                    )
                    break

            except Exception:
                pass

    finally:
        try:
            page.remove_listener(
                "response",
                capture,
            )
        except Exception:
            pass

    chosen_playlist = None

    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    ).lower()

    for url in playlist_urls:
        if (
            clip_id
            and clip_id in url.lower()
        ):
            chosen_playlist = url
            break

    if (
        not chosen_playlist
        and playlist_urls
    ):
        chosen_playlist = (
            playlist_urls[-1]
        )

    source_seconds = (
        probe_stream_duration(
            chosen_playlist
        )
        if chosen_playlist
        else None
    )

    text = norm(
        f"{title} {desc}"
    )

    row = dict(
        candidate
    )

    row.update(
        {
            "page_title":
                title,
            "page_description":
                desc,
            "metadata_text":
                text,
            "bad_hits":
                hits(
                    text,
                    BLOCKED,
                ),
            "music_hits":
                hits(
                    text,
                    MUSIC_BLOCKED,
                ),
            "action_hits":
                hits(
                    text,
                    ACTION,
                ),
            "playlist_url":
                chosen_playlist,
            "source_duration_seconds":
                (
                    round(
                        source_seconds,
                        3,
                    )
                    if source_seconds
                    is not None
                    else None
                ),
        }
    )

    return row

def main():
    if not MANIFEST.exists():
        raise RuntimeError(
            "Missing work/v11_candidate_manifest.json"
        )

    data = json.loads(
        MANIFEST.read_text(
            encoding="utf-8"
        )
    )

    all_candidates = data.get(
        "candidates",
        [],
    )

    blocked_ids, blocked_urls = (
        load_blocked_short_identities()
    )

    fresh_candidates = []
    skipped_history = 0
    seen_ids = set()
    seen_urls = set()

    for candidate in all_candidates:
        if not isinstance(candidate, dict):
            continue

        cid = normalize_clip_id(
            candidate.get("clip_id")
        )

        curl = normalize_clip_url(
            candidate.get("clip_url")
            or candidate.get("source_url")
            or candidate.get("url")
            or candidate.get("source")
        )

        if (
            (cid and cid in seen_ids)
            or
            (curl and curl in seen_urls)
        ):
            continue

        if candidate_is_blocked(
            candidate,
            blocked_ids,
            blocked_urls,
        ):
            skipped_history += 1
            continue

        fresh_candidates.append(
            candidate
        )

        if cid:
            seen_ids.add(cid)

        if curl:
            seen_urls.add(curl)

    candidates = fresh_candidates[
        :MAX_INSPECT
    ]

    print(
        f"V12.10 freshness filter: "
        f"{len(all_candidates)} discovered -> "
        f"{len(fresh_candidates)} fresh -> "
        f"{len(candidates)} metadata-inspected."
    )

    print(
        f"Skipped {skipped_history} previously "
        f"published/rejected Shorts before inspection."
    )

    inspected = []
    api_failures = []
    api_successes = 0
    too_short_count = 0
    blocked_count = 0
    music_count = 0
    no_media_count = 0
    unknown_duration_count = 0

    print(
        f"V12.10 DIRECT API: querying "
        f"{len(candidates)} clips with "
        f"{API_WORKERS} workers."
    )

    rows_by_index = {}

    with ThreadPoolExecutor(
        max_workers=API_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                inspect_metadata_api,
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
                row = future.result()
                rows_by_index[
                    index
                ] = row
                api_successes += 1

            except Exception as exc:
                api_failures.append(
                    (
                        index,
                        candidate,
                        str(exc),
                    )
                )

    # Preserve discovery order after concurrent lookups.
    for index in sorted(
        rows_by_index
    ):
        row = rows_by_index[
            index
        ]

        candidate = candidates[
            index - 1
        ]

        if row["bad_hits"]:
            blocked_count += 1
            print(
                f"[{index}/{len(candidates)}] "
                f"HARD REJECT blocked API metadata: "
                f"{candidate.get('clip_id')} "
                f"{row['bad_hits']}"
            )
            continue

        if row["music_hits"]:
            music_count += 1
            print(
                f"[{index}/{len(candidates)}] "
                f"HARD REJECT music API metadata: "
                f"{candidate.get('clip_id')} "
                f"{row['music_hits']}"
            )
            continue

        if not row.get(
            "media_url"
        ):
            no_media_count += 1
            print(
                f"[{index}/{len(candidates)}] "
                f"SKIP API has no clip media URL: "
                f"{candidate.get('clip_id')}"
            )
            continue

        source_seconds = row.get(
            "source_duration_seconds"
        )

        if source_seconds is None:
            unknown_duration_count += 1
            print(
                f"[{index}/{len(candidates)}] "
                f"SKIP API missing duration: "
                f"{candidate.get('clip_id')}"
            )
            continue

        if (
            float(
                source_seconds
            )
            <
            MIN_SOURCE_SECONDS
        ):
            too_short_count += 1
            print(
                f"[{index}/{len(candidates)}] "
                f"SKIP too short "
                f"({source_seconds:.2f}s): "
                f"{candidate.get('clip_id')}"
            )
            continue

        inspected.append(
            row
        )

    # Direct API should be the normal path. If it is temporarily blocked,
    # do a limited browser fallback rather than repeating V12.9's 100-page
    # serial crawl.
    browser_fallback_used = 0

    if (
        len(inspected)
        <
        MIN_DIRECT_SURVIVORS_BEFORE_SKIP_BROWSER
        and
        api_failures
    ):
        fallback_targets = (
            sorted(
                api_failures,
                key=lambda item: item[0],
            )[
                :MAX_BROWSER_FALLBACKS
            ]
        )

        print(
            f"V12.10 API fallback: only "
            f"{len(inspected)} eligible direct survivors; "
            f"trying up to "
            f"{len(fallback_targets)} clip pages."
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True
            )

            page = browser.new_page()

            for index, candidate, api_error in fallback_targets:
                try:
                    row = (
                        inspect_metadata_browser_fallback(
                            page,
                            candidate,
                        )
                    )

                    source_seconds = row.get(
                        "source_duration_seconds"
                    )

                    if (
                        row.get(
                            "bad_hits"
                        )
                        or
                        row.get(
                            "music_hits"
                        )
                        or
                        not row.get(
                            "playlist_url"
                        )
                        or
                        source_seconds is None
                        or
                        float(
                            source_seconds
                        )
                        <
                        MIN_SOURCE_SECONDS
                    ):
                        continue

                    # Keep consumers on the unified V12.10 field name.
                    row[
                        "media_url"
                    ] = row.get(
                        "playlist_url"
                    )

                    row.setdefault(
                        "kick_view_count",
                        0,
                    )

                    row.setdefault(
                        "kick_like_count",
                        0,
                    )

                    row.setdefault(
                        "kick_like_rate_pct",
                        0.0,
                    )

                    row.setdefault(
                        "kick_api_category",
                        row.get(
                            "game",
                            "",
                        ),
                    )

                    inspected.append(
                        row
                    )

                    browser_fallback_used += 1

                except Exception as exc:
                    print(
                        f"[{index}/{len(candidates)}] "
                        f"fallback failed "
                        f"{candidate.get('clip_id')}: "
                        f"{exc}"
                    )

            browser.close()

    print(
        "V12.10 API STATS: "
        f"{api_successes} direct API responses, "
        f"{len(api_failures)} direct failures, "
        f"{too_short_count} under {MIN_SOURCE_SECONDS:.0f}s, "
        f"{blocked_count} blocked, "
        f"{music_count} music, "
        f"{no_media_count} no-media, "
        f"{unknown_duration_count} unknown-duration, "
        f"{browser_fallback_used} browser fallbacks accepted."
    )

    if not inspected:
        raise RuntimeError(
            "No candidates survived deterministic "
            "metadata screening."
        )

    compact = []

    for i, row in enumerate(
        inspected
    ):
        compact.append(
            {
                "id": i,
                "game": row.get(
                    "game",
                    "",
                ),
                "channel": row.get(
                    "channel",
                    "",
                ),
                "title": row.get(
                    "page_title",
                    "",
                ),
                "description": row.get(
                    "page_description",
                    "",
                ),
                "action_hits": row.get(
                    "action_hits",
                    [],
                ),
                "duration_seconds": row.get(
                    "source_duration_seconds",
                ),
                "kick_category": row.get(
                    "kick_api_category",
                    "",
                ),
                "views": row.get(
                    "kick_view_count",
                    0,
                ),
                "likes": row.get(
                    "kick_like_count",
                    0,
                ),
                "like_rate_pct": row.get(
                    "kick_like_rate_pct",
                    0.0,
                ),
            }
        )

    client = OpenAI()

    prompt = f"""
You are the CHEAP metadata ordering stage for ViralSpawnTV.

A later WINDOW-AWARE visual prescreener will inspect actual frames from these clips,
so do not reject merely because metadata is sparse.

Rank ALL surviving gaming candidates from most promising to least
promising using ONLY the metadata below.

Prefer:
- gameplay action
- clutch/fail/rage/reaction/comedy
- clear stakes or challenge
- obvious payoff language
- something likely to create a strong first-second hook
- real Kick views/likes as a SECONDARY traction signal

Popularity is only a tie-breaker. A high-view clip with weak gaming
story/hook language should not outrank a clearly stronger story candidate.

Do not invent events that are not supported by metadata.

Return every candidate supplied, up to {MAX_RANKED}.
Score 0-100, but do not use this score as the final quality decision.

Return ONLY JSON:
{{
  "ranked": [
    {{
      "id": 0,
      "score": 72,
      "reason": "brief metadata reason"
    }}
  ]
}}

CANDIDATES:
{json.dumps(compact, ensure_ascii=False)}
"""

    response = client.responses.create(
        model="gpt-5.6",
        input=prompt,
    )

    raw = re.sub(
        r"^```json\s*|\s*```$",
        "",
        response.output_text.strip(),
    )

    ranked_json = json.loads(
        raw
    ).get(
        "ranked",
        [],
    )

    by_id = {}

    for item in ranked_json:
        try:
            idx = int(
                item["id"]
            )
            score = float(
                item.get(
                    "score",
                    0,
                )
            )
        except Exception:
            continue

        if (
            idx < 0
            or idx >= len(inspected)
        ):
            continue

        by_id[idx] = {
            "score": score,
            "reason": norm(
                item.get(
                    "reason",
                    "",
                )
            ),
        }

    # Keep every deterministic survivor, even if the model omitted one.
    ranked = []

    for idx, row in enumerate(
        inspected
    ):
        item = by_id.get(
            idx,
            {
                "score": 0,
                "reason":
                    "Metadata model omitted candidate; "
                    "visual prescreener will decide.",
            },
        )

        candidate = dict(
            row
        )

        candidate[
            "v12_metadata_score"
        ] = round(
            float(
                item["score"]
            ),
            1,
        )

        candidate[
            "v12_metadata_reason"
        ] = item[
            "reason"
        ]

        ranked.append(
            candidate
        )

    ranked.sort(
        key=lambda row: float(
            row.get(
                "v12_metadata_score",
                0,
            )
        ),
        reverse=True,
    )

    ranked = ranked[
        :MAX_RANKED
    ]

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "version":
            "12.10-direct-api-window-input",
        "source_candidate_count":
            len(all_candidates),
        "fresh_candidate_count":
            len(fresh_candidates),
        "metadata_inspected_count":
            len(candidates),
        "survived_deterministic_screen":
            len(inspected),
        "direct_api_success_count":
            api_successes,
        "direct_api_failure_count":
            len(api_failures),
        "browser_fallback_accepted_count":
            browser_fallback_used,
        "ranked_count":
            len(ranked),
        "min_metadata_score":
            MIN_SCORE,
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

    print(
        f"V12.10 direct-API metadata/duration stage: "
        f"{len(all_candidates)} discovered -> "
        f"{len(inspected)} deterministic survivors -> "
        f"{len(ranked)} sent to visual prescreen."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.10 RANKER FAILED:",
            exc,
        )
        sys.exit(1)
