import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI
from longform_motion_gate_v1_4 import validate_motion
from longform_quality_gate_v1_5 import validate_source, validate_final


ROOT = Path("work/longform")

SCREENED = ROOT / "screened_sources.json"

OUT = ROOT / "ViralSpawnTV_Longform_V1_5.mp4"

# Permanent history for clips conclusively rejected by long-form gates.
REJECTED_HISTORY = Path("longform_rejected_history.json")
LONGFORM_HISTORY = Path("longform_history.json")

OUTMETA = (
    ROOT /
    "ViralSpawnTV_Longform_V1_5_metadata.json"
)

VOICE = "cedar"  # V1.5.2 natural voice

MIN_GOOD_CLIPS = 5
MAX_GOOD_CLIPS = 42
TARGET_MIN_SECONDS = 480.0
TARGET_MAX_SECONDS = 600.0

INTRO_IMAGE = Path("viralspawntv_intro.png")
OUTRO_IMAGE = Path("viralspawntv_outro.png")
INTRO_SECONDS = 4.0
OUTRO_SECONDS = 8.0


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


def tts(client, text, path, delivery="setup"):

    text = (
        (text or "").strip()
        or
        "Watch this."
    )

    styles = {
        "intro": (
            "Sound welcoming and confident, but casual. "
            "Use a little energy without sounding like an announcer."
        ),
        "setup": (
            "Sound like a real gaming creator explaining a clip to a friend. "
            "Begin relaxed and conversational. Let interest and energy rise "
            "slightly when the wording points toward the upcoming moment. "
            "Do not oversell the clip or use a repetitive narrator cadence."
        ),
        "excited": (
            "Sound genuinely impressed. Let excitement build naturally through "
            "the line with slightly quicker pace, more pitch movement, and "
            "stronger emphasis near the important phrase. Do not yell."
        ),
        "amused": (
            "Sound genuinely entertained. Use playful timing and let a subtle "
            "smile come through. Keep it casual."
        ),
        "serious": (
            "Sound focused and slightly restrained. Use deliberate timing and "
            "natural emphasis for tension without becoming theatrical."
        ),
        "outro": (
            "Sound relaxed, appreciative, and conversational. Keep the call to "
            "action natural, not like an advertisement or canned sign-off."
        ),
    }

    delivery = str(delivery or "setup").lower().strip()

    if delivery not in styles:
        delivery = "setup"

    instructions = (
        "Young adult American male gaming creator speaking naturally to viewers. "
        "Neutral United States accent. "
        "Sound conversational and spontaneous, not like a commercial, "
        "documentary, radio host, sports announcer, or text-to-speech system. "
        "Use natural pitch movement and vary rhythm slightly. "
        "Do not give every word equal emphasis. Use brief natural pauses when "
        "the meaning calls for them. Keep pronunciation clear without "
        "over-enunciating. Do not add words that are not in the script. "
        + styles[delivery]
    )

    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice=VOICE,
        input=text,
        instructions=instructions
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
        narr,
        delivery="setup"
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
            f"apad,atrim=duration={dur},"
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
            f"apad,atrim=duration={dur}[a]"
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


def render_brand_card(image_path, dest, seconds, speech_text, audio_name):
    if not image_path.exists():
        raise RuntimeError(f"Missing branding asset: {image_path}")

    narr = ROOT / audio_name
    brand_delivery = (
        "outro"
        if "outro" in audio_name.lower()
        else "intro"
    )

    tts(
        CLIENT,
        speech_text,
        narr,
        delivery=brand_delivery
    )

    # Animate the permanent artwork with a subtle zoom. The blurred background
    # fills 16:9 even if the source artwork is portrait-oriented.
    fc = (
        "[0:v]split=2[ibg][ifg];"
        "[ibg]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,boxblur=24:12[bg];"
        "[ifg]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "zoompan=z='min(zoom+0.0007,1.05)':d=1:s=1920x1080:fps=30[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v];"
        f"sine=frequency=80:sample_rate=48000:duration={seconds},volume=0.015[bed];"
        f"[1:a]aresample=48000,volume=1.25,apad,atrim=duration={seconds}[voice];"
        "[bed][voice]amix=inputs=2:duration=first:dropout_transition=0[a]"
    )

    run([
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(seconds), "-i", str(image_path),
        "-i", str(narr),
        "-filter_complex", fc,
        "-map", "[v]", "-map", "[a]",
        "-t", str(seconds), "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        str(dest),
    ])


def validate_piece(
    path,
    label
):

    ok, info = validate_motion(
        path,

        # V1.5 intentionally strict.
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
        f"\nV1.5 ACTIVITY CHECK: {label}"
    )

    print(
        json.dumps(
            info,
            indent=2
        )
    )

    return ok, info


def load_rejected_history():
    """
    Load permanent long-form rejection history.

    Preferred format:
    {"version": 1, "rejected_clips": [...]}

    A legacy list is accepted for compatibility.
    """

    if not REJECTED_HISTORY.exists():
        return {
            "version": 1,
            "rejected_clips": [],
        }

    try:
        data = json.loads(
            REJECTED_HISTORY.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {
            "version": 1,
            "rejected_clips": [],
        }

    if isinstance(data, list):
        return {
            "version": 1,
            "rejected_clips": data,
        }

    if not isinstance(data, dict):
        return {
            "version": 1,
            "rejected_clips": [],
        }

    data.setdefault("version", 1)
    data.setdefault("rejected_clips", [])

    if not isinstance(
        data.get("rejected_clips"),
        list,
    ):
        data["rejected_clips"] = []

    return data


def save_production_rejections(rows, sources):
    """
    Persist only deterministic quality/activity rejects.

    Transient render/FFmpeg/tool errors are intentionally NOT
    permanently blacklisted.
    """

    permanent_reasons = {
        "v1_5_source_quality_reject",
        "v1_5_visual_activity_reject",
    }

    history = load_rejected_history()

    existing = history.get(
        "rejected_clips",
        [],
    )

    merged = []
    seen_ids = set()
    seen_urls = set()

    def add_item(item):
        if not isinstance(item, dict):
            return

        clip_id = str(
            item.get("clip_id")
            or ""
        ).strip()

        clip_url = str(
            item.get("clip_url")
            or item.get("source")
            or ""
        ).strip()

        if clip_id and clip_id in seen_ids:
            return

        if (
            not clip_id
            and clip_url
            and clip_url in seen_urls
        ):
            return

        if clip_id:
            seen_ids.add(clip_id)

        if clip_url:
            seen_urls.add(clip_url)

        merged.append(item)

    for item in existing:
        if isinstance(item, str):
            item = {
                "clip_id": item,
                "reason": "legacy_rejection",
            }

        add_item(item)

    added = 0

    for row in rows:
        reason = str(
            row.get("reason")
            or ""
        ).strip()

        if reason not in permanent_reasons:
            continue

        try:
            n = int(
                row.get("source_number")
            )
        except Exception:
            n = 0

        source = (
            sources[n - 1]
            if 1 <= n <= len(sources)
            else {}
        )

        item = {
            "clip_id": (
                row.get("clip_id")
                or source.get("clip_id")
            ),
            "clip_url": (
                row.get("clip_url")
                or source.get("clip_url")
                or source.get("source")
            ),
            "game": (
                row.get("game")
                or source.get("game")
            ),
            "channel": (
                row.get("channel")
                or source.get("channel")
            ),
            "reason": reason,
            "rejection_stage": "production",
        }

        before = len(merged)
        add_item(item)

        if len(merged) > before:
            added += 1

    REJECTED_HISTORY.write_text(
        json.dumps(
            {
                "version": 1,
                "rejected_clips": merged,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "\nPermanent long-form production rejection history: "
        f"{len(merged)} total | {added} newly added."
    )



def load_previous_titles():
    """Collect previously published long-form titles from history when present."""
    if not LONGFORM_HISTORY.exists():
        return []

    try:
        data = json.loads(LONGFORM_HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return []

    titles = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() == "title" and isinstance(item, str):
                    title = item.strip()
                    if title:
                        titles.append(title)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)

    seen = set()
    unique = []
    for title in titles:
        key = title.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(title)

    return unique


def build_seo_metadata(client, used_sources, duration_seconds):
    """
    Generate final YouTube metadata only AFTER production knows which sources
    actually survived all gates and appear in the finished episode.
    """
    previous_titles = load_previous_titles()

    actual_sources = []
    for s in used_sources:
        actual_sources.append(
            {
                "game": s.get("game"),
                "creator": s.get("channel"),
                "source_title": s.get("page_title"),
                "transcript_excerpt": (s.get("gate_transcript") or "")[:1200],
            }
        )

    seo_prompt = f"""
Create final YouTube metadata for a ViralSpawnTV long-form gaming compilation.

The finished video is {duration_seconds / 60:.2f} minutes long.
Only use games, events, creators, and details supported by ACTUAL SOURCES below.

GOALS:
- Maximize accurate YouTube Search relevance and strong human click appeal.
- Never keyword-stuff, mislead, invent a game/event, or promise something absent.
- The title must be UNIQUE compared with PREVIOUS TITLES.
- Do not reuse generic recurring titles such as
  "Gaming Moments That Escalated Way Too Fast".
- Prefer the strongest recognizable game/search phrase near the beginning when
  the actual episode supports it.
- Write for humans first. Keep the title concise and readable.
- Aim for roughly 45-70 title characters when natural; absolute maximum 100.
- Description opening: 1-2 natural sentences clearly describing the actual
  games/moments in this episode. Put the most useful search phrases naturally
  in those opening sentences.
- Then add a short ViralSpawnTV value proposition and natural subscribe CTA.
- Do not dump repetitive keywords into the description.
- Generate 8-15 accurate YouTube tags. Tags are supplemental only.
- Generate 3-5 accurate hashtags, without spam.
- Thumbnail text must be 2-4 punchy words and should COMPLEMENT the title,
  not simply repeat it.
- primary_search_phrase should be one natural phrase that accurately represents
  the episode.
- secondary_search_phrases should contain 2-4 additional accurate phrases.

PREVIOUS TITLES:
{json.dumps(previous_titles[-100:], ensure_ascii=False)}

ACTUAL SOURCES IN THE FINISHED VIDEO:
{json.dumps(actual_sources, ensure_ascii=False)}

Return ONLY JSON:
{{
  "title": "unique final title",
  "description": "final description",
  "thumbnail_text": "2-4 words",
  "primary_search_phrase": "one phrase",
  "secondary_search_phrases": ["phrase 1", "phrase 2"],
  "tags": ["tag 1", "tag 2"],
  "hashtags": ["#Gaming", "#Example"]
}}
"""

    response = client.responses.create(
        model="gpt-5.6",
        input=seo_prompt,
    )

    raw = re.sub(
        r"^```json\s*|\s*```$",
        "",
        response.output_text.strip(),
    )

    seo = json.loads(raw)

    title = str(seo.get("title") or "").strip()[:100]
    if not title:
        raise RuntimeError("SEO metadata generator returned an empty title.")

    previous_keys = {t.casefold() for t in previous_titles}
    if title.casefold() in previous_keys:
        retry_prompt = seo_prompt + f"""

IMPORTANT RETRY:
Your proposed title "{title}" exactly matches a previous published title.
Generate a genuinely different title while remaining accurate.
"""
        response = client.responses.create(
            model="gpt-5.6",
            input=retry_prompt,
        )
        raw = re.sub(
            r"^```json\s*|\s*```$",
            "",
            response.output_text.strip(),
        )
        seo = json.loads(raw)
        title = str(seo.get("title") or "").strip()[:100]

        if not title or title.casefold() in previous_keys:
            raise RuntimeError(
                "Could not generate a unique long-form YouTube title."
            )

    description = str(seo.get("description") or "").strip()
    hashtags = [
        str(x).strip()
        for x in seo.get("hashtags", [])
        if str(x).strip()
    ][:5]

    if hashtags:
        description = description.rstrip() + "\n\n" + " ".join(hashtags)

    tags = []
    seen_tags = set()
    for item in seo.get("tags", []):
        tag = str(item).strip()
        key = tag.casefold()
        if tag and key not in seen_tags:
            seen_tags.add(key)
            tags.append(tag)
        if len(tags) >= 15:
            break

    return {
        "title": title,
        "description": description,
        "thumbnail_text": str(
            seo.get("thumbnail_text") or ""
        ).strip()[:40],
        "primary_search_phrase": str(
            seo.get("primary_search_phrase") or ""
        ).strip(),
        "secondary_search_phrases": [
            str(x).strip()
            for x in seo.get("secondary_search_phrases", [])
            if str(x).strip()
        ][:4],
        "tags": tags,
        "hashtags": hashtags,
        "previous_title_count_checked": len(previous_titles),
    }

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
Plan a fast-moving ViralSpawnTV gaming compilation targeting 8-10 minutes.

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
- Supply enough strong candidates to build at least 8 minutes after quality/activity rejections.
- Do not stretch weak footage just to increase runtime.
- payoff_start is seconds AFTER excerpt start.
- Leave approximately 7-18 seconds for the source-only payoff.
- The viewer should not have to wait through dead footage for the moment.

NARRATION WRITING STYLE:

- Write setup narration the way a real gaming creator would SAY it aloud.
- Use natural American English and contractions where they fit.
- Vary sentence length and structure across clips.
- Short conversational fragments are okay when natural.
- Avoid repetitive templates such as "This player...", "He then...",
  "What happens next...", and "Watch how..." across the episode.
- Avoid documentary narration, sports-announcer language, and generic AI
  summary phrasing.
- Do not force slang, catchphrases, fake stutters, or filler words.
- Keep setup narration concise so the source audio and payoff remain the star.
- Let the wording carry more energy for genuinely intense, funny, surprising,
  or clutch moments, but do not make every clip sound hyped.
- Never invent details that are not supported by the approved source metadata
  or transcript.

Return ONLY JSON:

{{
  "title":"provisional accurate title for planning only",
  "description":"provisional 2-4 sentence description",
  "thumbnail_text":"provisional 2-5 word thumbnail text",
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

    intro_piece = ROOT / "v15_brand_intro.mp4"
    render_brand_card(
        INTRO_IMAGE,
        intro_piece,
        INTRO_SECONDS,
        "This is ViralSpawnTV.",
        "v15_intro_voice.mp3",
    )
    pieces.append(intro_piece)

    used = []

    accepted_gameplay_seconds = 0.0

    rejected = []

    seen = set()

    # -----------------------------------------------------
    # V1.5.1 deterministic candidate coverage
    #
    # Keep GPT's preferred ordering and edit choices first.
    # If GPT omits any approved source, append that source as
    # a fallback candidate so production can keep trying until
    # the 8-minute gameplay target is reached or the approved
    # pool is genuinely exhausted.
    # -----------------------------------------------------

    planned_candidates = []
    planned_source_numbers = set()

    for cand in plan.get(
        "candidates",
        []
    ):
        try:
            n = int(
                cand["source_number"]
            )
        except Exception:
            continue

        if (
            n < 1
            or n > len(sources)
            or n in planned_source_numbers
        ):
            continue

        planned_source_numbers.add(n)
        planned_candidates.append(cand)

    for n, source in enumerate(
        sources,
        1,
    ):
        if n in planned_source_numbers:
            continue

        total = probe_duration(
            source["local_path"]
        )

        # Deterministic fallback excerpt:
        # use up to the first 40 seconds of the already-approved
        # source and leave a source-only payoff at the end.
        fallback_end = min(
            total,
            40.0,
        )

        if fallback_end < 8.0:
            continue

        fallback_payoff = min(
            18.0,
            max(
                5.0,
                fallback_end - 10.0,
            ),
        )

        planned_candidates.append(
            {
                "source_number": n,
                "start": 0.0,
                "end": fallback_end,
                "setup": (
                    "Here is where this one starts to turn."
                ),
                "payoff_start": fallback_payoff,
                "fallback_candidate": True,
            }
        )

    print(
        "\nV1.5 candidate coverage: "
        f"{len(plan.get('candidates', []))} GPT-planned | "
        f"{len(planned_candidates)} total attempts available | "
        f"{len(sources)} approved sources."
    )

    for cand in planned_candidates:

        if len(used) >= MAX_GOOD_CLIPS:
            break

        if accepted_gameplay_seconds >= TARGET_MIN_SECONDS:
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

            quality = validate_source(src)
            if not quality.get("passed"):
                rejected.append({
                    "source_number": n,
                    "clip_id": s.get("clip_id"),
                    "game": s.get("game"),
                    "channel": s.get("channel"),
                    "reason": "v1_5_source_quality_reject",
                    "quality": quality,
                })
                print("\nV1.5 QUALITY REJECT:", s.get("game"), "/", s.get("channel"))
                continue

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
            # V1.5 filenames
            # ---------------------------------------------

            a = (
                ROOT /
                f"v15_{n:02d}_setup.mp4"
            )

            b = (
                ROOT /
                f"v15_{n:02d}_payoff.mp4"
            )

            narr_file = (
                f"v15_{n:02d}_setup.mp3"
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
            # V1.5 dual motion/activity validation
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
                            "v1_5_visual_activity_reject",

                        "setup_motion":
                            motion_a,

                        "payoff_motion":
                            motion_b
                    }
                )

                print(
                    "\nV1.5 ACTIVITY REJECT: "
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

            accepted_gameplay_seconds += probe_duration(a) + probe_duration(b)

            print(
                "\nV1.5 ACTIVITY PASS: "
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
                "\nV1.5 CANDIDATE ERROR:",
                e
            )

    # Persist deterministic source-quality and visual-activity rejects.
    # Do this before final episode validation so a later episode-level failure
    # does not lose the useful per-source rejection decisions from this run.
    save_production_rejections(
        rejected,
        sources,
    )

    outro_piece = ROOT / "v15_brand_outro.mp4"
    outro_text = (
        plan.get("outro")
        or "Like what you saw? Subscribe to ViralSpawnTV, drop a like, and share it with your squad."
    )
    render_brand_card(
        OUTRO_IMAGE,
        outro_piece,
        OUTRO_SECONDS,
        outro_text,
        "v15_outro_voice.mp3",
    )
    pieces.append(outro_piece)

    # -----------------------------------------------------
    # Need enough genuinely usable clips
    # -----------------------------------------------------

    if len(used) < MIN_GOOD_CLIPS:

        raise RuntimeError(
            f"Only {len(used)} "
            "V1.5 activity-safe clips; "
            f"need {MIN_GOOD_CLIPS}."
        )

    # -----------------------------------------------------
    # Concatenate
    # -----------------------------------------------------

    concat = (
        ROOT /
        "concat_v1_5.txt"
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
    # FINAL V1.5 EPISODE VALIDATION
    #
    # Do NOT give final episode a huge freeze allowance.
    # The completed video should remain visually active.
    # -----------------------------------------------------

    final_ok, final_motion = (
        validate_motion(
            OUT,

            # Individual setup/payoff pieces already passed the strict
            # 1.0-second freeze gate above. The completed 8-10 minute
            # episode also contains intentional branded intro/outro
            # material, so the whole-episode gate allows a short isolated
            # low-motion section without weakening source-clip screening.
            max_single_freeze=5.0,

            max_total_freeze=10.0,

            sample_fps=4,

            dead_threshold=1.15,

            severe_dead_threshold=0.55,

            max_dead_run_seconds=5.0,

            max_severe_run_seconds=5.0,

            # Still reject episodes with broadly inactive footage.
            max_dead_ratio=0.12
        )
    )

    print(
        "\nFINAL V1.5 ACTIVITY REPORT:"
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
            "V1.5 visual activity gate: "
            f"{final_motion}"
        )

    final_quality = validate_final(OUT)
    print("\nFINAL V1.5 QUALITY/AUDIO REPORT:")
    print(json.dumps(final_quality, indent=2))
    if not final_quality.get("passed"):
        raise RuntimeError(
            "Final episode failed V1.5 quality/audio gate: "
            f"{final_quality}"
        )

    duration = probe_duration(
        OUT
    )

    # -----------------------------------------------------
    # V1.5 duration target
    # -----------------------------------------------------

    if duration < TARGET_MIN_SECONDS:

        raise RuntimeError(
            f"Episode only {duration / 60:.2f} minutes; "
            f"V1.5 requires at least {TARGET_MIN_SECONDS / 60:.0f} minutes "
            "of final runtime before upload."
        )

    if duration > TARGET_MAX_SECONDS:
        print(
            f"V1.5 NOTE: final runtime is {duration / 60:.2f} minutes, "
            f"slightly above the {TARGET_MAX_SECONDS / 60:.0f}-minute target. "
            "Keeping the completed quality-approved episode rather than "
            "cutting an approved clip mid-story."
        )

    # -----------------------------------------------------
    # Final YouTube SEO metadata
    #
    # Generate this only after all gates pass so the title/description
    # describe the clips that ACTUALLY survived into the finished video.
    # -----------------------------------------------------

    seo = build_seo_metadata(
        CLIENT,
        used,
        duration,
    )

    print("\nFINAL YOUTUBE SEO METADATA:")
    print(json.dumps(seo, indent=2, ensure_ascii=False))

    # -----------------------------------------------------
    # Metadata
    # -----------------------------------------------------

    meta = {

        "version":
            "1.6-seo",

        "title":
            seo["title"],

        "description":
            seo["description"],

        "thumbnail_text":
            seo["thumbnail_text"],

        "primary_search_phrase":
            seo["primary_search_phrase"],

        "secondary_search_phrases":
            seo["secondary_search_phrases"],

        "tags":
            seo["tags"],

        "hashtags":
            seo["hashtags"],

        "seo_version":
            "1.6-final-used-sources",

        "previous_title_count_checked":
            seo["previous_title_count_checked"],

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

        "target_min_seconds":
            TARGET_MIN_SECONDS,

        "target_max_seconds":
            TARGET_MAX_SECONDS,

        "accepted_gameplay_seconds":
            accepted_gameplay_seconds,

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

        "quality_gate_version":
            "1.5",

        "quality_gate_passed":
            True,

        "audio_continuity_gate_passed":
            True,

        "branding_intro":
            str(INTRO_IMAGE),

        "branding_outro":
            str(OUTRO_IMAGE),

        "final_quality":
            final_quality,

        "final_motion":
            final_motion,

        "motion_rejections":
            rejected,

        "rejected_history_file":
            str(REJECTED_HISTORY),

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


    print(
        "\n"
        f"V1.5 CREATED: "
        f"{duration / 60:.2f} minutes | "
        f"good clips {len(used)} | "
        f"rejected {len(rejected)}"
    )


if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print(
            "LONGFORM V1.5 "
            "PRODUCTION FAILED:",
            e
        )

        sys.exit(1)
