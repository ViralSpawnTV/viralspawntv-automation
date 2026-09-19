import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


RANKED_PATH = Path("work/v12_ranked_candidates.json")
REJECTED_PATH = Path("work/rejected_clip_ids.json")
OUTPUT_DIR = Path("work/kick_gaming")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def rejected_ids():
    if not REJECTED_PATH.exists():
        return set()
    try:
        data = json.loads(REJECTED_PATH.read_text(encoding="utf-8"))
        return {str(x) for x in data if x}
    except Exception:
        return set()


def find_playlist(clip_url, clip_id):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        urls = []

        def capture(request):
            if ".m3u8" in request.url:
                urls.append(request.url)

        page.on("request", capture)
        page.goto(clip_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
        html = page.content()
        browser.close()

    urls += re.findall(
        r'https?[^"\'\\\s]+?\.m3u8[^"\'\\\s<]*',
        html,
    )

    cleaned = []
    for value in urls:
        value = (
            value.replace("\\u0026", "&")
            .replace("\\/", "/")
            .replace("&amp;", "&")
        )
        if value not in cleaned:
            cleaned.append(value)

    exact = [u for u in cleaned if clip_id in u]
    if not exact:
        raise RuntimeError("No exact media playlist found.")

    return exact[0]


def download(playlist, clip_url, output):
    subprocess.run([
        "ffmpeg", "-y",
        "-user_agent", USER_AGENT,
        "-headers",
        f"Referer: {clip_url}\r\nOrigin: https://kick.com\r\n",
        "-i", playlist,
        "-c", "copy",
        "-movflags", "+faststart",
        str(output),
    ], check=True)

    if not output.exists() or output.stat().st_size < 10000:
        raise RuntimeError("Downloaded source is missing or too small.")


def main():
    if not RANKED_PATH.exists():
        raise RuntimeError("Missing V12 ranked candidate batch.")

    data = json.loads(RANKED_PATH.read_text(encoding="utf-8"))
    rejected = rejected_ids()
    output = OUTPUT_DIR / "selected_kick_gaming_source.mp4"

    for rank, candidate in enumerate(data.get("candidates", []), start=1):
        clip_id = candidate.get("clip_id")
        if not clip_id or clip_id in rejected:
            continue

        print(
            f"V12 ranked candidate {rank}: "
            f"score={candidate.get('v12_metadata_score')} | "
            f"{candidate.get('game')} | {candidate.get('channel')}"
        )

        try:
            playlist = find_playlist(candidate["clip_url"], clip_id)

            if output.exists():
                output.unlink()

            download(playlist, candidate["clip_url"], output)

            result = {
                **candidate,
                "candidate_rank": rank,
                "video_path": str(output),
                "rights_status": "unverified",
                "creator_permission_verified": False,
                "game_rights_verified": False,
                "acquisition_context": "automated_public_pipeline",
                "public_publish_allowed": True,
            }

            (OUTPUT_DIR / "acquisition_result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            print("V12 ranked acquisition success.")
            return

        except Exception as exc:
            print(f"Ranked candidate unusable; trying next: {exc}")

    raise RuntimeError("V12 exhausted the ranked candidate batch.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"V12 ACQUISITION FAILED: {exc}")
        sys.exit(1)
