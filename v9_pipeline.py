import json
import shutil
import subprocess
import sys
from pathlib import Path


MAX_ATTEMPTS = 4

ACQUISITION = Path("work/kick_gaming/acquisition_result.json")
REJECTED = Path("work/rejected_clip_ids.json")
ATTEMPT_LOG = Path("work/v9_attempt_log.json")


def run_script(name):
    print()
    print("=" * 70)
    print(f"RUNNING: {name}")
    print("=" * 70)

    result = subprocess.run(
        [sys.executable, name],
        check=False,
    )

    return result.returncode == 0


def current_clip_id():
    if not ACQUISITION.exists():
        return None

    try:
        data = json.loads(
            ACQUISITION.read_text(encoding="utf-8")
        )
        return data.get("clip_id")
    except Exception:
        return None


def reject_current(reason, rejected_ids, attempts):
    clip_id = current_clip_id()

    if clip_id and clip_id not in rejected_ids:
        rejected_ids.append(clip_id)

    REJECTED.parent.mkdir(parents=True, exist_ok=True)
    REJECTED.write_text(
        json.dumps(rejected_ids, indent=2),
        encoding="utf-8",
    )

    attempts.append({
        "attempt": len(attempts) + 1,
        "clip_id": clip_id,
        "result": "rejected",
        "reason": reason,
    })


def clean_attempt_outputs():
    # Preserve rejected_clip_ids.json, but clear outputs that could
    # accidentally leak from a previous attempt.
    for path in [
        Path("work/kick_gaming"),
        Path("work/viral_gate"),
        Path("work/music_gate"),
        Path("work/production"),
    ]:
        if path.exists():
            shutil.rmtree(path)


def save_log(attempts):
    ATTEMPT_LOG.parent.mkdir(parents=True, exist_ok=True)
    ATTEMPT_LOG.write_text(
        json.dumps(attempts, indent=2),
        encoding="utf-8",
    )


def main():
    rejected_ids = []
    attempts = []

    if REJECTED.exists():
        REJECTED.unlink()

    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        print()
        print("#" * 70)
        print(f"V9 PRODUCTION ATTEMPT {attempt_number}/{MAX_ATTEMPTS}")
        print("#" * 70)

        clean_attempt_outputs()

        if not run_script("kick_gaming_acquisition.py"):
            attempts.append({
                "attempt": attempt_number,
                "result": "failed",
                "reason": "acquisition_failed",
            })
            save_log(attempts)
            continue

        if not run_script("viral_gate.py"):
            reject_current(
                "viral_quality_gate",
                rejected_ids,
                attempts,
            )
            save_log(attempts)
            continue

        if not run_script("music_gate.py"):
            reject_current(
                "music_gate",
                rejected_ids,
                attempts,
            )
            save_log(attempts)
            continue

        if not run_script("production_test.py"):
            reject_current(
                "production_failed",
                rejected_ids,
                attempts,
            )
            save_log(attempts)
            continue

        if not run_script("final_content_gate.py"):
            reject_current(
                "final_content_gate",
                rejected_ids,
                attempts,
            )
            save_log(attempts)
            continue

        clip_id = current_clip_id()

        attempts.append({
            "attempt": attempt_number,
            "clip_id": clip_id,
            "result": "ready_for_public_upload",
        })
        save_log(attempts)

        print()
        print("=" * 70)
        print("V9 FOUND A PUBLISHABLE SHORT")
        print("=" * 70)
        print(f"Clip ID: {clip_id}")
        return

    save_log(attempts)

    raise RuntimeError(
        f"No publishable Short found after {MAX_ATTEMPTS} attempts."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"V9 ORCHESTRATOR FAILED: {exc}")
        sys.exit(1)
