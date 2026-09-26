import json
import subprocess
import sys
from pathlib import Path


LOG = Path("work/v12_attempt_log.json")
REJECTED = Path("shorts_rejected_history.json")

MAX_EXPENSIVE_ATTEMPTS = 15


def run(script):
    process = subprocess.run(
        [
            sys.executable,
            script,
        ]
    )

    return process.returncode


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

    if isinstance(data, list):
        clip_ids = data

    elif isinstance(data, dict):
        clip_ids = data.get(
            "clip_ids",
            [],
        )

    else:
        clip_ids = []

    clean = []

    for clip_id in clip_ids:
        clip_id = str(
            clip_id
        ).strip()

        if (
            clip_id
            and clip_id not in clean
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
            and clip_id not in clean
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

    if not clip_id:
        return

    if clip_id not in rejected:
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
    # 1. DISCOVER 100 FRESH CANDIDATES
    # ---------------------------------------------------------

    if run(
        "kick_game_discovery.py"
    ) != 0:
        raise RuntimeError(
            "game discovery failed"
        )

    # ---------------------------------------------------------
    # 2. CHEAP METADATA ORDERING
    # ---------------------------------------------------------

    if run(
        "candidate_ranker_v12_1.py"
    ) != 0:
        raise RuntimeError(
            "V12.5 metadata ranking failed"
        )

    metadata_ranked = load_json(
        "work/v12_ranked_candidates.json",
        {},
    ).get(
        "candidates",
        [],
    )

    print(
        f"V12.5 metadata stage supplied "
        f"{len(metadata_ranked)} candidates "
        f"to visual prescreen."
    )

    # ---------------------------------------------------------
    # 3. FAST VISUAL PRESCREEN (TOP 40 ONLY)
    # ---------------------------------------------------------

    if run(
        "viral_prescreener.py"
    ) != 0:
        raise RuntimeError(
            "V12.5 visual prescreen failed"
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
            "visual prescreen shortlist empty"
        )

    print(
        f"V12.5 prescreen shortlist contains "
        f"{len(prescreened)} candidates."
    )

    print(
        f"V12.5 may inspect up to "
        f"{MAX_EXPENSIVE_ATTEMPTS} full-gate candidates."
    )

    # ---------------------------------------------------------
    # 4. FULL EXPENSIVE GATE LOOP
    # ---------------------------------------------------------

    for attempt_no in range(
        1,
        MAX_EXPENSIVE_ATTEMPTS + 1,
    ):
        code = run(
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
                        "prescreened_batch_exhausted_or_acquisition_failed",
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
            "metadata_score":
                acq.get(
                    "v12_metadata_score"
                ),
            "prescreen_predicted_score":
                acq.get(
                    "prescreen_predicted_score"
                ),
            "prescreen_probability_72_plus":
                acq.get(
                    "prescreen_probability_72_plus"
                ),
        }

        print(
            "\n"
            f"V12.5 full-gate attempt "
            f"{attempt_no}/"
            f"{MAX_EXPENSIVE_ATTEMPTS}: "
            f"{clip_id} | "
            f"pred="
            f"{row.get('prescreen_predicted_score')} | "
            f"P72="
            f"{row.get('prescreen_probability_72_plus')}"
        )

        # -----------------------------------------------------
        # FULL VIRAL QUALITY GATE
        # -----------------------------------------------------

        if run(
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
        # COMMERCIAL MUSIC GATE
        # -----------------------------------------------------

        if run(
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

        if run(
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
                "Short production failed"
            )

        # -----------------------------------------------------
        # FINAL CONTENT / GAMBLING GATE
        # -----------------------------------------------------

        if run(
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

        # -----------------------------------------------------
        # SUCCESS
        # -----------------------------------------------------

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
            f"V12.5 SUCCESS on full-gate "
            f"attempt {attempt_no}: "
            f"{clip_id}"
        )

        return

    save_log(
        attempts
    )

    save_rejected(
        rejected
    )

    raise RuntimeError(
        f"V12.5 found no publishable Short "
        f"after {MAX_EXPENSIVE_ATTEMPTS} "
        f"full-gate attempts."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.5 PIPELINE FAILED:",
            exc,
        )
        sys.exit(1)
