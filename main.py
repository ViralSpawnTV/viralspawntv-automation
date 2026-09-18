import os, json, hashlib, subprocess, pathlib, sys, requests
from datetime import datetime, timezone
from openai import OpenAI

ROOT=pathlib.Path(__file__).resolve().parents[1]
WORK=ROOT/'work'; OUT=ROOT/'output'; DATA=ROOT/'data'
WORK.mkdir(exist_ok=True); OUT.mkdir(exist_ok=True)

def load_config():
    p=ROOT/'config.json'
    if not p.exists():
        raise SystemExit('Missing config.json. Copy config.example.json to config.json and add authorized sources.')
    return json.loads(p.read_text())

def history():
    p=DATA/'history.json'; return json.loads(p.read_text()) if p.exists() else {'processed':[]}

def save_history(h): (DATA/'history.json').write_text(json.dumps(h,indent=2))

def discover(cfg):
    """Conservative discovery: only configured feeds/endpoints marked reuse_authorized.
    Extend this adapter for creator APIs you have permission to republish from."""
    candidates=[]
    for s in cfg.get('sources',[]):
        if not s.get('reuse_authorized'): continue
        if s.get('type')=='json':
            try:
                payload=requests.get(s['url'],timeout=20).json()
                for x in payload.get('clips',[]):
                    if x.get('download_url'):
                        candidates.append({**x,'creator':s.get('creator','unknown'),'authorized':True})
            except Exception as e: print('source error',e)
    return candidates

def choose(candidates,h):
    seen=set(h['processed'])
    fresh=[c for c in candidates if hashlib.sha256(c['download_url'].encode()).hexdigest() not in seen]
    return fresh[0] if fresh else None

def write_package(client, clip):
    prompt=f'''Create a transformative YouTube Short commentary package about this authorized streamer clip.\nCreator: {clip.get('creator')}\nTitle/context: {clip.get('title','')}\nReturn strict JSON with keys hook, narration, title, description. Narration 35-65 words, neutral American creator style, add context rather than merely describing the footage. No invented facts.'''
    r=client.responses.create(model=os.getenv('OPENAI_MODEL','gpt-5.6'),input=prompt)
    txt=r.output_text.strip();
    if txt.startswith('```'): txt=txt.split('\n',1)[1].rsplit('```',1)[0]
    return json.loads(txt)

def download(url,path):
    with requests.get(url,stream=True,timeout=60) as r:
        r.raise_for_status()
        with open(path,'wb') as f:
            for b in r.iter_content(1024*1024): f.write(b)

def voice(client,text,path):
    with client.audio.speech.with_streaming_response.create(model=os.getenv('TTS_MODEL','gpt-4o-mini-tts'),voice=os.getenv('TTS_VOICE','alloy'),input=text,instructions='Young adult American male. Neutral US accent. Conversational streamer commentary. Medium-fast, crisp, natural, not announcer-like.') as response:
        response.stream_to_file(path)

def render(src,vo,out,hook):
    # Vertical 1080x1920; source audio ducked beneath narration; hook text burned in.
    safe=hook.replace("'","’").replace(':','\\:')
    filt=("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
          f"drawtext=text='{safe}':x=(w-text_w)/2:y=180:fontsize=64:fontcolor=white:borderw=5:bordercolor=black[v];"
          "[0:a]volume=0.28[a0];[1:a]volume=1.0[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[a]")
    subprocess.run(['ffmpeg','-y','-i',str(src),'-i',str(vo),'-filter_complex',filt,'-map','[v]','-map','[a]','-c:v','libx264','-preset','medium','-crf','20','-c:a','aac','-b:a','192k','-t','60',str(out)],check=True)

def youtube_upload(video,meta,cfg):
    # OAuth token must be provisioned outside source control as YOUTUBE_TOKEN_JSON.
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    token=json.loads(os.environ['YOUTUBE_TOKEN_JSON'])
    creds=Credentials.from_authorized_user_info(token, ['https://www.googleapis.com/auth/youtube.upload'])
    yt=build('youtube','v3',credentials=creds)
    body={'snippet':{'title':meta['title'][:100],'description':meta['description'],'categoryId':'20'},'status':{'privacyStatus':cfg.get('publish_mode','private'),'selfDeclaredMadeForKids':False}}
    req=yt.videos().insert(part='snippet,status',body=body,media_body=MediaFileUpload(str(video),chunksize=-1,resumable=True))
    return req.execute()

def main():
    cfg=load_config(); h=history(); c=choose(discover(cfg),h)
    if not c: print('No new authorized clip found.'); return
    client=OpenAI(); meta=write_package(client,c)
    key=hashlib.sha256(c['download_url'].encode()).hexdigest(); src=WORK/f'{key}.mp4'; vo=WORK/f'{key}.mp3'; out=OUT/f'{key}.mp4'
    download(c['download_url'],src); voice(client,meta['narration'],vo); render(src,vo,out,meta['hook'])
    if os.getenv('AUTO_UPLOAD','false').lower()=='true': print(youtube_upload(out,meta,cfg))
    else: print('Rendered for review:',out,meta)
    h['processed'].append(key); save_history(h)
if __name__=='__main__': main()
