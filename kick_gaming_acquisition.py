import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


MANIFEST_PATH = Path("work/v11_candidate_manifest.json")
REJECTED_PATH = Path("work/rejected_clip_ids.json")
OUTPUT_DIR = Path("work/kick_gaming")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

GAMBLING_TERMS = {
    "casino", "slots", "slot machine", "gambling", "roulette",
    "blackjack", "sportsbook", "sports betting", "betting",
}


def rejected_ids():
    if not REJECTED_PATH.exists():
        return set()
    try:
        data = json.loads(REJECTED_PATH.read_text(encoding="utf-8"))
        return {str(x) for x in data if x}
    except Exception:
        return set()


def inspect_clip(clip_url, clip_id):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1000},
        )

        network_urls = []

        def capture(request):
            if ".m3u8" in request.url:
                network_urls.append(request.url)

        page.on("request", capture)
        page.goto(clip_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4500)

        html = page.content()
        title = page.title() or ""

        description = ""
        try:
            node = page.locator('meta[name="description"]')
            if node.count():
                description = node.first.get_attribute("content") or ""
        except Exception:
            pass

        browser.close()

    context = f"{title} {description}".lower()
    gambling = [term for term in GAMBLING_TERMS if term in context]
    if gambling:
        raise RuntimeError(f"gambling metadata: {', '.join(gambling[:3])}")

    html_urls = re.findall(
        r'https?[^"\'\\\s]+?\.m3u8[^"\'\\\s<]*',
        html,
    )

    cleaned = []
    for value in network_urls + html_urls:
        value = (
            value.replace("\\u0026", "&")
            .replace("\\/", "/")
            .replace("&amp;", "&")
        )
        if value not in cleaned:
            cleaned.append(value)

    exact = [url for url in cleaned if clip_id in url]
    if not exact:
        raise RuntimeError("No media playlist for this clip.")

    return exact[0], title, description


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
        raise RuntimeError("Downloaded clip is missing or unexpectedly small.")


def main():
    if not MANIFEST_PATH.exists():
        raise RuntimeError("Missing V11 candidate manifest.")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rejected = rejected_ids()
    output = OUTPUT_DIR / "selected_kick_gaming_source.mp4"

    # Important: this does NOT rescan categories. It walks the already-built
    # candidate batch until it acquires one usable candidate.
    for rank, candidate in enumerate(manifest.get("candidates", []), start=1):
        clip_id = candidate["clip_id"]

        if clip_id in rejected:
            continue

        print(
            f"V11 candidate {rank}: {candidate['game']} / "
            f"{candidate['channel']} / {clip_id}"
        )

        try:
            playlist, title, description = inspect_clip(
                candidate["clip_url"],
                clip_id,
            )

            if output.exists():
                output.unlink()

            download(playlist, candidate["clip_url"], output)

            result = {
                **candidate,
                "candidate_rank": rank,
                "page_title": title,
                "page_description": description,
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

            print("V11 acquisition success.")
            return

        except Exception as exc:
            print(f"Candidate unusable; moving through existing batch: {exc}")

    raise RuntimeError("V11 exhausted the discovered candidate batch.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"V11 ACQUISITION FAILED: {exc}")
        sys.exit(1)
