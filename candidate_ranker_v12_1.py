import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from openai import OpenAI
from playwright.sync_api import sync_playwright


MANIFEST = Path("work/v11_candidate_manifest.json")
OUT = Path("work/v12_ranked_candidates.json")
SHORTS_HISTORY = Path("history.json")
SHORTS_REJECTED_HISTORY = Path("shorts_rejected_history.json")

# V12.9 FAST PASS:
# One page visit now collects metadata + HLS playlist + duration.
MAX_INSPECT = 100
MAX_RANKED = 40
MIN_SCORE = 0
MIN_SOURCE_SECONDS = 49.0

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


def inspect_metadata(page, candidate):
    """
    V12.9 uses ONE Kick-page visit for:
    - title / description
    - HLS playlist capture
    - source duration

    The visual prescreener reuses the captured HLS URL instead of opening
    the same page again.
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
        f"V12.9 freshness filter: "
        f"{len(all_candidates)} discovered -> "
        f"{len(fresh_candidates)} fresh -> "
        f"{len(candidates)} metadata-inspected."
    )

    print(
        f"Skipped {skipped_history} previously "
        f"published/rejected Shorts before inspection."
    )

    inspected = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page()

        for i, candidate in enumerate(
            candidates,
            1,
        ):
            try:
                row = inspect_metadata(
                    page,
                    candidate,
                )

                if row["bad_hits"]:
                    print(
                        f"[{i}/{len(candidates)}] "
                        f"HARD REJECT blocked metadata: "
                        f"{candidate.get('clip_id')} "
                        f"{row['bad_hits']}"
                    )
                    continue

                if row["music_hits"]:
                    print(
                        f"[{i}/{len(candidates)}] "
                        f"HARD REJECT music metadata: "
                        f"{candidate.get('clip_id')} "
                        f"{row['music_hits']}"
                    )
                    continue

                if not row.get(
                    "playlist_url"
                ):
                    print(
                        f"[{i}/{len(candidates)}] "
                        f"SKIP no HLS captured: "
                        f"{candidate.get('clip_id')}"
                    )
                    continue

                source_seconds = row.get(
                    "source_duration_seconds"
                )

                if source_seconds is None:
                    print(
                        f"[{i}/{len(candidates)}] "
                        f"SKIP unknown duration: "
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
                    print(
                        f"[{i}/{len(candidates)}] "
                        f"SKIP too short "
                        f"({source_seconds:.2f}s): "
                        f"{candidate.get('clip_id')}"
                    )
                    continue

                inspected.append(
                    row
                )

            except Exception as exc:
                print(
                    f"[{i}/{len(candidates)}] "
                    f"metadata inspect failed: "
                    f"{candidate.get('clip_id')} "
                    f"{exc}"
                )

        browser.close()

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
            "12.9-fast-window-aware-input",
        "source_candidate_count":
            len(all_candidates),
        "fresh_candidate_count":
            len(fresh_candidates),
        "metadata_inspected_count":
            len(candidates),
        "survived_deterministic_screen":
            len(inspected),
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
        f"V12.9 one-pass metadata/duration stage: "
        f"{len(all_candidates)} discovered -> "
        f"{len(inspected)} deterministic survivors -> "
        f"{len(ranked)} sent to visual prescreen."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.9 RANKER FAILED:",
            exc,
        )
        sys.exit(1)
