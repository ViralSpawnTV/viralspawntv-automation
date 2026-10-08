"""Short-lived, mergeable paid-screen verdicts; network failures are never cached."""
import json
import os
import sys
import time
from pathlib import Path

PATH = Path("firefight_screen_cache.json")
POLICY = "direct-gunfight-window-v1"


def load(path=PATH):
    path = Path(path)
    if not path.exists():
        return {"version": 1, "entries": {}}
    data = json.loads(path.read_text())
    if not isinstance(data.get("entries"), dict):
        raise RuntimeError(f"Invalid screening cache: {path}")
    return data


def write(data, path=PATH):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, path)


def current(row, cache=None):
    cache = load() if cache is None else cache
    entry = cache["entries"].get(str(row.get("clip_id", "")))
    media = row.get("media_url") or row.get("playlist_url") or row.get("prevalidated_media_url") or ""
    if (not entry or entry.get("policy") != POLICY
            or entry.get("expires_at", 0) <= time.time()
            or entry.get("media_url", "") != media):
        return None
    return entry


def remember(row, verdict, passed):
    clip_id = str(row.get("clip_id", ""))
    if not clip_id:
        return
    now = time.time()
    data = load()
    data["entries"] = {key: entry for key, entry in data["entries"].items()
                       if entry.get("expires_at", 0) > now}
    data["entries"][clip_id] = {
        "policy": POLICY, "updated_at": now,
        "expires_at": now + (86400 if passed else 3 * 86400),
        "media_url": row.get("media_url") or row.get("playlist_url") or row.get("prevalidated_media_url") or "",
        "passed": passed, "verdict": verdict,
        "window_start": row.get("local_window_start"),
    }
    write(data)


def defer_failed_edit(row, action_result, hours=6):
    """Temporarily suppress a source after a completed, valid action rejection.

    This is not a permanent source-quality verdict. API errors, malformed
    assessments and unrelated render failures must never create a cooldown.
    """
    if (action_result.get("passed") is not False
            or action_result.get("valid_evidence") is not True
            or action_result.get("response_status") != "completed"):
        return False
    clip_id = str(row.get("clip_id", "")).strip()
    if not clip_id:
        return False
    media = row.get("media_url") or row.get("playlist_url") or row.get("prevalidated_media_url") or ""
    if not media:
        return False
    now = time.time()
    data = load()
    data["entries"][clip_id] = {
        "policy": POLICY, "updated_at": now,
        "expires_at": now + min(24, max(1, hours)) * 3600,
        "media_url": media, "passed": False,
        "window_start": row.get("local_window_start"),
        "temporary_edit_cooldown": True,
        "verdict": {"reason": "Temporary cooldown after rejected edit: "
                    + str(action_result.get("reason", "gunfight check failed"))},
    }
    write(data)
    return True


def merge(generated, latest=PATH):
    result = load(latest)
    for key, entry in load(generated)["entries"].items():
        if entry.get("updated_at", 0) >= result["entries"].get(key, {}).get("updated_at", 0):
            result["entries"][key] = entry
    result["entries"] = {key: entry for key, entry in result["entries"].items()
                         if entry.get("expires_at", 0) > time.time()}
    write(result, latest)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "merge":
        raise SystemExit("Usage: python firefight_cache.py merge GENERATED LATEST")
    merge(sys.argv[2], sys.argv[3])
