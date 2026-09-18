import base64
import json
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

CHANNEL = "ayezee"
CREATOR = "AyeZee"
CLIPS_URL = f"https://kick.com/{CHANNEL}/clips"

FINAL_VIDEO = WORK / "ViralSpawnTV_Short_V3.mp4"

FONT = (
    "/usr/share/fonts/truetype/"
    "dejavu/DejaVuSans-Bold.ttf"
)


# ============================================================
# HELPERS
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
        print(result.stderr[-12000:])
        raise RuntimeError("Command failed.")

    return result


def duration(path):

    result = run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    return float(result.stdout.strip())


def encode_image(path):

    return base64.b64encode(
        path.read_bytes()
    ).decode("utf-8")


def clean_text(text):

    text = str(text)

    text = text.replace("'", "")
    text = text.replace(":", " ")
    text = text.replace("%", " percent")

    return text.strip()


# ============================================================
# KICK ACQUISITION
# ============================================================

def acquire_clip():

    print("\n" + "=" * 60)
    print("ACQUIRING AUTHORIZED CLIP")
    print("=" * 60)

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000
            }
        )

        page = context.new_page()

        page.goto(
            CLIPS_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(6000)

        links = page.locator(
            f'a[href^="/{CHANNEL}/clips/clip_"]'
        )

        if links.count() == 0:
            links = page.locator(
                'a[href*="/clips/clip_"]'
            )

        if links.count() == 0:
            raise RuntimeError(
                "No clips found."
            )

        href = links.first.get_attribute("href")

        if not href:
            raise RuntimeError(
                "No clip href."
            )

        match = re.search(
            r"(clip_[A-Za-z0-9]+)",
            href
        )

        if not match:
            raise RuntimeError(
                "Clip ID not found."
            )

        clip_id = match.group(1)

        clip_url = (
            "https://kick.com" + href
            if href.startswith("/")
            else href
        )

        print("Selected:", clip_url)

        page.goto(
            clip_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(6000)

        html = page.content()

        pattern = (
            r'https://clips\.kick\.com/'
            r'clips/[^"\'\\<>\s]+/'
            + re.escape(clip_id)
            + r'/playlist\.m3u8'
        )

        playlists = list(
            dict.fromkeys(
                re.findall(pattern, html)
            )
        )

        if not playlists:
            raise RuntimeError(
                "Clip playlist not found."
            )

        video = WORK / f"{clip_id}.mp4"

        run([
            "ffmpeg",
            "-y",

            "-user_agent",
            (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),

            "-headers",
            (
                "Referer: https://kick.com/\r\n"
                "Origin: https://kick.com\r\n"
            ),

            "-i",
            playlists[0],

            "-c",
            "copy",

            "-movflags",
            "+faststart",

            str(video),
        ])

        browser.close()

    return {
        "creator": CREATOR,
        "clip_id": clip_id,
        "clip_url": clip_url,
        "video": video,
    }


# ============================================================
# SOURCE ANALYSIS
# ============================================================

def extract_frames(video):

    seconds = duration(video)

    timestamps = []

    t = 1.0

    while t < seconds:
        timestamps.append(t)
        t += 4.0

    timestamps = timestamps[:20]

    frames = []

    for i, timestamp in enumerate(timestamps):

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

        frames.append(
            (
                timestamp,
                path
            )
        )

    return seconds, frames


def extract_audio(video):

    audio = WORK / "source_audio.mp3"

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

        "-b:a",
        "64k",

        str(audio),
    ])

    return audio


def transcribe(client, audio):

    print("\nTRANSCRIBING...")

    with open(audio, "rb") as file:

        response = (
            client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=file,
            )
        )

    transcript = response.text

    (
        WORK /
        "transcript.txt"
    ).write_text(
        transcript,
        encoding="utf-8"
    )

    return transcript


# ============================================================
# AI V3 EDIT PLAN
# ============================================================

def create_plan(
    client,
    clip,
    seconds,
    frames,
    transcript
):

    print("\n" + "=" * 60)
    print("AI V3 EDITOR")
    print("=" * 60)

    content = []

    prompt = f"""
You are the automated editor for ViralSpawnTV.

Analyze this real streamer clip.

CREATOR:
{clip["creator"]}

SOURCE:
{clip["clip_url"]}

DURATION:
{seconds:.2f} seconds

TRANSCRIPT:
{transcript}

Representative frames are included after this prompt.

Your objective is to create an extremely engaging
25-45 second vertical YouTube Short.

Do not invent facts, quotes, dollar amounts, events,
or context.

Choose one continuous segment.

============================================================
COMMENTARY
============================================================

Create 2 or 3 SHORT ViralSpawnTV commentary beats.

Leave important creator reactions audible.

For each commentary beat choose ONE delivery:

normal
excited
hype
amused
serious

NORMAL should be used most often.

EXCITED only when something genuinely exciting,
surprising, or tense occurs.

HYPE should be rare and reserved for an exceptional peak.

AMUSED is for genuinely funny situations.

SERIOUS is for dramatic or explanatory moments.

Do not make every line excited.

============================================================
WOW MOMENTS
============================================================

Identify 0-2 genuine impact moments.

Do not force effects when the footage does not deserve one.

Each impact moment has:

time
text
style

"time" is seconds AFTER the selected segment begins.

"text" is 1-5 words of large on-screen text.

The text MUST be factually supported by the clip.

style must be ONE of:

celebration
shock
tension
funny

CELEBRATION:
positive payoff.
Can use flash + zoom + celebration particles.

SHOCK:
surprising reveal.
Use flash + aggressive zoom + giant text.

TENSION:
high pressure or negative realization.
Use punch zoom + giant text.
Avoid celebratory particles.

FUNNY:
funny payoff.
Use bounce-style large text.

Never use celebration effects for losses or negative events.

============================================================
CAPTIONS
============================================================

Create 4-8 short caption chunks representing important
creator dialogue from the selected segment.

Each caption needs:

start
end
text

start/end are seconds AFTER the selected segment begins.

Captions should generally contain 2-7 words.

Do not fabricate dialogue.

Do not caption ViralSpawnTV narration.

============================================================

Return ONLY valid JSON in exactly this structure:

{{
  "segment_start": 0,
  "segment_end": 0,

  "headline": "SHORT HEADLINE",

  "title": "YouTube Shorts title",

  "description": "Description with creator credit and source URL",

  "commentary": [
    {{
      "time": 0.5,
      "text": "commentary",
      "delivery": "normal"
    }}
  ],

  "impacts": [
    {{
      "time": 12.0,
      "text": "NO WAY",
      "style": "shock"
    }}
  ],

  "captions": [
    {{
      "start": 3.0,
      "end": 5.0,
      "text": "creator dialogue"
    }}
  ]
}}

The first commentary beat should normally occur within
the first 1.5 seconds.

Do not cover the entire clip with narration.

Keep commentary concise.

Impact text should be short and visually powerful.

Normally use no more than 2 impact moments.

For gambling footage, describe what happened without
encouraging gambling or portraying gambling as a reliable
way to make money.

The description must contain:

Creator: {clip["creator"]}
Source: {clip["clip_url"]}
"""

    content.append({
        "type": "input_text",
        "text": prompt,
    })

    for timestamp, path in frames:

        content.append({
            "type": "input_text",
            "text": (
                f"Frame approximately "
                f"{timestamp:.1f}s:"
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

    text = response.output_text.strip()

    if text.startswith("```"):

        text = (
            text
            .split("\n", 1)[1]
            .rsplit("```", 1)[0]
        )

    plan = json.loads(text)

    start = float(
        plan["segment_start"]
    )

    end = float(
        plan["segment_end"]
    )

    start = max(
        0,
        min(
            start,
            seconds - 10
        )
    )

    end = max(
        start + 10,
        min(
            end,
            seconds
        )
    )

    if end - start > 45:
        end = start + 45

    plan["segment_start"] = start
    plan["segment_end"] = end

    clip_length = end - start

    # -------------------------------
    # Validate commentary
    # -------------------------------

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
    )[:3]:

        beat_time = float(
            beat.get("time", 0)
        )

        if (
            0 <= beat_time <
            clip_length - 0.5
        ):

            delivery = (
                str(
                    beat.get(
                        "delivery",
                        "normal"
                    )
                )
                .lower()
            )

            if delivery not in valid_delivery:
                delivery = "normal"

            commentary.append({
                "time": beat_time,
                "text": str(
                    beat.get(
                        "text",
                        ""
                    )
                ).strip(),
                "delivery": delivery,
            })

    plan["commentary"] = commentary

    # -------------------------------
    # Validate impacts
    # -------------------------------

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

        impact_time = float(
            impact.get("time", 0)
        )

        style = str(
            impact.get(
                "style",
                "shock"
            )
        ).lower()

        if style not in valid_styles:
            style = "shock"

        if (
            0 <= impact_time <
            clip_length - 0.5
        ):

            impacts.append({
                "time": impact_time,
                "text": clean_text(
                    impact.get(
                        "text",
                        "WOW"
                    )
                ).upper()[:40],
                "style": style,
            })

    plan["impacts"] = impacts

    # -------------------------------
    # Validate captions
    # -------------------------------

    captions = []

    for caption in plan.get(
        "captions",
        []
    )[:10]:

        cstart = float(
            caption.get(
                "start",
                0
            )
        )

        cend = float(
            caption.get(
                "end",
                cstart + 1
            )
        )

        if (
            0 <= cstart <
            clip_length
            and
            cend > cstart
        ):

            cend = min(
                cend,
                clip_length
            )

            captions.append({
                "start": cstart,
                "end": cend,
                "text": clean_text(
                    caption.get(
                        "text",
                        ""
                    )
                ),
            })

    plan["captions"] = captions

    (
        WORK /
        "v3_edit_plan.json"
    ).write_text(
        json.dumps(
            plan,
            indent=2
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            plan,
            indent=2
        )
    )

    return plan


# ============================================================
# EMOTIONAL TTS
# ============================================================

DELIVERY_INSTRUCTIONS = {

    "normal":
        (
            "Young adult American male gaming commentator. "
            "Neutral United States accent. "
            "Conversational and natural. "
            "Clear pronunciation. Medium-fast pace. "
            "Sound like a streamer talking to a friend."
        ),

    "excited":
        (
            "Young adult American male gaming commentator. "
            "Neutral United States accent. "
            "Sound genuinely excited and surprised. "
            "Increase energy and pace slightly. "
            "Emphasize the surprising part naturally. "
            "Do not scream or sound like an announcer."
        ),

    "hype":
        (
            "Young adult American male streamer reacting "
            "to an exceptional moment. "
            "Neutral United States accent. "
            "High genuine energy and excitement. "
            "Speak faster with strong emphasis. "
            "Sound spontaneous, not theatrical. "
            "Do not become difficult to understand."
        ),

    "amused":
        (
            "Young adult American male gaming commentator. "
            "Neutral United States accent. "
            "Sound amused, like you are trying not to laugh. "
            "Use a subtle smile in the delivery. "
            "Natural conversational pace."
        ),

    "serious":
        (
            "Young adult American male gaming commentator. "
            "Neutral United States accent. "
            "Calm, focused, slightly serious delivery. "
            "Speak clearly and deliberately. "
            "Do not sound dramatic or theatrical."
        ),
}


def generate_voices(
    client,
    plan
):

    print("\n" + "=" * 60)
    print("GENERATING EMOTIONAL VOICES")
    print("=" * 60)

    beats = []

    for i, beat in enumerate(
        plan["commentary"]
    ):

        delivery = beat[
            "delivery"
        ]

        output = (
            VOICES /
            f"voice_{i:02d}_{delivery}.mp3"
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
                    "onyx"
                ),

                input=beat["text"],

                instructions=(
                    DELIVERY_INSTRUCTIONS[
                        delivery
                    ]
                ),
            )
        ) as response:

            response.stream_to_file(
                output
            )

        beat["file"] = output

        beat["duration"] = duration(
            output
        )

        beats.append(beat)

        print(
            f"{i}: "
            f"{delivery.upper()} | "
            f"{beat['text']}"
        )

    return beats


# ============================================================
# TEXT FILES
# ============================================================

def make_text_file(
    name,
    text,
    width=None
):

    text = clean_text(text)

    if width:

        text = "\n".join(
            textwrap.wrap(
                text,
                width=width
            )
        )

    path = (
        TEXTS /
        f"{name}.txt"
    )

    path.write_text(
        text,
        encoding="utf-8"
    )

    return path


# ============================================================
# VIDEO FILTER
# ============================================================

def build_video_filter(plan):

    headline = make_text_file(
        "headline",
        str(
            plan["headline"]
        ).upper(),
        18
    )

    credit = make_text_file(
        "credit",
        f"@{CHANNEL}  •  VIRALSPAWNTV"
    )

    filters = [

        # Blurred vertical background
        (
            "[0:v]"
            "scale=1080:1920:"
            "force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            "boxblur=25:12"
            "[bg]"
        ),

        # Main uncropped source
        (
            "[0:v]"
            "scale=1080:-2"
            "[main]"
        ),

        (
            "[bg][main]"
            "overlay=(W-w)/2:(H-h)/2"
            "[base]"
        ),
    ]

    current = "base"

    # --------------------------------------------------------
    # TOP HEADLINE
    # --------------------------------------------------------

    next_label = "headlinevideo"

    filters.append(
        f"[{current}]"
        "drawbox="
        "x=0:y=0:"
        "w=iw:h=245:"
        "color=black@0.48:"
        "t=fill,"
        f"drawtext="
        f"fontfile={FONT}:"
        f"textfile={headline}:"
        "fontcolor=white:"
        "fontsize=64:"
        "line_spacing=8:"
        "x=(w-text_w)/2:"
        "y=58:"
        "borderw=4:"
        "bordercolor=black:"
        "enable='between(t,0,4.5)'"
        f"[{next_label}]"
    )

    current = next_label

    # --------------------------------------------------------
    # DYNAMIC CAPTIONS
    # --------------------------------------------------------

    for i, caption in enumerate(
        plan["captions"]
    ):

        caption_file = make_text_file(
            f"caption_{i:02d}",
            caption["text"].upper(),
            22
        )

        label = f"caption{i}"

        filters.append(
            f"[{current}]"
            f"drawtext="
            f"fontfile={FONT}:"
            f"textfile={caption_file}:"
            f"fontcolor=white:"
            f"fontsize=58:"
            f"line_spacing=8:"
            f"x=(w-text_w)/2:"
            f"y=1420:"
            f"borderw=6:"
            f"bordercolor=black:"
            f"box=1:"
            f"boxcolor=black@0.32:"
            f"boxborderw=18:"
            f"enable='between(t,"
            f"{caption['start']:.3f},"
            f"{caption['end']:.3f})'"
            f"[{label}]"
        )

        current = label

    # --------------------------------------------------------
    # IMPACT / WOW MOMENTS
    # --------------------------------------------------------

    for i, impact in enumerate(
        plan["impacts"]
    ):

        moment = float(
            impact["time"]
        )

        style = impact[
            "style"
        ]

        text_file = make_text_file(
            f"impact_{i:02d}",
            impact["text"],
            14
        )

        #
        # Quick flash for celebration/shock.
        #

        if style in {
            "celebration",
            "shock"
        }:

            flash_label = (
                f"flash{i}"
            )

            filters.append(
                f"[{current}]"
                f"drawbox="
                f"x=0:y=0:"
                f"w=iw:h=ih:"
                f"color=white@0.55:"
                f"t=fill:"
                f"enable='between(t,"
                f"{moment:.3f},"
                f"{moment + 0.10:.3f})'"
                f"[{flash_label}]"
            )

            current = flash_label

        #
        # Giant impact text.
        #

        impact_label = (
            f"impactvideo{i}"
        )

        if style == "celebration":
            fontsize = 105

        elif style == "shock":
            fontsize = 115

        elif style == "tension":
            fontsize = 100

        else:
            fontsize = 95

        filters.append(
            f"[{current}]"
            f"drawtext="
            f"fontfile={FONT}:"
            f"textfile={text_file}:"
            f"fontcolor=white:"
            f"fontsize={fontsize}:"
            f"x=(w-text_w)/2:"
            f"y=(h-text_h)/2:"
            f"borderw=10:"
            f"bordercolor=black:"
            f"box=1:"
            f"boxcolor=black@0.35:"
            f"boxborderw=25:"
            f"enable='between(t,"
            f"{moment:.3f},"
            f"{moment + 1.15:.3f})'"
            f"[{impact_label}]"
        )

        current = impact_label

        #
        # FIREWORK / PARTICLE-STYLE WOW EFFECT
        #
        # Uses expanding circles via FFmpeg drawbox approximations.
        # No external graphics are required.
        #

        if style == "celebration":

            positions = [
                (150, 450),
                (850, 420),
                (240, 1180),
                (820, 1200),
                (530, 380),
                (530, 1280),
            ]

            for p, (x, y) in enumerate(
                positions
            ):

                particle_label = (
                    f"particle{i}_{p}"
                )

                particle_start = (
                    moment +
                    (p * 0.035)
                )

                particle_end = (
                    particle_start +
                    0.55
                )

                filters.append(
                    f"[{current}]"
                    f"drawbox="
                    f"x={x}:"
                    f"y={y}:"
                    f"w=22:h=22:"
                    f"color=white@0.90:"
                    f"t=fill:"
                    f"enable='between(t,"
                    f"{particle_start:.3f},"
                    f"{particle_end:.3f})'"
                    f"[{particle_label}]"
                )

                current = (
                    particle_label
                )

    # --------------------------------------------------------
    # BRANDING
    # --------------------------------------------------------

    filters.append(
        f"[{current}]"
        f"drawtext="
        f"fontfile={FONT}:"
        f"textfile={credit}:"
        f"fontcolor=white:"
        f"fontsize=30:"
        f"x=(w-text_w)/2:"
        f"y=h-92:"
        f"borderw=3:"
        f"bordercolor=black"
        f"[finalvideo]"
    )

    return ";".join(filters)


# ============================================================
# AUDIO FILTER
# ============================================================

def build_audio_filter(beats):

    source_volume = "1"

    for beat in beats:

        start = float(
            beat["time"]
        )

        end = (
            start +
            float(
                beat["duration"]
            )
            +
            0.15
        )

        source_volume += (
            f"*if("
            f"between(t,"
            f"{start:.3f},"
            f"{end:.3f}),"
            f"0.20,"
            f"1)"
        )

    filters = [
        (
            f"[0:a]"
            f"volume='{source_volume}'"
            f"[sourceaudio]"
        )
    ]

    inputs = [
        "[sourceaudio]"
    ]

    for i, beat in enumerate(
        beats
    ):

        delay = int(
            float(
                beat["time"]
            )
            * 1000
        )

        filters.append(
            f"[{i + 1}:a]"
            f"adelay={delay}|{delay},"
            f"volume=1.30"
            f"[voice{i}]"
        )

        inputs.append(
            f"[voice{i}]"
        )

    filters.append(
        "".join(inputs)
        +
        f"amix="
        f"inputs={len(inputs)}:"
        f"duration=first:"
        f"dropout_transition=0"
        f"[finalaudio]"
    )

    return ";".join(filters)


# ============================================================
# RENDER
# ============================================================

def render(
    clip,
    plan,
    beats
):

    print("\n" + "=" * 60)
    print("RENDERING VIRALSPAWNTV V3")
    print("=" * 60)

    start = float(
        plan["segment_start"]
    )

    end = float(
        plan["segment_end"]
    )

    clip_length = end - start

    video_filter = build_video_filter(
        plan
    )

    audio_filter = build_audio_filter(
        beats
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
        str(clip["video"]),
    ]

    for beat in beats:

        command.extend([
            "-i",
            str(
                beat["file"]
            )
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
        "20",

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
        str(clip_length),

        str(FINAL_VIDEO),
    ])

    run(command)

    if not FINAL_VIDEO.exists():

        raise RuntimeError(
            "Final V3 video not created."
        )

    print("\n" + "=" * 60)
    print("V3 SHORT CREATED")
    print("=" * 60)

    print(FINAL_VIDEO)

    print(
        f"Duration: "
        f"{duration(FINAL_VIDEO):.2f}s"
    )

    print(
        f"Size: "
        f"{FINAL_VIDEO.stat().st_size / 1_000_000:.2f} MB"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("\n" + "=" * 60)
    print("VIRALSPAWNTV V3 AUTOMATED EDITOR")
    print("=" * 60)

    client = OpenAI()

    clip = acquire_clip()

    seconds, frames = extract_frames(
        clip["video"]
    )

    audio = extract_audio(
        clip["video"]
    )

    transcript = transcribe(
        client,
        audio
    )

    plan = create_plan(
        client,
        clip,
        seconds,
        frames,
        transcript
    )

    beats = generate_voices(
        client,
        plan
    )

    render(
        clip,
        plan,
        beats
    )

    print("\n" + "=" * 60)
    print("VIRALSPAWNTV V3 COMPLETE")
    print("=" * 60)

    print(
        "\nFinished file:"
    )

    print(
        "ViralSpawnTV_Short_V3.mp4"
    )

    print(
        "\nYouTube auto-publishing remains OFF."
    )


if __name__ == "__main__":
    main()
