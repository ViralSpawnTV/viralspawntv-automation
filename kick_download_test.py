import pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
DOWNLOAD_DIR = ROOT / "work" / "kick_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

CHANNEL = "ayezee"
CLIPS_URL = f"https://kick.com/{CHANNEL}/clips"


def save_debug(page, name):
    page.screenshot(
        path=str(DOWNLOAD_DIR / f"{name}.png"),
        full_page=True,
    )

    (DOWNLOAD_DIR / f"{name}.html").write_text(
        page.content(),
        encoding="utf-8",
    )


def main():

    print("=" * 60)
    print("VIRALSPAWNTV KICK DOWNLOAD TEST")
    print("=" * 60)

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            accept_downloads=True,
            viewport={
                "width": 1440,
                "height": 1000
            },
        )

        page = context.new_page()

        print()
        print("Opening clips page:")
        print(CLIPS_URL)

        page.goto(
            CLIPS_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(6000)

        print("Page title:", page.title())
        print("Current URL:", page.url)

        save_debug(
            page,
            "01_clips_page"
        )

        # --------------------------------------------------
        # KICK'S CURRENT CLIP URL FORMAT
        #
        # Example:
        # /ayezee/clips/clip_01M2BQ8Y19MQ890YK9MBPRPP6B
        # --------------------------------------------------

        selector = (
            f'a[href^="/{CHANNEL}/clips/clip_"]'
        )

        links = page.locator(selector)

        count = links.count()

        print()
        print(
            f"Found {count} Kick clip links."
        )

        if count == 0:

            print(
                "Primary selector failed."
            )

            print(
                "Trying broader Kick clip selector..."
            )

            links = page.locator(
                'a[href*="/clips/clip_"]'
            )

            count = links.count()

            print(
                f"Broad selector found {count} links."
            )

        if count == 0:

            raise RuntimeError(
                "No Kick clip links were found."
            )

        # --------------------------------------------------
        # PRINT FIRST FEW CLIPS
        # --------------------------------------------------

        print()
        print("First clips discovered:")

        max_print = min(
            count,
            10
        )

        for i in range(max_print):

            href = (
                links
                .nth(i)
                .get_attribute("href")
            )

            print(
                f"{i + 1}. {href}"
            )

        # --------------------------------------------------
        # SELECT FIRST CLIP
        # --------------------------------------------------

        clip_href = (
            links
            .first
            .get_attribute("href")
        )

        if not clip_href:

            raise RuntimeError(
                "Clip link did not contain href."
            )

        if clip_href.startswith("/"):

            clip_url = (
                "https://kick.com"
                + clip_href
            )

        else:

            clip_url = clip_href

        print()
        print("=" * 60)
        print("SELECTED KICK CLIP")
        print("=" * 60)

        print(clip_url)

        # --------------------------------------------------
        # OPEN CLIP
        # --------------------------------------------------

        page.goto(
            clip_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(6000)

        print()
        print(
            "Clip page title:",
            page.title()
        )

        print(
            "Clip page URL:",
            page.url
        )

        save_debug(
            page,
            "02_clip_page"
        )

        # --------------------------------------------------
        # FIND DOWNLOAD CONTROL
        # --------------------------------------------------

        print()
        print(
            "Searching for Kick Download control..."
        )

        download_button = page.get_by_role(
            "button",
            name="Download",
        )

        download_link = page.get_by_role(
            "link",
            name="Download",
        )

        text_download = page.get_by_text(
            "Download",
            exact=True,
        )

        print(
            "Download buttons:",
            download_button.count()
        )

        print(
            "Download links:",
            download_link.count()
        )

        print(
            "Download text matches:",
            text_download.count()
        )

        control = None

        if download_button.count() > 0:

            control = download_button.first

            print(
                "Using Download button."
            )

        elif download_link.count() > 0:

            control = download_link.first

            print(
                "Using Download link."
            )

        elif text_download.count() > 0:

            control = text_download.first

            print(
                "Using Download text control."
            )

        # --------------------------------------------------
        # IF DOWNLOAD ISN'T IMMEDIATELY VISIBLE,
        # LOOK FOR MENU BUTTONS
        # --------------------------------------------------

        if control is None:

            print()
            print(
                "Download control not immediately visible."
            )

            print(
                "Checking buttons for menus..."
            )

            buttons = page.locator("button")

            button_count = buttons.count()

            print(
                "Buttons on page:",
                button_count
            )

            # Try buttons one at a time.
            # Some versions of Kick place Download
            # inside a three-dot/share menu.

            for i in range(
                min(button_count, 30)
            ):

                button = buttons.nth(i)

                try:

                    aria = (
                        button.get_attribute(
                            "aria-label"
                        )
                        or ""
                    )

                    title = (
                        button.get_attribute(
                            "title"
                        )
                        or ""
                    )

                    print(
                        f"Button {i}: "
                        f"aria='{aria}' "
                        f"title='{title}'"
                    )

                    combined = (
                        aria + " " + title
                    ).lower()

                    if (
                        "more" in combined
                        or
                        "share" in combined
                        or
                        "option" in combined
                    ):

                        print(
                            "Trying possible menu button..."
                        )

                        button.click()

                        page.wait_for_timeout(
                            1000
                        )

                        possible = (
                            page.get_by_text(
                                "Download",
                                exact=True,
                            )
                        )

                        if possible.count() > 0:

                            control = (
                                possible.first
                            )

                            print(
                                "Download found "
                                "inside menu."
                            )

                            break

                except Exception:

                    continue

        # --------------------------------------------------
        # STILL NOTHING?
        # --------------------------------------------------

        if control is None:

            save_debug(
                page,
                "03_no_download_control"
            )

            raise RuntimeError(
                "Kick clip loaded successfully, "
                "but the Download control could "
                "not be located. Diagnostic "
                "HTML/screenshots were saved."
            )

        # --------------------------------------------------
        # DOWNLOAD
        # --------------------------------------------------

        print()
        print(
            "Clicking Kick Download control..."
        )

        try:

            with page.expect_download(
                timeout=30000
            ) as download_info:

                control.click()

            download = (
                download_info.value
            )

            filename = (
                download.suggested_filename
                or
                "kick_clip.mp4"
            )

            if not filename.lower().endswith(
                ".mp4"
            ):

                filename += ".mp4"

            destination = (
                DOWNLOAD_DIR /
                filename
            )

            download.save_as(
                str(destination)
            )

            print()
            print(
                "Downloaded:"
            )

            print(destination)

        except Exception as exc:

            save_debug(
                page,
                "04_download_failure"
            )

            raise RuntimeError(
                "Kick Download control was found, "
                "but Chromium did not receive a "
                f"download event: {exc}"
            )

        # --------------------------------------------------
        # VERIFY FILE
        # --------------------------------------------------

        mp4_files = list(
            DOWNLOAD_DIR.glob(
                "*.mp4"
            )
        )

        if not mp4_files:

            raise RuntimeError(
                "No MP4 exists after download."
            )

        print()
        print("=" * 60)
        print("DOWNLOAD SUCCESS")
        print("=" * 60)

        for file in mp4_files:

            size_mb = (
                file.stat().st_size
                / 1_000_000
            )

            print(
                f"{file.name}"
            )

            print(
                f"Size: {size_mb:.2f} MB"
            )

        browser.close()


if __name__ == "__main__":
    main()
