import pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
DOWNLOAD_DIR = ROOT / "work" / "kick_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# First authorized-source acquisition test.
CLIPS_URL = "https://kick.com/ayezee/clips"


def main():
    print("=" * 60)
    print("VIRALSPAWNTV KICK DOWNLOAD TEST")
    print("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1440, "height": 1000},
        )

        page = context.new_page()

        print(f"Opening: {CLIPS_URL}")

        page.goto(
            CLIPS_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(5000)

        print("Page title:", page.title())
        print("Current URL:", page.url)

        # Save diagnostics so we can see exactly what GitHub's
        # browser received if Kick changes its interface.
        page.screenshot(
            path=str(DOWNLOAD_DIR / "01_clips_page.png"),
            full_page=True,
        )

        (DOWNLOAD_DIR / "01_clips_page.html").write_text(
            page.content(),
            encoding="utf-8",
        )

        # Find links that appear to lead to individual Kick clips.
        links = page.locator('a[href*="/clip/"]')

        count = links.count()

        print(f"Found {count} possible clip links.")

        if count == 0:
            raise RuntimeError(
                "No public clip links were found. "
                "Check the uploaded screenshot/artifact."
            )

        clip_url = links.first.get_attribute("href")

        if not clip_url:
            raise RuntimeError(
                "Found a clip element but it had no URL."
            )

        if clip_url.startswith("/"):
            clip_url = "https://kick.com" + clip_url

        print("Opening clip:", clip_url)

        page.goto(
            clip_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(5000)

        page.screenshot(
            path=str(DOWNLOAD_DIR / "02_clip_page.png"),
            full_page=True,
        )

        (DOWNLOAD_DIR / "02_clip_page.html").write_text(
            page.content(),
            encoding="utf-8",
        )

        # Kick documents a Download control for public clips.
        # Try accessible button/link text rather than relying
        # on private or undocumented media endpoints.
        download_controls = page.get_by_text(
            "Download",
            exact=True,
        )

        print(
            "Download controls found:",
            download_controls.count(),
        )

        if download_controls.count() == 0:
            raise RuntimeError(
                "Kick's Download control was not visible "
                "to the automated browser. Review the "
                "diagnostic artifacts."
            )

        print("Clicking official Download control...")

        try:
            with page.expect_download(timeout=30000) as info:
                download_controls.first.click()

            download = info.value

            suggested = (
                download.suggested_filename
                or "kick_clip.mp4"
            )

            destination = DOWNLOAD_DIR / suggested

            download.save_as(str(destination))

            print("Download saved:", destination)

        except Exception as exc:
            # Preserve another screenshot at the exact failure
            # point so we can diagnose the UI.
            page.screenshot(
                path=str(
                    DOWNLOAD_DIR /
                    "03_download_failure.png"
                ),
                full_page=True,
            )

            raise RuntimeError(
                f"Download button was found but the "
                f"browser did not receive a download: {exc}"
            )

        mp4_files = list(
            DOWNLOAD_DIR.glob("*.mp4")
        )

        if not mp4_files:
            raise RuntimeError(
                "Download completed but no MP4 was found."
            )

        print()
        print("=" * 60)
        print("SUCCESS")
        print("=" * 60)

        for file in mp4_files:
            size_mb = file.stat().st_size / 1_000_000

            print(
                f"{file.name}: {size_mb:.2f} MB"
            )

        browser.close()


if __name__ == "__main__":
    main()
