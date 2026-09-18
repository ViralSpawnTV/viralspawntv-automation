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
# PATHS / SETTINGS
# ============================================================

ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT / "work" / "production"
FRAMES = WORK / "frames"

WORK.mkdir(parents=True, exist_ok=True)
FRAMES.mkdir(parents=True, exist_ok=True)

CHANNEL = "ayezee"
CREATOR_NAME = "AyeZee"
CLIPS_URL = f"https://kick.com/{CHANNEL}/clips"

FINAL_VIDEO = WORK / "ViralSpawnTV_Short.mp4"


# ============================================================
# COMMAND HELPER
# ============================================================

def run(command):

    print()
    print("RUNNING:")
    print(" ".join(str(x) for x in command))

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:

        print()
        print("STDERR:")
        print(result.stderr[-8000:])

        raise RuntimeError(
            "Command failed."
        )

    return result


def encode_image(path):

    return base64.b64encode(
        path.read_bytes()
    ).decode("utf-8")


# ============================================================
# ACQUIRE AUTHORIZED KICK CLIP
# ============================================================

def acquire_clip():

    print()
    print("=" * 60)
    print("ACQUIRING KICK CLIP")
    print("=" * 60)

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000,
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

        count = links.count()

        print(
            f"Found {count} clips."
        )

        if count == 0:

            raise RuntimeError(
                "No Kick clips discovered."
            )

        href = (
            links
            .first
            .get_attribute("href")
        )

        if not href:

            raise RuntimeError(
                "Selected clip has no href."
            )

        match = re.search(
            r"(clip_[A-Za-z0-9]+)",
            href
        )

        if not match:

            raise RuntimeError(
                "Could not identify clip ID."
            )

        clip_id = match.group(1)

        clip_url = (
            "https://kick.com" + href
            if href.startswith("/")
            else href
        )

        print("Selected clip:")
        print(clip_url)

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
                re.findall(
                    pattern,
                    html
                )
            )
        )

        if not playlists:

            raise RuntimeError(
                "Clip media playlist not found."
            )

        playlist = playlists[0]

        video = (
            WORK /
            f"{clip_id}.mp4"
        )

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
            playlist,

            "-c",
            "copy",

            "-movflags",
            "+faststart",

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
# VIDEO INFO
# ============================================================

def duration(video):

    result = run([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ])

    return float(
        result.stdout.strip()
    )


# ============================================================
# FRAME EXTRACTION
# ============================================================

def extract_frames(video):

    print()
    print("=" * 60)
    print("EXTRACTING FRAMES")
    print("=" * 60)

    seconds = duration(video)

    timestamps = []

    current = 1.0

    while current < seconds:

        timestamps.append(current)

        current += 5.0

    timestamps = timestamps[:16]

    frames = []

    for index, timestamp in enumerate(
        timestamps
    ):

        path = (
            FRAMES /
            f"frame_{index:02d}.jpg"
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

    return (
        seconds,
        frames
    )


# ============================================================
# AUDIO EXTRACTION
# ============================================================

def extract_audio(video):

    audio = (
        WORK /
        "source_audio.mp3"
    )

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


# ============================================================
# TRANSCRIPTION
# ============================================================

def transcribe(client, audio):

    print()
    print("=" * 60)
    print("TRANSCRIBING CLIP")
    print("=" * 60)

    with open(
        audio,
        "rb"
    ) as file:

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
        encoding="utf-8",
    )

    return transcript


# ============================================================
# AI VIDEO ANALYSIS
# ============================================================

def analyze_clip(
    client,
    clip,
    seconds,
    frames,
    transcript
):

    print()
    print("=" * 60)
    print("ANALYZING VIDEO")
    print("=" * 60)

    content = []

    prompt = f"""
You are producing a YouTube Short for ViralSpawnTV.

Analyze this actual streamer clip.

Creator:
{clip["creator"]}

Source:
{clip["clip_url"]}

Duration:
{seconds:.2f} seconds

Transcript:

{transcript}

Representative video frames are supplied below.

Choose the strongest continuous 20-45 second segment.

It should contain the most entertaining, surprising,
funny, interesting, or noteworthy portion.

Do not invent dialogue or events.

Write original commentary that adds context,
observation, explanation, or humor.

If gambling appears in the footage, describe what happens
rather than encouraging viewers to gamble.

Return ONLY valid JSON.

Exactly these keys:

segment_start
segment_end
hook
narration
title
description
caption_top

segment_start and segment_end must be numbers.

hook:
Maximum 8 words.

narration:
25-50 words.
Natural young American gaming-commentary style.

title:
YouTube Shorts title.

description:
Credit the creator and include:
{clip["clip_url"]}

caption_top:
Maximum 7 words in uppercase.
"""

    content.append({
        "type": "input_text",
        "text": prompt,
    })

    for timestamp, path in frames:

        content.append({
            "type": "input_text",
            "text": (
                f"Video frame at "
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

    text = (
        response
        .output_text
        .strip()
    )

    if text.startswith("```"):

        text = (
            text
            .split("\n", 1)[1]
            .rsplit("```", 1)[0]
        )

    package = json.loads(text)

    # Safety bounds.
    start = float(
        package["segment_start"]
    )

    end = float(
        package["segment_end"]
    )

    start = max(
        0.0,
        min(
            start,
            seconds - 5
        )
    )

    end = max(
        start + 5,
        min(
            end,
            seconds
        )
    )

    # Keep the final Short reasonable.
    if end - start > 45:

        end = start + 45

    package["segment_start"] = start
    package["segment_end"] = end

    (
        WORK /
        "analysis.json"
    ).write_text(
        json.dumps(
            package,
            indent=2
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            package,
            indent=2
        )
    )

    return package


# ============================================================
# VOICEOVER
# ============================================================

def create_voice(
    client,
    package
):

    print()
    print("=" * 60)
    print("GENERATING VOICE")
    print("=" * 60)

    narration = (
        package["hook"].strip()
        + " "
        + package["narration"].strip()
    )

    output = (
        WORK /
        "viralspawntv_voice.mp3"
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

            input=narration,

            instructions=(
                "Young adult American male. "
                "Neutral United States accent. "
                "Very clear pronunciation. "
                "Natural gaming commentary. "
                "Medium-fast conversational pace. "
                "Energetic but not exaggerated. "
                "Sound like a normal American streamer "
                "telling a friend about the clip. "
                "Do not sound like a radio announcer."
            ),
        )
    ) as response:

        response.stream_to_file(
            output
        )

    return output


# ============================================================
# CREATE TEXT FILES FOR FFMPEG
# ============================================================

def prepare_text(package):

    hook = package[
        "caption_top"
    ].upper()

    hook = "\n".join(
        textwrap.wrap(
            hook,
            width=20
        )
    )

    hook_file = (
        WORK /
        "hook.txt"
    )

    hook_file.write_text(
        hook,
        encoding="utf-8",
    )

    creator_file = (
        WORK /
        "creator.txt"
    )

    creator_file.write_text(
        f"@{CHANNEL} • VIRALSPAWNTV",
        encoding="utf-8",
    )

    return (
        hook_file,
        creator_file
    )


# ============================================================
# RENDER VERTICAL SHORT
# ============================================================

def render_short(
    clip,
    package,
    voice,
    hook_file,
    creator_file
):

    print()
    print("=" * 60)
    print("RENDERING VIRALSPAWNTV SHORT")
    print("=" * 60)

    start = float(
        package["segment_start"]
    )

    end = float(
        package["segment_end"]
    )

    clip_length = (
        end -
        start
    )

    # Font supplied by Ubuntu.
    font = (
        "/usr/share/fonts/truetype/"
        "dejavu/DejaVuSans-Bold.ttf"
    )

    #
    # VIDEO:
    #
    # Background = enlarged blurred 16:9 video.
    # Foreground = original 16:9 video centered.
    #
    # This avoids stretching the source.
    #

    filter_complex = (
        "[0:v]"
        "scale=1080:1920:"
        "force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "boxblur=20:10"
        "[bg];"

        "[0:v]"
        "scale=1080:-2"
        "[fg];"

        "[bg][fg]"
        "overlay="
        "(W-w)/2:"
        "(H-h)/2,"
        
        "drawbox="
        "x=0:y=0:"
        "w=iw:h=240:"
        "color=black@0.50:"
        "t=fill,"

        f"drawtext="
        f"fontfile={font}:"
        f"textfile={hook_file}:"
        "fontcolor=white:"
        "fontsize=64:"
        "line_spacing=10:"
        "x=(w-text_w)/2:"
        "y=65:"
        "borderw=4:"
        "bordercolor=black:"
        "enable='between(t,0,5)',"

        f"drawtext="
        f"fontfile={font}:"
        f"textfile={creator_file}:"
        "fontcolor=white:"
        "fontsize=34:"
        "x=(w-text_w)/2:"
        "y=h-105:"
        "borderw=3:"
        "bordercolor=black"
        "[video];"

        # Original clip audio reduced underneath commentary.
        "[0:a]"
        "volume=0.38"
        "[original];"

        # Voice starts immediately.
        "[1:a]"
        "volume=1.35"
        "[voice];"

        # Mix original audio and commentary.
        "[original][voice]"
        "amix="
        "inputs=2:"
        "duration=first:"
        "dropout_transition=2"
        "[audio]"
    )

    run([
        "ffmpeg",
        "-y",

        # Start at AI-selected segment.
        "-ss",
        str(start),

        "-t",
        str(clip_length),

        "-i",
        str(
            clip["video"]
        ),

        "-i",
        str(voice),

        "-filter_complex",
        filter_complex,

        "-map",
        "[video]",

        "-map",
        "[audio]",

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

        "-shortest",

        str(
            FINAL_VIDEO
        ),
    ])

    if not FINAL_VIDEO.exists():

        raise RuntimeError(
            "Final video was not created."
        )

    size_mb = (
        FINAL_VIDEO
        .stat()
        .st_size
        /
        1_000_000
    )

    print()
    print("=" * 60)
    print("FINAL SHORT CREATED")
    print("=" * 60)

    print(FINAL_VIDEO)

    print(
        f"Size: {size_mb:.2f} MB"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("=" * 60)
    print("VIRALSPAWNTV FULL PRODUCTION")
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

    package = analyze_clip(
        client,
        clip,
        seconds,
        frames,
        transcript
    )

    voice = create_voice(
        client,
        package
    )

    hook_file, creator_file = (
        prepare_text(
            package
        )
    )

    render_short(
        clip,
        package,
        voice,
        hook_file,
        creator_file
    )

    print()
    print("=" * 60)
    print("VIRALSPAWNTV SHORT READY")
    print("=" * 60)

    print()
    print(
        "Finished video:"
    )

    print(
        "ViralSpawnTV_Short.mp4"
    )

    print()
    print(
        "YouTube upload remains OFF."
    )


if __name__ == "__main__":
    main()
