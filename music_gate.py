import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI


SOURCE = Path("work/kick_gaming/selected_kick_gaming_source.mp4")
OUT_DIR = Path("work/music_gate")
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUDIO = OUT_DIR / "source_audio.wav"
RESULT = OUT_DIR / "music_gate_result.json"

# Conservative markers frequently emitted by transcription systems when
# music dominates or is clearly audible.
MUSIC_MARKERS = [
    "[music]", "(music)", "♪", "♫",
    "music playing", "song playing",
    "singing", "[singing]", "(singing)",
]


def extract_audio():
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(SOURCE),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(AUDIO),
        ],
        check=True,
    )


def transcribe(client):
    with AUDIO.open("rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
        )

    if hasattr(response, "model_dump"):
        data = response.model_dump()
    elif isinstance(response, dict):
        data = response
    else:
        data = {"text": str(response)}

    return data


def ai_screen(client, transcript):
    prompt = f"""
You are a conservative pre-publication audio-risk screener for a gaming
YouTube Shorts channel.

We are trying to reject source clips when the transcript provides evidence
of recognizable/commercial music, a song, rapping/singing along to a song,
or music lyrics that could create a YouTube music Content ID risk.

Do NOT flag ordinary game sound effects, streamer speech, crowd noise,
weapon sounds, menu sounds, or generic instrumental game ambience unless
there is evidence it is an identifiable song/music track.

Because you only have a transcript, never claim certainty you do not have.
If there is clear evidence of music, return reject=true.
If the transcript is ordinary gaming/streamer speech with no music evidence,
return reject=false.

Return ONLY JSON:
{{
  "reject": true or false,
  "confidence": 0.0 to 1.0,
  "reason": "short reason"
}}

TRANSCRIPT:
{transcript[:12000]}
"""

    response = client.responses.create(
        model="gpt-5.6",
        input=prompt,
    )

    raw = response.output_text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    return json.loads(raw)


def main():
    if not SOURCE.exists():
        raise RuntimeError(f"Missing source video: {SOURCE}")

    client = OpenAI()
    extract_audio()
    transcription = transcribe(client)

    transcript = transcription.get("text", "") or ""
    lower = transcript.lower()

    marker_hits = [
        marker for marker in MUSIC_MARKERS
        if marker.lower() in lower
    ]

    ai = ai_screen(client, transcript)

    reject = bool(marker_hits) or bool(ai.get("reject", False))

    result = {
        "passed": not reject,
        "reject": reject,
        "marker_hits": marker_hits,
        "ai_reject": bool(ai.get("reject", False)),
        "ai_confidence": ai.get("confidence"),
        "reason": ai.get("reason", ""),
        "screening_method": (
            "conservative transcript-based pre-publication music screen"
        ),
        "important_limitation": (
            "This is not an audio-fingerprint/Content-ID database lookup "
            "and cannot guarantee detection of every commercial recording."
        ),
    }

    RESULT.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if reject:
        print("MUSIC GATE: REJECTED. Public upload blocked.")
        sys.exit(20)

    print("MUSIC GATE: PASSED.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"MUSIC GATE ERROR: {exc}")
        sys.exit(1)
