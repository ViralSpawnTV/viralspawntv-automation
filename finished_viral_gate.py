import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI


WORK = Path("work/production")
VIDEO = WORK / "ViralSpawnTV_Short_V4.mp4"
METADATA = WORK / "ViralSpawnTV_V4_metadata.json"
RESULT = WORK / "finished_viral_gate.json"
AUDIO = WORK / "finished_gate_audio.wav"
FRAMES = WORK / "finished_gate_frames"
FRAMES.mkdir(
    parents=True,
    exist_ok=True,
)

MIN_SCORE = 72
MIN_HOOK = 65
MIN_PAYOFF = 60


def run(command):
    subprocess.run(
        command,
        check=True,
    )


def probe_duration():
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(VIDEO),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    return float(
        result.stdout.strip()
    )


def extract_audio():
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(VIDEO),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(AUDIO),
        ]
    )


def extract_frame(
    timestamp,
    name,
):
    path = (
        FRAMES
        /
        f"{name}.jpg"
    )

    if path.exists():
        path.unlink()

    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(VIDEO),
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2",
            "-q:v",
            "4",
            str(path),
        ]
    )

    if (
        path.exists()
        and
        path.stat().st_size > 0
    ):
        return path

    return None


def extract_frames(
    seconds,
):
    # First-second hook evidence is intentionally dense.
    timestamps = [
        (
            0.20,
            "hook_020",
        ),
        (
            0.80,
            "hook_080",
        ),
        (
            1.60,
            "hook_160",
        ),
    ]

    usable_end = max(
        2.0,
        seconds - 1.2,
    )

    for fraction, name in [
        (
            0.25,
            "story_25",
        ),
        (
            0.50,
            "story_50",
        ),
        (
            0.75,
            "story_75",
        ),
        (
            0.94,
            "payoff_94",
        ),
    ]:
        timestamps.append(
            (
                max(
                    0.2,
                    usable_end
                    *
                    fraction,
                ),
                name,
            )
        )

    frames = []

    for timestamp, name in timestamps:
        timestamp = min(
            timestamp,
            max(
                0.2,
                seconds - 0.3,
            ),
        )

        try:
            path = extract_frame(
                timestamp,
                name,
            )
        except Exception:
            path = None

        if path:
            frames.append(
                (
                    name,
                    path,
                )
            )

    return frames


def transcribe(client):
    with AUDIO.open(
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


def data_url(path):
    encoded = base64.b64encode(
        path.read_bytes()
    ).decode("ascii")

    return (
        f"data:image/jpeg;base64,"
        f"{encoded}"
    )


def score(
    client,
    transcript,
    frames,
    metadata,
):
    prompt = f"""
You are the FINAL V12.14 viral-quality gate for ViralSpawnTV.

You are judging the ACTUAL FINISHED SHORT after production has added:
- its Big Hook
- on-screen hook text
- narration
- captions
- pacing/emphasis
- branding

This is the correct place to enforce the viral opening standard.

Publish thresholds:
- overall score >= {MIN_SCORE}
- finished hook >= {MIN_HOOK}
- payoff >= {MIN_PAYOFF}

Judge the actual finished viewer experience.

HOOK:
The first 1-2 seconds should immediately give a new viewer a truthful
reason to keep watching. Evaluate the rendered visuals, headline and spoken
opening together.

STORY:
The Short should keep progressing rather than spending most of its runtime
on filler.

PAYOFF:
The ending should deliver the result/reaction/reveal promised by the hook.

CLARITY:
A viewer unfamiliar with the streamer should still understand the basic
situation.

Return ONLY JSON:
{{
  "score": 0-100,
  "hook": 0-100,
  "story_sustain": 0-100,
  "payoff": 0-100,
  "ending_strength": 0-100,
  "action": 0-100,
  "clarity": 0-100,
  "recommended": true or false,
  "reason": "concise evidence-based explanation"
}}

Set recommended=true only if you would publish THIS FINISHED SHORT at the
current ViralSpawnTV quality target.

GENERATED METADATA:
headline={metadata.get("headline", "")}
title={metadata.get("title", "")}
hook_strength_from_editor={metadata.get("hook_strength", 0)}
hook_type={metadata.get("hook_type", "")}
hook_reason={metadata.get("hook_reason", "")}

FINISHED AUDIO TRANSCRIPT:
{transcript[:14000]}
"""

    content = [
        {
            "type":
                "input_text",
            "text":
                prompt,
        }
    ]

    for name, path in frames:
        content.append(
            {
                "type":
                    "input_text",
                "text":
                    f"FRAME LABEL: {name}",
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

    raw = response.output_text.strip()

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


def main():
    if not VIDEO.exists():
        raise RuntimeError(
            f"Missing finished Short: {VIDEO}"
        )

    metadata = {}

    if METADATA.exists():
        metadata = json.loads(
            METADATA.read_text(
                encoding="utf-8"
            )
        )

    client = OpenAI()

    seconds = probe_duration()

    extract_audio()

    frames = extract_frames(
        seconds
    )

    transcript = transcribe(
        client
    )

    scored = score(
        client,
        transcript,
        frames,
        metadata,
    )

    numeric_score = int(
        scored.get(
            "score",
            0,
        )
    )

    hook = int(
        scored.get(
            "hook",
            0,
        )
    )

    payoff = int(
        scored.get(
            "payoff",
            0,
        )
    )

    recommended = bool(
        scored.get(
            "recommended",
            False,
        )
    )

    passed = bool(
        recommended
        and
        numeric_score >= MIN_SCORE
        and
        hook >= MIN_HOOK
        and
        payoff >= MIN_PAYOFF
    )

    result = {
        "passed":
            passed,
        "minimum_score":
            MIN_SCORE,
        "minimum_hook":
            MIN_HOOK,
        "minimum_payoff":
            MIN_PAYOFF,
        "duration_seconds":
            round(
                seconds,
                3,
            ),
        "headline":
            metadata.get(
                "headline"
            ),
        **scored,
    }

    RESULT.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )

    if not passed:
        print(
            "V12.14 FINISHED VIRAL GATE: REJECTED"
        )
        sys.exit(23)

    print(
        "V12.14 FINISHED VIRAL GATE: PASSED"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"V12.14 FINISHED VIRAL GATE ERROR: "
            f"{exc}"
        )
        sys.exit(1)
