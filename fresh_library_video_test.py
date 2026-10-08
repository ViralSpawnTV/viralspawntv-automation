"""Fresh temporal screen experiment; no API calls or production approvals."""
import argparse
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import local_combat_screen as local
from clip_library import blocked_ids

OUT = Path('work/fresh_library_test')
EXCLUDED_REVIEW_IDS = frozenset(['clip_01M4D61YQW87ZT27B41D52PV9W', 'clip_01M4D4DZS7J4Y5422740EHXTYE', 'clip_01M45NQ8SWJV9C6CYEAW32HQ53', 'clip_01M4DDZKBR1RSSGTAPCTG8VBR3', 'clip_01M41G53KXN8XA57M55VD4EQT9', 'clip_01M2SB5M286AQB18S13NPC8Z0X', 'clip_01M4D7S0G4KKCMR8J74C6ZCE18', 'clip_01M4DENBQYSAM02ZC9PDKSFMZT', 'clip_01M4DEPGPMR420EJ80QXJAZHJD', 'clip_01M4D92VBP2QY3YTHCKVQWCTPD', 'clip_01M4DBDHH87KWVB576NKH2JP6W', 'clip_01M4DADC5GVAJKMZ36Y9HFVX91', 'clip_01M4D8P6Z1CKNCN2G2HQGHQ612', 'clip_01M4D6Q2V02C1N3S6APV5PGNKD', 'clip_01M4DEGXQVN18YMSXCRT6ZJKYM', 'clip_01M4DECNEA5KKXR9A32PHWZKP1', 'clip_01M4DAEAZRH8JQ0MCFXDRB7T6E', 'clip_01M4DB3KDJ776A48V92SJ7Y9JQ', 'clip_01M4CMRTGWWA5S11ZHAHCBVM0V', 'clip_01M4D083BP2MTS4RBSN5EC3WQF', 'clip_01M4D6VRRP4J6JT1QC6QXY6WK1', 'clip_01M4DEAARM2YS35BWP1CJCEQMG', 'clip_01M4DDF62A8CYDATG23JHECWP2', 'clip_01M4D8R5W1Y02F4YT03K5F06CM', 'clip_01M4DB98FKBXSZT3HXN1N4TMC0'])
FRAME_FPS = 1.0
FIRING_MARGIN = 0.005
GAMEPLAY_MARGIN = 0.01
GUNFIRE_MIN = 0.08
MIN_ACTIVE_RATIO = 0.35
MIN_ACTIVE_BINS = 3
MIN_ACTIVE_SPAN = 8.0
MAX_VISUAL_GAP = 8.0
LENGTHS = (19.0, 25.0, 35.0)
CROPS = {'full': (0,0,1,1), 'central_gameplay': (.18,.12,.95,.78)}
PROMPTS = [
 'first person shooter game screenshot, a gun firing, bright muzzle flash from the gun barrel',
 'video game screenshot, player firing an automatic rifle with a visible muzzle flash',
 'shooter game screenshot, gun firing through a scope at a target',
 'first person shooter game screenshot, player holding an idle gun without firing',
 'first person shooter game screenshot, player reloading a gun behind cover',
 'video game screenshot, player running with a knife or using an ability without gunfire',
 'video game screenshot, inventory, menu, scoreboard or death screen',
 'webcam photo of a streamer talking, no shooter gameplay',
 'screenshot of a mobile strategy game without firearms',
]


def choose_fresh(rows, blocked, limit):
    groups = {}; seen = set()
    for r in rows:
        cid = r.get('clip_id')
        if not isinstance(cid,str) or cid in seen or cid in EXCLUDED_REVIEW_IDS or cid in blocked:
            continue
        seen.add(cid)
        if not (r.get('prevalidated_media_url') or r.get('media_url') or r.get('playlist_url')):
            continue
        groups.setdefault(r.get('game','unknown'),[]).append(r)
    chosen = []
    while len(chosen)<limit and any(groups.values()):
        for group in groups.values():
            if group and len(chosen)<limit: chosen.append(group.pop(0))
    return chosen


def window_evidence(gameplay, firing, audio, duration):
    if len(gameplay)!=len(firing): raise ValueError('Mismatched frame evidence')
    if not np.isfinite(gameplay+firing).all() or not math.isfinite(duration):
        raise ValueError('Non-finite frame evidence')
    if any(not math.isfinite(b['score']) for b in audio): raise ValueError('Non-finite audio evidence')
    choices = []
    for seconds in LENGTHS:
        if duration<seconds: continue
        starts = list(np.arange(0,duration-seconds+.001,2.0))+[duration-seconds]
        for start in sorted(set(starts)):
            end = start+seconds
            samples = [(i/FRAME_FPS,g,f) for i,(g,f) in enumerate(zip(gameplay,firing))
                       if start<=i/FRAME_FPS<end]
            bins = [b for b in audio if b['start']>=start and b['end']<=end]
            if len(samples)<15 or len(bins)<3: continue
            active=[]; active_bins=set()
            for t,g,f in samples:
                matches=[(i,b) for i,b in enumerate(audio)
                         if b['start']<=t<b['end'] and b['score']>=GUNFIRE_MIN]
                if g>=GAMEPLAY_MARGIN and f>=FIRING_MARGIN and matches:
                    active.append(t); active_bins.update(i for i,b in matches)
            ratio=len(active)/len(samples)
            gameplay_ratio=sum(g>=GAMEPLAY_MARGIN for t,g,f in samples)/len(samples)
            gun_hits=sum(b['score']>=GUNFIRE_MIN for b in bins)
            gun_ratio=gun_hits/len(bins)
            span=active[-1]-active[0] if active else 0.0
            # Subtract one sampled interval between hits: estimate, not continuous proof.
            gap=max([active[0]-start,end-active[-1]]+
                    [max(0,b-a-1/FRAME_FPS) for a,b in zip(active,active[1:])]) if active else seconds
            passed=bool(gameplay_ratio>=.70 and gun_hits>=3 and gun_ratio>=.50
                        and ratio>=MIN_ACTIVE_RATIO and len(active_bins)>=MIN_ACTIVE_BINS
                        and span>=MIN_ACTIVE_SPAN and gap<=MAX_VISUAL_GAP)
            strength=100*(.4*ratio+.3*gameplay_ratio+.3*gun_ratio)
            evidence=dict(start=round(float(start),3),seconds=seconds,end=round(float(end),3),
              score=round(strength,3),passed=passed,gameplay_ratio=round(gameplay_ratio,3),
              aligned_firing_ratio=round(ratio,3),aligned_firing_frames=len(active),
              distinct_firing_audio_bins=len(active_bins),firing_span_seconds=round(float(span),3),
              estimated_longest_visual_gap=round(float(gap),3),
              gunfire_bins=gun_hits,audio_bins=len(bins),gunfire_ratio=round(gun_ratio,3),
              aligned_firing_source_seconds=[round(float(t),3) for t in active])
            choices.append((passed,strength,ratio,-gap,-seconds,-start,evidence))
    if not choices: return dict(passed=False,reason='Insufficient aligned audio/video evidence')
    return max(choices,key=lambda x:x[:6])[-1]


def run(cmd, timeout=60):
    return subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)

def export_window(preview, evidence, folder):
    if 'start' not in evidence: return None
    folder.mkdir(parents=True, exist_ok=True)
    start, seconds = evidence['start'], evidence['seconds']
    clip = folder/'window.mp4'
    run(['ffmpeg','-v','error','-y','-ss',str(start),'-i',str(preview),'-t',str(seconds),
         '-map','0:v:0','-map','0:a:0?','-c:v','libx264','-preset','fast','-crf','24',
         '-c:a','aac','-movflags','+faststart',str(clip)])
    sheet = Image.new('RGB', (960, 4*204), '#151515')
    draw = ImageDraw.Draw(sheet)
    timestamps = []
    for i in range(12):
        offset = seconds*(0.04 + 0.92*i/11)
        image_path = folder/f'frame_{i+1:02}.jpg'
        run(['ffmpeg','-v','error','-y','-ss',str(offset),'-i',str(clip),
             '-frames:v','1',str(image_path)], timeout=20)
        with Image.open(image_path) as image:
            thumb = image.convert('RGB'); thumb.thumbnail((320,180))
            x,y = (i%3)*320,(i//3)*204
            sheet.paste(thumb,(x+(320-thumb.width)//2,y))
        draw.text((x+5,y+183), f'source {start+offset:.2f}s | window +{offset:.2f}s', fill='white')
        timestamps.append(round(start+offset,3))
    sheet.save(folder/'contact_sheet.jpg', quality=90)
    (folder/'timestamps.json').write_text(json.dumps({'source_start':start,
        'seconds':seconds,'source_sample_seconds':timestamps},indent=2)+'\n')
    return {'video':str(clip.relative_to(OUT)),
            'contact_sheet':str((folder/'contact_sheet.jpg').relative_to(OUT))}

def score(model, images, prompts):
    inp = model.tokenizer(prompts,padding='max_length',truncation=True,max_length=77,return_tensors='np')
    text = model.output(model.text,dict(inp),'text_embeds')
    text = text/np.maximum(np.linalg.norm(text,axis=-1,keepdims=True),1e-8)
    margins = []
    for start in range(0,len(images),8):
        pixels = model.processor(images=images[start:start+8],return_tensors='np')['pixel_values']
        visual = model.output(model.vision,{'pixel_values':pixels},'image_embeds')
        visual = visual/np.maximum(np.linalg.norm(visual,axis=-1,keepdims=True),1e-8)
        sims = visual@text.T
        margins.extend((sims[:,:3].max(axis=1)-sims[:,3:].max(axis=1)).tolist())
    if not np.isfinite(margins).all(): raise ValueError('Non-finite model scores')
    return margins


def inspect(row, model, index):
    url=row.get('prevalidated_media_url') or row.get('media_url') or row.get('playlist_url')
    with tempfile.TemporaryDirectory(prefix='viralspawntv-fresh-') as td:
        td=Path(td);preview=td/'preview.mp4'
        run(['ffmpeg','-v','error','-y','-rw_timeout','12000000','-i',str(url),'-t','120',
          '-map','0:v:0','-map','0:a:0?','-vf','scale=640:-2','-r','8',
          '-c:v','libx264','-preset','ultrafast','-crf','28','-c:a','aac',str(preview)],120)
        probe=run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(preview)])
        duration=min(120,float(json.loads(probe.stdout)['format']['duration']))
        known=float(row.get('source_duration_seconds') or row.get('prevalidated_duration_seconds') or duration)
        duration=min(duration,known)
        if not math.isfinite(duration) or duration<19: raise ValueError('Insufficient source duration')
        images=[];times=list(np.arange(0,duration,1/FRAME_FPS))
        for i,t in enumerate(times):
            p=td/f'sample_{i:03}.jpg'
            run(['ffmpeg','-v','error','-y','-ss',str(t),'-i',str(preview),'-frames:v','1',str(p)],20)
            with Image.open(p) as im:images.append(im.convert('RGB'))
        raw=run(['ffmpeg','-v','error','-i',str(preview),'-vn','-ac','1',
                 '-ar',str(local.AUDIO_RATE),'-f','f32le','-']).stdout
        audio_samples=np.frombuffer(raw,dtype='<f4')
        duration=min(duration,len(audio_samples)/local.AUDIO_RATE)
        if duration<19: raise ValueError('Incomplete source audio')
        with model.lock:audio=model.audio_scores(audio_samples)
        configs={};folder=OUT/f'clip_{index:02}'
        for name,bounds in CROPS.items():
            cropped=[]
            for image in images:
                w,h=image.size
                cropped.append(image.crop(tuple(round(v*s) for v,s in zip(bounds,(w,h,w,h)))))
            with model.lock:
                gameplay=model.image_margins(cropped,'shooter')
                firing=score(model,cropped,PROMPTS)
            evidence=window_evidence(gameplay,firing,audio,duration)
            exports=export_window(preview,evidence,folder/name)
            # Keep both uncropped windows for honest human comparison, plus a crop preview.
            if exports:
                sheet=Image.new('RGB',(960,4*204),'#151515');draw=ImageDraw.Draw(sheet)
                for i in range(12):
                    path=folder/name/f'frame_{i+1:02}.jpg'
                    with Image.open(path) as image:
                        w,h=image.size;cropped_frame=image.crop(tuple(round(v*s) for v,s in zip(bounds,(w,h,w,h))))
                        cropped_frame.thumbnail((320,180));x=(i%3)*320;y=(i//3)*204
                        sheet.paste(cropped_frame,(x+(320-cropped_frame.width)//2,y))
                    draw.text((x+4,y+183),name,fill='white')
                sheet.save(folder/name/'scoring_crop_sheet.jpg',quality=90)
            configs[name]=dict(evidence=evidence,exports=exports,
              frame_scores=[dict(source_seconds=round(float(t),3),gameplay_margin=round(float(g),5),
                                firing_margin=round(float(f),5)) for t,g,f in zip(times,gameplay,firing)])
        result=dict(clip_id=row['clip_id'],clip_url=row.get('clip_url'),listed_game=row.get('game'),
                    listed_game_verified=False,approved_for_production=False,
                    screening_scope='Repeated sampled visual firing aligned with coarse gunfire tags',
                    configs=configs,audio_bins=audio,source_duration_reviewed=duration)
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'evidence.json').write_text(json.dumps(result,indent=2)+'\n')
        return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--limit',type=int,default=25)
    parser.add_argument('--minutes',type=int,default=60)
    args=parser.parse_args()
    if not 1<=args.limit<=25 or not 3<=args.minutes<=120:
        parser.error('limit must be 1-25; minutes must be 3-120')
    OUT.mkdir(parents=True,exist_ok=True)
    report=dict(mode='fresh unlabelled temporal experiment',openai_calls=0,
      approved_for_production=False,excluded_previous_review_ids=sorted(EXCLUDED_REVIEW_IDS),
      thresholds=dict(firing_margin=FIRING_MARGIN,gameplay_margin=GAMEPLAY_MARGIN,
       gunfire_score=GUNFIRE_MIN,minimum_aligned_ratio=MIN_ACTIVE_RATIO,
       minimum_distinct_audio_bins=MIN_ACTIVE_BINS,minimum_span_seconds=MIN_ACTIVE_SPAN,
       maximum_estimated_visual_gap=MAX_VISUAL_GAP,frame_fps=FRAME_FPS),
      crops=CROPS,model_revisions={k:v[1] for k,v in local.REPOS.items()},
      results=[],errors=[],limitations=[
        'Fixed experimental settings; no threshold optimization on this batch',
        'This is NOT an untouched holdout: the prior calibration results informed the experiment',
        'Fresh source clips are unlabelled, so pass counts are not accuracy measurements',
        'CLIP firing comparisons and five-second audio tags are imperfect and can miss combat',
        'No certified enemy detection, shot synchronization, ammo OCR or hit-marker detection',
        'Only the first 120 seconds of each source are reviewed; production histories untouched'])
    def save():
        report['completed']=len(report['results'])
        report['pass_counts']={name:sum(r['configs'][name]['evidence']['passed'] for r in report['results']) for name in CROPS}
        (OUT/'fresh_test_report.json').write_text(json.dumps(report,indent=2)+'\n')
    save()
    try:
        raw=json.loads(Path('data/clip_library.json').read_text())
        rows=raw['clips'];rows=list(rows.values()) if isinstance(rows,dict) else rows
        selected=choose_fresh(rows,blocked_ids(),args.limit)
        report['requested']=len(selected);save()
        if not selected:raise RuntimeError('No unreviewed, unblocked indexed sources available')
        model=local.engine();deadline=time.monotonic()+args.minutes*60
        for index,row in enumerate(selected,1):
            if time.monotonic()>=deadline-120:
                report['stopped_for_time_budget']=True;break
            try:
                result=inspect(row,model,index);report['results'].append(result)
                print(f"FRESH {index}/{len(selected)} {row['clip_id']}: "+
                      ', '.join(f"{name}={result['configs'][name]['evidence']['passed']}" for name in CROPS),flush=True)
            except Exception as exc:
                report['errors'].append(dict(clip_id=row['clip_id'],type=type(exc).__name__,message=str(exc)[:500]))
                print(f"FRESH ERROR {row['clip_id']}: {type(exc).__name__}",flush=True)
            save()
        if not report['results']:raise RuntimeError('No review windows produced; inspect fresh_test_report.json')
    except Exception as exc:
        report['fatal_error']=dict(type=type(exc).__name__,message=str(exc)[:500]);raise
    finally:save()
    print(json.dumps({k:report[k] for k in ('requested','completed','pass_counts','openai_calls')},indent=2))


if __name__=='__main__':main()
