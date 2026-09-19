import json, subprocess, sys
from pathlib import Path

LOG = Path("work/v12_attempt_log.json")
REJECTED = Path("work/rejected_clip_ids.json")
MAX_EXPENSIVE_ATTEMPTS = 8

def run(script, ok=(0,)):
    p = subprocess.run([sys.executable, script])
    return p.returncode

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def save_log(rows):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

def current_acquisition():
    return load_json("work/kick_gaming/acquisition_result.json", {})

def main():
    attempts = []

    # Discovery and ranking happen ONCE per scheduled run.
    if run("kick_game_discovery.py") != 0:
        raise RuntimeError("game discovery failed")
    if run("candidate_ranker_v12_1.py") != 0:
        raise RuntimeError("V12.1 candidate ranking failed")

    ranked = load_json("work/v12_ranked_candidates.json", {}).get("candidates", [])
    if not ranked:
        raise RuntimeError("ranked batch empty")

    # Preserve existing rejected IDs if present. Acquisition uses this list to move
    # through the already-ranked batch without rescanning all game categories.
    rejected = load_json(REJECTED, [])
    if isinstance(rejected, dict):
        rejected = rejected.get("clip_ids", [])
    rejected = list(dict.fromkeys(rejected))

    for attempt_no in range(1, MAX_EXPENSIVE_ATTEMPTS + 1):
        code = run("kick_gaming_acquisition_v12.py")
        if code != 0:
            attempts.append({"attempt":attempt_no,"result":"failed","reason":"ranked_batch_exhausted_or_acquisition_failed"})
            save_log(attempts)
            break

        acq = current_acquisition()
        clip_id = acq.get("clip_id")
        row = {
            "attempt":attempt_no,
            "clip_id":clip_id,
            "channel":acq.get("channel"),
            "game":acq.get("game"),
            "metadata_score":acq.get("v12_metadata_score")
        }

        # Expensive multimodal quality gate.
        if run("viral_gate.py") != 0:
            row.update({"result":"rejected","reason":"viral_quality_gate"})
            attempts.append(row)
            if clip_id and clip_id not in rejected:
                rejected.append(clip_id)
                REJECTED.parent.mkdir(parents=True, exist_ok=True)
                REJECTED.write_text(json.dumps(rejected, indent=2), encoding="utf-8")
            save_log(attempts)
            continue

        # Commercial music gate.
        if run("music_gate.py") != 0:
            row.update({"result":"rejected","reason":"commercial_music_gate"})
            attempts.append(row)
            if clip_id and clip_id not in rejected:
                rejected.append(clip_id)
                REJECTED.write_text(json.dumps(rejected, indent=2), encoding="utf-8")
            save_log(attempts)
            continue

        # Existing frozen V5 Shorts production.
        if run("production_test.py") != 0:
            row.update({"result":"failed","reason":"production"})
            attempts.append(row); save_log(attempts)
            raise RuntimeError("Short production failed")

        # Final selected-segment content/gambling gate.
        if run("final_content_gate.py") != 0:
            row.update({"result":"rejected","reason":"final_content_gate"})
            attempts.append(row)
            if clip_id and clip_id not in rejected:
                rejected.append(clip_id)
                REJECTED.write_text(json.dumps(rejected, indent=2), encoding="utf-8")
            save_log(attempts)
            continue

        row.update({"result":"accepted","reason":"all_gates_passed"})
        attempts.append(row)
        save_log(attempts)
        print(f"V12.1 SUCCESS on expensive attempt {attempt_no}: {clip_id}")
        return

    save_log(attempts)
    raise RuntimeError(f"V12.1 found no publishable Short after {MAX_EXPENSIVE_ATTEMPTS} expensive attempts.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("V12.1 PIPELINE FAILED:", e)
        sys.exit(1)
