import base64
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI


INPUT = Path("work/v12_ranked_candidates.json")
OUT = Path("work/v12_firefight_candidates.json")
WORK = Path("work/firefight_prescreen")
WORK.mkdir(parents=True, exist_ok=True)

# Inspect up to the full original search pool.
MAX_CANDIDATES = 100

# Strong preference for clips already showing active combat.
MIN_FIREFIGHT_SCORE = 60

# Reliability: if strict firefight filtering is unusually thin, keep the best
# action-heavy clips so the workflow still has enough material to finish.
TARGET_SURVIVORS = 30
MIN_SURVIVORS = 20

FRAME_WORKERS = 8
BATCH_SIZE = 10

# Four visual moments across each source are enough for a cheap "is combat
# actually happening?" pass, and are much cheaper than story/payoff analysis.
SAMPLE_FRACTIONS = [
    0.18,
    0.38,
    0.60,
    0.82,
]

# Games where the user's desired "active firefight" concept is directly
# applicable. These are broad labels from the current Kick discovery pool.
FIREFIGHT_GAMES = {
    "call of duty: warzone",
    "valorant",
    "counter-strike 2",
    "fortnite",
    "apex legends",
    "marvel rivals",
    "overwatch 2",
    "call of duty: black ops 7",
    "escape from tarkov",
    "rust",
    "grand theft auto v (gta)",
}

# Other games are allowed only when the visuals clearly show active combat
# equivalent to a firefight. This keeps creator diversity while biasing hard
# toward actual action.
ACTION_EQUIVALENT_GAMES = {
    "dead by daylight",
    "league of legends",
}


def safe_name(value):
    return re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(value or "clip"),
    )[:80]


def clamp(value):
    try:
        return max(
            0,
            min(
                100,
                int(
                    round(
                        float(value)
                    )
                ),
            ),
        )
    except Exception:
        return 0


def parse_json(raw):
    raw = str(raw or "").strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def data_url(path):
    encoded = base64.b64encode(
        Path(path).read_bytes()
    ).decode("ascii")

    return (
        "data:image/jpeg;base64,"
        +
        encoded
    )


def ffmpeg_frame(
    media_url,
    timestamp,
    out_path,
):
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        media_url,
        "-frames:v",
        "1",
        "-vf",
        "scale=384:-2",
        "-q:v",
        "5",
        str(out_path),
    ]

    subprocess.run(
        command,
        check=True,
        timeout=35,
    )

    return (
        out_path.exists()
        and
        out_path.stat().st_size > 1000
    )


def extract_candidate_frames(item):
    candidate = item[
        "candidate"
    ]

    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    )

    media_url = str(
        candidate.get("media_url")
        or
        candidate.get("playlist_url")
        or
        ""
    ).strip()

    duration = float(
        candidate.get(
            "source_duration_seconds",
            0,
        )
        or
        0
    )

    if (
        not clip_id
        or
        not media_url
        or
        duration < 10
    ):
        return []

    clip_dir = (
        WORK
        /
        safe_name(
            clip_id
        )
    )

    clip_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    frames = []

    for index, fraction in enumerate(
        SAMPLE_FRACTIONS
    ):
        timestamp = max(
            0.5,
            min(
                duration - 0.5,
                duration
                *
                fraction,
            ),
        )

        path = (
            clip_dir
            /
            f"sample_{index}.jpg"
        )

        try:
            if ffmpeg_frame(
                media_url,
                timestamp,
                path,
            ):
                frames.append(
                    {
                        "timestamp":
                            round(
                                timestamp,
                                2,
                            ),
                        "path":
                            str(
                                path
                            ),
                    }
                )
        except Exception:
            continue

    return frames


def extract_parallel(items):
    results = {}

    with ThreadPoolExecutor(
        max_workers=FRAME_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                extract_candidate_frames,
                item,
            ):
                item
            for item in items
        }

        for future in as_completed(
            future_map
        ):
            item = future_map[
                future
            ]

            try:
                results[
                    item[
                        "id"
                    ]
                ] = future.result()
            except Exception:
                results[
                    item[
                        "id"
                    ]
                ] = []

    return results


def classify_batch(
    client,
    batch,
):
    prompt = """
You are the V12.14.6 ACTIVE-FIREFIGHT PRESCREENER for ViralSpawnTV.

The user's goal is to bias the source pool toward clips where meaningful
combat is ALREADY HAPPENING, not clips that spend most of their runtime
walking, looting, camping, spectating, talking, using menus, or waiting.

For shooters, reward:
- weapon fire / muzzle flash
- enemies visibly engaged
- aiming/firing exchanges
- damage indicators / hit markers / shields breaking
- explosions / grenades during combat
- rapid combat movement
- kill feed / knock / elimination activity
- repeated combat evidence across multiple samples

For hero shooters / ability combat, count sustained direct PvP engagement
as firefight-equivalent even if the weapon is not a conventional gun.

Do NOT give a high score for:
- a player merely holding an angle with nobody engaged
- running toward a fight
- looting / inventory
- menus / respawn screens
- spectating
- one isolated shot with otherwise inactive footage
- post-fight celebration without visible combat
- PvE or non-combat social footage

Return:
- firefight_score 0-100
- active_samples 0-4
- sustained_combat true/false
- enemy_engagement 0-100
- combat_intensity 0-100
- dead_time_risk 0-100
- content_type gaming/non_gaming/gambling/music_performance
- reason

A score of 60+ should mean there is convincing evidence that the source
contains an actual active firefight/combat sequence worth sending deeper
into the Shorts pipeline.

Return ONLY JSON:
{
  "results": [
    {
      "id": 0,
      "firefight_score": 78,
      "active_samples": 3,
      "sustained_combat": true,
      "enemy_engagement": 82,
      "combat_intensity": 76,
      "dead_time_risk": 22,
      "content_type": "gaming",
      "reason": "Three sampled moments show direct PvP engagement with weapon fire and visible enemies."
    }
  ]
}
"""

    content = [
        {
            "type":
                "input_text",
            "text":
                prompt,
        }
    ]

    for local_id, item in enumerate(
        batch
    ):
        candidate = item[
            "candidate"
        ]

        content.append(
            {
                "type":
                    "input_text",
                "text":
                    (
                        f"CANDIDATE {local_id}\n"
                        f"clip_id={candidate.get('clip_id')}\n"
                        f"game={candidate.get('game')}\n"
                        f"title={candidate.get('page_title', '')}\n"
                        f"duration={candidate.get('source_duration_seconds')}\n"
                        f"traction_score={candidate.get('v12_metadata_score')}\n"
                        "NEXT: FOUR SAMPLED SOURCE FRAMES"
                    ),
            }
        )

        for frame in item.get(
            "frames",
            [],
        ):
            path = Path(
                frame[
                    "path"
                ]
            )

            if not path.exists():
                continue

            content.append(
                {
                    "type":
                        "input_text",
                    "text":
                        (
                            f"FRAME around "
                            f"{frame['timestamp']:.2f}s"
                        ),
                }
            )

            content.append(
                {
                    "type":
                        "input_image",
                    "image_url":
                        data_url(
                            path
                        ),
                }
            )

    response = client.responses.create(
        model="gpt-5.6",
        input=[
            {
                "role":
                    "user",
                "content":
                    content,
            }
        ],
    )

    data = parse_json(
        response.output_text
    )

    return data.get(
        "results",
        [],
    )


def final_firefight_rank(row):
    # Visual combat dominates. Existing audience traction breaks ties.
    return round(
        row[
            "active_firefight_score"
        ] * 0.70
        +
        row[
            "active_enemy_engagement"
        ] * 0.10
        +
        row[
            "active_combat_intensity"
        ] * 0.10
        +
        float(
            row.get(
                "v12_metadata_score",
                0,
            )
            or
            0
        ) * 0.10,
        2,
    )


def main():
    if not INPUT.exists():
        raise RuntimeError(
            "Missing work/v12_ranked_candidates.json"
        )

    started = time.perf_counter()

    payload = json.loads(
        INPUT.read_text(
            encoding="utf-8"
        )
    )

    candidates = payload.get(
        "candidates",
        [],
    )

    candidates = [
        row
        for row in candidates
        if isinstance(
            row,
            dict,
        )
    ][
        :MAX_CANDIDATES
    ]

    if not candidates:
        raise RuntimeError(
            "No ranked candidates available for active-firefight scan."
        )

    items = [
        {
            "id":
                index,
            "candidate":
                candidate,
        }
        for index, candidate in enumerate(
            candidates
        )
    ]

    print(
        f"V12.14.6 ACTIVE-FIREFIGHT SCAN: "
        f"{len(items)} ranked sources."
    )

    extracted = extract_parallel(
        items
    )

    visual_items = []

    for item in items:
        frames = extracted.get(
            item[
                "id"
            ],
            [],
        )

        if not frames:
            continue

        row = dict(
            item
        )

        row[
            "frames"
        ] = frames

        visual_items.append(
            row
        )

    if not visual_items:
        raise RuntimeError(
            "Could not extract frames for active-firefight scan."
        )

    client = OpenAI()
    scored_rows = []

    for start in range(
        0,
        len(visual_items),
        BATCH_SIZE,
    ):
        batch = visual_items[
            start:
            start + BATCH_SIZE
        ]

        results = classify_batch(
            client,
            batch,
        )

        by_id = {}

        for result in results:
            try:
                by_id[
                    int(
                        result.get(
                            "id"
                        )
                    )
                ] = result
            except Exception:
                pass

        for local_id, item in enumerate(
            batch
        ):
            result = by_id.get(
                local_id
            )

            if not result:
                continue

            content_type = str(
                result.get(
                    "content_type",
                    "gaming",
                )
            ).strip().lower()

            if content_type in {
                "gambling",
                "non_gaming",
                "music_performance",
            }:
                continue

            candidate = dict(
                item[
                    "candidate"
                ]
            )

            candidate[
                "active_firefight_score"
            ] = clamp(
                result.get(
                    "firefight_score"
                )
            )

            candidate[
                "active_firefight_samples"
            ] = max(
                0,
                min(
                    4,
                    int(
                        result.get(
                            "active_samples",
                            0,
                        )
                        or
                        0
                    ),
                ),
            )

            candidate[
                "active_sustained_combat"
            ] = bool(
                result.get(
                    "sustained_combat",
                    False,
                )
            )

            candidate[
                "active_enemy_engagement"
            ] = clamp(
                result.get(
                    "enemy_engagement"
                )
            )

            candidate[
                "active_combat_intensity"
            ] = clamp(
                result.get(
                    "combat_intensity"
                )
            )

            candidate[
                "active_dead_time_risk"
            ] = clamp(
                result.get(
                    "dead_time_risk"
                )
            )

            candidate[
                "active_firefight_reason"
            ] = str(
                result.get(
                    "reason",
                    "",
                )
            ).strip()

            candidate[
                "active_firefight_rank"
            ] = final_firefight_rank(
                candidate
            )

            scored_rows.append(
                candidate
            )

    if not scored_rows:
        raise RuntimeError(
            "No gaming clips survived active-firefight classification."
        )

    scored_rows.sort(
        key=lambda row: (
            float(
                row.get(
                    "active_firefight_rank",
                    0,
                )
            ),
            float(
                row.get(
                    "v12_metadata_score",
                    0,
                )
                or
                0
            ),
        ),
        reverse=True,
    )

    strict = [
        row
        for row in scored_rows
        if (
            row.get(
                "active_firefight_score",
                0,
            )
            >=
            MIN_FIREFIGHT_SCORE
            and
            (
                row.get(
                    "active_sustained_combat",
                    False,
                )
                or
                row.get(
                    "active_firefight_samples",
                    0,
                )
                >=
                2
            )
        )
    ]

    survivors = strict[
        :TARGET_SURVIVORS
    ]

    # Reliability backfill: active combat still ranks every clip. If the
    # strict set is too small, use highest-scoring action clips rather than
    # returning no content.
    survivor_ids = {
        str(
            row.get(
                "clip_id",
                "",
            )
        )
        for row in survivors
    }

    if len(
        survivors
    ) < MIN_SURVIVORS:
        for row in scored_rows:
            clip_id = str(
                row.get(
                    "clip_id",
                    "",
                )
            )

            if (
                not clip_id
                or
                clip_id in survivor_ids
            ):
                continue

            fallback = dict(
                row
            )

            fallback[
                "active_firefight_backfill"
            ] = True

            survivors.append(
                fallback
            )

            survivor_ids.add(
                clip_id
            )

            if len(
                survivors
            ) >= MIN_SURVIVORS:
                break

    survivors = survivors[
        :TARGET_SURVIVORS
    ]

    output = {
        "version":
            "12.14.6-active-firefight-first",
        "strategy":
            "100_source_visual_combat_scan_before_payoff_story",
        "input_count":
            len(candidates),
        "visually_scored_count":
            len(scored_rows),
        "strict_firefight_count":
            len(strict),
        "survivor_count":
            len(survivors),
        "minimum_firefight_score":
            MIN_FIREFIGHT_SCORE,
        "candidates":
            survivors,
    }

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUT.write_text(
        json.dumps(
            output,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print()
    print(
        f"V12.14.6 ACTIVE-FIREFIGHT COMPLETE: "
        f"{len(candidates)} scanned -> "
        f"{len(strict)} strict active-combat -> "
        f"{len(survivors)} sent to payoff/story prescreener | "
        f"{time.perf_counter() - started:.1f}s"
    )

    for index, row in enumerate(
        survivors[
            :15
        ],
        1,
    ):
        print(
            f"FIREFIGHT TOP {index}: "
            f"{row.get('clip_id')} | "
            f"game={row.get('game')} | "
            f"score={row.get('active_firefight_score')} | "
            f"samples={row.get('active_firefight_samples')} | "
            f"sustained={row.get('active_sustained_combat')} | "
            f"dead_time={row.get('active_dead_time_risk')} | "
            f"rank={row.get('active_firefight_rank')}"
        )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"V12.14.6 ACTIVE-FIREFIGHT ERROR: "
            f"{exc}"
        )
        sys.exit(1)
