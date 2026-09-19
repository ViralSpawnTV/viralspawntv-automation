import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright


OUTPUT_DIR = Path("work/kick_gaming")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_PATH = Path("history.json")
TEMP_REJECTED_PATH = Path("work/rejected_clip_ids.json")
MAX_CLIPS_PER_CHANNEL = 15
MAX_PUBLIC_UPLOADS_PER_CREATOR_24H = 2
DIVERSITY_WINDOW_HOURS = 24


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# PRIVATE TEST ONLY.
# Discovery/acquisition candidates are not claims of permission.
# Add/remove Kick gaming channels here as we expand testing.
TEST_GAMING_CHANNELS = [
    # Proven/current pool
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

    # Expanded gaming discovery pool
    "adinross",
    "trainwreckstv",
    "n3on",
    "westcol",
    "ac7ionman",
    "iceposeidon",
    "cuffem",
    "sweatergxd",
    "roshtein",
    "santana",
    "clix",
    "mongraal",
    "tfue",
    "symfuhny",
    "nickmercs",
    "scump",
    "shotzzy",
    "formal",
    "methodz",
    "cloakzy",
    "summit1g",
    "shroud",
    "tarik",
    "sacy",
    "gaules",
    "fps_shaka",
    "elraenn",
    "brucedropemoff",
    "yourrage",
    "rayasianboy",
    "carrington",
    "sneako",
    "rage",
    "agent00",
    "stable_ronaldo",
    "ronaldo",
    "ninja",
    "timthetatman",
    "lacy",
    "jasontheween",
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


def temporarily_rejected_clip_ids():
    if not TEMP_REJECTED_PATH.exists():
        return set()

    try:
        data = json.loads(
            TEMP_REJECTED_PATH.read_text(encoding="utf-8")
        )
    except Exception:
        return set()

    if not isinstance(data, list):
        return set()

    return {str(value) for value in data if value}




def parse_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except Exception:
        return None


def creator_public_upload_counts(history):
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=DIVERSITY_WINDOW_HOURS
    )
    counts = {}

    for item in history.get("used_clips", []):
        if str(item.get("privacy_status", "")).lower() != "public":
            continue

        channel = str(item.get("channel", "")).strip().lower()
        processed_at = parse_utc(item.get("processed_at_utc"))

        if not channel or processed_at is None or processed_at < cutoff:
            continue

        counts[channel] = counts.get(channel, 0) + 1

    return counts


def creator_is_capped(channel, public_counts):
    return public_counts.get(str(channel).lower(), 0) >= (
        MAX_PUBLIC_UPLOADS_PER_CREATOR_24H
    )




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
    used.update(temporarily_rejected_clip_ids())
    public_counts = creator_public_upload_counts(history)

    capped_creators = sorted(
        channel for channel, count in public_counts.items()
        if count >= MAX_PUBLIC_UPLOADS_PER_CREATOR_24H
    )
    print(
        "V10 creator diversity: "
        f"{MAX_PUBLIC_UPLOADS_PER_CREATOR_24H} public uploads/creator/"
        f"{DIVERSITY_WINDOW_HOURS}h max"
    )
    print(f"Currently capped creators: {capped_creators or 'none'}")

    candidates = []

    for channel in TEST_GAMING_CHANNELS:
        if creator_is_capped(channel, public_counts):
            print(
                f"Skipping {channel}: already has "
                f"{public_counts.get(channel.lower(), 0)} public uploads "
                f"in the last {DIVERSITY_WINDOW_HOURS} hours."
            )
            continue

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
                "acquisition_context": "automated_public_pipeline",
                "public_publish_allowed": True,
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
    print("ViralSpawnTV V10 Kick Source Rotation")
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
    print("Public publishing: ELIGIBLE FOR DOWNSTREAM GATES")


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
