import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from firefight_cache import defer_failed_edit, exclude


LOG = Path("work/v12_attempt_log.json")
REJECTED = Path("shorts_rejected_history.json")

MAX_SOURCE_CANDIDATES = 2

# Normal source quality remains preferred.
NORMAL_SOURCE_SCORE = 65
NORMAL_SOURCE_PAYOFF = 60

# Reliability fallback: when no candidate clears the normal source gate,
# the best borderline source can still be rendered. The strict final content
# and music gates remain active.
BORDERLINE_SOURCE_SCORE = 40
BORDERLINE_SOURCE_PAYOFF = 40

BEST_DIR = Path("work/reliability_best")
BEST_VIDEO = BEST_DIR / "ViralSpawnTV_Short_V4.mp4"
BEST_METADATA = BEST_DIR / "ViralSpawnTV_V4_metadata.json"
BEST_GATE = BEST_DIR / "finished_viral_gate.json"


def run(
    script,
    extra_env=None,
):
    env = os.environ.copy()
    env["VIRALSPAWN_AI_STAGE"] = script

    if extra_env:
        env.update(
            {
                str(key):
                    str(value)
                for key, value in extra_env.items()
            }
        )

    process = subprocess.run(
        [
            sys.executable,
            script,
        ],
        env=env,
    )

    return process.returncode


def run_timed(
    label,
    script,
    extra_env=None,
):
    started = time.perf_counter()

    code = run(
        script,
        extra_env=extra_env,
    )

    print(
        f"V12.14.6 TIMING | "
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
    ledger = os.environ.get("VIRALSPAWN_AI_LEDGER")
    if ledger:
        data = json.loads(Path(ledger).read_text())
        calls = data.get("calls", [])
        last = calls[-1] if calls else {}
        if data.get("exhausted") or (last.get("status") == "failed"):
            print("API failure/budget limit: deferring clip; no permanent rejection.")
            return
    clip_id = str(
        clip_id or ""
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


def relative_action_hint(candidate):
    """Convert original-source motion timestamps to the acquired clip's timeline."""
    hint = candidate.get("local_window_start")
    if hint is None:
        return ""
    acquisition = load_json("work/kick_gaming/acquisition_result.json", {})
    if str(acquisition.get("clip_id", "")) != str(candidate.get("clip_id", "")):
        return ""
    try:
        value = float(hint)
        if acquisition.get("local_source_is_selected_window") is True:
            value -= float(acquisition.get("proposed_window_start_original") or 0)
        seconds = float(acquisition.get("source_duration_seconds") or 0)
        return str(max(0.0, min(value, max(0.0, seconds - 19))))
    except (ValueError, TypeError):
        return ""


def force_acquire(
    clip_id,
    label,
):
    return run_timed(
        label,
        "kick_gaming_acquisition_v12.py",
        extra_env={
            "V12_FORCE_CLIP_ID":
                clip_id,
        },
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
        f"V12.14.6 reliability backup saved | "
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


def source_rank(
    row,
):
    source = int(
        row.get(
            "source_score",
            0,
        )
        or
        0
    )

    payoff = int(
        row.get(
            "payoff",
            0,
        )
        or
        0
    )

    story = int(
        row.get(
            "story_sustain",
            0,
        )
        or
        0
    )

    editability = int(
        row.get(
            "editability",
            0,
        )
        or
        0
    )

    return round(
        source * 0.40
        +
        payoff * 0.35
        +
        story * 0.15
        +
        editability * 0.10,
        2,
    )


def main():
    attempts = []

    if BEST_DIR.exists():
        shutil.rmtree(
            BEST_DIR,
            ignore_errors=True,
        )

    rejected = load_rejected()

    pipeline_started = time.perf_counter()

    print(
        "================================================"
    )
    print(
        "ViralSpawnTV V12.14.6 "
        "Reliability-First One-Short Pipeline"
    )
    print(
        "================================================"
    )

    # ---------------------------------------------------------
    # 1. DISCOVERY / RANK / PRESCREEN
    # ---------------------------------------------------------

    if run_timed(
        "discovery",
        "clip_library_feed.py",
    ) != 0:
        raise RuntimeError(
            "V12.14.6 discovery failed"
        )

    if run_timed(
        "direct_api_ranker",
        "candidate_ranker_v12_1.py",
    ) != 0:
        raise RuntimeError(
            "V12.14.6 ranking failed"
        )

    # ---------------------------------------------------------
    # ACTIVE FIREFIGHT / COMBAT FILTER
    # ---------------------------------------------------------

    if run_timed(
        "active_firefight_prescreener",
        "active_firefight_prescreener.py",
    ) != 0:
        raise RuntimeError(
            "V12.14.6 active-firefight scan failed"
        )

    if run_timed(
        "payoff_first_prescreener",
        "viral_prescreener.py",
    ) != 0:
        raise RuntimeError(
            "V12.14.6 prescreen failed"
        )

    prescreened = load_json(
        "work/v12_prescreened_candidates.json",
        {},
    ).get(
        "candidates",
        [],
    )

    prescreened = [
        row
        for row in prescreened
        if str(
            row.get(
                "clip_id",
                "",
            )
        ).strip()
    ][
        :MAX_SOURCE_CANDIDATES
    ]

    if not prescreened:
        raise RuntimeError(
            "V12.14.6 prescreen shortlist empty"
        )

    print(
        f"V12.14.6 guaranteed shortlist: "
        f"{len(prescreened)} candidate(s)."
    )

    # ---------------------------------------------------------
    # 2. SOURCE-GATE ALL SHORTLIST CANDIDATES FIRST.
    #    This is cheap compared with rendering.
    # ---------------------------------------------------------

    source_candidates = []

    for index, candidate in enumerate(
        prescreened,
        1,
    ):
        clip_id = str(
            candidate.get(
                "clip_id",
                "",
            )
        ).strip()

        row = {
            "source_attempt":
                index,
            "clip_id":
                clip_id,
            "game":
                candidate.get(
                    "game"
                ),
            "channel":
                candidate.get(
                    "channel"
                ),
            "prescreen_rank_score":
                candidate.get(
                    "prescreen_rank_score"
                ),
            "prescreen_payoff":
                candidate.get(
                    "prescreen_payoff"
                ),
            "prescreen_story_sustain":
                candidate.get(
                    "prescreen_story_sustain"
                ),
            "prescreen_reliability_backfill":
                bool(
                    candidate.get(
                        "prescreen_reliability_backfill",
                        False,
                    )
                ),
        }

        if force_acquire(
            clip_id,
            f"source_acquisition_{index}",
        ) != 0:
            row[
                "source_result"
            ] = "acquisition_failed"

            attempts.append(
                row
            )
            continue

        # Prevent a failed API request from reusing a previous source's verdict.
        Path("work/source_quality_gate/source_quality_gate_result.json").unlink(missing_ok=True)

        source_code = run_timed(
            f"source_quality_gate_{index}",
            "viral_gate.py",
        )

        source_result = load_json(
            "work/source_quality_gate/source_quality_gate_result.json",
            {},
        )

        # A completed negative source review is also an automatic spending skip.
        # Extraction/API/budget failures must stay retryable.
        ledger = load_json(os.environ.get("VIRALSPAWN_AI_LEDGER", "work/ai_budget.json"), {})
        calls = ledger.get("calls", [])
        latest_call = calls[-1] if calls else {}
        ai_error = bool(ledger.get("exhausted")) or latest_call.get("status") == "failed"
        scores_valid = all(type(source_result.get(key)) in (int, float)
                           and 0 <= source_result[key] <= 100
                           for key in ("source_score", "payoff"))
        if (source_code != 0 and not ai_error and scores_valid
                and isinstance(source_result.get("reason"), str) and source_result["reason"].strip()):
            acquisition = load_json("work/kick_gaming/acquisition_result.json", {})
            exclude(candidate, "completed_source_quality_rejection", source_result["reason"],
                    [{"stage": "viral_gate.py",
                      "start_original": acquisition.get("proposed_window_start_original"),
                      "end_original": acquisition.get("proposed_window_end_original"),
                      "source_score": source_result["source_score"], "payoff": source_result["payoff"]}])
            row["permanent_spending_exclusion"] = True

        row.update(
            {
                "source_gate_passed":
                    source_code == 0,
                "source_score":
                    source_result.get(
                        "source_score"
                    ),
                "source_payoff":
                    source_result.get(
                        "payoff"
                    ),
                "source_story":
                    source_result.get(
                        "story_sustain"
                    ),
                "source_editability":
                    source_result.get(
                        "editability"
                    ),
                "source_reason":
                    source_result.get(
                        "reason"
                    ),
            }
        )

        row[
            "source_rank"
        ] = source_rank(
            source_result
        )

        source_candidates.append(
            {
                "candidate":
                    candidate,
                "source_result":
                    source_result,
                "source_code":
                    source_code,
                "source_rank":
                    row[
                        "source_rank"
                    ],
            }
        )

        attempts.append(
            row
        )

    save_log(
        attempts
    )

    if not source_candidates:
        raise RuntimeError(
            "V12.14.6 could not source-gate any candidate."
        )

    # A failed source-quality gate is a stop, never a render fallback.
    render_order = [item for item in source_candidates
                    if item["source_code"] == 0
                    and float(item["source_result"].get("source_score", 0) or 0) >= NORMAL_SOURCE_SCORE
                    and float(item["source_result"].get("payoff", 0) or 0) >= NORMAL_SOURCE_PAYOFF]
    render_order.sort(key=lambda item: item["source_rank"], reverse=True)
    if not render_order:
        raise RuntimeError("No source passed the source-quality gate; production skipped.")
    print(f"V12.14.6 SOURCE SELECTION: {len(render_order)} passed source(s); rejected sources skipped.")

    # ---------------------------------------------------------
    # 3. RENDER BEST SOURCES UNTIL WE HAVE A DECENT/FINAL VIDEO.
    # ---------------------------------------------------------

    rendered_count = 0

    for render_index, item in enumerate(
        render_order,
        1,
    ):
        candidate = item[
            "candidate"
        ]

        clip_id = str(
            candidate.get(
                "clip_id",
                "",
            )
        ).strip()

        print()
        print(
            f"V12.14.6 render candidate "
            f"{render_index}/{len(render_order)}: "
            f"{clip_id} | "
            f"source_rank={item.get('source_rank')}"
        )

        if force_acquire(
            clip_id,
            f"render_acquisition_{render_index}",
        ) != 0:
            reject_clip(
                clip_id,
                rejected,
            )
            continue

        # Only explicit music rejection permanently blacklists a source.
        # Uncertain evidence, extraction/model errors and budget/API failures
        # still block this candidate, but leave it eligible for later review.
        music_code = run_timed(
            f"music_gate_{render_index}",
            "music_gate.py",
        )
        if music_code != 0:
            if music_code == 20:
                reject_clip(clip_id, rejected)
            else:
                print("Music screen uncertain/failed: deferring clip without permanent rejection.")
            continue

        # Never attribute a previous candidate's files to this attempt.
        for diagnostic in (
            "work/production/action_segment_gate/result.json",
            "work/production/v4_edit_plan.json",
            "work/production/pre_render_plan_gate.json",
        ):
            Path(diagnostic).unlink(missing_ok=True)

        production_code = run_timed(
            f"production_{render_index}",
            "production_test.py",
            extra_env={"FIREFIGHT_WINDOW_START": relative_action_hint(candidate)},
        )

        action_result = load_json("work/production/action_segment_gate/result.json", {})
        production_attempt = {
            "render_attempt": render_index, "clip_id": clip_id,
            "production_exit_code": production_code,
            "action_segment_validation": action_result,
            "edit_plan": load_json("work/production/v4_edit_plan.json", {}),
            "pre_render_validation": load_json("work/production/pre_render_plan_gate.json", {}),
        }
        if production_code != 0:
            acquisition = load_json("work/kick_gaming/acquisition_result.json", {})
            source_offset = acquisition.get("proposed_window_start_original", 0) if acquisition.get("local_source_is_selected_window") else 0
            production_attempt["permanent_spending_exclusion"] = defer_failed_edit(
                candidate, action_result, source_offset=source_offset)
        attempts.append(production_attempt)
        save_log(attempts)

        if production_code != 0:
            print(
                f"V12.14.6 production failed for "
                f"{clip_id}; trying next source."
            )
            if production_attempt.get("permanent_spending_exclusion"):
                print("Valid action rejection: reviewed candidate permanently excluded from automatic spending.")
            else:
                print("Edit/render/API failure: no permanent action exclusion.")
            continue

        # Gambling/content safety remains a hard gate.
        if run_timed(
            f"final_content_gate_{render_index}",
            "final_content_gate.py",
        ) != 0:
            reject_clip(
                clip_id,
                rejected,
            )
            continue

        rendered_count += 1

        finished_code = run_timed(
            f"finished_quality_gate_{render_index}",
            "finished_viral_gate.py",
        )

        finished = load_json(
            "work/production/finished_viral_gate.json",
            {},
        )

        finished[
            "clip_id"
        ] = clip_id

        backup_finished_candidate(
            finished
        )

        attempts.append(
            {
                "render_attempt":
                    render_index,
                "clip_id":
                    clip_id,
                "rendered":
                    True,
                "quality_tier":
                    finished.get(
                        "quality_tier"
                    ),
                "finished_score":
                    finished.get(
                        "score"
                    ),
                "finished_hook":
                    finished.get(
                        "hook"
                    ),
                "finished_payoff":
                    finished.get(
                        "payoff"
                    ),
            }
        )

        save_log(
            attempts
        )

        if finished_code == 0:
            print(
                f"V12.14.6 SUCCESS: "
                f"{clip_id} | "
                f"tier={finished.get('quality_tier')} | "
                f"score={finished.get('score')} | "
                f"hook={finished.get('hook')} | "
                f"payoff={finished.get('payoff')}"
            )

            print(
                f"V12.14.6 TOTAL PIPELINE TIME: "
                f"{time.perf_counter() - pipeline_started:.1f}s"
            )

            return

        # Below decent: keep best backup and try next candidate if available.

    # ---------------------------------------------------------
    # 4. BEST-AVAILABLE GUARANTEE
    # ---------------------------------------------------------

    best = restore_best_finished_candidate()

    if best is not None:
        print(
            "V12.14.6 RELIABILITY SUCCESS: "
            "no rendered source hit the viral/decent target, so the "
            "highest-scoring finished Short was restored as BEST AVAILABLE. | "
            f"score={best.get('score')} | "
            f"hook={best.get('hook')} | "
            f"payoff={best.get('payoff')}"
        )

        print(
            f"V12.14.6 TOTAL PIPELINE TIME: "
            f"{time.perf_counter() - pipeline_started:.1f}s"
        )

        return

    raise RuntimeError(
        "V12.14.6 could not create a finished Short. "
        "All shortlisted sources failed acquisition/music/content/production "
        "before any video could be rendered."
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            "V12.14.6 PIPELINE FAILED:",
            exc,
        )
        sys.exit(
            1
        )
