import json
import subprocess
import sys
import time
from pathlib import Path


LOG = Path("work/v12_attempt_log.json")
REJECTED = Path("shorts_rejected_history.json")

MAX_EXPENSIVE_ATTEMPTS = 4


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

    print(
        f"V12.14.3 TIMING | "
        f"{label}: "
        f"{time.perf_counter() - started:.1f}s"
    )

    return code


def load_json(
    path,
    default,
):
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

    if isinstance(
        data,
        dict,
    ):
        clip_ids = data.get(
            "clip_ids",
            [],
        )

    elif isinstance(
        data,
        list,
    ):
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

    pipeline_started = time.perf_counter()

    print(
        "================================================"
    )
    print(
        "ViralSpawnTV V12.14.3 "
        "Source -> Production -> Finished Gate"
    )
    print(
        "================================================"
    )

    # ---------------------------------------------------------
    # 1. BROAD DISCOVERY
    # ---------------------------------------------------------

    if run_timed(
        "discovery",
        "kick_game_discovery.py",
    ) != 0:
        raise RuntimeError(
            "V12.14.3 discovery failed"
        )

    # ---------------------------------------------------------
    # 2. QUALITY-FIRST API RANK
    # ---------------------------------------------------------

    if run_timed(
        "direct_api_ranker",
        "candidate_ranker_v12_1.py",
    ) != 0:
        raise RuntimeError(
            "V12.14.3 ranking failed"
        )

    # ---------------------------------------------------------
    # 3. PAYOFF-FIRST SOURCE PRESCREEN
    # ---------------------------------------------------------

    if run_timed(
        "payoff_first_prescreener",
        "viral_prescreener.py",
    ) != 0:
        raise RuntimeError(
            "V12.14.3 prescreen failed"
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
            "V12.14.3 prescreen shortlist empty"
        )

    actual_attempt_limit = min(
        MAX_EXPENSIVE_ATTEMPTS,
        len(
            prescreened
        ),
    )

    print(
        f"V12.14.3 source shortlist: "
        f"{len(prescreened)} candidates."
    )

    # ---------------------------------------------------------
    # 4. CANDIDATE LOOP
    # ---------------------------------------------------------

    for attempt_no in range(
        1,
        actual_attempt_limit + 1,
    ):
        code = run_timed(
            f"acquisition_{attempt_no}",
            "kick_gaming_acquisition_v12.py",
        )

        if code != 0:
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
            "prescreen_rank_score":
                acq.get(
                    "prescreen_rank_score"
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

        print()
        print(
            f"V12.14.3 attempt "
            f"{attempt_no}/"
            f"{actual_attempt_limit}: "
            f"{clip_id}"
        )

        # -----------------------------------------------------
        # RAW SOURCE QUALITY GATE
        #
        # NO raw hook requirement here.
        # -----------------------------------------------------

        if run_timed(
            f"source_quality_gate_{attempt_no}",
            "viral_gate.py",
        ) != 0:
            row.update(
                {
                    "result":
                        "rejected",
                    "reason":
                        "source_quality_gate",
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
            f"music_gate_{attempt_no}",
            "music_gate.py",
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
        # PRODUCTION CREATES THE BIG HOOK
        # -----------------------------------------------------

        production_code = run_timed(
            f"production_{attempt_no}",
            "production_test.py",
        )

        if production_code == 24:
            pre_render = load_json(
                "work/production/pre_render_plan_gate.json",
                {},
            )

            row.update(
                {
                    "result":
                        "rejected",
                    "reason":
                        "pre_render_plan_gate",
                    "pre_render_predicted_score":
                        pre_render.get(
                            "predicted_finished_score"
                        ),
                    "pre_render_payoff_coverage":
                        pre_render.get(
                            "payoff_coverage"
                        ),
                    "pre_render_progression":
                        pre_render.get(
                            "progression_clarity"
                        ),
                    "pre_render_claim_support":
                        pre_render.get(
                            "claim_support"
                        ),
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

            print(
                f"V12.14.3 candidate skipped before render: "
                f"{clip_id}"
            )

            continue

        if production_code != 0:
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
                "V12.14.3 production failed"
            )

        # -----------------------------------------------------
        # FINAL CONTENT / GAMBLING SAFETY GATE
        # -----------------------------------------------------

        if run_timed(
            f"final_content_gate_{attempt_no}",
            "final_content_gate.py",
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
        # ACTUAL FINISHED-SHORT VIRAL GATE
        #
        # This is where hook >=65 is enforced.
        # -----------------------------------------------------

        if run_timed(
            f"finished_viral_gate_{attempt_no}",
            "finished_viral_gate.py",
        ) != 0:
            finished_result = load_json(
                "work/production/finished_viral_gate.json",
                {},
            )

            row.update(
                {
                    "result":
                        "rejected",
                    "reason":
                        "finished_viral_gate",
                    "finished_score":
                        finished_result.get(
                            "score"
                        ),
                    "finished_hook":
                        finished_result.get(
                            "hook"
                        ),
                    "finished_payoff":
                        finished_result.get(
                            "payoff"
                        ),
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

        finished_result = load_json(
            "work/production/finished_viral_gate.json",
            {},
        )

        row.update(
            {
                "result":
                    "accepted",
                "reason":
                    "finished_short_passed",
                "finished_score":
                    finished_result.get(
                        "score"
                    ),
                "finished_hook":
                    finished_result.get(
                        "hook"
                    ),
                "finished_payoff":
                    finished_result.get(
                        "payoff"
                    ),
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
            f"V12.14.3 SUCCESS: "
            f"{clip_id} | "
            f"finished score="
            f"{row.get('finished_score')} | "
            f"hook="
            f"{row.get('finished_hook')} | "
            f"payoff="
            f"{row.get('finished_payoff')}"
        )

        print(
            f"V12.14.3 TOTAL PIPELINE TIME: "
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
        f"V12.14.3 TOTAL PIPELINE TIME: "
        f"{time.perf_counter() - pipeline_started:.1f}s"
    )

    raise RuntimeError(
        f"V12.14.3 found no finished Short "
        f"that passed after "
        f"{len(attempts)} attempts."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.14.3 PIPELINE FAILED:",
            exc,
        )
        sys.exit(1)
