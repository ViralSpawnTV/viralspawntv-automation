import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI
from playwright.sync_api import sync_playwright


INPUT = Path("work/v12_ranked_candidates.json")
OUT = Path("work/v12_prescreened_candidates.json")
WORK = Path("work/viral_prescreen")
WORK.mkdir(parents=True, exist_ok=True)

# V12.8 scans the full metadata bench cheaply, then spends visual/AI
# work on no more than 40 duration-eligible clips.
MAX_SCAN = 100
MAX_VISUAL_PRESCREEN = 40
PROMOTE_COUNT = 15
BATCH_SIZE = 6

# Keep these aligned with the real gate and V5.9.1 production strategy.
TARGET_FINAL_SCORE = 72
MIN_SOURCE_SECONDS = 49.0

# Opening standards.
MIN_BIG_HOOK_SCORE = 70
MIN_FIRST_SECOND_CLARITY = 60
MIN_CURIOSITY_GAP = 65

# Full-story standards.
MIN_PAYOFF_SCORE = 60
MIN_STORY_SUSTAIN = 60
MIN_ENDING_STRENGTH = 55
MIN_OVERALL_CLARITY = 55

# A small fallback pool is allowed only when a candidate is close enough
# to justify the expensive full gate.
MIN_FALLBACK_PREDICTED = 67
MIN_FALLBACK_HOOK = 68
MIN_FALLBACK_PAYOFF = 58
MIN_FALLBACK_STORY = 58


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


def capture_playlist(
    page,
    clip_url,
    clip_id,
):
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
            clip_url,
            wait_until="domcontentloaded",
            timeout=35000,
        )

        page.wait_for_timeout(
            1000
        )

        try:
            page.locator(
                "video"
            ).first.click(
                timeout=1500
            )

            page.wait_for_timeout(
                500
            )

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

    if not playlist_urls:
        return None

    for url in playlist_urls:
        if (
            str(clip_id).lower()
            in url.lower()
        ):
            return url

    return playlist_urls[-1]



def probe_stream_duration(playlist_url):
    """
    Cheap duration check before we spend visual/AI work on a source.
    Returns None if ffprobe cannot determine duration reliably.
    """
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
            timeout=25,
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


def extract_preview_frames(
    playlist_url,
    clip_id,
    source_seconds,
):
    """
    V12.8 samples the whole 50-60 second story shape in one FFmpeg pass:

    - 2 HOOK frames from roughly second 0-2
    - 2 MIDDLE frames around the center of the source
    - 2 ENDING/PAYOFF frames from the final ~12 seconds

    The prior version mostly stopped around second 30, which let clips with
    excellent openings but weak endings rank too highly.
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

    # Unknown duration was rare in testing. Use a conservative 60-second
    # estimate so the hook/middle branches still work; the ending branch may
    # simply produce fewer frames if the stream is actually shorter.
    seconds = float(
        source_seconds
        if source_seconds is not None
        else 60.0
    )

    middle_center = seconds * 0.50

    middle_start = max(
        4.0,
        middle_center - 6.0,
    )

    middle_end = min(
        max(
            middle_start + 4.0,
            seconds - 12.0,
        ),
        middle_center + 6.0,
    )

    if middle_end <= middle_start:
        middle_end = min(
            seconds,
            middle_start + 8.0,
        )

    ending_start = max(
        2.0,
        seconds - 12.0,
    )

    ending_end = seconds

    hook_pattern = str(
        clip_dir
        /
        "hook_%02d.jpg"
    )

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

    filter_complex = (
        "[0:v]split=3[hooksrc][midsrc][endsrc];"

        "[hooksrc]"
        "trim=start=0:end=2,"
        "setpts=PTS-STARTPTS,"
        "fps=1,"
        "scale=320:-2"
        "[hook];"

        "[midsrc]"
        f"trim=start={middle_start:.3f}:end={middle_end:.3f},"
        "setpts=PTS-STARTPTS,"
        "fps=1/5,"
        "scale=320:-2"
        "[middle];"

        "[endsrc]"
        f"trim=start={ending_start:.3f}:end={ending_end:.3f},"
        "setpts=PTS-STARTPTS,"
        "fps=1/6,"
        "scale=320:-2"
        "[ending]"
    )

    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        playlist_url,
        "-filter_complex",
        filter_complex,

        "-map",
        "[hook]",
        "-frames:v",
        "2",
        "-q:v",
        "5",
        hook_pattern,

        "-map",
        "[middle]",
        "-frames:v",
        "2",
        "-q:v",
        "5",
        middle_pattern,

        "-map",
        "[ending]",
        "-frames:v",
        "2",
        "-q:v",
        "5",
        ending_pattern,
    ]

    subprocess.run(
        command,
        check=True,
        timeout=90,
    )

    hook_frames = sorted(
        clip_dir.glob(
            "hook_*.jpg"
        )
    )[:2]

    middle_frames = sorted(
        clip_dir.glob(
            "middle_*.jpg"
        )
    )[:2]

    ending_frames = sorted(
        clip_dir.glob(
            "ending_*.jpg"
        )
    )[:2]

    return (
        hook_frames,
        middle_frames,
        ending_frames,
    )

def build_visual_candidates(
    candidates,
):
    visual = []
    short_skips = 0
    unknown_duration = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page()

        for i, candidate in enumerate(
            candidates,
            1,
        ):
            if len(visual) >= MAX_VISUAL_PRESCREEN:
                break

            clip_id = candidate.get(
                "clip_id"
            )

            clip_url = candidate.get(
                "clip_url"
            )

            print(
                f"PRESCREEN SCAN "
                f"{i}/{len(candidates)}: "
                f"{clip_id}"
            )

            if (
                not clip_id
                or not clip_url
            ):
                continue

            try:
                playlist = capture_playlist(
                    page,
                    clip_url,
                    clip_id,
                )

                if not playlist:
                    print(
                        "  no HLS playlist captured"
                    )
                    continue

                source_seconds = (
                    probe_stream_duration(
                        playlist
                    )
                )

                if source_seconds is not None:
                    print(
                        f"  source duration: "
                        f"{source_seconds:.2f}s"
                    )

                    if (
                        source_seconds
                        <
                        MIN_SOURCE_SECONDS
                    ):
                        short_skips += 1

                        print(
                            f"  SKIP: too short for "
                            f"50-60s strategy "
                            f"(<{MIN_SOURCE_SECONDS:.0f}s)"
                        )

                        continue

                else:
                    unknown_duration += 1

                    print(
                        "  duration unknown; "
                        "keeping candidate"
                    )

                (
                    hook_frames,
                    middle_frames,
                    ending_frames,
                ) = extract_preview_frames(
                    playlist,
                    clip_id,
                    source_seconds,
                )

                if not hook_frames:
                    print(
                        "  no opening-hook frames extracted"
                    )
                    continue

                row = dict(
                    candidate
                )

                row[
                    "_hook_frames"
                ] = [
                    str(frame)
                    for frame in hook_frames
                ]

                row[
                    "_middle_frames"
                ] = [
                    str(frame)
                    for frame in middle_frames
                ]

                row[
                    "_ending_frames"
                ] = [
                    str(frame)
                    for frame in ending_frames
                ]

                row[
                    "prescreen_source_duration_seconds"
                ] = (
                    round(
                        source_seconds,
                        3,
                    )
                    if source_seconds is not None
                    else None
                )

                visual.append(
                    row
                )

            except Exception as exc:
                print(
                    f"  preview failed: {exc}"
                )

        browser.close()

    print(
        f"DURATION PREFILTER: "
        f"{short_skips} clips skipped under "
        f"{MIN_SOURCE_SECONDS:.0f}s; "
        f"{unknown_duration} unknown duration; "
        f"{len(visual)} clips sent to visual AI."
    )

    return visual

def parse_json(raw):
    raw = raw.strip()

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


def score_batch(
    client,
    batch,
):
    prompt = f"""
You are the V12.8 HOOK + STORY + PAYOFF visual prescreener for ViralSpawnTV.

ViralSpawnTV is targeting 50-60 second GAMING Shorts.
The expensive final viral-quality gate requires {TARGET_FINAL_SCORE}+.

The previous prescreener over-rewarded clips with a great first 2 seconds
even when the rest of the clip had no satisfying payoff. Fix that.

For every candidate you will see:
1. HOOK FRAMES from roughly second 0-2.
2. MIDDLE FRAMES from around the center of the source.
3. ENDING/PAYOFF FRAMES from roughly the final 12 seconds.
4. Metadata and source duration.

A candidate should rank highly only when it has BOTH:
- a strong stop-the-scroll opening, AND
- enough escalation/story/payoff to justify a 50-60 second Short.

============================================================
HARD CONTENT REJECTION
============================================================

ViralSpawnTV is GAMING ONLY.

Set hard_reject=true if the actual visible content is:
- casino / slots / roulette / sportsbook / gambling
- non-gaming bedroom/lifestyle footage
- sports talk/rant footage with no video-game content
- music performance/content rather than gameplay
- otherwise clearly non-gaming

Do NOT trust the metadata game label when the frames contradict it.

============================================================
BIG HOOK
============================================================

Reward:
- immediate danger/failure risk
- challenge already in progress
- impossible-looking situation
- surprising visual
- funny problem needing resolution
- clutch pressure
- clear unresolved question
- stakes understandable within ~1 second

Penalize:
- menus/lobbies
- walking/travel without danger
- ordinary looting
- waiting
- static chatter
- confusing clutter
- generic gameplay with no visible stakes

============================================================
50-60 SECOND STORY
============================================================

The middle must show enough progression to sustain interest.
The ending must offer a meaningful result, reaction, reversal, reveal,
clutch, fail, punchline, escape, elimination, win/loss, or other payoff.

A strong hook followed by 40 seconds of routine movement is NOT a strong
candidate.

A visually exciting middle with no ending/payoff is NOT a strong candidate.

For EVERY candidate return:
- predicted_score: expected final-gate score 0-100
- probability_72_plus: confidence 0-100
- hook: first-2-second hook strength
- first_second_clarity
- curiosity_gap
- opening_action
- story_sustain: ability to hold interest through 50-60 seconds
- middle_escalation: whether the middle meaningfully progresses
- payoff: payoff/reaction strength
- ending_strength: how satisfying/clear the ending appears
- action: overall meaningful action/reaction
- clarity: overall story clarity
- hook_type: danger/challenge/impossible/surprise/comedy/clutch/other
- content_type: gaming/gambling/non_gaming/unclear
- hard_reject: true/false
- reason: one short evidence-based explanation

Do not inflate scores.
Do not invent unseen events.

Return ONLY JSON:
{{
  "results": [
    {{
      "id": 0,
      "predicted_score": 79,
      "probability_72_plus": 76,
      "hook": 82,
      "first_second_clarity": 76,
      "curiosity_gap": 84,
      "opening_action": 80,
      "story_sustain": 74,
      "middle_escalation": 72,
      "payoff": 78,
      "ending_strength": 75,
      "action": 79,
      "clarity": 72,
      "hook_type": "clutch",
      "content_type": "gaming",
      "hard_reject": false,
      "reason": "Immediate pressure leads to escalating action and a visible payoff."
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

    for local_id, row in enumerate(
        batch
    ):
        content.append(
            {
                "type":
                    "input_text",
                "text":
                    (
                        f"CANDIDATE {local_id}\n"
                        f"clip_id: {row.get('clip_id')}\n"
                        f"game_metadata: {row.get('game')}\n"
                        f"channel: {row.get('channel')}\n"
                        f"title: {row.get('page_title', '')}\n"
                        f"description: "
                        f"{row.get('page_description', '')}\n"
                        f"metadata_score: "
                        f"{row.get('v12_metadata_score', 0)}\n"
                        f"source_duration_seconds: "
                        f"{row.get('prescreen_source_duration_seconds')}\n"
                        "NEXT: OPENING HOOK FRAMES (roughly second 0-2)"
                    ),
            }
        )

        for frame_path in row.get(
            "_hook_frames",
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
                    "NEXT: MIDDLE / ESCALATION FRAMES",
            }
        )

        for frame_path in row.get(
            "_middle_frames",
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
                    "NEXT: ENDING / PAYOFF FRAMES",
            }
        )

        for frame_path in row.get(
            "_ending_frames",
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

def clamp(value):
    try:
        return max(
            0,
            min(
                100,
                int(
                    round(
                        float(
                            value
                        )
                    )
                ),
            ),
        )
    except Exception:
        return 0


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
    )[:MAX_SCAN]

    if not candidates:
        raise RuntimeError(
            "No candidates available for visual prescreen."
        )

    print(
        f"V12.8 Hook+Story prescreener received "
        f"{len(candidates)} candidates."
    )

    visual = build_visual_candidates(
        candidates
    )

    if not visual:
        raise RuntimeError(
            "No candidate preview frames could be acquired."
        )

    print(
        f"V12.8 visual prescreen acquired previews for "
        f"{len(visual)} candidates."
    )

    client = OpenAI()

    scored = []

    for start in range(
        0,
        len(visual),
        BATCH_SIZE,
    ):
        batch = visual[
            start:
            start + BATCH_SIZE
        ]

        print(
            f"AI visual scoring batch "
            f"{start // BATCH_SIZE + 1}/"
            f"{(len(visual) + BATCH_SIZE - 1) // BATCH_SIZE}"
        )

        try:
            results = score_batch(
                client,
                batch,
            )

        except Exception as exc:
            print(
                f"Prescreen batch failed: {exc}"
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

        for local_id, row in enumerate(
            batch
        ):
            result = by_id.get(
                local_id
            )

            if not result:
                continue

            promoted = dict(
                row
            )

            promoted.pop(
                "_preview_frames",
                None,
            )

            promoted.pop(
                "_hook_frames",
                None,
            )

            promoted.pop(
                "_context_frames",
                None,
            )

            promoted.pop(
                "_middle_frames",
                None,
            )

            promoted.pop(
                "_ending_frames",
                None,
            )

            promoted.update(
                {
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
                    "prescreen_opening_action":
                        clamp(
                            result.get(
                                "opening_action"
                            )
                        ),
                    "prescreen_story_sustain":
                        clamp(
                            result.get(
                                "story_sustain"
                            )
                        ),
                    "prescreen_middle_escalation":
                        clamp(
                            result.get(
                                "middle_escalation"
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
                    "prescreen_hook_type":
                        str(
                            result.get(
                                "hook_type",
                                "other",
                            )
                        ).strip().lower(),
                    "prescreen_content_type":
                        str(
                            result.get(
                                "content_type",
                                "unclear",
                            )
                        ).strip().lower(),
                    "prescreen_hard_reject":
                        bool(
                            result.get(
                                "hard_reject",
                                False,
                            )
                        ),
                    "prescreen_reason":
                        str(
                            result.get(
                                "reason",
                                "",
                            )
                        ).strip(),
                }
            )

            # Hard visual category rejection overrides metadata labels.
            if promoted[
                "prescreen_content_type"
            ] in {
                "gambling",
                "non_gaming",
            }:
                promoted[
                    "prescreen_hard_reject"
                ] = True

            promoted[
                "prescreen_big_hook_pass"
            ] = bool(
                promoted[
                    "prescreen_hook"
                ] >= MIN_BIG_HOOK_SCORE
                and
                promoted[
                    "prescreen_first_second_clarity"
                ] >= MIN_FIRST_SECOND_CLARITY
                and
                promoted[
                    "prescreen_curiosity_gap"
                ] >= MIN_CURIOSITY_GAP
            )

            promoted[
                "prescreen_story_pass"
            ] = bool(
                promoted[
                    "prescreen_payoff"
                ] >= MIN_PAYOFF_SCORE
                and
                promoted[
                    "prescreen_story_sustain"
                ] >= MIN_STORY_SUSTAIN
                and
                promoted[
                    "prescreen_ending_strength"
                ] >= MIN_ENDING_STRENGTH
                and
                promoted[
                    "prescreen_clarity"
                ] >= MIN_OVERALL_CLARITY
            )

            promoted[
                "prescreen_full_story_pass"
            ] = bool(
                not promoted[
                    "prescreen_hard_reject"
                ]
                and
                promoted[
                    "prescreen_big_hook_pass"
                ]
                and
                promoted[
                    "prescreen_story_pass"
                ]
            )

            # V12.8 weighting:
            # 45% opening
            # 30% story/payoff
            # 15% clarity/action
            # 10% predicted final-gate probability
            opening_score = (
                promoted[
                    "prescreen_hook"
                ] * 0.50
                +
                promoted[
                    "prescreen_curiosity_gap"
                ] * 0.30
                +
                promoted[
                    "prescreen_first_second_clarity"
                ] * 0.20
            )

            story_score = (
                promoted[
                    "prescreen_payoff"
                ] * 0.45
                +
                promoted[
                    "prescreen_story_sustain"
                ] * 0.35
                +
                promoted[
                    "prescreen_ending_strength"
                ] * 0.20
            )

            clarity_action_score = (
                promoted[
                    "prescreen_clarity"
                ] * 0.60
                +
                promoted[
                    "prescreen_action"
                ] * 0.40
            )

            rank_score = (
                opening_score * 0.45
                +
                story_score * 0.30
                +
                clarity_action_score * 0.15
                +
                promoted[
                    "prescreen_probability_72_plus"
                ] * 0.10
            )

            # Strong hook but weak payoff was the main V12.7 failure mode.
            if (
                promoted[
                    "prescreen_payoff"
                ] < 50
                or
                promoted[
                    "prescreen_story_sustain"
                ] < 50
            ):
                rank_score *= 0.65

            # Weak opening still matters, but no longer dominates the whole rank.
            if promoted[
                "prescreen_hook"
            ] < 60:
                rank_score *= 0.75

            # Clear non-gaming/gambling content should never reach the full gate.
            if promoted[
                "prescreen_hard_reject"
            ]:
                rank_score = 0.0

            promoted[
                "prescreen_rank_score"
            ] = round(
                rank_score,
                2,
            )

            scored.append(
                promoted
            )

    if not scored:
        raise RuntimeError(
            "Visual prescreen produced no scored candidates."
        )

    scored.sort(
        key=lambda row: (
            bool(
                row.get(
                    "prescreen_full_story_pass",
                    False,
                )
            ),
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
            float(
                row.get(
                    "prescreen_hook",
                    0,
                )
            ),
        ),
        reverse=True,
    )

    qualified = [
        row
        for row in scored
        if row.get(
            "prescreen_full_story_pass",
            False,
        )
    ]

    fallback = [
        row
        for row in scored
        if (
            not row.get(
                "prescreen_hard_reject",
                False,
            )
            and
            not row.get(
                "prescreen_full_story_pass",
                False,
            )
            and
            row.get(
                "prescreen_predicted_score",
                0,
            ) >= MIN_FALLBACK_PREDICTED
            and
            row.get(
                "prescreen_hook",
                0,
            ) >= MIN_FALLBACK_HOOK
            and
            row.get(
                "prescreen_payoff",
                0,
            ) >= MIN_FALLBACK_PAYOFF
            and
            row.get(
                "prescreen_story_sustain",
                0,
            ) >= MIN_FALLBACK_STORY
        )
    ]

    promoted = (
        qualified
        +
        fallback
    )[:PROMOTE_COUNT]

    hard_rejected_count = sum(
        1
        for row in scored
        if row.get(
            "prescreen_hard_reject",
            False,
        )
    )

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUT.write_text(
        json.dumps(
            {
                "version":
                    "12.8-hook-story-payoff-prescreen",
                "target_final_score":
                    TARGET_FINAL_SCORE,
                "minimum_source_seconds":
                    MIN_SOURCE_SECONDS,
                "minimum_big_hook_score":
                    MIN_BIG_HOOK_SCORE,
                "minimum_first_second_clarity":
                    MIN_FIRST_SECOND_CLARITY,
                "minimum_curiosity_gap":
                    MIN_CURIOSITY_GAP,
                "minimum_payoff_score":
                    MIN_PAYOFF_SCORE,
                "minimum_story_sustain":
                    MIN_STORY_SUSTAIN,
                "minimum_ending_strength":
                    MIN_ENDING_STRENGTH,
                "hard_rejected_count":
                    hard_rejected_count,
                "qualified_count":
                    len(qualified),
                "fallback_count":
                    len(fallback),
                "input_candidate_count":
                    len(candidates),
                "preview_success_count":
                    len(visual),
                "scored_count":
                    len(scored),
                "promoted_count":
                    len(promoted),
                "candidates":
                    promoted,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print()
    print(
        f"V12.8 HOOK+STORY PRESCREEN COMPLETE: "
        f"{len(candidates)} input -> "
        f"{len(visual)} previews -> "
        f"{len(scored)} scored -> "
        f"{len(promoted)} promoted."
    )

    for i, row in enumerate(
        promoted[:10],
        1,
    ):
        print(
            f"TOP {i}: "
            f"{row.get('clip_id')} | "
            f"pred={row.get('prescreen_predicted_score')} | "
            f"P72={row.get('prescreen_probability_72_plus')} | "
            f"hook={row.get('prescreen_hook')} | "
            f"story={row.get('prescreen_story_sustain')} | "
            f"payoff={row.get('prescreen_payoff')} | "
            f"ending={row.get('prescreen_ending_strength')} | "
            f"fullpass={row.get('prescreen_full_story_pass')} | "
            f"rank={row.get('prescreen_rank_score')}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.8 VIRAL PRESCREENER ERROR:",
            exc,
        )
        sys.exit(1)
