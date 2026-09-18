import json
import re
import subprocess
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright


OUTPUT_DIR = Path("work/kick_gaming")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def get_clip_links(channel):
    """
    Find public clips listed on a Kick channel.

    This does NOT imply permission to republish them.
    Rights authorization is handled separately.
    """

    url = f"https://kick.com/{channel}/clips"

    print(f"Opening Kick clips page: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={
                "width": 1440,
                "height": 1000,
            },
        )

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(7000)

        html = page.content()

        browser.close()

    pattern = (
        rf'href=["\']'
        rf'(/[^"\']+/clips/clip_[A-Za-z0-9]+)'
        rf'["\']'
    )

    matches = re.findall(pattern, html)

    links = []

    for match in matches:
        full_url = f"https://kick.com{match}"

        if full_url not in links:
            links.append(full_url)

    print(f"Found {len(links)} public Kick clip links.")

    return links


def get_clip_playlist(clip_url):
    """
    Open one Kick clip page and locate the media playlist
    associated with that exact clip ID.
    """

    clip_id_match = re.search(
        r"(clip_[A-Za-z0-9]+)",
        clip_url,
    )

    if not clip_id_match:
        raise RuntimeError(
            f"Could not determine clip ID from {clip_url}"
        )

    clip_id = clip_id_match.group(1)

    print(f"Inspecting Kick clip: {clip_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={
                "width": 1440,
                "height": 1000,
            },
        )

        page.goto(
            clip_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(5000)

        html = page.content()

        browser.close()

    # We only accept playlists containing the exact selected
    # clip ID so a live-stream playlist cannot be mistaken for
    # the clip.
    urls = re.findall(
        r'https?[^"\'\\\s]+?\.m3u8[^"\'\\\s<]*',
        html,
    )

    cleaned = []

    for value in urls:
        value = (
            value
            .replace("\\u0026", "&")
            .replace("\\/", "/")
            .replace("&amp;", "&")
        )

        if clip_id in value and value not in cleaned:
            cleaned.append(value)

    if not cleaned:
        raise RuntimeError(
            "No media playlist belonging to the selected "
            "Kick clip was found."
        )

    playlist = cleaned[0]

    print("Exact clip playlist located.")

    return playlist


def download_clip(
    playlist_url,
    clip_url,
    output_path,
):
    """
    Remux the Kick clip playlist into an MP4.

    No re-encoding here; V4 handles editing later.
    """

    command = [
        "ffmpeg",
        "-y",

        "-user_agent",
        USER_AGENT,

        "-headers",
        (
            f"Referer: {clip_url}\r\n"
            "Origin: https://kick.com\r\n"
        ),

        "-i",
        playlist_url,

        "-c",
        "copy",

        "-movflags",
        "+faststart",

        str(output_path),
    ]

    print("Downloading selected Kick clip...")

    subprocess.run(
        command,
        check=True,
    )

    if not output_path.exists():
        raise RuntimeError(
            "FFmpeg completed but no MP4 was created."
        )

    print(
        f"Downloaded: {output_path}"
    )


def acquire_from_source(source):
    """
    Acquisition is only allowed when the rights database
    explicitly enables the source.
    """

    source_id = source.get("id")
    channel = source.get("kick_channel")

    if not source.get("production_enabled"):
        raise RuntimeError(
            f"{source_id}: production_enabled is false."
        )

    if not source.get("creator_clipping_allowed"):
        raise RuntimeError(
            f"{source_id}: creator clipping permission missing."
        )

    if not source.get(
        "platform_monetization_allowed"
    ):
        raise RuntimeError(
            f"{source_id}: monetization permission missing."
        )

    if (
        source.get("authorized_acquisition_method")
        != "kick_public_clip_download"
    ):
        raise RuntimeError(
            f"{source_id}: Kick acquisition not authorized "
            "in gaming_sources.json."
        )

    if not channel:
        raise RuntimeError(
            f"{source_id}: kick_channel is missing."
        )

    links = get_clip_links(channel)

    if not links:
        raise RuntimeError(
            f"No public clips found for {channel}."
        )

    # TEMPORARY:
    # First acquisition test uses the first available clip.
    # Our selector will later choose the highest-scoring unused
    # eligible clip instead.
    selected_url = links[0]

    playlist = get_clip_playlist(
        selected_url
    )

    output_path = (
        OUTPUT_DIR
        / f"{source_id}_source.mp4"
    )

    download_clip(
        playlist,
        selected_url,
        output_path,
    )

    result = {
        "source_id": source_id,
        "creator": source.get("creator"),
        "organization": source.get("organization"),
        "kick_channel": channel,
        "clip_url": selected_url,
        "video_path": str(output_path),
        "permission_url":
            source.get("permission_url"),
        "acquisition_method":
            "kick_public_clip_download",
    }

    result_path = (
        OUTPUT_DIR
        / "acquisition_result.json"
    )

    with open(
        result_path,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            result,
            f,
            indent=2,
            ensure_ascii=False,
        )

    return result


def main():
    source_file = Path(
        "gaming_sources.json"
    )

    if not source_file.exists():
        raise RuntimeError(
            "gaming_sources.json not found."
        )

    with open(
        source_file,
        "r",
        encoding="utf-8",
    ) as f:
        database = json.load(f)

    enabled = [
        source
        for source in database.get(
            "sources",
            []
        )
        if source.get("production_enabled")
    ]

    print()
    print("=======================================")
    print("ViralSpawnTV Kick Gaming Acquisition")
    print("=======================================")

    if not enabled:
        print()
        print("NO ENABLED GAMING SOURCE")
        print()
        print(
            "Acquisition correctly stopped before "
            "downloading any media."
        )
        return

    # One source for this test.
    source = enabled[0]

    result = acquire_from_source(source)

    print()
    print("=======================================")
    print("KICK GAMING ACQUISITION SUCCESS")
    print("=======================================")
    print(f"Creator: {result['creator']}")
    print(f"Clip: {result['clip_url']}")
    print(f"File: {result['video_path']}")


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print()
        print("=======================================")
        print("ACQUISITION FAILED")
        print("=======================================")
        print(str(exc))
        sys.exit(1)
