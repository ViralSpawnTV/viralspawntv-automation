"""Read-only CPU discovery experiment. Never approves clips or calls OpenAI."""
import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import local_combat_screen as local

FAILED_CONTROL_IDS = (
 'clip_01M4D61YQW87ZT27B41D52PV9W', 'clip_01M4D4DZS7J4Y5422740EHXTYE',
 'clip_01M45NQ8SWJV9C6CYEAW32HQ53', 'clip_01M4DDZKBR1RSSGTAPCTG8VBR3',
 'clip_01M41G53KXN8XA57M55VD4EQT9', 'clip_01M2SB5M286AQB18S13NPC8Z0X',
)
OUT = Path('work/library_review')


def read_rows(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get('clips', data.get('candidates', []))
        if isinstance(rows, dict): rows = list(rows.values())
    else: raise ValueError('Input must contain clips or candidates')
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError('Invalid candidate collection')
    return rows


def choose_rows(rows, limit):
    unique = {r['clip_id']: r for r in rows if isinstance(r.get('clip_id'), str)}
    controls = [dict(unique[i], review_group='known_paid_reject')
                for i in FAILED_CONTROL_IDS if i in unique]
    # Explicit human labels can be supplied using --input for calibration.
    labelled = [dict(r, review_group='human_labelled_control') for i,r in unique.items()
                if i not in FAILED_CONTROL_IDS and r.get('review_label') in ('combat', 'noncombat')]
    other = [dict(r, review_group='unlabelled_candidate') for i,r in unique.items()
             if i not in FAILED_CONTROL_IDS and r.get('review_label') not in ('combat', 'noncombat')]
    # Mix games instead of consuming an entire game before the next.
    groups = {}
    for r in other: groups.setdefault(r.get('game', 'unknown'), []).append(r)
    mixed = []
    while any(groups.values()):
        for group in groups.values():
            if group: mixed.append(group.pop(0))
    return (controls + labelled + mixed)[:limit]


def combat_margins(engine, images, game):
    prompts = [
        f'a screenshot of {game}: the player firing a gun at a visible enemy during a firefight',
        'shooter video game gameplay showing muzzle flash and a visible enemy being shot',
        'shooter video game gameplay showing close range gun combat with an enemy',
        'shooter video game gameplay: a player running through an empty area without fighting',
        'shooter video game gameplay: a player holding a gun and waiting at an empty doorway',
        'shooter video game gameplay: a player looting items or opening an inventory menu',
        'shooter video game gameplay: death screen, scoreboard or spectating after elimination',
        'shooter video game gameplay: buy menu, lobby or loading screen',
    ]
    inputs = engine.tokenizer(prompts, padding='max_length', truncation=True,
                              max_length=77, return_tensors='np')
    text = engine.output(engine.text, dict(inputs), 'text_embeds')
    text /= np.maximum(np.linalg.norm(text, axis=-1, keepdims=True), 1e-8)
    result = []
    for i in range(0, len(images), 8):
        pixels = engine.processor(images=images[i:i+8], return_tensors='np')['pixel_values']
        vectors = engine.output(engine.vision, {'pixel_values': pixels}, 'image_embeds')
        vectors /= np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-8)
        sims = vectors @ text.T
        result.extend((sims[:,:3].max(axis=1)-sims[:,3:].max(axis=1)).tolist())
    if not np.isfinite(result).all(): raise ValueError('Non-finite combat evidence')
    return result


def compare_windows(gameplay, combat, audio, duration):
    baseline = local.select_window(gameplay, audio, duration)
    # Both margins must beat inactivity comparisons. Provisional threshold;
    # this is an experiment, NOT a new production approval policy.
    fused = [min(g,c) if any(b['start'] <= i/local.FRAME_FPS < b['end']
                    and b['score'] >= local.GUNFIRE_MIN for b in audio) else -1.0
             for i,(g,c) in enumerate(zip(gameplay, combat))]
    experimental = local.select_window(fused, audio, duration)
    experimental['method'] = 'experimental gameplay AND combat contrast aligned with gunfire bins'
    return baseline, experimental


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


def inspect(row, model, index):
    url = row.get('prevalidated_media_url') or row.get('media_url') or row.get('playlist_url')
    if not url: raise ValueError('Missing media URL')
    with tempfile.TemporaryDirectory(prefix='viralspawntv-review-') as td:
        td = Path(td); preview = td/'preview.mp4'
        run(['ffmpeg','-v','error','-y','-rw_timeout','12000000','-i',str(url),'-t','120',
             '-map','0:v:0','-map','0:a:0?','-vf','scale=640:-2','-r','8',
             '-c:v','libx264','-preset','ultrafast','-crf','28','-c:a','aac',str(preview)],120)
        probe = run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(preview)])
        duration = min(120,float(json.loads(probe.stdout)['format']['duration']))
        # Explicit time seeks avoid assuming the timestamp offset of ffmpeg's fps filter.
        frames = []; times = list(np.arange(0,duration,1/local.FRAME_FPS))
        for i,t in enumerate(times):
            p = td/f'sample_{i:03}.jpg'
            run(['ffmpeg','-v','error','-y','-ss',str(t),'-i',str(preview),
                 '-frames:v','1','-vf','scale=384:-2',str(p)],20)
            with Image.open(p) as im: frames.append(im.convert('RGB'))
        raw = run(['ffmpeg','-v','error','-i',str(preview),'-vn','-ac','1',
                   '-ar',str(local.AUDIO_RATE),'-f','f32le','-']).stdout
        samples = np.frombuffer(raw,dtype='<f4')
        known = float(row.get('source_duration_seconds') or row.get('prevalidated_duration_seconds') or duration)
        duration = min(duration,len(samples)/local.AUDIO_RATE,known)
        if duration < 19: raise ValueError('Less than 19 seconds of complete evidence')
        with model.lock:
            gameplay = model.image_margins(frames,str(row.get('game','shooter')))
            combat = combat_margins(model,frames,str(row.get('game','shooter')))
            audio = model.audio_scores(samples)
        baseline, experimental = compare_windows(gameplay,combat,audio,duration)
        folder = OUT/f'clip_{index:02}'
        exports = {}
        for name,evidence in [('baseline',baseline),('experimental',experimental)]:
            exports[name] = export_window(preview,evidence,folder/name)
        record = {'clip_id':row['clip_id'],'game':row.get('game'),'clip_url':row.get('clip_url'),
          'review_group':row['review_group'],'human_label':row.get('review_label'),
          'baseline':baseline,'experimental':experimental,'exports':exports,
          'frames':[{'source_seconds':round(float(t),3),'gameplay_margin':round(float(g),5),
                     'combat_margin':round(float(c),5)} for t,g,c in zip(times,gameplay,combat)],
          'audio_bins':audio,'approved_for_production':False}
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'evidence.json').write_text(json.dumps(record,indent=2)+'\n')
        return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input',default='data/clip_library.json')
    parser.add_argument('--limit',type=int,default=25)
    parser.add_argument('--minutes',type=int,default=60)
    args = parser.parse_args()
    if not 1 <= args.limit <= 25 or not 3 <= args.minutes <= 120:
        parser.error('limit must be 1-25; minutes must be 3-120')
    OUT.mkdir(parents=True,exist_ok=True)
    report = {'mode':'read-only calibration','openai_calls':0,'approved_for_production':False,
       'model_revisions':{k:v[1] for k,v in local.REPOS.items()},'results':[],'errors':[],
       'limitation':'Prompt comparisons are uncalibrated. Human review required; no library updates.'}
    def save():
        report['completed'] = len(report['results'])
        report['baseline_passes'] = sum(r['baseline']['passed'] for r in report['results'])
        report['experimental_passes'] = sum(r['experimental']['passed'] for r in report['results'])
        (OUT/'review_report.json').write_text(json.dumps(report,indent=2)+'\n')
    save()
    try:
        rows = choose_rows(read_rows(args.input),args.limit)
        report['requested'] = len(rows); save()
        model = local.engine()
        deadline = time.monotonic()+args.minutes*60
        for index,row in enumerate(rows,1):
            if time.monotonic() >= deadline-120:
                report['stopped_for_time_budget'] = True; break
            try:
                result = inspect(row,model,index); report['results'].append(result)
                print(f"REVIEW {index}/{len(rows)}: {row['clip_id']} baseline={result['baseline']['passed']} experimental={result['experimental']['passed']}",flush=True)
            except Exception as exc:
                report['errors'].append({'clip_id':row['clip_id'],'type':type(exc).__name__,
                                         'message':str(exc)[:500]})
                print(f'REVIEW ERROR: {row["clip_id"]} {type(exc).__name__}',flush=True)
            save()
        if not report['results']: raise RuntimeError('No review footage produced; inspect review_report.json')
    except Exception as exc:
        report['fatal_error'] = {'type':type(exc).__name__,'message':str(exc)[:500]}
        raise
    finally: save()
    print(json.dumps({k:v for k,v in report.items() if k not in ('results','errors')},indent=2))


if __name__ == '__main__': main()
