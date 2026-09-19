import json, re, subprocess, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

RANKED = Path("work/v12_ranked_candidates.json")
REJECTED = Path("work/rejected_clip_ids.json")
OUTDIR = Path("work/kick_gaming")
OUT = OUTDIR / "selected_kick_gaming_source.mp4"
RESULT = OUTDIR / "acquisition_result.json"

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def rejected_ids():
    data = load_json(REJECTED, [])
    if isinstance(data, dict):
        data = data.get("clip_ids", [])
    return set(str(x) for x in data)

def candidate_id(c):
    return str(c.get("clip_id") or c.get("id") or "").strip()

def candidate_url(c):
    return str(c.get("clip_url") or c.get("url") or "").strip()

def acquire(candidate):
    clip_id = candidate_id(candidate)
    clip_url = candidate_url(candidate)
    if not clip_id or not clip_url:
        raise RuntimeError("Candidate missing clip_id or clip_url.")

    OUTDIR.mkdir(parents=True, exist_ok=True)
    playlist_urls = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def capture(response):
            u = response.url
            if ".m3u8" in u:
                playlist_urls.append(u)

        page.on("response", capture)
        page.goto(clip_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)

        # Try to start playback in case the playlist is lazy-loaded.
        try:
            page.locator("video").first.click(timeout=3000)
            page.wait_for_timeout(2500)
        except Exception:
            pass

        browser.close()

    if not playlist_urls:
        raise RuntimeError("No HLS playlist captured from clip page.")

    # Prefer a playlist URL that contains this clip ID, otherwise use the last
    # media playlist observed. FFmpeg can resolve master playlists as well.
    chosen = None
    for u in playlist_urls:
        if clip_id.lower() in u.lower():
            chosen = u
            break
    if not chosen:
        chosen = playlist_urls[-1]

    if OUT.exists():
        OUT.unlink()

    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-i", chosen,
        "-c", "copy",
        "-movflags", "+faststart",
        str(OUT)
    ]
    proc = subprocess.run(cmd)
    if proc.returncode != 0 or not OUT.exists() or OUT.stat().st_size < 10000:
        # More resilient fallback: normalize instead of stream-copy.
        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-i", chosen,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000",
            "-movflags", "+faststart",
            str(OUT)
        ]
        subprocess.run(cmd, check=True)

    if not OUT.exists() or OUT.stat().st_size < 10000:
        raise RuntimeError("Acquired output file is missing or too small.")

    result = {
        "success": True,
        "version": "12.1-compatible",
        "clip_id": clip_id,
        "clip_url": clip_url,
        "channel": candidate.get("channel"),
        "game": candidate.get("game"),
        "page_title": candidate.get("page_title"),
        "page_description": candidate.get("page_description"),
        "v12_metadata_score": candidate.get("v12_metadata_score"),
        "v12_metadata_reason": candidate.get("v12_metadata_reason"),
        "local_path": str(OUT),
        "rights_status": "unverified",
        "creator_permission_verified": False,
        "game_rights_verified": False,
        "acquisition_context": "automated_public_pipeline",
        "public_publish_allowed": True
    }
    RESULT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result

def main():
    if not RANKED.exists():
        raise RuntimeError("Missing work/v12_ranked_candidates.json")

    data = load_json(RANKED, {})
    candidates = data.get("candidates", [])
    rejected = rejected_ids()

    print(f"Ranked candidates available: {len(candidates)}")
    print(f"Rejected/previously attempted IDs: {len(rejected)}")

    errors = []
    for rank, candidate in enumerate(candidates, 1):
        cid = candidate_id(candidate)
        if not cid:
            continue
        if cid in rejected:
            print(f"SKIP rank {rank}: already rejected {cid}")
            continue

        print(
            f"ACQUIRE rank {rank}: {candidate.get('game')} / "
            f"{candidate.get('channel')} / {cid} / "
            f"metadata score {candidate.get('v12_metadata_score')}"
        )
        try:
            result = acquire(candidate)
            print(f"ACQUIRED: {result['clip_id']} -> {result['local_path']}")
            return
        except Exception as e:
            print(f"ACQUISITION FAILED for {cid}: {e}")
            errors.append({"clip_id": cid, "error": str(e)})
            # Mark acquisition failures so the next invocation moves forward.
            rejected.add(cid)
            REJECTED.parent.mkdir(parents=True, exist_ok=True)
            REJECTED.write_text(json.dumps(sorted(rejected), indent=2), encoding="utf-8")

    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({
        "success": False,
        "reason": "ranked_batch_exhausted",
        "errors": errors
    }, indent=2), encoding="utf-8")
    raise RuntimeError("No remaining ranked candidate could be acquired.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("KICK V12 ACQUISITION FAILED:", e)
        sys.exit(1)
