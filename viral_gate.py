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

# V5.5 quality standard:
# Only clips scoring 80+ are allowed into Shorts production.
MIN_SCORE = 80


def run(command):
    subprocess.run(command, check=True)


def extract_audio():
    run([
        "ffmpeg", "-y",
        "-i", str(SOURCE),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(AUDIO),
    ])


def extract_frames():
    # Pull representative snapshots across the clip for visual scoring.
    pattern = str(WORK / "frame_%02d.jpg")

    run([
        "ffmpeg", "-y",
        "-i", str(SOURCE),
        "-vf", "fps=1/7,scale=640:-2",
        "-frames:v", "6",
        pattern,
    ])

    return sorted(
        WORK.glob("frame_*.jpg")
    )[:6]


def transcribe(client):
    with AUDIO.open("rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text",
        )

    return str(response).strip()


def data_url(path):
    encoded = base64.b64encode(
        path.read_bytes()
    ).decode("ascii")

    return (
        f"data:image/jpeg;base64,{encoded}"
    )


def score(
    client,
    transcript,
    frames,
    acquisition
):
    prompt = f"""
You are the viral-quality gate for ViralSpawnTV,
an English-language gaming Shorts channel.

Your job is to reject average clips.

Only clips with strong short-form potential should score 80 or higher.

Score this source clip BEFORE expensive production.

============================================================
VIRALSPAWNTV QUALITY STANDARD
============================================================

We strongly prefer clips with:

- an immediate hook or an obvious opportunity for a strong opening hook
- something interesting happening within the first few seconds of the
  usable moment
- visible gameplay/action or a strong streamer reaction
- clear stakes, tension, danger, challenge, surprise, humor, or skill
- a mini-story with setup, escalation, and payoff/reaction
- clutch plays, fails, rage, surprises, comedy, challenges, close calls,
  impressive plays, unusual strategy, or unexpected outcomes
- enough context to turn into a compelling 15-45 second Short
- moments that support original English ViralSpawnTV commentary
- a payoff worth staying to watch

============================================================
OPENING-HOOK STANDARD
============================================================

The strongest ViralSpawnTV Shorts should give the viewer a reason
to keep watching within roughly the first 1-2 seconds.

Reward clips where the editor can begin on:

- immediate danger
- an impossible-looking situation
- a risky decision
- a strange or unexpected visual
- obvious tension
- a funny setup
- a challenge already in progress
- an impressive play already developing
- a strong streamer reaction

Penalize clips that require too much explanation before becoming interesting.

============================================================
PENALIZE
============================================================

Penalize heavily for:

- dead air
- menus
- waiting
- long walking/travel sections
- greetings or introductions
- low-action conversation
- slow setup with no immediate intrigue
- confusing clips with no understandable payoff
- clips where the interesting moment cannot be inferred
- clips with weak or ordinary outcomes
- non-gaming content
- gambling/casino content
- clips dominated by commercial music rather than gaming/story
- moments that would still feel average after editing

============================================================
SCORING GUIDE
============================================================

90-100:
Exceptional viral potential.
Immediate hook, strong stakes, compelling escalation,
and a memorable payoff.

80-89:
Strong publishable ViralSpawnTV candidate.
Clearly above-average hook and payoff potential.

70-79:
Decent gaming clip but not strong enough for the current
ViralSpawnTV quality standard.

Below 70:
Weak, ordinary, confusing, slow, or unsuitable.

An 80 should NOT be easy to earn.

Do not inflate scores just because the clip contains gameplay.

Do not require English source speech.
ViralSpawnTV can translate foreign dialogue.

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

"recommended" should normally be true only when the overall score is
80 or higher AND the clip has a realistic path to a compelling Short.

SOURCE CHANNEL:
{acquisition.get("channel", "")}

TRANSCRIPT:
{transcript[:9000]}
"""

    content = [{
        "type": "input_text",
        "text": prompt
    }]

    for frame in frames:
        content.append({
            "type": "input_image",
            "image_url": data_url(frame),
        })

    response = client.responses.create(
        model="gpt-5.6",
        input=[{
            "role": "user",
            "content": content
        }],
    )

    raw = response.output_text.strip()

    raw = re.sub(
        r"^```json\s*",
        "",
        raw
    )

    raw = re.sub(
        r"\s*```$",
        "",
        raw
    )

    return json.loads(raw)


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
        acquisition
    )

    numeric_score = int(
        scored.get(
            "score",
            0
        )
    )

    recommended = bool(
        scored.get(
            "recommended",
            False
        )
    )

    passed = (
        recommended
        and
        numeric_score >= MIN_SCORE
    )

    result = {
        "passed": passed,
        "minimum_score": MIN_SCORE,
        "clip_id": acquisition.get(
            "clip_id"
        ),
        "channel": acquisition.get(
            "channel"
        ),
        **scored,
    }

    RESULT.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        )
    )

    if not passed:
        print(
            "VIRAL QUALITY GATE: REJECTED"
        )
        sys.exit(22)

    print(
        "VIRAL QUALITY GATE: PASSED"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"VIRAL QUALITY GATE ERROR: {exc}"
        )
        sys.exit(1)
