import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI


SOURCE = Path("work/kick_gaming/selected_kick_gaming_source.mp4")
ACQUISITION = Path("work/kick_gaming/acquisition_result.json")
WORK = Path("work/source_quality_gate")
WORK.mkdir(parents=True, exist_ok=True)

AUDIO = WORK / "audio.wav"
RESULT = WORK / "source_quality_gate_result.json"

MIN_SOURCE_SCORE = 65
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
    for old in WORK.glob("frame_*.jpg"):
        old.unlink()
    probe = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                            "-of","json",str(SOURCE)], check=True,capture_output=True,text=True)
    seconds = float(json.loads(probe.stdout)["format"]["duration"])
    if seconds < 19:
        raise RuntimeError("Source window is below the 19-second core minimum")
    paths = []
    for i,fraction in enumerate((0.04,0.15,0.27,0.39,0.51,0.63,0.75,0.87,0.97)):
        path = WORK/f"frame_{i:02d}.jpg"
        run(["ffmpeg","-y","-v","error","-ss",f"{seconds*fraction:.3f}",
             "-i",str(SOURCE),"-frames:v","1","-vf","scale=640:-2","-q:v","4",str(path)])
        if not path.exists() or path.stat().st_size < 1000:
            raise RuntimeError("Incomplete source-quality visual evidence")
        paths.append(path)
    return paths


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
You are the V12.14 SOURCE QUALITY GATE for ViralSpawnTV.

This is NOT the final viral-Short gate.

You are judging RAW gaming footage before ViralSpawnTV production creates:
- the Big Hook opening
- truthful narration
- on-screen headline
- captions
- pacing/emphasis
- branding

Therefore DO NOT reject a source merely because its raw first second is
slow or because it does not already look like a finished viral Short.

Question:
DOES THIS RAW 19-58 SECOND GAMING WINDOW CONTAIN ENOUGH QUALITY MATERIAL
FOR OUR EDITOR TO MAKE A STRONG SHORT?

Score:
- story_sustain
- payoff
- ending_strength
- action
- clarity
- editability
- source_score overall

A worthwhile source normally has:
- real progression
- a meaningful result/reaction/payoff
- enough context to explain the stakes truthfully
- enough action/change to avoid 40 seconds of filler

Penalize:
- long routine travel
- menus/inventory/loadouts
- unresolved footage
- repetitive low-stakes play
- no meaningful result
- source where a strong hook would require inventing facts

Hard reject:
- gambling/casino
- clearly non-gaming
- commercial-music-dominated content

Return ONLY JSON:
{{
  "source_score": 0-100,
  "story_sustain": 0-100,
  "payoff": 0-100,
  "ending_strength": 0-100,
  "action": 0-100,
  "clarity": 0-100,
  "editability": 0-100,
  "recommended": true or false,
  "reason": "concise evidence-based explanation",
  "moment_type": "clutch/fail/rage/funny/reaction/challenge/surprise/other"
}}

Set recommended=true when this is genuinely worth sending to production.
The raw opening itself does NOT need to be viral.

PRESCREEN:
source={acquisition.get("prescreen_predicted_score")}
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

    source_score = int(
        scored.get(
            "source_score",
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
        source_score >= MIN_SOURCE_SCORE
        and
        payoff >= MIN_PAYOFF
    )

    result = {
        "passed":
            passed,
        "minimum_source_score":
            MIN_SOURCE_SCORE,
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
            "V12.14 SOURCE QUALITY GATE: REJECTED"
        )
        sys.exit(22)

    print(
        "V12.14 SOURCE QUALITY GATE: PASSED"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"V12.14 SOURCE QUALITY GATE ERROR: "
            f"{exc}"
        )
        sys.exit(1)
