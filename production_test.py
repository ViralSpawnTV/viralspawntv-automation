import base64
import json
import os
import pathlib
import re
import subprocess
import textwrap

from openai import OpenAI
from playwright.sync_api import sync_playwright


ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT / "work" / "production"
FRAMES = WORK / "frames"
VOICE_DIR = WORK / "voices"

WORK.mkdir(parents=True, exist_ok=True)
FRAMES.mkdir(parents=True, exist_ok=True)
VOICE_DIR.mkdir(parents=True, exist_ok=True)

CHANNEL = "ayezee"
CREATOR_NAME = "AyeZee"
CLIPS_URL = f"https://kick.com/{CHANNEL}/clips"
FINAL_VIDEO = WORK / "ViralSpawnTV_Short_V2.mp4"

FONT = (
    "/usr/share/fonts/truetype/"
    "dejavu/DejaVuSans-Bold.ttf"
)


# ============================================================
# COMMAND HELPERS
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
        print(result.stderr[-10000:])
        raise RuntimeError("Command failed.")

    return result


def encode_image(path):
    return base64.b64encode(
        path.read_bytes()
    ).decode("utf-8")


def media_duration(path):

    result = run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    return float(result.stdout.strip())


# ============================================================
# ACQUIRE AUTHORIZED CLIP
# ============================================================

def acquire_clip():

    print("\n" + "=" * 60)
    print("ACQUIRING AUTHORIZED KICK CLIP")
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
                "No clips discovered."
            )

        href = links.first.get_attribute("href")

        if not href:
            raise RuntimeError(
                "Clip URL missing."
            )

        match = re.search(
            r"(clip_[A-Za-z0-9]+)",
            href
        )

        if not match:
            raise RuntimeError(
                "Clip ID missing."
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
                "Selected clip playlist not found."
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
            "-c", "copy",
            "-movflags", "+faststart",
            str(video),
        ])

        browser.close()

    return {
        "creator": CREATOR_NAME,
        "clip_id": clip_id,
        "clip_url": clip_url,
        "video": video,
    }


# ============================================================
# FRAMES
# ============================================================

def extract_frames(video):

    seconds = media_duration(video)

    timestamps = []

    t = 1.0

    while t < seconds:
        timestamps.append(t)
        t += 4.0

    timestamps = timestamps[:20]

    frames = []

    for index, timestamp in enumerate(timestamps):

        path = (
            FRAMES /
            f"frame_{index:02d}_{timestamp:.1f}.jpg"
        )

        run([
            "ffmpeg",
            "-y",
            "-ss", str(timestamp),
            "-i", str(video),
            "-frames:v", "1",
            "-vf", "scale=640:-2",
            "-q:v", "4",
            str(path),
        ])

        frames.append((timestamp, path))

    return seconds, frames


# ============================================================
# TRANSCRIPTION
# ============================================================

def extract_audio(video):

    audio = WORK / "source_audio.mp3"

    run([
        "ffmpeg",
        "-y",
        "-i", str(video),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "64k",
        str(audio),
    ])

    return audio


def transcribe(client, audio):

    with open(audio, "rb") as file:

        response = (
            client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=file,
            )
        )

    transcript = response.text

    (WORK / "transcript.txt").write_text(
        transcript,
        encoding="utf-8",
    )

    return transcript


# ============================================================
# V2 EDIT PLAN
# ============================================================

def create_edit_plan(
    client,
    clip,
    seconds,
    frames,
    transcript
):

    print("\n" + "=" * 60)
    print("BUILDING V2 EDIT PLAN")
    print("=" * 60)

    content = []

    prompt = f"""
You are the editor of ViralSpawnTV.

Create a highly engaging YouTube Shorts edit from this
actual streamer clip.

Creator: {clip["creator"]}
Source: {clip["clip_url"]}
Duration: {seconds:.2f} seconds

TRANSCRIPT:
{transcript}

Representative frames follow this prompt.

Choose ONE continuous source segment between 25 and
45 seconds long.

Do not invent events, dialogue, dollar amounts, or context.

The finished video should feel like an original commentary
Short rather than narration simply covering the entire clip.

Use this structure when the footage supports it:

1. Very short ViralSpawnTV hook.
2. Let the creator's original audio/reaction play.
3. Brief ViralSpawnTV context/commentary.
4. Let the important source payoff play.
5. Optional short closing commentary.

Commentary should be concise. Preserve important streamer
dialogue and reactions.

For gambling footage, report what occurs without encouraging
gambling or presenting the activity as a way to make money.

Return ONLY valid JSON using exactly this structure:

{{
  "segment_start": 0,
  "segment_end": 0,
  "headline": "MAXIMUM 6 WORD HEADLINE",
  "title": "YouTube title",
  "description": "YouTube description including creator credit and source URL",
  "commentary": [
    {{
      "time": 0.5,
      "text": "short narration"
    }},
    {{
      "time": 10.0,
      "text": "short narration"
    }}
  ]
}}

IMPORTANT:

"time" is seconds AFTER the selected segment begins,
not seconds in the original video.

Use 2 or 3 commentary beats.

Each commentary beat should normally be 5-18 words.

The first commentary beat should start between
0 and 1.5 seconds.

Leave meaningful gaps between commentary beats so viewers
can hear the creator.

Do not narrate continuously.

Do not place commentary over the most important creator
reaction if it can reasonably be avoided.

The headline must create curiosity without making a false
claim.

Description must include:
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
                f"Frame at approximately "
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

    text = response.output_text.strip()

    if text.startswith("```"):
        text = (
            text.split("\n", 1)[1]
            .rsplit("```", 1)[0]
        )

    plan = json.loads(text)

    start = float(plan["segment_start"])
    end = float(plan["segment_end"])

    start = max(
        0,
        min(start, seconds - 10)
    )

    end = min(
        seconds,
        max(end, start + 10)
    )

    if end - start > 45:
        end = start + 45

    plan["segment_start"] = start
    plan["segment_end"] = end

    clip_length = end - start

    valid_beats = []

    for beat in plan["commentary"]:

        beat_time = float(
            beat["time"]
        )

        if (
            beat_time >= 0
            and
            beat_time < clip_length - 1
        ):
            valid_beats.append({
                "time": beat_time,
                "text": str(
                    beat["text"]
                ).strip(),
            })

    plan["commentary"] = valid_beats[:3]

    (WORK / "edit_plan.json").write_text(
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
# TTS
# ============================================================

def generate_voice_beats(
    client,
    plan
):

    print("\n" + "=" * 60)
    print("GENERATING TIMED COMMENTARY")
    print("=" * 60)

    beats = []

    for index, beat in enumerate(
        plan["commentary"]
    ):

        output = (
            VOICE_DIR /
            f"voice_{index:02d}.mp3"
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
                    "Young adult American male. "
                    "Neutral United States accent. "
                    "Clear natural pronunciation. "
                    "Conversational gaming commentary. "
                    "Medium-fast pace. "
                    "Confident and energetic without shouting. "
                    "Do not sound like a commercial, radio "
                    "announcer, or documentary narrator. "
                    "Sound like an American streamer reacting "
                    "naturally to a clip."
                ),
            )
        ) as response:

            response.stream_to_file(
                output
            )

        beat["file"] = output
        beat["duration"] = media_duration(
            output
        )

        beats.append(beat)

        print(
            f"Beat {index}: "
            f"{beat['time']:.2f}s / "
            f"{beat['duration']:.2f}s"
        )

    return beats


# ============================================================
# TEXT
# ============================================================

def prepare_text(plan):

    headline = str(
        plan["headline"]
    ).upper()

    headline = "\n".join(
        textwrap.wrap(
            headline,
            width=18
        )
    )

    headline_file = (
        WORK /
        "headline.txt"
    )

    headline_file.write_text(
        headline,
        encoding="utf-8"
    )

    credit_file = (
        WORK /
        "credit.txt"
    )

    credit_file.write_text(
        f"@{CHANNEL}  •  VIRALSPAWNTV",
        encoding="utf-8"
    )

    return (
        headline_file,
        credit_file
    )


# ============================================================
# AUDIO MIX
# ============================================================

def build_audio_filter(beats):

    filters = []

    #
    # Source audio begins at full volume.
    # Each narration window ducks source audio.
    #

    volume_expression = "1"

    for beat in beats:

        start = float(
            beat["time"]
        )

        end = (
            start +
            float(beat["duration"]) +
            0.15
        )

        volume_expression += (
            f"*if(between(t,{start:.3f},"
            f"{end:.3f}),0.22,1)"
        )

    filters.append(
        f"[0:a]volume='{volume_expression}'"
        f"[sourceaudio]"
    )

    mix_inputs = [
        "[sourceaudio]"
    ]

    for index, beat in enumerate(beats):

        delay_ms = int(
            float(beat["time"]) * 1000
        )

        filters.append(
            f"[{index + 1}:a]"
            f"adelay={delay_ms}|{delay_ms},"
            f"volume=1.30"
            f"[voice{index}]"
        )

        mix_inputs.append(
            f"[voice{index}]"
        )

    joined = "".join(
        mix_inputs
    )

    filters.append(
        f"{joined}"
        f"amix=inputs={len(mix_inputs)}:"
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
    beats,
    headline_file,
    credit_file
):

    print("\n" + "=" * 60)
    print("RENDERING V2 SHORT")
    print("=" * 60)

    start = float(
        plan["segment_start"]
    )

    end = float(
        plan["segment_end"]
    )

    length = end - start

    #
    # Background fills vertical screen.
    # Main 16:9 video remains uncropped.
    #

    video_filter = (
        "[0:v]"
        "scale=1080:1920:"
        "force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "boxblur=25:12"
        "[bg];"

        "[0:v]"
        "scale=1080:-2"
        "[main];"

        "[bg][main]"
        "overlay=(W-w)/2:(H-h)/2,"

        "drawbox="
        "x=0:y=0:"
        "w=iw:h=250:"
        "color=black@0.48:"
        "t=fill,"

        f"drawtext="
        f"fontfile={FONT}:"
        f"textfile={headline_file}:"
        "fontcolor=white:"
        "fontsize=64:"
        "line_spacing=8:"
        "x=(w-text_w)/2:"
        "y=58:"
        "borderw=4:"
        "bordercolor=black:"
        "enable='between(t,0,4.5)',"

        f"drawtext="
        f"fontfile={FONT}:"
        f"textfile={credit_file}:"
        "fontcolor=white:"
        "fontsize=32:"
        "x=(w-text_w)/2:"
        "y=h-95:"
        "borderw=3:"
        "bordercolor=black"
        "[finalvideo]"
    )

    audio_filter = build_audio_filter(
        beats
    )

    filter_complex = (
        video_filter
        + ";"
        + audio_filter
    )

    command = [
        "ffmpeg",
        "-y",

        "-ss",
        str(start),

        "-t",
        str(length),

        "-i",
        str(clip["video"]),
    ]

    #
    # Each commentary beat becomes another FFmpeg input.
    #

    for beat in beats:
        command.extend([
            "-i",
            str(beat["file"])
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
        str(length),

        str(FINAL_VIDEO),
    ])

    run(command)

    if not FINAL_VIDEO.exists():
        raise RuntimeError(
            "V2 video wasn't created."
        )

    print("\nFINAL VIDEO:")
    print(FINAL_VIDEO)

    print(
        f"Duration: "
        f"{media_duration(FINAL_VIDEO):.2f}s"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("\n" + "=" * 60)
    print("VIRALSPAWNTV V2")
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

    plan = create_edit_plan(
        client,
        clip,
        seconds,
        frames,
        transcript
    )

    beats = generate_voice_beats(
        client,
        plan
    )

    headline, credit = prepare_text(
        plan
    )

    render(
        clip,
        plan,
        beats,
        headline,
        credit
    )

    print("\n" + "=" * 60)
    print("V2 COMPLETE")
    print("=" * 60)

    print(
        "\nViralSpawnTV_Short_V2.mp4"
    )

    print(
        "\nYouTube publishing remains OFF."
    )


if __name__ == "__main__":
    main()
