"""Free temporal shortlist. Scores are search hints, never production approval."""
import hashlib
import math
import subprocess
from pathlib import Path
import numpy as np
from PIL import Image
import local_combat_screen as local

PROMPTS=[
 'first person shooter game screenshot, a gun firing, bright muzzle flash from the gun barrel',
 'video game screenshot, player firing an automatic rifle with a visible muzzle flash',
 'shooter game screenshot, gun firing through a scope at a target',
 'first person shooter game screenshot, player holding an idle gun without firing',
 'first person shooter game screenshot, player reloading a gun behind cover',
 'video game screenshot, player running with a knife or using an ability without gunfire',
 'video game screenshot, inventory, menu, scoreboard or death screen',
 'webcam photo of a streamer talking, no shooter gameplay',
 'screenshot of a mobile strategy game without firearms']

def run(command,timeout=90):
    return subprocess.run(command,check=True,capture_output=True,timeout=timeout)

def duration(path):
    value=float(run(['ffprobe','-v','error','-show_entries','format=duration',
        '-of','default=noprint_wrappers=1:nokey=1',str(path)],20).stdout)
    if not math.isfinite(value) or value<1:raise ValueError('Invalid decoded duration')
    return value

def score(model,images):
    inp=model.tokenizer(PROMPTS,padding='max_length',truncation=True,max_length=77,return_tensors='np')
    text=model.output(model.text,dict(inp),'text_embeds')
    text=text/np.maximum(np.linalg.norm(text,axis=-1,keepdims=True),1e-8)
    values=[]
    for start in range(0,len(images),8):
        pixels=model.processor(images=images[start:start+8],return_tensors='np')['pixel_values']
        vision=model.output(model.vision,{'pixel_values':pixels},'image_embeds')
        vision=vision/np.maximum(np.linalg.norm(vision,axis=-1,keepdims=True),1e-8)
        sims=vision@text.T;values.extend((sims[:,:3].max(axis=1)-sims[:,3:].max(axis=1)).tolist())
    return values

def windows(gameplay,firing,audio,seconds):
    if len(gameplay)!=len(firing) or not np.isfinite(gameplay+firing).all():
        raise ValueError('Invalid temporal visual evidence')
    if not math.isfinite(seconds) or any(not math.isfinite(b['score']) for b in audio):
        raise ValueError('Invalid temporal audio evidence')
    active=[g>=.01 and f>=.005 and any(b['start']<=i<b['end'] and b['score']>=.08 for b in audio)
            for i,(g,f) in enumerate(zip(gameplay,firing))]
    choices=[]
    for length in (19,22,25,30,35):
        for start in range(max(0,int(seconds-length)+1)):
            flags=active[start:start+length]
            if len(flags)!=length:continue
            positions=[i for i,a in enumerate(flags) if a]
            if not positions:continue
            first=positions[0];tail=length-positions[-1]-1
            gap=longest=0
            for a in flags:gap=0 if a else gap+1;longest=max(longest,gap)
            density=sum(flags)/length
            game_ratio=sum(x>=.01 for x in gameplay[start:start+length])/length
            gun_ratio=sum(any(b['start']<=i<b['end'] and b['score']>=.08 for b in audio)
                          for i in range(start,start+length))/length
            # Action at the start and end is mandatory BEFORE any paid request.
            if first>2 or tail>2 or longest>5 or density<.4 or game_ratio<.7 or gun_ratio<.5:
                continue
            if positions[-1]-positions[0]<8:continue
            rank=(density,-first,-longest,-tail,gun_ratio,-length,-start)
            choices.append((rank,dict(start=float(start),seconds=float(length),
                active_ratio=density,gameplay_ratio=game_ratio,gunfire_ratio=gun_ratio,
                first_action_hint_seconds=first,last_action_tail_seconds=tail,longest_gap_seconds=longest)))
    choices.sort(key=lambda x:x[0],reverse=True)
    selected=[]
    for _,w in choices:
        if all(max(0,min(w['start']+w['seconds'],s['start']+s['seconds'])-max(w['start'],s['start']))
               /min(w['seconds'],s['seconds'])<.5 for s in selected):
            selected.append(w)
        if len(selected)==2:break
    return selected

def inspect(row,folder,max_seconds=300,offset=0.):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);preview=folder/'scan.mp4'
    url=row.get('prevalidated_media_url') or row.get('media_url') or row.get('playlist_url')
    if not url:raise ValueError('Missing media URL')
    run(['ffmpeg','-v','error','-y','-rw_timeout','12000000','-ss',str(offset),'-i',str(url),'-t',str(max_seconds),
         '-map','0:v:0','-map','0:a:0?','-vf','scale=480:-2','-r','8','-c:v','libx264',
         '-preset','ultrafast','-crf','30','-c:a','aac',str(preview)],150)
    seconds=min(max_seconds,duration(preview))
    run(['ffmpeg','-v','error','-y','-i',str(preview),'-vf','fps=1','-q:v','5',str(folder/'sample_%04d.jpg')],60)
    images=[]
    for p in sorted(folder.glob('sample_*.jpg')):
        with Image.open(p) as im:images.append(im.convert('RGB'))
    raw=run(['ffmpeg','-v','error','-i',str(preview),'-vn','-ac','1','-ar','16000','-f','f32le','-'],60).stdout
    signal=np.frombuffer(raw,dtype='<f4');seconds=min(seconds,len(signal)/16000,len(images))
    if seconds<19:raise ValueError('Incomplete video/audio or too short')
    images=images[:int(math.ceil(seconds))]
    crops=[]
    for im in images:
        w,h=im.size;crops.append(im.crop((int(w*.18),int(h*.12),int(w*.95),int(h*.78))))
    model=local.engine()
    with model.lock:
        # Coarse audio is a shortlist hint; the exact saved window gets dense visual review.
        audio=model.audio_scores(signal)
        full_g=model.image_margins(images,'shooter');full_f=score(model,images)
        crop_g=model.image_margins(crops,'shooter');crop_f=score(model,crops)
    choices=[]
    for name,g,f in [('full',full_g,full_f),('central',crop_g,crop_f)]:
        for win in windows(g,f,audio,seconds):choices.append(dict(win,scoring_crop=name))
    choices.sort(key=lambda w:(w['active_ratio'],-w['first_action_hint_seconds'],
                 -w['longest_gap_seconds'],-w['last_action_tail_seconds']),reverse=True)
    dedup=[]
    for w in choices:
        if all(abs(w['start']-s['start'])>=8 for s in dedup):dedup.append(w)
        if len(dedup)==2:break
    known=float(row.get('prevalidated_duration_seconds') or row.get('source_duration_seconds') or max_seconds+1)
    for w in dedup:w['start']+=offset
    return dedup,dict(scanned_seconds=seconds,scan_offset=offset,scanned_through_seconds=offset+seconds,
                      next_scan_offset=offset+max(0,seconds-35),source_fully_scanned=known<=offset+seconds+1,
                      local_model_revisions={k:v[1] for k,v in local.REPOS.items()})

def save_window(row,win,path):
    url=row.get('prevalidated_media_url') or row.get('media_url') or row.get('playlist_url')
    # Accurate encoding avoids shifting the verified interval to an earlier keyframe.
    run(['ffmpeg','-v','error','-y','-rw_timeout','12000000','-ss',str(win['start']),'-i',str(url),
         '-t',str(win['seconds']),'-map','0:v:0','-map','0:a:0?',
         '-vf',"scale=w='min(1280,iw)':h=-2",'-r','30','-c:v','libx264','-preset','veryfast',
         '-crf','21','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-movflags','+faststart',str(path)],120)
    actual=duration(path)
    if abs(actual-win['seconds'])>.2 or actual<19:raise ValueError('Saved action window duration mismatch')
    # Catch corrupt/undecodable media before paying to review it.
    run(['ffmpeg','-v','error','-i',str(path),'-f','null','-'],60)
    return actual

def window_key(clip_id,win):
    return hashlib.sha256(f"{clip_id}|{win['start']:.3f}|{win['seconds']:.3f}".encode()).hexdigest()[:24]
