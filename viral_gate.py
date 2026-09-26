import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI


SOURCE = Path("work/kick_gaming/selected_kick_gaming_source.mp4")
ACQUISITION = Path("work/kick_gaming/acquisition_result.json")
WORK = Path("work/viral_gate")
WORK.mkdir(parents=True, exist_ok=True)

AUDIO = WORK / "audio.wav"
RESULT = WORK / "viral_gate_result.json"

MIN_SCORE = 72


def run(command):
    subprocess.run(
        command,
        check=True,
    )


def extract_audio():
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(SOURCE),
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


def extract_frames():
    pattern = str(
        WORK
        /
        "frame_%02d.jpg"
    )

    for old in WORK.glob(
        "frame_*.jpg"
    ):
        old.unlink()

    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(SOURCE),
            "-vf",
            "fps=1/7,scale=640:-2",
            "-frames:v",
            "6",
            pattern,
        ]
    )

    return sorted(
        WORK.glob(
            "frame_*.jpg"
        )
    )[:6]


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
    ).decode(
        "ascii"
    )

    return (
        f"data:image/jpeg;base64,"
        f"{encoded}"
    )


def score(
    client,
    transcript,
    frames,
    acquisition,
):
    prompt = f"""
You are the FINAL viral-quality gate for ViralSpawnTV,
an English-language gaming Shorts channel.

The CURRENT publish threshold is {MIN_SCORE}/100.

A score of {MIN_SCORE} or higher means the clip can be recommended when
there is a realistic path to a compelling 15-45 second Short.

Do not use an old 80-point recommendation threshold.
Judge against the CURRENT {MIN_SCORE}-point standard.

============================================================
VIRALSPAWNTV QUALITY STANDARD
============================================================

Strong clips usually have:
- an immediate hook or obvious strong-hook opportunity
- interesting action/reaction in the first few usable seconds
- clear stakes, danger, challenge, surprise, humor, or skill
- a mini-story with escalation and payoff/reaction
- enough context for a compelling 15-45 second Short
- a payoff worth staying to watch

============================================================
OPENING-HOOK STANDARD
============================================================

Reward:
- immediate danger
- impossible-looking situations
- risky decisions
- strange/unexpected visuals
- tension
- funny setups
- challenges in progress
- impressive plays developing
- strong streamer reactions

Penalize:
- dead air
- menus/lobbies
- waiting
- greetings
- low-action conversation
- long context requirements
- confusing footage
- weak/ordinary outcomes
- non-gaming
- gambling/casino content
- commercial-music-dominated footage

============================================================
SCORING GUIDE
============================================================

90-100:
Exceptional viral potential.

80-89:
Very strong candidate.

72-79:
Good publishable candidate when the hook/story/payoff are clear enough.

60-71:
Some potential, but not strong enough for the current publish threshold.

Below 60:
Weak, ordinary, confusing, slow, or unsuitable.

Do not inflate scores simply because gameplay exists.

Return ONLY JSON:

{{
  "score": 0-100,
  "hook": 0-100,
  "payoff": 0-100,
  "action": 0-100,
  "clarity": 0-100,
  "recommended": true or false,
  "reason": "one short explanation",
  "moment_type": "clutch/fail/rage/funny/reaction/challenge/surprise/other"
}}

IMPORTANT:
Set "recommended" to true when the overall score is {MIN_SCORE} or higher
AND the footage has a realistic path to a compelling ViralSpawnTV Short.

SOURCE CHANNEL:
{acquisition.get("channel", "")}

PRESCREEN PREDICTION:
score={acquisition.get("prescreen_predicted_score")}
probability_72_plus={acquisition.get("prescreen_probability_72_plus")}
reason={acquisition.get("prescreen_reason", "")}

TRANSCRIPT:
{transcript[:9000]}
"""

    content = [
        {
            "type":
                "input_text",
            "text":
                prompt,
        }
    ]

    for frame in frames:
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
    if not SOURCE.exists():
        raise RuntimeError(
            f"Missing source: {SOURCE}"
        )

    acquisition = json.loads(
        ACQUISITION.read_text(
            encoding="utf-8"
        )
    )

    client = OpenAI()

    extract_audio()

    frames = extract_frames()

    transcript = transcribe(
        client
    )

    scored = score(
        client,
        transcript,
        frames,
        acquisition,
    )

    numeric_score = int(
        scored.get(
            "score",
            0,
        )
    )

    recommended = bool(
        scored.get(
            "recommended",
            False,
        )
    )

    passed = (
        recommended
        and
        numeric_score >= MIN_SCORE
    )

    result = {
        "passed":
            passed,
        "minimum_score":
            MIN_SCORE,
        "clip_id":
            acquisition.get(
                "clip_id"
            ),
        "channel":
            acquisition.get(
                "channel"
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
            "VIRAL QUALITY GATE: REJECTED"
        )
        sys.exit(
            22
        )

    print(
        "VIRAL QUALITY GATE: PASSED"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"VIRAL QUALITY GATE ERROR: "
            f"{exc}"
        )
        sys.exit(
            1
        )
