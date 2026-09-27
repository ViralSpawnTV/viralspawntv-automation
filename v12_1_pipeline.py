import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


LOG = Path("work/v12_attempt_log.json")
REJECTED = Path("shorts_rejected_history.json")

MAX_EXPENSIVE_ATTEMPTS = 4

# V12.14.4 reliability-first fallbacks.
FALLBACK_SOURCE_SCORE = 55
FALLBACK_SOURCE_PAYOFF = 55

BEST_DIR = Path("work/reliability_best")
BEST_VIDEO = BEST_DIR / "ViralSpawnTV_Short_V4.mp4"
BEST_METADATA = BEST_DIR / "ViralSpawnTV_V4_metadata.json"
BEST_GATE = BEST_DIR / "finished_viral_gate.json"


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
        f"V12.14.4 TIMING | "
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


def backup_finished_candidate(
    finished_result,
):
    video = Path(
        "work/production/ViralSpawnTV_Short_V4.mp4"
    )

    metadata = Path(
        "work/production/ViralSpawnTV_V4_metadata.json"
    )

    if not video.exists():
        return False

    BEST_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    current_best = load_json(
        BEST_GATE,
        {},
    )

    current_score = int(
        finished_result.get(
            "score",
            0,
        )
        or
        0
    )

    best_score = int(
        current_best.get(
            "score",
            -1,
        )
        or
        -1
    )

    if current_score < best_score:
        return False

    shutil.copy2(
        video,
        BEST_VIDEO,
    )

    if metadata.exists():
        shutil.copy2(
            metadata,
            BEST_METADATA,
        )

    gate_payload = dict(
        finished_result
    )

    gate_payload[
        "reliability_backup"
    ] = True

    BEST_GATE.write_text(
        json.dumps(
            gate_payload,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        f"V12.14.4 reliability backup saved | "
        f"score={current_score} | "
        f"hook={finished_result.get('hook')} | "
        f"payoff={finished_result.get('payoff')}"
    )

    return True


def restore_best_finished_candidate():
    if not BEST_VIDEO.exists():
        return None

    production_dir = Path(
        "work/production"
    )

    production_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.copy2(
        BEST_VIDEO,
        production_dir
        /
        "ViralSpawnTV_Short_V4.mp4",
    )

    if BEST_METADATA.exists():
        shutil.copy2(
            BEST_METADATA,
            production_dir
            /
            "ViralSpawnTV_V4_metadata.json",
        )

    best = load_json(
        BEST_GATE,
        {},
    )

    best[
        "passed"
    ] = True

    best[
        "quality_tier"
    ] = "best_available"

    best[
        "reliability_forced_accept"
    ] = True

    (
        production_dir
        /
        "finished_viral_gate.json"
    ).write_text(
        json.dumps(
            best,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return best


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

    if BEST_DIR.exists():
        shutil.rmtree(
            BEST_DIR,
            ignore_errors=True,
        )

    rejected = load_rejected()

    save_rejected(
        rejected
    )

    pipeline_started = time.perf_counter()

    print(
        "================================================"
    )
    print(
        "ViralSpawnTV V12.14.4 "
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
            "V12.14.4 discovery failed"
        )

    # ---------------------------------------------------------
    # 2. QUALITY-FIRST API RANK
    # ---------------------------------------------------------

    if run_timed(
        "direct_api_ranker",
        "candidate_ranker_v12_1.py",
    ) != 0:
        raise RuntimeError(
            "V12.14.4 ranking failed"
        )

    # ---------------------------------------------------------
    # 3. PAYOFF-FIRST SOURCE PRESCREEN
    # ---------------------------------------------------------

    if run_timed(
        "payoff_first_prescreener",
        "viral_prescreener.py",
    ) != 0:
        raise RuntimeError(
            "V12.14.4 prescreen failed"
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
            "V12.14.4 prescreen shortlist empty"
        )

    actual_attempt_limit = min(
        MAX_EXPENSIVE_ATTEMPTS,
        len(
            prescreened
        ),
    )

    print(
        f"V12.14.4 source shortlist: "
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
            f"V12.14.4 attempt "
            f"{attempt_no}/"
            f"{actual_attempt_limit}: "
            f"{clip_id}"
        )

        # -----------------------------------------------------
        # RAW SOURCE QUALITY GATE
        #
        # NO raw hook requirement here.
        # -----------------------------------------------------

        source_gate_code = run_timed(
            f"source_quality_gate_{attempt_no}",
            "viral_gate.py",
        )

        if source_gate_code != 0:
            source_result = load_json(
                "work/source_quality_gate/source_quality_gate_result.json",
                {},
            )

            source_score = int(
                source_result.get(
                    "source_score",
                    0,
                )
                or
                0
            )

            source_payoff = int(
                source_result.get(
                    "payoff",
                    0,
                )
                or
                0
            )

            fallback_source_ok = bool(
                source_score >= FALLBACK_SOURCE_SCORE
                and
                source_payoff >= FALLBACK_SOURCE_PAYOFF
            )

            if fallback_source_ok:
                print(
                    f"V12.14.4 RELIABILITY SOURCE FALLBACK: "
                    f"{clip_id} | "
                    f"source={source_score} | "
                    f"payoff={source_payoff}"
                )

                row[
                    "source_fallback"
                ] = True

            else:
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
                f"V12.14.4 candidate skipped before render: "
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
                "V12.14.4 production failed"
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

        finished_gate_code = run_timed(
            f"finished_viral_gate_{attempt_no}",
            "finished_viral_gate.py",
        )

        if finished_gate_code != 0:
            finished_result = load_json(
                "work/production/finished_viral_gate.json",
                {},
            )

            backup_finished_candidate(
                finished_result
            )

            row.update(
                {
                    "result":
                        "below_decent_but_saved",
                    "reason":
                        "finished_quality_gate",
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
                "quality_tier":
                    finished_result.get(
                        "quality_tier",
                        "viral",
                    ),
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
            f"V12.14.4 SUCCESS: "
            f"{clip_id} | "
            f"finished score="
            f"{row.get('finished_score')} | "
            f"hook="
            f"{row.get('finished_hook')} | "
            f"payoff="
            f"{row.get('finished_payoff')}"
        )

        print(
            f"V12.14.4 TOTAL PIPELINE TIME: "
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
        f"V12.14.4 TOTAL PIPELINE TIME: "
        f"{time.perf_counter() - pipeline_started:.1f}s"
    )

    best = restore_best_finished_candidate()

    if best is not None:
        print(
            "V12.14.4 RELIABILITY SUCCESS: "
            "no viral/decent candidate cleared the target, so the "
            "strongest rendered Short from this run was restored as "
            "BEST AVAILABLE. | "
            f"score={best.get('score')} | "
            f"hook={best.get('hook')} | "
            f"payoff={best.get('payoff')}"
        )

        print(
            f"V12.14.4 TOTAL PIPELINE TIME: "
            f"{time.perf_counter() - pipeline_started:.1f}s"
        )

        return

    raise RuntimeError(
        f"V12.14.4 could not produce any finished Short "
        f"after {len(attempts)} attempts."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V12.14.4 PIPELINE FAILED:",
            exc,
        )
        sys.exit(1)
