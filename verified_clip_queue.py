"""Exact-media verified reserves. No model calls in this module."""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

POLICY = 'exact-action-reserve-v1'
QUEUE = Path('data/verified_clip_queue.json')
STATE = Path('data/verified_discovery_state.json')
MEDIA = Path('work/verified_media')
ARTIFACT_TTL = 25 * 86400

def read(path, default):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else default

def write(path, data):
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, allow_nan=False)+'\n')
    os.replace(tmp, p)

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()

def number(value):
    return type(value) in (int,float) and math.isfinite(value)

def judge(verdict, seconds):
    """Compute timing locally from complete per-second labels; reject malformed output."""
    n = int(math.ceil(seconds))
    rows = verdict.get('samples') if isinstance(verdict,dict) else None
    if not isinstance(rows,list) or len(rows)!=n: raise ValueError('Incomplete review timeline')
    by_id = {}
    for r in rows:
        if not isinstance(r,dict) or type(r.get('id')) is not int or r['id'] in by_id:
            raise ValueError('Invalid/duplicate review interval')
        if type(r.get('direct_gunfight')) is not bool or not isinstance(r.get('evidence'),str) or not r['evidence'].strip():
            raise ValueError('Invalid visible-action evidence')
        by_id[r['id']] = r
    if set(by_id)!=set(range(n)): raise ValueError('Missing review intervals')
    for name in ('firefight_score','dead_time_risk','source_score','payoff','story_sustain','editability'):
        if not number(verdict.get(name)) or not 0<=verdict[name]<=100: raise ValueError('Invalid '+name)
    for name in ('ranged_shooter_gameplay','sustained_combat','recommended'):
        if type(verdict.get(name)) is not bool: raise ValueError('Invalid '+name)
    if not isinstance(verdict.get('reason'),str) or not verdict['reason'].strip(): raise ValueError('Missing review reason')
    weights = [min(1.,seconds-i) for i in range(n)]
    active = [by_id[i]['direct_gunfight'] for i in range(n)]
    first = next((i for i,x in enumerate(active) if x), seconds)
    last_end = max((i+weights[i] for i,x in enumerate(active) if x),default=0.)
    gap = longest = 0.
    for x,w in zip(active,weights):
        gap = 0. if x else gap+w; longest=max(longest,gap)
    fraction = sum(w for x,w in zip(active,weights) if x)/seconds
    anchors = [min(n-1,int(seconds*f)) for f in (.16,.34,.52,.70,.86)]
    anchor_hits = sum(active[i] for i in anchors)
    reasons=[]
    if not verdict['ranged_shooter_gameplay']: reasons.append('not confirmed ranged gameplay')
    if not verdict['sustained_combat']: reasons.append('not sustained combat')
    if verdict['firefight_score']<80 or verdict['dead_time_risk']>20 or anchor_hits<4:
        reasons.append('strict firefight requirement failed')
    if fraction<.55 or first>4 or longest>6 or seconds-last_end>3:
        reasons.append('action timing/density requirement failed')
    if not verdict['recommended'] or verdict['source_score']<65 or verdict['payoff']<60:
        reasons.append('source quality/payoff requirement failed')
    return dict(passed=not reasons,reason='; '.join(reasons) or verdict['reason'],
                active_fraction=fraction,first_action_seconds=first,longest_inactive_seconds=longest,
                post_combat_seconds=seconds-last_end,anchor_active_samples=anchor_hits,
                source_score=verdict['source_score'],payoff=verdict['payoff'])

def valid(entry):
    try:
        seconds=entry['seconds']
        return (entry['policy']==POLICY and number(seconds) and 19<=seconds<=35
            and number(entry['start_original']) and entry['start_original']>=0
            and entry['verification'].get('passed') is True
            and judge(entry['verdict'],seconds)['passed']
            and re.fullmatch(r'[a-f0-9]{64}',entry['media_sha256']) is not None
            and entry['media_file']==entry['key']+'.mp4'
            and re.fullmatch(r'[a-f0-9]{24}',entry['key']) is not None
            and isinstance(entry['candidate'],dict) and bool(entry['candidate'].get('clip_id')))
    except (KeyError,TypeError,ValueError): return False

def ready(queue=None, blocked=None, now=None):
    if queue is None: queue=read(QUEUE,{'entries':{}})
    if blocked is None:
        from clip_library import blocked_ids
        blocked=blocked_ids()
    now=time.time() if now is None else now
    rows=[e for e in queue.get('entries',{}).values() if valid(e)
          and e.get('status')=='ready' and e['candidate']['clip_id'] not in blocked
          and number(e.get('expires_at')) and e['expires_at']>now
          and e.get('retry_after',0)<=now and e.get('artifact_id')]
    return sorted(rows,key=lambda e:(e['verification']['active_fraction'],
          e['verdict']['payoff'],e['verdict']['firefight_score'],-e['created_at']),reverse=True)

def update(queue, key, **fields):
    queue['entries'][key].update(fields,updated_at=time.time());write(QUEUE,queue)

def claim(limit, daily_limit):
    """Reserve requests before network access. Workflow MUST commit this before scanning."""
    if not 0<=limit<=6 or not 0<=daily_limit<=100: raise ValueError('Budget limit out of range')
    state=read(STATE,{'version':1,'seen':{},'pending':{},'lanes':{},'leases':{}})
    leases=state.setdefault('leases',{})
    run=str(os.environ['GITHUB_RUN_ID'])+'-'+str(os.environ.get('GITHUB_RUN_ATTEMPT','1'))
    day=dt.datetime.now(dt.timezone.utc).date().isoformat()
    if run in leases: raise RuntimeError('This run already reserved review requests; rerun as a new attempt')
    used=sum(x['limit'] for x in leases.values() if x['day']==day)
    target=int(os.environ.get('READY_TARGET','30'))
    amount=0 if len(ready())>=target else min(limit,max(0,daily_limit-used))
    leases[run]={'day':day,'limit':amount,'created_at':time.time()}
    write(STATE,state)
    write('work/verified_review_lease.json',{'run':run,'limit':amount})
    print(f'Reserved {amount} review requests; {used+amount}/{daily_limit} reserved today (UTC).')

def merge_objects(latest, generated):
    result=dict(latest);result.setdefault('entries',{})
    for k,e in generated.get('entries',{}).items():
        old=result['entries'].get(k,{})
        if old.get('status') in ('published','rejected') and e.get('status')=='ready':continue
        if e.get('updated_at',0)>=old.get('updated_at',0):result['entries'][k]=e
    return result

def persist(paths):
    """Push only queue-owned JSON files; retry against concurrent discovery commits."""
    branch=os.environ.get('TARGET_BRANCH','main')
    generated={p:read(p,{}) for p in paths if Path(p).exists()}
    if not generated:return
    for attempt in range(3):
        subprocess.run(['git','fetch','origin',branch],check=True,capture_output=True)
        head=subprocess.check_output(['git','rev-parse','origin/'+branch],text=True).strip()
        # Reset tracked code to the current branch only AFTER the caller snapshots its outputs.
        subprocess.run(['git','reset','--hard',head],check=True,capture_output=True)
        for path,new in generated.items():
            old=read(path,{})
            if path==str(QUEUE): merged=merge_objects(old,new)
            else:
                merged=dict(old)
                for field in ('seen','pending','lanes','leases'):
                    merged[field]=dict(old.get(field,{}));merged[field].update(new.get(field,{}))
                for field in ('version','lane_position'):merged[field]=new.get(field,old.get(field,1))
            write(path,merged)
        subprocess.run(['git','add','--',*generated],check=True)
        if subprocess.run(['git','diff','--cached','--quiet']).returncode==0:return
        subprocess.run(['git','commit','-m','Update verified action reserve'],check=True,capture_output=True)
        if subprocess.run(['git','push','origin','HEAD:'+branch],capture_output=True).returncode==0:return
    raise RuntimeError('Queue persistence failed; recovery artifact contains generated progress')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def github_request(url):
    if not url.startswith('https://api.github.com/'):raise ValueError('Unexpected GitHub API host')
    token=os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:raise RuntimeError('GitHub artifact read token missing')
    return urllib.request.Request(url,headers={'Authorization':'Bearer '+token,
        'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28',
        'User-Agent':'ViralSpawnTV-verified-reserve'})

def download(entry):
    folder=Path('work/verified_downloads')/str(entry['artifact_id']);path=folder/entry['media_file']
    if path.is_file() and digest(path)==entry['media_sha256']:return path
    repo=os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise ValueError('Invalid repository')
    aid=int(entry['artifact_id']);url=f'https://api.github.com/repos/{repo}/actions/artifacts/{aid}/zip'
    try:
        urllib.request.build_opener(NoRedirect()).open(github_request(url),timeout=30)
        raise RuntimeError('Artifact API did not redirect')
    except urllib.error.HTTPError as exc:
        if exc.code!=302:raise RuntimeError(f'Artifact unavailable (HTTP {exc.code})') from None
        location=exc.headers['Location']
    if urllib.parse.urlparse(location).scheme!='https':raise ValueError('Insecure artifact redirect')
    folder.mkdir(parents=True,exist_ok=True);archive=folder/'bundle.zip'
    # Do not forward the GitHub token to the temporary storage URL.
    with urllib.request.urlopen(location,timeout=60) as source,archive.open('wb') as out:
        total=0
        while True:
            block=source.read(1024*1024)
            if not block:break
            total+=len(block)
            if total>500*1024*1024:raise ValueError('Artifact exceeds size bound')
            out.write(block)
    with zipfile.ZipFile(archive) as z:
        matches=[i for i in z.infolist() if Path(i.filename).name==entry['media_file']]
        if len(matches)!=1 or matches[0].file_size>100*1024*1024:raise ValueError('Missing/ambiguous media in artifact')
        with z.open(matches[0]) as src,path.open('wb') as dst:shutil.copyfileobj(src,dst)
    if digest(path)!=entry['media_sha256']:
        path.unlink(missing_ok=True);raise ValueError('Verified media hash mismatch')
    return path

def promote(artifact_id):
    queue=read(QUEUE,{'version':1,'entries':{}})
    run=str(os.environ['GITHUB_RUN_ID'])+'-'+str(os.environ.get('GITHUB_RUN_ATTEMPT','1'))
    for key,e in queue['entries'].items():
        if e.get('status')=='pending_media' and e.get('artifact_run')==run:
            if not valid(e) or digest(MEDIA/e['media_file'])!=e['media_sha256']:
                raise RuntimeError('Invalid queue entry during promotion')
            e.update(status='ready',artifact_id=int(artifact_id),updated_at=time.time())
    write(QUEUE,queue)

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    c=sub.add_parser('claim');c.add_argument('--limit',type=int,required=True);c.add_argument('--daily-limit',type=int,required=True)
    c=sub.add_parser('persist');c.add_argument('paths',nargs='+')
    c=sub.add_parser('promote');c.add_argument('artifact_id',type=int)
    sub.add_parser('count')
    c=sub.add_parser('publish');c.add_argument('--succeeded',action='store_true')
    a=p.parse_args()
    if a.command=='claim':claim(a.limit,a.daily_limit)
    elif a.command=='persist':persist(a.paths)
    elif a.command=='promote':promote(a.artifact_id)
    elif a.command=='publish':
        selected=read('work/verified_selected.json',{})
        queue=read(QUEUE,{'entries':{}})
        if a.succeeded and selected.get('key') in queue['entries']:
            update(queue,selected['key'],status='published',published_at=time.time())
    else:print(len(ready()))
if __name__=='__main__':main()
