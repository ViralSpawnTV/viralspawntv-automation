import json
import subprocess
import sys
from pathlib import Path


LOG = Path("work/v12_attempt_log.json")

# Permanent cross-run rejection history.
#
# This lives at repository root rather than inside work/ so the
# GitHub Actions workflow can persist it between scheduled runs.
REJECTED = Path("shorts_rejected_history.json")

# Give V12.1 a deeper search budget while keeping the existing
# viral-quality threshold unchanged.
MAX_EXPENSIVE_ATTEMPTS = 15


def run(script, ok=(0,)):
    p = subprocess.run(
        [
            sys.executable,
            script,
        ]
    )

    return p.returncode


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
    """
    Load permanent rejected Shorts clip IDs.

    Supports both:
    - legacy plain-list format
    - dictionary format with clip_ids
    """

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
    """
    Save permanent rejected clip IDs.

    The GitHub Actions workflow will persist this root-level
    file back to the repository after the pipeline finishes,
    including when no Short is ultimately produced.
    """

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
    """
    Permanently reject a source clip after a content/quality gate
    determines that it should not be used for a ViralSpawnTV Short.
    """

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

    # ---------------------------------------------------------
    # LOAD PERMANENT REJECTION HISTORY
    # ---------------------------------------------------------

    rejected = load_rejected()

    # Always ensure the file exists so the workflow can persist it.
    save_rejected(
        rejected
    )

    print(
        f"Loaded {len(rejected)} permanently "
        f"rejected Shorts clip IDs."
    )

    # ---------------------------------------------------------
    # DISCOVERY + RANKING
    # ---------------------------------------------------------
    #
    # Discovery and ranking happen once per scheduled run.
    #
    # The acquisition script reads the rejection history and
    # advances through the already-ranked candidate batch.
    # ---------------------------------------------------------

    if run(
        "kick_game_discovery.py"
    ) != 0:
        raise RuntimeError(
            "game discovery failed"
        )

    if run(
        "candidate_ranker_v12_1.py"
    ) != 0:
        raise RuntimeError(
            "V12.1 candidate ranking failed"
        )

    ranked = load_json(
        "work/v12_ranked_candidates.json",
        {},
    ).get(
        "candidates",
        [],
    )

    if not ranked:
        raise RuntimeError(
            "ranked batch empty"
        )

    print(
        f"V12.1 ranked batch contains "
        f"{len(ranked)} candidates."
    )

    print(
        f"V12.1 may inspect up to "
        f"{MAX_EXPENSIVE_ATTEMPTS} expensive candidates."
    )

    # ---------------------------------------------------------
    # EXPENSIVE ATTEMPT LOOP
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
                    "attempt": attempt_no,
                    "result": "failed",
                    "reason":
                        "ranked_batch_exhausted_or_acquisition_failed",
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
        }

        print(
            "\n"
            f"V12.1 expensive attempt "
            f"{attempt_no}/"
            f"{MAX_EXPENSIVE_ATTEMPTS}: "
            f"{clip_id}"
        )

        # -----------------------------------------------------
        # VIRAL QUALITY GATE
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
        # FROZEN V5 SHORTS PRODUCTION
        # -----------------------------------------------------
        #
        # IMPORTANT:
        # A technical production failure does NOT permanently
        # reject the source clip. The source may still be good.
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
        # FINAL SELECTED-SEGMENT CONTENT / GAMBLING GATE
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

        # Persist the current rejection file even though this
        # particular source passed.
        save_rejected(
            rejected
        )

        print(
            f"V12.1 SUCCESS on expensive "
            f"attempt {attempt_no}: "
            f"{clip_id}"
        )

        return

    # ---------------------------------------------------------
    # NO PUBLISHABLE SHORT FOUND
    # ---------------------------------------------------------

    save_log(
        attempts
    )

    # Critical:
    # Save all rejected IDs even though the overall workflow
    # will exit non-zero. The workflow persistence step must use
    # `if: always()` so these rejections survive failed runs.
    save_rejected(
        rejected
    )

    raise RuntimeError(
        f"V12.1 found no publishable Short "
        f"after {MAX_EXPENSIVE_ATTEMPTS} "
        f"expensive attempts."
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as e:

        print(
            "V12.1 PIPELINE FAILED:",
            e,
        )

        sys.exit(1)
