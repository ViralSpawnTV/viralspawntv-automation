import base64
import json
import math
import os
import pathlib
import re
import subprocess
import textwrap

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

    timestamps = []

    t = 1.0

    while t < seconds:

        timestamps.append(t)

        # More visual context than V3.
        t += 3.0

    timestamps = timestamps[:24]

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
    print("VIRALSPAWNTV V4 AI EDITOR")
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
fast-paced, professional 25-45 second YouTube Short.

Do not invent facts.

Do not invent quotes.

Do not invent dollar amounts.

Do not claim something happened unless the transcript
or visible video supports it.

============================================================
SELECT THE CLIP
============================================================

Choose ONE continuous 25-45 second segment.

OPENING-HOOK PRIORITY:
The first 1-2 seconds are the most important part of the Short.
Start the selected segment at the earliest moment that creates immediate
curiosity, tension, surprise, danger, comedy, or visible action.

Do NOT spend the opening on dead air, menus, walking, waiting, greetings,
slow setup, or context that can be understood later.

The viewer should have a clear reason to keep watching before second 1 ends.

Do not reveal the full payoff immediately unless the payoff itself is the
hook and the remaining clip still has a compelling escalation or reaction.

Choose the portion with the strongest story arc that ALSO supports this
immediate cold open.

The selected segment should ideally contain:

instant hook
setup or context
tension/escalation
payoff or reaction

============================================================
VIRALSPAWNTV COMMENTARY
============================================================

Create 3-5 short original commentary beats.

Each beat must add ORIGINAL ViralSpawnTV value: explain strategy, build the
story, point out a meaningful gameplay decision, react to a specific moment,
or connect setup to payoff. Do not merely restate what the viewer can already
see. Make every line specific to THIS clip so the narration would not make
sense pasted onto a different gaming clip.

The FIRST commentary beat is the opening hook.

It must occur 0.10-0.75 seconds after the selected segment begins.

It should be one short, natural sentence or fragment that creates curiosity
or establishes stakes based ONLY on what the real clip supports.

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
Prefer roughly 6-14 useful caption moments across a 25-45 second Short.
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

HEADLINE:
2-6 words.
This is the on-screen opening hook, not a generic label or summary.
It MUST communicate a specific stake, danger, challenge, decision, or
unresolved question that a viewer can understand immediately.

Do NOT use vague one-word hooks such as:
"SAVE?"
"CLUTCH?"
"WHAT?"
"WHY?"
"INSANE"

Prefer specific, truthful hooks such as:
"CAN HE SAVE THIS?"
"ONE SHOT TO SURVIVE"
"THIS SHOULD BE OVER"
"HE HAS NO WAY OUT"
"THEY THINK HE'S DONE"

Never invent stakes that the footage does not support.
Avoid generic phrases such as "NO WAY", "INSANE CLIP", or "WATCH THIS".

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

  "headline": "HEADLINE",

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

    start = max(
        0.0,
        min(
            start,
            max(
                0,
                seconds - 25
            )
        )
    )

    end = min(
        seconds,
        max(
            end,
            start + 25
        )
    )

    if end - start > 45:
        end = start + 45

    plan["segment_start"] = start
    plan["segment_end"] = end

    clip_length = end - start

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

    # The first narration line is the cold-open hook.
    # Keep it inside the first 0.10-0.75 seconds even if
    # the planner returns a slightly later timestamp.
    if commentary:
        commentary[0]["time"] = max(
            0.10,
            min(
                float(
                    commentary[0]["time"]
                ),
                0.75,
            ),
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
        "v4_headline",
        str(
            plan["headline"]
        ).upper(),
        width=18,
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

    current = "composite"

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
        "x=0:"
        "y=0:"
        "w=iw:"
        "h=245:"
        "color=black@0.48:"
        "t=fill:"
        "enable='between(t,0,4.5)',"

        "drawtext="
        f"fontfile={FONT}:"
        f"textfile={headline_file}:"
        "fontcolor=white:"
        "fontsize=64:"
        "line_spacing=8:"
        "x=(w-text_w)/2:"
        "y=58:"
        "borderw=5:"
        "bordercolor=black:"
        "enable='between(t,0,4.5)'"
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
        "segment_start": plan[
            "segment_start"
        ],
        "segment_end": plan[
            "segment_end"
        ],
        "commentary": plan[
            "commentary"
        ],
        "impacts": plan[
            "impacts"
        ],
        "shorts_branding_version": "5.7-larger-gameplay-stronger-hook",
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
