import base64
import json
import math
import os
import pathlib
import re
import subprocess
import textwrap
from difflib import SequenceMatcher

from openai import OpenAI
from playwright.sync_api import sync_playwright


# ============================================================
# SETTINGS
# ============================================================

ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT / "work" / "production"
FRAMES = WORK / "frames"
VOICES = WORK / "voices"
TEXTS = WORK / "texts"

for folder in [WORK, FRAMES, VOICES, TEXTS]:
    folder.mkdir(parents=True, exist_ok=True)

CHANNEL = "unknown"
CREATOR = "Unknown Creator"

KICK_WORK = ROOT / "work" / "kick_gaming"
KICK_VIDEO = KICK_WORK / "selected_kick_gaming_source.mp4"
KICK_RESULT = KICK_WORK / "acquisition_result.json"

FINAL_VIDEO = WORK / "ViralSpawnTV_Short_V4.mp4"

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# V5.1 SHORTS BRAND BOOKENDS
INTRO_IMAGE = ROOT / "viralspawntv_intro.png"
OUTRO_IMAGE = ROOT / "viralspawntv_outro.png"
OUTRO_CTA = "FOLLOW FOR DAILY GAMING CLIPS"
NEON_FRAME = ROOT / "viralspawntv_neon_frame_overlay.png"
INTRO_SECONDS = 0.0
OUTRO_SECONDS = 1.0

# V5.9.2 DURATION TARGET
# Final Shorts may now run 40-60 seconds.
# Continue preferring longer 52-58 second cores when the story supports it,
# but allow shorter strong stories instead of padding them.
FINAL_MIN_SECONDS = 40.0
FINAL_MAX_SECONDS = 60.0
CORE_MIN_SECONDS = FINAL_MIN_SECONDS - OUTRO_SECONDS
CORE_MAX_SECONDS = 58.0
CORE_IDEAL_MIN_SECONDS = 52.0
CORE_IDEAL_MAX_SECONDS = 58.0

CORE_VIDEO = WORK / "ViralSpawnTV_Short_V4_core.mp4"
INTRO_VIDEO = WORK / "ViralSpawnTV_Short_V5_1_intro.mp4"
OUTRO_VIDEO = WORK / "ViralSpawnTV_Short_V5_1_outro.mp4"


# ============================================================
# BASIC HELPERS
# ============================================================

def run(command):

    print("\nRUNNING:")
    print(" ".join(str(x) for x in command))

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("\nSTDERR:")
        print(result.stderr[-15000:])
        raise RuntimeError("Command failed.")

    return result


def duration(path):

    result = run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    return float(result.stdout.strip())


def encode_image(path):

    return base64.b64encode(
        path.read_bytes()
    ).decode("utf-8")


def clean_text(text):

    text = str(text)

    text = text.replace("\n", " ")
    text = text.replace("\r", " ")
    text = text.replace("'", "")
    text = text.replace(":", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def make_text_file(name, text, width=None):

    text = clean_text(text)

    if width:
        text = "\n".join(
            textwrap.wrap(
                text,
                width=width,
                break_long_words=False,
            )
        )

    path = TEXTS / f"{name}.txt"

    path.write_text(
        text,
        encoding="utf-8"
    )

    return path


# ============================================================
# KICK GAMING INPUT
# ============================================================

def acquire_clip():

    global CHANNEL
    global CREATOR

    print("\n" + "=" * 65)
    print("LOADING ACQUIRED KICK GAMING CLIP")
    print("=" * 65)

    if not KICK_VIDEO.exists():
        raise RuntimeError(
            "Kick gaming source video was not found. "
            "Run kick_gaming_acquisition.py before production."
        )

    if not KICK_RESULT.exists():
        raise RuntimeError(
            "Kick acquisition_result.json was not found. "
            "Run kick_gaming_acquisition.py before production."
        )

    result = json.loads(
        KICK_RESULT.read_text(encoding="utf-8")
    )

    clip_url = str(
        result.get("clip_url", "")
    ).strip()

    if not clip_url:
        raise RuntimeError(
            "Kick acquisition result is missing clip_url."
        )

    CHANNEL = str(
        result.get("channel", "unknown")
    ).strip() or "unknown"

    CREATOR = str(
        result.get("creator")
        or CHANNEL
    ).strip() or CHANNEL

    rights_status = str(
        result.get("rights_status", "unverified")
    )

    public_publish_allowed = bool(
        result.get("public_publish_allowed", False)
    )

    clip_id_match = re.search(
        r"(clip_[A-Za-z0-9_-]+)",
        clip_url,
    )

    clip_id = (
        clip_id_match.group(1)
        if clip_id_match
        else "kick_gaming_clip"
    )

    print(f"Channel: @{CHANNEL}")
    print(f"Creator: {CREATOR}")
    print(f"Source: {clip_url}")
    print(f"Video: {KICK_VIDEO}")
    print(f"Rights status: {rights_status}")
    print(
        "Public publishing allowed: "
        f"{public_publish_allowed}"
    )

    return {
        "creator": CREATOR,
        "channel": CHANNEL,
        "clip_id": clip_id,
        "clip_url": clip_url,
        "video": KICK_VIDEO,
        "rights_status": rights_status,
        "creator_permission_verified": bool(
            result.get(
                "creator_permission_verified",
                False,
            )
        ),
        "game_rights_verified": bool(
            result.get(
                "game_rights_verified",
                False,
            )
        ),
        "public_publish_allowed":
            public_publish_allowed,
        "acquisition_context": str(
            result.get(
                "acquisition_context",
                "private_pipeline_test",
            )
        ),
    }


# ============================================================
# FRAME EXTRACTION
# ============================================================

def extract_frames(video):

    seconds = duration(video)

    # V5.9.4: keep broad story coverage, but deliberately reserve evidence
    # near the END of the source. Earlier versions could miss the actual
    # terminal result/ACE/ROUND WON/reaction because the last sampled frame
    # landed several seconds too early.
    timestamps = []

    t = 0.8

    while t < seconds:

        timestamps.append(
            round(
                t,
                3,
            )
        )

        t += 2.75

    for tail_offset in [
        7.0,
        5.0,
        3.0,
        1.5,
        0.6,
    ]:
        timestamp = max(
            0.2,
            seconds - tail_offset,
        )

        timestamps.append(
            round(
                timestamp,
                3,
            )
        )

    timestamps = sorted(
        {
            value
            for value in timestamps
            if (
                0.0
                <
                value
                <
                seconds
            )
        }
    )

    # Keep the request bounded but preserve the newest tail samples.
    if len(timestamps) > 24:
        head = timestamps[:19]
        tail = timestamps[-5:]

        timestamps = sorted(
            {
                *head,
                *tail,
            }
        )

    frames = []

    for i, timestamp in enumerate(
        timestamps
    ):

        path = (
            FRAMES /
            f"frame_{i:02d}.jpg"
        )

        run([
            "ffmpeg",
            "-y",

            "-ss",
            str(timestamp),

            "-i",
            str(video),

            "-frames:v",
            "1",

            "-vf",
            "scale=640:-2",

            "-q:v",
            "4",

            str(path),
        ])

        # FFmpeg can return success for a timestamp near the end of a
        # variable-frame-rate/stream-copied clip without actually writing
        # an output frame. Only pass real image files to the AI planner.
        if path.exists() and path.stat().st_size > 0:
            frames.append(
                (
                    timestamp,
                    path
                )
            )
        else:
            print(
                f"Skipping unavailable analysis frame at "
                f"{timestamp:.1f}s: {path.name}"
            )

    return seconds, frames


# ============================================================
# AUDIO EXTRACTION
# ============================================================

def extract_audio(video):

    audio = WORK / "source_audio.wav"

    run([
        "ffmpeg",
        "-y",

        "-i",
        str(video),

        "-vn",

        "-ac",
        "1",

        "-ar",
        "16000",

        "-c:a",
        "pcm_s16le",

        str(audio),
    ])

    return audio


# ============================================================
# TIMESTAMPED TRANSCRIPTION
# ============================================================

def transcribe_timestamped(
    client,
    audio
):

    print("\n" + "=" * 65)
    print("TIMESTAMPED TRANSCRIPTION")
    print("=" * 65)

    #
    # Use verbose_json so we can obtain real segment
    # timestamps rather than asking the editor AI to guess.
    #

    with open(audio, "rb") as file:

        response = (
            client.audio.transcriptions.create(
                model=os.getenv(
                    "TRANSCRIBE_MODEL",
                    "whisper-1"
                ),
                file=file,
                response_format="verbose_json",
                timestamp_granularities=[
                    "segment"
                ],
            )
        )

    transcript = response.text

    segments = []

    raw_segments = getattr(
        response,
        "segments",
        None
    )

    if raw_segments:

        for segment in raw_segments:

            if hasattr(
                segment,
                "model_dump"
            ):
                segment = (
                    segment.model_dump()
                )

            segments.append({
                "start": float(
                    segment["start"]
                ),
                "end": float(
                    segment["end"]
                ),
                "text": clean_text(
                    segment["text"]
                ),
            })

    if not segments:

        raise RuntimeError(
            "Timestamped transcription "
            "returned no segments."
        )

    (
        WORK /
        "transcript.txt"
    ).write_text(
        transcript,
        encoding="utf-8"
    )

    (
        WORK /
        "timestamped_transcript.json"
    ).write_text(
        json.dumps(
            segments,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        f"Transcript segments: "
        f"{len(segments)}"
    )

    return transcript, segments


# ============================================================
# V5.9 BIG HOOK REFINER
# ============================================================

def refine_big_hook(
    client,
    plan,
    segments,
    frames,
    segment_start,
    segment_end,
):
    """
    V5.9.3 factual Big Hook pass.

    The prior refiner could borrow facts from later in the clip and present
    them as if they were visible at frame 1. This version judges the opening
    using ONLY:
    - transcript from roughly the first 3 seconds of the selected segment
    - the first available analysis frames near that opening

    The result may frame an unresolved problem, but it may not import later
    HP values, enemy counts, scores, names, or outcomes into the opening.
    """

    opening_end = min(
        segment_end,
        segment_start + 3.0,
    )

    opening_lines = []

    for item in segments:
        try:
            start = float(item["start"])
            end = float(item["end"])
        except Exception:
            continue

        if end <= segment_start:
            continue

        if start >= opening_end:
            continue

        opening_lines.append(
            f"[{start:.2f}-{end:.2f}] "
            f"{clean_text(item.get('text', ''))}"
        )

    opening_transcript = "\n".join(
        opening_lines
    )[:2500]

    opening_frames = []

    for timestamp, path in frames:
        try:
            timestamp = float(timestamp)
        except Exception:
            continue

        if (
            segment_start - 0.05
            <= timestamp
            <= segment_start + 4.0
        ):
            opening_frames.append(
                (
                    timestamp,
                    path,
                )
            )

    if not opening_frames:
        future_frames = [
            (
                float(timestamp),
                path,
            )
            for timestamp, path in frames
            if float(timestamp) >= segment_start
        ]

        opening_frames = future_frames[:2]

    opening_frames = opening_frames[:2]

    draft_commentary = (
        plan.get("commentary")
        if isinstance(
            plan.get("commentary"),
            list,
        )
        else []
    )

    draft_first_line = ""

    if draft_commentary:
        draft_first_line = clean_text(
            draft_commentary[0].get(
                "text",
                "",
            )
        )

    prompt = f"""
You are the final factual-retention editor for ViralSpawnTV Shorts.

Your ONLY job is to create the first 1-2 second Big Hook for the selected
gaming Short.

CRITICAL FACTUALITY RULE:
You may use ONLY facts that are visible in the supplied OPENING FRAMES or
spoken in the OPENING TRANSCRIPT below.

Do NOT borrow facts from later in the clip.

Specifically:
- Do not state an HP value, score, enemy count, weapon count, round count,
  location, or other number unless it is explicitly supported in the
  opening evidence.
- Do not use ANY player name, streamer name, handle, HUD name, chat name,
  or inferred identity in the hook. Use "he", "they", or "the player"
  when a subject is needed.
- Do not reveal the final payoff.
- Do not invent stakes, quotes, wins, losses, weapons, enemies, or outcomes.
- On-screen hook: 3-8 words.
- Spoken hook: 5-14 words.
- Prefer an unresolved question/problem when truthful.
- Avoid generic hooks like NO WAY, WATCH THIS, INSANE, CRAZY,
  WHAT HAPPENS NEXT, or YOU WON'T BELIEVE THIS.
- A safe truthful hook is better than a specific but unsupported hook.

CURRENT DRAFT HEADLINE:
{plan.get("headline", "")}

CURRENT DRAFT FIRST COMMENTARY:
{draft_first_line}

OPENING TRANSCRIPT:
{opening_transcript or "(no usable opening speech)"}

Return ONLY JSON:
{{
  "headline": "3-8 word truthful opening hook",
  "voice_line": "5-14 word truthful spoken opening hook",
  "delivery": "normal/excited/hype/amused/serious",
  "hook_strength": 0-100,
  "hook_type": "danger/challenge/impossible/surprise/comedy/clutch/other",
  "reason": "one short explanation"
}}
"""

    content = [
        {
            "type":
                "input_text",
            "text":
                prompt,
        }
    ]

    for timestamp, path in opening_frames:
        content.append(
            {
                "type":
                    "input_text",
                "text":
                    (
                        f"OPENING FRAME around "
                        f"{timestamp - segment_start:.2f}s "
                        f"after selected start:"
                    ),
            }
        )

        content.append(
            {
                "type":
                    "input_image",
                "image_url":
                    (
                        "data:image/jpeg;base64,"
                        +
                        encode_image(
                            path
                        )
                    ),
            }
        )

    response = client.responses.create(
        model=os.getenv(
            "OPENAI_MODEL",
            "gpt-5.6",
        ),
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

    if raw.startswith("```"):
        raw = (
            raw
            .split("\n", 1)[1]
            .rsplit("```", 1)[0]
        )

    try:
        hook = json.loads(raw)
    except Exception:
        hook = {}

    hook_type = str(
        hook.get(
            "hook_type",
            plan.get(
                "hook_type",
                "other",
            ),
        )
    ).lower()

    valid_types = {
        "danger",
        "challenge",
        "impossible",
        "surprise",
        "comedy",
        "clutch",
        "other",
    }

    if hook_type not in valid_types:
        hook_type = "other"

    safe_fallbacks = {
        "danger":
            (
                "CAN HE GET OUT?",
                "Can he get out of this?",
            ),
        "challenge":
            (
                "CAN HE PULL THIS OFF?",
                "Can he actually pull this off?",
            ),
        "impossible":
            (
                "THIS SHOULD NOT WORK",
                "This really should not work.",
            ),
        "surprise":
            (
                "SOMETHING IS OFF HERE",
                "Something is definitely off here.",
            ),
        "comedy":
            (
                "THIS GOES WRONG FAST",
                "This goes wrong really fast.",
            ),
        "clutch":
            (
                "CAN HE PULL THIS OFF?",
                "Can he actually pull this off?",
            ),
        "other":
            (
                "HOW DOES THIS END?",
                "How does this actually end?",
            ),
    }

    fallback_headline, fallback_voice = (
        safe_fallbacks[
            hook_type
        ]
    )

    headline = clean_text(
        hook.get(
            "headline",
            "",
        )
    ).upper()

    voice_line = clean_text(
        hook.get(
            "voice_line",
            "",
        )
    )

    headline_words = headline.split()
    voice_words = voice_line.split()

    generic_exact = {
        "NO WAY",
        "WATCH THIS",
        "INSANE",
        "CRAZY",
        "CRAZY MOMENT",
        "WHAT HAPPENS NEXT",
        "YOU WONT BELIEVE THIS",
        "YOU WON'T BELIEVE THIS",
    }

    # Deterministic numeric guard:
    # if the generated hook contains a numeral not spoken in the first
    # ~3 seconds, discard the claim. Visual-only numeric claims are
    # intentionally not trusted because that was the exact V12.14 failure.
    opening_numbers = set(
        re.findall(
            r"\b\d+\b",
            opening_transcript,
        )
    )

    generated_numbers = set(
        re.findall(
            r"\b\d+\b",
            f"{headline} {voice_line}",
        )
    )

    unsupported_numbers = (
        generated_numbers
        -
        opening_numbers
    )

    if (
        len(headline_words) < 3
        or
        len(headline_words) > 8
        or
        headline in generic_exact
        or
        unsupported_numbers
    ):
        headline = fallback_headline

    if (
        len(voice_words) < 5
        or
        len(voice_words) > 14
        or
        unsupported_numbers
    ):
        voice_line = fallback_voice

    valid_delivery = {
        "normal",
        "excited",
        "hype",
        "amused",
        "serious",
    }

    delivery = str(
        hook.get(
            "delivery",
            "serious",
        )
    ).lower()

    if delivery not in valid_delivery:
        delivery = "serious"

    try:
        strength = int(
            round(
                float(
                    hook.get(
                        "hook_strength",
                        0,
                    )
                )
            )
        )
    except Exception:
        strength = 0

    strength = max(
        0,
        min(
            100,
            strength,
        ),
    )

    if unsupported_numbers:
        strength = min(
            strength,
            65,
        )

    return {
        "headline":
            headline,
        "voice_line":
            voice_line,
        "delivery":
            delivery,
        "hook_strength":
            strength,
        "hook_type":
            hook_type,
        "reason":
            clean_text(
                hook.get(
                    "reason",
                    "",
                )
            )
            or
            "Factual opening-only hook pass.",
    }


def commentary_similarity(
    left,
    right,
):
    left = clean_text(
        left
    ).lower()

    right = clean_text(
        right
    ).lower()

    if (
        not left
        or
        not right
    ):
        return 0.0

    sequence = SequenceMatcher(
        None,
        left,
        right,
    ).ratio()

    left_words = set(
        re.findall(
            r"[a-z0-9]+",
            left,
        )
    )

    right_words = set(
        re.findall(
            r"[a-z0-9]+",
            right,
        )
    )

    union = (
        left_words
        |
        right_words
    )

    jaccard = (
        len(
            left_words
            &
            right_words
        )
        /
        max(
            1,
            len(
                union
            ),
        )
    )

    return max(
        sequence,
        jaccard,
    )


def sanitize_commentary(
    client,
    commentary,
    segments,
    segment_start,
    segment_end,
):
    """
    V5.9.3 identity + repetition repair.

    The first hook line is already handled by refine_big_hook. This pass
    rewrites only the remaining narration so it:
    - uses no player/streamer names or handles
    - does not introduce unsupported numbers/facts
    - does not repeat the same idea in slightly different words
    """

    if len(commentary) <= 1:
        return commentary

    selected_lines = []

    for item in segments:
        try:
            start = float(
                item[
                    "start"
                ]
            )
            end = float(
                item[
                    "end"
                ]
            )
        except Exception:
            continue

        if end <= segment_start:
            continue

        if start >= segment_end:
            continue

        selected_lines.append(
            f"[{start:.2f}-{end:.2f}] "
            f"{clean_text(item.get('text', ''))}"
        )

    selected_transcript = "\n".join(
        selected_lines
    )[:7000]

    editable = [
        {
            "id":
                i,
            "time":
                round(
                    float(
                        beat.get(
                            "time",
                            0,
                        )
                    ),
                    2,
                ),
            "text":
                clean_text(
                    beat.get(
                        "text",
                        "",
                    )
                ),
        }
        for i, beat in enumerate(
            commentary[
                1:
            ],
            start=1,
        )
    ]

    prompt = f"""
You are repairing ViralSpawnTV narration for factual accuracy and variety.

The FIRST hook line is already locked and is not included below.

Rewrite the remaining lines using ONLY the supplied transcript.

STRICT RULES:
- Do not use ANY player name, streamer name, HUD name, chat name, handle,
  nickname, or inferred identity. Refer to people as "he", "they",
  "the player", "the opponent", etc.
- Do not state a number unless the transcript clearly supports it.
- Do not repeat the same idea, setup, or phrase in multiple lines.
- Each line should add NEW information, strategy, tension, or reaction.
- Keep each line concise and natural for gaming Shorts.
- Do not invent outcomes or facts.
- Keep the same IDs. Return one rewritten text for each ID.

TRANSCRIPT:
{selected_transcript}

LINES TO REPAIR:
{json.dumps(editable, ensure_ascii=False)}

Return ONLY JSON:
{{
  "lines": [
    {{"id": 1, "text": "rewritten narration"}}
  ]
}}
"""

    try:
        response = client.responses.create(
            model=os.getenv(
                "OPENAI_MODEL",
                "gpt-5.6",
            ),
            input=prompt,
        )

        raw = response.output_text.strip()

        if raw.startswith("```"):
            raw = (
                raw
                .split("\n", 1)[1]
                .rsplit("```", 1)[0]
            )

        repaired = json.loads(
            raw
        )

        by_id = {}

        for item in repaired.get(
            "lines",
            [],
        ):
            try:
                idx = int(
                    item.get(
                        "id"
                    )
                )
            except Exception:
                continue

            value = clean_text(
                item.get(
                    "text",
                    "",
                )
            )

            if value:
                by_id[
                    idx
                ] = value

        for idx in range(
            1,
            len(
                commentary
            ),
        ):
            if idx in by_id:
                commentary[
                    idx
                ][
                    "text"
                ] = by_id[
                    idx
                ]

    except Exception as exc:
        print(
            "Narration repair failed; "
            f"using deterministic dedupe only: {exc}"
        )

    deduped = []

    for beat in commentary:
        text = clean_text(
            beat.get(
                "text",
                "",
            )
        )

        if not text:
            continue

        duplicate = any(
            commentary_similarity(
                text,
                previous.get(
                    "text",
                    "",
                ),
            )
            >=
            0.70
            for previous in deduped
        )

        if duplicate:
            print(
                "Dropping repetitive narration: "
                f"{text}"
            )
            continue

        new_beat = dict(
            beat
        )

        new_beat[
            "text"
        ] = text

        deduped.append(
            new_beat
        )

    return deduped


# ============================================================
# V5.9.4 PRE-RENDER STORY / PAYOFF VALIDATOR
# ============================================================

def validate_and_repair_story_plan(
    client,
    plan,
    seconds,
    frames,
    segments,
):
    """
    Cheap validation BEFORE TTS/rendering.

    Goals:
    - verify the terminal payoff timestamp
    - guarantee the selected segment actually contains it
    - repair vague/unsupported narration
    - make the payoff visually explicit when evidence supports a label
    - reject an edit plan before a 6-8 minute render if the source cannot
      produce a coherent finished Short
    """

    segment_start = float(
        plan.get(
            "segment_start",
            0.0,
        )
    )

    segment_end = float(
        plan.get(
            "segment_end",
            seconds,
        )
    )

    current_payoff = float(
        plan.get(
            "payoff_time",
            segment_end,
        )
    )

    selected_lines = []

    for item in segments:
        try:
            start = float(
                item.get(
                    "start",
                    0,
                )
            )

            end = float(
                item.get(
                    "end",
                    0,
                )
            )
        except Exception:
            continue

        if end <= segment_start:
            continue

        if start >= seconds:
            continue

        selected_lines.append(
            (
                f"[{start:.2f}-{end:.2f}] "
                f"{clean_text(item.get('text', ''))}"
            )
        )

    selected_transcript = "\n".join(
        selected_lines
    )[:10000]

    content = [
        {
            "type":
                "input_text",
            "text":
                f"""
You are the PRE-RENDER story validator for ViralSpawnTV V5.9.4.

The source has already passed a strict source-quality gate.
Your job is to make sure the EDIT PLAN preserves the source's real story
and terminal payoff BEFORE we spend several minutes rendering.

SOURCE LENGTH:
{seconds:.2f}s

CURRENT SEGMENT:
{segment_start:.2f}-{segment_end:.2f}s

CURRENT PAYOFF TIME:
{current_payoff:.2f}s

CURRENT HEADLINE:
{plan.get("headline", "")}

CURRENT COMMENTARY:
{json.dumps(plan.get("commentary", []), ensure_ascii=False)}

CURRENT IMPACTS:
{json.dumps(plan.get("impacts", []), ensure_ascii=False)}

TIMESTAMPED TRANSCRIPT:
{selected_transcript}

RULES:

1. TERMINAL PAYOFF
Find the LAST meaningful event that truly completes the story promised by
the edit. Examples: ACE/ROUND WON, confirmed death, final survivor result,
goal/result confirmation, visible fail, reveal, or decisive reaction.

Do not choose an intermediate goal/kill if a later banner, result, reaction,
or confirmation is the real payoff.

2. PAYOFF COVERAGE
The final segment MUST contain the terminal payoff plus enough time for the
viewer to register it. Recommend an ending about 1.0-2.0 seconds after the
verified payoff when source footage allows it.

3. STORY PROGRESSION
The finished narration must help a viewer unfamiliar with the game follow
the progression. The hook can stay short, but later narration should explain
what materially changes.

4. CLAIM SUPPORT
Do not say everyone died, an ACE happened, a plant was stopped, a round was
won, a score changed, etc. unless the transcript/frames actually support it.

5. PAYOFF LABEL
If the final evidence visibly or verbally supports a concise payoff label
such as "ACE", "ROUND WON", "FINAL SURVIVOR", "GOAL", "CLUTCH", or
"HE'S OUT", return it. Otherwise return an empty string.
Never invent a label.

6. DEAD MIDDLE
If the source contains unavoidable static/holding footage, narration should
use that time to explain the stakes/progression rather than repeating hype.

Return ONLY JSON:
{{
  "verified_payoff_time": 0.0,
  "recommended_segment_end": 0.0,
  "payoff_label": "",
  "payoff_style": "celebration/shock/tension/funny",
  "payoff_coverage": 0-100,
  "progression_clarity": 0-100,
  "claim_support": 0-100,
  "dead_middle_risk": 0-100,
  "predicted_finished_score": 0-100,
  "repairable": true,
  "reason": "short explanation",
  "commentary": [
    {{
      "time": 0.15,
      "text": "short factual line",
      "delivery": "normal/excited/hype/amused/serious"
    }}
  ]
}}

COMMENTARY RULES:
- Preserve the existing first hook idea unless it is unsupported.
- Return 3-5 total lines when the footage supports them.
- Use relative times from the selected segment start.
- Every later line must add NEW context/progression/reaction.
- No player names/handles.
- No unsupported numbers.
- Do not narrate the payoff before it becomes visible.
""",
        }
    ]

    # Give the validator broad visual coverage, especially the tail.
    relevant_frames = []

    for timestamp, path in frames:
        try:
            timestamp = float(
                timestamp
            )
        except Exception:
            continue

        if timestamp < segment_start - 0.25:
            continue

        relevant_frames.append(
            (
                timestamp,
                path,
            )
        )

    if len(relevant_frames) > 12:
        # First 7 story frames + last 5 payoff frames.
        relevant_frames = (
            relevant_frames[:7]
            +
            relevant_frames[-5:]
        )

    for timestamp, path in relevant_frames:
        content.append(
            {
                "type":
                    "input_text",
                "text":
                    (
                        f"SOURCE FRAME around "
                        f"{timestamp:.2f}s:"
                    ),
            }
        )

        content.append(
            {
                "type":
                    "input_image",
                "image_url":
                    (
                        "data:image/jpeg;base64,"
                        +
                        encode_image(
                            path
                        )
                    ),
            }
        )

    response = client.responses.create(
        model=os.getenv(
            "OPENAI_MODEL",
            "gpt-5.6",
        ),
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

    if raw.startswith("```"):
        raw = (
            raw
            .split("\n", 1)[1]
            .rsplit("```", 1)[0]
        )

    result = json.loads(
        raw
    )

    def bounded_score(name):
        try:
            return max(
                0,
                min(
                    100,
                    int(
                        round(
                            float(
                                result.get(
                                    name,
                                    0,
                                )
                            )
                        )
                    ),
                ),
            )
        except Exception:
            return 0

    try:
        verified_payoff = float(
            result.get(
                "verified_payoff_time",
                current_payoff,
            )
        )
    except Exception:
        verified_payoff = current_payoff

    verified_payoff = max(
        segment_start,
        min(
            verified_payoff,
            seconds,
        )
    )

    try:
        recommended_end = float(
            result.get(
                "recommended_segment_end",
                verified_payoff + 1.75,
            )
        )
    except Exception:
        recommended_end = (
            verified_payoff
            +
            1.75
        )

    # Never allow the validator to trim BEFORE the verified payoff.
    recommended_end = max(
        recommended_end,
        verified_payoff + 1.0,
    )

    recommended_end = min(
        seconds,
        recommended_end,
    )

    # Extend the current edit when needed so the actual terminal payoff is
    # guaranteed to survive production.
    segment_end = max(
        segment_end,
        recommended_end,
    )

    # Keep the hard Shorts ceiling by moving the start forward only if the
    # source is long enough to require it.
    if (
        segment_end
        -
        segment_start
        >
        CORE_MAX_SECONDS
    ):
        segment_start = max(
            0.0,
            segment_end
            -
            CORE_MAX_SECONDS,
        )

    # Preserve minimum duration.
    if (
        segment_end
        -
        segment_start
        <
        CORE_MIN_SECONDS
    ):
        segment_start = max(
            0.0,
            segment_end
            -
            CORE_MIN_SECONDS,
        )

    segment_end = min(
        seconds,
        segment_end,
        segment_start
        +
        CORE_MAX_SECONDS,
    )

    plan[
        "segment_start"
    ] = segment_start

    plan[
        "segment_end"
    ] = segment_end

    plan[
        "payoff_time"
    ] = verified_payoff

    # Replace post-hook commentary with validator-repaired progression beats.
    repaired_commentary = []

    valid_delivery = {
        "normal",
        "excited",
        "hype",
        "amused",
        "serious",
    }

    for beat in result.get(
        "commentary",
        [],
    )[:5]:
        try:
            beat_time = float(
                beat.get(
                    "time",
                    0,
                )
            )
        except Exception:
            continue

        beat_text = clean_text(
            beat.get(
                "text",
                "",
            )
        )

        delivery = str(
            beat.get(
                "delivery",
                "normal",
            )
        ).lower()

        if delivery not in valid_delivery:
            delivery = "normal"

        if (
            beat_text
            and
            0.0
            <=
            beat_time
            <
            (
                segment_end
                -
                segment_start
                -
                0.25
            )
        ):
            repaired_commentary.append(
                {
                    "time":
                        beat_time,
                    "text":
                        beat_text,
                    "delivery":
                        delivery,
                }
            )

    # Keep the factual Big Hook line if the validator forgot to return one.
    existing_commentary = (
        plan.get(
            "commentary",
            [],
        )
        if isinstance(
            plan.get(
                "commentary",
                [],
            ),
            list,
        )
        else []
    )

    if existing_commentary:
        existing_hook = dict(
            existing_commentary[
                0
            ]
        )

        existing_hook[
            "time"
        ] = 0.15

        if not repaired_commentary:
            repaired_commentary = [
                existing_hook
            ]

        else:
            repaired_commentary[
                0
            ] = existing_hook

    repaired_commentary = sanitize_commentary(
        client,
        repaired_commentary,
        segments,
        segment_start,
        segment_end,
    )

    plan[
        "commentary"
    ] = repaired_commentary

    payoff_label = clean_text(
        result.get(
            "payoff_label",
            "",
        )
    ).upper()[:32]

    payoff_style = str(
        result.get(
            "payoff_style",
            "celebration",
        )
    ).lower()

    if payoff_style not in {
        "celebration",
        "shock",
        "tension",
        "funny",
    }:
        payoff_style = "celebration"

    # Guarantee one visually explicit payoff marker when supported.
    if payoff_label:
        payoff_relative = max(
            0.0,
            verified_payoff
            -
            segment_start,
        )

        impacts = [
            item
            for item in (
                plan.get(
                    "impacts",
                    [],
                )
                if isinstance(
                    plan.get(
                        "impacts",
                        [],
                    ),
                    list,
                )
                else []
            )
            if (
                abs(
                    float(
                        item.get(
                            "time",
                            -999,
                        )
                    )
                    -
                    payoff_relative
                )
                >
                1.25
            )
        ]

        impacts.append(
            {
                "time":
                    min(
                        payoff_relative,
                        max(
                            0.0,
                            (
                                segment_end
                                -
                                segment_start
                                -
                                0.6
                            ),
                        ),
                    ),
                "text":
                    payoff_label,
                "style":
                    payoff_style,
                "intensity":
                    2,
            }
        )

        impacts.sort(
            key=lambda item: float(
                item.get(
                    "time",
                    0,
                )
            )
        )

        plan[
            "impacts"
        ] = impacts[
            -2:
        ]

    validation = {
        "passed":
            bool(
                bool(
                    result.get(
                        "repairable",
                        True,
                    )
                )
                and
                bounded_score(
                    "payoff_coverage"
                )
                >=
                60
                and
                bounded_score(
                    "claim_support"
                )
                >=
                55
                and
                bounded_score(
                    "progression_clarity"
                )
                >=
                45
                and
                bounded_score(
                    "predicted_finished_score"
                )
                >=
                58
            ),
        "verified_payoff_time":
            verified_payoff,
        "segment_start":
            segment_start,
        "segment_end":
            segment_end,
        "payoff_label":
            payoff_label,
        "payoff_coverage":
            bounded_score(
                "payoff_coverage"
            ),
        "progression_clarity":
            bounded_score(
                "progression_clarity"
            ),
        "claim_support":
            bounded_score(
                "claim_support"
            ),
        "dead_middle_risk":
            bounded_score(
                "dead_middle_risk"
            ),
        "predicted_finished_score":
            bounded_score(
                "predicted_finished_score"
            ),
        "repairable":
            bool(
                result.get(
                    "repairable",
                    True,
                )
            ),
        "reason":
            clean_text(
                result.get(
                    "reason",
                    "",
                )
            ),
    }

    plan[
        "pre_render_validation"
    ] = validation

    (
        WORK
        /
        "pre_render_plan_gate.json"
    ).write_text(
        json.dumps(
            validation,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (
        WORK
        /
        "v4_edit_plan.json"
    ).write_text(
        json.dumps(
            plan,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "V5.9.4 PRE-RENDER PLAN GATE: "
        f"payoff={validation['payoff_coverage']} | "
        f"progression={validation['progression_clarity']} | "
        f"claims={validation['claim_support']} | "
        f"pred={validation['predicted_finished_score']} | "
        f"end={segment_end:.2f}s"
    )

    return (
        plan,
        validation,
    )


# ============================================================
# AI EDIT PLAN
# ============================================================

def create_plan(
    client,
    clip,
    seconds,
    frames,
    transcript,
    segments
):

    print("\n" + "=" * 65)
    print("VIRALSPAWNTV V5.9.5 RELIABILITY EDITOR")
    print("=" * 65)

    transcript_with_times = "\n".join(
        (
            f"[{x['start']:.2f}-"
            f"{x['end']:.2f}] "
            f"{x['text']}"
        )
        for x in segments
    )

    prompt = f"""
You are the senior automated Shorts editor for ViralSpawnTV.

Analyze this REAL gaming streamer clip.

This ViralSpawnTV pipeline is GAMING ONLY.
Focus on the gameplay, gaming challenge, clutch, fail, reaction,
record, speedrun, glitch, strategy, competition, or other visible
gaming moment. Do not invent context that is not supported by the
video or transcript.

LANGUAGE REQUIREMENT:
ViralSpawnTV is an English-language channel.
The source streamer may speak ANY language.
You must return ALL generated text in natural American English:
headline, title, description, commentary text, and impact text.
Translate the meaning of non-English source dialogue when needed.
Never answer in the source language merely because the transcript
is non-English.

CREATOR:
{clip["creator"]}

SOURCE:
{clip["clip_url"]}

FULL CLIP LENGTH:
{seconds:.2f} seconds

TRANSCRIPT:
{transcript}

TIMESTAMPED TRANSCRIPT:
{transcript_with_times}

Representative video frames are supplied after this prompt.

Your job is to turn this source into a highly engaging,
fast-paced, professional YouTube Short whose FINAL runtime is 40-60 seconds.

The finished video adds a 1-second ViralSpawnTV outro after the selected
source segment. Therefore, select a CORE gameplay segment between 39 and
58 seconds whenever the source supports it.

Prefer a 52-58 second CORE segment when the story naturally supports it. A strong 39-51 second core is allowed; do not pad with dead air simply to make the Short longer.

Do not invent facts.

Do not invent quotes.

Do not invent dollar amounts.

Do not claim something happened unless the transcript
or visible video supports it.

IDENTITY LOCK:
- Do not use player names, HUD names, chat names, nicknames, or handles in
  generated narration/headlines.
- Even if a name appears visually, refer to the subject as "he", "they",
  "the player", or "the opponent".
- The permanent creator credit is handled separately by the renderer.

============================================================
SELECT THE CLIP
============================================================

Choose ONE continuous CORE segment.

PAYOFF-FIRST ENDING:
Also identify the exact source timestamp where the TERMINAL meaningful
payoff/result occurs. Return it as "payoff_time" using seconds from the
start of THIS source file.

TERMINAL means the latest moment that actually completes the story:
- confirmed ROUND WON / ACE / victory / death / goal / save / result
- the reaction that proves the result landed
- a scoreboard/banner/replay that is necessary to confirm the outcome

Do NOT choose an earlier intermediate success if a later moment is the
real confirmation of the promised outcome.

The finished core must include that terminal payoff and should normally
end about 1.0-2.0 seconds AFTER the confirmation/reaction.

Do NOT keep post-payoff filler such as:
- developer consoles
- menus
- loadout/inventory screens
- scoreboards with no reaction
- dead movement after the result

If a 39-second minimum core requires a little extra footage, preserve the
minimum runtime but still cut as much post-payoff filler as possible.

DURATION REQUIREMENT:
- Absolute minimum CORE length: 39 seconds.
- Preferred CORE length: 52-58 seconds.
- Absolute maximum CORE length: 58 seconds.
- The separate 1-second ViralSpawnTV outro makes the final Short about
  40-59 seconds.

If the source is only 40-51 seconds long, use nearly the entire usable story rather than rejecting it for being shorter than the preferred range.

BIG HOOK OPENING — HIGHEST PRIORITY:
The first 1-2 seconds are the most important part of the entire Short.
The opening must create an immediate CURIOSITY GAP that makes the viewer
need to see the outcome.

Start the selected segment at the earliest moment that contains one or more
of these:
- imminent danger or failure
- a difficult challenge already in progress
- an impossible-looking situation
- a surprising visual or decision
- a funny problem that obviously needs a resolution
- a clutch attempt with clear stakes
- a moment where the viewer naturally asks "does this work?"

The opening must be understandable even if the viewer has never seen this
creator before.

Do NOT spend the opening on:
- menus or lobbies
- walking/travel with no immediate threat
- greetings
- explanations that can come later
- dead air
- ordinary gameplay
- generic streamer chatter

By 0.5 seconds, the viewer should see something interesting.
By 1.0 second, the viewer should understand WHY they should keep watching.

Do NOT reveal the final payoff in the hook unless the aftermath/reaction
creates a second compelling question.

Choose the portion with the strongest story arc that ALSO supports this
immediate cold open.

The selected segment should ideally contain:

BIG HOOK
fast context
rising tension/escalation
clear payoff or reaction

============================================================
VIRALSPAWNTV COMMENTARY
============================================================

Create 3-5 short original commentary beats.

Each beat must add ORIGINAL ViralSpawnTV value: explain strategy, build the
story, point out a meaningful gameplay decision, react to a specific moment,
or connect setup to payoff. Do not merely restate what the viewer can already
see. Make every line specific to THIS clip so the narration would not make
sense pasted onto a different gaming clip.

PROGRESSION CLARITY:
At least ONE non-hook narration beat should help a viewer unfamiliar with
the game understand how the situation is progressing toward the payoff.
Use only facts supported by the transcript or visible evidence.
Examples of useful progression information:
- what objective is being defended/attempted
- what changed after an elimination/goal/failure
- why the current position matters
- what still has to happen before the result is secure

Avoid vague narration that merely says the action is "crazy", "close",
"intense", or "not over yet" without explaining what actually changed.

The FIRST commentary beat is the BIG HOOK voice line.

FACTUAL OPENING RULE:
The hook may not use a later HP value, enemy count, score, player name,
location label, or outcome as though it is already true at frame 1.
Only describe facts supported at the actual opening moment.

It must occur 0.10-0.55 seconds after the selected segment begins.

It should be a short, natural 5-14 word sentence or fragment that creates
a curiosity gap or establishes the stakes based ONLY on what the real clip
supports.

Whenever truthful and natural, phrase the hook around an UNRESOLVED OUTCOME:
- "CAN HE CLUTCH THIS?"
- "DOES THIS ACTUALLY WORK?"
- "CAN HE GET OUT?"
- "IS THIS ENOUGH TO SURVIVE?"

Do not force a question when a stronger truthful statement creates more
tension, but the viewer should still feel that the outcome is unresolved.

It should NOT summarize or reveal the ending.
It should make the viewer want the answer.

Good hook approaches include:
- point out the impossible-looking situation
- identify the risk or challenge
- tease the decision the player is about to make
- react to something visibly unusual
- frame why this specific moment matters

Do NOT use generic hooks like:
"Wait for it"
"You won't believe this"
"What happens next is crazy"
"This is insane"
"Watch until the end"

Do NOT greet the audience.
Do NOT say the channel name in the hook.
Do NOT explain everything before the gameplay has a chance to work.

Do not narrate constantly.

Allow the creator's important dialogue and reactions
to breathe.

For every commentary beat choose ONE delivery:

normal
excited
hype
amused
serious

Write the commentary the way a real gaming creator would actually SAY it,
not like a documentary narrator, sports announcer, ad read, or AI summary.

Use contractions naturally.
Vary sentence length.
Short fragments are allowed when they sound natural.
Avoid repetitive templates such as "This player...", "He then...", or
"What happens next..." unless they genuinely fit the moment.
Do not stuff every sentence with slang.
Do not force catchphrases.
Do not use fake stutters, deliberate misspellings, or filler words just
to imitate a human.

NORMAL:
relaxed, conversational gaming commentary with natural pitch movement.

EXCITED:
genuine surprise or rising excitement. Let the energy build through the
line instead of starting at maximum intensity.

HYPE:
rare. Only exceptional peak moments. Fast, punchy, spontaneous reaction
with a clear rise in energy, but never screaming.

AMUSED:
funny or ridiculous moments. Let a smile come through in the delivery;
slightly playful timing is appropriate.

SERIOUS:
context, tension, losses, consequences. Lower and more focused delivery
with deliberate pacing.

Most narration should remain normal.

Do NOT make everything sound excited.
The delivery should change only when the actual clip earns the change.

============================================================
WOW / IMPACT SYSTEM
============================================================

Identify 0-2 moments worthy of a major visual effect.

Effects should feel earned.

For every impact provide:

time
text
style
intensity

"time" is seconds AFTER the selected segment starts.

"text" must be 1-5 words.

"style" must be one of:

celebration
shock
tension
funny

"intensity" must be:

1
2
3

Intensity 1:
minor emphasis.

Intensity 2:
strong moment.

Intensity 3:
rare peak moment.

CELEBRATION:
positive payoff.
Use energetic visual burst.

SHOCK:
unexpected reveal.
Use flash, punch zoom and large text.

TENSION:
high-pressure or negative realization.
Use punch zoom, shake and large text.
NO celebration particles.

FUNNY:
ridiculous/funny payoff.
Use bounce-like impact text.

Never celebrate financial loss.

============================================================
ENGLISH CAPTIONS
============================================================

Create "english_caption_segments" for the selected segment.
Use timestamps relative to the START of the selected segment.

These are SELECTIVE VIRAL CAPTIONS, not full subtitles.
Only caption dialogue that materially helps the viewer understand the
setup, tension, payoff, joke, clutch, fail, or reaction.

When choosing which lines to caption, prioritize information that explains
WHAT the player is trying to do, WHY the moment is difficult, WHAT is at
risk, or WHAT changed. Do not fill the Short with generic reaction captions
like "NICE!" or "NO WAY!" when a clearer story/stakes caption is available.

Translate important non-English dialogue into concise, natural
American English. If the source is already English, preserve its
meaning while cleaning it up for readable Shorts captions.

Do NOT caption every sentence.
Prefer roughly 7-16 useful caption moments across a 39-58 second CORE Short.
Leave intentional gaps with no captions.
Do not invent dialogue.
Keep each caption short, ideally 2-7 words.

IMPORTANT:
Avoid captions during ViralSpawnTV commentary whenever possible.
The narrator should have visual and audio space to speak clearly.

============================================================
YOUTUBE METADATA
============================================================

Create:

headline
title
description

HEADLINE / BIG HOOK TEXT:
3-8 words.
This is the LARGE on-screen opening hook.
It must be understandable instantly and create an unresolved question,
danger, challenge, contradiction, or funny problem.

The text should make sense with the FIRST visible action.
It should tease the payoff without revealing it.

Prefer an unresolved outcome/question when the footage supports it.
Examples:
"SQUAD DOWN. CAN HE CLUTCH?"
"ONE SHOT. DOES HE SURVIVE?"
"TRAPPED HERE. CAN HE ESCAPE?"
"THIS SHOULD FAIL... DOES IT?"

BAD:
"SAVE?"
"CLUTCH?"
"WHAT?"
"WHY?"
"INSANE"
"NO WAY"
"WATCH THIS"
"CRAZY MOMENT"

BETTER:
"CAN HE ACTUALLY SAVE THIS?"
"ONE SHOT LEFT TO SURVIVE"
"THIS HIDING SPOT SHOULD NOT WORK"
"HE HAS NO WAY OUT"
"THEY THINK THIS FIGHT IS OVER"
"HE SHOULD NOT WIN THIS"

Never invent stakes that the footage does not support.

Also return:
hook_strength: 0-100
hook_type: one of danger/challenge/impossible/surprise/comedy/clutch/other

Use 80+ only when the opening creates a genuinely strong reason to keep
watching. Do not inflate this score.

TITLE:
interesting but truthful.

DESCRIPTION:
brief explanation.
Credit creator.
Include exact source URL.

============================================================

Return ONLY valid JSON:

{{
  "segment_start": 0,
  "segment_end": 0,
  "payoff_time": 0,

  "headline": "BIG HOOK TEXT",
  "hook_strength": 0,
  "hook_type": "danger/challenge/impossible/surprise/comedy/clutch/other",

  "title": "YouTube title",

  "description": "description",

  "commentary": [
    {{
      "time": 0.5,
      "text": "short commentary",
      "delivery": "normal"
    }}
  ],

  "impacts": [
    {{
      "time": 10.0,
      "text": "NO WAY",
      "style": "shock",
      "intensity": 2
    }}
  ]
}}
"""

    content = [{
        "type": "input_text",
        "text": prompt,
    }]

    for timestamp, path in frames:

        content.append({
            "type": "input_text",
            "text": (
                f"Video frame around "
                f"{timestamp:.1f} seconds:"
            ),
        })

        content.append({
            "type": "input_image",
            "image_url": (
                "data:image/jpeg;base64,"
                + encode_image(path)
            ),
        })

    response = client.responses.create(
        model=os.getenv(
            "OPENAI_MODEL",
            "gpt-5.6"
        ),

        input=[{
            "role": "user",
            "content": content,
        }],
    )

    raw = response.output_text.strip()

    if raw.startswith("```"):

        raw = (
            raw
            .split("\n", 1)[1]
            .rsplit("```", 1)[0]
        )

    plan = json.loads(raw)

    start = float(
        plan["segment_start"]
    )

    end = float(
        plan["segment_end"]
    )

    try:
        payoff_time = float(
            plan.get(
                "payoff_time",
                end,
            )
        )
    except Exception:
        payoff_time = end

    # V5.9.3 duration enforcement.
    if seconds < CORE_MIN_SECONDS:
        raise RuntimeError(
            f"Source clip is only {seconds:.2f}s; "
            f"need at least {CORE_MIN_SECONDS:.1f}s "
            "for the 40-60 second Shorts strategy."
        )

    # Long sources still prefer a 52s+ story when available.
    # Short 40-51s sources are no longer forced to use the ENTIRE file,
    # because doing so kept post-payoff console/menu footage.
    preferred_min = (
        CORE_IDEAL_MIN_SECONDS
        if seconds >= CORE_IDEAL_MIN_SECONDS
        else CORE_MIN_SECONDS
    )

    max_start = max(
        0.0,
        seconds - preferred_min,
    )

    start = max(
        0.0,
        min(
            start,
            max_start,
        )
    )

    end = max(
        end,
        start + preferred_min,
    )

    end = min(
        seconds,
        end,
        start + CORE_MAX_SECONDS,
    )

    # Payoff-aware trim:
    # end soon after the decisive result/reaction instead of drifting into
    # developer consoles, menus, inventory, or dead post-round footage.
    payoff_time = max(
        start,
        min(
            payoff_time,
            seconds,
        )
    )

    if (
        start
        <
        payoff_time
        <=
        end
    ):
        payoff_end = min(
            seconds,
            payoff_time + 1.75,
        )

        end = min(
            end,
            max(
                start + CORE_MIN_SECONDS,
                payoff_end,
            ),
        )

    # Final safety: guarantee the absolute 39-second core.
    if end - start < CORE_MIN_SECONDS:
        if end >= CORE_MIN_SECONDS:
            start = max(
                0.0,
                end - CORE_MIN_SECONDS,
            )
        else:
            start = 0.0
            end = min(
                seconds,
                CORE_MIN_SECONDS,
            )

    # Re-apply hard ceiling after any start adjustment.
    end = min(
        seconds,
        end,
        start + CORE_MAX_SECONDS,
    )

    plan["segment_start"] = start
    plan["segment_end"] = end
    plan["payoff_time"] = payoff_time

    clip_length = end - start

    if not (
        CORE_MIN_SECONDS
        <= clip_length
        <= CORE_MAX_SECONDS + 0.05
    ):
        raise RuntimeError(
            f"V5.9.3 core duration invalid: {clip_length:.2f}s. "
            f"Expected {CORE_MIN_SECONDS:.1f}-"
            f"{CORE_MAX_SECONDS:.1f}s."
        )

    print(
        "V5.9.3 payoff trim: "
        f"start={start:.2f}s | "
        f"payoff={payoff_time:.2f}s | "
        f"end={end:.2f}s"
    )

    # ---------------------------------------------
    # V5.9 Big Hook second pass
    # ---------------------------------------------

    hook_plan = refine_big_hook(
        client,
        plan,
        segments,
        frames,
        start,
        end,
    )

    plan["headline"] = hook_plan[
        "headline"
    ]

    plan["hook_strength"] = hook_plan[
        "hook_strength"
    ]

    plan["hook_type"] = hook_plan[
        "hook_type"
    ]

    plan["hook_reason"] = hook_plan[
        "reason"
    ]

    # ---------------------------------------------
    # Commentary validation
    # ---------------------------------------------

    valid_delivery = {
        "normal",
        "excited",
        "hype",
        "amused",
        "serious",
    }

    commentary = []

    for beat in plan.get(
        "commentary",
        []
    )[:5]:

        try:
            beat_time = float(
                beat.get(
                    "time",
                    0
                )
            )
        except Exception:
            continue

        delivery = str(
            beat.get(
                "delivery",
                "normal"
            )
        ).lower()

        if delivery not in valid_delivery:
            delivery = "normal"

        text = clean_text(
            beat.get(
                "text",
                ""
            )
        )

        if (
            text
            and
            0 <= beat_time <
            clip_length - 0.5
        ):

            commentary.append({
                "time": beat_time,
                "text": text,
                "delivery": delivery,
            })

    commentary.sort(
        key=lambda x: float(
            x.get("time", 0)
        )
    )

    # V5.9: the hook refiner owns the first spoken line.
    refined_voice_line = clean_text(
        hook_plan.get(
            "voice_line",
            "",
        )
    )

    refined_delivery = str(
        hook_plan.get(
            "delivery",
            "serious",
        )
    ).lower()

    if refined_delivery not in valid_delivery:
        refined_delivery = "serious"

    if refined_voice_line:
        if commentary:
            commentary[0][
                "text"
            ] = refined_voice_line

            commentary[0][
                "delivery"
            ] = refined_delivery

            commentary[0][
                "time"
            ] = 0.15
        else:
            commentary.insert(
                0,
                {
                    "time": 0.15,
                    "text": refined_voice_line,
                    "delivery": refined_delivery,
                },
            )

    # The first narration line must land almost immediately.
    if commentary:
        commentary[0]["time"] = max(
            0.10,
            min(
                float(
                    commentary[0]["time"]
                ),
                0.55,
            ),
        )

    # V5.9.3: remove identity drift and near-duplicate narration.
    commentary = sanitize_commentary(
        client,
        commentary,
        segments,
        start,
        end,
    )

    plan["commentary"] = commentary

    # ---------------------------------------------
    # Impact validation
    # ---------------------------------------------

    valid_styles = {
        "celebration",
        "shock",
        "tension",
        "funny",
    }

    impacts = []

    for impact in plan.get(
        "impacts",
        []
    )[:2]:

        try:
            impact_time = float(
                impact.get(
                    "time",
                    0
                )
            )
        except Exception:
            continue

        style = str(
            impact.get(
                "style",
                "shock"
            )
        ).lower()

        if style not in valid_styles:
            style = "shock"

        try:
            intensity = int(
                impact.get(
                    "intensity",
                    2
                )
            )
        except Exception:
            intensity = 2

        intensity = max(
            1,
            min(
                intensity,
                3
            )
        )

        text = clean_text(
            impact.get(
                "text",
                "WOW"
            )
        ).upper()[:45]

        if (
            text
            and
            0 <= impact_time <
            clip_length - 0.5
        ):

            impacts.append({
                "time": impact_time,
                "text": text,
                "style": style,
                "intensity": intensity,
            })

    plan["impacts"] = impacts

    # ---------------------------------------------
    # English translated caption validation
    # ---------------------------------------------

    english_caption_segments = []

    for item in plan.get(
        "english_caption_segments",
        []
    ):
        try:
            cstart = float(item.get("start", 0))
            cend = float(item.get("end", 0))
        except Exception:
            continue

        ctext = clean_text(
            item.get("text", "")
        )

        if (
            ctext
            and
            0 <= cstart < clip_length
            and
            cend > cstart
        ):
            english_caption_segments.append({
                "start": cstart,
                "end": min(cend, clip_length),
                "text": ctext,
            })

    plan["english_caption_segments"] = (
        english_caption_segments
    )

    (
        WORK /
        "v4_edit_plan.json"
    ).write_text(
        json.dumps(
            plan,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        json.dumps(
            plan,
            indent=2
        )
    )

    return plan


# ============================================================
# REAL CAPTION GENERATION
# ============================================================

def create_real_captions(
    segments,
    segment_start,
    segment_end
):

    print("\n" + "=" * 65)
    print("BUILDING TIMESTAMPED CAPTIONS")
    print("=" * 65)

    captions = []

    for segment in segments:

        source_start = float(
            segment["start"]
        )

        source_end = float(
            segment["end"]
        )

        if source_end <= segment_start:
            continue

        if source_start >= segment_end:
            continue

        relative_start = max(
            0,
            source_start - segment_start
        )

        relative_end = min(
            segment_end - segment_start,
            source_end - segment_start
        )

        text = clean_text(
            segment["text"]
        )

        if not text:
            continue

        words = text.split()

        #
        # Split longer transcript segments into
        # smaller Shorts-style chunks.
        #

        max_words = 5

        chunks = [
            words[i:i + max_words]
            for i in range(
                0,
                len(words),
                max_words
            )
        ]

        total_words = max(
            1,
            len(words)
        )

        segment_duration = (
            relative_end -
            relative_start
        )

        consumed = 0

        for chunk in chunks:

            fraction_start = (
                consumed /
                total_words
            )

            consumed += len(chunk)

            fraction_end = (
                consumed /
                total_words
            )

            cstart = (
                relative_start
                +
                segment_duration
                *
                fraction_start
            )

            cend = (
                relative_start
                +
                segment_duration
                *
                fraction_end
            )

            #
            # Prevent captions flashing too quickly.
            #

            if cend - cstart < 0.45:
                cend = min(
                    relative_end,
                    cstart + 0.45
                )

            captions.append({
                "start": cstart,
                "end": cend,
                "text": " ".join(
                    chunk
                ).upper(),
            })

    (
        WORK /
        "v4_captions.json"
    ).write_text(
        json.dumps(
            captions,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        f"Caption chunks: "
        f"{len(captions)}"
    )

    return captions


# ============================================================
# CAPTION / NARRATION COLLISION CONTROL
# ============================================================

def suppress_captions_during_narration(
    captions,
    beats,
    padding=0.18
):

    print("\n" + "=" * 65)
    print("SUPPRESSING CAPTIONS DURING NARRATION")
    print("=" * 65)

    if not captions or not beats:
        return captions

    narration_windows = []

    for beat in beats:

        start = max(
            0.0,
            float(beat["time"]) - padding
        )

        end = (
            float(beat["time"])
            +
            float(beat["duration"])
            +
            padding
        )

        narration_windows.append(
            (start, end)
        )

    output = []

    for caption in captions:

        pieces = [(
            float(caption["start"]),
            float(caption["end"])
        )]

        for nstart, nend in narration_windows:

            new_pieces = []

            for pstart, pend in pieces:

                # No overlap.
                if pend <= nstart or pstart >= nend:
                    new_pieces.append(
                        (pstart, pend)
                    )
                    continue

                # Keep usable portion before narration.
                if nstart - pstart >= 0.55:
                    new_pieces.append(
                        (pstart, nstart)
                    )

                # Keep usable portion after narration.
                if pend - nend >= 0.55:
                    new_pieces.append(
                        (nend, pend)
                    )

            pieces = new_pieces

            if not pieces:
                break

        for pstart, pend in pieces:

            if pend - pstart < 0.55:
                continue

            output.append({
                "start": pstart,
                "end": pend,
                "text": caption["text"],
            })

    # Prevent pathological caption density even if the model ignores
    # the selective-caption instruction.
    max_captions = 14

    if len(output) > max_captions:

        # Evenly sample across the finished Short so we retain context
        # from beginning, middle and end instead of only early captions.
        if max_captions == 1:
            output = [output[0]]
        else:
            indexes = [
                round(
                    i * (len(output) - 1)
                    / (max_captions - 1)
                )
                for i in range(max_captions)
            ]

            output = [
                output[i]
                for i in indexes
            ]

    (
        WORK /
        "v4_captions_final.json"
    ).write_text(
        json.dumps(
            output,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        f"Captions after narration suppression: "
        f"{len(output)}"
    )

    return output


# ============================================================
# NATURAL, CONTEXT-AWARE TTS
# ============================================================

# V5.2 VOICE UPDATE
#
# Keep the same narration timing/render pipeline, but make the spoken
# delivery less uniform. The TTS model receives a shared natural-speech
# direction plus a context-specific direction for each commentary beat.
#
# "cedar" is the default because it is one of OpenAI's recommended
# high-quality built-in voices. TTS_VOICE can still override it from
# the environment without another code change.

BASE_VOICE_INSTRUCTIONS = (
    "Young adult American male gaming creator speaking to viewers. "
    "Neutral United States accent. "
    "Sound like natural live commentary recorded for a gaming channel, "
    "not a commercial, documentary, radio host, sports announcer, or "
    "text-to-speech system. "
    "Use natural pitch movement, conversational rhythm, and subtle changes "
    "in pace. Do not give every word equal emphasis. "
    "Let important words receive emphasis naturally and let unimportant "
    "words stay relaxed. "
    "Use brief natural pauses where the sentence meaning calls for them. "
    "Keep the delivery grounded and believable. "
    "Do not over-enunciate. Do not sound polished like an advertisement. "
    "Do not add words that are not in the script. "
)

DELIVERY_INSTRUCTIONS = {

    "normal": (
        "Keep this beat relaxed and conversational. "
        "Start casually, as if reacting while watching the gameplay. "
        "Use small natural changes in pitch and tempo rather than a flat "
        "narrator cadence. End the sentence naturally instead of giving it "
        "an announcer-style finish."
    ),

    "excited": (
        "Let genuine excitement build during this beat. "
        "Begin conversationally, then raise the energy, pitch, and pace "
        "slightly as the important moment lands. Put stronger emphasis on "
        "the key phrase near the payoff. Sound surprised and engaged, "
        "but do not yell or become theatrical."
    ),

    "hype": (
        "This is a rare peak gaming moment. "
        "React with noticeably higher energy and a quicker pace. "
        "Use a spontaneous punch on the most important words and allow "
        "the pitch to rise naturally with the moment. Keep it controlled "
        "and intelligible. It should feel like a real creator reacting "
        "to a clutch play, not an announcer reading promotional copy."
    ),

    "amused": (
        "Sound genuinely entertained by what happened. "
        "Use playful timing and let a subtle smile be audible in the voice. "
        "A tiny breathy chuckle quality is okay only if it happens naturally, "
        "but do not insert extra spoken words or force laughter. "
        "Keep the line casual and slightly mischievous."
    ),

    "serious": (
        "Lower the energy and sound focused. "
        "Use a slightly slower, more deliberate pace with restrained pitch. "
        "Create tension through timing and emphasis rather than sounding "
        "dramatic or ominous. Keep it conversational."
    ),
}


def voice_instructions(delivery, beat_index, total_beats):
    """
    Build one natural-speech instruction set for each narration beat.

    The small beat-position directions help prevent every line from using
    the exact same cadence while keeping the requested emotional delivery.
    """

    delivery = str(delivery).lower().strip()

    if delivery not in DELIVERY_INSTRUCTIONS:
        delivery = "normal"

    if total_beats <= 1:
        position_instruction = (
            "This is the only narration beat in the Short, so give it a "
            "complete natural thought without sounding rehearsed."
        )

    elif beat_index == 0:
        position_instruction = (
            "This is the opening narration beat. Enter quickly and naturally "
            "without a formal introduction or announcer-style setup."
        )

    elif beat_index == total_beats - 1:
        position_instruction = (
            "This is the final narration beat. Let the delivery respond to "
            "the payoff and finish cleanly without a canned sign-off."
        )

    else:
        position_instruction = (
            "This is a middle narration beat. Make it feel like a continuation "
            "of a real reaction rather than restarting a scripted narration."
        )

    return (
        BASE_VOICE_INSTRUCTIONS
        + DELIVERY_INSTRUCTIONS[delivery]
        + " "
        + position_instruction
    )


def generate_voices(
    client,
    plan
):

    print("\n" + "=" * 65)
    print("GENERATING NATURAL CONTEXT-AWARE VOICE")
    print("=" * 65)

    beats = []

    commentary = plan.get(
        "commentary",
        []
    )

    total_beats = len(
        commentary
    )

    for i, beat in enumerate(
        commentary
    ):

        delivery = str(
            beat.get(
                "delivery",
                "normal"
            )
        ).lower().strip()

        if delivery not in DELIVERY_INSTRUCTIONS:
            delivery = "normal"

        output = (
            VOICES /
            f"v4_voice_{i:02d}_{delivery}.mp3"
        )

        print(
            f"\nVoice {i + 1}: "
            f"{delivery.upper()}"
        )

        print(
            beat["text"]
        )

        instructions = voice_instructions(
            delivery,
            i,
            total_beats
        )

        with (
            client.audio.speech
            .with_streaming_response
            .create(
                model=os.getenv(
                    "TTS_MODEL",
                    "gpt-4o-mini-tts"
                ),

                voice=os.getenv(
                    "TTS_VOICE",
                    "cedar"
                ),

                input=beat["text"],

                instructions=instructions,
            )
        ) as response:

            response.stream_to_file(
                output
            )

        voice_duration = duration(
            output
        )

        new_beat = dict(
            beat
        )

        new_beat["delivery"] = delivery

        new_beat["file"] = output

        new_beat["duration"] = (
            voice_duration
        )

        beats.append(
            new_beat
        )

    return beats


# ============================================================
# VIDEO FILTER
# ============================================================

def build_video_filter(
    plan,
    captions
):

    headline_file = make_text_file(
        "v5_9_1_big_hook",
        str(
            plan["headline"]
        ).upper(),
        # V5.9.1: wrap earlier so even wide letters remain inside
        # the mobile-safe area. Three short lines are allowed.
        width=15,
    )

    credit_file = make_text_file(
        "v4_credit",
        f"@{CHANNEL}  •  VIRALSPAWNTV"
    )

    filters = []

    # ========================================================
    # BACKGROUND
    # ========================================================

    filters.append(
        "[0:v]"
        "scale=1080:1920:"
        "force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "boxblur=28:14,"
        "eq=brightness=-0.10"
        "[background]"
    )

    # ========================================================
    # MAIN SOURCE
    # ========================================================

    # V5.7: Make gameplay materially larger on mobile.
    # 16:9 footage is scaled to 820px tall, then horizontally cropped
    # to 1080px so the action occupies ~35% more vertical space.
    filters.append(
        "[0:v]"
        "scale=-2:820,"
        "crop=1080:820"
        "[foreground]"
    )

    filters.append(
        "[background][foreground]"
        "overlay="
        "x=(W-w)/2:"
        "y=(H-h)/2"
        "[composite]"
    )

    # V5.9 BIG HOOK visual punch-in.
    # For the first 1.35 seconds, show the same gameplay slightly larger.
    # This creates immediate movement/visual emphasis without adding an
    # intro card or delaying the actual clip.
    filters.append(
        "[0:v]"
        "scale=-2:900,"
        "crop=1080:900"
        "[hookforeground]"
    )

    filters.append(
        "[composite][hookforeground]"
        "overlay="
        "x=(W-w)/2:"
        "y=(H-h)/2:"
        "enable='between(t,0,1.35)'"
        "[hookzoom]"
    )

    current = "hookzoom"

    # ========================================================
    # PUNCH ZOOM EFFECT
    #
    # Rather than trying to dynamically rescale the entire
    # graph, create temporary zoom overlays from source.
    # ========================================================

    for i, impact in enumerate(
        plan["impacts"]
    ):

        moment = float(
            impact["time"]
        )

        intensity = int(
            impact["intensity"]
        )

        zoom_duration = (
            0.45
            +
            0.12 * intensity
        )

        zoom_factor = {
            1: 1.04,
            2: 1.07,
            3: 1.11,
        }[intensity]

        # Base gameplay is now 820px tall. Punch zoom by increasing
        # height further and cropping horizontally back to 1080.
        zoom_height = int(
            820 *
            zoom_factor
        )

        if zoom_height % 2:
            zoom_height += 1

        zoom_source = (
            f"zoomsource{i}"
        )

        filters.append(
            f"[0:v]"
            f"scale=-2:"
            f"{zoom_height},"
            f"crop=1080:"
            f"{zoom_height}"
            f"[{zoom_source}]"
        )

        zoom_output = (
            f"zoomoverlay{i}"
        )

        filters.append(
            f"[{current}]"
            f"[{zoom_source}]"
            f"overlay="
            f"x=(W-w)/2:"
            f"y=(H-h)/2:"
            f"enable='between(t,"
            f"{moment:.3f},"
            f"{moment + zoom_duration:.3f})'"
            f"[{zoom_output}]"
        )

        current = zoom_output

    # ========================================================
    # HEADLINE
    # ========================================================

    filters.append(
        f"[{current}]"
        "drawbox="
        # V5.9.1 mobile-safe Big Hook panel:
        # 80px side margins and enough height for up to 3 wrapped lines.
        "x=80:"
        "y=150:"
        "w=920:"
        "h=350:"
        "color=black@0.68:"
        "t=fill:"
        "enable='between(t,0,2.80)',"

        "drawtext="
        f"fontfile={FONT}:"
        f"textfile={headline_file}:"
        "fontcolor=white:"
        "fontsize=68:"
        "line_spacing=8:"
        "x='max(95,(w-text_w)/2)':"
        # Keep text safely below the top UI area and centered vertically
        # inside the panel for 1-3 lines.
        "y=205:"
        "borderw=6:"
        "bordercolor=black:"
        "shadowx=3:"
        "shadowy=3:"
        "shadowcolor=black@0.85:"
        "enable='between(t,0,2.80)'"
        "[headline]"
    )

    current = "headline"

    # ========================================================
    # REAL TIMESTAMPED CAPTIONS
    # ========================================================

    for i, caption in enumerate(
        captions
    ):

        text_file = make_text_file(
            f"v4_caption_{i:03d}",
            caption["text"],
            width=18,
        )

        output_label = (
            f"caption{i}"
        )

        filters.append(
            f"[{current}]"
            "drawtext="
            f"fontfile={FONT}:"
            f"textfile={text_file}:"
            "fontcolor=white:"
            "fontsize=62:"
            "line_spacing=6:"
            "x=(w-text_w)/2:"
            "y=1400:"
            "borderw=7:"
            "bordercolor=black:"
            "shadowx=3:"
            "shadowy=3:"
            "shadowcolor=black@0.8:"
            f"enable='between(t,"
            f"{caption['start']:.3f},"
            f"{caption['end']:.3f})'"
            f"[{output_label}]"
        )

        current = output_label

    # ========================================================
    # WOW EFFECTS
    # ========================================================

    for i, impact in enumerate(
        plan["impacts"]
    ):

        moment = float(
            impact["time"]
        )

        style = impact[
            "style"
        ]

        intensity = int(
            impact["intensity"]
        )

        impact_text = make_text_file(
            f"v4_impact_{i}",
            impact["text"],
            width=13,
        )

        # ----------------------------------------------------
        # FLASH
        # ----------------------------------------------------

        if style in {
            "celebration",
            "shock"
        }:

            flash_duration = (
                0.07
                +
                intensity * 0.035
            )

            label = (
                f"flash{i}"
            )

            filters.append(
                f"[{current}]"
                "drawbox="
                "x=0:"
                "y=0:"
                "w=iw:"
                "h=ih:"
                "color=white@0.65:"
                "t=fill:"
                f"enable='between(t,"
                f"{moment:.3f},"
                f"{moment + flash_duration:.3f})'"
                f"[{label}]"
            )

            current = label

        # ----------------------------------------------------
        # SCREEN SHAKE FEEL
        #
        # Add quick black edge pulses on high intensity
        # shock/tension moments.
        # ----------------------------------------------------

        if (
            style in {
                "shock",
                "tension"
            }
            and
            intensity >= 2
        ):

            for pulse in range(
                intensity + 1
            ):

                pulse_start = (
                    moment
                    +
                    pulse * 0.09
                )

                pulse_end = (
                    pulse_start
                    +
                    0.045
                )

                side = (
                    8
                    +
                    intensity * 5
                )

                label = (
                    f"shake_{i}_{pulse}"
                )

                filters.append(
                    f"[{current}]"
                    "drawbox="
                    f"x={side}:"
                    "y=0:"
                    f"w=iw-{side * 2}:"
                    "h=ih:"
                    "color=black@0.12:"
                    "t=fill:"
                    f"enable='between(t,"
                    f"{pulse_start:.3f},"
                    f"{pulse_end:.3f})'"
                    f"[{label}]"
                )

                current = label

        # ----------------------------------------------------
        # CELEBRATION BURST
        # ----------------------------------------------------

        if style == "celebration":

            center_x = 540
            center_y = 930

            particle_count = (
                10
                +
                intensity * 6
            )

            for particle in range(
                particle_count
            ):

                angle = (
                    2 *
                    math.pi *
                    particle /
                    particle_count
                )

                radius = (
                    170
                    +
                    (
                        particle % 3
                    ) * 70
                )

                x = int(
                    center_x
                    +
                    math.cos(angle)
                    *
                    radius
                )

                y = int(
                    center_y
                    +
                    math.sin(angle)
                    *
                    radius
                )

                size = (
                    12
                    +
                    (
                        particle % 4
                    ) * 5
                )

                delay = (
                    (
                        particle % 5
                    )
                    *
                    0.025
                )

                particle_start = (
                    moment
                    +
                    delay
                )

                particle_end = (
                    particle_start
                    +
                    0.45
                    +
                    intensity * 0.10
                )

                label = (
                    f"burst_{i}_{particle}"
                )

                filters.append(
                    f"[{current}]"
                    "drawbox="
                    f"x={x}:"
                    f"y={y}:"
                    f"w={size}:"
                    f"h={size}:"
                    "color=white@0.95:"
                    "t=fill:"
                    f"enable='between(t,"
                    f"{particle_start:.3f},"
                    f"{particle_end:.3f})'"
                    f"[{label}]"
                )

                current = label

        # ----------------------------------------------------
        # GIANT IMPACT TEXT
        # ----------------------------------------------------

        if intensity == 1:
            font_size = 90

        elif intensity == 2:
            font_size = 112

        else:
            font_size = 132

        impact_duration = (
            0.80
            +
            intensity * 0.18
        )

        label = (
            f"impacttext{i}"
        )

        filters.append(
            f"[{current}]"
            "drawtext="
            f"fontfile={FONT}:"
            f"textfile={impact_text}:"
            "fontcolor=white:"
            f"fontsize={font_size}:"
            "x=(w-text_w)/2:"
            "y=(h-text_h)/2:"
            "borderw=11:"
            "bordercolor=black:"
            "shadowx=5:"
            "shadowy=5:"
            "shadowcolor=black@0.8:"
            f"enable='between(t,"
            f"{moment:.3f},"
            f"{moment + impact_duration:.3f})'"
            f"[{label}]"
        )

        current = label

    # ========================================================
    # V5.4 VIRALSPAWNTV ELECTRIC FRAME OVERLAY
    # ========================================================
    # Uses the custom neon cyber frame asset instead of geometric
    # drawbox bars. The frame loops for the full Short and receives
    # a subtle continuous electrical brightness pulse.
    # ========================================================

    frame_path = NEON_FRAME.as_posix().replace(":", "\\:")

    filters.append(
        f"movie={frame_path},"
        "loop=loop=-1:size=1:start=0,"
        "fps=30,"
        "scale=1080:1920,"
        "format=rgba,"
        "eq=brightness='0.045*sin(2*PI*t/1.55)':eval=frame"
        "[viralframe]"
    )

    filters.append(
        f"[{current}][viralframe]"
        "overlay=0:0:repeatlast=1"
        "[framedvideo]"
    )

    current = "framedvideo"

    # ========================================================
    # PERMANENT BRANDING
    # ========================================================

    filters.append(
        f"[{current}]"
        "drawbox="
        "x=0:"
        "y=h-125:"
        "w=iw:"
        "h=125:"
        "color=black@0.25:"
        "t=fill,"

        "drawtext="
        f"fontfile={FONT}:"
        f"textfile={credit_file}:"
        "fontcolor=white:"
        "fontsize=31:"
        "x=(w-text_w)/2:"
        "y=h-82:"
        "borderw=3:"
        "bordercolor=black"
        "[finalvideo]"
    )

    return ";".join(
        filters
    )


# ============================================================
# AUDIO FILTER
# ============================================================

def build_audio_filter(beats):

    filters = []

    #
    # More robust than the V3 multiplied volume expression.
    #
    # Start with source audio at full volume.
    #

    source_chain = "[0:a]volume=1.0"

    for beat in beats:

        start = float(
            beat["time"]
        )

        end = (
            start
            +
            float(
                beat["duration"]
            )
            +
            0.12
        )

        source_chain += (
            ",volume="
            "volume=0.20:"
            f"enable='between(t,"
            f"{start:.3f},"
            f"{end:.3f})'"
        )

    source_chain += (
        "[sourceaudio]"
    )

    filters.append(
        source_chain
    )

    mix_inputs = [
        "[sourceaudio]"
    ]

    for i, beat in enumerate(
        beats
    ):

        delay_ms = int(
            float(
                beat["time"]
            )
            *
            1000
        )

        delivery = beat[
            "delivery"
        ]

        #
        # Small volume boost for excited delivery,
        # but not enough to distort.
        #

        voice_volume = {
            "normal": 1.24,
            "excited": 1.30,
            "hype": 1.33,
            "amused": 1.27,
            "serious": 1.22,
        }.get(
            delivery,
            1.25
        )

        filters.append(
            f"[{i + 1}:a]"
            f"adelay={delay_ms}:all=1,"
            f"volume={voice_volume}"
            f"[voice{i}]"
        )

        mix_inputs.append(
            f"[voice{i}]"
        )

    filters.append(
        "".join(
            mix_inputs
        )
        +
        "amix="
        f"inputs={len(mix_inputs)}:"
        "duration=first:"
        "dropout_transition=0:"
        "normalize=0"
        "[finalaudio]"
    )

    return ";".join(
        filters
    )


# ============================================================
# RENDER
# ============================================================

def render(
    clip,
    plan,
    beats,
    captions
):

    print("\n" + "=" * 65)
    print("RENDERING VIRALSPAWNTV V4")
    print("=" * 65)

    start = float(
        plan["segment_start"]
    )

    end = float(
        plan["segment_end"]
    )

    clip_length = (
        end -
        start
    )

    video_filter = (
        build_video_filter(
            plan,
            captions
        )
    )

    audio_filter = (
        build_audio_filter(
            beats
        )
    )

    filter_complex = (
        video_filter
        +
        ";"
        +
        audio_filter
    )

    command = [
        "ffmpeg",
        "-y",

        "-ss",
        str(start),

        "-t",
        str(clip_length),

        "-i",
        str(
            clip["video"]
        ),
    ]

    for beat in beats:

        command.extend([
            "-i",
            str(
                beat["file"]
            ),
        ])

    command.extend([
        "-filter_complex",
        filter_complex,

        "-map",
        "[finalvideo]",

        "-map",
        "[finalaudio]",

        "-c:v",
        "libx264",

        "-preset",
        "medium",

        "-crf",
        "19",

        "-pix_fmt",
        "yuv420p",

        "-r",
        "30",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-ar",
        "48000",

        "-movflags",
        "+faststart",

        "-t",
        str(
            clip_length
        ),

        str(
            CORE_VIDEO
        ),
    ])

    run(
        command
    )

    if not CORE_VIDEO.exists():

        raise RuntimeError(
            "V4 final video "
            "was not created."
        )

    final_duration = duration(
        CORE_VIDEO
    )

    final_size = (
        CORE_VIDEO
        .stat()
        .st_size
        /
        1_000_000
    )

    print("\n" + "=" * 65)
    print("V4 SHORT CREATED SUCCESSFULLY")
    print("=" * 65)

    print(
        f"\nFile: "
        f"{FINAL_VIDEO}"
    )

    print(
        f"Duration: "
        f"{final_duration:.2f}s"
    )

    print(
        f"Size: "
        f"{final_size:.2f} MB"
    )


# ============================================================
# V5.1 SHORTS BRAND BOOKENDS
# ============================================================

def render_short_brand_card(image_path, dest, seconds, label):

    if not image_path.exists():
        raise RuntimeError(
            f"Missing Shorts branding asset: {image_path}"
        )

    channel_file = make_text_file(
        "v5_7_outro_channel",
        "VIRALSPAWNTV",
        width=18,
    )

    cta_file = make_text_file(
        "v5_7_outro_cta",
        OUTRO_CTA,
        width=18,
    )

    # V5.7: intentionally simple outro.
    # Use the old image only as a heavily blurred/darkened background so
    # its extra icons/copy cannot compete with the two messages below.
    filter_complex = (
        "[0:v]"
        "scale=1080:1920:"
        "force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "boxblur=45:20,"
        "eq=brightness=-0.38:saturation=0.45,"
        "drawbox="
        "x=0:y=0:w=1080:h=1920:"
        "color=black@0.42:t=fill,"
        "drawtext="
        f"fontfile={FONT}:"
        f"textfile={channel_file}:"
        "fontcolor=white:"
        "fontsize=92:"
        "x=(w-text_w)/2:"
        "y=760:"
        "borderw=5:"
        "bordercolor=black,"
        "drawtext="
        f"fontfile={FONT}:"
        f"textfile={cta_file}:"
        "fontcolor=white:"
        "fontsize=48:"
        "x=(w-text_w)/2:"
        "y=900:"
        "borderw=4:"
        "bordercolor=black:"
        "shadowx=3:"
        "shadowy=3:"
        "shadowcolor=black@0.8,"
        "format=yuv420p"
        "[v]"
    )

    run([
        "ffmpeg", "-y",
        "-loop", "1",
        "-t", str(seconds),
        "-i", str(image_path),
        "-f", "lavfi",
        "-t", str(seconds),
        "-i", "anullsrc=r=48000:cl=stereo",
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "1:a:0",
        "-t", str(seconds),
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        str(dest),
    ])

    print(f"V5.6 {label} created: {seconds:.1f}s")


def add_short_brand_bookends():

    if not CORE_VIDEO.exists():
        raise RuntimeError(
            "Frozen V5 core Short was not created."
        )

    # V5.5: cold-open directly on gameplay.
    # Keep only the branded outro so the viewer sees the actual
    # clip immediately instead of a pre-roll channel card.
    render_short_brand_card(
        OUTRO_IMAGE, OUTRO_VIDEO, OUTRO_SECONDS, "outro"
    )

    concat = WORK / "v5_1_brand_concat.txt"
    concat.write_text(
        "\n".join([
            f"file '{CORE_VIDEO.resolve().as_posix()}'",
            f"file '{OUTRO_VIDEO.resolve().as_posix()}'",
        ]),
        encoding="utf-8",
    )

    # V5.1.1 BRAND-CONCAT AUDIO SAFETY FIX
    #
    # Keep the frozen V5.1 video/branding behavior unchanged, but
    # normalize the concatenated audio into a fresh finite stream
    # before AAC encoding. This prevents rare concat-boundary
    # invalid audio samples from crashing the AAC encoder.
    run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat),
        "-filter_complex",
        (
            "[0:a]"
            "aresample=48000:"
            "async=1:"
            "first_pts=0,"
            "aformat="
            "sample_fmts=fltp:"
            "sample_rates=48000:"
            "channel_layouts=stereo,"
            "volume=1.0"
            "[safeaudio]"
        ),
        "-map", "0:v:0",
        "-map", "[safeaudio]",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        str(FINAL_VIDEO),
    ])

    if not FINAL_VIDEO.exists():
        raise RuntimeError(
            "V5.1 branded final Short was not created."
        )

    final_duration = duration(FINAL_VIDEO)
    core_duration = duration(CORE_VIDEO)

    if final_duration < core_duration + 1.0:
        raise RuntimeError(
            "V5.1 branding validation failed: "
            f"core={core_duration:.2f}s, "
            f"final={final_duration:.2f}s"
        )

    # V5.8: hard-enforce requested final runtime.
    if not (
        FINAL_MIN_SECONDS
        <= final_duration
        <= FINAL_MAX_SECONDS
    ):
        raise RuntimeError(
            "V5.8 final duration gate failed: "
            f"{final_duration:.2f}s. "
            f"Expected {FINAL_MIN_SECONDS:.0f}-"
            f"{FINAL_MAX_SECONDS:.0f}s."
        )

    print("\\n" + "=" * 65)
    print("V5.1 SHORTS BRANDING COMPLETE")
    print("=" * 65)
    print(
        f"Core: {core_duration:.2f}s | "
        f"Intro: {INTRO_SECONDS:.1f}s | "
        f"Outro: {OUTRO_SECONDS:.1f}s | "
        f"Final: {final_duration:.2f}s"
    )


# ============================================================
# SAVE METADATA
# ============================================================

def save_metadata(
    clip,
    plan
):

    metadata = {
        "channel": "ViralSpawnTV",
        "creator": clip[
            "creator"
        ],
        "source": clip[
            "clip_url"
        ],
        "clip_id": clip[
            "clip_id"
        ],
        "source_platform": "kick",
        "rights_status": clip.get(
            "rights_status",
            "unverified"
        ),
        "creator_permission_verified": clip.get(
            "creator_permission_verified",
            False
        ),
        "game_rights_verified": clip.get(
            "game_rights_verified",
            False
        ),
        "public_publish_allowed": clip.get(
            "public_publish_allowed",
            False
        ),
        "acquisition_context": clip.get(
            "acquisition_context",
            "private_pipeline_test"
        ),
        "title": plan[
            "title"
        ],
        "description": plan[
            "description"
        ],
        "headline": plan[
            "headline"
        ],
        "hook_strength": plan.get(
            "hook_strength",
            0
        ),
        "hook_type": plan.get(
            "hook_type",
            "other"
        ),
        "hook_reason": plan.get(
            "hook_reason",
            ""
        ),
        "segment_start": plan[
            "segment_start"
        ],
        "segment_end": plan[
            "segment_end"
        ],
        "payoff_time": plan.get(
            "payoff_time"
        ),
        "pre_render_validation": plan.get(
            "pre_render_validation",
            {},
        ),
        "commentary": plan[
            "commentary"
        ],
        "impacts": plan[
            "impacts"
        ],
        "shorts_branding_version": "5.9.5-reliability-first-best-effort",
        "branding_intro": str(INTRO_IMAGE),
        "branding_outro": str(OUTRO_IMAGE),
        "branding_intro_seconds": INTRO_SECONDS,
        "branding_outro_seconds": OUTRO_SECONDS,
        "publish_status": "NOT_UPLOADED",
    }

    (
        WORK /
        "ViralSpawnTV_V4_metadata.json"
    ).write_text(
        json.dumps(
            metadata,
            indent=2
        ),
        encoding="utf-8"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("\n" + "=" * 65)
    print("VIRALSPAWNTV V4 AUTOMATED SHORTS EDITOR")
    print("=" * 65)

    if not os.getenv(
        "OPENAI_API_KEY"
    ):

        raise RuntimeError(
            "OPENAI_API_KEY is missing."
        )

    if not NEON_FRAME.exists():
        raise RuntimeError(
            f"Missing ViralSpawnTV neon frame asset: {NEON_FRAME}"
        )

    client = OpenAI()

    # --------------------------------------------------------
    # 1. Load media acquired by kick_gaming_acquisition.py
    # --------------------------------------------------------

    clip = acquire_clip()

    # --------------------------------------------------------
    # 2. Analyze video
    # --------------------------------------------------------

    seconds, frames = (
        extract_frames(
            clip["video"]
        )
    )

    # --------------------------------------------------------
    # 3. Extract source audio
    # --------------------------------------------------------

    audio = extract_audio(
        clip["video"]
    )

    # --------------------------------------------------------
    # 4. Timestamped transcription
    # --------------------------------------------------------

    transcript, segments = (
        transcribe_timestamped(
            client,
            audio
        )
    )

    # --------------------------------------------------------
    # 5. AI editing decisions
    # --------------------------------------------------------

    plan = create_plan(
        client,
        clip,
        seconds,
        frames,
        transcript,
        segments
    )

    # --------------------------------------------------------
    # 5A. V5.9.4 pre-render story/payoff verification
    # --------------------------------------------------------

    plan, pre_render_validation = (
        validate_and_repair_story_plan(
            client,
            plan,
            seconds,
            frames,
            segments,
        )
    )

    if not pre_render_validation.get(
        "passed",
        False,
    ):
        print(
            "V5.9.5 RELIABILITY MODE: "
            "pre-render plan is below target, but the source has already "
            "passed the source/music gates. Rendering the repaired "
            "best-effort plan rather than returning no video. | "
            f"{pre_render_validation.get('reason', '')}"
        )
    else:
        print(
            "V5.9.5 PRE-RENDER PLAN GATE: PASSED"
        )

    # --------------------------------------------------------
    # 5B. English-output safety gate
    # --------------------------------------------------------

    generated_text = " ".join([
        str(plan.get("headline", "")),
        str(plan.get("title", "")),
        str(plan.get("description", "")),
        " ".join(
            str(x.get("text", ""))
            for x in plan.get("commentary", [])
        ),
        " ".join(
            str(x.get("text", ""))
            for x in plan.get("impacts", [])
        ),
    ])

    # Common Portuguese/Spanish function words are used only as
    # a last-resort guard. The primary language control is the
    # explicit model instruction above.
    suspicious_words = {
        " você ", " vocês ", " não ", " uma ", " para ",
        " porque ", " então ", " muito ", " está ", " com ",
        " pero ", " porque ", " entonces ", " muy ", " está ",
        " una ", " para ", " con ", " que ",
    }

    normalized_generated = (
        " " + generated_text.lower() + " "
    )

    suspicious_hits = sum(
        1
        for word in suspicious_words
        if word in normalized_generated
    )

    if suspicious_hits >= 4:
        raise RuntimeError(
            "English-output safety gate failed: "
            "generated ViralSpawnTV text appears to be "
            "non-English. Upload stopped."
        )

    # --------------------------------------------------------
    # 6. Generate captions from REAL timestamps
    # --------------------------------------------------------

    if plan.get("english_caption_segments"):
        captions = [
            {
                "start": float(item["start"]),
                "end": float(item["end"]),
                "text": clean_text(
                    item["text"]
                ).upper(),
            }
            for item in plan[
                "english_caption_segments"
            ]
        ]

        (
            WORK /
            "v4_captions.json"
        ).write_text(
            json.dumps(
                captions,
                indent=2
            ),
            encoding="utf-8"
        )

        print(
            f"English caption chunks: "
            f"{len(captions)}"
        )

    else:
        # Fallback for an English source or if the model
        # returns no translated caption segments.
        captions = create_real_captions(
            segments,
            float(
                plan["segment_start"]
            ),
            float(
                plan["segment_end"]
            ),
        )

    # --------------------------------------------------------
    # 7. Generate context-sensitive AI narration
    # --------------------------------------------------------

    beats = generate_voices(
        client,
        plan
    )

    # --------------------------------------------------------
    # 7B. Remove captions that compete with narration
    # --------------------------------------------------------

    captions = suppress_captions_during_narration(
        captions,
        beats
    )

    # --------------------------------------------------------
    # 8. Render
    # --------------------------------------------------------

    render(
        clip,
        plan,
        beats,
        captions
    )

    # --------------------------------------------------------
    # 8B. V5.6 cold-open + branded subscribe outro
    # --------------------------------------------------------

    add_short_brand_bookends()

    # --------------------------------------------------------
    # 9. Save metadata for future YouTube uploader
    # --------------------------------------------------------

    save_metadata(
        clip,
        plan
    )

    print("\n" + "=" * 65)
    print("VIRALSPAWNTV V4 COMPLETE")
    print("=" * 65)

    print(
        "\nFinished Short:"
    )

    print(
        "ViralSpawnTV_Short_V4.mp4"
    )

    print(
        "\nV4 gaming render complete."
    )

    print(
        "\nThe GitHub workflow may now pass this file "
        "to youtube_upload.py for PRIVATE upload only."
    )


if __name__ == "__main__":
    main()
