import json
import subprocess
import sys
from pathlib import Path


VERSION = "1.5"

# Minimum source-quality requirements.
MIN_LANDSCAPE_WIDTH = 960
MIN_LANDSCAPE_HEIGHT = 540

MIN_PORTRAIT_WIDTH = 540
MIN_PORTRAIT_HEIGHT = 960

MIN_SHORT_SIDE = 540
MIN_FPS = 24.0

# Extremely low bitrate is often a sign of heavily compressed/blurry source.
MIN_VIDEO_BITRATE = 700_000

# Audio verification.
SILENCE_DB = -50
MAX_SILENCE_SECONDS = 2.5


def run_command(cmd):
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    return result.returncode, result.stdout, result.stderr


def ffprobe_json(video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(video_path),
    ]

    code, stdout, stderr = run_command(cmd)

    if code != 0:
        raise RuntimeError(
            f"ffprobe failed for {video_path}: {stderr.strip()}"
        )

    return json.loads(stdout)


def parse_fraction(value):
    if not value:
        return 0.0

    try:
        if "/" in str(value):
            numerator, denominator = str(value).split("/", 1)

            numerator = float(numerator)
            denominator = float(denominator)

            if denominator == 0:
                return 0.0

            return numerator / denominator

        return float(value)

    except Exception:
        return 0.0


def get_video_info(video_path):
    data = ffprobe_json(video_path)

    streams = data.get("streams", [])
    format_info = data.get("format", {})

    video_stream = None
    audio_stream = None

    for stream in streams:
        if stream.get("codec_type") == "video" and video_stream is None:
            video_stream = stream

        if stream.get("codec_type") == "audio" and audio_stream is None:
            audio_stream = stream

    if video_stream is None:
        raise RuntimeError("No video stream found.")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)

    fps = parse_fraction(
        video_stream.get("avg_frame_rate")
        or video_stream.get("r_frame_rate")
    )

    duration = float(
        video_stream.get("duration")
        or format_info.get("duration")
        or 0
    )

    bitrate = int(
        video_stream.get("bit_rate")
        or format_info.get("bit_rate")
        or 0
    )

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration": duration,
        "bitrate": bitrate,
        "has_audio": audio_stream is not None,
    }


def resolution_passes(info):
    width = info["width"]
    height = info["height"]

    if width <= 0 or height <= 0:
        return False, "invalid resolution"

    short_side = min(width, height)

    if short_side < MIN_SHORT_SIDE:
        return (
            False,
            f"short side {short_side}px is below {MIN_SHORT_SIDE}px",
        )

    if width >= height:
        if (
            width < MIN_LANDSCAPE_WIDTH
            or height < MIN_LANDSCAPE_HEIGHT
        ):
            return (
                False,
                f"landscape resolution {width}x{height} is too low",
            )

    else:
        if (
            width < MIN_PORTRAIT_WIDTH
            or height < MIN_PORTRAIT_HEIGHT
        ):
            return (
                False,
                f"portrait resolution {width}x{height} is too low",
            )

    return True, "resolution passed"


def fps_passes(info):
    fps = info["fps"]

    if fps < MIN_FPS:
        return False, f"{fps:.2f} FPS is below {MIN_FPS:.2f}"

    return True, "FPS passed"


def bitrate_passes(info):
    bitrate = info["bitrate"]

    # Some containers do not report a reliable bitrate.
    # In that case we do not reject solely for missing metadata.
    if bitrate <= 0:
        return True, "bitrate unavailable; not used as sole rejection"

    if bitrate < MIN_VIDEO_BITRATE:
        return (
            False,
            f"video bitrate {bitrate} is below {MIN_VIDEO_BITRATE}",
        )

    return True, "bitrate passed"


def detect_long_silence(video_path):
    """
    Detects prolonged silence.

    This is mainly intended for FINAL produced video validation so that
    the technical bug where gameplay audio disappears after narration
    cannot silently reach YouTube.
    """

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(video_path),
        "-af",
        f"silencedetect=noise={SILENCE_DB}dB:d={MAX_SILENCE_SECONDS}",
        "-f",
        "null",
        "-",
    ]

    code, stdout, stderr = run_command(cmd)

    # ffmpeg sends filter metadata to stderr.
    text = stderr or ""

    silence_starts = []
    silence_ends = []

    for line in text.splitlines():
        line = line.strip()

        if "silence_start:" in line:
            try:
                value = line.split("silence_start:", 1)[1].strip()
                silence_starts.append(float(value))
            except Exception:
                pass

        if "silence_end:" in line and "silence_duration:" in line:
            try:
                end_part = line.split("silence_end:", 1)[1]
                end_value = end_part.split("|", 1)[0].strip()

                duration_part = line.split("silence_duration:", 1)[1]
                duration_value = duration_part.strip()

                silence_ends.append(
                    {
                        "end": float(end_value),
                        "duration": float(duration_value),
                    }
                )
            except Exception:
                pass

    longest = 0.0

    for item in silence_ends:
        longest = max(longest, item["duration"])

    return {
        "silence_events": silence_ends,
        "open_silence_starts": silence_starts,
        "longest_silence": longest,
        "has_long_silence": bool(
            silence_ends or silence_starts
        ),
    }


def validate_source(video_path):
    video_path = Path(video_path)

    result = {
        "version": VERSION,
        "mode": "source",
        "file": str(video_path),
        "passed": False,
        "reasons": [],
    }

    if not video_path.exists():
        result["reasons"].append("file does not exist")
        return result

    if video_path.stat().st_size <= 0:
        result["reasons"].append("file is empty")
        return result

    try:
        info = get_video_info(video_path)
        result["video_info"] = info

        resolution_ok, resolution_reason = resolution_passes(info)
        fps_ok, fps_reason = fps_passes(info)
        bitrate_ok, bitrate_reason = bitrate_passes(info)

        result["checks"] = {
            "resolution": {
                "passed": resolution_ok,
                "reason": resolution_reason,
            },
            "fps": {
                "passed": fps_ok,
                "reason": fps_reason,
            },
            "bitrate": {
                "passed": bitrate_ok,
                "reason": bitrate_reason,
            },
        }

        if not resolution_ok:
            result["reasons"].append(resolution_reason)

        if not fps_ok:
            result["reasons"].append(fps_reason)

        if not bitrate_ok:
            result["reasons"].append(bitrate_reason)

        result["passed"] = (
            resolution_ok
            and fps_ok
            and bitrate_ok
        )

    except Exception as exc:
        result["reasons"].append(str(exc))

    return result


def validate_final(video_path):
    """
    Final-output validation.

    Source clips are allowed to contain naturally quiet gameplay.
    The final episode receives the additional audio continuity check.
    """

    video_path = Path(video_path)

    result = validate_source(video_path)

    result["mode"] = "final"

    if not result["passed"]:
        return result

    info = result.get("video_info", {})

    if not info.get("has_audio"):
        result["passed"] = False
        result["reasons"].append(
            "final video has no audio stream"
        )
        return result

    try:
        silence = detect_long_silence(video_path)
        result["audio_continuity"] = silence

        if silence["has_long_silence"]:
            result["passed"] = False
            result["reasons"].append(
                "final video contains a prolonged silent section"
            )

    except Exception as exc:
        result["passed"] = False
        result["reasons"].append(
            f"audio continuity analysis failed: {exc}"
        )

    return result


def save_result(result, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )


def main():
    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  python longform_quality_gate_v1_5.py "
            "<video> [source|final] [output_json]"
        )
        sys.exit(2)

    video_path = Path(sys.argv[1])

    mode = "source"

    if len(sys.argv) >= 3:
        mode = sys.argv[2].strip().lower()

    if len(sys.argv) >= 4:
        output_path = Path(sys.argv[3])
    else:
        output_path = Path(
            "work/longform/quality_gate_v1_5.json"
        )

    if mode == "final":
        result = validate_final(video_path)
    else:
        result = validate_source(video_path)

    save_result(result, output_path)

    print(json.dumps(result, indent=2))

    if result["passed"]:
        print("\nV1.5 QUALITY GATE: PASS")
        sys.exit(0)

    print("\nV1.5 QUALITY GATE: REJECT")
    sys.exit(24)


if __name__ == "__main__":
    main()
