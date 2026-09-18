import os
import json
import hashlib
import pathlib
import requests

from datetime import datetime, timezone
from openai import OpenAI


# ============================================================
# VIRALSPAWNTV AUTOMATION
# Stage 1:
# Twitch discovery + ranking + OpenAI commentary generation
# ============================================================

ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT / "work"
OUT = ROOT / "output"
DATA = ROOT / "data"

WORK.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)


# ============================================================
# CONFIG
# ============================================================

def load_config():
    p = ROOT / "config.json"

    if not p.exists():
        raise SystemExit("Missing config.json.")

    return json.loads(p.read_text())


# ============================================================
# HISTORY
# ============================================================

def history():
    p = DATA / "history.json"

    if p.exists():
        return json.loads(p.read_text())

    return {"processed": []}


def save_history(h):
    """
    We will use this later after an actual finished
    ViralSpawnTV video has been successfully created.
    """
    (DATA / "history.json").write_text(
        json.dumps(h, indent=2)
    )


# ============================================================
# TWITCH AUTHENTICATION
# ============================================================

def twitch_token():
    client_id = os.environ["TWITCH_CLIENT_ID"]
    client_secret = os.environ["TWITCH_CLIENT_SECRET"]

    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )

    r.raise_for_status()

    return r.json()["access_token"]


def twitch_headers(token):
    return {
        "Client-ID": os.environ["TWITCH_CLIENT_ID"],
        "Authorization": f"Bearer {token}",
    }


# ============================================================
# TWITCH USER LOOKUP
# ============================================================

def get_broadcaster_id(username, token):
    r = requests.get(
        "https://api.twitch.tv/helix/users",
        headers=twitch_headers(token),
        params={
            "login": username
        },
        timeout=30,
    )

    r.raise_for_status()

    data = r.json().get("data", [])

    if not data:
        print(f"Twitch user not found: {username}")
        return None

    return data[0]["id"]


# ============================================================
# GET TODAY'S CLIPS
# ============================================================

def get_daily_clips(username, token):

    broadcaster_id = get_broadcaster_id(
        username,
        token
    )

    if not broadcaster_id:
        return []

    now = datetime.now(timezone.utc)

    start = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    params = {
        "broadcaster_id": broadcaster_id,
        "started_at": start.isoformat().replace(
            "+00:00",
            "Z"
        ),
        "ended_at": now.isoformat().replace(
            "+00:00",
            "Z"
        ),
        "first": 100,
    }

    r = requests.get(
        "https://api.twitch.tv/helix/clips",
        headers=twitch_headers(token),
        params=params,
        timeout=30,
    )

    r.raise_for_status()

    clips = []

    for clip in r.json().get("data", []):

        clips.append(
            {
                "id": clip["id"],
                "creator": username,
                "title": clip.get(
                    "title",
                    ""
                ),
                "url": clip.get("url"),
                "thumbnail_url": clip.get(
                    "thumbnail_url"
                ),
                "view_count": clip.get(
                    "view_count",
                    0
                ),
                "created_at": clip.get(
                    "created_at"
                ),
                "duration": clip.get(
                    "duration",
                    0
                ),
            }
        )

    return clips


# ============================================================
# DISCOVER TOP CLIPS
# ============================================================

def discover(cfg):

    token = twitch_token()

    candidates = []

    for username in cfg.get(
        "streamers",
        []
    ):

        try:

            print(
                f"Checking Twitch clips for "
                f"{username}..."
            )

            clips = get_daily_clips(
                username,
                token
            )

            candidates.extend(clips)

        except Exception as e:

            print(
                f"Twitch error for "
                f"{username}: {e}"
            )

    # Rank everything by current view count
    candidates.sort(
        key=lambda x: x.get(
            "view_count",
            0
        ),
        reverse=True
    )

    top_clips = candidates[:10]

    print("\nTop Twitch clips found:")

    for clip in top_clips:

        print(
            clip["creator"],
            clip["view_count"],
            clip["title"],
            clip["url"]
        )

    return top_clips


# ============================================================
# CHOOSE TOP UNUSED CLIP
# ============================================================

def choose(candidates, h):

    seen = set(
        h.get(
            "processed",
            []
        )
    )

    for clip in candidates:

        key = hashlib.sha256(
            clip["id"].encode()
        ).hexdigest()

        if key not in seen:
            return clip

    return None


# ============================================================
# OPENAI COMMENTARY PACKAGE
# ============================================================

def write_package(client, clip):

    prompt = f"""
You are writing original commentary for ViralSpawnTV,
a YouTube Shorts channel covering viral streamer moments.

Create a transformative YouTube Short commentary package
about the following streamer clip.

Creator:
{clip.get('creator')}

Clip title:
{clip.get('title')}

Current views:
{clip.get('view_count')}

Clip URL:
{clip.get('url')}

IMPORTANT:

At this stage you only have the Twitch metadata and clip
title. Do not pretend that you watched or analyzed the
actual video.

Do not invent events, dialogue, people, actions, or context
that are not supported by the information provided.

If the title alone does not provide enough context, keep
the commentary focused on the available information rather
than guessing what happened.

Return ONLY valid JSON.

Use exactly these keys:

hook
narration
title
description

REQUIREMENTS:

hook:
Short attention-grabbing opening.

narration:
35-65 words.

Use a young, conversational American gaming/commentary
style.

The narration should add commentary or context rather than
simply repeat the clip title.

title:
Create an attention-grabbing YouTube Shorts title without
making unsupported claims.

description:
Create a short ViralSpawnTV description.

Mention the original creator.

Include the original Twitch clip URL.

Do not claim ViralSpawnTV owns the original footage.
"""

    r = client.responses.create(
        model=os.getenv(
            "OPENAI_MODEL",
            "gpt-5.6"
        ),
        input=prompt,
    )

    txt = r.output_text.strip()

    # Remove markdown code fences if the model happens
    # to include them.
    if txt.startswith("```"):

        txt = txt.split(
            "\n",
            1
        )[1].rsplit(
            "```",
            1
        )[0]

    return json.loads(txt)


# ============================================================
# AI VOICE
# ============================================================

def voice(client, text, path):

    with client.audio.speech.with_streaming_response.create(

        model=os.getenv(
            "TTS_MODEL",
            "gpt-4o-mini-tts"
        ),

        voice=os.getenv(
            "TTS_VOICE",
            "onyx"
        ),

        input=text,

        instructions=(
            "Young adult American male. "
            "Neutral United States accent. "
            "Conversational gaming and streamer commentary. "
            "Medium-fast pace. "
            "Very clear pronunciation. "
            "Energetic but natural. "
            "Do not sound like a radio announcer."
        ),

    ) as response:

        response.stream_to_file(path)


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():

    print(
        "\n=============================="
    )
    print(
        "VIRALSPAWNTV AUTOMATION"
    )
    print(
        "==============================\n"
    )

    cfg = load_config()

    h = history()

    # ------------------------------------------
    # Find today's top clips
    # ------------------------------------------

    candidates = discover(cfg)

    if not candidates:

        print(
            "\nNo Twitch clips found today."
        )

        return

    # ------------------------------------------
    # Choose highest-ranked unused clip
    # ------------------------------------------

    clip = choose(
        candidates,
        h
    )

    if not clip:

        print(
            "\nTop clips have already "
            "been processed."
        )

        return

    # ------------------------------------------
    # Show selected clip
    # ------------------------------------------

    print(
        "\nSELECTED CLIP"
    )

    print(
        json.dumps(
            clip,
            indent=2
        )
    )

    # ------------------------------------------
    # Connect to OpenAI
    # ------------------------------------------

    client = OpenAI()

    # ------------------------------------------
    # Generate ViralSpawnTV commentary
    # ------------------------------------------

    meta = write_package(
        client,
        clip
    )

    print(
        "\nVIRALSPAWNTV PACKAGE"
    )

    print(
        json.dumps(
            meta,
            indent=2
        )
    )

    # ------------------------------------------
    # IMPORTANT
    # ------------------------------------------
    #
    # We intentionally DO NOT add this clip
    # to processed history yet.
    #
    # A clip should only be marked processed
    # AFTER:
    #
    # 1. Authorized video media is obtained
    # 2. The video is analyzed
    # 3. Commentary is finalized
    # 4. Voiceover is generated
    # 5. The vertical Short is rendered
    # 6. The finished MP4 passes successfully
    #
    # This prevents scheduled runs from
    # consuming clips without creating videos.
    # ------------------------------------------

    print(
        "\nCandidate successfully "
        "discovered and analyzed."
    )

    print(
        "Clip URL:",
        clip["url"]
    )

    print(
        "\nClip has NOT been marked "
        "as processed yet."
    )

    print(
        "Waiting for successful video "
        "production before adding it "
        "to history."
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
