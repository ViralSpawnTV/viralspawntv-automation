import json
import re
import sys
from pathlib import Path

from openai import OpenAI


WORK = Path("work/production")
PLAN_PATH = WORK / "v4_edit_plan.json"
TRANSCRIPT_PATH = WORK / "timestamped_transcript.json"
METADATA_PATH = WORK / "ViralSpawnTV_V4_metadata.json"
RESULT_PATH = WORK / "final_content_gate.json"

BLOCKED_TERMS = [
    "casino",
    "gambling",
    "sports betting",
    "sportsbook",
    "roulette",
    "blackjack",
    "slot machine",
    "slots",
    "betting",
]


def load_json(path):
    if not path.exists():
        raise RuntimeError(f"Missing required file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def segment_text(transcript, start, end):
    pieces = []

    # production_test.py currently writes timestamped_transcript.json
    # as a LIST of segment dictionaries. Also support dict-shaped
    # transcripts so this gate remains compatible with either format.
    if isinstance(transcript, list):
        segments = transcript
        fallback_text = " ".join(
            str(seg.get("text", "")).strip()
            for seg in transcript
            if isinstance(seg, dict)
            and str(seg.get("text", "")).strip()
        )

    elif isinstance(transcript, dict):
        segments = transcript.get("segments", [])

        if not isinstance(segments, list):
            segments = []

        fallback_text = str(
            transcript.get("text", "")
        ).strip()

    else:
        segments = []
        fallback_text = ""

    for seg in segments:
        if not isinstance(seg, dict):
            continue

        try:
            s = float(seg.get("start", 0))
            e = float(seg.get("end", 0))
        except Exception:
            continue

        if e <= start or s >= end:
            continue

        text = str(seg.get("text", "")).strip()

        if text:
            pieces.append(text)

    if pieces:
        return " ".join(pieces)

    return fallback_text


def normalize(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def ai_check(client, selected_text, metadata):
    prompt = f"""
You are the final publication gate for an English-language gaming Shorts channel.

Evaluate ONLY the material that remains in the FINAL SELECTED SOURCE SEGMENT,
plus the generated title, description, commentary and captions supplied below.

Block the Short if the final selected material meaningfully contains or promotes:
- casino gambling
- slot machines
- roulette or blackjack gambling
- sportsbook/sports betting
- wagering/betting as the subject of the clip

Do NOT block merely because a word has an unrelated meaning.
Do NOT evaluate dialogue outside the selected segment.
Ordinary gaming competition, loot, points, kills, wins, losses, or in-game
currency are not automatically gambling.

Return ONLY JSON:
{{
  "pass": true or false,
  "confidence": 0.0 to 1.0,
  "reason": "short explanation"
}}

SELECTED SOURCE DIALOGUE:
{selected_text[:10000]}

FINAL METADATA:
{json.dumps(metadata, ensure_ascii=False)[:10000]}
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
    plan = load_json(PLAN_PATH)
    transcript = load_json(TRANSCRIPT_PATH)
    metadata = load_json(METADATA_PATH)

    start = float(plan.get("segment_start", 0))
    end = float(plan.get("segment_end", 0))

    if end <= start:
        raise RuntimeError("Invalid selected segment timestamps.")

    selected = segment_text(transcript, start, end)
    selected_norm = normalize(selected)

    term_hits = [
        term for term in BLOCKED_TERMS
        if term in selected_norm
    ]

    client = OpenAI()
    ai = ai_check(client, selected, metadata)

    passed = (not term_hits) and bool(ai.get("pass", False))

    result = {
        "passed": passed,
        "selected_segment_start": start,
        "selected_segment_end": end,
        "blocked_term_hits": term_hits,
        "ai_pass": bool(ai.get("pass", False)),
        "ai_confidence": ai.get("confidence"),
        "reason": ai.get("reason", ""),
        "selected_dialogue_checked": selected,
    }

    RESULT_PATH.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if not passed:
        print("FINAL CONTENT GATE: REJECTED. Public upload blocked.")
        sys.exit(21)

    print("FINAL CONTENT GATE: PASSED.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FINAL CONTENT GATE ERROR: {exc}")
        sys.exit(1)
