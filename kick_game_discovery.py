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

# V12.13 QUALITY-FIRST DISCOVERY
#
# The old pipeline usually discovered about 100 clips, then found that most
# were too short or ordinary. We now collect a much deeper cheap pool first
# and let the direct Kick API rank real traction before visual AI is spent.
MAX_CLIPS_PER_GAME = int(
    os.getenv(
        "DISCOVERY_MAX_CLIPS_PER_GAME",
        "32",
    )
)

MAX_TOTAL_CANDIDATES = int(
    os.getenv(
        "DISCOVERY_MAX_TOTAL_CANDIDATES",
        "320",
    )
)

DISCOVERY_SCROLL_ROUNDS = int(
    os.getenv(
        "DISCOVERY_SCROLL_ROUNDS",
        "5",
    )
)

DISCOVERY_SCROLL_WAIT_MS = int(
    os.getenv(
        "DISCOVERY_SCROLL_WAIT_MS",
        "600",
    )
)

DISCOVERY_INITIAL_WAIT_MS = int(
    os.getenv(
        "DISCOVERY_INITIAL_WAIT_MS",
        "2200",
    )
)

# Gather extra raw links because published/rejected history filtering happens
# after the category page is scanned.
RAW_LINK_TARGET_MULTIPLIER = 2

# Shorts is the default mode now that its candidate target is >100.
# Long-form can opt in explicitly without the old "candidate count > 100"
# heuristic incorrectly treating Shorts as long-form.
LONGFORM_MODE = (
    os.getenv(
        "DISCOVERY_LONGFORM_MODE",
        "0",
    ).strip().lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# High-action / competitive categories first. Discovery is still diversified;
# the direct API traction ranker makes the actual quality ordering.
GAME_CATEGORIES = [
    ("Call of Duty: Warzone", "call-of-duty-warzone"),
    ("Valorant", "valorant"),
    ("Counter-Strike 2", "counter-strike-2"),
    ("Fortnite", "fortnite"),
    ("Apex Legends", "apex-legends"),
    ("Marvel Rivals", "marvel-rivals"),
    ("Rocket League", "rocket-league"),
    ("Overwatch 2", "overwatch-2"),
    ("Call of Duty: Black Ops 7", "call-of-duty-black-ops-7"),
    ("Dead by Daylight", "dead-by-daylight"),
    ("Escape from Tarkov", "escape-from-tarkov"),
    ("Rust", "rust"),
    ("Grand Theft Auto V (GTA)", "grand-theft-auto-v"),
    ("League of Legends", "league-of-legends"),
    ("Minecraft", "minecraft"),
    ("Roblox", "roblox"),
]

GAMBLING_TERMS = {
    "casino",
    "slots",
    "slot machine",
    "gambling",
    "roulette",
    "blackjack",
    "sportsbook",
    "sports betting",
    "betting",
}


def load_history():
    if not HISTORY_PATH.exists():
        return {
            "version": 1,
            "used_clips": [],
        }

    try:
        data = json.loads(
            HISTORY_PATH.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {
            "version": 1,
            "used_clips": [],
        }

    if isinstance(
        data,
        list,
    ):
        return {
            "version": 1,
            "used_clips": data,
        }

    if not isinstance(
        data,
        dict,
    ):
        return {
            "version": 1,
            "used_clips": [],
        }

    data.setdefault(
        "used_clips",
        [],
    )

    return data


def load_aux_history(
    path,
    key,
):
    if not path.exists():
        return {
            "version": 1,
            key: [],
        }

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {
            "version": 1,
            key: [],
        }

    if isinstance(
        data,
        list,
    ):
        return {
            "version": 1,
            key: data,
        }

    if not isinstance(
        data,
        dict,
    ):
        return {
            "version": 1,
            key: [],
        }

    data.setdefault(
        key,
        [],
    )

    if not isinstance(
        data.get(
            key
        ),
        list,
    ):
        data[
            key
        ] = []

    return data


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


def clip_id_from_url(url):
    match = re.search(
        r"(clip_[A-Za-z0-9_-]+)",
        str(
            url or ""
        ),
    )

    return (
        match.group(
            1
        )
        if match
        else None
    )


def channel_from_url(url):
    match = re.search(
        r"kick\.com/([^/]+)/clips/clip_",
        str(
            url or ""
        ),
        re.I,
    )

    return (
        match.group(
            1
        ).lower()
        if match
        else ""
    )


def identity_sets(
    history,
    key,
):
    ids = set()
    urls = set()

    for item in history.get(
        key,
        [],
    ):
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
                cid = normalize_clip_id(
                    clip_id_from_url(
                        raw
                    )
                )

                curl = normalize_clip_url(
                    raw
                )

            else:
                cid = normalize_clip_id(
                    raw
                )

                curl = ""

        elif isinstance(
            item,
            dict,
        ):
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

        else:
            continue

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


def parse_utc(value):
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

    for item in history.get(
        "used_clips",
        [],
    ):
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
                cid = clip_id_from_url(
                    raw
                )

                if cid:
                    used.add(
                        normalize_clip_id(
                            cid
                        )
                    )

            elif raw:
                used.add(
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


def discover_category(
    page,
    game,
    slug,
):
    url = (
        f"https://kick.com/"
        f"category/{slug}/clips"
    )

    print(
        f"Scanning game category: "
        f"{game}"
    )

    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=45000,
    )

    page.wait_for_timeout(
        DISCOVERY_INITIAL_WAIT_MS
    )

    html = page.content()
    links = extract_clip_links(
        html
    )

    raw_target = max(
        MAX_CLIPS_PER_GAME,
        MAX_CLIPS_PER_GAME
        *
        RAW_LINK_TARGET_MULTIPLIER,
    )

    stagnant_rounds = 0
    previous_count = len(
        links
    )

    for round_no in range(
        DISCOVERY_SCROLL_ROUNDS
    ):
        if len(
            links
        ) >= raw_target:
            print(
                f"Raw link target reached "
                f"for {game}: "
                f"{len(links)}"
            )
            break

        page.evaluate(
            "window.scrollTo("
            "0, document.body.scrollHeight)"
        )

        page.wait_for_timeout(
            DISCOVERY_SCROLL_WAIT_MS
        )

        html = page.content()
        links = extract_clip_links(
            html
        )

        if len(
            links
        ) <= previous_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
            previous_count = len(
                links
            )

        print(
            f"Discovery scroll "
            f"{round_no + 1}/"
            f"{DISCOVERY_SCROLL_ROUNDS} "
            f"for {game}: "
            f"{len(links)} raw clips"
        )

        if stagnant_rounds >= 2:
            break

    print(
        f"Found {len(links)} raw clip links "
        f"for {game}."
    )

    return links


def interleave(per_game):
    output = []

    max_len = max(
        (
            len(
                values
            )
            for values in per_game.values()
        ),
        default=0,
    )

    for position in range(
        max_len
    ):
        for game in GAME_CATEGORIES:
            game_name = game[
                0
            ]

            candidates = per_game.get(
                game_name,
                [],
            )

            if position < len(
                candidates
            ):
                output.append(
                    candidates[
                        position
                    ]
                )

                if (
                    len(
                        output
                    )
                    >=
                    MAX_TOTAL_CANDIDATES
                ):
                    return output

    return output


def main():
    history = load_history()

    (
        used,
        creator_counts,
        game_counts,
    ) = history_state(
        history
    )

    (
        published_ids,
        published_urls,
    ) = identity_sets(
        history,
        "used_clips",
    )

    used |= published_ids

    shorts_rejected_history = (
        load_aux_history(
            SHORTS_REJECTED_HISTORY_PATH,
            "clip_ids",
        )
    )

    (
        shorts_rejected_ids,
        shorts_rejected_urls,
    ) = identity_sets(
        shorts_rejected_history,
        "clip_ids",
    )

    longform_history = (
        load_aux_history(
            LONGFORM_HISTORY_PATH,
            "used_clips",
        )
    )

    rejected_history = (
        load_aux_history(
            LONGFORM_REJECTED_HISTORY_PATH,
            "rejected_clips",
        )
    )

    (
        longform_used_ids,
        longform_used_urls,
    ) = identity_sets(
        longform_history,
        "used_clips",
    )

    (
        rejected_ids,
        rejected_urls,
    ) = identity_sets(
        rejected_history,
        "rejected_clips",
    )

    print(
        "================================================"
    )
    print(
        "ViralSpawnTV V12.13 "
        "Quality-First Deep Discovery"
    )
    print(
        "================================================"
    )

    print(
        f"Target raw fresh candidates: "
        f"{MAX_TOTAL_CANDIDATES}"
    )

    print(
        f"Per-game fresh cap: "
        f"{MAX_CLIPS_PER_GAME}"
    )

    print(
        f"Published Shorts loaded: "
        f"{len(published_ids)}"
    )

    print(
        f"Rejected Shorts loaded: "
        f"{len(shorts_rejected_ids)}"
    )

    per_game = {}

    skipped_published = 0
    skipped_shorts_rejected = 0
    skipped_longform_used = 0
    skipped_longform_rejected = 0

    seen_ids = set()
    seen_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={
                "width": 1440,
                "height": 1000,
            },
        )

        for game, slug in GAME_CATEGORIES:
            if (
                game_counts.get(
                    game.lower(),
                    0,
                )
                >=
                MAX_PUBLIC_UPLOADS_PER_GAME_24H
            ):
                print(
                    f"Skipping {game}: "
                    f"24h game diversity cap reached."
                )
                continue

            try:
                links = discover_category(
                    page,
                    game,
                    slug,
                )

            except Exception as exc:
                print(
                    f"Category skipped: "
                    f"{game}: {exc}"
                )
                continue

            rows = []

            for position, url in enumerate(
                links,
                start=1,
            ):
                clip_id = clip_id_from_url(
                    url
                )

                channel = channel_from_url(
                    url
                )

                if (
                    not clip_id
                    or
                    not channel
                ):
                    continue

                normalized_id = (
                    normalize_clip_id(
                        clip_id
                    )
                )

                normalized_url = (
                    normalize_clip_url(
                        url
                    )
                )

                if (
                    normalized_id
                    in seen_ids
                    or
                    normalized_url
                    in seen_urls
                ):
                    continue

                if (
                    normalized_id in used
                    or
                    normalized_id
                    in published_ids
                    or
                    normalized_url
                    in published_urls
                ):
                    skipped_published += 1
                    continue

                if (
                    normalized_id
                    in shorts_rejected_ids
                    or
                    normalized_url
                    in shorts_rejected_urls
                ):
                    skipped_shorts_rejected += 1
                    continue

                if (
                    creator_counts.get(
                        channel,
                        0,
                    )
                    >=
                    MAX_PUBLIC_UPLOADS_PER_CREATOR_24H
                ):
                    continue

                if LONGFORM_MODE and (
                    normalized_id
                    in longform_used_ids
                    or
                    normalized_url
                    in longform_used_urls
                ):
                    skipped_longform_used += 1
                    continue

                if LONGFORM_MODE and (
                    normalized_id
                    in rejected_ids
                    or
                    normalized_url
                    in rejected_urls
                ):
                    skipped_longform_rejected += 1
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
                    }
                )

                seen_ids.add(
                    normalized_id
                )

                seen_urls.add(
                    normalized_url
                )

                if (
                    len(
                        rows
                    )
                    >=
                    MAX_CLIPS_PER_GAME
                ):
                    break

            if rows:
                per_game[
                    game
                ] = rows

                print(
                    f"{game}: "
                    f"{len(rows)} fresh candidates"
                )

        browser.close()

    candidates = interleave(
        per_game
    )

    manifest = {
        "version":
            13,
        "strategy":
            "quality_first_deep_discovery",
        "created_at_utc":
            datetime.now(
                timezone.utc
            ).isoformat(),
        "creator_cap_24h":
            MAX_PUBLIC_UPLOADS_PER_CREATOR_24H,
        "game_cap_24h":
            MAX_PUBLIC_UPLOADS_PER_GAME_24H,
        "max_clips_per_game":
            MAX_CLIPS_PER_GAME,
        "max_total_candidates":
            MAX_TOTAL_CANDIDATES,
        "games_with_candidates":
            list(
                per_game.keys()
            ),
        "candidate_count":
            len(
                candidates
            ),
        "fresh_candidate_target":
            MAX_TOTAL_CANDIDATES,
        "fresh_target_reached":
            len(
                candidates
            )
            >=
            MAX_TOTAL_CANDIDATES,
        "skipped_published_shorts":
            skipped_published,
        "skipped_rejected_shorts":
            skipped_shorts_rejected,
        "skipped_longform_history":
            skipped_longform_used,
        "skipped_longform_rejected":
            skipped_longform_rejected,
        "candidates":
            candidates,
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
        f"V12.13 games with candidates: "
        f"{len(per_game)}"
    )

    print(
        f"V12.13 fresh raw candidate pool: "
        f"{len(candidates)}"
    )

    print(
        f"Skipped published Shorts: "
        f"{skipped_published}"
    )

    print(
        f"Skipped permanently rejected Shorts: "
        f"{skipped_shorts_rejected}"
    )

    if not candidates:
        raise RuntimeError(
            "V12.13 discovery found no "
            "eligible fresh clips."
        )

    print(
        "V12.13 quality-first deep discovery complete."
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"V12.13 DISCOVERY FAILED: "
            f"{exc}"
        )
        sys.exit(
            1
        )
