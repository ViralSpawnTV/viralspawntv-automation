"""Resume Kick listing cursors and locally screen candidates; never calls OpenAI."""
import argparse
import concurrent.futures
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode

import kick_game_discovery as discovery
from clip_library import LIBRARY, STATE, LOCAL_POLICY, blocked_ids, read, ready, write

QUERIES = [('date', 'day'), ('date', 'week'), ('date', 'month'),
           ('view', 'week'), ('view', 'month'), ('view', 'all')]


def screen(row):
    from local_combat_screen import screen as combat_screen
    if not row.get('prevalidated_media_url') or row.get('prevalidated_duration_seconds') is None:
        row = discovery.detail_candidate(row)
    if not row.get('prevalidated_media_url') or row.get('prevalidated_duration_seconds') is None:
        raise ValueError('Clip detail did not return required source metadata')
    if not discovery.is_eligible(row):
        return dict(row,library_screen_tag=LOCAL_POLICY,library_local_pass=False,
                    library_visual_approved=False,library_scanned_at=time.time()),False
    return combat_screen(row)


def collect_page(state):
    # One page per lane in a persistent round-robin, not the same first pages
    # on each run. Completed lanes reset after six hours to discover new clips.
    games = discovery.GAME_CATEGORIES[:8]
    lanes = [(g,s,q,t) for g,s in games for q,t in QUERIES]
    pos = state.get('lane_position', 0) % len(lanes)
    state['lane_position'] = (pos + 1) % len(lanes)
    game, slug, sort, span = lanes[pos]
    key = f'{slug}:{sort}:{span}'
    lane = state.setdefault('lanes', {}).setdefault(key, {})
    if lane.get('done_at'):
        if time.time() - lane['done_at'] < 21600:
            return [], False
        lane.clear()
    params = {'sort':sort, 'time':span}
    if lane.get('cursor'): params['cursor'] = lane['cursor']
    url = f'https://kick.com/api/v2/categories/{slug}/clips?' + urlencode(params)
    payload = discovery.fetch_json(url)  # Listing metadata, no AI API.
    rows = []
    for index, clip in enumerate(discovery.extract_listing_clips(payload)):
        row = discovery.candidate_from_clip(clip, game, slug, index)
        if row: rows.append(row)
    cursor = discovery.next_cursor(payload)
    if cursor and cursor != lane.get('cursor'):
        lane['cursor'] = cursor
    else:
        lane.pop('cursor', None); lane['done_at'] = time.time()
    return rows, True


def main(minutes=120, target=5000, max_scans=250, workers=1):
    minutes = max(3,min(240,minutes)); target = max(1,min(5000,target))
    max_scans = max(1,min(5000,max_scans)); workers = max(1,min(4,workers))
    deadline = time.monotonic() + minutes*60
    library = read(LIBRARY, {'version':1,'clips':{}})
    state = read(STATE, {'version':1,'seen':{},'pending':{},'lanes':{}})
    state.setdefault('seen', {}); state.setdefault('pending', {})
    blocked = blocked_ids(); scans = failures = listing_failures = pages = idle = 0
    # Pending work may have become published/rejected while discovery was idle.
    state['pending'] = {cid: row for cid, row in state['pending'].items()
                        if cid not in blocked and not (cid in library['clips'] and library['clips'][cid].get('library_screen_tag')==LOCAL_POLICY)}
    def save():
        write(LIBRARY, library); write(STATE,state)
    # Preserve old rows, but only new-policy passed evidence is available to
    # production. Re-screen legacy entries before fetching more listings.
    for cid,row in library['clips'].items():
        previous=state['seen'].get(cid,{})
        if cid in blocked or row.get('library_screen_tag') == LOCAL_POLICY:continue
        if previous.get('retry_after',0)>time.time():continue
        if previous.get('policy') == LOCAL_POLICY and previous.get('status') in ('passed','local_reject'):continue
        state['pending'].setdefault(cid,row)
    save()
    from local_combat_screen import engine
    if len(ready(library,blocked)) < target:
        engine()  # Fail before scanning/paid work if models cannot load.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(4,workers))) as pool:
        while time.monotonic() < deadline-80 and scans < max_scans and len(ready(library,blocked)) < target:
            if not state['pending']:
                try:
                    rows, requested = collect_page(state); pages += int(requested)
                    for row in rows:
                        cid = row['clip_id']; previous = state['seen'].get(cid,{})
                        if cid in blocked:continue
                        if cid in library['clips'] and library['clips'][cid].get('library_screen_tag')==LOCAL_POLICY:continue
                        if previous.get('policy')==LOCAL_POLICY and previous.get('status') in ('passed','local_reject'):continue
                        if previous.get('retry_after',0) > time.time(): continue
                        state['pending'][cid] = row
                    save()
                    idle = idle+1 if not state['pending'] else 0
                    if idle >= 96:
                        print('No fresh eligible clips across listing lanes; saving progress.');break
                except Exception as exc:
                    failures += 1; listing_failures += 1; save()
                    print(f'Listing unavailable: {type(exc).__name__}; cursor remains retryable.')
                    if listing_failures >= 12:break
                time.sleep(1)
                continue
            room = min(workers, max_scans-scans, target-len(ready(library,blocked)))
            batch = list(state['pending'].items())[:room]
            futures = {pool.submit(screen,row):(cid,row) for cid,row in batch}
            for future in concurrent.futures.as_completed(futures):
                cid,row = futures[future]; scans += 1; state['pending'].pop(cid,None)
                try:
                    candidate,passed = future.result()
                    state['seen'][cid] = {'status':'passed' if passed else 'local_reject',
                        'updated_at':time.time(), 'policy':LOCAL_POLICY}
                    if passed or cid in library['clips']:
                        library['clips'][cid]=candidate
                    recent=state.setdefault('recent_evidence',[])
                    recent.append({'clip_id':cid,'game':candidate.get('game'),'passed':passed,
                                   'evidence':candidate.get('library_combat_evidence'),
                                   'window_start':candidate.get('local_window_start'),
                                   'window_seconds':candidate.get('local_window_seconds')})
                    state['recent_evidence']=recent[-50:]
                except Exception as exc:
                    state['seen'][cid] = {'status':'error','updated_at':time.time(),
                        'retry_after':time.time()+21600,'policy':LOCAL_POLICY,'error':type(exc).__name__}
                    failures += 1
                save()
            count = len(ready(library,blocked))
            print(f'LIBRARY {count}/{target} candidates | local scans={scans} | no OpenAI calls',flush=True)
    summary = {'target':target,'available_locally_screened':len(ready(library,blocked)),
               'indexed_total':len(library['clips']),'scans_this_run':scans,
               'listing_pages_this_run':pages,'errors_this_run':failures,
               'visual_approved':False,'openai_calls':0,'policy':LOCAL_POLICY,
               'quarantined_or_failed':len(library['clips'])-len(ready(library,blocked)),
               'screening':'local gameplay model plus gunfire sound model'}
    write('work/clip_library_summary.json',summary)
    write('work/clip_library_evidence.json', {'policy':LOCAL_POLICY,'samples':state.get('recent_evidence',[])})
    summary_path = __import__('os').environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path,'a') as f:
            f.write(f"Candidate library: **{summary['available_locally_screened']}/{target}**. "
                    f"Scanned {scans} new clips. OpenAI calls: **0**. "
                    "Local image/audio screening only; final visual action approval remains required.\n")
    save();return summary


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--minutes',type=int,default=120)
    parser.add_argument('--target',type=int,default=5000)
    parser.add_argument('--max-scans',type=int,default=250)
    args=parser.parse_args();main(args.minutes,args.target,args.max_scans)
