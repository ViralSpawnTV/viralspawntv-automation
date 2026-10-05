import base64
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI


INPUT = Path("work/v12_firefight_candidates.json")
OUT = Path("work/v12_prescreened_candidates.json")
WORK = Path("work/viral_prescreen")
WORK.mkdir(parents=True, exist_ok=True)

MAX_VISUAL_CANDIDATES = 4
MAX_PAYOFF_WINDOWS = 4
PAYOFF_PHASE_SURVIVORS = 4
PROMOTE_COUNT = 4
MAX_WINDOWS_PER_CLIP = 1

PAYOFF_BATCH_SIZE = 10
PAYOFF_FRAME_WORKERS = 5
STORY_BATCH_SIZE = 6
STORY_FRAME_WORKERS = 4
AUDIO_TRANSCRIBE_WORKERS = 4

MIN_SOURCE_SECONDS = 40.0
MIN_WINDOW_SECONDS = 40.0
TARGET_WINDOW_SECONDS = 55.0
MAX_WINDOW_SECONDS = 58.0

OPENING_AUDIO_SECONDS = 7.0
ENDING_AUDIO_SECONDS = 7.0

# V12.14.6: expensive source-gate attempts should only be spent on clips
# that already show BOTH a worthwhile payoff and enough visible story.
# These remain looser than the real source gate (65 source / 60 payoff).
MIN_PROMOTE_PAYOFF = 55
MIN_PROMOTE_ENDING = 50
MIN_PROMOTE_SOURCE = 55
MIN_PROMOTE_STORY = 50
MIN_PROMOTE_EDITABILITY = 55


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
    return f"data:image/jpeg;base64,{encoded}"


def make_windows(source_seconds, preferred_start=None):
    """
    Build up to three 40-58 second windows.

    V12.14.6 judges the ENDING/PAYOFF first. The window itself is still
    continuous, but later ranking starts from "is the ending worth waiting
    for?" rather than "does the raw source already have a viral first second?"
    """
    seconds = float(source_seconds)

    if seconds < MIN_SOURCE_SECONDS:
        return []

    if seconds <= MAX_WINDOW_SECONDS:
        return [
            {
                "start": 0.0,
                "end": round(seconds, 3),
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

    if preferred_start is not None:
        start = round(max(0.0, min(float(preferred_start), max_start)), 3)
        return [{"start": start, "end": round(start + length, 3), "label": "local_action_window"}]

    if seconds <= 75:
        starts = [
            0.0,
            max_start,
        ]
    else:
        starts = [
            0.0,
            max_start / 2.0,
            max_start,
        ]

    windows = []
    seen = []

    labels = [
        "early",
        "middle",
        "late",
    ]

    for i, start in enumerate(starts):
        start = round(
            max(
                0.0,
                min(
                    start,
                    max_start,
                ),
            ),
            3,
        )

        if any(
            abs(start - old) < 5.0
            for old in seen
        ):
            continue

        seen.append(start)

        end = min(
            seconds,
            start + length,
        )

        if end - start < MIN_WINDOW_SECONDS:
            continue

        windows.append(
            {
                "start": start,
                "end": round(end, 3),
                "label": labels[
                    min(
                        i,
                        len(labels) - 1,
                    )
                ],
            }
        )

    return windows[
        :MAX_WINDOWS_PER_CLIP
    ]


def build_window_index(candidates):
    items = []

    for candidate in candidates[
        :MAX_VISUAL_CANDIDATES
    ]:
        clip_id = str(
            candidate.get(
                "clip_id",
                "",
            )
        ).strip()

        media_url = str(
            candidate.get("media_url")
            or
            candidate.get("playlist_url")
            or
            ""
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
            continue

        for window_index, window in enumerate(
            make_windows(
                source_seconds, candidate.get("local_window_start")
            )
        ):
            items.append(
                {
                    "window_item_id":
                        len(items),
                    "candidate":
                        candidate,
                    "window_index":
                        window_index,
                    "window":
                        window,
                    "media_url":
                        media_url,
                }
            )

    return items


def run_ffmpeg(command, timeout=45):
    subprocess.run(
        command,
        check=True,
        timeout=timeout,
    )


def extract_payoff_evidence(item):
    """
    Phase 1 only inspects the last ~7 seconds of a proposed window.
    This is deliberately payoff-first.
    """
    candidate = item[
        "candidate"
    ]

    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    )

    window = item[
        "window"
    ]

    start = float(
        window[
            "start"
        ]
    )

    end = float(
        window[
            "end"
        ]
    )

    ending_seek = max(
        start,
        end - 7.0,
    )

    clip_dir = (
        WORK
        /
        safe_name(
            clip_id
        )
        /
        f"window_{item['window_index']}"
    )

    clip_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    for pattern in [
        "payoff_*.jpg",
        "payoff_audio.wav",
    ]:
        for old in clip_dir.glob(
            pattern
        ):
            old.unlink()

    frame_pattern = str(
        clip_dir
        /
        "payoff_%02d.jpg"
    )

    payoff_audio = (
        clip_dir
        /
        "payoff_audio.wav"
    )

    frame_cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{ending_seek:.3f}",
        "-i",
        item[
            "media_url"
        ],
        "-t",
        "7.0",
        "-an",
        "-vf",
        "fps=0.5,scale=360:-2",
        "-frames:v",
        "3",
        "-q:v",
        "5",
        frame_pattern,
    ]

    audio_cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{ending_seek:.3f}",
        "-i",
        item[
            "media_url"
        ],
        "-t",
        f"{ENDING_AUDIO_SECONDS:.2f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(
            payoff_audio
        ),
    ]

    run_ffmpeg(
        frame_cmd
    )

    try:
        run_ffmpeg(
            audio_cmd
        )
    except Exception:
        try:
            payoff_audio.unlink()
        except Exception:
            pass

    frames = [
        str(path)
        for path in sorted(
            clip_dir.glob(
                "payoff_*.jpg"
            )
        )[:3]
        if (
            path.exists()
            and
            path.stat().st_size > 0
        )
    ]

    return {
        "payoff_frames":
            frames,
        "payoff_audio":
            (
                str(payoff_audio)
                if (
                    payoff_audio.exists()
                    and
                    payoff_audio.stat().st_size > 1000
                )
                else ""
            ),
    }


def extract_story_evidence(item):
    """
    Phase 2 examines setup + middle + opening audio only after a window has
    demonstrated a potentially worthwhile ending.
    """
    candidate = item[
        "candidate"
    ]

    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    )

    window = item[
        "window"
    ]

    start = float(
        window[
            "start"
        ]
    )

    end = float(
        window[
            "end"
        ]
    )

    middle = (
        start
        +
        (end - start) * 0.50
    )

    clip_dir = (
        WORK
        /
        safe_name(
            clip_id
        )
        /
        f"window_{item['window_index']}"
    )

    clip_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    for pattern in [
        "setup_*.jpg",
        "middle_*.jpg",
        "opening_audio.wav",
    ]:
        for old in clip_dir.glob(
            pattern
        ):
            old.unlink()

    setup_pattern = str(
        clip_dir
        /
        "setup_%02d.jpg"
    )

    middle_pattern = str(
        clip_dir
        /
        "middle_%02d.jpg"
    )

    opening_audio = (
        clip_dir
        /
        "opening_audio.wav"
    )

    setup_cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        item[
            "media_url"
        ],
        "-t",
        "4.0",
        "-an",
        "-vf",
        "fps=0.5,scale=360:-2",
        "-frames:v",
        "2",
        "-q:v",
        "5",
        setup_pattern,
    ]

    middle_seek = max(
        start,
        middle - 2.0,
    )

    middle_cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{middle_seek:.3f}",
        "-i",
        item[
            "media_url"
        ],
        "-t",
        "4.0",
        "-an",
        "-vf",
        "fps=0.5,scale=360:-2",
        "-frames:v",
        "2",
        "-q:v",
        "5",
        middle_pattern,
    ]

    audio_cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-i",
        item[
            "media_url"
        ],
        "-t",
        f"{OPENING_AUDIO_SECONDS:.2f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(
            opening_audio
        ),
    ]

    run_ffmpeg(
        setup_cmd
    )

    run_ffmpeg(
        middle_cmd
    )

    try:
        run_ffmpeg(
            audio_cmd
        )
    except Exception:
        try:
            opening_audio.unlink()
        except Exception:
            pass

    setup_frames = [
        str(path)
        for path in sorted(
            clip_dir.glob(
                "setup_*.jpg"
            )
        )[:2]
        if (
            path.exists()
            and
            path.stat().st_size > 0
        )
    ]

    middle_frames = [
        str(path)
        for path in sorted(
            clip_dir.glob(
                "middle_*.jpg"
            )
        )[:2]
        if (
            path.exists()
            and
            path.stat().st_size > 0
        )
    ]

    return {
        "setup_frames":
            setup_frames,
        "middle_frames":
            middle_frames,
        "opening_audio":
            (
                str(opening_audio)
                if (
                    opening_audio.exists()
                    and
                    opening_audio.stat().st_size > 1000
                )
                else ""
            ),
    }


def extract_parallel(
    items,
    fn,
    workers,
    label,
):
    results = {}

    with ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        future_map = {
            executor.submit(
                fn,
                item,
            ): item
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
                        "window_item_id"
                    ]
                ] = future.result()

            except Exception as exc:
                print(
                    f"{label} extraction failed "
                    f"{item['candidate'].get('clip_id')} "
                    f"window="
                    f"{item['window'].get('start'):.1f}-"
                    f"{item['window'].get('end'):.1f}: "
                    f"{exc}"
                )

    return results


def transcribe_file(path):
    raw_path = str(
        path or ""
    ).strip()

    if not raw_path:
        return ""

    path = Path(
        raw_path
    )

    if (
        not path.is_file()
        or
        path.stat().st_size <= 1000
    ):
        return ""

    try:
        client = OpenAI()

        with path.open(
            "rb"
        ) as file:
            response = (
                client
                .audio
                .transcriptions
                .create(
                    model="whisper-1",
                    file=file,
                    response_format="text",
                )
            )

        return str(
            response
        ).strip()

    except Exception as exc:
        print(
            f"Transcript failed "
            f"{path.name}: {exc}"
        )
        return ""


def transcribe_parallel(
    items,
    path_key,
):
    if os.getenv("VIRALSPAWN_VISUAL_PRESCREEN_ONLY", "1") == "1":
        return {}

    results = {}

    with ThreadPoolExecutor(
        max_workers=AUDIO_TRANSCRIBE_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                transcribe_file,
                item.get(
                    path_key,
                    "",
                ),
            ): item
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
                        "window_item_id"
                    ]
                ] = future.result()

            except Exception:
                results[
                    item[
                        "window_item_id"
                    ]
                ] = ""

    return results


def score_payoff_batch(
    client,
    batch,
):
    prompt = """
You are Phase 1 of ViralSpawnTV V12.14.6.

This is PAYOFF-FIRST source selection.

Each item is a proposed 40-58 second gaming window. You are shown only
evidence from its FINAL ~7 seconds plus any transcript from those seconds.

Judge whether the ending contains something worth building a Short around.

Reward:
- a decisive win/loss
- kill/elimination/clutch result
- goal/save/comeback result
- fail/crash/death
- escape/survival
- reveal/surprise
- strong streamer reaction
- punchline/comedic result
- meaningful challenge result

Penalize:
- menus
- inventory/loadout screens
- routine movement
- unresolved action
- ordinary repositioning
- endings where nothing materially changes

Hard-reject ONLY:
- gambling/casino
- clearly non-gaming material
- music-performance content

A weak gaming ending is NOT a hard reject. Give it low scores.

Return ONLY JSON:
{
  "results": [
    {
      "id": 0,
      "payoff": 78,
      "ending_strength": 75,
      "payoff_clarity": 72,
      "reaction": 70,
      "hard_reject": false,
      "content_type": "gaming",
      "reason": "A decisive result occurs and the reaction makes the ending feel complete."
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
                        f"WINDOW {local_id}\n"
                        f"clip_id="
                        f"{candidate.get('clip_id')}\n"
                        f"duration="
                        f"{item['window']['end'] - item['window']['start']:.1f}s\n"
                        "ENDING TRANSCRIPT:\n"
                        f"{item.get('payoff_transcript') or '(no usable transcript)'}\n"
                        "NEXT: ENDING FRAMES"
                    ),
            }
        )

        for frame in item.get(
            "payoff_frames",
            [],
        ):
            if Path(frame).exists():
                content.append(
                    {
                        "type":
                            "input_image",
                        "image_url":
                            data_url(
                                frame
                            ),
                    }
                )

    response = client.responses.create(
        max_output_tokens=3000,
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


def payoff_rank(row):
    return round(
        row[
            "prescreen_payoff"
        ] * 0.45
        +
        row[
            "prescreen_ending_strength"
        ] * 0.25
        +
        row[
            "prescreen_payoff_clarity"
        ] * 0.15
        +
        row[
            "prescreen_reaction"
        ] * 0.15,
        2,
    )


def score_story_batch(
    client,
    batch,
):
    prompt = """
You are Phase 2 of ViralSpawnTV V12.14.6.

These gaming windows already have the strongest available endings/payoffs.
Now decide whether the footage BEFORE that payoff contains enough material
for ViralSpawnTV's editor to MAKE a good 40-60 second Short.

IMPORTANT:
Do NOT require the raw source to already have a viral first-second hook.
The production editor will create a Big Hook using truthful on-screen text,
narration, captions and pacing.

Judge the SOURCE MATERIAL for:
- story_sustain: meaningful progression toward the ending
- action: meaningful gameplay/reaction rather than filler
- clarity: enough understandable context to tell the story
- setup_quality: whether the beginning gives usable facts/stakes
- editability: whether an editor can create a compelling opening without
  inventing anything
- source_score: overall quality of the raw material as editable source

Penalize long stretches of:
- routine travel
- menus/inventory/loadouts
- dead air
- unrelated chatter
- repetitive low-stakes movement

Do NOT punish a merely slow raw first second if the window has a strong
story and payoff that can be truthfully reframed in production.

Hard-reject ONLY gambling/casino, clearly non-gaming, or music-performance
content.

Return ONLY JSON:
{
  "results": [
    {
      "id": 0,
      "source_score": 72,
      "story_sustain": 72,
      "action": 70,
      "clarity": 74,
      "setup_quality": 68,
      "editability": 80,
      "hard_reject": false,
      "content_type": "gaming",
      "reason": "The source has clear progression toward a worthwhile ending and enough truthful context to build a stronger opening."
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
                        f"WINDOW {local_id}\n"
                        f"clip_id="
                        f"{candidate.get('clip_id')}\n"
                        f"payoff={item.get('prescreen_payoff')}\n"
                        f"ending_strength={item.get('prescreen_ending_strength')}\n"
                        f"payoff_clarity={item.get('prescreen_payoff_clarity')}\n"
                        f"reaction={item.get('prescreen_reaction')}\n"
                        "OPENING TRANSCRIPT:\n"
                        f"{item.get('opening_transcript') or '(no usable transcript)'}\n"
                        "NEXT: SETUP FRAMES"
                    ),
            }
        )

        for frame in item.get(
            "setup_frames",
            [],
        ):
            if Path(frame).exists():
                content.append(
                    {
                        "type":
                            "input_image",
                        "image_url":
                            data_url(
                                frame
                            ),
                    }
                )

        content.append(
            {
                "type":
                    "input_text",
                "text":
                    "NEXT: MIDDLE FRAMES",
            }
        )

        for frame in item.get(
            "middle_frames",
            [],
        ):
            if Path(frame).exists():
                content.append(
                    {
                        "type":
                            "input_image",
                        "image_url":
                            data_url(
                                frame
                            ),
                    }
                )

    response = client.responses.create(
        max_output_tokens=3000,
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


def final_rank(row):
    return round(
        row[
            "prescreen_payoff_rank"
        ] * 0.45
        +
        row[
            "prescreen_source_score"
        ] * 0.20
        +
        row[
            "prescreen_story_sustain"
        ] * 0.15
        +
        row[
            "prescreen_editability"
        ] * 0.10
        +
        (
            row[
                "prescreen_action"
            ]
            +
            row[
                "prescreen_clarity"
            ]
        )
        /
        2.0
        *
        0.10,
        2,
    )


def main():
    if not INPUT.exists():
        raise RuntimeError(
            "Missing work/v12_ranked_candidates.json"
        )

    started = time.perf_counter()

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
            "No ranked candidates."
        )

    windows = build_window_index(
        candidates
    )

    if not windows:
        raise RuntimeError(
            "No eligible 40-58 second windows."
        )

    raw_window_count = len(
        windows
    )

    # V12.14.6 COST CONTROL:
    # Candidate_ranker already orders sources by traction/quality evidence.
    # Do not spend ~14 minutes analyzing every possible window. Keep the
    # strongest front of the ranked pool and cap Phase-1 at 36 windows.
    windows = windows[
        :MAX_PAYOFF_WINDOWS
    ]

    print(
        f"V12.14.6 PAYOFF-FIRST: "
        f"{len(candidates)} ranked sources -> "
        f"{raw_window_count} possible windows -> "
        f"{len(windows)} payoff windows inspected."
    )

    # ---------------------------------------------------------
    # PHASE 1: PAYOFF FIRST
    # ---------------------------------------------------------

    payoff_started = time.perf_counter()

    extracted = extract_parallel(
        windows,
        extract_payoff_evidence,
        PAYOFF_FRAME_WORKERS,
        "payoff",
    )

    payoff_items = []

    for item in windows:
        evidence = extracted.get(
            item[
                "window_item_id"
            ]
        )

        if not isinstance(
            evidence,
            dict,
        ):
            continue

        if not evidence.get(
            "payoff_frames"
        ):
            continue

        row = dict(
            item
        )

        row.update(
            evidence
        )

        payoff_items.append(
            row
        )

    payoff_transcripts = transcribe_parallel(
        payoff_items,
        "payoff_audio",
    )

    for item in payoff_items:
        item[
            "payoff_transcript"
        ] = payoff_transcripts.get(
            item[
                "window_item_id"
            ],
            "",
        )

    client = OpenAI()
    payoff_scored = []

    for start in range(
        0,
        len(payoff_items),
        PAYOFF_BATCH_SIZE,
    ):
        batch = payoff_items[
            start:
            start + PAYOFF_BATCH_SIZE
        ]

        results = score_payoff_batch(
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
                    "unclear",
                )
            ).strip().lower()

            hard_reject = (
                content_type
                in {
                    "gambling",
                    "non_gaming",
                    "music_performance",
                }
            )

            if hard_reject:
                continue

            row = dict(
                item
            )

            row[
                "prescreen_payoff"
            ] = clamp(
                result.get(
                    "payoff"
                )
            )

            row[
                "prescreen_ending_strength"
            ] = clamp(
                result.get(
                    "ending_strength"
                )
            )

            row[
                "prescreen_payoff_clarity"
            ] = clamp(
                result.get(
                    "payoff_clarity"
                )
            )

            row[
                "prescreen_reaction"
            ] = clamp(
                result.get(
                    "reaction"
                )
            )

            row[
                "prescreen_payoff_reason"
            ] = str(
                result.get(
                    "reason",
                    "",
                )
            ).strip()

            row[
                "prescreen_payoff_rank"
            ] = payoff_rank(
                row
            )

            payoff_scored.append(
                row
            )

    payoff_scored.sort(
        key=lambda row: float(
            row.get(
                "prescreen_payoff_rank",
                0,
            )
        ),
        reverse=True,
    )

    # Max 2 windows from the same source entering Phase 2.
    per_clip = {}
    phase2_seed = []

    for row in payoff_scored:
        clip_id = str(
            row[
                "candidate"
            ].get(
                "clip_id",
                "",
            )
        )

        count = per_clip.get(
            clip_id,
            0,
        )

        if count >= 2:
            continue

        phase2_seed.append(
            row
        )

        per_clip[
            clip_id
        ] = count + 1

        if (
            len(
                phase2_seed
            )
            >=
            PAYOFF_PHASE_SURVIVORS
        ):
            break

    print(
        f"V12.14.6 PAYOFF PHASE: "
        f"{len(payoff_items)} endings inspected -> "
        f"{len(payoff_scored)} usable -> "
        f"{len(phase2_seed)} strongest payoffs advance | "
        f"{time.perf_counter() - payoff_started:.1f}s"
    )

    for i, row in enumerate(
        phase2_seed,
        1,
    ):
        print(
            f"PAYOFF TOP {i}: "
            f"{row['candidate'].get('clip_id')} | "
            f"window="
            f"{row['window']['start']:.1f}-"
            f"{row['window']['end']:.1f}s | "
            f"payoff={row.get('prescreen_payoff')} | "
            f"ending={row.get('prescreen_ending_strength')} | "
            f"rank={row.get('prescreen_payoff_rank')}"
        )

    if not phase2_seed:
        raise RuntimeError(
            "No gaming window survived payoff phase."
        )

    # ---------------------------------------------------------
    # PHASE 2: SOURCE STORY / EDITABILITY
    # ---------------------------------------------------------

    story_started = time.perf_counter()

    story_evidence = extract_parallel(
        phase2_seed,
        extract_story_evidence,
        STORY_FRAME_WORKERS,
        "story",
    )

    story_items = []

    for item in phase2_seed:
        evidence = story_evidence.get(
            item[
                "window_item_id"
            ]
        )

        if not isinstance(
            evidence,
            dict,
        ):
            continue

        row = dict(
            item
        )

        row.update(
            evidence
        )

        story_items.append(
            row
        )

    opening_transcripts = transcribe_parallel(
        story_items,
        "opening_audio",
    )

    for item in story_items:
        item[
            "opening_transcript"
        ] = opening_transcripts.get(
            item[
                "window_item_id"
            ],
            "",
        )

    final_rows = []

    for start in range(
        0,
        len(story_items),
        STORY_BATCH_SIZE,
    ):
        batch = story_items[
            start:
            start + STORY_BATCH_SIZE
        ]

        results = score_story_batch(
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
                    "unclear",
                )
            ).strip().lower()

            if (
                content_type
                in {
                    "gambling",
                    "non_gaming",
                    "music_performance",
                }
            ):
                continue

            candidate = dict(
                item[
                    "candidate"
                ]
            )

            candidate[
                "proposed_window_start"
            ] = float(
                item[
                    "window"
                ][
                    "start"
                ]
            )

            candidate[
                "proposed_window_end"
            ] = float(
                item[
                    "window"
                ][
                    "end"
                ]
            )

            candidate[
                "proposed_window_label"
            ] = item[
                "window"
            ][
                "label"
            ]

            candidate[
                "prescreen_payoff"
            ] = item[
                "prescreen_payoff"
            ]

            candidate[
                "prescreen_ending_strength"
            ] = item[
                "prescreen_ending_strength"
            ]

            candidate[
                "prescreen_payoff_clarity"
            ] = item[
                "prescreen_payoff_clarity"
            ]

            candidate[
                "prescreen_reaction"
            ] = item[
                "prescreen_reaction"
            ]

            candidate[
                "prescreen_payoff_rank"
            ] = item[
                "prescreen_payoff_rank"
            ]

            candidate[
                "prescreen_source_score"
            ] = clamp(
                result.get(
                    "source_score"
                )
            )

            candidate[
                "prescreen_story_sustain"
            ] = clamp(
                result.get(
                    "story_sustain"
                )
            )

            candidate[
                "prescreen_action"
            ] = clamp(
                result.get(
                    "action"
                )
            )

            candidate[
                "prescreen_clarity"
            ] = clamp(
                result.get(
                    "clarity"
                )
            )

            candidate[
                "prescreen_setup_quality"
            ] = clamp(
                result.get(
                    "setup_quality"
                )
            )

            candidate[
                "prescreen_editability"
            ] = clamp(
                result.get(
                    "editability"
                )
            )

            # Compatibility with older acquisition/log fields.
            candidate[
                "prescreen_predicted_score"
            ] = candidate[
                "prescreen_source_score"
            ]

            candidate[
                "prescreen_probability_72_plus"
            ] = 0

            candidate[
                "prescreen_hook"
            ] = 0

            candidate[
                "prescreen_reason"
            ] = str(
                result.get(
                    "reason",
                    "",
                )
            ).strip()

            candidate[
                "prescreen_rank_score"
            ] = final_rank(
                candidate
            )

            final_rows.append(
                candidate
            )

            print(
                f"SOURCE RESULT: "
                f"{candidate.get('clip_id')} | "
                f"source={candidate.get('prescreen_source_score')} | "
                f"story={candidate.get('prescreen_story_sustain')} | "
                f"payoff={candidate.get('prescreen_payoff')} | "
                f"ending={candidate.get('prescreen_ending_strength')} | "
                f"editability={candidate.get('prescreen_editability')} | "
                f"rank={candidate.get('prescreen_rank_score')}"
            )

    print(
        f"V12.14.6 STORY PHASE: "
        f"{len(story_items)} windows -> "
        f"{len(final_rows)} scored | "
        f"{time.perf_counter() - story_started:.1f}s"
    )

    if not final_rows:
        raise RuntimeError(
            "No usable source stories survived Phase 2."
        )

    # Keep only best window per source clip.
    best_by_clip = {}

    for row in final_rows:
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

    best_rows.sort(
        key=lambda row: (
            float(
                row.get(
                    "prescreen_rank_score",
                    0,
                )
            ),
            float(
                row.get(
                    "prescreen_payoff",
                    0,
                )
            ),
        ),
        reverse=True,
    )

    # V12.14.6: do not spend one of the four expensive source-gate
    # attempts on a clip that has only an isolated reaction/payoff or only
    # general editability. It must show BOTH a worthwhile ending AND enough
    # visible story substance.
    promotion_ready = [
        row
        for row in best_rows
        if (
            row.get(
                "prescreen_payoff",
                0,
            )
            >=
            MIN_PROMOTE_PAYOFF
            and
            row.get(
                "prescreen_ending_strength",
                0,
            )
            >=
            MIN_PROMOTE_ENDING
            and
            row.get(
                "prescreen_source_score",
                0,
            )
            >=
            MIN_PROMOTE_SOURCE
            and
            row.get(
                "prescreen_story_sustain",
                0,
            )
            >=
            MIN_PROMOTE_STORY
            and
            row.get(
                "prescreen_editability",
                0,
            )
            >=
            MIN_PROMOTE_EDITABILITY
        )
    ]

    promoted = promotion_ready[
        :PROMOTE_COUNT
    ]

    # V12.14.6 RELIABILITY BACKFILL:
    # Strict candidates remain first. If fewer than four clear the cheap
    # promotion thresholds, fill the remaining slots with the highest-ranked
    # distinct source clips instead of letting the entire workflow depend on
    # one candidate.
    strict_ids = {
        str(
            row.get(
                "clip_id",
                "",
            )
        )
        for row in promoted
    }

    backfilled = []

    if len(
        promoted
    ) < PROMOTE_COUNT:
        for row in best_rows:
            clip_id = str(
                row.get(
                    "clip_id",
                    "",
                )
            )

            if (
                not clip_id
                or
                clip_id in strict_ids
            ):
                continue

            fallback = dict(
                row
            )

            fallback[
                "prescreen_reliability_backfill"
            ] = True

            backfilled.append(
                fallback
            )

            strict_ids.add(
                clip_id
            )

            if (
                len(
                    promoted
                )
                +
                len(
                    backfilled
                )
                >=
                PROMOTE_COUNT
            ):
                break

    promoted = (
        promoted
        +
        backfilled
    )[
        :PROMOTE_COUNT
    ]

    print(
        f"V12.14.6 PROMOTION FILTER: "
        f"{len(best_rows)} best-per-clip -> "
        f"{len(promotion_ready)} strict qualified -> "
        f"{len(backfilled)} reliability backfill -> "
        f"{len(promoted)} source-gate candidates."
    )

    payload = {
        "version":
            "12.14.5-four-source-reliability",
        "strategy":
            "capped_payoff_scan_strict_plus_four_source_backfill",
        "input_candidate_count":
            len(candidates),
        "candidate_window_count":
            len(windows),
        "raw_candidate_window_count":
            raw_window_count,
        "max_payoff_windows":
            MAX_PAYOFF_WINDOWS,
        "payoff_scored_count":
            len(payoff_scored),
        "story_phase_count":
            len(story_items),
        "best_clip_count":
            len(best_rows),
        "promotion_ready_count":
            len(promotion_ready),
        "backfill_count":
            len(backfilled),
        "promotion_thresholds":
            {
                "payoff":
                    MIN_PROMOTE_PAYOFF,
                "ending":
                    MIN_PROMOTE_ENDING,
                "source":
                    MIN_PROMOTE_SOURCE,
                "story":
                    MIN_PROMOTE_STORY,
                "editability":
                    MIN_PROMOTE_EDITABILITY,
            },
        "promoted_count":
            len(promoted),
        "prescreen_seconds":
            round(
                time.perf_counter()
                -
                started,
                2,
            ),
        "candidates":
            promoted,
    }

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

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
        f"V12.14.6 PRESCREEN COMPLETE: "
        f"{len(windows)} windows -> "
        f"{len(best_rows)} source clips -> "
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
            f"source={row.get('prescreen_source_score')} | "
            f"story={row.get('prescreen_story_sustain')} | "
            f"payoff={row.get('prescreen_payoff')} | "
            f"ending={row.get('prescreen_ending_strength')} | "
            f"rank={row.get('prescreen_rank_score')}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.14.6 PRESCREENER ERROR:",
            exc,
        )
        sys.exit(1)
