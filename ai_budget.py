"""Shared, fail-closed API workload limits for a single production run.

These are workload caps, not a dollar quote. Actual tokens are recorded.
Every attempted request is charged to the ledger before network access;
failed calls do not refund reservations. SDK retries are disabled.
"""
import functools
import fcntl
import json
import os
import time
import wave
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


LIMITS = {
    "requests": 28, "response_requests": 18, "images": 240,
    "reserved_output_tokens": 72000, "text_bytes": 800000,
    "transcription_seconds": 300, "speech_characters": 800,
}


def ledger_path():
    value = os.environ.get("VIRALSPAWN_AI_LEDGER")
    if not value:
        raise BudgetExceeded("Shared AI ledger is not configured")
    return Path(value)


def change_ledger(callback):
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Never recreate a lost ledger: that could silently reset the run budget.
        data = json.loads(path.read_text())
        result = callback(data)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, path)
        return result


def input_size(value):
    images = text_bytes = 0
    if isinstance(value, list):
        for item in value:
            count, size = input_size(item)
            images += count
            text_bytes += size
    elif isinstance(value, dict):
        if value.get("type") in {"input_image", "image_url"}:
            url = value.get("image_url", "")
            if isinstance(url, dict):
                url = url.get("url", "")
            if not isinstance(url, str) or not url.startswith("data:image/") or len(url) > 2000000:
                raise BudgetExceeded("Only bounded local image inputs are supported")
            return 1, 0
        if value.get("type") in {"input_file", "input_audio"}:
            raise BudgetExceeded("Unmetered file/audio response inputs are disabled")
        for key, item in value.items():
            if key not in {"image_url", "url"}:
                count, size = input_size(item)
                images += count
                text_bytes += size
    elif isinstance(value, str):
        text_bytes = len(value.encode("utf-8"))
    return images, text_bytes


def audio_seconds(file):
    if isinstance(file, tuple):
        file = file[1]
    if isinstance(file, (str, Path)):
        with wave.open(str(file), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    if not hasattr(file, "seek") or not hasattr(file, "tell"):
        raise BudgetExceeded("Cannot measure transcription audio duration")
    position = file.tell()
    try:
        file.seek(0)
        with wave.open(file, "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    finally:
        file.seek(position)


def reserve(kind, kwargs):
    amounts = {"requests": 1}
    if kind == "responses":
        if kwargs.get("stream") or kwargs.get("background"):
            raise BudgetExceeded("Streaming/background responses are not supported by this budget")
        if any(kwargs.get(key) for key in ("tools", "previous_response_id", "conversation")):
            raise BudgetExceeded("Unbounded tools or hidden conversation context are disabled")
        output = min(6000, int(kwargs.get("max_output_tokens") or 6000))
        if output < 1:
            raise BudgetExceeded("Invalid output limit")
        kwargs["max_output_tokens"] = output
        images, size = input_size(kwargs.get("input"))
        size += len(str(kwargs.get("instructions", "")).encode())
        amounts.update(response_requests=1, images=images,
                       reserved_output_tokens=output, text_bytes=size)
    elif kind == "transcription":
        if kwargs.get("stream"):
            raise BudgetExceeded("Streaming transcription is disabled")
        try:
            amounts["transcription_seconds"] = audio_seconds(kwargs["file"])
        except Exception as exc:
            raise BudgetExceeded(f"Cannot safely meter audio: {exc}") from exc
    elif kind == "speech":
        amounts["speech_characters"] = len(str(kwargs.get("input", "")))
    else:
        raise BudgetExceeded(f"Unmetered OpenAI endpoint: {kind}")

    def update(data):
        if data.get("exhausted"):
            return data.get("stop_reason", "AI BUDGET STOP: run budget exhausted")
        for key, amount in amounts.items():
            if data["reserved"].get(key, 0) + amount > data["limits"][key]:
                data["exhausted"] = True
                data["stop_reason"] = f"AI BUDGET STOP: {key} cap {data['limits'][key]} reached"
                return data["stop_reason"]
        for key, amount in amounts.items():
            data["reserved"][key] = data["reserved"].get(key, 0) + amount
        index = len(data["calls"])
        data["calls"].append({"kind": kind, "model": kwargs.get("model"),
                              "reserved": amounts, "status": "reserved", "time": time.time()})
        return index
    result = change_ledger(update)
    if isinstance(result, str):
        raise BudgetExceeded(result)
    return result


def complete(index, response=None, error=None):
    usage = getattr(response, "usage", None)
    if usage and hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    elif usage and not isinstance(usage, dict):
        usage = {key: getattr(usage, key, 0) for key in ("input_tokens", "output_tokens", "total_tokens")}
    def update(data):
        row = data["calls"][index]
        row["status"] = "failed" if error else "completed"
        if error:
            # Do not include prompts, keys, media URLs, or full exception payloads.
            row["error_type"] = type(error).__name__
        if usage:
            row["usage"] = usage
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                data["actual_tokens"][key] += int(usage.get(key, 0) or 0)
    change_ledger(update)


def wrap(method, kind):
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        if args:
            raise BudgetExceeded("Positional API arguments cannot be metered")
        # with_options(max_retries=...) cannot re-enable invisible SDK attempts.
        self._client.max_retries = 0
        index = reserve(kind, kwargs)
        try:
            response = method(self, **kwargs)
        except Exception as exc:
            complete(index, error=exc)
            raise
        complete(index, response=response)
        return response
    return guarded


def install():
    if os.environ.get("VIRALSPAWN_AI_BUDGET_ACTIVE") != "1":
        return
    import openai
    if getattr(openai, "_viralspawn_budget_installed", False):
        return
    from openai.resources.responses.responses import Responses
    from openai.resources.audio.transcriptions import Transcriptions
    from openai.resources.audio.speech import Speech
    from openai.resources.chat.completions.completions import Completions
    Responses.create = wrap(Responses.create, "responses")
    Transcriptions.create = wrap(Transcriptions.create, "transcription")
    Speech.create = wrap(Speech.create, "speech")
    Completions.create = wrap(Completions.create, "unsupported_chat")
    openai._viralspawn_budget_installed = True
