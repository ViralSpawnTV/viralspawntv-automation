import json
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


RANKED = Path("work/v12_ranked_candidates.json")

# Permanent cross-run rejection history.
#
# Shared with v12_1_pipeline.py and persisted by GitHub Actions.
REJECTED = Path("shorts_rejected_history.json")

OUTDIR = Path("work/kick_gaming")
OUT = OUTDIR / "selected_kick_gaming_source.mp4"
RESULT = OUTDIR / "acquisition_result.json"


def load_json(path, default):
    try:
        return json.loads(
            Path(path).read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return default


def load_rejected_ids():
    """
    Load permanent rejected Shorts clip IDs.

    Supports both:
    - legacy plain-list format
    - current dictionary format with clip_ids
    """

    data = load_json(
        REJECTED,
        {
            "version": 1,
            "clip_ids": [],
        },
    )

    if isinstance(data, list):
        data = data

    elif isinstance(data, dict):
        data = data.get(
            "clip_ids",
            [],
        )

    else:
        data = []

    rejected = set()

    for item in data:
        clip_id = str(
            item
        ).strip()

        if clip_id:
            rejected.add(
                clip_id
            )

    return rejected


def save_rejected_ids(rejected):
    """
    Save the permanent rejection history in the same format used
    by v12_1_pipeline.py.
    """

    clean = sorted(
        {
            str(item).strip()
            for item in rejected
            if str(item).strip()
        }
    )

    REJECTED.write_text(
        json.dumps(
            {
                "version": 1,
                "clip_ids": clean,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def candidate_id(candidate):
    return str(
        candidate.get("clip_id")
        or candidate.get("id")
        or ""
    ).strip()


def candidate_url(candidate):
    return str(
        candidate.get("clip_url")
        or candidate.get("url")
        or ""
    ).strip()


def acquire(candidate):

    clip_id = candidate_id(
        candidate
    )

    clip_url = candidate_url(
        candidate
    )

    if not clip_id or not clip_url:
        raise RuntimeError(
            "Candidate missing clip_id or clip_url."
        )

    OUTDIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    playlist_urls = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page()

        def capture(response):

            url = response.url

            if ".m3u8" in url:
                playlist_urls.append(
                    url
                )

        page.on(
            "response",
            capture,
        )

        page.goto(
            clip_url,
            wait_until="domcontentloaded",
            timeout=45000,
        )

        page.wait_for_timeout(
            5000
        )

        # Try to start playback in case the playlist is
        # lazy-loaded.
        try:

            page.locator(
                "video"
            ).first.click(
                timeout=3000
            )

            page.wait_for_timeout(
                2500
            )

        except Exception:
            pass

        browser.close()

    if not playlist_urls:
        raise RuntimeError(
            "No HLS playlist captured from clip page."
        )

    # Prefer a playlist URL containing this clip ID.
    #
    # Otherwise use the last media playlist observed.
    # FFmpeg can resolve master playlists as well.
    chosen = None

    for url in playlist_urls:

        if (
            clip_id.lower()
            in url.lower()
        ):
            chosen = url
            break

    if not chosen:
        chosen = playlist_urls[-1]

    if OUT.exists():
        OUT.unlink()

    # ---------------------------------------------------------
    # FIRST ATTEMPT: STREAM COPY
    # ---------------------------------------------------------

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "warning",
        "-i",
        chosen,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(OUT),
    ]

    proc = subprocess.run(
        cmd
    )

    # ---------------------------------------------------------
    # FALLBACK: NORMALIZE VIDEO/AUDIO
    # ---------------------------------------------------------

    if (
        proc.returncode != 0
        or not OUT.exists()
        or OUT.stat().st_size < 10000
    ):

        if OUT.exists():
            OUT.unlink()

        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "warning",
            "-i",
            chosen,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(OUT),
        ]

        subprocess.run(
            cmd,
            check=True,
        )

    if (
        not OUT.exists()
        or OUT.stat().st_size < 10000
    ):
        raise RuntimeError(
            "Acquired output file is missing or too small."
        )

    result = {
        "success":
            True,
        "version":
            "12.1-compatible",
        "clip_id":
            clip_id,
        "clip_url":
            clip_url,
        "channel":
            candidate.get(
                "channel"
            ),
        "game":
            candidate.get(
                "game"
            ),
        "page_title":
            candidate.get(
                "page_title"
            ),
        "page_description":
            candidate.get(
                "page_description"
            ),
        "v12_metadata_score":
            candidate.get(
                "v12_metadata_score"
            ),
        "v12_metadata_reason":
            candidate.get(
                "v12_metadata_reason"
            ),
        "local_path":
            str(OUT),
        "rights_status":
            "unverified",
        "creator_permission_verified":
            False,
        "game_rights_verified":
            False,
        "acquisition_context":
            "automated_public_pipeline",
        "public_publish_allowed":
            True,
    }

    RESULT.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return result


def main():

    if not RANKED.exists():
        raise RuntimeError(
            "Missing work/v12_ranked_candidates.json"
        )

    data = load_json(
        RANKED,
        {},
    )

    candidates = data.get(
        "candidates",
        [],
    )

    rejected = load_rejected_ids()

    # Ensure the permanent history file exists even when empty.
    save_rejected_ids(
        rejected
    )

    print(
        f"Ranked candidates available: "
        f"{len(candidates)}"
    )

    print(
        f"Permanently rejected/previously "
        f"attempted IDs: {len(rejected)}"
    )

    errors = []

    for rank, candidate in enumerate(
        candidates,
        1,
    ):

        clip_id = candidate_id(
            candidate
        )

        if not clip_id:
            continue

        if clip_id in rejected:

            print(
                f"SKIP rank {rank}: "
                f"permanently rejected "
                f"{clip_id}"
            )

            continue

        print(
            f"ACQUIRE rank {rank}: "
            f"{candidate.get('game')} / "
            f"{candidate.get('channel')} / "
            f"{clip_id} / "
            f"metadata score "
            f"{candidate.get('v12_metadata_score')}"
        )

        try:

            result = acquire(
                candidate
            )

            print(
                f"ACQUIRED: "
                f"{result['clip_id']} -> "
                f"{result['local_path']}"
            )

            return

        except Exception as exc:

            print(
                f"ACQUISITION FAILED for "
                f"{clip_id}: {exc}"
            )

            errors.append(
                {
                    "clip_id":
                        clip_id,
                    "error":
                        str(exc),
                }
            )

            # A source that cannot be acquired should not keep
            # consuming expensive attempts in future runs.
            rejected.add(
                clip_id
            )

            save_rejected_ids(
                rejected
            )

    RESULT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    RESULT.write_text(
        json.dumps(
            {
                "success":
                    False,
                "reason":
                    "ranked_batch_exhausted",
                "errors":
                    errors,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Save once more before failing so GitHub Actions can
    # persist every rejection generated during this invocation.
    save_rejected_ids(
        rejected
    )

    raise RuntimeError(
        "No remaining ranked candidate could be acquired."
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            "KICK V12 ACQUISITION FAILED:",
            exc,
        )

        sys.exit(1)
