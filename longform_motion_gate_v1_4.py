import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# ViralSpawnTV Long-Form V1.4 Motion / Activity Gate
#
# V1.3 only used FFmpeg freezedetect. That can miss "dead" gameplay where
# HUDs, particles, compression noise, etc. change while the actual scene
# appears frozen to a viewer.
#
# V1.4 combines:
#   1. FFmpeg freeze detection
#   2. Meaningful frame-change/activity detection
#
# A video must pass BOTH.
# ---------------------------------------------------------------------------


def probe_duration(path):
    return float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            text=True,
        ).strip()
    )


def freeze_seconds(path, minimum_freeze=0.65):
    """
    Traditional FFmpeg freeze detection.

    This remains useful for genuinely duplicated/static frames, but it is no
    longer our only test.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i", str(path),
        "-vf", f"freezedetect=n=0.003:d={minimum_freeze}",
        "-an",
        "-f", "null",
        "-"
    ]

    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    text = (p.stderr or "") + (p.stdout or "")

    vals = re.findall(
        r"freeze_duration:\s*([0-9.]+)",
        text
    )

    freezes = [float(x) for x in vals]

    return sum(freezes), freezes


def extract_activity_scores(
    path,
    sample_fps=4,
    width=320
):
    """
    Uses FFmpeg's signalstats filter to measure frame-to-frame difference.

    Workflow:
      - sample several frames per second
      - downscale for speed
      - convert to grayscale
      - blend each frame with the previous frame using difference mode
      - signalstats measures average brightness of the difference image

    YAVG therefore acts as an inexpensive visual-change score.

    High score = meaningful image change
    Very low score = visually static / dead
    """

    vf = (
        f"fps={sample_fps},"
        f"scale={width}:-2,"
        "format=gray,"
        "tblend=all_mode=difference,"
        "signalstats,"
        "metadata=print"
    )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-i", str(path),
        "-vf", vf,
        "-an",
        "-f", "null",
        "-"
    ]

    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    text = (p.stderr or "") + (p.stdout or "")

    scores = []

    # signalstats metadata output contains:
    #
    # lavfi.signalstats.YAVG=...
    #
    # Because our difference image is grayscale, YAVG gives us a simple
    # measure of how much the picture changed since the preceding sample.

    for match in re.finditer(
        r"lavfi\.signalstats\.YAVG=([0-9.]+)",
        text
    ):
        try:
            scores.append(float(match.group(1)))
        except ValueError:
            pass

    return scores


def activity_analysis(
    path,
    sample_fps=4,
    dead_threshold=1.15,
    severe_dead_threshold=0.55,
    max_dead_run_seconds=1.25,
    max_severe_run_seconds=0.85,
    max_dead_ratio=0.30
):
    """
    Detects viewer-perceived dead footage.

    dead_threshold:
        Difference score below which a frame interval is considered weak /
        visually inactive.

    severe_dead_threshold:
        Extremely little visual change.

    max_dead_run_seconds:
        Maximum continuous low-activity period allowed.

    max_severe_run_seconds:
        Maximum continuous near-static period allowed.

    max_dead_ratio:
        Maximum percentage of sampled frames that may be classified as dead.

    These values intentionally lean stricter than V1.3 because ViralSpawnTV
    gaming compilations should remain visually active.
    """

    scores = extract_activity_scores(
        path,
        sample_fps=sample_fps
    )

    if not scores:
        return False, {
            "error": "no_activity_samples",
            "activity_samples": 0
        }

    seconds_per_sample = 1.0 / sample_fps

    dead_count = 0
    severe_count = 0

    current_dead_run = 0
    current_severe_run = 0

    longest_dead_run = 0
    longest_severe_run = 0

    for score in scores:

        # General dead / weak visual activity
        if score < dead_threshold:
            dead_count += 1
            current_dead_run += 1
            longest_dead_run = max(
                longest_dead_run,
                current_dead_run
            )
        else:
            current_dead_run = 0

        # Extremely static
        if score < severe_dead_threshold:
            severe_count += 1
            current_severe_run += 1
            longest_severe_run = max(
                longest_severe_run,
                current_severe_run
            )
        else:
            current_severe_run = 0

    dead_ratio = dead_count / len(scores)

    longest_dead_seconds = (
        longest_dead_run * seconds_per_sample
    )

    longest_severe_seconds = (
        longest_severe_run * seconds_per_sample
    )

    average_score = sum(scores) / len(scores)

    sorted_scores = sorted(scores)

    p10_index = max(
        0,
        min(
            len(sorted_scores) - 1,
            int(len(sorted_scores) * 0.10)
        )
    )

    p10_score = sorted_scores[p10_index]

    ok = (
        longest_dead_seconds <= max_dead_run_seconds
        and
        longest_severe_seconds <= max_severe_run_seconds
        and
        dead_ratio <= max_dead_ratio
    )

    info = {
        "activity_samples": len(scores),

        "average_activity_score": round(
            average_score,
            3
        ),

        "p10_activity_score": round(
            p10_score,
            3
        ),

        "dead_sample_ratio": round(
            dead_ratio,
            4
        ),

        "dead_sample_percent": round(
            dead_ratio * 100,
            2
        ),

        "longest_dead_run_seconds": round(
            longest_dead_seconds,
            3
        ),

        "longest_severe_dead_run_seconds": round(
            longest_severe_seconds,
            3
        ),

        "dead_threshold": dead_threshold,

        "severe_dead_threshold":
            severe_dead_threshold,

        "activity_gate_passed": ok
    }

    return ok, info


def validate_motion(
    path,
    max_single_freeze=1.0,
    max_total_freeze=2.5,
    sample_fps=4,
    dead_threshold=1.15,
    severe_dead_threshold=0.55,
    max_dead_run_seconds=1.25,
    max_severe_run_seconds=0.85,
    max_dead_ratio=0.30
):
    """
    Full V1.4 validation.

    PASS requires:

        FFmpeg freeze gate
                AND
        visual activity gate
    """

    path = Path(path)

    if not path.exists():
        return False, {
            "error": "file_not_found",
            "path": str(path)
        }

    try:
        duration = probe_duration(path)
    except Exception as e:
        return False, {
            "error": "duration_probe_failed",
            "detail": str(e)
        }

    # ---------------------------------------------------------
    # Freeze gate
    # ---------------------------------------------------------

    total_freeze, freezes = freeze_seconds(
        path
    )

    worst_freeze = max(
        freezes,
        default=0.0
    )

    freeze_ok = (
        worst_freeze <= max_single_freeze
        and
        total_freeze <= max_total_freeze
    )

    # ---------------------------------------------------------
    # Activity gate
    # ---------------------------------------------------------

    activity_ok, activity = activity_analysis(
        path,
        sample_fps=sample_fps,
        dead_threshold=dead_threshold,
        severe_dead_threshold=severe_dead_threshold,
        max_dead_run_seconds=max_dead_run_seconds,
        max_severe_run_seconds=max_severe_run_seconds,
        max_dead_ratio=max_dead_ratio
    )

    # ---------------------------------------------------------
    # Final decision
    # ---------------------------------------------------------

    ok = freeze_ok and activity_ok

    info = {
        "version": "1.4",

        "duration_seconds": round(
            duration,
            3
        ),

        "passed": ok,

        "freeze_gate_passed":
            freeze_ok,

        "activity_gate_passed":
            activity_ok,

        "total_freeze_seconds": round(
            total_freeze,
            3
        ),

        "worst_freeze_seconds": round(
            worst_freeze,
            3
        ),

        "freeze_events":
            len(freezes),

        "activity":
            activity
    }

    return ok, info


def print_result(path):
    ok, info = validate_motion(path)

    print(
        json.dumps(
            info,
            indent=2
        )
    )

    if ok:
        print(
            "\nV1.4 MOTION/ACTIVITY PASS"
        )
    else:
        print(
            "\nV1.4 MOTION/ACTIVITY REJECT"
        )

    return ok


if __name__ == "__main__":

    if len(sys.argv) != 2:
        print(
            "usage: "
            "python longform_motion_gate_v1_4.py VIDEO"
        )
        sys.exit(2)

    video = Path(sys.argv[1])

    try:
        passed = print_result(video)
        sys.exit(
            0 if passed else 23
        )

    except Exception as e:
        print(
            json.dumps(
                {
                    "version": "1.4",
                    "passed": False,
                    "error": str(e)
                },
                indent=2
            )
        )

        sys.exit(23)
