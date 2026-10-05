import array
import base64
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI
from firefight_cache import load as load_cache, current as cached_verdict, remember


INPUT = Path("work/v12_ranked_candidates.json")
OUT = Path("work/v12_firefight_candidates.json")
WORK = Path("work/firefight_prescreen")
WORK.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------
# V12.14.7
# LOCAL FIRST -> AI SECOND
#
# Up to 100 clips are screened LOCALLY with FFmpeg/Python.
# At most 12 clips reach paid vision; stop once four have passed.
# Paid confirmation is mandatory; motion-only candidates cannot publish.
# ------------------------------------------------------------

MAX_CANDIDATES = 100

LOCAL_AI_CANDIDATES = min(12, max(1, int(os.getenv("FIREFIGHT_MAX_PAID_CLIPS", "12"))))
TARGET_SURVIVORS = 4
MIN_SURVIVORS = 16

MIN_FIREFIGHT_SCORE = 70
FRAME_WORKERS = 8
LOCAL_WORKERS = 4
AI_BATCH_SIZE = 4

# Low-resolution raw grayscale frames.
LOCAL_WIDTH = 96
LOCAL_HEIGHT = 54
LOCAL_FRAME_BYTES = LOCAL_WIDTH * LOCAL_HEIGHT
LOCAL_FPS = "1/5"

# These are the shooter / ranged-combat lanes where "firefight" is literal
# or very close to literal.
SHOOTER_GAMES = {
    "call of duty: warzone",
    "valorant",
    "counter-strike 2",
    "fortnite",
    "apex legends",
    "marvel rivals",
    "overwatch 2",
    "call of duty: black ops 7",
    "escape from tarkov",
    "rust",
    "grand theft auto v (gta)",
}

TITLE_COMBAT_TERMS = {
    "clutch",
    "ace",
    "1v1",
    "1v2",
    "1v3",
    "1v4",
    "1v5",
    "kill",
    "kills",
    "wipe",
    "wiped",
    "squad",
    "headshot",
    "sniper",
    "noscope",
    "no scope",
    "gunfight",
    "fight",
    "shot",
    "shots",
    "beam",
    "beamed",
    "frag",
    "elimination",
    "knock",
    "down",
}


def safe_name(value):
    return re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(value or "clip"),
    )[:80]


def clamp(value):
    try:
        return max(
            0,
            min(
                100,
                int(
                    round(
                        float(value)
                    )
                ),
            ),
        )
    except Exception:
        return 0


def parse_json(raw):
    raw = str(raw or "").strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def data_url(path):
    encoded = base64.b64encode(
        Path(path).read_bytes()
    ).decode("ascii")
    return "data:image/jpeg;base64," + encoded


def title_term_score(candidate):
    text = " ".join(
        [
            str(candidate.get("page_title", "")),
            str(candidate.get("metadata_text", "")),
            " ".join(candidate.get("action_hits", []) or []),
        ]
    ).casefold()

    hits = sum(
        1
        for term in TITLE_COMBAT_TERMS
        if term in text
    )

    return min(
        100.0,
        hits * 18.0,
    )


def frame_motion_metrics(media_url):
    """
    One local FFmpeg process per clip.

    We decode a tiny 96x54 grayscale frame every ~5 seconds and calculate:
      - average frame-to-frame pixel change
      - burst motion (how many pixels change substantially)
      - sustained motion (how many frame transitions are active)

    This is intentionally cheap and local. It is NOT the final firearm
    classifier. Its purpose is to eliminate obviously static / menu /
    spectating / low-action clips before any paid vision call.
    """

    command = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-i",
        media_url,
        "-vf",
        f"fps={LOCAL_FPS},scale={LOCAL_WIDTH}:{LOCAL_HEIGHT},format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]

    proc = subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=60,
    )

    raw = proc.stdout

    if len(raw) < LOCAL_FRAME_BYTES * 2:
        return {
            "frame_count": 0,
            "avg_change": 0.0,
            "burst_change": 0.0,
            "active_transition_ratio": 0.0,
        }

    count = len(raw) // LOCAL_FRAME_BYTES

    frames = [
        raw[
            i * LOCAL_FRAME_BYTES:
            (i + 1) * LOCAL_FRAME_BYTES
        ]
        for i in range(count)
    ]

    transition_means = []
    transition_bursts = []

    previous = frames[0]

    for current in frames[1:]:
        total = 0
        burst_pixels = 0

        for a, b in zip(previous, current):
            diff = abs(a - b)
            total += diff

            if diff >= 22:
                burst_pixels += 1

        mean_change = total / LOCAL_FRAME_BYTES
        burst_ratio = burst_pixels / LOCAL_FRAME_BYTES

        transition_means.append(mean_change)
        transition_bursts.append(burst_ratio)

        previous = current

    avg_change = (
        sum(transition_means)
        /
        max(1, len(transition_means))
    )

    burst_change = (
        sum(transition_bursts)
        /
        max(1, len(transition_bursts))
    )

    # Transition is "active" when there is both meaningful whole-frame
    # motion and a non-trivial fraction of strongly changing pixels.
    active = 0

    for mean_change, burst_ratio in zip(
        transition_means,
        transition_bursts,
    ):
        if (
            mean_change >= 10.0
            and
            burst_ratio >= 0.12
        ):
            active += 1

    active_transition_ratio = (
        active
        /
        max(1, len(transition_means))
    )

    # A promising 35-second window, rather than whole-clip sustained motion.
    width = max(1, min(len(transition_means), 7))
    peak = max(range(max(1, len(transition_means) - width + 1)),
               key=lambda i: sum(transition_means[i:i + width]))
    return {
        "window_start": peak * 5.0,
        "frame_count":
            count,
        "avg_change":
            round(avg_change, 4),
        "burst_change":
            round(burst_change, 4),
        "active_transition_ratio":
            round(active_transition_ratio, 4),
    }


def local_candidate_score(candidate):
    media_url = str(
        candidate.get("media_url")
        or
        candidate.get("playlist_url")
        or
        ""
    ).strip()

    if not media_url:
        raise RuntimeError(
            "candidate has no media URL"
        )

    # Download once, then reuse the local preview for all frame extraction.
    preview = WORK / safe_name(candidate.get("clip_id", "")) / "preview.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.unlink(missing_ok=True)
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-rw_timeout", "12000000",
                        "-i", media_url, "-t", "120", "-map", "0:v:0", "-an",
                        "-vf", "scale=640:-2", "-r", "15", "-c:v", "libx264",
                        "-preset", "ultrafast", "-crf", "30", str(preview)],
                       check=True, capture_output=True, timeout=90)
        if not preview.is_file() or not preview.stat().st_size:
            raise RuntimeError("Empty local preview")
        metrics = frame_motion_metrics(str(preview))
    except Exception:
        preview.unlink(missing_ok=True)
        raise

    game = str(
        candidate.get(
            "game",
            "",
        )
    ).strip().casefold()

    shooter_prior = (
        100.0
        if game in SHOOTER_GAMES
        else 10.0
    )

    # Map local raw motion into a loose 0-100 range.
    motion_score = min(
        100.0,
        metrics[
            "avg_change"
        ] * 4.5,
    )

    burst_score = min(
        100.0,
        metrics[
            "burst_change"
        ] * 260.0,
    )

    sustained_score = min(
        100.0,
        metrics[
            "active_transition_ratio"
        ] * 100.0,
    )

    title_score = title_term_score(
        candidate
    )

    # Firefight-first means shooter eligibility and sustained visual activity
    # matter more than raw views at this stage.
    score = (
        motion_score * 0.28
        +
        burst_score * 0.25
        +
        sustained_score * 0.27
        +
        shooter_prior * 0.15
        +
        title_score * 0.05
    )

    row = dict(candidate)
    row["local_preview_path"] = str(preview)
    row["local_window_start"] = metrics.get("window_start", 0.0)
    row["local_preview_seconds"] = min(120.0, float(candidate.get("source_duration_seconds") or 120))

    row[
        "local_firefight_score"
    ] = round(
        max(
            0.0,
            min(
                100.0,
                score,
            ),
        ),
        2,
    )

    row[
        "local_motion_score"
    ] = round(
        motion_score,
        2,
    )

    row[
        "local_burst_score"
    ] = round(
        burst_score,
        2,
    )

    row[
        "local_sustained_motion_score"
    ] = round(
        sustained_score,
        2,
    )

    row[
        "local_title_combat_score"
    ] = round(
        title_score,
        2,
    )

    row[
        "local_shooter_game"
    ] = bool(
        game in SHOOTER_GAMES
    )

    row[
        "local_frame_count"
    ] = metrics[
        "frame_count"
    ]

    return row


def local_scan_parallel(candidates):
    rows = []

    with ThreadPoolExecutor(
        max_workers=LOCAL_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                local_candidate_score,
                candidate,
            ):
                candidate
            for candidate in candidates
        }

        for future in as_completed(
            future_map
        ):
            candidate = future_map[
                future
            ]

            try:
                rows.append(
                    future.result()
                )
            except Exception as exc:
                print(
                    f"LOCAL FIREFIGHT SKIP "
                    f"{candidate.get('clip_id')}: "
                    f"{exc}"
                )

    rows.sort(
        key=lambda row: (
            float(
                row.get(
                    "local_firefight_score",
                    0,
                )
            ),
            float(
                row.get(
                    "v12_metadata_score",
                    0,
                )
                or
                0
            ),
        ),
        reverse=True,
    )

    return rows


def extract_ai_frames(candidate):
    """
    Slightly richer visual evidence only for the small batch of clips that survived
    the local filter.
    """

    media_url = candidate.get("local_preview_path", "")
    duration = float(candidate.get("local_preview_seconds") or 0)
    window_start = max(0.0, float(candidate.get("local_window_start") or 0))
    window_start = min(window_start, max(0.0, duration - 35))
    window_seconds = min(35.0, duration - window_start)

    clip_id = str(
        candidate.get(
            "clip_id",
            "",
        )
    )

    if (
        not media_url
        or
        duration <= 0
    ):
        return []

    fractions = [
        0.16,
        0.34,
        0.52,
        0.70,
        0.86,
    ]

    clip_dir = (
        WORK
        /
        safe_name(
            clip_id
        )
    )

    clip_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output = []

    for index, fraction in enumerate(
        fractions
    ):
        timestamp = max(
            0.5,
            min(
                duration - 0.5,
                window_start + window_seconds * fraction,
            ),
        )

        path = (
            clip_dir
            /
            f"ai_{index}.jpg"
        )

        path.unlink(missing_ok=True)
        command = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            media_url,
            "-frames:v",
            "1",
            "-vf",
            "scale=384:-2",
            "-q:v",
            "5",
            str(path),
        ]

        try:
            subprocess.run(
                command,
                check=True,
                timeout=35,
            )
        except Exception:
            continue

        if (
            path.exists()
            and
            path.stat().st_size > 1000
        ):
            output.append(
                {
                    "timestamp":
                        round(
                            timestamp,
                            2,
                        ),
                    "path":
                        str(
                            path
                        ),
                }
            )

    return output


def extract_ai_parallel(candidates):
    results = {}

    with ThreadPoolExecutor(
        max_workers=FRAME_WORKERS
    ) as executor:
        future_map = {
            executor.submit(
                extract_ai_frames,
                candidate,
            ):
                candidate
            for candidate in candidates
        }

        for future in as_completed(
            future_map
        ):
            candidate = future_map[
                future
            ]

            clip_id = str(
                candidate.get(
                    "clip_id",
                    "",
                )
            )

            try:
                results[
                    clip_id
                ] = future.result()
            except Exception:
                results[
                    clip_id
                ] = []

    return results


def classify_batch(
    client,
    batch,
):
    prompt = """
You are the PAID SECOND-STAGE ACTIVE-FIREFIGHT CHECK for ViralSpawnTV.

The clips reaching you have ALREADY survived a free local motion/action
prefilter. Your job is narrower:

KEEP clips that visibly contain REAL direct PvP firefights/ranged combat.

For shooter games, require convincing evidence such as:
- firearm/weapon fire
- visible enemies being actively engaged
- aiming and firing exchanges
- hit markers / damage / shield breaks
- direct firearm/ranged-weapon engagement (abilities alone do not qualify)
- kill/knock/elimination activity
- multiple sampled moments from the same active fight

You see the most promising window, not the entire source. Accept a source
with at least two convincing direct gunfight samples, even if the entire
source has downtime. A separate mandatory gate verifies the exact final edit.
Set direct_gunfight true ONLY when direct weapon engagement is visible.
Aiming, running, melee, camera motion or celebration alone must be false.

Do NOT treat these as firefights:
- merely holding an angle
- running toward combat
- looting
- inventory/menu screens
- spectating
- chase-only footage
- melee-only action
- post-fight celebration
- one isolated shot with otherwise inactive footage

Return ONLY JSON:
{
  "results": [
    {
      "id": 0,
      "firefight_score": 0-100,
      "active_samples": 0-5,
      "sustained_combat": true,
      "direct_gunfight": true,
      "enemy_engagement": 0-100,
      "combat_intensity": 0-100,
      "dead_time_risk": 0-100,
      "content_type": "gaming",
      "reason": "short explanation"
    }
  ]
}

A firefight_score of 60+ should mean this clip is genuinely worth sending
to the more expensive payoff/story pipeline.
"""

    content = [
        {
            "type":
                "input_text",
            "text":
                prompt,
        }
    ]

    for local_id, item in enumerate(
        batch
    ):
        candidate = item[
            "candidate"
        ]

        content.append(
            {
                "type":
                    "input_text",
                "text":
                    (
                        f"CANDIDATE {local_id}\n"
                        f"clip_id={candidate.get('clip_id')}\n"
                        f"game={candidate.get('game')}\n"
                        f"title={candidate.get('page_title', '')}\n"
                        f"local_firefight_score="
                        f"{candidate.get('local_firefight_score')}\n"
                        f"local_motion="
                        f"{candidate.get('local_motion_score')}\n"
                        f"local_sustained="
                        f"{candidate.get('local_sustained_motion_score')}\n"
                        "NEXT: SAMPLED FRAMES"
                    ),
            }
        )

        for frame in item.get(
            "frames",
            [],
        ):
            path = Path(
                frame[
                    "path"
                ]
            )

            if not path.exists():
                continue

            content.append(
                {
                    "type":
                        "input_text",
                    "text":
                        (
                            f"FRAME around "
                            f"{frame['timestamp']:.2f}s"
                        ),
                }
            )

            content.append(
                {
                    "type":
                        "input_image",
                    "image_url":
                        data_url(
                            path
                        ),
                }
            )

    response = client.responses.create(
        max_output_tokens=2200,
        model=os.getenv("FIREFIGHT_MODEL", "gpt-5.6"),
        input=[
            {
                "role":
                    "user",
                "content":
                    content,
            }
        ],
    )

    data = parse_json(
        response.output_text
    )

    return data.get(
        "results",
        [],
    )


def final_ai_rank(row):
    return round(
        row[
            "active_firefight_score"
        ] * 0.65
        +
        row[
            "active_enemy_engagement"
        ] * 0.15
        +
        row[
            "active_combat_intensity"
        ] * 0.10
        +
        float(
            row.get(
                "local_firefight_score",
                0,
            )
        ) * 0.10,
        2,
    )


def local_only_survivors(local_rows):
    # Motion cannot confirm gunfire. No unverified fallback is promoted.
    return []


def confirmed_gunfight(row):
    # Broad source eligibility; the exact edit still faces the strict final gate.
    return (row.get("local_shooter_game") is True
            and row.get("active_direct_gunfight") is True
            and row.get("active_firefight_score", 0) >= 60
            and row.get("active_firefight_samples", 0) >= 2)


def valid_verdict(result):
    if not isinstance(result, dict):
        return False
    if type(result.get("direct_gunfight")) is not bool:
        return False
    if type(result.get("active_samples")) is not int or not 0 <= result["active_samples"] <= 5:
        return False
    if type(result.get("sustained_combat")) is not bool:
        return False
    for name in ("firefight_score", "enemy_engagement", "combat_intensity", "dead_time_risk"):
        value = result.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
            return False
    return True


def apply_verdict(row, result):
    row = dict(row)
    for source, target in (("firefight_score", "active_firefight_score"),
                           ("active_samples", "active_firefight_samples"),
                           ("sustained_combat", "active_sustained_combat"),
                           ("direct_gunfight", "active_direct_gunfight"),
                           ("enemy_engagement", "active_enemy_engagement"),
                           ("combat_intensity", "active_combat_intensity"),
                           ("dead_time_risk", "active_dead_time_risk"),
                           ("reason", "active_firefight_reason")):
        row[target] = result.get(source)
    row["active_firefight_rank"] = final_ai_rank(row)
    return row


def main():
    started = time.perf_counter()
    OUT.unlink(missing_ok=True)
    payload = json.loads(INPUT.read_text(encoding="utf-8"))
    cache = load_cache()
    rows = []
    seen = set()
    for row in payload.get("candidates", []):
        if not isinstance(row, dict):
            continue
        clip_id = str(row.get("clip_id", ""))
        if not clip_id or clip_id in seen:
            continue
        seen.add(clip_id)
        if str(row.get("game", "")).casefold() not in SHOOTER_GAMES:
            continue
        rows.append(row)
        if len(rows) >= MAX_CANDIDATES:
            break
    report = {"version": "budget-screen-v1", "input_count": len(rows),
              "paid_clip_limit": LOCAL_AI_CANDIDATES, "paid_clip_count": 0,
              "cached_passes": 0, "cached_rejects": 0, "decisions": [], "errors": []}
    survivors = []
    uncached = []
    for row in rows:
        entry = cached_verdict(row, cache)
        if entry:
            checked = apply_verdict(dict(row, local_shooter_game=True, local_window_start=entry.get("window_start")), entry["verdict"])
            if entry["passed"] and confirmed_gunfight(checked):
                survivors.append(checked)
                report["cached_passes"] += 1
            else:
                report["cached_rejects"] += 1
        else:
            uncached.append(row)
    print(f"FREE SCREEN: {len(rows)} unique shooter clips; "
          f"{report['cached_passes']} cached passes, {report['cached_rejects']} cached rejects")
    local_rows = []
    try:
        if len(survivors) < TARGET_SURVIVORS:
            local_rows = local_scan_parallel(uncached)
            # A loose motion threshold removes static footage; it never confirms combat.
            shortlist = [row for row in local_rows if row.get("local_shooter_game") is True
                         and row.get("local_frame_count", 0) >= 5
                         and row.get("local_motion_score", 0) >= 12][:LOCAL_AI_CANDIDATES]
            report["local_scored_count"] = len(local_rows)
            report["local_shortlist_count"] = len(shortlist)
            print(f"LOCAL SCREEN: {len(uncached)} attempted -> {len(local_rows)} decoded "
                  f"-> {len(shortlist)} shortlisted; at most {LOCAL_AI_CANDIDATES} paid reviews")
            client = OpenAI()
            for start in range(0, len(shortlist), AI_BATCH_SIZE):
                if len(survivors) >= TARGET_SURVIVORS:
                    print("EARLY STOP: four confirmed source candidates available")
                    break
                batch_rows = shortlist[start:start + AI_BATCH_SIZE]
                frames_by_id = extract_ai_parallel(batch_rows)
                batch = []
                for row in batch_rows:
                    frames = frames_by_id.get(str(row.get("clip_id")), [])
                    if len(frames) != 5:
                        report["errors"].append({"clip_id": row.get("clip_id"), "reason": "Incomplete frames; no paid review/cache"})
                        continue
                    batch.append({"candidate": row, "frames": frames})
                if not batch:
                    continue
                report["paid_clip_count"] += len(batch)
                results = classify_batch(client, batch)
                # Duplicate or missing IDs cannot become approvals or cached rejections.
                by_id = {}
                duplicates = set()
                if not isinstance(results, list):
                    results = []
                for result in results:
                    if not isinstance(result, dict) or type(result.get("id")) is not int:
                        continue
                    if result["id"] in by_id:
                        duplicates.add(result["id"])
                    by_id[result["id"]] = result
                for index, item in enumerate(batch):
                    row = item["candidate"]
                    result = by_id.get(index)
                    if index in duplicates or not valid_verdict(result):
                        report["errors"].append({"clip_id": row.get("clip_id"), "reason": "Invalid/missing AI verdict; not cached"})
                        continue
                    checked = apply_verdict(row, result)
                    passed = confirmed_gunfight(checked)
                    remember(row, result, passed)
                    decision = {"clip_id": row.get("clip_id"), "passed": passed,
                                "window_start": row.get("local_window_start"), "verdict": result}
                    report["decisions"].append(decision)
                    print(f"PAID SCREEN {'PASS' if passed else 'REJECT'} {row.get('clip_id')}: "
                          f"gunfight={result['direct_gunfight']} score={result['firefight_score']} "
                          f"active={result['active_samples']}/5 | {result.get('reason', '')}")
                    if passed:
                        survivors.append(checked)
    except Exception as exc:
        report["errors"].append({"reason": str(exc)})
        print(f"PAID SCREEN STOPPED: {exc}")
    finally:
        survivors.sort(key=lambda row: row.get("active_firefight_rank", 0), reverse=True)
        survivors = survivors[:TARGET_SURVIVORS]
        report["survivor_count"] = len(survivors)
        report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "screening_report.json").write_text(json.dumps(report, indent=2))
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"version": "budget-screen-v1", "candidates": survivors,
                                   "survivor_count": len(survivors)}, indent=2))
    if not survivors:
        raise RuntimeError("No confirmed gunfight source survived; skipping upload. See screening_report.json")
    print(f"FIREFIGHT COMPLETE: {len(survivors)} confirmed sources; "
          f"{report['paid_clip_count']} paid clip reviews")


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"BUDGET FIREFIGHT FILTER ERROR: "
            f"{exc}"
        )
        sys.exit(1)
