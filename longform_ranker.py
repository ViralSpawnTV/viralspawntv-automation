import json
import re
import sys
from pathlib import Path

from openai import OpenAI
from playwright.sync_api import sync_playwright


MANIFEST = Path("work/v11_candidate_manifest.json")
OUT = Path("work/longform/ranked_sources.json")

# Permanent history of clips already published in
# ViralSpawnTV long-form videos.
LONGFORM_HISTORY = Path("longform_history.json")

# V1.5 expanded source pool.
#
# Inspect the full discovery manifest and give downstream gates
# a deep enough bench to survive:
# - acquisition failures
# - English-language rejection
# - music/content rejection
# - source quality rejection
# - motion/activity rejection
#
# The final production system still stops once enough approved
# footage exists for the 8-10 minute episode.
MAX_INSPECT = 80
MAX_SELECT = 48

MAX_PER_CREATOR = 4
MAX_PER_GAME = 8
MIN_SCORE = 30


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130 Safari/537.36"
)


BLOCKED = {
    "casino",
    "gambling",
    "slots",
    "roulette",
    "blackjack",
    "sportsbook",
    "sports betting",
    "betting",
    "crypto",
    "prediction market",
}


def load_longform_history():
    """
    Load permanent long-form clip history.

    A missing or malformed history file is treated as an empty
    history so the pipeline can still run on first use.
    """

    if not LONGFORM_HISTORY.exists():
        return {
            "version": 1,
            "used_clips": [],
        }

    try:
        data = json.loads(
            LONGFORM_HISTORY.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return {
            "version": 1,
            "used_clips": [],
        }

    if isinstance(data, list):
        return {
            "version": 1,
            "used_clips": data,
        }

    if not isinstance(data, dict):
        return {
            "version": 1,
            "used_clips": [],
        }

    data.setdefault("version", 1)
    data.setdefault("used_clips", [])

    if not isinstance(
        data.get("used_clips"),
        list,
    ):
        data["used_clips"] = []

    return data


def build_used_clip_sets(history):
    """
    Build permanent exclusion sets from previously published
    long-form episodes.

    clip_id is the primary identifier.

    Source/clip URL is also checked as a second layer so the same
    source cannot return merely because metadata formatting changed.
    """

    used_clip_ids = set()
    used_clip_urls = set()

    for item in history.get(
        "used_clips",
        [],
    ):
        if not isinstance(item, dict):
            continue

        clip_id = str(
            item.get("clip_id")
            or ""
        ).strip()

        clip_url = str(
            item.get("clip_url")
            or item.get("source")
            or ""
        ).strip()

        if clip_id:
            used_clip_ids.add(clip_id)

        if clip_url:
            used_clip_urls.add(clip_url)

    return (
        used_clip_ids,
        used_clip_urls,
    )


def candidate_was_used(
    candidate,
    used_clip_ids,
    used_clip_urls,
):
    """
    Return True when this exact source has already appeared in a
    successfully published ViralSpawnTV long-form video.
    """

    clip_id = str(
        candidate.get("clip_id")
        or ""
    ).strip()

    clip_url = str(
        candidate.get("clip_url")
        or candidate.get("source")
        or ""
    ).strip()

    if (
        clip_id
        and clip_id in used_clip_ids
    ):
        return True

    if (
        clip_url
        and clip_url in used_clip_urls
    ):
        return True

    return False


def inspect(page, candidate):

    try:

        page.goto(
            candidate["clip_url"],
            wait_until="domcontentloaded",
            timeout=45000,
        )

        page.wait_for_timeout(900)

        title = page.title() or ""

        desc = ""

        node = page.locator(
            'meta[name="description"]'
        )

        if node.count():

            desc = (
                node.first.get_attribute(
                    "content"
                )
                or ""
            )

        text = (
            title
            + " "
            + desc
        ).lower()

        if any(
            term in text
            for term in BLOCKED
        ):
            return None

        return {
            **candidate,
            "page_title": title,
            "page_description": desc,
        }

    except Exception:

        # Keep the candidate available to the AI ranker even
        # when metadata inspection fails. Later source gates
        # still protect production.
        return {
            **candidate,
            "page_title": "",
            "page_description": "",
        }


def main():

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
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

    if not all_candidates:

        raise RuntimeError(
            "Discovery manifest contained "
            "no long-form candidates."
        )

    # ---------------------------------------------------------
    # PERMANENT LONG-FORM DUPLICATE EXCLUSION
    # ---------------------------------------------------------

    history = load_longform_history()

    (
        used_clip_ids,
        used_clip_urls,
    ) = build_used_clip_sets(history)

    eligible_candidates = []
    skipped_history = []

    for candidate in all_candidates:

        if candidate_was_used(
            candidate,
            used_clip_ids,
            used_clip_urls,
        ):
            skipped_history.append(
                candidate
            )
            continue

        eligible_candidates.append(
            candidate
        )

    print(
        "\nLong-form permanent history:"
    )

    print(
        f"  Previously used clip IDs: "
        f"{len(used_clip_ids)}"
    )

    print(
        f"  Previously used source URLs: "
        f"{len(used_clip_urls)}"
    )

    print(
        f"  Discovery candidates excluded "
        f"by long-form history: "
        f"{len(skipped_history)}"
    )

    for candidate in skipped_history:

        print(
            "  SKIPPED USED CLIP: "
            f"{candidate.get('clip_id', '')} | "
            f"{candidate.get('game', '')} | "
            f"{candidate.get('channel', '')}"
        )

    candidates = eligible_candidates[
        :MAX_INSPECT
    ]

    if not candidates:

        raise RuntimeError(
            "All discovery candidates were "
            "already used in previous "
            "long-form videos."
        )

    inspected = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={
                "width": 1280,
                "height": 800,
            },
        )

        for candidate in candidates:

            row = inspect(
                page,
                candidate,
            )

            if row:
                inspected.append(row)

        browser.close()

    if not inspected:

        raise RuntimeError(
            "No candidates survived "
            "metadata inspection."
        )

    compact = [
        {
            "index": i,
            "game": item.get("game"),
            "channel": item.get("channel"),
            "title": item.get(
                "page_title"
            ),
            "description": item.get(
                "page_description"
            ),
        }

        for i, item
        in enumerate(inspected)
    ]

    prompt = f"""
Rank gaming clips for an original narrated ViralSpawnTV
long-form compilation.

TARGET EPISODE:
8-10 minutes of genuinely entertaining gaming footage.

IMPORTANT:
Downstream systems will perform additional source-quality,
English-language, music/content, motion/activity and audio checks.
Because some clips will be rejected later, provide a DEEP ranked
bench of good candidates.

Clips previously published in ViralSpawnTV long-form videos have
already been removed from this candidate list.

Prefer clips likely to contain a clear visual event:

- clutch plays
- fails
- ambushes
- rage or funny reactions
- boss fights
- high-kill plays
- surprises
- comebacks
- funny accidents
- impressive skill
- escapes
- chases
- close calls
- chaotic fights
- unexpected gaming moments

Penalize:

- generic talking
- podcasts
- movie watching
- IRL content
- politics
- crypto
- gambling
- menus
- loading screens
- generic metadata with no likely gaming event

QUALITY comes first.

VARIETY comes second.

We need enough candidates to build a full episode after
downstream quality rejection.

Do not intentionally fill the list with obviously weak clips
just to reach the maximum.

Return ONLY JSON:

{{
  "ranked": [
    {{
      "index": 0,
      "score": 80,
      "reason": "brief reason"
    }}
  ]
}}

Return up to {MAX_SELECT} candidates.

Include plausible gaming candidates scoring {MIN_SCORE}+.

CANDIDATES:

{json.dumps(compact, ensure_ascii=False)}
"""

    client = OpenAI()

    response = client.responses.create(
        model="gpt-5.6",
        input=prompt,
    )

    raw = re.sub(
        r"^```json\s*|\s*```$",
        "",
        response.output_text.strip(),
    )

    ranked_json = json.loads(raw)

    chosen = []

    creator_counts = {}
    game_counts = {}

    seen_indexes = set()

    for row in ranked_json.get(
        "ranked",
        [],
    ):

        try:

            idx = int(
                row["index"]
            )

            score = int(
                row["score"]
            )

            if (
                idx < 0
                or idx >= len(inspected)
                or idx in seen_indexes
            ):
                continue

            candidate = inspected[idx]

        except Exception:
            continue

        if score < MIN_SCORE:
            continue

        # Second defensive history check.
        #
        # The candidates were already filtered before inspection,
        # but this makes sure a used clip cannot accidentally
        # enter the final ranked output.
        if candidate_was_used(
            candidate,
            used_clip_ids,
            used_clip_urls,
        ):
            continue

        creator = (
            candidate.get(
                "channel",
                "",
            )
            or ""
        ).lower()

        game = (
            candidate.get(
                "game",
                "",
            )
            or ""
        ).lower()

        if (
            creator_counts.get(
                creator,
                0,
            )
            >= MAX_PER_CREATOR
        ):
            continue

        if (
            game_counts.get(
                game,
                0,
            )
            >= MAX_PER_GAME
        ):
            continue

        seen_indexes.add(idx)

        creator_counts[creator] = (
            creator_counts.get(
                creator,
                0,
            )
            + 1
        )

        game_counts[game] = (
            game_counts.get(
                game,
                0,
            )
            + 1
        )

        chosen.append(
            {
                **candidate,
                "longform_rank_score":
                    score,
                "longform_rank_reason":
                    row.get(
                        "reason",
                        "",
                    ),
            }
        )

        if len(chosen) >= MAX_SELECT:
            break

    if len(chosen) < 10:

        raise RuntimeError(
            f"Only {len(chosen)} usable "
            "long-form candidates. "
            "V1.5 requires at least 10 "
            "before acquisition."
        )

    OUT.write_text(
        json.dumps(
            {
                "version": "1.5",
                "target_episode_minutes":
                    "8-10",
                "history_file":
                    str(LONGFORM_HISTORY),
                "historical_clip_ids":
                    len(used_clip_ids),
                "historical_urls":
                    len(used_clip_urls),
                "history_excluded_count":
                    len(skipped_history),
                "eligible_before_inspection":
                    len(eligible_candidates),
                "inspected_count":
                    len(inspected),
                "selected_count":
                    len(chosen),
                "candidates":
                    chosen,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "\n"
        f"Long-form V1.5 ranker inspected "
        f"{len(inspected)} sources and selected "
        f"{len(chosen)} candidates."
    )

    print(
        f"Permanent history excluded "
        f"{len(skipped_history)} candidates."
    )

    for i, candidate in enumerate(
        chosen,
        1,
    ):

        print(
            f"{i}. "
            f"{candidate['longform_rank_score']} | "
            f"{candidate.get('game')} | "
            f"{candidate.get('channel')} | "
            f"{candidate.get('page_title', '')[:80]}"
        )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            "LONGFORM RANKER FAILED:",
            exc,
        )

        sys.exit(1)
