import array
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

# ------------------------------------------------------------
# V12.14.7
# LOCAL FIRST -> AI SECOND
#
# Up to 100 clips are screened LOCALLY with FFmpeg/Python.
# Only ~30 strongest combat-looking clips reach AI vision.
# If OpenAI quota is unavailable, the workflow falls back to the
# local ranking instead of failing the entire Short run.
# ------------------------------------------------------------

MAX_CANDIDATES = 100

LOCAL_AI_CANDIDATES = 30
TARGET_SURVIVORS = 24
MIN_SURVIVORS = 16

MIN_FIREFIGHT_SCORE = 60
FRAME_WORKERS = 8
LOCAL_WORKERS = 8
AI_BATCH_SIZE = 10

# Low-resolution raw grayscale frames.
LOCAL_WIDTH = 96
LOCAL_HEIGHT = 54
LOCAL_FRAME_BYTES = LOCAL_WIDTH * LOCAL_HEIGHT
LOCAL_FPS = "1/5"

# These are the shooter / ranged-combat lanes where "firefight" is literal
# or very close to literal.
SHOOTER_GAMES = {
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

TITLE_COMBAT_TERMS = {
    "clutch",
    "ace",
    "1v1",
    "1v2",
    "1v3",
    "1v4",
    "1v5",
    "kill",
    "kills",
    "wipe",
    "wiped",
    "squad",
    "headshot",
    "sniper",
    "noscope",
    "no scope",
    "gunfight",
    "fight",
    "shot",
    "shots",
    "beam",
    "beamed",
    "frag",
    "elimination",
    "knock",
    "down",
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
    return "data:image/jpeg;base64," + encoded


def title_term_score(candidate):
    text = " ".join(
        [
            str(candidate.get("page_title", "")),
            str(candidate.get("metadata_text", "")),
            " ".join(candidate.get("action_hits", []) or []),
        ]
    ).casefold()

    hits = sum(
        1
        for term in TITLE_COMBAT_TERMS
        if term in text
    )

    return min(
        100.0,
        hits * 18.0,
    )


def frame_motion_metrics(media_url):
    """
    One local FFmpeg process per clip.

    We decode a tiny 96x54 grayscale frame every ~5 seconds and calculate:
      - average frame-to-frame pixel change
      - burst motion (how many pixels change substantially)
      - sustained motion (how many frame transitions are active)

    This is intentionally cheap and local. It is NOT the final firearm
    classifier. Its purpose is to eliminate obviously static / menu /
    spectating / low-action clips before any paid vision call.
    """

    command = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-i",
        media_url,
        "-vf",
        f"fps={LOCAL_FPS},scale={LOCAL_WIDTH}:{LOCAL_HEIGHT},format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]

    proc = subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=35,
    )

    raw = proc.stdout

    if len(raw) < LOCAL_FRAME_BYTES * 2:
        return {
            "frame_count": 0,
            "avg_change": 0.0,
            "burst_change": 0.0,
            "active_transition_ratio": 0.0,
        }

    count = len(raw) // LOCAL_FRAME_BYTES

    frames = [
        raw[
            i * LOCAL_FRAME_BYTES:
            (i + 1) * LOCAL_FRAME_BYTES
        ]
        for i in range(count)
    ]

    transition_means = []
    transition_bursts = []

    previous = frames[0]

    for current in frames[1:]:
        total = 0
        burst_pixels = 0

        for a, b in zip(previous, current):
            diff = abs(a - b)
            total += diff

            if diff >= 22:
                burst_pixels += 1

        mean_change = total / LOCAL_FRAME_BYTES
        burst_ratio = burst_pixels / LOCAL_FRAME_BYTES

        transition_means.append(mean_change)
        transition_bursts.append(burst_ratio)

        previous = current

    avg_change = (
        sum(transition_means)
        /
        max(1, len(transition_means))
    )

    burst_change = (
        sum(transition_bursts)
        /
        max(1, len(transition_bursts))
    )

    # Transition is "active" when there is both meaningful whole-frame
    # motion and a non-trivial fraction of strongly changing pixels.
    active = 0

    for mean_change, burst_ratio in zip(
        transition_means,
        transition_bursts,
    ):
        if (
            mean_change >= 10.0
            and
            burst_ratio >= 0.12
        ):
            active += 1

    active_transition_ratio = (
        active
        /
        max(1, len(transition_means))
    )

    return {
        "frame_count":
            count,
        "avg_change":
            round(avg_change, 4),
        "burst_change":
            round(burst_change, 4),
        "active_transition_ratio":
            round(active_transition_ratio, 4),
    }


def local_candidate_score(candidate):
    media_url = str(
        candidate.get("media_url")
        or
        candidate.get("playlist_url")
        or
        ""
    ).strip()

    if not media_url:
        raise RuntimeError(
            "candidate has no media URL"
        )

    metrics = frame_motion_metrics(
        media_url
    )

    game = str(
        candidate.get(
            "game",
            "",
        )
    ).strip().casefold()

    shooter_prior = (
        100.0
        if game in SHOOTER_GAMES
        else 10.0
    )

    # Map local raw motion into a loose 0-100 range.
    motion_score = min(
        100.0,
        metrics[
            "avg_change"
        ] * 4.5,
    )

    burst_score = min(
        100.0,
        metrics[
            "burst_change"
        ] * 260.0,
    )

    sustained_score = min(
        100.0,
        metrics[
            "active_transition_ratio"
        ] * 100.0,
    )

    title_score = title_term_score(
        candidate
    )

    # Firefight-first means shooter eligibility and sustained visual activity
    # matter more than raw views at this stage.
    score = (
        motion_score * 0.28
        +
        burst_score * 0.25
        +
        sustained_score * 0.27
        +
        shooter_prior * 0.15
        +
        title_score * 0.05
    )

    row = dict(
        candidate
    )

    row[
        "local_firefight_score"
    ] = round(
        max(
            0.0,
            min(
                100.0,
                score,
            ),
        ),
        2,
    )

    row[
        "local_motion_score"
    ] = round(
        motion_score,
        2,
    )

    row[
        "local_burst_score"
    ] = round(
        burst_score,
        2,
    )

    row[
        "local_sustained_motion_score"
    ] = round(
        sustained_score,
        2,
    )

    row[
        "local_title_combat_score"
    ] = round(
        title_score,
        2,
    )

    row[
        "local_shooter_game"
    ] = bool(
        game in SHOOTER_GAMES
    )

    row[
        "local_frame_count"
    ] = metrics[
        "frame_count"
    ]

    return row


def local_scan_parallel(candidates):
    rows = []

    with ThreadPoolExecutor(
        max_workers=LOCAL_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                local_candidate_score,
                candidate,
            ):
                candidate
            for candidate in candidates
        }

        for future in as_completed(
            future_map
        ):
            candidate = future_map[
                future
            ]

            try:
                rows.append(
                    future.result()
                )
            except Exception as exc:
                print(
                    f"LOCAL FIREFIGHT SKIP "
                    f"{candidate.get('clip_id')}: "
                    f"{exc}"
                )

    rows.sort(
        key=lambda row: (
            float(
                row.get(
                    "local_firefight_score",
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

    return rows


def extract_ai_frames(candidate):
    """
    Slightly richer visual evidence only for the ~30 clips that survived
    the local filter.
    """

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

    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    )

    if (
        not media_url
        or
        duration <= 0
    ):
        return []

    fractions = [
        0.16,
        0.34,
        0.52,
        0.70,
        0.86,
    ]

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

    output = []

    for index, fraction in enumerate(
        fractions
    ):
        timestamp = max(
            0.5,
            min(
                duration - 0.5,
                duration * fraction,
            ),
        )

        path = (
            clip_dir
            /
            f"ai_{index}.jpg"
        )

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
            str(path),
        ]

        try:
            subprocess.run(
                command,
                check=True,
                timeout=35,
            )
        except Exception:
            continue

        if (
            path.exists()
            and
            path.stat().st_size > 1000
        ):
            output.append(
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

    return output


def extract_ai_parallel(candidates):
    results = {}

    with ThreadPoolExecutor(
        max_workers=FRAME_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                extract_ai_frames,
                candidate,
            ):
                candidate
            for candidate in candidates
        }

        for future in as_completed(
            future_map
        ):
            candidate = future_map[
                future
            ]

            clip_id = str(
                candidate.get(
                    "clip_id",
                    "",
                )
            )

            try:
                results[
                    clip_id
                ] = future.result()
            except Exception:
                results[
                    clip_id
                ] = []

    return results


def classify_batch(
    client,
    batch,
):
    prompt = """
You are the PAID SECOND-STAGE ACTIVE-FIREFIGHT CHECK for ViralSpawnTV.

The clips reaching you have ALREADY survived a free local motion/action
prefilter. Your job is narrower:

KEEP clips that visibly contain REAL direct PvP firefights/ranged combat.

For shooter games, require convincing evidence such as:
- firearm/weapon fire
- visible enemies being actively engaged
- aiming and firing exchanges
- hit markers / damage / shield breaks
- explosions or abilities being used directly in combat
- kill/knock/elimination activity
- multiple sampled moments from the same active fight

Do NOT treat these as firefights:
- merely holding an angle
- running toward combat
- looting
- inventory/menu screens
- spectating
- chase-only footage
- melee-only action
- post-fight celebration
- one isolated shot with otherwise inactive footage

Return ONLY JSON:
{
  "results": [
    {
      "id": 0,
      "firefight_score": 0-100,
      "active_samples": 0-5,
      "sustained_combat": true,
      "enemy_engagement": 0-100,
      "combat_intensity": 0-100,
      "dead_time_risk": 0-100,
      "content_type": "gaming",
      "reason": "short explanation"
    }
  ]
}

A firefight_score of 60+ should mean this clip is genuinely worth sending
to the more expensive payoff/story pipeline.
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
                        f"local_firefight_score="
                        f"{candidate.get('local_firefight_score')}\n"
                        f"local_motion="
                        f"{candidate.get('local_motion_score')}\n"
                        f"local_sustained="
                        f"{candidate.get('local_sustained_motion_score')}\n"
                        "NEXT: SAMPLED FRAMES"
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


def final_ai_rank(row):
    return round(
        row[
            "active_firefight_score"
        ] * 0.65
        +
        row[
            "active_enemy_engagement"
        ] * 0.15
        +
        row[
            "active_combat_intensity"
        ] * 0.10
        +
        float(
            row.get(
                "local_firefight_score",
                0,
            )
        ) * 0.10,
        2,
    )


def local_only_survivors(local_rows):
    """
    API-credit fallback.

    We still strongly prefer shooter games and actual sustained local motion.
    This keeps the workflow alive instead of failing with insufficient_quota.
    """

    strict = [
        row
        for row in local_rows
        if (
            row.get(
                "local_shooter_game",
                False,
            )
            and
            row.get(
                "local_firefight_score",
                0,
            )
            >=
            48
            and
            row.get(
                "local_sustained_motion_score",
                0,
            )
            >=
            30
        )
    ]

    if len(
        strict
    ) < MIN_SURVIVORS:
        used = {
            str(
                row.get(
                    "clip_id",
                    "",
                )
            )
            for row in strict
        }

        for row in local_rows:
            clip_id = str(
                row.get(
                    "clip_id",
                    "",
                )
            )

            if (
                not clip_id
                or
                clip_id in used
            ):
                continue

            if not row.get(
                "local_shooter_game",
                False,
            ):
                continue

            fallback = dict(
                row
            )

            fallback[
                "active_firefight_backfill"
            ] = True

            strict.append(
                fallback
            )

            used.add(
                clip_id
            )

            if len(
                strict
            ) >= MIN_SURVIVORS:
                break

    output = []

    for row in strict[
        :TARGET_SURVIVORS
    ]:
        candidate = dict(
            row
        )

        candidate[
            "active_firefight_score"
        ] = clamp(
            candidate.get(
                "local_firefight_score",
                0,
            )
        )

        candidate[
            "active_firefight_samples"
        ] = 0

        candidate[
            "active_sustained_combat"
        ] = bool(
            candidate.get(
                "local_sustained_motion_score",
                0,
            )
            >=
            35
        )

        candidate[
            "active_enemy_engagement"
        ] = 0

        candidate[
            "active_combat_intensity"
        ] = clamp(
            candidate.get(
                "local_motion_score",
                0,
            )
        )

        candidate[
            "active_dead_time_risk"
        ] = clamp(
            100
            -
            candidate.get(
                "local_sustained_motion_score",
                0,
            )
        )

        candidate[
            "active_firefight_reason"
        ] = (
            "Local-only fallback because paid vision was unavailable."
        )

        candidate[
            "active_firefight_rank"
        ] = round(
            float(
                candidate.get(
                    "local_firefight_score",
                    0,
                )
            ),
            2,
        )

        output.append(
            candidate
        )

    return output


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

    candidates = [
        row
        for row in payload.get(
            "candidates",
            [],
        )
        if isinstance(
            row,
            dict,
        )
    ][
        :MAX_CANDIDATES
    ]

    if not candidates:
        raise RuntimeError(
            "No candidates available."
        )

    print(
        f"V12.14.7 LOCAL FIREFIGHT PREFILTER: "
        f"{len(candidates)} candidates."
    )

    local_rows = local_scan_parallel(
        candidates
    )

    if not local_rows:
        raise RuntimeError(
            "Local firefight scan produced no usable clips."
        )

    # Prefer shooter-game footage before paid AI.
    local_shooters = [
        row
        for row in local_rows
        if row.get(
            "local_shooter_game",
            False,
        )
    ]

    if not local_shooters:
        local_shooters = local_rows

    ai_candidates = local_shooters[
        :LOCAL_AI_CANDIDATES
    ]

    print(
        f"V12.14.7 LOCAL PREFILTER COMPLETE: "
        f"{len(candidates)} -> "
        f"{len(local_rows)} locally scored -> "
        f"{len(ai_candidates)} sent to paid vision."
    )

    for index, row in enumerate(
        ai_candidates[
            :12
        ],
        1,
    ):
        print(
            f"LOCAL TOP {index}: "
            f"{row.get('clip_id')} | "
            f"game={row.get('game')} | "
            f"local={row.get('local_firefight_score')} | "
            f"motion={row.get('local_motion_score')} | "
            f"sustained={row.get('local_sustained_motion_score')}"
        )

    # --------------------------------------------------------
    # Paid AI confirmation only on the top ~30.
    # If quota is unavailable, degrade gracefully to local-only.
    # --------------------------------------------------------

    frames_by_id = extract_ai_parallel(
        ai_candidates
    )

    visual_items = []

    for candidate in ai_candidates:
        clip_id = str(
            candidate.get(
                "clip_id",
                "",
            )
        )

        frames = frames_by_id.get(
            clip_id,
            [],
        )

        if not frames:
            continue

        visual_items.append(
            {
                "candidate":
                    candidate,
                "frames":
                    frames,
            }
        )

    scored_rows = []
    ai_available = True
    ai_error = ""

    if visual_items:
        try:
            client = OpenAI()

            for start in range(
                0,
                len(visual_items),
                AI_BATCH_SIZE,
            ):
                batch = visual_items[
                    start:
                    start + AI_BATCH_SIZE
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
                            5,
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
                    ] = final_ai_rank(
                        candidate
                    )

                    scored_rows.append(
                        candidate
                    )

        except Exception as exc:
            ai_available = False
            ai_error = str(
                exc
            )

            print(
                "V12.14.7 PAID VISION UNAVAILABLE: "
                f"{ai_error}"
            )

    if ai_available and scored_rows:
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
                        "local_firefight_score",
                        0,
                    )
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

        used = {
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
                    clip_id in used
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

                used.add(
                    clip_id
                )

                if len(
                    survivors
                ) >= MIN_SURVIVORS:
                    break

    else:
        survivors = local_only_survivors(
            local_rows
        )

    if not survivors:
        raise RuntimeError(
            "No active-combat candidates survived local/AI filtering."
        )

    output = {
        "version":
            "12.14.7-local-first-firefight",
        "strategy":
            "100_local_motion_scan_then_top30_paid_firefight_confirmation",
        "input_count":
            len(candidates),
        "local_scored_count":
            len(local_rows),
        "paid_ai_candidate_count":
            len(ai_candidates),
        "paid_ai_available":
            ai_available,
        "paid_ai_error":
            ai_error,
        "survivor_count":
            len(survivors),
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
        f"V12.14.7 FIREFIGHT FILTER COMPLETE: "
        f"{len(candidates)} initial -> "
        f"{len(ai_candidates)} paid-check candidates -> "
        f"{len(survivors)} survivors | "
        f"paid_ai_available={ai_available} | "
        f"{time.perf_counter() - started:.1f}s"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"V12.14.7 FIREFIGHT FILTER ERROR: "
            f"{exc}"
        )
        sys.exit(1)
