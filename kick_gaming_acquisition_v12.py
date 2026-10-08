import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path



RANKED = Path("work/v12_prescreened_candidates.json")
REJECTED = Path("shorts_rejected_history.json")

OUTDIR = Path("work/kick_gaming")
OUT = OUTDIR / "selected_kick_gaming_source.mp4"
RESULT = OUTDIR / "acquisition_result.json"

MIN_WINDOW_SECONDS = 19.0
MAX_WINDOW_SECONDS = 58.5

# Preserve a minimum 19-second core for the existing 1-second branded outro.
# An undersized stream-copy result uses the existing accurate encode fallback.
MIN_LOCAL_SECONDS = 19.0

KICK_API_TEMPLATE = "https://kick.com/api/v2/clips/{clip_id}/play"
API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://kick.com/",
    "Origin": "https://kick.com",
    "X-Requested-With": "XMLHttpRequest",
}


def load_json(path, default):
    try:
        return json.loads(
            Path(path).read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return default


def load_rejected_ids():
    data = load_json(
        REJECTED,
        {
            "version": 1,
            "clip_ids": [],
        },
    )

    if isinstance(data, dict):
        data = data.get(
            "clip_ids",
            [],
        )

    if not isinstance(
        data,
        list,
    ):
        data = []

    return {
        str(item).strip()
        for item in data
        if str(item).strip()
    }


def save_rejected_ids(rejected):
    REJECTED.write_text(
        json.dumps(
            {
                "version": 1,
                "clip_ids": sorted(
                    {
                        str(x).strip()
                        for x in rejected
                        if str(x).strip()
                    }
                ),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def candidate_id(candidate):
    return str(
        candidate.get(
            "clip_id"
        )
        or candidate.get(
            "id"
        )
        or ""
    ).strip()


def candidate_url(candidate):
    return str(
        candidate.get(
            "clip_url"
        )
        or candidate.get(
            "url"
        )
        or ""
    ).strip()


def forced_candidate_id():
    return str(
        os.getenv(
            "V12_FORCE_CLIP_ID",
            "",
        )
    ).strip()


def ordered_candidates(
    candidates,
):
    forced_id = forced_candidate_id()

    if not forced_id:
        return list(
            candidates
        )

    forced = [
        candidate
        for candidate in candidates
        if candidate_id(
            candidate
        )
        ==
        forced_id
    ]

    if not forced:
        raise RuntimeError(
            f"Forced candidate not found in prescreen list: "
            f"{forced_id}"
        )

    # Forced mode intentionally ignores rejection-history ordering.
    # It is used by the reliability pipeline to re-acquire the best source
    # after all shortlist candidates have been source-gated.
    return forced


def probe_duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    return float(
        result.stdout.strip()
    )


def refresh_media_url(
    clip_id,
):
    url = KICK_API_TEMPLATE.format(
        clip_id=clip_id
    )

    request = urllib.request.Request(
        url,
        headers=API_HEADERS,
        method="GET",
    )

    with urllib.request.urlopen(
        request,
        timeout=12,
    ) as response:
        payload = json.loads(
            response.read().decode(
                "utf-8",
                errors="replace",
            )
        )

    clip = payload.get(
        "clip"
    )

    if not isinstance(
        clip,
        dict,
    ):
        return None

    return str(
        clip.get(
            "clip_url",
            "",
        )
    ).strip() or None



def render_window(
    playlist_url,
    start,
    length,
):
    if OUT.exists():
        OUT.unlink()

    # Fast path: reuse the cached HLS URL and stream-copy only the proposed
    # 40-58 second window.
    fast = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "warning",
        "-ss",
        f"{start:.3f}",
        "-i",
        playlist_url,
        "-t",
        f"{length:.3f}",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(OUT),
    ]

    proc = subprocess.run(
        fast
    )

    if (
        proc.returncode == 0
        and OUT.exists()
        and OUT.stat().st_size > 10000
    ):
        try:
            seconds = probe_duration(
                OUT
            )

            if (
                seconds >= MIN_LOCAL_SECONDS
                and
                seconds <= 60.0
            ):
                return seconds

        except Exception:
            pass

    if OUT.exists():
        OUT.unlink()

    # Accurate fallback.
    accurate = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "warning",
        "-ss",
        f"{start:.3f}",
        "-i",
        playlist_url,
        "-t",
        f"{length:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(OUT),
    ]

    subprocess.run(
        accurate,
        check=True,
    )

    return probe_duration(
        OUT
    )


def acquire(candidate):
    clip_id = candidate_id(
        candidate
    )

    clip_url = candidate_url(
        candidate
    )

    if not clip_id or not clip_url:
        raise RuntimeError(
            "Candidate missing clip_id or clip_url."
        )

    start = float(
        candidate.get(
            "proposed_window_start",
            0.0,
        )
    )

    end = float(
        candidate.get(
            "proposed_window_end",
            0.0,
        )
    )

    length = (
        end - start
    )

    if not (
        MIN_WINDOW_SECONDS
        <= length
        <= MAX_WINDOW_SECONDS
    ):
        raise RuntimeError(
            f"Invalid V12.9 proposed window: "
            f"{start:.2f}-{end:.2f}s "
            f"({length:.2f}s)."
        )

    OUTDIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    playlist_url = str(
        candidate.get(
            "media_url"
        )
        or
        candidate.get(
            "playlist_url"
        )
        or ""
    ).strip()

    cached_playlist_used = bool(
        playlist_url
    )

    errors = []

    if playlist_url:
        try:
            local_seconds = (
                render_window(
                    playlist_url,
                    start,
                    length,
                )
            )
        except Exception as exc:
            errors.append(
                f"cached HLS: {exc}"
            )
            local_seconds = None
    else:
        local_seconds = None

    # Signed HLS URLs can occasionally expire. Only then pay for one page
    # revisit instead of doing it for every candidate.
    if local_seconds is None:
        playlist_url = (
            refresh_media_url(
                clip_id,
            )
        )

        if not playlist_url:
            raise RuntimeError(
                "No direct Kick media URL available; "
                + "; ".join(errors)
            )

        cached_playlist_used = False

        local_seconds = (
            render_window(
                playlist_url,
                start,
                length,
            )
        )

    if (
        not OUT.exists()
        or
        OUT.stat().st_size < 10000
    ):
        raise RuntimeError(
            "Acquired window file is missing or too small."
        )

    if local_seconds < MIN_LOCAL_SECONDS:
        raise RuntimeError(
            f"Selected local window too short: "
            f"{local_seconds:.2f}s."
        )

    print(
        f"ACQUIRED V12.14.5 WINDOW: "
        f"original {start:.2f}-{end:.2f}s -> "
        f"local {local_seconds:.2f}s | "
        f"cached_hls={cached_playlist_used}"
    )

    result = {
        "success":
            True,
        "version":
            "12.14.5-forced-candidate-window-acquisition",
        "clip_id":
            clip_id,
        "clip_url":
            clip_url,
        "channel":
            candidate.get(
                "channel"
            ),
        "game":
            candidate.get(
                "game"
            ),
        "page_title":
            candidate.get(
                "page_title"
            ),
        "page_description":
            candidate.get(
                "page_description"
            ),
        "original_source_duration_seconds":
            candidate.get(
                "source_duration_seconds"
            ),
        "proposed_window_start_original":
            start,
        "proposed_window_end_original":
            end,
        "proposed_window_label":
            candidate.get(
                "proposed_window_label"
            ),
        "local_source_is_selected_window":
            True,
        "local_path":
            str(OUT),
        "source_duration_seconds":
            round(
                local_seconds,
                3,
            ),
        "playlist_cache_reused":
            cached_playlist_used,
        "v12_metadata_score":
            candidate.get(
                "v12_metadata_score"
            ),
        "v12_metadata_reason":
            candidate.get(
                "v12_metadata_reason"
            ),
        "prescreen_predicted_score":
            candidate.get(
                "prescreen_predicted_score"
            ),
        "prescreen_probability_72_plus":
            candidate.get(
                "prescreen_probability_72_plus"
            ),
        "prescreen_hook":
            candidate.get(
                "prescreen_hook"
            ),
        "prescreen_story_sustain":
            candidate.get(
                "prescreen_story_sustain"
            ),
        "prescreen_payoff":
            candidate.get(
                "prescreen_payoff"
            ),
        "prescreen_ending_strength":
            candidate.get(
                "prescreen_ending_strength"
            ),
        "prescreen_action":
            candidate.get(
                "prescreen_action"
            ),
        "prescreen_clarity":
            candidate.get(
                "prescreen_clarity"
            ),
        "prescreen_opening_coherence":
            candidate.get(
                "prescreen_opening_coherence"
            ),
        "prescreen_audio_context":
            candidate.get(
                "prescreen_audio_context"
            ),
        "prescreen_ending_audio_relevance":
            candidate.get(
                "prescreen_ending_audio_relevance"
            ),
        "prescreen_audio_reason":
            candidate.get(
                "prescreen_audio_reason"
            ),
        "prescreen_rank_score":
            candidate.get(
                "prescreen_rank_score"
            ),
        "prescreen_reason":
            candidate.get(
                "prescreen_reason"
            ),
        "kick_view_count":
            candidate.get(
                "kick_view_count",
                0,
            ),
        "kick_like_count":
            candidate.get(
                "kick_like_count",
                0,
            ),
        "kick_like_rate_pct":
            candidate.get(
                "kick_like_rate_pct",
                0.0,
            ),
        "rights_status":
            "unverified",
        "creator_permission_verified":
            False,
        "game_rights_verified":
            False,
        "acquisition_context":
            "automated_public_pipeline",
        "public_publish_allowed":
            True,
    }

    RESULT.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return result


def main():
    if not RANKED.exists():
        raise RuntimeError(
            "Missing work/v12_prescreened_candidates.json"
        )

    data = load_json(
        RANKED,
        {},
    )

    candidates = data.get(
        "candidates",
        [],
    )

    rejected = load_rejected_ids()

    save_rejected_ids(
        rejected
    )

    print(
        f"V12.14.5 prescreened candidates available: "
        f"{len(candidates)}"
    )

    errors = []

    forced_id = forced_candidate_id()

    candidate_sequence = ordered_candidates(
        candidates
    )

    for rank, candidate in enumerate(
        candidate_sequence,
        1,
    ):
        clip_id = candidate_id(
            candidate
        )

        if not clip_id:
            continue

        if (
            not forced_id
            and
            clip_id in rejected
        ):
            print(
                f"SKIP rank {rank}: "
                f"rejected {clip_id}"
            )
            continue

        print(
            f"ACQUIRE V12.14.5 rank {rank}: "
            f"{candidate.get('game')} / "
            f"{candidate.get('channel')} / "
            f"{clip_id} | "
            f"window="
            f"{candidate.get('proposed_window_start')}-"
            f"{candidate.get('proposed_window_end')} | "
            f"rank="
            f"{candidate.get('prescreen_rank_score')}"
        )

        try:
            result = acquire(
                candidate
            )

            print(
                f"ACQUIRED: "
                f"{result['clip_id']} -> "
                f"{result['local_path']}"
            )

            return

        except Exception as exc:
            print(
                f"ACQUISITION FAILED "
                f"{clip_id}: {exc}"
            )

            errors.append(
                {
                    "clip_id":
                        clip_id,
                    "error":
                        str(exc),
                }
            )

            rejected.add(
                clip_id
            )

            save_rejected_ids(
                rejected
            )

    RESULT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    RESULT.write_text(
        json.dumps(
            {
                "success":
                    False,
                "reason":
                    "prescreened_batch_exhausted",
                "errors":
                    errors,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    raise RuntimeError(
        "No remaining V12.13 prescreened candidate."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "KICK V12.14.5 ACQUISITION FAILED:",
            exc,
        )
        sys.exit(1)
