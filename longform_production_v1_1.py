import json, re, subprocess, sys
from pathlib import Path
from openai import OpenAI

ROOT = Path("work/longform")
SCREENED = ROOT / "screened_sources.json"
OUT = ROOT / "ViralSpawnTV_Longform_V1_1.mp4"
OUTMETA = ROOT / "ViralSpawnTV_Longform_V1_1_metadata.json"
VOICE = "onyx"

def run(cmd):
    subprocess.run(cmd, check=True)

def probe_duration(path):
    return float(subprocess.check_output([
        "ffprobe","-v","error","-show_entries","format=duration",
        "-of","default=nw=1:nk=1",str(path)
    ], text=True).strip())

def tts(client, text, path):
    text = (text or "").strip()
    if not text:
        text = "Next up."
    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice=VOICE,
        input=text,
        instructions=(
            "Young adult American male with a neutral U.S. accent. "
            "Conversational gaming commentary, medium-fast, crisp and natural. "
            "Use excitement selectively. Never sound like an announcer."
        )
    ) as r:
        r.stream_to_file(path)

def narration_card(client, text, stem):
    mp3 = ROOT / f"{stem}.mp3"
    mp4 = ROOT / f"{stem}.mp4"
    tts(client, text, mp3)

    # No drawtext in V1.1: this deliberately removes font/path/text escaping
    # as a render failure point. Visual branding can be layered back later.
    run([
        "ffmpeg","-y",
        "-f","lavfi","-i","color=c=0x080b12:s=1920x1080:r=30",
        "-i",str(mp3),
        "-map","0:v","-map","1:a",
        "-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
        "-r","30","-c:a","aac","-ar","48000","-ac","2",
        "-shortest",str(mp4)
    ])
    return mp4

def normalize_clip(src, start, end, dest):
    # Every clip receives a guaranteed stereo audio track, even if source audio
    # is absent. This makes final concat deterministic.
    has_audio = subprocess.run([
        "ffprobe","-v","error","-select_streams","a:0",
        "-show_entries","stream=index","-of","csv=p=0",str(src)
    ], capture_output=True, text=True).stdout.strip() != ""

    dur = max(4.0, end - start)

    if has_audio:
        run([
            "ffmpeg","-y","-ss",str(start),"-t",str(dur),"-i",str(src),
            "-filter_complex",
            "[0:v]split=2[v1][v2];"
            "[v1]scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,boxblur=20:10[bg];"
            "[v2]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]",
            "-map","[v]","-map","0:a:0",
            "-r","30","-c:v","libx264","-preset","veryfast",
            "-c:a","aac","-ar","48000","-ac","2",
            "-t",str(dur),str(dest)
        ])
    else:
        run([
            "ffmpeg","-y","-ss",str(start),"-t",str(dur),"-i",str(src),
            "-f","lavfi","-t",str(dur),"-i","anullsrc=r=48000:cl=stereo",
            "-filter_complex",
            "[0:v]split=2[v1][v2];"
            "[v1]scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,boxblur=20:10[bg];"
            "[v2]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]",
            "-map","[v]","-map","1:a:0",
            "-r","30","-c:v","libx264","-preset","veryfast",
            "-c:a","aac","-ar","48000","-ac","2",
            "-shortest",str(dest)
        ])

def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    client = OpenAI()
    data = json.loads(SCREENED.read_text(encoding="utf-8"))
    sources = data["passed_sources"]

    compact = []
    for i, s in enumerate(sources):
        compact.append({
            "source_number": i + 1,
            "game": s.get("game"),
            "creator": s.get("channel"),
            "title": s.get("page_title"),
            "transcript": s.get("gate_transcript","")[:2500],
            "duration": probe_duration(s["local_path"])
        })

    prompt = f"""
Create one coherent original ViralSpawnTV gaming compilation episode.

Use 5-10 of the supplied clips. The narrator must provide original context,
reactions and transitions so this is an edited commentary episode rather than
a raw clip compilation. Do not invent factual details not supported below.
Translate important non-English dialogue naturally when useful.

For each selected source choose a continuous excerpt between 12 and 35 seconds,
within its actual duration. Put stronger moments later where practical.

Return ONLY JSON:
{{
 "title":"accurate clickable YouTube title",
 "description":"2-4 sentence description",
 "thumbnail_text":"2-5 words",
 "intro":"15-30 seconds of spoken cold-open narration",
 "segments":[
   {{"source_number":1,"start":0,"end":25,
     "before":"20-45 seconds original setup/commentary",
     "after":"10-25 seconds original reaction/transition"}}
 ],
 "outro":"10-20 second CTA"
}}

SOURCES:
{json.dumps(compact, ensure_ascii=False)}
"""
    r = client.responses.create(model="gpt-5.6", input=prompt)
    raw = re.sub(r"^```json\s*|\s*```$", "", r.output_text.strip())
    plan = json.loads(raw)

    pieces = [narration_card(client, plan["intro"], "intro")]
    bynum = {x["source_number"]: x for x in compact}
    used = []

    for idx, seg in enumerate(plan.get("segments", [])):
        try:
            n = int(seg["source_number"])
            info = bynum[n]
            source = sources[n-1]
            total = float(info["duration"])
            start = max(0.0, min(float(seg.get("start",0)), max(0,total-4)))
            end = max(start+4, min(float(seg.get("end",start+25)), total))
        except Exception:
            continue

        pieces.append(narration_card(client, seg.get("before",""), f"before_{idx:02d}"))

        clip = ROOT / f"clip_{idx:02d}.mp4"
        normalize_clip(Path(source["local_path"]), start, end, clip)
        pieces.append(clip)

        if (seg.get("after") or "").strip():
            pieces.append(narration_card(client, seg["after"], f"after_{idx:02d}"))

        used.append(source)

    if len(used) < 5:
        raise RuntimeError(f"Episode plan produced only {len(used)} usable clips.")

    pieces.append(narration_card(client, plan["outro"], "outro"))

    concat = ROOT / "concat_v1_1.txt"
    concat.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in pieces),
        encoding="utf-8"
    )

    # All pieces are already normalized to the same codecs/resolution/audio
    # format, so stream-copy concat is fast and much less failure-prone.
    run([
        "ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),
        "-c","copy","-movflags","+faststart",str(OUT)
    ])

    d = probe_duration(OUT)
    metadata = {
        "version":"1.1",
        "title":plan["title"][:100],
        "description":plan["description"],
        "thumbnail_text":plan["thumbnail_text"],
        "video_path":str(OUT),
        "duration_seconds":d,
        "source_count":len(used),
        "source_clips":[{
            "clip_id":s.get("clip_id"),
            "channel":s.get("channel"),
            "game":s.get("game"),
            "clip_url":s.get("clip_url"),
            "rights_status":"unverified"
        } for s in used],
        "rights_status":"unverified",
        "publish_status":"READY_FOR_UPLOAD"
    }
    OUTMETA.write_text(json.dumps(metadata,indent=2,ensure_ascii=False),encoding="utf-8")

    if d < 240:
        raise RuntimeError(
            f"Rendered episode is only {d/60:.1f} minutes; refusing public upload."
        )

    print(f"LONG-FORM V1.1 CREATED: {d/60:.2f} minutes")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("LONGFORM V1.1 PRODUCTION FAILED:", e)
        sys.exit(1)
