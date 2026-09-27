import base64
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI


INPUT = Path("work/v12_ranked_candidates.json")
OUT = Path("work/v12_prescreened_candidates.json")
WORK = Path("work/viral_prescreen")
WORK.mkdir(parents=True, exist_ok=True)

MAX_VISUAL_CANDIDATES = 24
PROMOTE_COUNT = 8
BATCH_SIZE = 12

# Remote FFmpeg preview extraction was the largest V12.9 cost after the
# browser ranker. Run a few independent sources in parallel.
FRAME_WORKERS = 3

TARGET_FINAL_SCORE = 72

MIN_SOURCE_SECONDS = 49.0
TARGET_WINDOW_SECONDS = 55.0
MIN_WINDOW_SECONDS = 49.0
MAX_WINDOW_SECONDS = 58.0

# Primary-quality targets.
MIN_PRIMARY_PREDICTED = 72
MIN_PRIMARY_HOOK = 75
MIN_PRIMARY_FIRST_SECOND_CLARITY = 62
MIN_PRIMARY_CURIOSITY = 68
MIN_PRIMARY_STORY = 62
MIN_PRIMARY_PAYOFF = 62
MIN_PRIMARY_ENDING = 58
MIN_PRIMARY_CLARITY = 58

# Fallback candidates still need to be close enough to justify Whisper /
# full-gate expense.
MIN_FALLBACK_PREDICTED = 65
MIN_FALLBACK_HOOK = 68
MIN_FALLBACK_STORY = 58
MIN_FALLBACK_PAYOFF = 58
MIN_FALLBACK_RANK = 64.0


def safe_name(value):
    return re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(value or "clip"),
    )[:80]


def data_url(path):
    encoded = base64.b64encode(
        path.read_bytes()
    ).decode("ascii")

    return (
        f"data:image/jpeg;base64,{encoded}"
    )


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

    raw = re.sub(
        r"^```json\s*",
        "",
        raw,
    )

    raw = re.sub(
        r"\s*```$",
        "",
        raw,
    )

    return json.loads(raw)


def make_windows(source_seconds):
    """
    Build 1-3 candidate 55-second windows.

    Short eligible sources get one window.
    Medium sources get early + late.
    Longer sources get early + middle + late.
    """
    seconds = float(
        source_seconds
    )

    if seconds < MIN_SOURCE_SECONDS:
        return []

    if seconds <= MAX_WINDOW_SECONDS:
        return [
            {
                "start": 0.0,
                "end": round(
                    seconds,
                    3,
                ),
                "label": "full",
            }
        ]

    length = min(
        TARGET_WINDOW_SECONDS,
        seconds,
    )

    max_start = max(
        0.0,
        seconds - length,
    )

    if seconds <= 75:
        raw_starts = [
            0.0,
            max_start,
        ]
    else:
        raw_starts = [
            0.0,
            max_start / 2.0,
            max_start,
        ]

    starts = []

    for value in raw_starts:
        value = round(
            max(
                0.0,
                min(
                    value,
                    max_start,
                ),
            ),
            3,
        )

        if not any(
            abs(
                value - old
            ) < 4.0
            for old in starts
        ):
            starts.append(
                value
            )

    labels = [
        "early",
        "middle",
        "late",
    ]

    windows = []

    for i, start in enumerate(
        starts
    ):
        end = min(
            seconds,
            start + length,
        )

        if (
            end - start
            <
            MIN_WINDOW_SECONDS
        ):
            continue

        windows.append(
            {
                "start":
                    round(
                        start,
                        3,
                    ),
                "end":
                    round(
                        end,
                        3,
                    ),
                "label":
                    labels[
                        min(
                            i,
                            len(labels) - 1,
                        )
                    ],
            }
        )

    return windows


def extract_window_frames(
    playlist_url,
    clip_id,
    windows,
):
    """
    One FFmpeg pass per source, even when we evaluate 3 candidate windows.

    Each window gets:
    - one opening frame (~0.8s into the window)
    - one middle frame
    - one ending/payoff frame (~1.5s before the window ends)
    """
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

    for old in clip_dir.glob(
        "*.jpg"
    ):
        old.unlink()

    samples = []

    for window_index, window in enumerate(
        windows
    ):
        start = float(
            window["start"]
        )

        end = float(
            window["end"]
        )

        middle = (
            start
            +
            (end - start) * 0.50
        )

        timestamps = [
            (
                "hook",
                min(
                    end - 0.2,
                    start + 0.8,
                ),
            ),
            (
                "middle",
                middle,
            ),
            (
                "ending",
                max(
                    start + 0.2,
                    end - 1.5,
                ),
            ),
        ]

        for kind, timestamp in timestamps:
            samples.append(
                {
                    "window_index":
                        window_index,
                    "kind":
                        kind,
                    "timestamp":
                        max(
                            0.0,
                            float(
                                timestamp
                            ),
                        ),
                }
            )

    if not samples:
        return {}

    split_labels = [
        f"s{i}"
        for i in range(
            len(samples)
        )
    ]

    output_labels = [
        f"o{i}"
        for i in range(
            len(samples)
        )
    ]

    filters = [
        (
            f"[0:v]split="
            f"{len(samples)}"
            +
            "".join(
                f"[{label}]"
                for label in split_labels
            )
        )
    ]

    outputs = []

    for i, sample in enumerate(
        samples
    ):
        timestamp = float(
            sample[
                "timestamp"
            ]
        )

        filters.append(
            (
                f"[{split_labels[i]}]"
                f"trim=start={timestamp:.3f}:"
                f"end={timestamp + 0.75:.3f},"
                "setpts=PTS-STARTPTS,"
                "fps=1,"
                "scale=320:-2"
                f"[{output_labels[i]}]"
            )
        )

        path = (
            clip_dir
            /
            (
                f"w{sample['window_index']}_"
                f"{sample['kind']}.jpg"
            )
        )

        outputs.append(
            (
                output_labels[i],
                path,
            )
        )

    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        playlist_url,
        "-filter_complex",
        ";".join(
            filters
        ),
    ]

    for label, path in outputs:
        command.extend(
            [
                "-map",
                f"[{label}]",
                "-frames:v",
                "1",
                "-q:v",
                "6",
                str(path),
            ]
        )

    subprocess.run(
        command,
        check=True,
        timeout=120,
    )

    grouped = {}

    for sample, (_, path) in zip(
        samples,
        outputs,
    ):
        if (
            path.exists()
            and
            path.stat().st_size > 0
        ):
            grouped.setdefault(
                sample[
                    "window_index"
                ],
                {},
            )[
                sample["kind"]
            ] = path

    return grouped


def build_one_candidate_window_set(
    candidate_index,
    candidate,
    total,
):
    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    ).strip()

    media_url = str(
        candidate.get(
            "media_url"
        )
        or
        candidate.get(
            "playlist_url"
        )
        or ""
    ).strip()

    source_seconds = candidate.get(
        "source_duration_seconds"
    )

    if (
        not clip_id
        or
        not media_url
        or
        source_seconds is None
    ):
        return {
            "index":
                candidate_index,
            "clip_id":
                clip_id,
            "windows":
                [],
            "items":
                [],
            "error":
                "missing direct media URL/duration",
        }

    windows = make_windows(
        source_seconds
    )

    if not windows:
        return {
            "index":
                candidate_index,
            "clip_id":
                clip_id,
            "windows":
                [],
            "items":
                [],
            "error":
                "no eligible window",
        }

    try:
        grouped = (
            extract_window_frames(
                media_url,
                clip_id,
                windows,
            )
        )

    except Exception as exc:
        return {
            "index":
                candidate_index,
            "clip_id":
                clip_id,
            "windows":
                windows,
            "items":
                [],
            "error":
                str(exc),
        }

    local_items = []

    for window_index, window in enumerate(
        windows
    ):
        frame_map = grouped.get(
            window_index,
            {},
        )

        if not frame_map.get(
            "hook"
        ):
            continue

        local_items.append(
            {
                "candidate":
                    candidate,
                "window_index":
                    window_index,
                "window":
                    window,
                "frames":
                    frame_map,
            }
        )

    return {
        "index":
            candidate_index,
        "clip_id":
            clip_id,
        "windows":
            windows,
        "items":
            local_items,
        "error":
            "",
    }


def build_window_items(candidates):
    selected = candidates[
        :MAX_VISUAL_CANDIDATES
    ]

    print(
        f"V12.10 parallel preview extraction: "
        f"{len(selected)} sources with "
        f"{FRAME_WORKERS} FFmpeg workers."
    )

    results = []

    with ThreadPoolExecutor(
        max_workers=FRAME_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                build_one_candidate_window_set,
                index,
                candidate,
                len(selected),
            ): index
            for index, candidate in enumerate(
                selected,
                1,
            )
        }

        for future in as_completed(
            future_map
        ):
            result = future.result()
            results.append(
                result
            )

    results.sort(
        key=lambda row: row[
            "index"
        ]
    )

    items = []

    for result in results:
        clip_id = result[
            "clip_id"
        ]

        error = result.get(
            "error",
            "",
        )

        if error:
            print(
                f"WINDOW PREVIEW "
                f"{result['index']}/"
                f"{len(selected)}: "
                f"{clip_id} -> "
                f"{error}"
            )
            continue

        print(
            f"WINDOW PREVIEW "
            f"{result['index']}/"
            f"{len(selected)}: "
            f"{clip_id} | "
            f"candidate windows: "
            f"{len(result['windows'])}"
        )

        for item in result[
            "items"
        ]:
            item[
                "window_item_id"
            ] = len(
                items
            )

            items.append(
                item
            )

    return items

def score_batch(
    client,
    batch,
):
    prompt = f"""
You are the V12.10 DIRECT-API WINDOW prescreener for ViralSpawnTV.

The FINAL gate threshold is {TARGET_FINAL_SCORE}/100.
ViralSpawnTV is targeting final Shorts around 50-60 seconds.

Each item below is ONE SPECIFIC 49-58 second candidate window from a
longer Kick clip.

You see exactly three representative images for that window:
1. OPENING: roughly 0.8 seconds after the proposed window starts
2. MIDDLE: around the center
3. ENDING: roughly 1.5 seconds before the proposed window ends

Judge THIS WINDOW, not the entire raw source.

Kick views/likes are supplied only as a secondary real-world traction
signal. They must never override weak visible hook/story/payoff quality.

A strong window needs BOTH:
- a first-second stop-the-scroll hook
- a middle/ending that can sustain the story and deliver a payoff

HARD REJECT when the visible material is:
- casino / slots / roulette / gambling
- clearly non-gaming
- music-performance content rather than gameplay
- otherwise unsuitable for a gaming Shorts channel

OPENING:
Reward danger, challenge, clutch pressure, unusual visuals, comedy,
surprise, or an unresolved outcome that is understandable quickly.

STORY:
Reward meaningful progression, escalation, decisions, tension, or changing
circumstances. Do not reward 40 seconds of routine movement.

ENDING/PAYOFF:
Reward a clear result, clutch, fail, reveal, escape, elimination, win/loss,
reaction, punchline, or other satisfying resolution.

Return for EVERY window:
- predicted_score 0-100
- probability_72_plus 0-100
- hook 0-100
- first_second_clarity 0-100
- curiosity_gap 0-100
- story_sustain 0-100
- payoff 0-100
- ending_strength 0-100
- action 0-100
- clarity 0-100
- hard_reject true/false
- content_type gaming/gambling/non_gaming/unclear
- reason

Do not inflate scores.
Do not invent unseen events.

Return ONLY JSON:
{{
  "results": [
    {{
      "id": 0,
      "predicted_score": 78,
      "probability_72_plus": 74,
      "hook": 80,
      "first_second_clarity": 75,
      "curiosity_gap": 82,
      "story_sustain": 72,
      "payoff": 76,
      "ending_strength": 73,
      "action": 77,
      "clarity": 71,
      "hard_reject": false,
      "content_type": "gaming",
      "reason": "Specific evidence-based reason."
    }}
  ]
}}
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

        window = item[
            "window"
        ]

        content.append(
            {
                "type":
                    "input_text",
                "text":
                    (
                        f"WINDOW {local_id}\n"
                        f"clip_id: "
                        f"{candidate.get('clip_id')}\n"
                        f"game: "
                        f"{candidate.get('game')}\n"
                        f"channel: "
                        f"{candidate.get('channel')}\n"
                        f"title: "
                        f"{candidate.get('page_title', '')}\n"
                        f"description: "
                        f"{candidate.get('page_description', '')}\n"
                        f"source_duration: "
                        f"{candidate.get('source_duration_seconds')}\n"
                        f"kick_views: "
                        f"{candidate.get('kick_view_count', 0)}\n"
                        f"kick_likes: "
                        f"{candidate.get('kick_like_count', 0)}\n"
                        f"kick_like_rate_pct: "
                        f"{candidate.get('kick_like_rate_pct', 0)}\n"
                        f"window_start: "
                        f"{window.get('start')}\n"
                        f"window_end: "
                        f"{window.get('end')}\n"
                        "NEXT IMAGE = OPENING"
                    ),
            }
        )

        for kind in [
            "hook",
            "middle",
            "ending",
        ]:
            path = item[
                "frames"
            ].get(
                kind
            )

            if (
                path
                and
                Path(path).exists()
            ):
                if kind != "hook":
                    content.append(
                        {
                            "type":
                                "input_text",
                            "text":
                                (
                                    "NEXT IMAGE = "
                                    f"{kind.upper()}"
                                ),
                        }
                    )

                content.append(
                    {
                        "type":
                            "input_image",
                        "image_url":
                            data_url(
                                Path(path)
                            ),
                    }
                )

    response = (
        client
        .responses
        .create(
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
    )

    data = parse_json(
        response.output_text
    )

    return data.get(
        "results",
        [],
    )


def score_window(result):
    hook = clamp(
        result.get(
            "hook"
        )
    )

    clarity1 = clamp(
        result.get(
            "first_second_clarity"
        )
    )

    curiosity = clamp(
        result.get(
            "curiosity_gap"
        )
    )

    story = clamp(
        result.get(
            "story_sustain"
        )
    )

    payoff = clamp(
        result.get(
            "payoff"
        )
    )

    ending = clamp(
        result.get(
            "ending_strength"
        )
    )

    action = clamp(
        result.get(
            "action"
        )
    )

    clarity = clamp(
        result.get(
            "clarity"
        )
    )

    probability = clamp(
        result.get(
            "probability_72_plus"
        )
    )

    opening_score = (
        hook * 0.50
        +
        curiosity * 0.30
        +
        clarity1 * 0.20
    )

    story_score = (
        story * 0.40
        +
        payoff * 0.40
        +
        ending * 0.20
    )

    clarity_action = (
        clarity * 0.60
        +
        action * 0.40
    )

    rank_score = (
        opening_score * 0.40
        +
        story_score * 0.35
        +
        clarity_action * 0.15
        +
        probability * 0.10
    )

    return round(
        rank_score,
        2,
    )


def main():
    if not INPUT.exists():
        raise RuntimeError(
            "Missing work/v12_ranked_candidates.json"
        )

    data = json.loads(
        INPUT.read_text(
            encoding="utf-8"
        )
    )

    candidates = data.get(
        "candidates",
        [],
    )

    if not candidates:
        raise RuntimeError(
            "No duration-eligible ranked candidates."
        )

    print(
        f"V12.10 WINDOW PRESCREENER received "
        f"{len(candidates)} one-pass candidates."
    )

    items = build_window_items(
        candidates
    )

    if not items:
        raise RuntimeError(
            "No candidate windows could be previewed."
        )

    print(
        f"V12.10 generated "
        f"{len(items)} candidate windows."
    )

    client = OpenAI()

    scored_windows = []

    for start in range(
        0,
        len(items),
        BATCH_SIZE,
    ):
        batch = items[
            start:
            start + BATCH_SIZE
        ]

        print(
            f"WINDOW AI batch "
            f"{start // BATCH_SIZE + 1}/"
            f"{(len(items) + BATCH_SIZE - 1) // BATCH_SIZE}"
        )

        try:
            results = score_batch(
                client,
                batch,
            )

        except Exception as exc:
            print(
                f"  scoring batch failed: "
                f"{exc}"
            )
            continue

        by_id = {}

        for result in results:
            try:
                local_id = int(
                    result.get(
                        "id"
                    )
                )
            except Exception:
                continue

            by_id[
                local_id
            ] = result

        for local_id, item in enumerate(
            batch
        ):
            result = by_id.get(
                local_id
            )

            if not result:
                continue

            candidate = dict(
                item[
                    "candidate"
                ]
            )

            window = item[
                "window"
            ]

            metrics = {
                "prescreen_predicted_score":
                    clamp(
                        result.get(
                            "predicted_score"
                        )
                    ),
                "prescreen_probability_72_plus":
                    clamp(
                        result.get(
                            "probability_72_plus"
                        )
                    ),
                "prescreen_hook":
                    clamp(
                        result.get(
                            "hook"
                        )
                    ),
                "prescreen_first_second_clarity":
                    clamp(
                        result.get(
                            "first_second_clarity"
                        )
                    ),
                "prescreen_curiosity_gap":
                    clamp(
                        result.get(
                            "curiosity_gap"
                        )
                    ),
                "prescreen_story_sustain":
                    clamp(
                        result.get(
                            "story_sustain"
                        )
                    ),
                "prescreen_payoff":
                    clamp(
                        result.get(
                            "payoff"
                        )
                    ),
                "prescreen_ending_strength":
                    clamp(
                        result.get(
                            "ending_strength"
                        )
                    ),
                "prescreen_action":
                    clamp(
                        result.get(
                            "action"
                        )
                    ),
                "prescreen_clarity":
                    clamp(
                        result.get(
                            "clarity"
                        )
                    ),
                "prescreen_hard_reject":
                    bool(
                        result.get(
                            "hard_reject",
                            False,
                        )
                    ),
                "prescreen_content_type":
                    str(
                        result.get(
                            "content_type",
                            "unclear",
                        )
                    ).strip().lower(),
                "prescreen_reason":
                    str(
                        result.get(
                            "reason",
                            "",
                        )
                    ).strip(),
            }

            if metrics[
                "prescreen_content_type"
            ] in {
                "gambling",
                "non_gaming",
            }:
                metrics[
                    "prescreen_hard_reject"
                ] = True

            candidate.update(
                metrics
            )

            candidate[
                "proposed_window_start"
            ] = float(
                window[
                    "start"
                ]
            )

            candidate[
                "proposed_window_end"
            ] = float(
                window[
                    "end"
                ]
            )

            candidate[
                "proposed_window_label"
            ] = window[
                "label"
            ]

            candidate[
                "prescreen_rank_score"
            ] = score_window(
                result
            )

            candidate[
                "prescreen_primary_pass"
            ] = bool(
                not candidate[
                    "prescreen_hard_reject"
                ]
                and
                candidate[
                    "prescreen_predicted_score"
                ] >= MIN_PRIMARY_PREDICTED
                and
                candidate[
                    "prescreen_hook"
                ] >= MIN_PRIMARY_HOOK
                and
                candidate[
                    "prescreen_first_second_clarity"
                ] >= MIN_PRIMARY_FIRST_SECOND_CLARITY
                and
                candidate[
                    "prescreen_curiosity_gap"
                ] >= MIN_PRIMARY_CURIOSITY
                and
                candidate[
                    "prescreen_story_sustain"
                ] >= MIN_PRIMARY_STORY
                and
                candidate[
                    "prescreen_payoff"
                ] >= MIN_PRIMARY_PAYOFF
                and
                candidate[
                    "prescreen_ending_strength"
                ] >= MIN_PRIMARY_ENDING
                and
                candidate[
                    "prescreen_clarity"
                ] >= MIN_PRIMARY_CLARITY
            )

            candidate[
                "prescreen_fallback_pass"
            ] = bool(
                not candidate[
                    "prescreen_hard_reject"
                ]
                and
                candidate[
                    "prescreen_predicted_score"
                ] >= MIN_FALLBACK_PREDICTED
                and
                candidate[
                    "prescreen_hook"
                ] >= MIN_FALLBACK_HOOK
                and
                candidate[
                    "prescreen_story_sustain"
                ] >= MIN_FALLBACK_STORY
                and
                candidate[
                    "prescreen_payoff"
                ] >= MIN_FALLBACK_PAYOFF
                and
                candidate[
                    "prescreen_rank_score"
                ] >= MIN_FALLBACK_RANK
            )

            scored_windows.append(
                candidate
            )

    if not scored_windows:
        raise RuntimeError(
            "Window scoring produced no results."
        )

    # Keep ONLY the best candidate window per clip.
    best_by_clip = {}

    for row in scored_windows:
        if row.get(
            "prescreen_hard_reject",
            False,
        ):
            continue

        clip_id = str(
            row.get(
                "clip_id",
                "",
            )
        )

        current = best_by_clip.get(
            clip_id
        )

        if (
            current is None
            or
            float(
                row.get(
                    "prescreen_rank_score",
                    0,
                )
            )
            >
            float(
                current.get(
                    "prescreen_rank_score",
                    0,
                )
            )
        ):
            best_by_clip[
                clip_id
            ] = row

    best_rows = list(
        best_by_clip.values()
    )

    primary = [
        row
        for row in best_rows
        if row.get(
            "prescreen_primary_pass",
            False,
        )
    ]

    fallback = [
        row
        for row in best_rows
        if (
            not row.get(
                "prescreen_primary_pass",
                False,
            )
            and
            row.get(
                "prescreen_fallback_pass",
                False,
            )
        )
    ]

    primary.sort(
        key=lambda row: float(
            row.get(
                "prescreen_rank_score",
                0,
            )
        ),
        reverse=True,
    )

    fallback.sort(
        key=lambda row: float(
            row.get(
                "prescreen_rank_score",
                0,
            )
        ),
        reverse=True,
    )

    promoted = (
        primary
        +
        fallback
    )[:PROMOTE_COUNT]

    # Avoid a 20+ minute run ending with zero candidates when there are
    # clearly near-threshold windows. The final 72 gate still protects
    # publication quality.
    if not promoted:
        near = [
            row
            for row in best_rows
            if (
                not row.get(
                    "prescreen_hard_reject",
                    False,
                )
                and
                row.get(
                    "prescreen_rank_score",
                    0,
                ) >= 61
                and
                row.get(
                    "prescreen_hook",
                    0,
                ) >= 64
                and
                row.get(
                    "prescreen_payoff",
                    0,
                ) >= 55
            )
        ]

        near.sort(
            key=lambda row: float(
                row.get(
                    "prescreen_rank_score",
                    0,
                )
            ),
            reverse=True,
        )

        promoted = near[:2]

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "version":
            "12.10-direct-api-parallel-window-prescreen",
        "target_final_score":
            TARGET_FINAL_SCORE,
        "input_candidate_count":
            len(candidates),
        "visual_candidate_limit":
            MAX_VISUAL_CANDIDATES,
        "window_count":
            len(items),
        "scored_window_count":
            len(scored_windows),
        "best_clip_count":
            len(best_rows),
        "primary_count":
            len(primary),
        "fallback_count":
            len(fallback),
        "promoted_count":
            len(promoted),
        "candidates":
            promoted,
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
        f"V12.10 WINDOW PRESCREEN COMPLETE: "
        f"{len(candidates)} eligible sources -> "
        f"{len(items)} windows -> "
        f"{len(best_rows)} best-per-clip -> "
        f"{len(promoted)} promoted."
    )

    for i, row in enumerate(
        promoted,
        1,
    ):
        print(
            f"TOP {i}: "
            f"{row.get('clip_id')} | "
            f"window="
            f"{row.get('proposed_window_start'):.1f}-"
            f"{row.get('proposed_window_end'):.1f}s | "
            f"pred={row.get('prescreen_predicted_score')} | "
            f"hook={row.get('prescreen_hook')} | "
            f"story={row.get('prescreen_story_sustain')} | "
            f"payoff={row.get('prescreen_payoff')} | "
            f"rank={row.get('prescreen_rank_score')}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.10 WINDOW PRESCREENER ERROR:",
            exc,
        )
        sys.exit(1)
