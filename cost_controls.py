"""Lower-cost drafts and exact-audio transcript reuse within one run.

Source/music/final gates and structured action verdicts keep their requested
model. No cached transcript can be used for a different waveform or prompt.
"""
import functools
import hashlib
import json
import math
import os
import time
import wave
from pathlib import Path


CHEAP_STAGES = {"active_firefight_prescreener.py", "viral_prescreener.py"}
DRAFT_MODEL = "gpt-5.4-mini"


def route_response(kwargs):
    if os.environ.get("VIRALSPAWN_COST_MODE", "balanced") == "original":
        return
    stage = Path(os.environ.get("VIRALSPAWN_AI_STAGE", "unknown")).name
    # Explicit custom models are respected. Unknown stages keep their model.
    if kwargs.get("model") not in {"gpt-5.6", "gpt-5.6-sol"}:
        return
    structured = bool((kwargs.get("text") or {}).get("format"))
    if stage in CHEAP_STAGES or (stage == "production_test.py" and not structured):
        kwargs["model"] = DRAFT_MODEL
        # Enough room for JSON plus bounded reasoning; never raise a caller cap.
        kwargs["max_output_tokens"] = min(4000, int(kwargs.get("max_output_tokens") or 4000))
        kwargs.setdefault("reasoning", {"effort": "low"})


def transcript_key(kwargs):
    supported = {"model", "file", "response_format", "timestamp_granularities",
                 "language", "prompt", "temperature", "timeout"}
    if (kwargs.get("model") != "whisper-1" or set(kwargs) - supported
            or kwargs.get("response_format", "json") not in {"json", "verbose_json", "text"}
            or kwargs.get("timestamp_granularities", ["segment"]) not in ([], ["segment"])):
        return None
    file = kwargs["file"]
    if isinstance(file, tuple):
        file = file[1]
    opened = isinstance(file, (str, Path))
    if opened:
        file = open(file, "rb")
    if not hasattr(file, "seek") or not hasattr(file, "tell"):
        return None
    position = file.tell()
    try:
        file.seek(0)
        digest = hashlib.sha256()
        with wave.open(file, "rb") as audio:
            metadata = [audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()]
            if metadata[2] <= 0 or metadata[3] <= 0:
                return None
            digest.update(json.dumps(metadata).encode())
            # Hash PCM, not WAV header metadata or the filename.
            total = 0
            while chunk := audio.readframes(65536):
                digest.update(chunk)
                total += len(chunk)
            if total != metadata[0] * metadata[1] * metadata[3]:
                return None
        options = {k: kwargs[k] for k in ("model", "language", "prompt", "temperature") if k in kwargs}
        options["cache_version"] = 1
        digest.update(json.dumps(options, sort_keys=True, allow_nan=False).encode())
        return digest.hexdigest(), metadata[3] / metadata[2]
    except (OSError, ValueError, TypeError, wave.Error, EOFError):
        return None
    finally:
        file.seek(position)
        if opened:
            file.close()


def valid_transcript(data, seconds):
    if (not isinstance(data, dict) or not isinstance(data.get("text"), str)
            or not isinstance(data.get("segments"), list)
            or not isinstance(data.get("language"), str)):
        return False
    duration = data.get("duration")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or abs(duration - seconds) > 2):
        return False
    for segment in data["segments"]:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            return False
        start, end = segment.get("start"), segment.get("end")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (start, end)) or not 0 <= start <= end <= seconds + 2:
            return False
    return True


def adapt_transcript(data, requested):
    from openai.types.audio import Transcription, TranscriptionVerbose
    if requested == "text":
        return data["text"]
    if requested == "verbose_json":
        return TranscriptionVerbose(**data)
    return Transcription(text=data["text"])


def cache_transcriptions(metered_method):
    @functools.wraps(metered_method)
    def cached(self, *args, **kwargs):
        directory = os.environ.get("VIRALSPAWN_TRANSCRIPT_CACHE")
        key = transcript_key(kwargs) if directory and not args else None
        if not key:
            return metered_method(self, *args, **kwargs)
        fingerprint, seconds = key
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        path = root / (fingerprint + ".json")
        requested = kwargs.get("response_format", "json")
        # Same-audio calls from parallel helpers wait for the first paid result.
        import fcntl
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                entry = json.loads(path.read_text())
                data = entry["transcription"]
                if entry.get("fingerprint") == fingerprint and valid_transcript(data, seconds):
                    response = adapt_transcript(data, requested)
                    from ai_budget import change_ledger
                    def hit(ledger):
                        stats = ledger.setdefault("transcript_cache", {"hits": 0, "avoided_seconds": 0, "events": []})
                        stats["hits"] += 1
                        stats["avoided_seconds"] += seconds
                        stats["events"].append({"stage": os.environ.get("VIRALSPAWN_AI_STAGE", "unknown"),
                                                "seconds": seconds, "time": time.time()})
                    change_ledger(hit)
                    print(f"TRANSCRIPT REUSED: {seconds:.2f}s; no paid transcription")
                    return response
            except (OSError, ValueError, KeyError, TypeError):
                pass
            options = dict(kwargs)
            options["response_format"] = "verbose_json"
            options["timestamp_granularities"] = ["segment"]
            response = metered_method(self, **options)
            data = response.model_dump(mode="json")
            if not valid_transcript(data, seconds):
                # Do not create a reusable result from malformed or failed data.
                raise ValueError("Invalid timestamped transcription; not cached")
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"fingerprint": fingerprint, "transcription": data}, allow_nan=False))
            os.replace(temp, path)
            return adapt_transcript(data, requested)
    return cached
