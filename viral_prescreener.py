import base64
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI


INPUT = Path("work/v12_ranked_candidates.json")
OUT = Path("work/v12_prescreened_candidates.json")
WORK = Path("work/viral_prescreen")
WORK.mkdir(parents=True, exist_ok=True)

MAX_VISUAL_CANDIDATES = 24
PROMOTE_COUNT = 4

# Phase 1: every window gets only its first ~2 seconds inspected.
HOOK_BATCH_SIZE = 12
HOOK_FRAME_WORKERS = 6
HOOK_PHASE_SURVIVORS = 10
MAX_HOOK_WINDOWS_PER_CLIP = 2

# Phase 2: only the strongest opening windows get middle/end evidence.
STORY_BATCH_SIZE = 6
STORY_FRAME_WORKERS = 4

# V12.12 audio-context pass. Only Phase-2 survivors pay this cost.
AUDIO_TRANSCRIBE_WORKERS = 4
OPENING_AUDIO_SECONDS = 7.0
ENDING_AUDIO_SECONDS = 7.0

TARGET_FINAL_SCORE = 72

# V12.12 duration strategy:
# Shorts may now be as short as 40 seconds.
MIN_SOURCE_SECONDS = 40.0
TARGET_WINDOW_SECONDS = 55.0
MIN_WINDOW_SECONDS = 40.0
MAX_WINDOW_SECONDS = 58.0

# V12.12 Phase-1 ranking funnel.
# Rank every non-hard-rejected opening and send the top 10 to Phase 2.

# V12.12:
# Phase 2 does not apply another publishability threshold.
# It ranks the completed candidate windows and sends the top 4 to the
# unchanged final 72 viral-quality gate.


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
    raw = str(
        raw or ""
    ).strip()

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

    return json.loads(
        raw
    )


def make_windows(source_seconds):
    seconds = float(
        source_seconds
    )

    if seconds < MIN_SOURCE_SECONDS:
        return []

    if seconds <= MAX_WINDOW_SECONDS:
        return [
            {
                "start":
                    0.0,
                "end":
                    round(
                        seconds,
                        3,
                    ),
                "label":
                    "full",
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
            continue

        for window_index, window in enumerate(
            make_windows(
                source_seconds
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


def extract_hook_frames(item):
    """
    INPUT-SIDE SEEK:
    seek directly to this specific window start and decode only ~2.1 seconds.
    This is the key V12.12 speed change.
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

    for old in clip_dir.glob(
        "hook_*.jpg"
    ):
        old.unlink()

    pattern = str(
        clip_dir
        /
        "hook_%02d.jpg"
    )

    command = [
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
        "2.10",
        "-an",
        "-vf",
        "fps=1.5,scale=360:-2",
        "-frames:v",
        "3",
        "-q:v",
        "5",
        pattern,
    ]

    subprocess.run(
        command,
        check=True,
        timeout=45,
    )

    frames = sorted(
        clip_dir.glob(
            "hook_*.jpg"
        )
    )[:3]

    return [
        str(path)
        for path in frames
        if (
            path.exists()
            and
            path.stat().st_size > 0
        )
    ]


def extract_story_frames(item):
    """
    V12.12 Phase 2:
    - input-side seek to the middle for visual story evidence
    - input-side seek to the ending for payoff evidence
    - extract a short opening audio sample
    - extract a short ending audio sample

    Audio extraction is best-effort. A clip with no usable audio still stays
    in the visual ranking pool.
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

    middle_seek = max(
        start,
        middle - 1.5,
    )

    ending_seek = max(
        start,
        end - 6.0,
    )

    opening_audio_seek = start

    ending_audio_seek = max(
        start,
        end - ENDING_AUDIO_SECONDS,
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
        "middle_*.jpg",
        "ending_*.jpg",
        "opening_audio.wav",
        "ending_audio.wav",
    ]:
        for old in clip_dir.glob(
            pattern
        ):
            old.unlink()

    middle_pattern = str(
        clip_dir
        /
        "middle_%02d.jpg"
    )

    ending_pattern = str(
        clip_dir
        /
        "ending_%02d.jpg"
    )

    opening_audio = (
        clip_dir
        /
        "opening_audio.wav"
    )

    ending_audio = (
        clip_dir
        /
        "ending_audio.wav"
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
        "3.20",
        "-an",
        "-vf",
        "fps=0.7,scale=360:-2",
        "-frames:v",
        "2",
        "-q:v",
        "5",
        middle_pattern,
    ]

    ending_cmd = [
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
        "6.00",
        "-an",
        "-vf",
        "fps=0.5,scale=360:-2",
        "-frames:v",
        "3",
        "-q:v",
        "5",
        ending_pattern,
    ]

    opening_audio_cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{opening_audio_seek:.3f}",
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

    ending_audio_cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{ending_audio_seek:.3f}",
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
            ending_audio
        ),
    ]

    subprocess.run(
        middle_cmd,
        check=True,
        timeout=45,
    )

    subprocess.run(
        ending_cmd,
        check=True,
        timeout=45,
    )

    # Audio is best-effort. Missing/unsupported audio must not destroy an
    # otherwise usable gaming candidate.
    try:
        subprocess.run(
            opening_audio_cmd,
            check=True,
            timeout=45,
        )
    except Exception:
        try:
            opening_audio.unlink()
        except Exception:
            pass

    try:
        subprocess.run(
            ending_audio_cmd,
            check=True,
            timeout=45,
        )
    except Exception:
        try:
            ending_audio.unlink()
        except Exception:
            pass

    middle_frames = sorted(
        clip_dir.glob(
            "middle_*.jpg"
        )
    )[:2]

    ending_frames = sorted(
        clip_dir.glob(
            "ending_*.jpg"
        )
    )[:3]

    return {
        "middle_frames":
            [
                str(path)
                for path in middle_frames
                if (
                    path.exists()
                    and
                    path.stat().st_size > 0
                )
            ],
        "ending_frames":
            [
                str(path)
                for path in ending_frames
                if (
                    path.exists()
                    and
                    path.stat().st_size > 0
                )
            ],
        "opening_audio":
            (
                str(
                    opening_audio
                )
                if (
                    opening_audio.exists()
                    and
                    opening_audio.stat().st_size > 1000
                )
                else ""
            ),
        "ending_audio":
            (
                str(
                    ending_audio
                )
                if (
                    ending_audio.exists()
                    and
                    ending_audio.stat().st_size > 1000
                )
                else ""
            ),
    }


def transcribe_audio_file(path):
    path = Path(
        str(
            path or ""
        )
    )

    if (
        not path.exists()
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
            f"AUDIO TRANSCRIBE FAILED "
            f"{path.name}: {exc}"
        )

        return ""


def transcribe_story_audio(item):
    return {
        "opening_transcript":
            transcribe_audio_file(
                item.get(
                    "opening_audio",
                    "",
                )
            ),
        "ending_transcript":
            transcribe_audio_file(
                item.get(
                    "ending_audio",
                    "",
                )
            ),
    }


def transcribe_audio_parallel(
    items,
):
    results = {}

    with ThreadPoolExecutor(
        max_workers=AUDIO_TRANSCRIBE_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                transcribe_story_audio,
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

            item_id = item[
                "window_item_id"
            ]

            try:
                results[
                    item_id
                ] = future.result()

            except Exception as exc:
                print(
                    f"AUDIO CONTEXT FAILED "
                    f"{item['candidate'].get('clip_id')}: "
                    f"{exc}"
                )

                results[
                    item_id
                ] = {
                    "opening_transcript":
                        "",
                    "ending_transcript":
                        "",
                }

    return results

def extract_phase_parallel(
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

            item_id = item[
                "window_item_id"
            ]

            try:
                results[
                    item_id
                ] = future.result()

            except Exception as exc:
                print(
                    f"{label} extraction failed "
                    f"{item['candidate'].get('clip_id')} "
                    f"window="
                    f"{item['window'].get('start')}-"
                    f"{item['window'].get('end')}: "
                    f"{exc}"
                )

    return results


def score_hook_batch(
    client,
    batch,
):
    prompt = """
You are PHASE 1 of ViralSpawnTV's V12.12 prescreener.

Your ONLY job is to judge whether the FIRST ~2 SECONDS of each proposed
40-60 second gaming Short are strong enough to stop a viewer from swiping.

IMPORTANT:
You are intentionally NOT given the title, description, creator popularity,
view count, game name, or later frames.

Judge ONLY what is actually visible in these opening frames.

Reward:
- immediate danger
- obvious clutch pressure
- a challenge already happening
- surprising/funny visual problem
- unusual action
- visually understandable conflict
- a clear unresolved outcome
- strong visible reaction

Penalize heavily:
- healing/repositioning with no visible danger
- menus/lobbies
- routine running/driving/flying
- looting
- static facecam
- clutter where a new viewer cannot tell what matters
- ordinary gameplay
- openings that require explanation before becoming interesting

Return:
- hook 0-100
- first_second_clarity 0-100
- curiosity_gap 0-100
- opening_action 0-100
- hard_reject true/false
- content_type gaming/gambling/non_gaming/music_performance/unclear
- reason

HARD-REJECT RULE:
hard_reject=true ONLY when the visible content is clearly:
- gambling/casino
- non-gaming content
- music-performance content

A weak, boring, confusing, slow, or low-action GAMING opening is NOT a
hard reject. Score its hook/clarity/curiosity/action low, but set
hard_reject=false and content_type="gaming" or "unclear".

This distinction is critical. Phase 1 ranks weak gaming openings; it does
not eliminate them except for clearly unsuitable content types.

Use 80+ only for genuinely strong stop-the-scroll openings.
Do not inflate scores.

Return ONLY JSON:
{
  "results": [
    {
      "id": 0,
      "hook": 82,
      "first_second_clarity": 78,
      "curiosity_gap": 84,
      "opening_action": 80,
      "hard_reject": false,
      "content_type": "gaming",
      "reason": "Visible immediate threat with a clear unresolved outcome."
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
        content.append(
            {
                "type":
                    "input_text",
                "text":
                    (
                        f"OPENING {local_id}\n"
                        "The next images are consecutive samples "
                        "from only the first ~2 seconds."
                    ),
            }
        )

        for frame_path in item.get(
            "hook_frames",
            [],
        ):
            path = Path(
                frame_path
            )

            if path.exists():
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


def hook_rank(row):
    return round(
        row[
            "prescreen_hook"
        ] * 0.45
        +
        row[
            "prescreen_curiosity_gap"
        ] * 0.30
        +
        row[
            "prescreen_first_second_clarity"
        ] * 0.15
        +
        row[
            "prescreen_opening_action"
        ] * 0.10,
        2,
    )


def score_story_batch(
    client,
    batch,
):
    prompt = f"""
You are PHASE 2 of ViralSpawnTV's V12.12 prescreener.

These windows already survived a dedicated first-2-second Big Hook test.
Now judge whether the REST of the SAME 40-58 second window earns the
viewer staying until the end.

The final viral gate threshold is {TARGET_FINAL_SCORE}/100.

For each candidate you will see:
- its already-measured VISUAL opening-hook scores
- OPENING AUDIO TRANSCRIPT from roughly the first 7 seconds
- 2 MIDDLE frames
- up to 3 ENDING/PAYOFF frames
- ENDING AUDIO TRANSCRIPT from roughly the final 7 seconds

Use the transcripts to catch cases that screenshots cannot:
- speech/commentary unrelated to the visible gameplay
- confusing opening chatter
- whether the spoken context explains the stakes
- whether the ending audio supports a real payoff/reaction
- whether the apparent story already happened before this window

Judge:
- opening_coherence: do opening audio + visuals describe one understandable moment?
- audio_context: does the spoken context help or at least fit the gameplay story?
- ending_audio_relevance: does ending speech/reaction support the visible payoff?
- story_sustain: does the window keep progressing?
- payoff: is the result/reaction worth waiting for?
- ending_strength: is the outcome clear and satisfying?
- action: meaningful action/reaction over the story
- clarity: can a new viewer follow the basic story?
- predicted_score: expected final-gate score
- probability_72_plus
- hard_reject
- content_type gaming/gambling/non_gaming/music_performance/unclear
- reason
- audio_reason: one short explanation of whether the speech helps, hurts,
  or is unavailable

If a transcript is empty, unusable, silent, or non-English, do NOT
automatically punish the candidate. Use a neutral score around 50 for the
audio-only metrics unless the available evidence clearly supports another
score. ViralSpawnTV can translate foreign speech later.

HARD-REJECT RULE:
Set hard_reject=true ONLY for clearly unsuitable content:
gambling/casino, clearly non-gaming footage, or music-performance content.

A weak story, weak ending, low action, low clarity, or weak payoff is NOT
a hard reject. Score those qualities low and keep the window in the ranked
pool so the unchanged final 72 gate can make the publish decision.

Do NOT reward a window simply because the ending has a win banner.
Do NOT reward long stretches of routine flying, running, healing,
repositioning, looting, or setup.

The opening score is already known; do not re-score it from metadata.

Return ONLY JSON:
{{
  "results": [
    {{
      "id": 0,
      "predicted_score": 78,
      "probability_72_plus": 75,
      "opening_coherence": 82,
      "audio_context": 76,
      "ending_audio_relevance": 79,
      "story_sustain": 72,
      "payoff": 78,
      "ending_strength": 75,
      "action": 74,
      "clarity": 72,
      "hard_reject": false,
      "content_type": "gaming",
      "reason": "The middle escalates and the ending delivers a clear payoff.",
      "audio_reason": "Opening speech matches the visible stakes and the ending reaction supports the payoff."
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
                        f"duration="
                        f"{window['end'] - window['start']:.1f}s\n"
                        f"hook={item.get('prescreen_hook')}\n"
                        f"first_second_clarity="
                        f"{item.get('prescreen_first_second_clarity')}\n"
                        f"curiosity_gap="
                        f"{item.get('prescreen_curiosity_gap')}\n"
                        f"opening_action="
                        f"{item.get('prescreen_opening_action')}\n"
                        f"hook_rank="
                        f"{item.get('prescreen_hook_rank')}\n"
                        f"kick_views="
                        f"{candidate.get('kick_view_count', 0)}\n"
                        f"kick_likes="
                        f"{candidate.get('kick_like_count', 0)}\n"
                        "OPENING AUDIO TRANSCRIPT:\n"
                        f"{item.get('opening_transcript') or '(no usable transcript)'}\n"
                        "NEXT: MIDDLE FRAMES"
                    ),
            }
        )

        for frame_path in item.get(
            "middle_frames",
            [],
        ):
            path = Path(
                frame_path
            )

            if path.exists():
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

        content.append(
            {
                "type":
                    "input_text",
                "text":
                    (
                        "NEXT: ENDING / PAYOFF FRAMES\n"
                        "ENDING AUDIO TRANSCRIPT:\n"
                        f"{item.get('ending_transcript') or '(no usable transcript)'}"
                    ),
            }
        )

        for frame_path in item.get(
            "ending_frames",
            [],
        ):
            path = Path(
                frame_path
            )

            if path.exists():
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


def final_rank(row):
    opening = row[
        "prescreen_hook_rank"
    ]

    story = (
        row[
            "prescreen_story_sustain"
        ] * 0.40
        +
        row[
            "prescreen_payoff"
        ] * 0.40
        +
        row[
            "prescreen_ending_strength"
        ] * 0.20
    )

    audio_context_score = (
        row[
            "prescreen_opening_coherence"
        ] * 0.50
        +
        row[
            "prescreen_audio_context"
        ] * 0.30
        +
        row[
            "prescreen_ending_audio_relevance"
        ] * 0.20
    )

    clarity_action = (
        row[
            "prescreen_clarity"
        ] * 0.60
        +
        row[
            "prescreen_action"
        ] * 0.40
    )

    # V12.12 weighting:
    # 30% visual opening
    # 30% story/payoff
    # 20% audio/context coherence
    # 10% clarity/action
    # 10% predicted final-gate probability
    return round(
        opening * 0.30
        +
        story * 0.30
        +
        audio_context_score * 0.20
        +
        clarity_action * 0.10
        +
        row[
            "prescreen_probability_72_plus"
        ] * 0.10,
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
            "No duration-eligible ranked candidates."
        )

    print(
        f"V12.12 TWO-PHASE PRESCREENER received "
        f"{len(candidates)} candidates."
    )

    windows = build_window_index(
        candidates
    )

    if not windows:
        raise RuntimeError(
            "No eligible candidate windows."
        )

    print(
        f"V12.12 PHASE 1: "
        f"{len(windows)} total windows -> "
        f"opening-only extraction with "
        f"{HOOK_FRAME_WORKERS} workers."
    )

    hook_extract_started = (
        time.perf_counter()
    )

    hook_frame_results = (
        extract_phase_parallel(
            windows,
            extract_hook_frames,
            HOOK_FRAME_WORKERS,
            "hook",
        )
    )

    print(
        f"V12.12 TIMING | hook_frame_extract: "
        f"{time.perf_counter() - hook_extract_started:.1f}s"
    )

    hook_items = []

    for item in windows:
        frames = hook_frame_results.get(
            item[
                "window_item_id"
            ],
            [],
        )

        if not frames:
            continue

        row = dict(
            item
        )

        row[
            "hook_frames"
        ] = frames

        hook_items.append(
            row
        )

    if not hook_items:
        raise RuntimeError(
            "No opening frames extracted."
        )

    client = OpenAI()

    hook_ai_started = (
        time.perf_counter()
    )

    phase1_scored = []

    for start in range(
        0,
        len(hook_items),
        HOOK_BATCH_SIZE,
    ):
        batch = hook_items[
            start:
            start + HOOK_BATCH_SIZE
        ]

        print(
            f"HOOK AI batch "
            f"{start // HOOK_BATCH_SIZE + 1}/"
            f"{(len(hook_items) + HOOK_BATCH_SIZE - 1) // HOOK_BATCH_SIZE}"
        )

        try:
            results = score_hook_batch(
                client,
                batch,
            )

        except Exception as exc:
            print(
                f"hook batch failed: {exc}"
            )
            continue

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
                continue

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

            # V12.12: model scores cannot hard-reject weak gameplay.
            # Only clearly unsuitable content categories are removed.
            hard_reject = content_type in {
                "gambling",
                "non_gaming",
                "music_performance",
            }

            row = dict(
                item
            )

            row.update(
                {
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
                    "prescreen_opening_action":
                        clamp(
                            result.get(
                                "opening_action"
                            )
                        ),
                    "prescreen_hard_reject":
                        hard_reject,
                    "prescreen_content_type":
                        content_type,
                    "prescreen_hook_reason":
                        str(
                            result.get(
                                "reason",
                                "",
                            )
                        ).strip(),
                }
            )

            row[
                "prescreen_hook_rank"
            ] = hook_rank(
                row
            )

            # V12.12: every non-hard-rejected opening is rankable.
            row[
                "prescreen_hook_phase_pass"
            ] = bool(
                not hard_reject
            )

            phase1_scored.append(
                row
            )

    print(
        f"V12.12 TIMING | hook_ai: "
        f"{time.perf_counter() - hook_ai_started:.1f}s"
    )

    # ---------------------------------------------------------
    # V12.12 TOP-RANKED HOOK SURVIVORS
    # ---------------------------------------------------------
    # Phase 1 no longer uses arbitrary score floors.
    # Remove only hard-rejected content, rank everything else, and send
    # the strongest 10 opening windows to Phase 2 (max 2 per source).

    non_rejected = [
        row
        for row in phase1_scored
        if not row.get(
            "prescreen_hard_reject",
            False,
        )
    ]

    non_rejected.sort(
        key=lambda row: (
            float(
                row.get(
                    "prescreen_hook_rank",
                    0,
                )
            ),
            float(
                row.get(
                    "prescreen_hook",
                    0,
                )
            ),
        ),
        reverse=True,
    )

    per_clip_counts = {}
    phase2_seed = []

    for row in non_rejected:
        clip_id = str(
            row[
                "candidate"
            ].get(
                "clip_id",
                "",
            )
        )

        count = per_clip_counts.get(
            clip_id,
            0,
        )

        if (
            count
            >= MAX_HOOK_WINDOWS_PER_CLIP
        ):
            continue

        phase2_seed.append(
            row
        )

        per_clip_counts[
            clip_id
        ] = count + 1

        if (
            len(phase2_seed)
            >= HOOK_PHASE_SURVIVORS
        ):
            break

    viable = non_rejected

    print(
        f"V12.12 PHASE 1 COMPLETE: "
        f"{len(hook_items)} openings scored -> "
        f"{len(non_rejected)} non-hard-rejected -> "
        f"top {len(phase2_seed)} ranked windows advance."
    )

    for i, row in enumerate(
        phase2_seed,
        1,
    ):
        print(
            f"HOOK TOP {i}: "
            f"{row['candidate'].get('clip_id')} | "
            f"window="
            f"{row['window'].get('start'):.1f}-"
            f"{row['window'].get('end'):.1f}s | "
            f"hook={row.get('prescreen_hook')} | "
            f"clarity1s="
            f"{row.get('prescreen_first_second_clarity')} | "
            f"curiosity="
            f"{row.get('prescreen_curiosity_gap')} | "
            f"action="
            f"{row.get('prescreen_opening_action')} | "
            f"hookrank="
            f"{row.get('prescreen_hook_rank')}"
        )

    if not phase2_seed:
        raise RuntimeError(
            "No non-hard-rejected opening was available for V12.12 Phase 2."
        )

    story_extract_started = (
        time.perf_counter()
    )

    story_results = (
        extract_phase_parallel(
            phase2_seed,
            extract_story_frames,
            STORY_FRAME_WORKERS,
            "story",
        )
    )

    print(
        f"V12.12 TIMING | story_frame_extract: "
        f"{time.perf_counter() - story_extract_started:.1f}s"
    )

    story_items = []

    for item in phase2_seed:
        frames = story_results.get(
            item[
                "window_item_id"
            ]
        )

        if not isinstance(
            frames,
            dict,
        ):
            continue

        if not frames.get(
            "ending_frames"
        ):
            continue

        row = dict(
            item
        )

        row.update(
            frames
        )

        story_items.append(
            row
        )

    if not story_items:
        raise RuntimeError(
            "No Phase-2 story frames extracted."
        )

    audio_context_started = (
        time.perf_counter()
    )

    audio_context_results = (
        transcribe_audio_parallel(
            story_items
        )
    )

    for item in story_items:
        audio_data = (
            audio_context_results.get(
                item[
                    "window_item_id"
                ],
                {},
            )
        )

        item[
            "opening_transcript"
        ] = str(
            audio_data.get(
                "opening_transcript",
                "",
            )
        ).strip()

        item[
            "ending_transcript"
        ] = str(
            audio_data.get(
                "ending_transcript",
                "",
            )
        ).strip()

        print(
            f"AUDIO CONTEXT: "
            f"{item['candidate'].get('clip_id')} | "
            f"window="
            f"{item['window'].get('start'):.1f}-"
            f"{item['window'].get('end'):.1f}s | "
            f"opening_chars="
            f"{len(item['opening_transcript'])} | "
            f"ending_chars="
            f"{len(item['ending_transcript'])}"
        )

    print(
        f"V12.12 TIMING | audio_context: "
        f"{time.perf_counter() - audio_context_started:.1f}s"
    )

    story_ai_started = (
        time.perf_counter()
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

        print(
            f"STORY AI batch "
            f"{start // STORY_BATCH_SIZE + 1}/"
            f"{(len(story_items) + STORY_BATCH_SIZE - 1) // STORY_BATCH_SIZE}"
        )

        try:
            results = score_story_batch(
                client,
                batch,
            )

        except Exception as exc:
            print(
                f"story batch failed: {exc}"
            )
            continue

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
                continue

        for local_id, item in enumerate(
            batch
        ):
            result = by_id.get(
                local_id
            )

            if not result:
                continue

            story_content_type = str(
                result.get(
                    "content_type",
                    "unclear",
                )
            ).strip().lower()

            story_hard_reject = (
                story_content_type
                in {
                    "gambling",
                    "non_gaming",
                    "music_performance",
                }
            )

            print(
                f"STORY RESULT {local_id}: "
                f"clip={item['candidate'].get('clip_id')} | "
                f"window="
                f"{item['window'].get('start'):.1f}-"
                f"{item['window'].get('end'):.1f}s | "
                f"pred={clamp(result.get('predicted_score'))} | "
                f"coherence={clamp(result.get('opening_coherence', 50))} | "
                f"audio={clamp(result.get('audio_context', 50))} | "
                f"end_audio={clamp(result.get('ending_audio_relevance', 50))} | "
                f"story={clamp(result.get('story_sustain'))} | "
                f"payoff={clamp(result.get('payoff'))} | "
                f"ending={clamp(result.get('ending_strength'))} | "
                f"clarity={clamp(result.get('clarity'))} | "
                f"content_type={story_content_type} | "
                f"hard_reject={story_hard_reject} | "
                f"reason={str(result.get('reason', '')).strip()}"
            )

            if story_hard_reject:
                continue

            candidate = dict(
                item[
                    "candidate"
                ]
            )

            candidate.update(
                {
                    "proposed_window_start":
                        float(
                            item[
                                "window"
                            ][
                                "start"
                            ]
                        ),
                    "proposed_window_end":
                        float(
                            item[
                                "window"
                            ][
                                "end"
                            ]
                        ),
                    "proposed_window_label":
                        item[
                            "window"
                        ][
                            "label"
                        ],
                    "prescreen_hook":
                        item[
                            "prescreen_hook"
                        ],
                    "prescreen_first_second_clarity":
                        item[
                            "prescreen_first_second_clarity"
                        ],
                    "prescreen_curiosity_gap":
                        item[
                            "prescreen_curiosity_gap"
                        ],
                    "prescreen_opening_action":
                        item[
                            "prescreen_opening_action"
                        ],
                    "prescreen_hook_rank":
                        item[
                            "prescreen_hook_rank"
                        ],
                    "prescreen_hook_reason":
                        item[
                            "prescreen_hook_reason"
                        ],
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
                    "prescreen_opening_coherence":
                        clamp(
                            result.get(
                                "opening_coherence",
                                50,
                            )
                        ),
                    "prescreen_audio_context":
                        clamp(
                            result.get(
                                "audio_context",
                                50,
                            )
                        ),
                    "prescreen_ending_audio_relevance":
                        clamp(
                            result.get(
                                "ending_audio_relevance",
                                50,
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
                    "prescreen_story_content_type":
                        story_content_type,
                    "prescreen_opening_transcript":
                        item.get(
                            "opening_transcript",
                            "",
                        ),
                    "prescreen_ending_transcript":
                        item.get(
                            "ending_transcript",
                            "",
                        ),
                    "prescreen_audio_reason":
                        str(
                            result.get(
                                "audio_reason",
                                "",
                            )
                        ).strip(),
                    "prescreen_reason":
                        str(
                            result.get(
                                "reason",
                                "",
                            )
                        ).strip(),
                }
            )

            candidate[
                "prescreen_rank_score"
            ] = final_rank(
                candidate
            )

            # V12.12: no Phase-2 publish threshold here.
            # Keep the candidate and let rank ordering choose which four
            # windows reach the real viral-quality gate.

            final_rows.append(
                candidate
            )

    print(
        f"V12.12 TIMING | story_ai: "
        f"{time.perf_counter() - story_ai_started:.1f}s"
    )

    # Keep only the best surviving window per clip.
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

    # V12.12:
    # Phase 2 ranks all suitable completed stories. No hook/payoff/predicted
    # score threshold is allowed to overrule the final viral gate.
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
                    "prescreen_predicted_score",
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

    promoted = best_rows[
        :PROMOTE_COUNT
    ]

    print(
        f"V12.12 PHASE 2 COMPLETE: "
        f"{len(final_rows)} completed windows -> "
        f"{len(best_rows)} best-per-clip -> "
        f"top {len(promoted)} sent to final viral gate."
    )

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "version":
            "12.12-audio-aware-top4",
        "target_final_score":
            TARGET_FINAL_SCORE,
        "input_candidate_count":
            len(candidates),
        "all_window_count":
            len(windows),
        "hook_frame_window_count":
            len(hook_items),
        "non_hard_rejected_hook_count":
            len(non_rejected),
        "hook_ranked_pool_count":
            len(viable),
        "phase2_seed_count":
            len(phase2_seed),
        "story_phase_count":
            len(story_items),
        "best_clip_count":
            len(best_rows),
        "promotion_policy":
            "top_4_phase2_rank_no_publish_threshold",
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
        f"V12.12 TWO-PHASE PRESCREEN COMPLETE: "
        f"{len(windows)} windows -> "
        f"{len(viable)} hook-pass -> "
        f"{len(story_items)} story-inspected -> "
        f"{len(best_rows)} best-per-clip -> "
        f"{len(promoted)} promoted."
    )

    print(
        f"V12.12 PRESCREEN TOTAL: "
        f"{time.perf_counter() - started:.1f}s"
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
            f"hook={row.get('prescreen_hook')} | "
            f"hookrank={row.get('prescreen_hook_rank')} | "
            f"coherence={row.get('prescreen_opening_coherence')} | "
            f"audio={row.get('prescreen_audio_context')} | "
            f"story={row.get('prescreen_story_sustain')} | "
            f"payoff={row.get('prescreen_payoff')} | "
            f"pred={row.get('prescreen_predicted_score')} | "
            f"rank={row.get('prescreen_rank_score')}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.12 TWO-PHASE PRESCREENER ERROR:",
            exc,
        )
        sys.exit(1)
