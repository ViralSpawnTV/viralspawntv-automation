import base64
import json
import os
import pathlib
import re
import subprocess

from openai import OpenAI
from playwright.sync_api import sync_playwright


ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT / "work" / "production"
FRAMES = WORK / "frames"

WORK.mkdir(parents=True, exist_ok=True)
FRAMES.mkdir(parents=True, exist_ok=True)

CHANNEL = "ayezee"
CLIPS_URL = f"https://kick.com/{CHANNEL}/clips"


# ============================================================
# HELPERS
# ============================================================

def run(command):

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:

        print(result.stderr[-5000:])

        raise RuntimeError(
            "Command failed: "
            + " ".join(command)
        )

    return result


def encode_image(path):

    return base64.b64encode(
        path.read_bytes()
    ).decode("utf-8")


# ============================================================
# ACQUIRE KICK CLIP
# ============================================================

def acquire_clip():

    print()
    print("=" * 60)
    print("ACQUIRING AUTHORIZED KICK CLIP")
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
            f"Found {count} public Kick clips."
        )

        if count == 0:

            raise RuntimeError(
                "No clips discovered."
            )

        href = (
            links
            .first
            .get_attribute("href")
        )

        if not href:

            raise RuntimeError(
                "Selected clip has no URL."
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

        print("Selected:")
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

        matches = list(
            dict.fromkeys(
                re.findall(
                    pattern,
                    html
                )
            )
        )

        if not matches:

            raise RuntimeError(
                "Clip playlist not found."
            )

        playlist = matches[0]

        video = WORK / f"{clip_id}.mp4"

        print("Acquiring MP4...")

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

    if not video.exists():

        raise RuntimeError(
            "MP4 acquisition failed."
        )

    print("MP4 ready:")
    print(video)

    return {
        "creator": "AyeZee",
        "clip_id": clip_id,
        "clip_url": clip_url,
        "video": video,
    }


# ============================================================
# VIDEO DURATION
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
# EXTRACT REPRESENTATIVE FRAMES
# ============================================================

def extract_frames(video):

    print()
    print("=" * 60)
    print("EXTRACTING VIDEO FRAMES")
    print("=" * 60)

    seconds = duration(video)

    print(
        f"Source duration: {seconds:.2f}s"
    )

    # Sample roughly every 5 seconds.
    timestamps = []

    t = 1.0

    while t < seconds:

        timestamps.append(t)

        t += 5.0

    # Keep API request reasonable.
    timestamps = timestamps[:16]

    frame_paths = []

    for index, timestamp in enumerate(
        timestamps
    ):

        path = (
            FRAMES /
            f"frame_{index:02d}_{timestamp:.1f}.jpg"
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
            "scale=768:-2",
            "-q:v",
            "3",
            str(path),
        ])

        frame_paths.append(
            (
                timestamp,
                path
            )
        )

    print(
        f"Extracted {len(frame_paths)} frames."
    )

    return (
        seconds,
        frame_paths
    )


# ============================================================
# EXTRACT AUDIO
# ============================================================

def extract_audio(video):

    print()
    print("=" * 60)
    print("EXTRACTING AUDIO")
    print("=" * 60)

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


# ============================================================
# TRANSCRIBE SOURCE AUDIO
# ============================================================

def transcribe(client, audio):

    print()
    print("=" * 60)
    print("TRANSCRIBING SOURCE")
    print("=" * 60)

    with open(
        audio,
        "rb"
    ) as audio_file:

        result = (
            client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_file,
            )
        )

    transcript = result.text

    (
        WORK /
        "transcript.txt"
    ).write_text(
        transcript,
        encoding="utf-8",
    )

    print(transcript)

    return transcript


# ============================================================
# ANALYZE ACTUAL CLIP
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
    print("ANALYZING ACTUAL VIDEO")
    print("=" * 60)

    content = []

    prompt = f"""
You are the video producer for ViralSpawnTV.

You are analyzing an actual streamer clip.

Creator:
{clip["creator"]}

Original clip:
{clip["clip_url"]}

Source duration:
{seconds:.2f} seconds

TRANSCRIPT:

{transcript}

I am also providing representative frames from the video.
Each frame is labeled with its approximate timestamp.

Your job is to choose the strongest segment for a
YouTube Short.

Prefer approximately 20-45 seconds.

The segment should make sense on its own and should contain
the funniest, most surprising, most interesting, or most
engaging moment available.

Do NOT invent anything not supported by the transcript
and frames.

Then create original ViralSpawnTV commentary.

The commentary must add context, observation, humor,
explanation, or reaction. Do not merely restate what the
streamer says.

Return ONLY valid JSON with exactly these keys:

segment_start
segment_end
hook
narration
title
description
caption_top

Rules:

segment_start:
Number of seconds from beginning of source.

segment_end:
Number of seconds from beginning of source.

hook:
Very short opening hook.

narration:
Approximately 25-55 words.
Young American gaming/commentary style.
Natural and conversational.

title:
YouTube Shorts title.
Do not make unsupported claims.

description:
Short description.
Credit the original creator.
Include the original clip URL.
Do not claim ownership of original footage.

caption_top:
Very short uppercase on-screen hook.
Maximum 7 words.
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
# CREATE VOICEOVER
# ============================================================

def create_voice(
    client,
    package
):

    print()
    print("=" * 60)
    print("GENERATING VIRALSPAWNTV VOICE")
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
                "Speak as a young adult American male "
                "gaming commentator. "
                "Use a neutral United States accent. "
                "Speak clearly and naturally. "
                "Medium-fast conversational pace. "
                "Crisp pronunciation. "
                "Energetic enough for YouTube Shorts "
                "without shouting. "
                "Do not use an Indian, British, "
                "Australian, or exaggerated accent. "
                "Do not sound like a radio announcer. "
                "Sound like a normal American streamer "
                "explaining a wild clip to a friend."
            ),
        )
    ) as response:

        response.stream_to_file(
            output
        )

    print(
        "Voice generated:"
    )

    print(output)

    return output


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("=" * 60)
    print("VIRALSPAWNTV PRODUCTION TEST")
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

    print()
    print("=" * 60)
    print("PRODUCTION TEST COMPLETE")
    print("=" * 60)

    print()
    print("Review these artifact files:")

    print(
        "1. analysis.json"
    )

    print(
        "2. transcript.txt"
    )

    print(
        "3. viralspawntv_voice.mp3"
    )

    print()
    print(
        "If the selected segment, commentary, "
        "and voice are good, the next stage "
        "will render the finished Short."
    )


if __name__ == "__main__":
    main()
