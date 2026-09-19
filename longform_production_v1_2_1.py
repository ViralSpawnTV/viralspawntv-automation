import json, re, subprocess, sys
from pathlib import Path
from openai import OpenAI

ROOT = Path("work/longform")
SCREENED = ROOT / "screened_sources.json"
OUT = ROOT / "ViralSpawnTV_Longform_V1_2_1.mp4"
OUTMETA = ROOT / "ViralSpawnTV_Longform_V1_2_1_metadata.json"
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
    text = (text or "").strip() or "Watch this."
    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice=VOICE,
        input=text,
        instructions=(
            "Young adult American male, neutral U.S. accent. Natural gaming commentary, "
            "medium-fast, crisp and conversational. Selective excitement. Never announcer-like."
        )
    ) as r:
        r.stream_to_file(path)

def video_filter():
    return (
        "[0:v]split=2[v1][v2];"
        "[v1]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,boxblur=20:10[bg];"
        "[v2]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )

def make_gameplay_piece(src, start, end, narration, dest, narration_name):
    narr = ROOT / narration_name
    tts(CLIENT, narration, narr)
    ndur = probe_duration(narr)
    total = probe_duration(src)
    dur = max(4.0, end-start, ndur+1.0)
    end = min(total, start+dur)
    dur = max(0.5, end-start)

    if has_audio(src):
        # FIX: sidechaincompress consumes both inputs, so split narrator audio first.
        # One copy drives the compressor sidechain; the other is mixed into final audio.
        fc = (
            video_filter() +
            ";[0:a]aresample=48000,volume=1.0[game];"
            "[1:a]aresample=48000,volume=1.35,asplit=2[voice_sc][voice_mix];"
            "[game][voice_sc]sidechaincompress="
            "threshold=0.02:ratio=10:attack=20:release=350[ducked];"
            "[ducked][voice_mix]amix=inputs=2:duration=first:"
            "dropout_transition=0[a]"
        )
        run([
            "ffmpeg","-y","-ss",str(start),"-t",str(dur),"-i",str(src),
            "-i",str(narr),"-filter_complex",fc,
            "-map","[v]","-map","[a]",
            "-r","30","-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
            "-c:a","aac","-ar","48000","-ac","2","-t",str(dur),str(dest)
        ])
    else:
        fc = video_filter() + ";[1:a]aresample=48000,volume=1.35,apad[a]"
        run([
            "ffmpeg","-y","-ss",str(start),"-t",str(dur),"-i",str(src),
            "-i",str(narr),"-filter_complex",fc,
            "-map","[v]","-map","[a]",
            "-r","30","-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
            "-c:a","aac","-ar","48000","-ac","2","-t",str(dur),str(dest)
        ])

def make_source_only(src, start, end, dest):
    dur=max(0.5,end-start)
    vf=video_filter()
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
Create a fast-moving ViralSpawnTV gaming compilation using 6-10 approved clips.

There must be NO black screens and NO narration cards.
Moving gameplay is visible continuously from frame one.
Narration is spoken over gameplay, then stops before the key payoff so original clip audio
can breathe. Keep setups concise.

For each segment choose an excerpt of roughly 20-40 seconds.
payoff_start is seconds after the selected excerpt begins and should leave roughly 7-18
seconds of source-only payoff whenever possible.

Return ONLY JSON:
{{
 "title":"clickable accurate title",
 "description":"2-4 sentences",
 "thumbnail_text":"2-5 words",
 "intro":"1-2 sentence cold open",
 "segments":[
  {{"source_number":1,"start":0,"end":30,"setup":"short setup","payoff_start":12}}
 ],
 "outro":"one short CTA"
}}

SOURCES:
{json.dumps(compact,ensure_ascii=False)}
"""
    resp=CLIENT.responses.create(model="gpt-5.6",input=prompt)
    raw=re.sub(r"^```json\s*|\s*```$","",resp.output_text.strip())
    plan=json.loads(raw)

    bynum={x["source_number"]:x for x in compact}
    pieces=[]
    used=[]

    for idx,seg in enumerate(plan.get("segments",[])):
        n=int(seg["source_number"])
        if n not in bynum: continue
        info=bynum[n]
        source=sources[n-1]
        src=Path(source["local_path"])
        total=float(info["duration"])
        start=max(0.0,min(float(seg.get("start",0)),max(0,total-8)))
        end=max(start+8,min(float(seg.get("end",start+30)),total))
        payoff=max(start+5,min(start+float(seg.get("payoff_start",12)),end-5))

        setup=(seg.get("setup") or "").strip()
        if idx==0:
            setup=((plan.get("intro") or "").strip()+" "+setup).strip()

        a=ROOT/f"v121_{idx:02d}_setup.mp4"
        make_gameplay_piece(src,start,payoff,setup,a,f"v121_{idx:02d}_setup.mp3")
        pieces.append(a)

        b=ROOT/f"v121_{idx:02d}_payoff.mp4"
        make_source_only(src,payoff,end,b)
        pieces.append(b)
        used.append(source)

    if len(used)<5:
        raise RuntimeError(f"Only {len(used)} usable planned clips.")

    concat=ROOT/"concat_v1_2_1.txt"
    concat.write_text("\n".join(f"file '{x.resolve().as_posix()}'" for x in pieces),encoding="utf-8")

    # Re-encode final concat for resilience instead of stream-copying.
    run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),
         "-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p","-r","30",
         "-c:a","aac","-ar","48000","-ac","2","-movflags","+faststart",str(OUT)])

    duration=probe_duration(OUT)
    meta={
        "version":"1.2.1",
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
    print(f"LONG-FORM V1.2.1 CREATED: {duration/60:.2f} minutes")
    print("Continuous gameplay: YES | Narration cards: NONE")

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("LONGFORM V1.2.1 PRODUCTION FAILED:",e)
        sys.exit(1)
