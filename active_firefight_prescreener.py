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
from firefight_cache import load as load_cache, current as cached_verdict, remember, exclude
from local_combat_screen import verified, POLICY as LOCAL_SCREEN_POLICY


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

LOCAL_AI_CANDIDATES = min(12, max(1, int(os.getenv("FIREFIGHT_MAX_PAID_CLIPS", "6"))))
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


def ensure_ai_preview(candidate):
    """Library stores scores/URLs, so prepare media only for this shortlist.

    This is a local FFmpeg operation. It does not change motion scores or
    grant visual approval. Failed downloads remain retryable.
    """
    existing = str(candidate.get("local_preview_path") or "")
    if existing and Path(existing).is_file() and candidate.get("local_preview_seconds", 0) > 0:
        return
    media_url = str(candidate.get("media_url") or candidate.get("playlist_url")
                    or candidate.get("prevalidated_media_url") or "").strip()
    if not media_url:
        raise RuntimeError("No source URL for visual preview")
    preview = WORK / safe_name(candidate.get("clip_id")) / "preview.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.unlink(missing_ok=True)
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-rw_timeout", "12000000",
                        "-i", media_url, "-t", "120", "-map", "0:v:0", "-an",
                        "-vf", "scale=640:-2", "-r", "15", "-c:v", "libx264",
                        "-preset", "ultrafast", "-crf", "30", str(preview)],
                       check=True, capture_output=True, timeout=90)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                                str(preview)], check=True, capture_output=True, text=True, timeout=15)
        seconds = float(probe.stdout.strip())
        if not math.isfinite(seconds) or seconds < 2 or not preview.stat().st_size:
            raise RuntimeError("Empty or invalid visual preview")
    except Exception:
        preview.unlink(missing_ok=True)
        candidate.pop("local_preview_path", None)
        candidate.pop("local_preview_seconds", None)
        raise
    candidate["local_preview_path"] = str(preview)
    candidate["local_preview_seconds"] = min(120.0, seconds)
    print(f"LIBRARY VISUAL PREVIEW: {candidate.get('clip_id')} ready ({seconds:.1f}s); no API call")


def extract_ai_frames(candidate):
    """
    Slightly richer visual evidence only for the small batch of clips that survived
    the local filter.
    """

    ensure_ai_preview(candidate)
    media_url = candidate.get("local_preview_path", "")
    duration = float(candidate.get("local_preview_seconds") or 0)
    window_start = max(0.0, float(candidate.get("local_window_start") or 0))
    requested_seconds = float(candidate.get("local_window_seconds") or 35)
    requested_seconds = max(19.0, min(35.0, requested_seconds))
    window_start = min(window_start, max(0.0, duration - requested_seconds))
    window_seconds = min(requested_seconds, duration - window_start)
    # Record the window actually sampled if the live source is shorter.
    candidate["local_window_start"] = window_start

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
            except Exception as exc:
                candidate["visual_preview_error"] = f"Preview/frame preparation failed: {type(exc).__name__}"
                print(f"VISUAL PREVIEW RETRYABLE: {clip_id}: {type(exc).__name__}")
                results[clip_id] = []

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

You see five samples from the most promising continuous window, not the entire
source. We need HIGH-INTENSITY combat, not merely a strong match result.
Require direct weapon engagement in at least FOUR of the FIVE sampled moments,
sustained_combat=true, firefight_score at least 80, and dead_time_risk at most 20.
Count only samples with visible firing/weapon exchange, not kill banners or aiming.
A victory, ace or clutch narrative cannot compensate for sparse combat or long
pauses. Blind firing without evidence of an actual engagement is not sufficient.
When uncertain, reject. The strict final action gate still verifies the exact edit.
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
                        f"window_seconds={candidate.get('local_window_seconds')}\n"
                        "Local screen is provisional; decide combat from these images.\n"
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
    # Spending filter: prefer dense combat before paying to plan a victory story.
    # Five sparse samples cannot guarantee the final edit, which keeps its gate.
    return (row.get("local_shooter_game") is True
            and row.get("active_direct_gunfight") is True
            and row.get("active_sustained_combat") is True
            and row.get("active_firefight_score", 0) >= 80
            and row.get("active_firefight_samples", 0) >= 4
            and row.get("active_dead_time_risk", 100) <= 20)


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
    unverified_count = 0
    seen = set()
    for row in payload.get("candidates", []):
        if not isinstance(row, dict):
            continue
        if not verified(row):
            unverified_count += 1
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
              "cached_passes": 0, "cached_rejects": 0, "unverified_skips":unverified_count, "decisions": [], "errors": []}
    survivors = []
    uncached = []
    for row in rows:
        entry = cached_verdict(row, cache)
        if entry and entry.get("passed") is True:
            # Reuse approval only for this exact local window/policy. Old
            # cached passes reviewed a different longer window.
            if (entry.get("local_screen_policy") != LOCAL_SCREEN_POLICY
                    or entry.get("window_seconds") != row.get("local_window_seconds")
                    or entry.get("window_start") != row.get("local_window_start")):
                entry = None
        if entry and entry.get("passed") is not True:
            report["cached_rejects"] += 1
            if entry.get("permanent_exclusion"):
                report["decisions"].append({"clip_id": row.get("clip_id"), "passed": False,
                    "paid": False, "permanent_exclusion": True,
                    "reason": entry.get("verdict", {}).get("reason", "Saved rejection")})
            continue
        if entry and valid_verdict(entry.get("verdict")):
            checked = apply_verdict(dict(row, local_shooter_game=True,
                                        local_window_start=entry.get("window_start")), entry["verdict"])
            if confirmed_gunfight(checked):
                survivors.append(checked)
                report["cached_passes"] += 1
            else:
                # Reuse the completed old screen as a spending decision, not a
                # fresh paid review of the same low-density candidate.
                exclude(row, "cached_action_evidence_below_density_requirement",
                        "Prior paid screen did not meet the high-intensity action requirement")
                report["cached_rejects"] += 1
                report["decisions"].append({"clip_id": row.get("clip_id"),
                    "passed": False, "paid": False, "permanent_exclusion": True,
                    "reason": "Prior paid evidence below action-density requirement"})
        else:
            uncached.append(row)
    print(f"FREE SCREEN: {len(rows)} unique shooter clips; "
          f"{report['cached_passes']} cached passes, {report['cached_rejects']} cached rejects")
    local_rows = []
    try:
        if len(survivors) < TARGET_SURVIVORS:
            # Library records are provisional local image/audio evidence, NOT visual approvals.
            # Reuse only complete records from our local scan policy; all still
            # face the paid gunfight confirmation and exact edit checks.
            library_rows = [row for row in uncached if verified(row)]
            library_ids = {row["clip_id"] for row in library_rows}
            local_rows = [dict(row, local_shooter_game=True) for row in library_rows]
            report["legacy_unverified_skips"] = len(uncached) - len(library_rows)
            local_rows.sort(key=lambda row: row.get("local_firefight_score", 0), reverse=True)
            shortlist = [row for row in local_rows if row.get("local_shooter_game") is True
                         and verified(row)][:LOCAL_AI_CANDIDATES]
            report["local_scored_count"] = len(local_rows)
            report["local_shortlist_count"] = len(shortlist)
            print(f"LOCAL EVIDENCE: {len(uncached)} uncached -> {len(local_rows)} verified library rows "
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
                        report["errors"].append({"clip_id": row.get("clip_id"), "reason": row.get("visual_preview_error", "Incomplete frames; no paid review/cache")})
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
                                "window_start": row.get("local_window_start"), "verdict": result,
                                "permanent_exclusion": not passed}
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
