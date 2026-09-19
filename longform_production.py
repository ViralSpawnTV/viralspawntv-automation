import base64, json, subprocess, sys, tempfile
from pathlib import Path
from openai import OpenAI

ROOT=Path("work/longform")
META=ROOT/"acquired_sources.json"
OUT=ROOT/"ViralSpawnTV_Longform_V1.mp4"
OUTMETA=ROOT/"ViralSpawnTV_Longform_V1_metadata.json"
VOICE="onyx"

def run(cmd):
    subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def duration(path):
    x=subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration",
                               "-of","default=nw=1:nk=1",str(path)],text=True)
    return float(x.strip())

def transcript(client,path):
    wav=ROOT/(Path(path).stem+".wav")
    run(["ffmpeg","-y","-i",path,"-vn","-ac","1","-ar","16000",str(wav)])
    with open(wav,"rb") as f:
        t=client.audio.transcriptions.create(model="whisper-1",file=f)
    wav.unlink(missing_ok=True)
    return getattr(t,"text",str(t))

def frame_b64(path):
    jpg=ROOT/(Path(path).stem+".jpg")
    run(["ffmpeg","-y","-ss","2","-i",path,"-frames:v","1","-q:v","3",str(jpg)])
    b=base64.b64encode(jpg.read_bytes()).decode()
    jpg.unlink(missing_ok=True)
    return b

def tts(client,text,path):
    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",voice=VOICE,input=text,
        instructions=("Young adult American male. Neutral U.S. accent. "
                      "Conversational gaming commentary, medium-fast, crisp and natural. "
                      "Use excitement selectively; never sound like an announcer.")
    ) as r:
        r.stream_to_file(path)

def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    client=OpenAI()
    data=json.loads(META.read_text(encoding="utf-8"))
    sources=data["sources"][:10]
    analyzed=[]

    for i,s in enumerate(sources):
        p=s["local_path"]
        tr=transcript(client,p)
        analyzed.append({**s,"transcript":tr,"duration":duration(p)})
        print(f"Analyzed {i+1}/{len(sources)}")

    # Build an original episode script around the source moments.
    compact=[{"n":i+1,"game":s.get("game"),"creator":s.get("channel"),
              "title":s.get("page_title"),"transcript":s["transcript"][:1800],
              "duration":s["duration"]} for i,s in enumerate(analyzed)]

    prompt=f"""
Write an original ViralSpawnTV gaming compilation episode using these source moments.
Target finished length: about 8-10 minutes if source material permits.

Requirements:
- Strong 12-20 second cold open.
- Original narrator is the backbone, not a clip dump.
- Give context before each moment and a short reaction/transition after it.
- Do not claim facts not supported by supplied metadata/transcripts.
- Translate non-English dialogue naturally when useful.
- Keep commentary punchy, American-English, YouTube-gaming style.
- No gambling promotion.
- End with a brief CTA.
- Create a clickable but accurate title, description, and thumbnail text (2-5 words).
- For each clip, choose at most 35 seconds. Prefer 15-30 seconds.
Return ONLY JSON:
{{
 "title":"...",
 "description":"...",
 "thumbnail_text":"...",
 "intro":"...",
 "segments":[
   {{"source_number":1,"start":0,"end":25,"before":"narration before","after":"short reaction"}}
 ],
 "outro":"..."
}}
Use 7-10 of the strongest supplied clips. Arrange for escalation and variety.

SOURCES:
{json.dumps(compact,ensure_ascii=False)}
"""
    r=client.responses.create(model="gpt-5.6",input=prompt)
    raw=r.output_text.strip().removeprefix("```json").removesuffix("```").strip()
    plan=json.loads(raw)

    pieces=[]
    # Intro
    intro_mp3=ROOT/"narr_intro.mp3"; tts(client,plan["intro"],intro_mp3)
    intro_mp4=ROOT/"intro.mp4"
    run(["ffmpeg","-y","-f","lavfi","-i","color=c=0x080b12:s=1920x1080:r=30",
         "-i",str(intro_mp3),"-vf",
         "drawtext=text='VIRALSPAWNTV':fontcolor=white:fontsize=82:x=(w-text_w)/2:y=(h-text_h)/2",
         "-c:v","libx264","-preset","veryfast","-c:a","aac","-shortest",str(intro_mp4)])
    pieces.append(intro_mp4)

    bynum={i+1:s for i,s in enumerate(analyzed)}
    used=[]
    for idx,seg in enumerate(plan["segments"]):
        n=int(seg["source_number"])
        if n not in bynum: continue
        s=bynum[n]; src=s["local_path"]
        st=max(0,float(seg.get("start",0)))
        en=min(float(seg.get("end",st+25)),s["duration"])
        if en-st<4: continue

        before=ROOT/f"before_{idx}.mp3"; tts(client,seg["before"],before)
        card=ROOT/f"before_{idx}.mp4"
        game=(s.get("game") or "GAMING").replace("'","")[:30]
        run(["ffmpeg","-y","-f","lavfi","-i","color=c=0x080b12:s=1920x1080:r=30",
             "-i",str(before),"-vf",
             f"drawtext=text='{game}':fontcolor=white:fontsize=72:x=(w-text_w)/2:y=(h-text_h)/2",
             "-c:v","libx264","-preset","veryfast","-c:a","aac","-shortest",str(card)])
        pieces.append(card)

        clip=ROOT/f"clip_{idx}.mp4"
        # Normalize every source to 1080p 16:9 with blurred background + centered foreground.
        fc=("[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
            "boxblur=20:10[bg];"
            "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]")
        run(["ffmpeg","-y","-ss",str(st),"-to",str(en),"-i",src,
             "-filter_complex",fc,"-map","[v]","-map","0:a?",
             "-r","30","-c:v","libx264","-preset","veryfast","-c:a","aac",
             "-ar","48000","-ac","2",str(clip)])
        pieces.append(clip)

        after_text=seg.get("after","").strip()
        if after_text:
            after=ROOT/f"after_{idx}.mp3"; tts(client,after_text,after)
            react=ROOT/f"after_{idx}.mp4"
            run(["ffmpeg","-y","-f","lavfi","-i","color=c=0x080b12:s=1920x1080:r=30",
                 "-i",str(after),"-c:v","libx264","-preset","veryfast",
                 "-c:a","aac","-shortest",str(react)])
            pieces.append(react)
        used.append(s)

    outro_mp3=ROOT/"narr_outro.mp3"; tts(client,plan["outro"],outro_mp3)
    outro_mp4=ROOT/"outro.mp4"
    run(["ffmpeg","-y","-f","lavfi","-i","color=c=0x080b12:s=1920x1080:r=30",
         "-i",str(outro_mp3),"-vf",
         "drawtext=text='VIRALSPAWNTV':fontcolor=white:fontsize=82:x=(w-text_w)/2:y=(h-text_h)/2",
         "-c:v","libx264","-preset","veryfast","-c:a","aac","-shortest",str(outro_mp4)])
    pieces.append(outro_mp4)

    concat=ROOT/"concat.txt"
    concat.write_text("\n".join("file '"+str(p.resolve()).replace("'","'\\''")+"'" for p in pieces),
                      encoding="utf-8")
    run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),
         "-c:v","libx264","-preset","veryfast","-c:a","aac","-movflags","+faststart",str(OUT)])

    metadata={
        "version":1,"title":plan["title"][:100],"description":plan["description"],
        "thumbnail_text":plan["thumbnail_text"],"video_path":str(OUT),
        "duration_seconds":duration(OUT),
        "source_clips":[{"clip_id":s.get("clip_id"),"channel":s.get("channel"),
                         "game":s.get("game"),"clip_url":s.get("clip_url"),
                         "rights_status":"unverified"} for s in used],
        "rights_status":"unverified"
    }
    OUTMETA.write_text(json.dumps(metadata,indent=2,ensure_ascii=False),encoding="utf-8")
    print("LONG-FORM V1 CREATED:",OUT)
    print("Duration:",round(metadata["duration_seconds"]/60,2),"minutes")

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("LONGFORM PRODUCTION FAILED:",e); sys.exit(1)
