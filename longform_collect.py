import json, re, subprocess, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

RANKED=Path("work/longform/ranked_sources.json")
OUT=Path("work/longform/sources")
META=Path("work/longform/acquired_sources.json")
TARGET=10
USER_AGENT=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36")

def playlist(url, clip_id):
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        page=b.new_page(user_agent=USER_AGENT)
        hits=[]
        page.on("request",lambda req: hits.append(req.url) if ".m3u8" in req.url else None)
        page.goto(url,wait_until="domcontentloaded",timeout=60000)
        page.wait_for_timeout(3000)
        html=page.content(); b.close()
    hits += re.findall(r'https?[^"\'\\\s]+?\.m3u8[^"\'\\\s<]*',html)
    clean=[]
    for x in hits:
        x=x.replace("\\u0026","&").replace("\\/","/").replace("&amp;","&")
        if x not in clean: clean.append(x)
    exact=[x for x in clean if clip_id in x]
    if not exact: raise RuntimeError("playlist not found")
    return exact[0]

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    data=json.loads(RANKED.read_text(encoding="utf-8"))
    acquired=[]
    for c in data["candidates"]:
        if len(acquired)>=TARGET: break
        try:
            p=playlist(c["clip_url"],c["clip_id"])
            dest=OUT/f"{len(acquired)+1:02d}_{c['clip_id']}.mp4"
            subprocess.run(["ffmpeg","-y","-user_agent",USER_AGENT,
                "-headers",f"Referer: {c['clip_url']}\r\nOrigin: https://kick.com\r\n",
                "-i",p,"-c","copy","-movflags","+faststart",str(dest)],
                check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if dest.stat().st_size<10000: raise RuntimeError("tiny download")
            acquired.append({**c,"local_path":str(dest),"rights_status":"unverified"})
            print("Acquired:",c.get("game"),c.get("channel"))
        except Exception as e:
            print("Skip:",c.get("clip_id"),e)

    if len(acquired)<5: raise RuntimeError(f"Only acquired {len(acquired)} clips.")
    META.write_text(json.dumps({"sources":acquired},indent=2,ensure_ascii=False),encoding="utf-8")
    print(f"Acquired {len(acquired)} long-form sources.")

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("LONGFORM COLLECTOR FAILED:",e); sys.exit(1)
