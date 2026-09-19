import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


OUTPUT_DIR = Path("work/kick_gaming")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# PRIVATE TEST ONLY.
# These are discovery/acquisition candidates, not claims of permission.
TEST_GAMING_CHANNELS = [
    "dona",
]


def get_clip_links(channel):
    url = f"https://kick.com/{channel}/clips"

    print(f"Opening Kick gaming clips page: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1000},
        )

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(7000)

        html = page.content()
        browser.close()

    patterns = [
        r'href=["\'](/[^"\']+/clips/clip_[A-Za-z0-9_-]+)["\']',
        r'https://kick\.com/[^"\'\\\s]+/clips/clip_[A-Za-z0-9_-]+',
    ]

    links = []

    for pattern in patterns:
        matches = re.findall(pattern, html)

        for match in matches:
            if match.startswith("http"):
                full_url = match
            else:
                full_url = f"https://kick.com{match}"

            if full_url not in links:
                links.append(full_url)

    print(f"Found {len(links)} clip links for {channel}.")

    return links


def get_clip_playlist(clip_url):
    clip_id_match = re.search(
        r"(clip_[A-Za-z0-9_-]+)",
        clip_url,
    )

    if not clip_id_match:
        raise RuntimeError(
            f"Could not determine clip ID from {clip_url}"
        )

    clip_id = clip_id_match.group(1)

    print(f"Inspecting clip: {clip_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1000},
        )

        # Collect network requests too. This makes the adapter
        # more robust than relying only on HTML.
        network_urls = []

        def capture_request(request):
            url = request.url

            if ".m3u8" in url:
                network_urls.append(url)

        page.on("request", capture_request)

        page.goto(
            clip_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(7000)

        html = page.content()
        browser.close()

    html_urls = re.findall(
        r'https?[^"\'\\\s]+?\.m3u8[^"\'\\\s<]*',
        html,
    )

    candidates = network_urls + html_urls

    cleaned = []

    for value in candidates:
        value = (
            value
            .replace("\\u0026", "&")
            .replace("\\/", "/")
            .replace("&amp;", "&")
        )

        if value not in cleaned:
            cleaned.append(value)

    # Prefer a playlist containing the exact clip ID.
    exact = [
        url
        for url in cleaned
        if clip_id in url
    ]

    if exact:
        print("Exact clip playlist located.")
        return exact[0]

    # Do NOT blindly use an unrelated playlist.
    raise RuntimeError(
        "No media playlist belonging to the selected "
        "Kick clip was found."
    )


def download_clip(
    playlist_url,
    clip_url,
    output_path,
):
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

    print("Acquiring Kick clip for PRIVATE pipeline test...")

    subprocess.run(
        command,
        check=True,
    )

    if not output_path.exists():
        raise RuntimeError(
            "FFmpeg completed but no MP4 was created."
        )

    if output_path.stat().st_size < 10000:
        raise RuntimeError(
            "Downloaded MP4 is unexpectedly small."
        )

    print(
        f"Acquired {output_path} "
        f"({output_path.stat().st_size:,} bytes)"
    )


def try_channel(channel):
    links = get_clip_links(channel)

    if not links:
        return None

    # Try several clips instead of failing because the first
    # clip happens to have an unusable page/media structure.
    for index, clip_url in enumerate(
        links[:10],
        start=1,
    ):
        print()
        print(
            f"Trying {channel} clip "
            f"{index}/{min(len(links), 10)}"
        )
        print(clip_url)

        try:
            playlist = get_clip_playlist(
                clip_url
            )

            output_path = (
                OUTPUT_DIR
                / "selected_kick_gaming_source.mp4"
            )

            download_clip(
                playlist,
                clip_url,
                output_path,
            )

            return {
                "platform": "kick",
                "channel": channel,
                "clip_url": clip_url,
                "video_path": str(output_path),

                # IMPORTANT:
                # Downloadability is not being treated as
                # republication permission.
                "rights_status": "unverified",

                "creator_permission_verified": False,
                "game_rights_verified": False,

                "acquisition_context":
                    "private_pipeline_test",

                "public_publish_allowed": False,
            }

        except Exception as exc:
            print(
                f"Clip attempt failed: {exc}"
            )

    return None


def main():
    print()
    print("=======================================")
    print("ViralSpawnTV Kick Gaming Private Test")
    print("=======================================")
    print()
    print(
        "This mode may acquire a public Kick clip "
        "for a PRIVATE production-pipeline test."
    )
    print(
        "It does NOT mark the clip as cleared for "
        "public publication."
    )

    result = None

    for channel in TEST_GAMING_CHANNELS:
        print()
        print(
            f"Searching gaming channel: {channel}"
        )

        try:
            result = try_channel(channel)

        except Exception as exc:
            print(
                f"Channel failed: {exc}"
            )

        if result:
            break

    if not result:
        raise RuntimeError(
            "No usable Kick gaming clip could be acquired "
            "from the configured private-test channels."
        )

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

    print()
    print("=======================================")
    print("PRIVATE GAMING ACQUISITION SUCCESS")
    print("=======================================")
    print(f"Channel: {result['channel']}")
    print(f"Clip: {result['clip_url']}")
    print(f"Video: {result['video_path']}")
    print(
        f"Rights status: "
        f"{result['rights_status']}"
    )
    print(
        "Public publishing: BLOCKED"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print()
        print("=======================================")
        print("PRIVATE ACQUISITION FAILED")
        print("=======================================")
        print(str(exc))
        sys.exit(1)
