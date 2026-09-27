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
MIN_HOOK = 65
MIN_PAYOFF = 60


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
            "-loglevel",
            "error",
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

    # Sample through the entire 50-60 second selected window.
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(SOURCE),
            "-vf",
            "fps=1/9,scale=640:-2",
            "-frames:v",
            "7",
            "-q:v",
            "4",
            pattern,
        ]
    )

    return sorted(
        WORK.glob(
            "frame_*.jpg"
        )
    )[:7]


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
    acquisition,
):
    prompt = f"""
You are the FINAL viral-quality gate for ViralSpawnTV,
an English-language gaming Shorts channel.

The local video is ALREADY the exact 40-58 second window selected by the
V12.11 two-phase prescreener. Judge THIS WHOLE WINDOW independently.

Publish threshold: {MIN_SCORE}/100.

The current ViralSpawnTV strategy is:
- final Short around 40-60 seconds
- strong first-second Big Hook
- enough story/escalation to sustain the longer Short
- clear payoff near the end

A clip with a strong beginning but 40 seconds of routine movement should
NOT pass.

A clip with good action but no understandable opening should NOT pass.

A clip with no satisfying result/reaction/payoff should NOT pass.

Hard reject:
- gambling / casino
- clearly non-gaming
- commercial-music-dominated content
- confusing or ordinary footage with no meaningful story

SCORING:
90-100 exceptional
80-89 very strong
72-79 publishable if hook + story + payoff are genuinely adequate
60-71 some potential but below current quality target
below 60 weak/ordinary/unsuitable

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
  "reason": "one concise evidence-based explanation",
  "moment_type": "clutch/fail/rage/funny/reaction/challenge/surprise/other"
}}

Set recommended=true only when the overall score is {MIN_SCORE}+ and the
window is genuinely worth publishing as a 40-60 second ViralSpawnTV Short.

SOURCE CHANNEL:
{acquisition.get("channel", "")}

PRESCREEN:
pred={acquisition.get("prescreen_predicted_score")}
P72={acquisition.get("prescreen_probability_72_plus")}
hook={acquisition.get("prescreen_hook")}
story={acquisition.get("prescreen_story_sustain")}
payoff={acquisition.get("prescreen_payoff")}
ending={acquisition.get("prescreen_ending_strength")}
reason={acquisition.get("prescreen_reason", "")}

TRANSCRIPT:
{transcript[:12000]}
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
        "clip_id":
            acquisition.get(
                "clip_id"
            ),
        "channel":
            acquisition.get(
                "channel"
            ),
        "selected_window_original":
            [
                acquisition.get(
                    "proposed_window_start_original"
                ),
                acquisition.get(
                    "proposed_window_end_original"
                ),
            ],
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
            "V12.11.2 VIRAL QUALITY GATE: REJECTED"
        )
        sys.exit(22)

    print(
        "V12.11.2 VIRAL QUALITY GATE: PASSED"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"V12.11.2 VIRAL QUALITY GATE ERROR: "
            f"{exc}"
        )
        sys.exit(1)
