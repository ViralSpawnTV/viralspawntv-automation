import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright


OUTPUT_DIR = Path("work/kick_gaming")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_PATH = Path("history.json")
MAX_CLIPS_PER_CHANNEL = 15

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# PRIVATE TEST ONLY.
# Discovery/acquisition candidates are not claims of permission.
# Add/remove Kick gaming channels here as we expand testing.
TEST_GAMING_CHANNELS = [
    # Larger gaming-only discovery pool.
    # A dead/offline/no-clips channel is skipped automatically.
    "dona",
    "xqc",
    "piipou4k",
    "zitomo2",
    "crimsonskorpio",
    "sliccgaming",
    "sadatonn",
    "big_mikey",
    "30ibra",
    "thelostdrake",
    "brozak",
    "reaperreapz",
    "finesse-hlg",
    "ohnourkourt",
    "misterarther",
    "soyminatita",
]

GAMBLING_TERMS = {
    "casino", "slots", "slot", "gambling", "roulette",
    "blackjack", "sportsbook", "betting", "stake",
}

GAMING_TERMS = {
    "fortnite", "minecraft", "valorant", "warzone", "cod",
    "call of duty", "gta", "elden ring", "marvel rivals",
    "league", "rocket league", "apex", "overwatch", "cs2",
    "counter-strike", "gaming", "game", "ranked", "clutch",
    "kill", "kills", "elim", "elimination", "boss", "speedrun",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clip_id_from_url(url):
    match = re.search(r"(clip_[A-Za-z0-9_-]+)", url)
    return match.group(1) if match else None


def load_history():
    if not HISTORY_PATH.exists():
        return {"version": 1, "used_clips": []}

    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "used_clips": []}

    if isinstance(data, list):
        # Preserve compatibility with an older list-only history file.
        return {"version": 1, "used_clips": data}

    if not isinstance(data, dict):
        return {"version": 1, "used_clips": []}

    data.setdefault("version", 1)
    data.setdefault("used_clips", [])
    return data


def used_clip_ids(history):
    ids = set()

    for item in history.get("used_clips", []):
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict):
            value = item.get("clip_id")
            if value:
                ids.add(str(value))

    return ids


def get_clip_links(channel):
    url = f"https://kick.com/{channel}/clips"
    print(f"Opening Kick gaming clips page: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1000},
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)
        html = page.content()
        browser.close()

    patterns = [
        r'href=["\'](/[^"\']+/clips/clip_[A-Za-z0-9_-]+)["\']',
        r'https://kick\.com/[^"\'\\\s]+/clips/clip_[A-Za-z0-9_-]+',
    ]

    links = []

    for pattern in patterns:
        for match in re.findall(pattern, html):
            full_url = match if match.startswith("http") else f"https://kick.com{match}"
            if full_url not in links:
                links.append(full_url)

    print(f"Found {len(links)} clip links for {channel}.")
    return links


def inspect_clip(clip_url):
    clip_id = clip_id_from_url(clip_url)

    if not clip_id:
        raise RuntimeError(f"Could not determine clip ID from {clip_url}")

    print(f"Inspecting clip: {clip_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1000},
        )

        network_urls = []

        def capture_request(request):
            if ".m3u8" in request.url:
                network_urls.append(request.url)

        page.on("request", capture_request)
        page.goto(clip_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)

        html = page.content()

        title = ""
        try:
            title = page.title() or ""
        except Exception:
            pass

        description = ""
        try:
            node = page.locator('meta[name="description"]')
            if node.count():
                description = node.first.get_attribute("content") or ""
        except Exception:
            pass

        browser.close()

    html_urls = re.findall(
        r'https?[^"\'\\\s]+?\.m3u8[^"\'\\\s<]*',
        html,
    )

    cleaned = []

    for value in network_urls + html_urls:
        value = (
            value
            .replace("\\u0026", "&")
            .replace("\\/", "/")
            .replace("&amp;", "&")
        )
        if value not in cleaned:
            cleaned.append(value)

    exact = [url for url in cleaned if clip_id in url]

    if not exact:
        raise RuntimeError(
            "No media playlist belonging to the selected Kick clip was found."
        )

    context = f"{title} {description}".lower()

    return {
        "clip_id": clip_id,
        "playlist": exact[0],
        "page_title": title,
        "page_description": description,
        "context": context,
    }


def candidate_score(channel, position, context):
    # Newer/top-listed clips get a modest base advantage.
    score = max(0, 30 - position)

    for term in GAMING_TERMS:
        if term in context:
            score += 8

    for term in GAMBLING_TERMS:
        if term in context:
            score -= 1000

    # Tiny deterministic rotation bonus prevents channel order from
    # completely dominating selection when candidates are otherwise tied.
    score += sum(ord(c) for c in channel) % 7

    return score


def discover_candidates(history):
    used = used_clip_ids(history)
    candidates = []

    for channel in TEST_GAMING_CHANNELS:
        print()
        print(f"Searching gaming channel: {channel}")

        try:
            links = get_clip_links(channel)
        except Exception as exc:
            print(f"Channel discovery failed: {exc}")
            continue

        for position, clip_url in enumerate(
            links[:MAX_CLIPS_PER_CHANNEL],
            start=1,
        ):
            clip_id = clip_id_from_url(clip_url)

            if not clip_id:
                continue

            if clip_id in used:
                print(f"Skipping duplicate: {clip_id}")
                continue

            # We do not open every clip here; that would make discovery slow.
            # Initial ranking uses freshness/order. Media/context inspection
            # happens when attempting top candidates.
            candidates.append({
                "platform": "kick",
                "channel": channel,
                "clip_id": clip_id,
                "clip_url": clip_url,
                "position": position,
                "score": max(0, 30 - position)
                         + (sum(ord(c) for c in channel) % 7),
            })

    candidates.sort(
        key=lambda x: (
            x["score"],
            -x["position"],
        ),
        reverse=True,
    )

    print()
    print(f"Fresh candidate pool: {len(candidates)}")
    return candidates


def download_clip(playlist_url, clip_url, output_path):
    command = [
        "ffmpeg", "-y",
        "-user_agent", USER_AGENT,
        "-headers",
        (
            f"Referer: {clip_url}\r\n"
            "Origin: https://kick.com\r\n"
        ),
        "-i", playlist_url,
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]

    print("Acquiring selected Kick clip for PRIVATE pipeline test...")
    subprocess.run(command, check=True)

    if not output_path.exists():
        raise RuntimeError("FFmpeg completed but no MP4 was created.")

    if output_path.stat().st_size < 10000:
        raise RuntimeError("Downloaded MP4 is unexpectedly small.")

    print(
        f"Acquired {output_path} "
        f"({output_path.stat().st_size:,} bytes)"
    )


def choose_and_acquire(history):
    candidates = discover_candidates(history)

    if not candidates:
        raise RuntimeError(
            "No fresh Kick gaming clips remain in the configured candidate pool."
        )

    output_path = OUTPUT_DIR / "selected_kick_gaming_source.mp4"

    if output_path.exists():
        output_path.unlink()

    # Try ranked fresh candidates until one is both usable and not
    # obviously gambling based on available page metadata.
    for rank, candidate in enumerate(candidates[:40], start=1):
        print()
        print(
            f"Trying ranked candidate {rank}/"
            f"{min(len(candidates), 40)}"
        )
        print(candidate["clip_url"])

        try:
            inspected = inspect_clip(candidate["clip_url"])
            context = inspected["context"]

            score = candidate_score(
                candidate["channel"],
                candidate["position"],
                context,
            )

            if score < 0:
                print("Rejected by gambling/non-gaming metadata gate.")
                continue

            download_clip(
                inspected["playlist"],
                candidate["clip_url"],
                output_path,
            )

            return {
                "platform": "kick",
                "channel": candidate["channel"],
                "clip_id": candidate["clip_id"],
                "clip_url": candidate["clip_url"],
                "video_path": str(output_path),
                "selection_score": score,
                "candidate_rank": rank,
                "page_title": inspected["page_title"],
                "page_description": inspected["page_description"],

                # Downloadability is NOT republication permission.
                "rights_status": "unverified",
                "creator_permission_verified": False,
                "game_rights_verified": False,
                "acquisition_context": "private_pipeline_test",
                "public_publish_allowed": False,
                "selected_at_utc": utc_now(),
            }

        except Exception as exc:
            print(f"Candidate failed: {exc}")

    raise RuntimeError(
        "Fresh clips were found, but no usable gaming clip could be acquired."
    )


def main():
    print()
    print("================================================")
    print("ViralSpawnTV V6 Kick Source Rotation")
    print("================================================")
    print(
        "PRIVATE pipeline test only. Public publishing remains blocked."
    )

    history = load_history()
    print(
        f"Previously used clips: "
        f"{len(used_clip_ids(history))}"
    )

    result = choose_and_acquire(history)

    result_path = OUTPUT_DIR / "acquisition_result.json"
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print("================================================")
    print("V6 PRIVATE GAMING ACQUISITION SUCCESS")
    print("================================================")
    print(f"Channel: {result['channel']}")
    print(f"Clip ID: {result['clip_id']}")
    print(f"Clip: {result['clip_url']}")
    print(f"Selection score: {result['selection_score']}")
    print(f"Video: {result['video_path']}")
    print("Public publishing: BLOCKED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print()
        print("================================================")
        print("V6 PRIVATE ACQUISITION FAILED")
        print("================================================")
        print(str(exc))
        sys.exit(1)
