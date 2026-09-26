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

MAX_PRESCREEN = 100
PROMOTE_COUNT = 30
BATCH_SIZE = 5

# Keep this aligned with the real gate in viral_gate.py.
TARGET_FINAL_SCORE = 72


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
            2500
        )

        try:
            page.locator(
                "video"
            ).first.click(
                timeout=1500
            )

            page.wait_for_timeout(
                1200
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


def extract_preview_frames(
    playlist_url,
    clip_id,
):
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
        "frame_*.jpg"
    ):
        old.unlink()

    pattern = str(
        clip_dir
        /
        "frame_%02d.jpg"
    )

    # Match the real viral gate's early-clip visual sampling style,
    # but use fewer/smaller frames to keep this prescreen cheaper.
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        playlist_url,
        "-vf",
        "fps=1/8,scale=480:-2",
        "-frames:v",
        "4",
        "-q:v",
        "5",
        pattern,
    ]

    subprocess.run(
        command,
        check=True,
        timeout=90,
    )

    return sorted(
        clip_dir.glob(
            "frame_*.jpg"
        )
    )[:4]


def build_visual_candidates(
    candidates,
):
    visual = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page()

        for i, candidate in enumerate(
            candidates,
            1,
        ):
            clip_id = candidate.get(
                "clip_id"
            )

            clip_url = candidate.get(
                "clip_url"
            )

            print(
                f"PRESCREEN PREVIEW "
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
                        f"  no HLS playlist captured"
                    )
                    continue

                frames = (
                    extract_preview_frames(
                        playlist,
                        clip_id,
                    )
                )

                if not frames:
                    print(
                        f"  no preview frames extracted"
                    )
                    continue

                row = dict(
                    candidate
                )

                row[
                    "_preview_frames"
                ] = [
                    str(frame)
                    for frame in frames
                ]

                visual.append(
                    row
                )

            except Exception as exc:
                print(
                    f"  preview failed: {exc}"
                )

        browser.close()

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
You are the LIGHTWEIGHT VISUAL PRESCREENER for ViralSpawnTV.

Your job is to predict which candidates are MOST LIKELY to pass the
real ViralSpawnTV viral-quality gate at {TARGET_FINAL_SCORE}+.

You are seeing:
- clip metadata
- 1-4 lightweight frames sampled from roughly the first 30 seconds

This is NOT the final gate.
Do not demand certainty.
Predict which clips deserve the expensive full audio/transcript/video gate.

Use the same concepts as the real gate:
- immediate hook
- payoff
- visible action/reaction
- clarity
- stakes/tension/comedy/skill/surprise
- whether a 15-45 second Short could be compelling

Penalize:
- lobby/menu/static footage
- low action
- unclear context
- no visible escalation
- ordinary moments
- confusing footage

A clip can still be promising when metadata is sparse.
Do not invent unseen events.

For EVERY candidate, return:
- predicted_score: expected real-gate score 0-100
- probability_72_plus: integer 0-100 representing your confidence
  that the real gate will score it {TARGET_FINAL_SCORE} or higher
- hook
- payoff
- action
- clarity
- reason

Return ONLY JSON:
{{
  "results": [
    {{
      "id": 0,
      "predicted_score": 76,
      "probability_72_plus": 78,
      "hook": 82,
      "payoff": 70,
      "action": 79,
      "clarity": 68,
      "reason": "short evidence-based reason"
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
                    ),
            }
        )

        for frame_path in row.get(
            "_preview_frames",
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
    )[:MAX_PRESCREEN]

    if not candidates:
        raise RuntimeError(
            "No candidates available for visual prescreen."
        )

    print(
        f"V12.4 visual prescreen received "
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
        f"V12.4 visual prescreen acquired previews for "
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
                    "prescreen_reason":
                        str(
                            result.get(
                                "reason",
                                "",
                            )
                        ).strip(),
                }
            )

            # Composite ranking favors predicted pass probability first,
            # then expected real-gate score and hook quality.
            promoted[
                "prescreen_rank_score"
            ] = round(
                (
                    promoted[
                        "prescreen_probability_72_plus"
                    ] * 0.50
                    +
                    promoted[
                        "prescreen_predicted_score"
                    ] * 0.30
                    +
                    promoted[
                        "prescreen_hook"
                    ] * 0.12
                    +
                    promoted[
                        "prescreen_payoff"
                    ] * 0.08
                ),
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
            float(
                row.get(
                    "prescreen_rank_score",
                    0,
                )
            ),
            float(
                row.get(
                    "prescreen_probability_72_plus",
                    0,
                )
            ),
            float(
                row.get(
                    "prescreen_predicted_score",
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
                    "12.4-lightweight-visual-prescreen",
                "target_final_score":
                    TARGET_FINAL_SCORE,
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
        f"V12.4 PRESCREEN COMPLETE: "
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
