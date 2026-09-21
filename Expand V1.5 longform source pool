import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


RANKED = Path("work/longform/ranked_sources.json")
OUT = Path("work/longform/sources")
META = Path("work/longform/acquired_sources.json")

# V1.5 expanded source pool.
#
# Production needs enough material to survive:
# - source quality rejection
# - motion/activity rejection
# - content screening
# - excerpt selection
#
# We therefore acquire a deep pool instead of stopping at 10.
TARGET = 28
MIN_ACQUIRED = 10


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130 Safari/537.36"
)


def playlist(url, clip_id):

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            user_agent=USER_AGENT
        )

        hits = []

        page.on(
            "request",
            lambda req: (
                hits.append(req.url)
                if ".m3u8" in req.url
                else None
            ),
        )

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(3000)

        html = page.content()

        browser.close()

    hits += re.findall(
        r'https?[^"\'\\\s]+?\.m3u8[^"\'\\\s<]*',
        html,
    )

    clean = []

    for item in hits:

        item = (
            item.replace(
                "\\u0026",
                "&",
            )
            .replace(
                "\\/",
                "/",
            )
            .replace(
                "&amp;",
                "&",
            )
        )

        if item not in clean:
            clean.append(item)

    exact = [
        item
        for item in clean
        if clip_id in item
    ]

    if not exact:
        raise RuntimeError(
            "playlist not found"
        )

    return exact[0]


def main():

    OUT.mkdir(
        parents=True,
        exist_ok=True,
    )

    data = json.loads(
        RANKED.read_text(
            encoding="utf-8"
        )
    )

    candidates = data.get(
        "candidates",
        [],
    )

    if not candidates:

        raise RuntimeError(
            "No ranked long-form candidates."
        )

    acquired = []

    attempted = 0

    for candidate in candidates:

        if len(acquired) >= TARGET:
            break

        attempted += 1

        try:

            playlist_url = playlist(
                candidate["clip_url"],
                candidate["clip_id"],
            )

            destination = (
                OUT
                / (
                    f"{len(acquired) + 1:02d}_"
                    f"{candidate['clip_id']}.mp4"
                )
            )

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-user_agent",
                    USER_AGENT,
                    "-headers",
                    (
                        f"Referer: "
                        f"{candidate['clip_url']}\r\n"
                        f"Origin: "
                        f"https://kick.com\r\n"
                    ),
                    "-i",
                    playlist_url,
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(destination),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            if (
                not destination.exists()
                or destination.stat().st_size
                < 10000
            ):
                raise RuntimeError(
                    "tiny download"
                )

            acquired.append(
                {
                    **candidate,
                    "local_path":
                        str(destination),
                    "rights_status":
                        "unverified",
                }
            )

            print(
                f"Acquired "
                f"{len(acquired)}/{TARGET}: "
                f"{candidate.get('game')} | "
                f"{candidate.get('channel')}"
            )

        except Exception as exc:

            print(
                "Skip:",
                candidate.get(
                    "clip_id"
                ),
                exc,
            )

    if len(acquired) < MIN_ACQUIRED:

        raise RuntimeError(
            f"Only acquired "
            f"{len(acquired)} clips. "
            f"V1.5 requires at least "
            f"{MIN_ACQUIRED}."
        )

    META.write_text(
        json.dumps(
            {
                "version": "1.5",
                "target_acquired":
                    TARGET,
                "attempted":
                    attempted,
                "acquired_count":
                    len(acquired),
                "sources":
                    acquired,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "\n"
        f"V1.5 collector attempted "
        f"{attempted} ranked candidates."
    )

    print(
        f"Acquired {len(acquired)} "
        f"long-form sources."
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            "LONGFORM COLLECTOR FAILED:",
            exc,
        )

        sys.exit(1)
