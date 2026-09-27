import json
import subprocess
import sys
import time
from pathlib import Path


LOG = Path("work/v12_attempt_log.json")
REJECTED = Path("shorts_rejected_history.json")

MAX_EXPENSIVE_ATTEMPTS = 8


def run(script):
    process = subprocess.run(
        [
            sys.executable,
            script,
        ]
    )

    return process.returncode


def run_timed(
    label,
    script,
):
    started = time.perf_counter()

    code = run(
        script
    )

    elapsed = (
        time.perf_counter()
        -
        started
    )

    print(
        f"V12.11 TIMING | "
        f"{label}: "
        f"{elapsed:.1f}s"
    )

    return code



def load_json(path, default):
    try:
        return json.loads(
            Path(path).read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return default


def save_log(rows):
    LOG.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    LOG.write_text(
        json.dumps(
            rows,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def current_acquisition():
    return load_json(
        "work/kick_gaming/acquisition_result.json",
        {},
    )


def load_rejected():
    data = load_json(
        REJECTED,
        {
            "version": 1,
            "clip_ids": [],
        },
    )

    if isinstance(data, dict):
        clip_ids = data.get(
            "clip_ids",
            [],
        )
    elif isinstance(data, list):
        clip_ids = data
    else:
        clip_ids = []

    clean = []

    for clip_id in clip_ids:
        clip_id = str(
            clip_id
        ).strip()

        if (
            clip_id
            and
            clip_id not in clean
        ):
            clean.append(
                clip_id
            )

    return clean


def save_rejected(rejected):
    clean = []

    for clip_id in rejected:
        clip_id = str(
            clip_id
        ).strip()

        if (
            clip_id
            and
            clip_id not in clean
        ):
            clean.append(
                clip_id
            )

    REJECTED.write_text(
        json.dumps(
            {
                "version": 1,
                "clip_ids": clean,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def reject_clip(
    clip_id,
    rejected,
):
    if not clip_id:
        return

    clip_id = str(
        clip_id
    ).strip()

    if (
        clip_id
        and
        clip_id not in rejected
    ):
        rejected.append(
            clip_id
        )

    save_rejected(
        rejected
    )


def main():
    attempts = []

    rejected = load_rejected()

    save_rejected(
        rejected
    )

    print(
        f"Loaded {len(rejected)} permanently "
        f"rejected Shorts clip IDs."
    )

    # ---------------------------------------------------------
    # 1. DISCOVERY
    # ---------------------------------------------------------

    pipeline_started = time.perf_counter()

    if run_timed(
        "discovery",
        "kick_game_discovery.py"
    ) != 0:
        raise RuntimeError(
            "V12.11 discovery failed"
        )

    # ---------------------------------------------------------
    # 2. ONE-PASS METADATA + HLS + DURATION RANKING
    # ---------------------------------------------------------

    if run_timed(
        "direct_api_ranker",
        "candidate_ranker_v12_1.py"
    ) != 0:
        raise RuntimeError(
            "V12.11 one-pass ranking failed"
        )

    ranked = load_json(
        "work/v12_ranked_candidates.json",
        {},
    ).get(
        "candidates",
        [],
    )

    print(
        f"V12.11 one-pass stage supplied "
        f"{len(ranked)} duration-eligible candidates."
    )

    # ---------------------------------------------------------
    # 3. WINDOW-AWARE VISUAL PRESCREEN
    # ---------------------------------------------------------

    if run_timed(
        "window_prescreener",
        "viral_prescreener.py"
    ) != 0:
        raise RuntimeError(
            "V12.11 window prescreen failed"
        )

    prescreened = load_json(
        "work/v12_prescreened_candidates.json",
        {},
    ).get(
        "candidates",
        [],
    )

    if not prescreened:
        raise RuntimeError(
            "V12.11 prescreen shortlist empty"
        )

    actual_attempt_limit = min(
        MAX_EXPENSIVE_ATTEMPTS,
        len(
            prescreened
        ),
    )

    print(
        f"V12.11 prescreen shortlist: "
        f"{len(prescreened)} candidates."
    )

    print(
        f"V12.11 will inspect at most "
        f"{actual_attempt_limit} expensive candidates."
    )

    # ---------------------------------------------------------
    # 4. FULL GATE LOOP
    # ---------------------------------------------------------

    for attempt_no in range(
        1,
        actual_attempt_limit + 1,
    ):
        code = run_timed(
            f"acquisition_attempt_{attempt_no}",
            "kick_gaming_acquisition_v12.py"
        )

        if code != 0:
            attempts.append(
                {
                    "attempt":
                        attempt_no,
                    "result":
                        "failed",
                    "reason":
                        "shortlist_exhausted_or_acquisition_failed",
                }
            )

            save_log(
                attempts
            )

            break

        acq = current_acquisition()

        clip_id = acq.get(
            "clip_id"
        )

        row = {
            "attempt":
                attempt_no,
            "clip_id":
                clip_id,
            "channel":
                acq.get(
                    "channel"
                ),
            "game":
                acq.get(
                    "game"
                ),
            "window_start_original":
                acq.get(
                    "proposed_window_start_original"
                ),
            "window_end_original":
                acq.get(
                    "proposed_window_end_original"
                ),
            "prescreen_rank_score":
                acq.get(
                    "prescreen_rank_score"
                ),
            "prescreen_predicted_score":
                acq.get(
                    "prescreen_predicted_score"
                ),
            "prescreen_probability_72_plus":
                acq.get(
                    "prescreen_probability_72_plus"
                ),
            "prescreen_hook":
                acq.get(
                    "prescreen_hook"
                ),
            "prescreen_story_sustain":
                acq.get(
                    "prescreen_story_sustain"
                ),
            "prescreen_payoff":
                acq.get(
                    "prescreen_payoff"
                ),
        }

        print(
            "\n"
            f"V12.11 full-gate attempt "
            f"{attempt_no}/"
            f"{actual_attempt_limit}: "
            f"{clip_id} | "
            f"window="
            f"{row.get('window_start_original')}-"
            f"{row.get('window_end_original')} | "
            f"rank="
            f"{row.get('prescreen_rank_score')}"
        )

        # -----------------------------------------------------
        # FINAL 72 VIRAL QUALITY GATE
        # -----------------------------------------------------

        if run_timed(
            f"viral_gate_attempt_{attempt_no}",
            "viral_gate.py"
        ) != 0:
            row.update(
                {
                    "result":
                        "rejected",
                    "reason":
                        "viral_quality_gate",
                }
            )

            attempts.append(
                row
            )

            reject_clip(
                clip_id,
                rejected,
            )

            save_log(
                attempts
            )

            continue

        # -----------------------------------------------------
        # MUSIC GATE
        # -----------------------------------------------------

        if run_timed(
            f"music_gate_attempt_{attempt_no}",
            "music_gate.py"
        ) != 0:
            row.update(
                {
                    "result":
                        "rejected",
                    "reason":
                        "commercial_music_gate",
                }
            )

            attempts.append(
                row
            )

            reject_clip(
                clip_id,
                rejected,
            )

            save_log(
                attempts
            )

            continue

        # -----------------------------------------------------
        # PRODUCTION
        # -----------------------------------------------------

        if run_timed(
            f"production_attempt_{attempt_no}",
            "production_test.py"
        ) != 0:
            row.update(
                {
                    "result":
                        "failed",
                    "reason":
                        "production",
                }
            )

            attempts.append(
                row
            )

            save_log(
                attempts
            )

            raise RuntimeError(
                "V12.11 Short production failed"
            )

        # -----------------------------------------------------
        # FINAL CONTENT GATE
        # -----------------------------------------------------

        if run_timed(
            f"final_content_gate_attempt_{attempt_no}",
            "final_content_gate.py"
        ) != 0:
            row.update(
                {
                    "result":
                        "rejected",
                    "reason":
                        "final_content_gate",
                }
            )

            attempts.append(
                row
            )

            reject_clip(
                clip_id,
                rejected,
            )

            save_log(
                attempts
            )

            continue

        row.update(
            {
                "result":
                    "accepted",
                "reason":
                    "all_gates_passed",
            }
        )

        attempts.append(
            row
        )

        save_log(
            attempts
        )

        save_rejected(
            rejected
        )

        print(
            f"V12.11 SUCCESS on attempt "
            f"{attempt_no}: "
            f"{clip_id}"
        )

        print(
            f"V12.11 TOTAL PIPELINE TIME: "
            f"{time.perf_counter() - pipeline_started:.1f}s"
        )

        return

    save_log(
        attempts
    )

    save_rejected(
        rejected
    )

    print(
        f"V12.11 TOTAL PIPELINE TIME: "
        f"{time.perf_counter() - pipeline_started:.1f}s"
    )

    raise RuntimeError(
        f"V12.11 found no publishable Short "
        f"after {len(attempts)} attempted "
        f"candidate(s)."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.11 PIPELINE FAILED:",
            exc,
        )
        sys.exit(1)
