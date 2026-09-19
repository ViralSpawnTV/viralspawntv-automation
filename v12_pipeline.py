import json
import shutil
import subprocess
import sys
from pathlib import Path


MAX_EXPENSIVE_ATTEMPTS = 3

ACQUISITION = Path("work/kick_gaming/acquisition_result.json")
REJECTED = Path("work/rejected_clip_ids.json")
ATTEMPT_LOG = Path("work/v12_attempt_log.json")


def run_script(name):
    print("\n" + "=" * 70)
    print(f"RUNNING: {name}")
    print("=" * 70)
    return subprocess.run([sys.executable, name], check=False).returncode == 0


def current_source():
    if not ACQUISITION.exists():
        return {}
    try:
        return json.loads(ACQUISITION.read_text(encoding="utf-8"))
    except Exception:
        return {}


def reject_current(reason, rejected_ids, attempts):
    source = current_source()
    clip_id = source.get("clip_id")

    if clip_id and clip_id not in rejected_ids:
        rejected_ids.append(clip_id)

    REJECTED.write_text(
        json.dumps(rejected_ids, indent=2),
        encoding="utf-8",
    )

    attempts.append({
        "attempt": len(attempts) + 1,
        "clip_id": clip_id,
        "channel": source.get("channel"),
        "game": source.get("game"),
        "metadata_score": source.get("v12_metadata_score"),
        "result": "rejected",
        "reason": reason,
    })


def clean_candidate_outputs():
    for path in [
        Path("work/kick_gaming"),
        Path("work/viral_gate"),
        Path("work/music_gate"),
        Path("work/production"),
    ]:
        if path.exists():
            shutil.rmtree(path)


def save(attempts):
    ATTEMPT_LOG.write_text(
        json.dumps(attempts, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main():
    Path("work").mkdir(exist_ok=True)
    attempts = []
    rejected_ids = []

    if REJECTED.exists():
        REJECTED.unlink()

    # Discover once.
    if not run_script("kick_game_discovery.py"):
        raise RuntimeError("V11 game-first discovery failed.")

    # Cheaply inspect/rank once.
    if not run_script("candidate_ranker.py"):
        raise RuntimeError("V12 candidate pre-ranking failed.")

    # Expensive analysis only touches the strongest ranked candidates.
    for attempt_number in range(1, MAX_EXPENSIVE_ATTEMPTS + 1):
        print("\n" + "#" * 70)
        print(f"V12 EXPENSIVE ATTEMPT {attempt_number}/{MAX_EXPENSIVE_ATTEMPTS}")
        print("#" * 70)

        clean_candidate_outputs()

        if not run_script("kick_gaming_acquisition.py"):
            attempts.append({
                "attempt": attempt_number,
                "result": "failed",
                "reason": "ranked_batch_exhausted",
            })
            save(attempts)
            break

        if not run_script("viral_gate.py"):
            reject_current("viral_quality_gate", rejected_ids, attempts)
            save(attempts)
            continue

        if not run_script("music_gate.py"):
            reject_current("music_gate", rejected_ids, attempts)
            save(attempts)
            continue

        if not run_script("production_test.py"):
            reject_current("production_failed", rejected_ids, attempts)
            save(attempts)
            continue

        if not run_script("final_content_gate.py"):
            reject_current("final_content_gate", rejected_ids, attempts)
            save(attempts)
            continue

        source = current_source()
        attempts.append({
            "attempt": attempt_number,
            "clip_id": source.get("clip_id"),
            "channel": source.get("channel"),
            "game": source.get("game"),
            "metadata_score": source.get("v12_metadata_score"),
            "result": "ready_for_public_upload",
        })
        save(attempts)

        print("\n" + "=" * 70)
        print("V12 FOUND A PUBLISHABLE SHORT")
        print("=" * 70)
        return

    save(attempts)
    raise RuntimeError(
        f"No publishable Short after {MAX_EXPENSIVE_ATTEMPTS} "
        "top-ranked candidates."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"V12 PIPELINE FAILED: {exc}")
        sys.exit(1)
