import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI
from longform_motion_gate_v1_4 import validate_motion


ROOT = Path("work/longform")

SCREENED = ROOT / "screened_sources.json"

OUT = ROOT / "ViralSpawnTV_Longform_V1_4.mp4"

OUTMETA = (
    ROOT /
    "ViralSpawnTV_Longform_V1_4_metadata.json"
)

VOICE = "onyx"

MIN_GOOD_CLIPS = 5
MAX_GOOD_CLIPS = 9


def run(cmd):
    subprocess.run(
        cmd,
        check=True
    )


def probe_duration(path):
    return float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path)
            ],
            text=True
        ).strip()
    )


def has_audio(path):
    return bool(
        subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries",
                "stream=index",
                "-of", "csv=p=0",
                str(path)
            ],
            capture_output=True,
            text=True
        ).stdout.strip()
    )


def tts(client, text, path):

    text = (
        (text or "").strip()
        or
        "Watch this."
    )

    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice=VOICE,
        input=text,
        instructions=(
            "Young adult American male, "
            "neutral U.S. accent. "
            "Natural gaming commentary, "
            "medium-fast, crisp and conversational. "
            "Selective excitement. "
            "Never announcer-like."
        )
    ) as r:

        r.stream_to_file(path)


def vf():

    return (
        "[0:v]split=2[v1][v2];"

        "[v1]"
        "scale=1920:1080:"
        "force_original_aspect_ratio=increase,"
        "crop=1920:1080,"
        "boxblur=20:10[bg];"

        "[v2]"
        "scale=1920:1080:"
        "force_original_aspect_ratio=decrease"
        "[fg];"

        "[bg][fg]"
        "overlay=(W-w)/2:(H-h)/2,"
        "format=yuv420p[v]"
    )


def render_narrated(
    src,
    start,
    end,
    text,
    dest,
    audio_name
):

    narr = ROOT / audio_name

    tts(
        CLIENT,
        text,
        narr
    )

    ndur = probe_duration(narr)

    total = probe_duration(src)

    dur = max(
        4.0,
        end - start,
        ndur + 0.8
    )

    end = min(
        total,
        start + dur
    )

    dur = max(
        0.5,
        end - start
    )

    if has_audio(src):

        fc = (
            vf()
            +
            ";[0:a]"
            "aresample=48000,"
            "volume=1.0"
            "[game];"

            "[1:a]"
            "aresample=48000,"
            "volume=1.35,"
            "asplit=2"
            "[voice_sc][voice_mix];"

            "[game][voice_sc]"
            "sidechaincompress="
            "threshold=0.02:"
            "ratio=10:"
            "attack=20:"
            "release=350"
            "[ducked];"

            "[ducked][voice_mix]"
            "amix="
            "inputs=2:"
            "duration=first:"
            "dropout_transition=0"
            "[a]"
        )

        run(
            [
                "ffmpeg",
                "-y",

                "-ss", str(start),
                "-t", str(dur),
                "-i", str(src),

                "-i", str(narr),

                "-filter_complex", fc,

                "-map", "[v]",
                "-map", "[a]",

                "-r", "30",

                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",

                "-c:a", "aac",
                "-ar", "48000",
                "-ac", "2",

                "-t", str(dur),

                str(dest)
            ]
        )

    else:

        fc = (
            vf()
            +
            ";[1:a]"
            "aresample=48000,"
            "volume=1.35,"
            "apad[a]"
        )

        run(
            [
                "ffmpeg",
                "-y",

                "-ss", str(start),
                "-t", str(dur),
                "-i", str(src),

                "-i", str(narr),

                "-filter_complex", fc,

                "-map", "[v]",
                "-map", "[a]",

                "-r", "30",

                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",

                "-c:a", "aac",
                "-ar", "48000",
                "-ac", "2",

                "-t", str(dur),

                str(dest)
            ]
        )


def render_source(
    src,
    start,
    end,
    dest
):

    dur = max(
        0.5,
        end - start
    )

    if has_audio(src):

        run(
            [
                "ffmpeg",
                "-y",

                "-ss", str(start),
                "-t", str(dur),

                "-i", str(src),

                "-filter_complex", vf(),

                "-map", "[v]",
                "-map", "0:a:0",

                "-r", "30",

                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",

                "-c:a", "aac",
                "-ar", "48000",
                "-ac", "2",

                str(dest)
            ]
        )

    else:

        run(
            [
                "ffmpeg",
                "-y",

                "-ss", str(start),
                "-t", str(dur),

                "-i", str(src),

                "-f", "lavfi",
                "-t", str(dur),
                "-i",
                "anullsrc=r=48000:cl=stereo",

                "-filter_complex", vf(),

                "-map", "[v]",
                "-map", "1:a:0",

                "-r", "30",

                "-c:v", "libx264",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",

                "-c:a", "aac",
                "-ar", "48000",
                "-ac", "2",

                "-shortest",

                str(dest)
            ]
        )


def validate_piece(
    path,
    label
):

    ok, info = validate_motion(
        path,

        # V1.4 intentionally strict.
        max_single_freeze=1.0,
        max_total_freeze=2.5,

        sample_fps=4,

        dead_threshold=1.15,
        severe_dead_threshold=0.55,

        max_dead_run_seconds=1.25,
        max_severe_run_seconds=0.85,

        max_dead_ratio=0.30
    )

    print(
        f"\nV1.4 ACTIVITY CHECK: {label}"
    )

    print(
        json.dumps(
            info,
            indent=2
        )
    )

    return ok, info


def main():

    global CLIENT

    CLIENT = OpenAI()

    ROOT.mkdir(
        parents=True,
        exist_ok=True
    )

    data = json.loads(
        SCREENED.read_text(
            encoding="utf-8"
        )
    )

    sources = data[
        "passed_sources"
    ]

    compact = []

    for i, s in enumerate(sources):

        compact.append(
            {
                "source_number": i + 1,

                "game":
                    s.get("game"),

                "creator":
                    s.get("channel"),

                "title":
                    s.get("page_title"),

                "transcript":
                    s.get(
                        "gate_transcript",
                        ""
                    )[:3000],

                "duration":
                    probe_duration(
                        s["local_path"]
                    )
            }
        )

    prompt = f"""
Plan a fast-moving ViralSpawnTV gaming compilation.

Rank ALL supplied approved sources in the order they should be attempted so
replacements are available when a clip fails the visual activity test.

CRITICAL EDITING RULES:

- NO black screens.
- NO narration cards.
- Moving gameplay must remain visible continuously.
- Narration goes directly over moving gameplay.
- Narration stops before the main payoff.
- Reject boring setup whenever possible.
- Prefer excerpts with obvious player movement, combat, camera movement,
  action, reactions, kills, escapes, chases, fights or other meaningful
  visual activity.
- Avoid menus, loading screens, scoreboards, static inventory screens,
  waiting, standing still, spectating dead scenes, or long periods where
  almost nothing visually changes.
- Start as close to the meaningful action as possible.
- For each candidate choose a roughly 20-40 second excerpt where possible.
- payoff_start is seconds AFTER excerpt start.
- Leave approximately 7-18 seconds for the source-only payoff.
- The viewer should not have to wait through dead footage for the moment.

Return ONLY JSON:

{{
  "title":"accurate clickable title",
  "description":"2-4 sentences",
  "thumbnail_text":"2-5 words",
  "intro":"1-2 sentence cold open",

  "candidates":[
    {{
      "source_number":1,
      "start":0,
      "end":30,
      "setup":"short setup",
      "payoff_start":12
    }}
  ],

  "outro":"short CTA"
}}

SOURCES:

{json.dumps(compact, ensure_ascii=False)}
"""

    resp = CLIENT.responses.create(
        model="gpt-5.6",
        input=prompt
    )

    raw = re.sub(
        r"^```json\s*|\s*```$",
        "",
        resp.output_text.strip()
    )

    plan = json.loads(raw)

    pieces = []

    used = []

    rejected = []

    seen = set()

    for cand in plan.get(
        "candidates",
        []
    ):

        if len(used) >= MAX_GOOD_CLIPS:
            break

        try:

            n = int(
                cand["source_number"]
            )

            if (
                n < 1
                or
                n > len(sources)
                or
                n in seen
            ):
                continue

            seen.add(n)

            s = sources[n - 1]

            src = Path(
                s["local_path"]
            )

            total = probe_duration(src)

            start = max(
                0.0,
                min(
                    float(
                        cand.get(
                            "start",
                            0
                        )
                    ),
                    max(
                        0,
                        total - 8
                    )
                )
            )

            end = max(
                start + 8,
                min(
                    float(
                        cand.get(
                            "end",
                            start + 30
                        )
                    ),
                    total
                )
            )

            payoff = max(
                start + 5,
                min(
                    start
                    +
                    float(
                        cand.get(
                            "payoff_start",
                            12
                        )
                    ),
                    end - 5
                )
            )

            setup = (
                cand.get(
                    "setup"
                )
                or
                ""
            ).strip()

            if not used:

                setup = (
                    (
                        plan.get(
                            "intro"
                        )
                        or
                        ""
                    ).strip()
                    +
                    " "
                    +
                    setup
                ).strip()

            # ---------------------------------------------
            # V1.4 filenames
            # ---------------------------------------------

            a = (
                ROOT /
                f"v14_{n:02d}_setup.mp4"
            )

            b = (
                ROOT /
                f"v14_{n:02d}_payoff.mp4"
            )

            narr_file = (
                f"v14_{n:02d}_setup.mp3"
            )

            # ---------------------------------------------
            # Render setup over moving gameplay
            # ---------------------------------------------

            render_narrated(
                src,
                start,
                payoff,
                setup,
                a,
                narr_file
            )

            # ---------------------------------------------
            # Render source-only payoff
            # ---------------------------------------------

            render_source(
                src,
                payoff,
                end,
                b
            )

            # ---------------------------------------------
            # V1.4 dual motion/activity validation
            # ---------------------------------------------

            ok_a, motion_a = (
                validate_piece(
                    a,
                    (
                        f"{s.get('game')} / "
                        f"{s.get('channel')} / "
                        "SETUP"
                    )
                )
            )

            ok_b, motion_b = (
                validate_piece(
                    b,
                    (
                        f"{s.get('game')} / "
                        f"{s.get('channel')} / "
                        "PAYOFF"
                    )
                )
            )

            if not (
                ok_a
                and
                ok_b
            ):

                rejected.append(
                    {
                        "source_number":
                            n,

                        "clip_id":
                            s.get(
                                "clip_id"
                            ),

                        "game":
                            s.get(
                                "game"
                            ),

                        "channel":
                            s.get(
                                "channel"
                            ),

                        "reason":
                            "v1_4_visual_activity_reject",

                        "setup_motion":
                            motion_a,

                        "payoff_motion":
                            motion_b
                    }
                )

                print(
                    "\nV1.4 ACTIVITY REJECT: "
                    f"{s.get('game')} / "
                    f"{s.get('channel')} / "
                    f"{s.get('clip_id')}"
                )

                a.unlink(
                    missing_ok=True
                )

                b.unlink(
                    missing_ok=True
                )

                continue

            pieces.extend(
                [a, b]
            )

            used.append(s)

            print(
                "\nV1.4 ACTIVITY PASS: "
                f"{s.get('game')} / "
                f"{s.get('channel')} / "
                f"{s.get('clip_id')}"
            )

        except Exception as e:

            rejected.append(
                {
                    "source_number":
                        cand.get(
                            "source_number"
                        ),

                    "reason":
                        "render_or_activity_error",

                    "error":
                        str(e)
                }
            )

            print(
                "\nV1.4 CANDIDATE ERROR:",
                e
            )

    # -----------------------------------------------------
    # Need enough genuinely usable clips
    # -----------------------------------------------------

    if len(used) < MIN_GOOD_CLIPS:

        raise RuntimeError(
            f"Only {len(used)} "
            "V1.4 activity-safe clips; "
            f"need {MIN_GOOD_CLIPS}."
        )

    # -----------------------------------------------------
    # Concatenate
    # -----------------------------------------------------

    concat = (
        ROOT /
        "concat_v1_4.txt"
    )

    concat.write_text(
        "\n".join(
            (
                "file "
                f"'{p.resolve().as_posix()}'"
            )
            for p in pieces
        ),
        encoding="utf-8"
    )

    run(
        [
            "ffmpeg",
            "-y",

            "-f", "concat",
            "-safe", "0",

            "-i", str(concat),

            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-r", "30",

            "-c:a", "aac",
            "-ar", "48000",
            "-ac", "2",

            "-movflags",
            "+faststart",

            str(OUT)
        ]
    )

    # -----------------------------------------------------
    # FINAL V1.4 EPISODE VALIDATION
    #
    # Do NOT give final episode a huge freeze allowance.
    # The completed video should remain visually active.
    # -----------------------------------------------------

    final_ok, final_motion = (
        validate_motion(
            OUT,

            max_single_freeze=1.0,

            # Slightly more cumulative tolerance because
            # the episode is several minutes long.
            max_total_freeze=4.0,

            sample_fps=4,

            dead_threshold=1.15,

            severe_dead_threshold=0.55,

            max_dead_run_seconds=1.25,

            max_severe_run_seconds=0.85,

            # Across an entire episode we want the dead
            # percentage substantially below V1.3 behavior.
            max_dead_ratio=0.22
        )
    )

    print(
        "\nFINAL V1.4 ACTIVITY REPORT:"
    )

    print(
        json.dumps(
            final_motion,
            indent=2
        )
    )

    if not final_ok:

        raise RuntimeError(
            "Final episode failed "
            "V1.4 visual activity gate: "
            f"{final_motion}"
        )

    duration = probe_duration(
        OUT
    )

    # -----------------------------------------------------
    # Metadata
    # -----------------------------------------------------

    meta = {

        "version":
            "1.4",

        "title":
            plan["title"][:100],

        "description":
            plan["description"],

        "thumbnail_text":
            plan["thumbnail_text"],

        "outro_text":
            plan.get(
                "outro",
                ""
            ),

        "video_path":
            str(OUT),

        "duration_seconds":
            duration,

        "source_count":
            len(used),

        "continuous_gameplay":
            True,

        "narration_cards":
            False,

        "black_screen_intro":
            False,

        "motion_gate_version":
            "1.4",

        "motion_gate_passed":
            True,

        "visual_activity_gate_passed":
            True,

        "final_motion":
            final_motion,

        "motion_rejections":
            rejected,

        "source_clips":
            [
                {
                    "clip_id":
                        s.get(
                            "clip_id"
                        ),

                    "channel":
                        s.get(
                            "channel"
                        ),

                    "game":
                        s.get(
                            "game"
                        ),

                    "clip_url":
                        s.get(
                            "clip_url"
                        ),

                    "rights_status":
                        "unverified"
                }

                for s in used
            ],

        "rights_status":
            "unverified",

        "publish_status":
            "READY_FOR_UPLOAD"
    }

    OUTMETA.write_text(
        json.dumps(
            meta,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    # -----------------------------------------------------
    # Duration floor
    # -----------------------------------------------------

    if duration < 150:

        raise RuntimeError(
            f"Episode only "
            f"{duration / 60:.1f} minutes; "
            "refusing upload."
        )

    print(
        "\n"
        f"V1.4 CREATED: "
        f"{duration / 60:.2f} minutes | "
        f"good clips {len(used)} | "
        f"rejected {len(rejected)}"
    )


if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print(
            "LONGFORM V1.4 "
            "PRODUCTION FAILED:",
            e
        )

        sys.exit(1)
