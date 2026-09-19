import json, re, subprocess, sys
from pathlib import Path
from openai import OpenAI

ROOT = Path("work/longform")
SCREENED = ROOT / "screened_sources.json"
OUT = ROOT / "ViralSpawnTV_Longform_V1_2.mp4"
OUTMETA = ROOT / "ViralSpawnTV_Longform_V1_2_metadata.json"
VOICE = "onyx"

def run(cmd):
    subprocess.run(cmd, check=True)

def probe_duration(path):
    return float(subprocess.check_output([
        "ffprobe","-v","error","-show_entries","format=duration",
        "-of","default=nw=1:nk=1",str(path)
    ], text=True).strip())

def has_audio(path):
    return bool(subprocess.run([
        "ffprobe","-v","error","-select_streams","a:0",
        "-show_entries","stream=index","-of","csv=p=0",str(path)
    ], capture_output=True, text=True).stdout.strip())

def tts(client, text, path):
    text = (text or "").strip()
    if not text:
        text = "Watch this."
    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice=VOICE,
        input=text,
        instructions=(
            "Young adult American male, neutral U.S. accent. Natural gaming commentary, "
            "medium-fast, crisp, conversational. React like a real gaming creator. "
            "Use excitement only when the moment earns it. Never sound like an announcer."
        )
    ) as r:
        r.stream_to_file(path)

def make_gameplay_piece(src, start, end, narration, dest, narration_name):
    """Continuous gameplay. Narration overlays gameplay; source audio ducks beneath it."""
    dur = max(4.0, end - start)
    narr = ROOT / narration_name
    tts(CLIENT, narration, narr)
    ndur = probe_duration(narr)

    # Give narration room while keeping the actual clip payoff intact.
    # If narration is longer than the selected clip, extend the source window where possible.
    total = probe_duration(src)
    needed = max(dur, ndur + 1.0)
    end = min(total, start + needed)
    dur = max(4.0, end - start)

    vf = (
        "[0:v]split=2[v1][v2];"
        "[v1]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,boxblur=20:10[bg];"
        "[v2]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )

    if has_audio(src):
        # narration begins almost immediately; game audio ducks while voice is active.
        fc = (
            vf +
            f";[0:a]aresample=48000,volume=1.0[game];"
            f"[1:a]aresample=48000,volume=1.35[voice];"
            f"[game][voice]sidechaincompress=threshold=0.02:ratio=10:"
            f"attack=20:release=350[ducked];"
            f"[ducked][voice]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
        run([
            "ffmpeg","-y","-ss",str(start),"-t",str(dur),"-i",str(src),
            "-i",str(narr),"-filter_complex",fc,
            "-map","[v]","-map","[a]",
            "-r","30","-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
            "-c:a","aac","-ar","48000","-ac","2","-t",str(dur),str(dest)
        ])
    else:
        fc = vf + ";[1:a]aresample=48000,apad[a]"
        run([
            "ffmpeg","-y","-ss",str(start),"-t",str(dur),"-i",str(src),
            "-i",str(narr),"-filter_complex",fc,
            "-map","[v]","-map","[a]",
            "-r","30","-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
            "-c:a","aac","-ar","48000","-ac","2","-t",str(dur),str(dest)
        ])

    return dest

def make_source_only(src, start, end, dest):
    dur=max(4.0,end-start)
    vf=(
        "[0:v]split=2[v1][v2];"
        "[v1]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,boxblur=20:10[bg];"
        "[v2]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )
    if has_audio(src):
        run(["ffmpeg","-y","-ss",str(start),"-t",str(dur),"-i",str(src),
             "-filter_complex",vf,"-map","[v]","-map","0:a:0",
             "-r","30","-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
             "-c:a","aac","-ar","48000","-ac","2",str(dest)])
    else:
        run(["ffmpeg","-y","-ss",str(start),"-t",str(dur),"-i",str(src),
             "-f","lavfi","-t",str(dur),"-i","anullsrc=r=48000:cl=stereo",
             "-filter_complex",vf,"-map","[v]","-map","1:a:0",
             "-r","30","-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
             "-c:a","aac","-ar","48000","-ac","2","-shortest",str(dest)])

def main():
    global CLIENT
    CLIENT = OpenAI()
    ROOT.mkdir(parents=True, exist_ok=True)
    data=json.loads(SCREENED.read_text(encoding="utf-8"))
    sources=data["passed_sources"]

    compact=[]
    for i,s in enumerate(sources):
        compact.append({
            "source_number":i+1,
            "game":s.get("game"),
            "creator":s.get("channel"),
            "title":s.get("page_title"),
            "transcript":s.get("gate_transcript","")[:3000],
            "duration":probe_duration(s["local_path"])
        })

    prompt=f"""
Build a fast-moving ViralSpawnTV gaming episode using 6-10 of these approved clips.

CRITICAL EDITING RULE: there are NO narration cards and NO black/dark narration screens.
Gameplay must be moving continuously from the first frame of the episode.
Narration is spoken OVER gameplay. The source/game audio becomes prominent for the payoff.
Do not write long setups. Hook quickly.

For each selected source choose:
- start/end: total excerpt 18-40 sec
- setup: 1-3 concise sentences spoken over the opening gameplay
- payoff_start: seconds after the excerpt starts where narration should stop and the original
  clip should breathe. Prefer at least 6-15 seconds of source-only payoff.
- reaction: OPTIONAL very short reaction. Use empty string if the source moment is stronger alone.

The intro is 1-2 punchy sentences spoken over a rapid preview of the strongest first clip.
The outro is one short CTA spoken over the final gameplay, never a separate card.

Return ONLY JSON:
{{
 "title":"accurate clickable title",
 "description":"2-4 sentences",
 "thumbnail_text":"2-5 words",
 "intro":"short cold open",
 "segments":[
  {{"source_number":1,"start":0,"end":30,"setup":"short setup",
    "payoff_start":12,"reaction":"short optional reaction"}}
 ],
 "outro":"short CTA"
}}

SOURCES:
{json.dumps(compact,ensure_ascii=False)}
"""
    r=CLIENT.responses.create(model="gpt-5.6",input=prompt)
    raw=re.sub(r"^```json\s*|\s*```$","",r.output_text.strip())
    plan=json.loads(raw)

    bynum={x["source_number"]:x for x in compact}
    pieces=[]
    used=[]

    for idx,seg in enumerate(plan.get("segments",[])):
        try:
            n=int(seg["source_number"])
            info=bynum[n]
            source=sources[n-1]
            src=Path(source["local_path"])
            total=float(info["duration"])
            start=max(0.0,min(float(seg.get("start",0)),max(0,total-6)))
            end=max(start+6,min(float(seg.get("end",start+30)),total))
            relative_payoff=float(seg.get("payoff_start",10))
            payoff=max(start+4,min(start+relative_payoff,end-4))
        except Exception:
            continue

        # First clip carries the episode cold-open plus its setup over moving gameplay.
        setup=(seg.get("setup") or "").strip()
        if idx==0:
            setup=((plan.get("intro") or "").strip()+" "+setup).strip()

        # Part A: moving gameplay + narration.
        a=ROOT/f"v12_{idx:02d}_setup.mp4"
        make_gameplay_piece(src,start,payoff,setup,a,f"v12_{idx:02d}_setup.mp3")
        pieces.append(a)

        # Part B: source-only payoff. No narrator talking over the key moment.
        b=ROOT/f"v12_{idx:02d}_payoff.mp4"
        make_source_only(src,payoff,end,b)
        pieces.append(b)

        # Reactions are intentionally omitted as separate pieces because that would
        # recreate dead time. The next segment's setup supplies the transition.
        used.append(source)

    if len(used)<5:
        raise RuntimeError(f"Only {len(used)} usable clips in episode plan.")

    # Outro is over the final seconds of existing gameplay in spirit; V1.2 avoids
    # adding any standalone visual card. Metadata retains CTA for later overlay work.
    concat=ROOT/"concat_v1_2.txt"
    concat.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in pieces),encoding="utf-8")

    run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),
         "-c","copy","-movflags","+faststart",str(OUT)])

    duration=probe_duration(OUT)
    meta={
        "version":"1.2",
        "title":plan["title"][:100],
        "description":plan["description"],
        "thumbnail_text":plan["thumbnail_text"],
        "outro_text":plan.get("outro",""),
        "video_path":str(OUT),
        "duration_seconds":duration,
        "source_count":len(used),
        "continuous_gameplay":True,
        "narration_cards":False,
        "source_clips":[{
            "clip_id":s.get("clip_id"),"channel":s.get("channel"),
            "game":s.get("game"),"clip_url":s.get("clip_url"),
            "rights_status":"unverified"
        } for s in used],
        "rights_status":"unverified",
        "publish_status":"READY_FOR_UPLOAD"
    }
    OUTMETA.write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding="utf-8")

    if duration<150:
        raise RuntimeError(f"Episode only {duration/60:.1f} minutes; refusing upload.")

    print(f"LONG-FORM V1.2 CREATED: {duration/60:.2f} minutes")
    print("Continuous gameplay: YES")
    print("Narration cards: NONE")

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        print("LONGFORM V1.2 PRODUCTION FAILED:",e)
        sys.exit(1)
