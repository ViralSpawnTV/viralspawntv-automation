"""Find action-first windows, review a bounded shortlist, stage exact media reserves."""
import base64
import json
import math
import os
import shutil
import tempfile
import time
from pathlib import Path
from openai import OpenAI
import kick_game_discovery as discovery
from build_clip_library import collect_page
from clip_library import blocked_ids
from verified_clip_queue import QUEUE,STATE,MEDIA,POLICY,ARTIFACT_TTL,read,write,digest,judge,ready
from action_first_discovery import inspect,save_window,window_key,run

OUT=Path('work/verified_discovery')
SCAN_POLICY='action-first-temporal-v1'

def schema(n):
    properties={k:{'type':'number'} for k in ('firefight_score','dead_time_risk','source_score','payoff','story_sustain','editability')}
    properties.update({k:{'type':'boolean'} for k in ('ranged_shooter_gameplay','sustained_combat','recommended')})
    properties['reason']={'type':'string'}
    properties['samples']={'type':'array','minItems':n,'maxItems':n,'items':{'type':'object','additionalProperties':False,
        'properties':{'id':{'type':'integer','enum':list(range(n))},'direct_gunfight':{'type':'boolean'},'evidence':{'type':'string'}},
        'required':['id','direct_gunfight','evidence']}}
    return {'type':'object','additionalProperties':False,'properties':properties,'required':list(properties)}

def review(client,video,seconds,folder):
    folder.mkdir(parents=True,exist_ok=True);n=int(math.ceil(seconds))
    content=[{'type':'input_text','text':(
        'Review this EXACT continuous gameplay segment for ViralSpawnTV. Each numbered interval has two ordered images. '
        'Ignore title, facecam, subtitles, overlays, donations and game-category labels. '
        'direct_gunfight=true ONLY for visible LIVE ranged-weapon engagement with an opponent: firing/recoil/ammo '
        'decrease together with enemy engagement, hit markers during shooting, or an actual exchange of fire. '
        'Holding an angle, running, looting, reloading without firing, abilities alone, training-range targets, '
        'spectating, menus, victory banners alone and final-kill REPLAYS are false. Never infer firing from a held gun. '
        'When uncertain mark false. Label every interval, including inactive ones. '
        'Require high-intensity sustained direct combat; source_score grades THIS segment only; payoff needs a visible '
        'result resolving the fight, not an invented story. recommended=true only if this exact segment is usable. '
        'All six scores are 0 to 100. Return ranged_shooter_gameplay, sustained_combat, recommended, firefight_score, '
        'dead_time_risk, source_score, payoff, story_sustain, editability, reason, samples. '
        f'Samples must contain exactly {n} objects, integer id 0 through {n-1}, boolean direct_gunfight, '
        'and evidence describing visible observations in at most 12 words. Do not use sound or unseen events as evidence.') }]
    for i in range(n):
        width=min(1.,seconds-i)
        content.append({'type':'input_text','text':f'Interval {i}: {i:.2f} to {i+width:.2f} seconds'})
        for j,frac in enumerate((.2,.8)):
            p=folder/f'{i:02d}_{j}.jpg';p.unlink(missing_ok=True)
            run(['ffmpeg','-v','error','-y','-ss',str(i+width*frac),'-i',str(video),
                '-frames:v','1','-vf','scale=640:-2','-q:v','5',str(p)],20)
            if not p.is_file() or p.stat().st_size<500:raise ValueError('Incomplete review frame')
            content.append({'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(p.read_bytes()).decode()})
    # No retries: one review request per shortlisted exact window.
    response=client.responses.create(model=os.environ.get('READY_REVIEW_MODEL','gpt-5.6'),max_output_tokens=6000,
        input=[{'role':'user','content':content}],text={'format':{'type':'json_schema',
        'name':'exact_action_reserve','strict':True,'schema':schema(n)}})
    if getattr(response,'status','completed')!='completed':raise ValueError('Incomplete review response')
    verdict=json.loads(response.output_text);verification=judge(verdict,seconds)
    write(folder/'review.json',dict(verdict=verdict,verification=verification))
    return verdict,verification

def bounds(name,default,lo,hi):
    v=int(os.environ.get(name,str(default)))
    if not lo<=v<=hi:raise ValueError(name+' out of range')
    return v

def main():
    OUT.mkdir(parents=True,exist_ok=True);MEDIA.mkdir(parents=True,exist_ok=True)
    queue=read(QUEUE,{'version':1,'entries':{}});state=read(STATE,{'version':1,'seen':{},'pending':{},'lanes':{},'leases':{}})
    lease=read('work/verified_review_lease.json',{})
    if state.get('leases',{}).get(lease.get('run'),{}).get('limit')!=lease.get('limit'):
        raise RuntimeError('Durable review reservation missing')
    limit=lease['limit'];target=bounds('READY_TARGET',30,1,60)
    max_scans=bounds('READY_MAX_SCANS',25,1,100);minutes=bounds('READY_SCAN_MINUTES',45,3,120)
    report=dict(policy=POLICY,scans=0,paid_reviews=0,approved=0,errors=[],decisions=[],
        request_limit=limit,ready_before=len(ready(queue)),ready_target=target)
    def save():
        write(QUEUE,queue);write(STATE,state);write(OUT/'report.json',report)
    save()
    if limit==0 or report['ready_before']>=target:
        print('No refill required or daily review budget reserved; no paid calls.');return
    blocked=blocked_ids();seen=state.setdefault('seen',{});pending=state.setdefault('pending',{})
    indexed=read('data/clip_library.json',{'clips':{}}).get('clips',{})
    existing_ids={e['candidate']['clip_id'] for e in queue['entries'].values()
        if e.get('status') in ('ready','pending_media') and e.get('expires_at',0)>time.time()}
    for cid,row in indexed.items():
        if cid not in blocked|existing_ids and (seen.get(cid,{}).get('scan_policy')!=SCAN_POLICY or
            seen[cid].get('evidence',{}).get('source_fully_scanned') is False):pending.setdefault(cid,row)
    for cid,record in seen.items():
        if cid not in blocked|existing_ids and record.get('candidate') and (
            record.get('evidence',{}).get('source_fully_scanned') is False or record.get('status')=='error'):
            pending.setdefault(cid,record['candidate'])
    deadline=time.monotonic()+minutes*60;pages=0;shortlist=[]
    with tempfile.TemporaryDirectory(prefix='viralspawn-reserve-') as temp:
        while report['scans']<max_scans and time.monotonic()<deadline-(limit*180+120):
            # Fetch fresh listings once indexed legacy rows have been considered.
            if not pending:
                if pages>=48:break
                try:
                    rows,_=collect_page(state);pages+=1
                    for r in rows:
                        cid=r['clip_id']
                        if cid not in blocked|existing_ids and seen.get(cid,{}).get('scan_policy')!=SCAN_POLICY:
                            pending.setdefault(cid,r)
                except Exception as exc:
                    report['errors'].append({'stage':'listing','type':type(exc).__name__});pages+=1
                    if len(report['errors'])>=8:break
                save()
                if not pending:continue
            # Round robin game labels to avoid filling the shortlist from one lane.
            groups={}
            for cid,r in pending.items():groups.setdefault(r.get('game','unknown'),[]).append(cid)
            group=list(groups)[report['scans']%len(groups)];cid=groups[group][0];row=pending.pop(cid)
            if cid in blocked|existing_ids:continue
            previous=seen.get(cid,{})
            if previous.get('retry_after',0)>time.time():continue
            if previous.get('scan_policy')==SCAN_POLICY and previous.get('evidence',{}).get('source_fully_scanned') is not False:continue
            report['scans']+=1
            try:
                if not row.get('prevalidated_media_url'):row=discovery.detail_candidate(row)
                if not discovery.is_eligible(row):
                    seen[cid]={'scan_policy':SCAN_POLICY,'status':'ineligible','updated_at':time.time()};save();continue
                folder=Path(temp)/str(report['scans'])
                offset=previous.get('evidence',{}).get('next_scan_offset',0.)
                wins,evidence=inspect(row,folder,offset=offset)
                seen[cid]={'scan_policy':SCAN_POLICY,'status':'scanned','updated_at':time.time(),
                           'evidence':evidence,'windows':previous.get('windows',[])+wins,'candidate':row,'reviewed':previous.get('reviewed',{})}
                for win in wins:shortlist.append((row,win))
                print(f"LOCAL {report['scans']}/{max_scans}: {cid}: {len(wins)} action-first windows",flush=True)
            except Exception as exc:
                seen[cid]=dict(previous,status='error',retry_after=time.time()+21600,updated_at=time.time(),candidate=row)
                report['errors'].append({'clip_id':cid,'stage':'scan','type':type(exc).__name__})
            save()
        # Reuse unpaid free candidates on later runs instead of scanning the same source again.
        for cid,record in seen.items():
            if cid in blocked|existing_ids or record.get('scan_policy')!=SCAN_POLICY:continue
            for win in record.get('windows',[]):
                key=window_key(cid,win);past=record.get('reviewed',{}).get(key,{})
                if past.get('status') in ('passed','rejected') or past.get('retry_after',0)>time.time():continue
                shortlist.append((record['candidate'],win))
        shortlist.sort(key=lambda item:(item[1]['active_ratio'],-item[1]['first_action_hint_seconds'],
                       -item[1]['longest_gap_seconds'],-item[1]['last_action_tail_seconds']),reverse=True)
        unique=[];keys=set()
        for row,win in shortlist:
            key=window_key(row['clip_id'],win)
            past=seen.get(row['clip_id'],{}).get('reviewed',{}).get(key,{})
            if past.get('status') in ('passed','rejected') or past.get('retry_after',0)>time.time():continue
            if key not in keys:unique.append((row,win,key));keys.add(key)
        client=OpenAI(max_retries=0,timeout=90);approved_ids=set()
        for row,win,key in unique:
            if report['paid_reviews']>=limit or report['ready_before']+report['approved']>=target:break
            if time.monotonic()>deadline-120:break
            cid=row['clip_id']
            if cid in approved_ids:continue
            p=MEDIA/(key+'.mp4');folder=OUT/key
            try:
                actual=save_window(row,win,p)
                # Mark attempted before request; malformed responses/errors never become content rejections.
                report['paid_reviews']+=1;save()
                verdict,verification=review(client,p,win['seconds'],folder)
                seen[cid].setdefault('reviewed',{})[key]={'status':'passed' if verification['passed'] else 'rejected',
                    'updated_at':time.time(),'verification':verification,'media_sha256':digest(p)}
                report['decisions'].append(dict(clip_id=cid,key=key,start=win['start'],seconds=win['seconds'],verification=verification))
                if verification['passed']:
                    now=time.time()
                    queue['entries'][key]=dict(key=key,policy=POLICY,status='pending_media',candidate=row,
                        start_original=win['start'],seconds=win['seconds'],decoded_seconds=actual,
                        media_file=p.name,media_sha256=digest(p),verdict=verdict,verification=verification,
                        local_evidence=win,created_at=now,updated_at=now,expires_at=now+ARTIFACT_TTL,
                        artifact_run=lease['run'])
                    report['approved']+=1;approved_ids.add(cid)
                else:p.unlink(missing_ok=True)
                print(f"REVIEW {'PASS' if verification['passed'] else 'REJECT WINDOW'} {cid}: {verification['reason']}",flush=True)
            except Exception as exc:
                p.unlink(missing_ok=True)
                seen[cid].setdefault('reviewed',{})[key]={'status':'error','retry_after':time.time()+21600,'updated_at':time.time()}
                report['errors'].append({'clip_id':cid,'stage':'review','type':type(exc).__name__})
            save()
    report['ready_after_media_upload']=report['ready_before']+report['approved']
    ledger=read(os.environ.get('VIRALSPAWN_AI_LEDGER','work/verified_discovery/ai_budget.json'),{})
    report['api_request_attempts']=len(ledger.get('calls',[]))
    report['actual_tokens']=ledger.get('actual_tokens',{})
    save()
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a') as f:f.write(f"Verified reserve refill: {report['scans']} local scans, {report['paid_reviews']} paid review attempts, **{report['approved']} approved**. Ready before refill: {report['ready_before']}. Media promotion still required.\n")
    print(json.dumps(report,indent=2))
if __name__=='__main__':main()
