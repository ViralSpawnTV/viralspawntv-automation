import pathlib
import re
import subprocess

from playwright.sync_api import sync_playwright


ROOT = pathlib.Path(__file__).resolve().parent

DOWNLOAD_DIR = (
    ROOT /
    "work" /
    "kick_downloads"
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)

CHANNEL = "ayezee"

CLIPS_URL = (
    f"https://kick.com/{CHANNEL}/clips"
)


def save_debug(page, name):

    page.screenshot(
        path=str(
            DOWNLOAD_DIR /
            f"{name}.png"
        ),
        full_page=True,
    )

    (
        DOWNLOAD_DIR /
        f"{name}.html"
    ).write_text(
        page.content(),
        encoding="utf-8",
    )


def main():

    print("=" * 60)
    print("VIRALSPAWNTV KICK ACQUISITION TEST")
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

        print()
        print("Opening:")
        print(CLIPS_URL)

        page.goto(
            CLIPS_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(6000)

        save_debug(
            page,
            "01_clips_page"
        )

        # ---------------------------------------------
        # FIND PUBLIC CLIP LINKS
        # ---------------------------------------------

        links = page.locator(
            f'a[href^="/{CHANNEL}/clips/clip_"]'
        )

        count = links.count()

        if count == 0:

            links = page.locator(
                'a[href*="/clips/clip_"]'
            )

            count = links.count()

        print()
        print(
            f"Found {count} Kick clips."
        )

        if count == 0:

            raise RuntimeError(
                "No Kick clips discovered."
            )

        # ---------------------------------------------
        # SELECT FIRST CLIP
        # ---------------------------------------------

        clip_href = (
            links
            .first
            .get_attribute("href")
        )

        if not clip_href:

            raise RuntimeError(
                "Selected clip had no href."
            )

        clip_match = re.search(
            r"(clip_[A-Za-z0-9]+)",
            clip_href
        )

        if not clip_match:

            raise RuntimeError(
                "Could not determine Kick clip ID."
            )

        clip_id = clip_match.group(1)

        clip_url = (
            "https://kick.com"
            + clip_href
            if clip_href.startswith("/")
            else clip_href
        )

        print()
        print("=" * 60)
        print("SELECTED CLIP")
        print("=" * 60)

        print("Clip ID:")
        print(clip_id)

        print()
        print("Clip URL:")
        print(clip_url)

        # ---------------------------------------------
        # OPEN SELECTED CLIP
        # ---------------------------------------------

        page.goto(
            clip_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(6000)

        save_debug(
            page,
            "02_selected_clip"
        )

        html = page.content()

        # ---------------------------------------------
        # FIND ONLY THE SELECTED CLIP'S MEDIA PLAYLIST
        #
        # We deliberately require the selected clip ID
        # so we don't accidentally grab the channel's
        # live-stream playlist.
        # ---------------------------------------------

        pattern = (
            r'https://clips\.kick\.com/'
            r'clips/[^"\'\\<>\s]+/'
            + re.escape(clip_id)
            + r'/playlist\.m3u8'
        )

        matches = re.findall(
            pattern,
            html
        )

        # Remove duplicates while retaining order.
        playlists = list(
            dict.fromkeys(matches)
        )

        print()
        print(
            "Matching clip playlists found:",
            len(playlists)
        )

        if not playlists:

            raise RuntimeError(
                "Selected clip playlist "
                "was not found in Kick page."
            )

        playlist_url = playlists[0]

        print()
        print("Selected media playlist:")
        print(playlist_url)

        # ---------------------------------------------
        # DOWNLOAD / REMUX WITH FFMPEG
        # ---------------------------------------------

        output_file = (
            DOWNLOAD_DIR /
            f"{clip_id}.mp4"
        )

        print()
        print(
            "Creating MP4 with FFmpeg..."
        )

        command = [
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
            playlist_url,

            "-c",
            "copy",

            "-movflags",
            "+faststart",

            str(output_file),
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
        )

        print()
        print("FFmpeg return code:")
        print(result.returncode)

        if result.returncode != 0:

            print()
            print("FFmpeg STDERR:")
            print(result.stderr[-5000:])

            raise RuntimeError(
                "FFmpeg could not create "
                "the Kick clip MP4."
            )

        # ---------------------------------------------
        # VERIFY MP4
        # ---------------------------------------------

        if not output_file.exists():

            raise RuntimeError(
                "FFmpeg returned successfully "
                "but MP4 does not exist."
            )

        size = output_file.stat().st_size

        if size < 10000:

            raise RuntimeError(
                "MP4 exists but is unexpectedly small."
            )

        size_mb = (
            size /
            1_000_000
        )

        print()
        print("=" * 60)
        print("SUCCESS")
        print("=" * 60)

        print()
        print("MP4:")
        print(output_file)

        print()
        print(
            f"Size: {size_mb:.2f} MB"
        )

        browser.close()


if __name__ == "__main__":
    main()
