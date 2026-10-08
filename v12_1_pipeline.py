"""Production consumes exact verified reserves; discovery never runs here."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
import kick_game_discovery as discovery
from clip_library import blocked_ids
from verified_clip_queue import QUEUE,read,write,ready,download,update,valid,digest
from firefight_cache import defer_failed_edit

LOG=Path('work/v12_attempt_log.json')

def run(script,extra=None):
    env=os.environ.copy();env['VIRALSPAWN_AI_STAGE']=script
    env.update(extra or {})
    return subprocess.run([sys.executable,script],env=env).returncode

def clear_attempt():
    # A failed next attempt cannot publish a previous attempt's output or verdict.
    shutil.rmtree('work/production',ignore_errors=True)
    shutil.rmtree('work/music_gate',ignore_errors=True)
    shutil.rmtree('work/source_quality_gate',ignore_errors=True)
    Path('work/kick_gaming/selected_kick_gaming_source.mp4').unlink(missing_ok=True)
    Path('work/kick_gaming/acquisition_result.json').unlink(missing_ok=True)

def install_source(entry,media):
    from action_first_discovery import duration,run as media_run
    seconds=duration(media)
    if abs(seconds-entry['seconds'])>.2:raise ValueError('Verified source duration mismatch')
    media_run(['ffmpeg','-v','error','-i',str(media),'-f','null','-'],60)
    row=dict(entry['candidate']);v=entry['verdict'];key=entry['key']
    folder=Path('work/kick_gaming');folder.mkdir(parents=True,exist_ok=True)
    target=folder/'selected_kick_gaming_source.mp4';shutil.copy2(media,target)
    if digest(target)!=entry['media_sha256']:raise ValueError('Installed media hash mismatch')
    start=entry['start_original'];end=start+entry['seconds']
    row.update(proposed_window_start=start,proposed_window_end=end,local_window_start=start,
        local_window_seconds=entry['seconds'],prescreen_predicted_score=v['source_score'],
        prescreen_payoff=v['payoff'],prescreen_story_sustain=v['story_sustain'],
        prescreen_action=v['firefight_score'],prescreen_reason=v['reason'],
        prescreen_rank_score=v['firefight_score'])
    write('work/v12_prescreened_candidates.json',{'selection_mode':'exact_verified_reserve','candidates':[row]})
    result=dict(row,creator=row.get('creator') or row.get('channel'),local_path=str(target),
        original_source_duration_seconds=row.get('prevalidated_duration_seconds'),
        proposed_window_start_original=start,proposed_window_end_original=end,
        local_source_is_selected_window=True,source_duration_seconds=entry['seconds'],
        proposed_window_label='verified_action_first',verified_queue_key=key,
        verified_media_sha256=entry['media_sha256'],rights_status=row.get('rights_status','unverified'),
        creator_permission_verified=row.get('creator_permission_verified',False),
        game_rights_verified=row.get('game_rights_verified',False),
        acquisition_context='automated_public_pipeline',public_publish_allowed=True)
    write(folder/'acquisition_result.json',result)
    # Reuse ONLY the source review attached to this SHA-256 checked exact media.
    write('work/source_quality_gate/source_quality_gate_result.json',dict(v,passed=True,
          minimum_source_score=65,minimum_payoff=60,clip_id=row['clip_id'],
          selected_window_original=[start,end],verified_media_sha256=entry['media_sha256']))
    return row

def api_failed():
    d=read(os.environ.get('VIRALSPAWN_AI_LEDGER','work/ai_budget.json'),{})
    calls=d.get('calls',[])
    return bool(d.get('exhausted')) or bool(calls and calls[-1].get('status')=='failed')

def permanently_reject_music(cid):
    p=Path('shorts_rejected_history.json');data=read(p,{'version':1,'clip_ids':[]})
    ids=set(data if isinstance(data,list) else data.get('clip_ids',[]));ids.add(cid)
    write(p,{'version':1,'clip_ids':sorted(ids)})

def eligible(entries):
    history=read('history.json',{'used_clips':[]})
    _,creator_counts,game_counts=discovery.history_state(history)
    seen=set();result=[]
    for e in entries:
        row=e['candidate'];cid=row['clip_id'];creator=str(row.get('channel','')).lower();game=str(row.get('game','')).lower()
        if cid in seen:continue
        if creator_counts.get(creator,0)>=discovery.MAX_PUBLIC_UPLOADS_PER_CREATOR_24H:continue
        if game_counts.get(game,0)>=discovery.MAX_PUBLIC_UPLOADS_PER_GAME_24H:continue
        if not discovery.is_eligible(row):continue
        result.append(e);seen.add(cid)
    return result

def main():
    clear_attempt();attempts=[];write(LOG,attempts)
    queue=read(QUEUE,{'version':1,'entries':{}})
    candidates=eligible(ready(queue,blocked_ids()))
    write('work/v11_candidate_manifest.json',{'selection_mode':'exact_verified_reserve',
        'candidate_count':len(candidates),'candidates':[e['candidate'] for e in candidates]})
    max_attempts=int(os.environ.get('READY_MAX_PRODUCTION_ATTEMPTS','3'))
    if not 1<=max_attempts<=4:raise ValueError('Production attempts must be 1-4')
    if not candidates:raise RuntimeError('Verified reserve empty or diversity-limited. Run Verified Action Reserve refill; no production OpenAI calls made.')
    for entry in candidates[:max_attempts]:
        clear_attempt();key=entry['key'];cid=entry['candidate']['clip_id']
        attempt=dict(key=key,clip_id=cid);attempts.append(attempt);write(LOG,attempts)
        try:
            if not valid(entry):raise ValueError('Invalid saved verification')
            row=install_source(entry,download(entry))
            attempt['source']='sha256_checked_verified_media'
        except Exception as exc:
            attempt.update(stage='media',error_type=type(exc).__name__)
            update(queue,key,retry_after=time.time()+21600,last_error='media_unavailable')
            write(LOG,attempts);continue
        code=run('music_gate.py');attempt['music_exit_code']=code
        if code!=0:
            if code==20 and not api_failed():
                permanently_reject_music(cid);update(queue,key,status='rejected',reason='explicit_music_rejection')
            else:update(queue,key,retry_after=time.time()+21600,last_error='music_review_unavailable')
            write(LOG,attempts)
            if api_failed():break
            continue
        code=run('production_test.py',{'FIREFIGHT_WINDOW_START':'0'})
        attempt.update(production_exit_code=code,action_segment_validation=read('work/production/action_segment_gate/result.json',{}))
        if code!=0:
            permanent=defer_failed_edit(row,attempt['action_segment_validation'],source_offset=entry['start_original'])
            update(queue,key,**({'status':'rejected','reason':'valid_action_edit_rejection'} if permanent else
                              {'retry_after':time.time()+21600,'last_error':'production_failed'}))
            write(LOG,attempts)
            if api_failed():break
            continue
        # Strict renderer action gate remains mandatory, even after discovery approval.
        action=attempt['action_segment_validation']
        if action.get('passed') is not True:
            update(queue,key,retry_after=time.time()+21600,last_error='missing_final_action_approval');continue
        code=run('final_content_gate.py');attempt['content_exit_code']=code
        if code!=0:
            update(queue,key,retry_after=time.time()+21600,last_error='final_content_gate_failed');write(LOG,attempts)
            if api_failed():break
            continue
        Path('work/production/finished_viral_gate.json').unlink(missing_ok=True)
        code=run('finished_viral_gate.py');finished=read('work/production/finished_viral_gate.json',{})
        attempt.update(finished_exit_code=code,finished_verdict=finished);write(LOG,attempts)
        if code==0 and finished.get('passed') is True and Path('work/production/ViralSpawnTV_Short_V4.mp4').is_file():
            write('work/verified_selected.json',{'key':key,'clip_id':cid})
            # History persistence after upload blocks reuse; hold through uncertain upload outcomes.
            update(queue,key,retry_after=time.time()+86400,last_error='awaiting_upload_outcome')
            print(f'PRODUCTION READY: {cid}; exact verified source, all final gates passed.');return
        update(queue,key,retry_after=time.time()+21600,last_error='finished_quality_failed')
        if api_failed():break
    clear_attempt()
    raise RuntimeError('Verified candidates failed final checks or run budget exhausted; no upload. See attempt log.')
if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('VERIFIED RESERVE PIPELINE:',exc);raise SystemExit(1)
