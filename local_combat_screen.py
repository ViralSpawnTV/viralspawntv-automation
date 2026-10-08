"""CPU-only image/audio screening. Downloads model weights; no inference APIs.

CLIP similarities and AudioSet scores are provisional evidence, not certified
combat detection or a calibrated probability that a Short will pass review.
"""
import argparse
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

POLICY = 'local-gameplay-gunfire-v2'
CACHE = Path(os.getenv('LOCAL_MODEL_CACHE', str(Path.home() / '.cache/viralspawntv-models')))
REPOS = {
    'vision': ('Xenova/clip-vit-base-patch32', 'd15189d7028b43f1d3e65039190477f6af591c2a'),
    'audio': ('Xenova/ast-finetuned-audioset-10-10-0.4593', '249a1fbf0286b40e7f1ed687a8ae396997bf7dc6'),
}
FRAME_FPS = 0.5
AUDIO_RATE = 16000
GUNFIRE_MIN = 0.08
GAMEPLAY_MARGIN_MIN = 0.01
GAMEPLAY_RATIO_MIN = 0.70
GUNFIRE_RATIO_MIN = 0.50
MAX_INACTIVE_SECONDS = 8.0
WINDOW_LENGTHS = (19.0, 25.0, 35.0)
_LOCK = threading.Lock()
_ENGINE = None


def verified(row):
    """Validate stored evidence before production spends on this candidate."""
    import math
    e = row.get('library_combat_evidence', {})
    try:
        numbers = [row['local_window_start'], row['local_window_seconds'],
                   row['local_firefight_score'], e['gameplay_ratio'], e['gunfire_ratio'],
                   e['longest_inactive_seconds']]
        if not all(type(v) in (int,float) and math.isfinite(v) for v in numbers):
            return False
        return (row.get('library_screen_tag') == POLICY
            and row.get('library_visual_approved') is False
            and row.get('library_local_pass') is True and e.get('passed') is True
            and row.get('library_model_revisions') == {k:v[1] for k,v in REPOS.items()}
            and row['local_window_seconds'] in WINDOW_LENGTHS
            and row['local_window_start'] >= 0 and 0 <= row['local_firefight_score'] <= 100
            and e['gameplay_ratio'] >= GAMEPLAY_RATIO_MIN
            and e['gunfire_ratio'] >= GUNFIRE_RATIO_MIN
            and e.get('gunfire_bins',0) >= 3 and e.get('audio_bins',0) >= 3
            and e['longest_inactive_seconds'] <= MAX_INACTIVE_SECONDS)
    except (KeyError,TypeError,ValueError):
        return False


def prepare_models():
    from huggingface_hub import hf_hub_download
    result = {}
    for kind, (repo, revision) in REPOS.items():
        names = ['config.json', 'preprocessor_config.json']
        if kind == 'vision':
            names += ['tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json',
                      'onnx/vision_model_quantized.onnx', 'onnx/text_model_quantized.onnx']
        else:
            names += ['onnx/model_quantized.onnx']
        directory = CACHE / kind / revision
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            hf_hub_download(repo_id=repo, filename=name, revision=revision, local_dir=directory)
        result[kind] = directory
    return result


def audio_features(samples):
    # Same NumPy filterbank settings as Hugging Face ASTFeatureExtractor's
    # non-TorchAudio path, with NumPy padding so PyTorch is not required.
    from transformers.audio_utils import mel_filter_bank, spectrogram, window_function
    filters = mel_filter_bank(257, 128, 20, 8000, 16000, norm=None,
                              mel_scale='kaldi', triangularize_in_mel_space=True)
    values = spectrogram(np.asarray(samples, dtype=np.float32),
        window_function(400, 'hann', periodic=False), frame_length=400,
        hop_length=160, fft_length=512, power=2.0, center=False,
        preemphasis=0.97, mel_filters=filters, log_mel='log',
        mel_floor=1.192092955078125e-07, remove_dc_offset=True).T
    values = values[:1024]
    values = np.pad(values, ((0, max(0, 1024-len(values))), (0, 0)))
    return ((values - (-4.2677393)) / (4.5689974 * 2)).astype(np.float32)[None]


class Engine:
    def __init__(self):
        import onnxruntime as ort
        from transformers import CLIPImageProcessor, CLIPTokenizerFast
        paths = prepare_models()
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(2, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        self.vision = ort.InferenceSession(str(paths['vision']/'onnx/vision_model_quantized.onnx'),
            sess_options=options, providers=['CPUExecutionProvider'])
        self.text = ort.InferenceSession(str(paths['vision']/'onnx/text_model_quantized.onnx'),
            sess_options=options, providers=['CPUExecutionProvider'])
        self.audio = ort.InferenceSession(str(paths['audio']/'onnx/model_quantized.onnx'),
            sess_options=options, providers=['CPUExecutionProvider'])
        self.processor = CLIPImageProcessor.from_pretrained(paths['vision'], local_files_only=True)
        self.tokenizer = CLIPTokenizerFast.from_pretrained(paths['vision'], local_files_only=True)
        config = json.loads((paths['audio']/'config.json').read_text())
        labels = config['id2label']
        self.gun_indices = [int(k) for k,v in labels.items()
                            if v.casefold() in ('gunshot, gunfire', 'machine gun', 'fusillade')]
        if not self.gun_indices:
            raise RuntimeError('Audio model lacks required gunfire labels')
        self.text_cache = {}
        self.lock = threading.Lock()
        self._self_test()

    @staticmethod
    def output(session, inputs, name):
        feed = {i.name: inputs[i.name] for i in session.get_inputs()}
        names = [o.name for o in session.get_outputs()]
        return session.run([name if name in names else names[0]], feed)[0]

    def text_vectors(self, game):
        if game not in self.text_cache:
            prompts = [
                f'a screenshot of {game} shooter video game gameplay, with a weapon and game HUD',
                'a screenshot of a first person shooter video game, a gun, crosshair and game HUD',
                'a screenshot of a third person shooter video game with weapons and game HUD',
                'a video game buy menu or inventory screen with item icons and text',
                'a video game loading screen, lobby menu or character selection menu',
                'a photograph of real people in a room, a webcam or a talking livestream',
                'a photograph of a real world outdoor scene, not a video game',
                'a screenshot of a non shooter platform game, sports game or racing game',
            ]
            inputs = self.tokenizer(prompts, padding='max_length', truncation=True,
                                     max_length=77, return_tensors='np')
            vectors = self.output(self.text, dict(inputs), 'text_embeds')
            vectors /= np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-8)
            self.text_cache[game] = vectors
        return self.text_cache[game]

    def image_margins(self, images, game):
        text = self.text_vectors(game)
        output = []
        for i in range(0, len(images), 8):
            pixels = self.processor(images=images[i:i+8], return_tensors='np')['pixel_values']
            features = self.output(self.vision, {'pixel_values':pixels}, 'image_embeds')
            features /= np.maximum(np.linalg.norm(features, axis=-1, keepdims=True), 1e-8)
            similarity = features @ text.T
            output.extend((similarity[:,:3].max(axis=1)-similarity[:,3:].max(axis=1)).tolist())
        return output

    def audio_scores(self, samples):
        output = []
        # Non-overlapping five-second bins keep gunfire evidence from being
        # counted multiple times merely because overlapping inputs repeat it.
        for start in range(0, len(samples), AUDIO_RATE*5):
            chunk = samples[start:start+AUDIO_RATE*5]
            if len(chunk) < AUDIO_RATE*2:
                break
            logits = self.output(self.audio, {'input_values':audio_features(chunk)}, 'logits')
            probabilities = 1/(1+np.exp(-np.clip(logits[0], -40, 40)))
            output.append({'start':start/AUDIO_RATE,
                           'end':min(len(samples)/AUDIO_RATE, start/AUDIO_RATE+5),
                           'score':float(probabilities[self.gun_indices].max())})
        return output

    def _self_test(self):
        # Exercise all real model inputs/outputs before discovery or paid work.
        margins = self.image_margins([Image.new('RGB',(224,224))], 'Valorant')
        scores = self.audio_scores(np.zeros(AUDIO_RATE*5, dtype=np.float32))
        if not margins or not np.isfinite(margins).all() or not scores or not np.isfinite(scores[0]['score']):
            raise RuntimeError('Local model inference self-test failed')


def engine():
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = Engine()
    return _ENGINE


def select_window(margins, audio, duration):
    if not np.isfinite(margins).all() or any(not np.isfinite(b['score']) for b in audio):
        raise ValueError('Non-finite local model evidence')
    choices = []
    for seconds in WINDOW_LENGTHS:
        if duration < seconds:
            continue
        starts = list(np.arange(0, max(0,duration-seconds)+0.01, 2.0))
        starts.append(max(0,duration-seconds))
        for start in sorted(set(starts)):
            end = start+seconds
            image_values = [v for i,v in enumerate(margins)
                            if start <= i/FRAME_FPS < end]
            bins = [b for b in audio if b['start'] >= start and b['end'] <= end]
            if len(image_values)<8 or len(bins)<3:
                continue
            gameplay = sum(v>=GAMEPLAY_MARGIN_MIN for v in image_values)/len(image_values)
            hits = sum(b['score']>=GUNFIRE_MIN for b in bins)
            gun_ratio = hits/len(bins)
            # Count gaps from audio bins plus partial margins at each edge.
            idle = longest = max(0,bins[0]['start']-start)
            for b in bins:
                idle = idle+(b['end']-b['start']) if b['score']<GUNFIRE_MIN else 0
                longest=max(longest,idle)
            longest=max(longest,idle+max(0,end-bins[-1]['end']))
            avg_gun=sum(b['score'] for b in bins)/len(bins)
            score = 100*(0.4*gameplay+0.4*gun_ratio+0.2*avg_gun)
            passed=bool(gameplay>=GAMEPLAY_RATIO_MIN and hits>=3
                    and gun_ratio>=GUNFIRE_RATIO_MIN and longest<=MAX_INACTIVE_SECONDS)
            choices.append((passed, round(score,6), round(avg_gun,6), -longest, -seconds, -start,
                {'start':round(float(start),3), 'seconds':seconds, 'end':round(float(end),3),
                 'score':round(score,3),'gameplay_ratio':round(gameplay,3),
                 'gunfire_bins':hits,'audio_bins':len(bins),
                 'gunfire_ratio':round(gun_ratio,3),'mean_gunfire_score':round(avg_gun,4),
                 'longest_inactive_seconds':round(float(longest),3),'passed':passed}))
    if not choices:
        return {'passed':False,'reason':'Insufficient aligned audio/visual evidence'}
    return max(choices, key=lambda x:x[:6])[-1]


def screen(row):
    e = engine()
    url=row.get('prevalidated_media_url') or row.get('media_url') or row.get('playlist_url')
    if not url:
        raise RuntimeError('Missing clip media URL')
    with tempfile.TemporaryDirectory(prefix='viralspawntv-local-') as td:
        preview=Path(td)/'preview.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-rw_timeout','12000000','-i',str(url),
            '-t','120','-map','0:v:0','-map','0:a:0?','-vf','scale=384:-2','-r','8',
            '-c:v','libx264','-preset','ultrafast','-crf','30','-c:a','aac',str(preview)],
            check=True,capture_output=True,timeout=90)
        probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration',
            '-of','json',str(preview)],check=True,capture_output=True,text=True,timeout=15)
        duration=min(120,float(json.loads(probe.stdout)['format']['duration']))
        subprocess.run(['ffmpeg','-v','error','-i',str(preview),'-vf',f'fps={FRAME_FPS}',
            str(Path(td)/'frame_%03d.jpg')],check=True,capture_output=True,timeout=30)
        images=[]
        for p in sorted(Path(td).glob('frame_*.jpg')):
            with Image.open(p) as im:images.append(im.convert('RGB'))
        # Missing audio is an infrastructure/evidence error, not a permanent
        # clip-wide action rejection; collector stores a retryable error.
        audio=subprocess.run(['ffmpeg','-v','error','-i',str(preview),'-vn','-ac','1',
            '-ar',str(AUDIO_RATE),'-f','f32le','-'],check=True,capture_output=True,timeout=30).stdout
        samples=np.frombuffer(audio,dtype='<f4')
        if len(images)<10 or len(samples)<AUDIO_RATE*19:
            raise RuntimeError('Incomplete preview/audio evidence')
        with e.lock:
            margins=e.image_margins(images,str(row.get('game','shooter')))
            scores=e.audio_scores(samples)
        known_seconds=float(row.get('source_duration_seconds') or row.get('prevalidated_duration_seconds') or duration)
        evidence=select_window(margins,scores,min(duration,len(samples)/AUDIO_RATE,known_seconds))
    result=dict(row, library_screen_tag=POLICY, library_visual_approved=False,
        library_local_pass=bool(evidence['passed']), library_scanned_at=time.time(),
        library_model_revisions={k:v[1] for k,v in REPOS.items()},
        library_screen_method='local CLIP gameplay contrast plus AudioSet gunfire tags',
        library_combat_evidence=evidence, local_frame_count=len(images),
        local_shooter_game=bool(evidence['passed']), preview_seconds=duration)
    if 'start' in evidence:
        result.update(local_window_start=evidence['start'], local_window_seconds=evidence['seconds'],
            library_window_end=evidence['end'], local_firefight_score=evidence['score'])
    return result, bool(evidence['passed'])


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--clip-json');args=parser.parse_args()
    if args.prepare:
        status={'models':{k:{'repo':v[0],'revision':v[1]} for k,v in REPOS.items()},'openai_calls':0}
        try:
            engine();status['passed']=True
            print('LOCAL MODEL SELF-TEST PASSED; OpenAI calls: 0')
        except Exception as exc:
            status.update(passed=False,error_type=type(exc).__name__,reason=str(exc)[:500])
            raise
        finally:
            output=Path('work/local_model_setup_result.json');output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(status,indent=2)+'\n')
    elif args.clip_json:
        row=json.loads(Path(args.clip_json).read_text());result,passed=screen(row)
        print(json.dumps(result,indent=2))
    else:parser.error('Use --prepare or --clip-json FILE')
