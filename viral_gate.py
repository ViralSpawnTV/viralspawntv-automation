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

MIN_SCORE = 70


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
    # Five evenly distributed snapshots without needing exact duration math.
    pattern = str(WORK / "frame_%02d.jpg")
    run([
        "ffmpeg", "-y",
        "-i", str(SOURCE),
        "-vf", "fps=1/7,scale=640:-2",
        "-frames:v", "6",
        pattern,
    ])
    return sorted(WORK.glob("frame_*.jpg"))[:6]


def transcribe(client):
    with AUDIO.open("rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text",
        )
    return str(response).strip()


def data_url(path):
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def score(client, transcript, frames, acquisition):
    prompt = f"""
You are selecting source material for ViralSpawnTV, an English-language
gaming Shorts channel.

Score this source clip BEFORE expensive production.

We want clips with:
- an immediate or understandable hook
- visible gameplay/action or a strong streamer reaction
- a mini-story with setup, escalation and payoff
- clutch, fail, rage, surprise, comedy, danger, challenge or impressive play
- enough context to turn into a compelling 15-45 second Short
- moments that can support original English commentary

Penalize:
- dead air, menus, waiting, low-action conversation
- confusing clips with no understandable payoff
- clips where the interesting moment cannot be inferred
- non-gaming content
- gambling/casino content
- clips dominated by music rather than gaming/story

Do not require English source speech. ViralSpawnTV translates foreign speech.

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

SOURCE CHANNEL: {acquisition.get("channel", "")}
TRANSCRIPT:
{transcript[:9000]}
"""

    content = [{"type": "input_text", "text": prompt}]

    for frame in frames:
        content.append({
            "type": "input_image",
            "image_url": data_url(frame),
        })

    response = client.responses.create(
        model="gpt-5.6",
        input=[{"role": "user", "content": content}],
    )

    raw = response.output_text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def main():
    if not SOURCE.exists():
        raise RuntimeError(f"Missing source: {SOURCE}")

    acquisition = json.loads(
        ACQUISITION.read_text(encoding="utf-8")
    )

    client = OpenAI()
    extract_audio()
    frames = extract_frames()
    transcript = transcribe(client)
    scored = score(client, transcript, frames, acquisition)

    numeric_score = int(scored.get("score", 0))
    recommended = bool(scored.get("recommended", False))
    passed = recommended and numeric_score >= MIN_SCORE

    result = {
        "passed": passed,
        "minimum_score": MIN_SCORE,
        "clip_id": acquisition.get("clip_id"),
        "channel": acquisition.get("channel"),
        **scored,
    }

    RESULT.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if not passed:
        print("VIRAL QUALITY GATE: REJECTED")
        sys.exit(22)

    print("VIRAL QUALITY GATE: PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"VIRAL QUALITY GATE ERROR: {exc}")
        sys.exit(1)
