import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI


ROOT = Path("work/longform")
META = ROOT / "acquired_sources.json"
OUT = ROOT / "screened_sources.json"

VERSION = "1.2-english-only"

BLOCKED = [
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

# We do not want tiny Whisper fragments such as "oh", "wow",
# player names, or game callouts to falsely reject a clip.
#
# If Whisper detects a non-English language but there is not enough
# actual speech to make the result meaningful, the clip remains
# eligible and the AI gate performs a second contextual check.
MIN_LANGUAGE_WORDS = 4
MIN_LANGUAGE_CHARACTERS = 15


def run(cmd):
    return subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def normalize_language(language):
    language = str(
        language or ""
    ).strip().lower()

    aliases = {
        "en": "english",
        "eng": "english",
        "english": "english",
    }

    return aliases.get(
        language,
        language,
    )


def meaningful_speech(text):
    """
    Decide whether there is enough transcript to make language
    detection meaningful.

    Gameplay-only clips and clips with only tiny vocal fragments
    should not be rejected merely because Whisper guessed a
    non-English language.
    """

    cleaned = re.sub(
        r"\s+",
        " ",
        str(text or ""),
    ).strip()

    words = re.findall(
        r"\b[\w'-]+\b",
        cleaned,
        flags=re.UNICODE,
    )

    alphanumeric = re.sub(
        r"[^\w]+",
        "",
        cleaned,
        flags=re.UNICODE,
    )

    return (
        len(words) >= MIN_LANGUAGE_WORDS
        and len(alphanumeric)
        >= MIN_LANGUAGE_CHARACTERS
    )


def main():
    client = OpenAI()

    data = json.loads(
        META.read_text(
            encoding="utf-8"
        )
    )

    passed = []
    rejected = []

    sources = data.get(
        "sources",
        [],
    )

    print(
        f"Long-form English source gate: "
        f"{len(sources)} acquired sources."
    )

    for i, s in enumerate(
        sources,
        1,
    ):
        src = Path(
            s["local_path"]
        )

        wav = ROOT / (
            f"gate_{i:02d}.wav"
        )

        try:
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(src),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(wav),
                ]
            )

            with open(
                wav,
                "rb",
            ) as f:
                tr = (
                    client.audio.transcriptions.create(
                        model="whisper-1",
                        file=f,
                        response_format="verbose_json",
                    )
                )

        finally:
            wav.unlink(
                missing_ok=True
            )

        text = (
            getattr(
                tr,
                "text",
                "",
            )
            or ""
        ).strip()

        detected_language = (
            normalize_language(
                getattr(
                    tr,
                    "language",
                    "",
                )
            )
        )

        has_meaningful_speech = (
            meaningful_speech(text)
        )

        lower = text.lower()

        hard = [
            x
            for x in BLOCKED
            if x in lower
        ]

        # -----------------------------------------------------
        # ENGLISH-ONLY LONG-FORM LANGUAGE GATE
        # -----------------------------------------------------

        language_status = "unknown"

        if not has_meaningful_speech:
            # Gameplay-only clips or clips containing only tiny
            # speech fragments remain eligible.
            language_status = (
                "no_meaningful_speech"
            )

        elif detected_language == "english":
            language_status = "english"

        elif detected_language:
            language_status = (
                "non_english"
            )

        else:
            language_status = (
                "unknown_meaningful_speech"
            )

        # Clearly non-English clips with meaningful speech are
        # rejected immediately before spending another GPT call.
        if (
            language_status
            == "non_english"
        ):
            verdict = {
                "pass": False,
                "music_risk": False,
                "gambling_risk": False,
                "language_risk": True,
                "detected_language":
                    detected_language,
                "confidence": 1.0,
                "reason": (
                    "Meaningful speech was detected "
                    f"in {detected_language or 'a non-English language'}."
                ),
            }

            row = {
                **s,
                "gate_transcript": text,
                "detected_language":
                    detected_language,
                "language_status":
                    language_status,
                "meaningful_speech":
                    has_meaningful_speech,
                "source_gate":
                    verdict,
            }

            rejected.append(row)

            print(
                f"REJECT {i}: "
                f"{s.get('game')} / "
                f"{s.get('channel')} / "
                f"non-English speech "
                f"({detected_language})"
            )

            continue

        # -----------------------------------------------------
        # EXISTING MUSIC / GAMBLING GATE
        # +
        # SECONDARY LANGUAGE SAFETY CHECK
        # -----------------------------------------------------

        prompt = f"""
Screen this gaming clip before commercial YouTube production
for ViralSpawnTV.

The finished long-form episode should feel like a cohesive
English-language gaming compilation.

Reject if there is meaningful evidence of:

1. recognizable/commercial music, a song, singing, rapping,
   or lyrics;

2. gambling/casino/sports-betting promotion or substantive
   gambling content;

3. meaningful spoken dialogue that is clearly primarily
   NON-ENGLISH.

LANGUAGE RULES:

- Normal English streamer speech is allowed.
- English gaming callouts are allowed.
- Accented English is allowed.
- A few isolated foreign words or names do NOT require rejection.
- Gameplay with no meaningful speech is allowed.
- Game sound effects, character noises, crowd noise and generic
  ambience are allowed.
- If the transcript is extremely short or unclear, do NOT invent
  a foreign-language problem.
- Reject when meaningful spoken dialogue is clearly primarily
  a language other than English.

Do NOT reject ordinary game sound effects, streamer speech,
crowd noise, or generic ambience when there is no evidence of
identifiable commercial music.

Whisper detected language:
{detected_language or "unknown"}

Meaningful speech detected:
{has_meaningful_speech}

Return ONLY JSON:

{{
  "pass": true,
  "music_risk": false,
  "gambling_risk": false,
  "language_risk": false,
  "primary_language": "english",
  "confidence": 0.95,
  "reason": "brief reason"
}}

GAME:
{s.get("game")}

TITLE:
{s.get("page_title")}

TRANSCRIPT:
{text[:5000]}
"""

        r = client.responses.create(
            model="gpt-5.6",
            input=prompt,
        )

        raw = re.sub(
            r"^```json\s*|\s*```$",
            "",
            r.output_text.strip(),
        )

        verdict = json.loads(raw)

        music_risk = bool(
            verdict.get(
                "music_risk"
            )
        )

        gambling_risk = bool(
            verdict.get(
                "gambling_risk"
            )
        )

        language_risk = bool(
            verdict.get(
                "language_risk"
            )
        )

        ai_pass = bool(
            verdict.get(
                "pass"
            )
        )

        ok = (
            ai_pass
            and not hard
            and not music_risk
            and not gambling_risk
            and not language_risk
        )

        row = {
            **s,
            "gate_transcript": text,
            "detected_language":
                detected_language,
            "language_status":
                language_status,
            "meaningful_speech":
                has_meaningful_speech,
            "source_gate":
                verdict,
        }

        if ok:
            passed.append(row)

            print(
                f"PASS {i}: "
                f"{s.get('game')} / "
                f"{s.get('channel')} / "
                f"language="
                f"{detected_language or 'unknown'}"
            )

        else:
            rejected.append(row)

            reasons = []

            if hard:
                reasons.append(
                    "blocked gambling term"
                )

            if music_risk:
                reasons.append(
                    "music risk"
                )

            if gambling_risk:
                reasons.append(
                    "gambling risk"
                )

            if language_risk:
                reasons.append(
                    "non-English speech"
                )

            if not reasons:
                reasons.append(
                    str(
                        verdict.get(
                            "reason",
                            "source gate rejected",
                        )
                    )
                )

            print(
                f"REJECT {i}: "
                f"{s.get('game')} / "
                f"{s.get('channel')} / "
                + "; ".join(reasons)
            )

    if len(passed) < 5:
        raise RuntimeError(
            f"Only {len(passed)} sources "
            "passed English-language "
            "source screening."
        )

    OUT.write_text(
        json.dumps(
            {
                "version": VERSION,
                "language_policy":
                    "English speech or no meaningful speech",
                "source_count":
                    len(sources),
                "passed_count":
                    len(passed),
                "rejected_count":
                    len(rejected),
                "passed_sources":
                    passed,
                "rejected_sources":
                    rejected,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "\nSource screening complete: "
        f"{len(passed)} pass, "
        f"{len(rejected)} reject."
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as e:
        print(
            "LONGFORM SOURCE GATE FAILED:",
            e,
        )

        sys.exit(1)
