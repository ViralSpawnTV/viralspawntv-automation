"""Frozen-frame experiment. CPU only; no production approval or API calls."""
import json
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
import local_combat_screen as local

OUT = Path('work/library_calibration')
CROPS = {
 'full':(0,0,1,1),
 'upper_gameplay':(0,0,1,.78),
 'central_gameplay':(.18,.12,.95,.78),
}
BANKS = {
 'combat':[
  'a screenshot of shooter game combat with the player firing at an enemy',
  'a screenshot of shooter game combat with a gun muzzle flash',
  'a screenshot of shooter game close range fighting with guns',
  'a screenshot of a shooter game: holding a gun and waiting without firing',
  'a screenshot of a shooter game: running or looting without shooting',
  'a screenshot of a video game menu, scoreboard, death screen or lobby',
  'a webcam photograph of a person talking',
  'a screenshot of a mobile strategy game without guns',
  'a screenshot of a shooter game: using a glowing ability without firing a gun',
 ],
 'weapon_firing':[
  'first person shooter game screenshot, a gun firing, bright muzzle flash from the gun barrel',
  'video game screenshot, player firing an automatic rifle with a visible muzzle flash',
  'shooter game screenshot, gun firing through a scope at a target',
  'first person shooter game screenshot, player holding an idle gun without firing',
  'first person shooter game screenshot, player reloading a gun behind cover',
  'video game screenshot, player running with a knife or using an ability without gunfire',
  'video game screenshot, inventory, menu, scoreboard or death screen',
  'webcam photo of a streamer talking, no shooter gameplay',
  'screenshot of a mobile strategy game without firearms',
 ],
}
THRESHOLDS = (-.02,-.01,0,.005,.01,.02,.03)


def crop_image(image, bounds):
    w,h = image.size
    return image.crop(tuple(round(v*s) for v,s in zip(bounds,(w,h,w,h))))


def metrics(rows, threshold):
    tp = sum(r['label']=='combat' and r['margin']>=threshold for r in rows)
    fp = sum(r['label']=='inactive' and r['margin']>=threshold for r in rows)
    fn = sum(r['label']=='combat' and r['margin']<threshold for r in rows)
    tn = sum(r['label']=='inactive' and r['margin']<threshold for r in rows)
    return dict(tp=tp,fp=fp,fn=fn,tn=tn,positive_count=tp+fn,negative_count=fp+tn,
                recall=tp/(tp+fn) if tp+fn else None,
                precision=tp/(tp+fp) if tp+fp else None)


def choose(development):
    # Holdout must not influence selection, tie breaks or threshold tuning.
    if not development: raise ValueError('Missing development results')
    return max(development,key=lambda x:(-x['metrics']['fp'],x['metrics']['tp'],
                                        -x['metrics']['fn'],x['threshold']))


def load_set(path='data/library_calibration_set.json'):
    data = json.loads(Path(path).read_text())
    frames = data['frames']
    ids = {split:{r['clip_id'] for r in frames if r['split']==split}
           for split in ('development','holdout')}
    if ids['development'] & ids['holdout']:
        raise ValueError('Source clip overlap between development and holdout')
    if not all(r['label'] in ('combat','inactive') and r['split'] in ids for r in frames):
        raise ValueError('Invalid labels or splits')
    for split in ids:
        if {r['label'] for r in frames if r['split']==split} != {'combat','inactive'}:
            raise ValueError('Both classes required in each split')
    return data


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


def save_crop_preview(images, records, crop):
    sheet = Image.new('RGB',(960,((len(images)+2)//3)*220),'#171717')
    draw = ImageDraw.Draw(sheet)
    for i,(im,r) in enumerate(zip(images,records)):
        thumb = im.copy();thumb.thumbnail((320,180))
        x,y=(i%3)*320,(i//3)*220
        sheet.paste(thumb,(x+(320-thumb.width)//2,y))
        draw.text((x+4,y+183),f"clip {r['review_clip_number']:02} {r['source_seconds']:.2f}s",fill='white')
        draw.text((x+4,y+199),f"{r['label']} | {r['split']}",fill='white')
    sheet.save(OUT/f'controls_{crop}.jpg',quality=90)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    report = dict(mode='frozen-frame calibration',openai_calls=0,approved_for_production=False,
                  model_revisions={k:v[1] for k,v in local.REPOS.items()})
    try:
        data = load_set(); records=data['frames']; report['label_scope']=data['label_scope']
        report['limitations']=data['limitations']; report['frame_count']=len(records)
        images=[]
        for r in records:
            with Image.open(r['file']) as im:images.append(im.convert('RGB'))
        model=local.engine(); configurations=[]; all_scores=[]
        for crop,bounds in CROPS.items():
            cropped=[crop_image(im,bounds) for im in images]
            save_crop_preview(cropped,records,crop)
            for bank,prompts in BANKS.items():
                with model.lock: values=score(model,cropped,prompts)
                rows=[dict(r,margin=float(v),crop=crop,prompt_bank=bank) for r,v in zip(records,values)]
                all_scores.extend(rows)
                dev=[r for r in rows if r['split']=='development']
                for threshold in THRESHOLDS:
                    configurations.append(dict(crop=crop,prompt_bank=bank,threshold=threshold,
                                               metrics=metrics(dev,threshold)))
        selected=choose(configurations)
        holdout=[r for r in all_scores if r['split']=='holdout' and
                 r['crop']==selected['crop'] and r['prompt_bank']==selected['prompt_bank']]
        check=metrics(holdout,selected['threshold'])
        report.update(development_comparison=configurations,selected_by_development=selected,
                      holdout_check=check,frame_scores=all_scores,
                      next_step='Larger labelled temporal test required; do not promote these settings')
        report['small_test_separation_found'] = (selected['metrics']['fp']==0 and
             selected['metrics']['fn']==0 and check['fp']==0 and check['fn']==0)
        lines=['# Library calibration results','',
               'OpenAI calls: 0. Production approvals: 0.',
               'Labels test visible weapon firing in still frames, not sustained gunfights.','',
               f"Development-selected crop: {selected['crop']}; prompts: {selected['prompt_bank']}; margin: {selected['threshold']}",
               f"Development counts: {selected['metrics']}",f'Untuned holdout counts: {check}','',
               'Do not promote a threshold based on this tiny set. Check crop previews and frame_scores.',
               'A zero-false-positive configuration that misses every positive is not a useful detector.']
        (OUT/'RESULTS.md').write_text('\n'.join(lines)+'\n')
        print('\n'.join(lines))
    except Exception as exc:
        report['fatal_error']=dict(type=type(exc).__name__,message=str(exc)[:500])
        raise
    finally:
        (OUT/'calibration_report.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
