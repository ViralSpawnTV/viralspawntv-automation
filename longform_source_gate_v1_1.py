import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI


ROOT = Path("work/longform")
META = ROOT / "acquired_sources.json"
OUT = ROOT / "screened_sources.json"

# Permanent long-form source rejection history.
REJECTED_HISTORY = Path("longform_rejected_history.json")

VERSION = "1.5.1-english-only-expanded-pool"

# V1.5.1 requires a healthy approved reserve before production.
# The prior minimum of 5 allowed production to start with far too little
# footage after language/music/content screening. With the expanded ranker
# and collector, require 25 approved sources before production begins.
MIN_PASSED_SOURCES = 25

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


def load_rejected_history():
    """
    Load permanent long-form rejection history.

    Preferred format:
    {
      "version": 1,
      "rejected_clips": [...]
    }

    A legacy list is accepted for compatibility.
    """

    if not REJECTED_HISTORY.exists():
        return {
            "version": 1,
            "rejected_clips": [],
        }

    try:
        data = json.loads(
            REJECTED_HISTORY.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {
            "version": 1,
            "rejected_clips": [],
        }

    if isinstance(data, list):
        return {
            "version": 1,
            "rejected_clips": data,
        }

    if not isinstance(data, dict):
        return {
            "version": 1,
            "rejected_clips": [],
        }

    data.setdefault("version", 1)
    data.setdefault("rejected_clips", [])

    if not isinstance(
        data.get("rejected_clips"),
        list,
    ):
        data["rejected_clips"] = []

    return data


def save_permanent_rejections(rows):
    """
    Merge source-gate content rejections into permanent history.

    This function is called before the minimum-pass-count check so
    valid rejections are retained even when the overall source gate
    later fails for having too few passing sources.
    """

    history = load_rejected_history()

    existing = history.get(
        "rejected_clips",
        [],
    )

    merged = []
    seen_ids = set()
    seen_urls = set()

    def add_item(item):
        if not isinstance(item, dict):
            return

        clip_id = str(
            item.get("clip_id")
            or ""
        ).strip()

        clip_url = str(
            item.get("clip_url")
            or item.get("source")
            or ""
        ).strip()

        if clip_id and clip_id in seen_ids:
            return

        if (
            not clip_id
            and clip_url
            and clip_url in seen_urls
        ):
            return

        if clip_id:
            seen_ids.add(clip_id)

        if clip_url:
            seen_urls.add(clip_url)

        merged.append(item)

    for item in existing:
        if isinstance(item, str):
            item = {
                "clip_id": item,
                "reason": "legacy_rejection",
            }

        add_item(item)

    for row in rows:
        verdict = row.get(
            "source_gate",
            {},
        )

        reason = str(
            verdict.get(
                "reason",
                "longform_source_gate_reject",
            )
        ).strip()

        add_item({
            "clip_id": row.get("clip_id"),
            "clip_url": (
                row.get("clip_url")
                or row.get("source")
            ),
            "game": row.get("game"),
            "channel": row.get("channel"),
            "reason": reason,
            "rejection_stage": "source_gate",
            "music_risk": bool(
                verdict.get("music_risk")
            ),
            "gambling_risk": bool(
                verdict.get("gambling_risk")
            ),
            "language_risk": bool(
                verdict.get("language_risk")
            ),
            "detected_language": row.get(
                "detected_language"
            ),
        })

    REJECTED_HISTORY.write_text(
        json.dumps(
            {
                "version": 1,
                "rejected_clips": merged,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "\nPermanent long-form rejection history: "
        f"{len(merged)} clips."
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

    # Persist deterministic source/content/language rejections before
    # checking whether enough clips survived. This means a failed overall
    # run still remembers clips that were conclusively rejected.
    save_permanent_rejections(
        rejected
    )

    if len(passed) < MIN_PASSED_SOURCES:
        raise RuntimeError(
            f"Only {len(passed)} sources "
            "passed English-language source screening. "
            f"V1.5.1 requires at least "
            f"{MIN_PASSED_SOURCES} approved sources "
            "before production."
        )

    OUT.write_text(
        json.dumps(
            {
                "version": VERSION,
                "language_policy":
                    "English speech or no meaningful speech",
                "source_count":
                    len(sources),
                "minimum_passed_sources":
                    MIN_PASSED_SOURCES,
                "passed_count":
                    len(passed),
                "rejected_count":
                    len(rejected),
                "rejected_history_file":
                    str(REJECTED_HISTORY),
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
