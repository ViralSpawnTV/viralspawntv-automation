import os
import json
import hashlib
import pathlib
import requests

from datetime import datetime, timezone
from openai import OpenAI


# ============================================================
# PATHS
# ============================================================

ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT / "work"
OUT = ROOT / "output"
DATA = ROOT / "data"

WORK.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)


# ============================================================
# CONFIG / HISTORY
# ============================================================

def load_config():
    path = ROOT / "config.json"

    if not path.exists():
        raise SystemExit("Missing config.json.")

    return json.loads(path.read_text())


def history():
    path = DATA / "history.json"

    if path.exists():
        return json.loads(path.read_text())

    return {
        "processed": []
    }


def save_history(h):
    path = DATA / "history.json"
    path.write_text(json.dumps(h, indent=2))


def clip_key(clip):
    raw = f"{clip.get('platform')}:{clip.get('id')}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ============================================================
# TWITCH AUTHENTICATION
# ============================================================

def twitch_token():

    client_id = os.environ["TWITCH_CLIENT_ID"]
    client_secret = os.environ["TWITCH_CLIENT_SECRET"]

    response = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json()["access_token"]


def twitch_headers(token):

    return {
        "Client-ID": os.environ["TWITCH_CLIENT_ID"],
        "Authorization": f"Bearer {token}",
    }


# ============================================================
# TWITCH DISCOVERY
# ============================================================

def get_broadcaster_id(username, token):

    response = requests.get(
        "https://api.twitch.tv/helix/users",
        headers=twitch_headers(token),
        params={
            "login": username
        },
        timeout=30,
    )

    response.raise_for_status()

    data = response.json().get("data", [])

    if not data:
        print(f"Twitch user not found: {username}")
        return None

    return data[0]["id"]


def get_daily_twitch_clips(username, token):

    broadcaster_id = get_broadcaster_id(username, token)

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
        "started_at": start.isoformat().replace("+00:00", "Z"),
        "ended_at": now.isoformat().replace("+00:00", "Z"),
        "first": 100,
    }

    response = requests.get(
        "https://api.twitch.tv/helix/clips",
        headers=twitch_headers(token),
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    clips = []

    for clip in response.json().get("data", []):

        clips.append({
            "platform": "twitch",
            "id": clip["id"],
            "creator": username,
            "title": clip.get("title", ""),
            "url": clip.get("url"),
            "thumbnail_url": clip.get("thumbnail_url"),
            "view_count": clip.get("view_count", 0),
            "created_at": clip.get("created_at"),
            "duration": clip.get("duration", 0),

            # Twitch discovery clips are NOT automatically
            # authorized for production.
            "reuse_allowed": False,
            "download_allowed": False,
            "permission_url": None,
        })

    return clips


def discover_twitch(cfg):

    print("\n==============================")
    print("TWITCH DISCOVERY")
    print("==============================")

    token = twitch_token()

    candidates = []

    for username in cfg.get("streamers", []):

        try:

            print(
                f"Checking Twitch clips for {username}..."
            )

            clips = get_daily_twitch_clips(
                username,
                token
            )

            candidates.extend(clips)

        except Exception as e:

            print(
                f"Twitch error for {username}: {e}"
            )

    candidates.sort(
        key=lambda x: x.get("view_count", 0),
        reverse=True
    )

    top_clips = candidates[:10]

    print("\nTop Twitch discovery clips:")

    for clip in top_clips:

        print(
            clip["creator"],
            clip["view_count"],
            clip["title"],
            clip["url"]
        )

    return top_clips


# ============================================================
# AUTHORIZED SOURCE CONFIGURATION
# ============================================================

def get_authorized_sources(cfg):

    authorized = []

    source_groups = cfg.get(
        "authorized_sources",
        {}
    )

    for platform, creators in source_groups.items():

        for creator in creators:

            item = creator.copy()

            item["platform"] = platform

            authorized.append(item)

    return authorized


def print_authorized_sources(cfg):

    sources = get_authorized_sources(cfg)

    print("\n==============================")
    print("AUTHORIZED PRODUCTION SOURCES")
    print("==============================")

    if not sources:

        print(
            "No authorized production sources configured."
        )

        return

    for source in sources:

        print(
            f"\nCreator: {source.get('creator')}"
        )

        print(
            f"Platform: {source.get('platform')}"
        )

        print(
            f"Channel: {source.get('channel')}"
        )

        print(
            f"Reuse allowed: "
            f"{source.get('reuse_allowed')}"
        )

        print(
            f"Download allowed: "
            f"{source.get('download_allowed')}"
        )

        print(
            f"Permission: "
            f"{source.get('permission_url')}"
        )


# ============================================================
# PRODUCTION AUTHORIZATION CHECK
# ============================================================

def production_authorized(clip):

    return (
        clip.get("reuse_allowed") is True
        and
        clip.get("download_allowed") is True
    )


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def choose_unprocessed(candidates, h):

    seen = set(
        h.get("processed", [])
    )

    for clip in candidates:

        key = clip_key(clip)

        if key not in seen:

            return clip

    return None


def mark_processed(clip, h):

    key = clip_key(clip)

    if key not in h["processed"]:

        h["processed"].append(key)

    save_history(h)


# ============================================================
# OPENAI COMMENTARY PACKAGE
# ============================================================

def write_package(client, clip):

    prompt = f"""
You are writing original commentary for ViralSpawnTV,
a YouTube Shorts channel covering viral streamer moments.

Create a transformative YouTube Short commentary package
about the following clip.

Platform:
{clip.get('platform')}

Creator:
{clip.get('creator')}

Clip title:
{clip.get('title')}

Current views:
{clip.get('view_count')}

Clip URL:
{clip.get('url')}

IMPORTANT:

At this stage you may only have metadata and the clip title.

Do not pretend that you watched or analyzed the actual
video unless actual video analysis has been supplied.

Do not invent events, dialogue, people, actions, or context
that are not supported by the supplied information.

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
simply repeating the clip title.

title:
Create an attention-grabbing YouTube Shorts title without
making unsupported claims.

description:
Create a short ViralSpawnTV description.

Mention the original creator.

Include the original clip URL.

Do not claim ViralSpawnTV owns the original footage.
"""

    response = client.responses.create(
        model=os.getenv(
            "OPENAI_MODEL",
            "gpt-5.6"
        ),
        input=prompt,
    )

    text = response.output_text.strip()

    if text.startswith("```"):

        text = (
            text
            .split("\n", 1)[1]
            .rsplit("```", 1)[0]
        )

    return json.loads(text)


# ============================================================
# VOICE GENERATION
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
# MAIN
# ============================================================

def main():

    print("\n==============================")
    print("VIRALSPAWNTV AUTOMATION")
    print("==============================\n")

    cfg = load_config()

    h = history()

    # ----------------------------------------
    # Show approved production sources
    # ----------------------------------------

    print_authorized_sources(cfg)

    # ----------------------------------------
    # Twitch discovery
    # ----------------------------------------

    twitch_candidates = discover_twitch(cfg)

    if not twitch_candidates:

        print(
            "\nNo Twitch discovery clips found today."
        )

    else:

        candidate = choose_unprocessed(
            twitch_candidates,
            h
        )

        if candidate:

            print("\n==============================")
            print("TOP DISCOVERY CANDIDATE")
            print("==============================")

            print(
                json.dumps(
                    candidate,
                    indent=2
                )
            )

            # --------------------------------
            # Critical rights check
            # --------------------------------

            if not production_authorized(candidate):

                print(
                    "\nDISCOVERY ONLY:"
                )

                print(
                    "This clip has NOT been approved "
                    "for automatic production."
                )

                print(
                    "No video will be downloaded."
                )

                print(
                    "No voiceover will be generated."
                )

                print(
                    "No Short will be rendered."
                )

                print(
                    "No YouTube upload will occur."
                )

            else:

                print(
                    "\nAUTHORIZED FOR PRODUCTION."
                )

        else:

            print(
                "\nTop Twitch clips have already "
                "been processed."
            )

    # ----------------------------------------
    # Current authorized Kick sources
    # ----------------------------------------

    kick_sources = (
        cfg
        .get("authorized_sources", {})
        .get("kick", [])
    )

    if kick_sources:

        print("\n==============================")
        print("KICK PRODUCTION QUEUE")
        print("==============================")

        for source in kick_sources:

            print(
                f"{source.get('creator')} "
                f"({source.get('channel')})"
            )

            print(
                "  Authorized for reuse:",
                source.get("reuse_allowed")
            )

            print(
                "  Authorized for download:",
                source.get("download_allowed")
            )

            print(
                "  Permission:",
                source.get("permission_url")
            )

        print(
            "\nAuthorized Kick creators are configured."
        )

        print(
            "Automated Kick media acquisition is "
            "the next module to connect."
        )

    print("\n==============================")
    print("RUN COMPLETE")
    print("==============================\n")


if __name__ == "__main__":
    main()
