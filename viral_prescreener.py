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

# V12.7 scans a deeper metadata bench cheaply, but only spends visual/AI
# work on the first 40 duration-eligible clips.
MAX_SCAN = 70
MAX_VISUAL_PRESCREEN = 40
PROMOTE_COUNT = 25
BATCH_SIZE = 8

# Keep these aligned with the real gate and V5.9 production strategy.
TARGET_FINAL_SCORE = 72
MIN_SOURCE_SECONDS = 49.0
MIN_BIG_HOOK_SCORE = 72
MIN_FIRST_SECOND_CLARITY = 60
MIN_CURIOSITY_GAP = 65


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
):
    """
    One FFmpeg pass produces:
    - 3 BIG-HOOK frames from roughly the first 2 seconds
    - 2 context/payoff frames from later in the clip

    This keeps runtime close to the old prescreener while giving the AI
    much better information about whether second 0-2 can hold a viewer.
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

    early_pattern = str(
        clip_dir
        /
        "hook_%02d.jpg"
    )

    later_pattern = str(
        clip_dir
        /
        "context_%02d.jpg"
    )

    filter_complex = (
        "[0:v]split=2[earlysrc][latersrc];"
        "[earlysrc]"
        "trim=start=0:end=2,"
        "setpts=PTS-STARTPTS,"
        "fps=1.5,"
        "scale=360:-2"
        "[early];"
        "[latersrc]"
        "trim=start=8:end=30,"
        "setpts=PTS-STARTPTS,"
        "fps=1/10,"
        "scale=360:-2"
        "[later]"
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
        "[early]",
        "-frames:v",
        "3",
        "-q:v",
        "5",
        early_pattern,

        "-map",
        "[later]",
        "-frames:v",
        "2",
        "-q:v",
        "5",
        later_pattern,
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
    )[:3]

    context_frames = sorted(
        clip_dir.glob(
            "context_*.jpg"
        )
    )[:2]

    return (
        hook_frames,
        context_frames,
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
                    context_frames,
                ) = extract_preview_frames(
                    playlist,
                    clip_id,
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
                    "_context_frames"
                ] = [
                    str(frame)
                    for frame in context_frames
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
You are the BIG-HOOK VISUAL PRESCREENER for ViralSpawnTV.

ViralSpawnTV is now targeting 50-60 second gaming Shorts.
The FINAL viral-quality gate is {TARGET_FINAL_SCORE}+.

Your most important job is NOT simply to find good gameplay.
Your job is to find source clips whose FIRST 1-2 SECONDS can become a
strong "Big Hook Opening" that stops a Shorts viewer from swiping.

For each candidate you will see:
1. Three HOOK FRAMES sampled from roughly second 0-2.
2. Up to two CONTEXT/PAYOFF FRAMES sampled later in the clip.
3. Metadata.

Score the OPENING aggressively.

A strong Big Hook opening has one or more of:
- immediate danger or likely failure
- a difficult challenge already in progress
- an impossible-looking situation
- a surprising visual
- a funny problem needing resolution
- clutch pressure
- an obvious unresolved question
- a viewer can understand the stakes very quickly

Penalize the opening heavily for:
- menus/lobbies
- walking/travel with no threat
- waiting
- ordinary looting/setup
- static facecam with no clear reaction
- generic gameplay with no visible stakes
- context that takes several seconds to understand
- footage where the first 1-2 seconds give no reason to keep watching

IMPORTANT:
Do not reward a clip merely because something good may happen later.
The OPENING itself needs to give us material for a strong first-second hook.

The later frames are only to estimate whether the clip can sustain a
50-60 second story and deliver a payoff.

For EVERY candidate return:
- predicted_score: expected real-gate score 0-100
- probability_72_plus: 0-100 confidence it passes the real gate
- hook: overall opening-hook strength 0-100
- first_second_clarity: how quickly a new viewer understands the situation
- curiosity_gap: how strongly the opening creates a need to know the outcome
- opening_action: amount of meaningful visible action/reaction immediately
- payoff: likely payoff strength
- action: overall action/reaction
- clarity: overall story clarity
- hook_type: danger/challenge/impossible/surprise/comedy/clutch/other
- reason: one short evidence-based explanation

SCORING STANDARD:
90-100 hook = exceptional stop-the-scroll opening.
80-89 = strong Big Hook material.
72-79 = usable but needs strong editing.
60-71 = mediocre opening.
Below 60 = weak opening; should normally lose to stronger candidates.

Do not inflate scores.
Do not invent unseen events.

Return ONLY JSON:
{{
  "results": [
    {{
      "id": 0,
      "predicted_score": 77,
      "probability_72_plus": 78,
      "hook": 84,
      "first_second_clarity": 78,
      "curiosity_gap": 86,
      "opening_action": 82,
      "payoff": 72,
      "action": 80,
      "clarity": 70,
      "hook_type": "clutch",
      "reason": "Immediate pressure is visible and the outcome is unresolved."
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
                        f"game: {row.get('game')}\n"
                        f"channel: {row.get('channel')}\n"
                        f"title: {row.get('page_title', '')}\n"
                        f"description: "
                        f"{row.get('page_description', '')}\n"
                        f"metadata_score: "
                        f"{row.get('v12_metadata_score', 0)}\n"
                        f"source_duration_seconds: "
                        f"{row.get('prescreen_source_duration_seconds')}\n"
                        "THE NEXT IMAGES ARE FIRST-2-SECOND HOOK FRAMES:"
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
                    "THE NEXT IMAGES ARE LATER CONTEXT/PAYOFF FRAMES:",
            }
        )

        for frame_path in row.get(
            "_context_frames",
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
        f"V12.7 Big Hook prescreener received "
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
        f"V12.6 visual prescreen acquired previews for "
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
                    "prescreen_payoff":
                        clamp(
                            result.get(
                                "payoff"
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
                    "prescreen_reason":
                        str(
                            result.get(
                                "reason",
                                "",
                            )
                        ).strip(),
                }
            )

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

            # V12.7: Big Hook dominates the ranking.
            #
            # 70% of the score now comes from the opening itself:
            # hook + curiosity gap + first-second clarity.
            rank_score = (
                promoted[
                    "prescreen_hook"
                ] * 0.35
                +
                promoted[
                    "prescreen_curiosity_gap"
                ] * 0.20
                +
                promoted[
                    "prescreen_first_second_clarity"
                ] * 0.15
                +
                promoted[
                    "prescreen_probability_72_plus"
                ] * 0.10
                +
                promoted[
                    "prescreen_predicted_score"
                ] * 0.08
                +
                promoted[
                    "prescreen_payoff"
                ] * 0.07
                +
                promoted[
                    "prescreen_opening_action"
                ] * 0.05
            )

            # Weak openings are deliberately demoted even if the later
            # gameplay looks good.
            if promoted[
                "prescreen_hook"
            ] < 60:
                rank_score *= 0.55

            elif promoted[
                "prescreen_hook"
            ] < MIN_BIG_HOOK_SCORE:
                rank_score *= 0.78

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
                    "prescreen_big_hook_pass",
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
                    "prescreen_hook",
                    0,
                )
            ),
            float(
                row.get(
                    "prescreen_probability_72_plus",
                    0,
                )
            ),
        ),
        reverse=True,
    )

    promoted = scored[
        :PROMOTE_COUNT
    ]

    OUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUT.write_text(
        json.dumps(
            {
                "version":
                    "12.7-big-hook-prescreen",
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
        f"V12.7 BIG HOOK PRESCREEN COMPLETE: "
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
            f"clarity1s={row.get('prescreen_first_second_clarity')} | "
            f"curiosity={row.get('prescreen_curiosity_gap')} | "
            f"hookpass={row.get('prescreen_big_hook_pass')} | "
            f"rank={row.get('prescreen_rank_score')}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "VIRAL PRESCREENER ERROR:",
            exc,
        )
        sys.exit(1)
