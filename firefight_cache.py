"""Persist screening results and permanent automatic-pipeline spending exclusions."""
import copy
import json
import math
import os
import sys
import time
from pathlib import Path

PATH = Path("firefight_screen_cache.json")
POLICY = "direct-gunfight-window-v1"


def media(row):
    return row.get("media_url") or row.get("playlist_url") or row.get("prevalidated_media_url") or ""


def retained(entry):
    return entry.get("permanent_exclusion") is True or (entry.get("expires_at") or 0) > time.time()


def load(path=PATH):
    path = Path(path)
    if not path.exists():
        return {"version": 1, "entries": {}}
    data = json.loads(path.read_text())
    if not isinstance(data.get("entries"), dict):
        raise RuntimeError(f"Invalid screening cache: {path}")
    # These entries were created only after valid, completed visual rejection.
    # Upgrade them so already-failed sources do not restart paid reviews.
    for entry in data["entries"].values():
        old_paid_reject = (entry.get("passed") is False
                           and type(entry.get("verdict", {}).get("direct_gunfight")) is bool
                           and type(entry.get("verdict", {}).get("active_samples")) is int)
        if entry.get("temporary_edit_cooldown") is True or old_paid_reject:
            entry.update(permanent_exclusion=True, expires_at=None, passed=False,
                         exclusion_basis="prior_saved_action_rejection",
                         temporary_edit_cooldown=False)
            entry["verdict"] = {"reason": "Automatic spending exclusion after prior rejected action edit"}
    return data


def write(data, path=PATH):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, path)


def current(row, cache=None):
    cache = load() if cache is None else cache
    entry = cache["entries"].get(str(row.get("clip_id", "")))
    if not entry:
        return None
    # Permanent exclusions are keyed by stable clip ID; URL/policy changes and
    # expired signed media links must not silently restart automatic spending.
    if entry.get("permanent_exclusion") is True:
        return entry
    if (entry.get("policy") != POLICY or not retained(entry)
            or entry.get("media_url", "") != media(row)):
        return None
    return entry


def exclude(row, basis, reason, reviewed_windows=None):
    clip_id = str(row.get("clip_id", "")).strip()
    if not clip_id:
        return False
    data = load()
    previous = data["entries"].get(clip_id, {})
    if previous.get("permanent_exclusion") is True:
        return True
    data["entries"] = {k: v for k, v in data["entries"].items() if retained(v)}
    data["entries"][clip_id] = {
        "policy": POLICY, "updated_at": time.time(), "expires_at": None,
        "media_url": media(row), "passed": False, "permanent_exclusion": True,
        "window_start": row.get("local_window_start"), "exclusion_basis": basis,
        "reviewed_windows": reviewed_windows or [],
        "verdict": {"reason": str(reason)},
        "limitation": "Automatic spending exclusion; not proof that every source frame lacks combat.",
    }
    write(data)
    return True


def remember(row, verdict, passed):
    clip_id = str(row.get("clip_id", "")).strip()
    if not clip_id:
        return
    if not passed:
        start = row.get("local_window_start", 0)
        exclude(row, "strongest_preview_window_failed_paid_action_screen",
                verdict.get("reason", "No qualifying gunfight in reviewed window"),
                [{"stage": "active_firefight_prescreener.py", "start_original": start,
                  "nominal_window_seconds": row.get("local_window_seconds",35), "verdict": copy.deepcopy(verdict)}])
        return
    data = load()
    if data["entries"].get(clip_id, {}).get("permanent_exclusion") is True:
        return
    now = time.time()
    data["entries"] = {k: v for k, v in data["entries"].items() if retained(v)}
    data["entries"][clip_id] = {
        "policy": POLICY, "updated_at": now, "expires_at": now + 86400,
        "media_url": media(row), "passed": True, "verdict": verdict,
        "window_start": row.get("local_window_start"),
        "window_seconds": row.get("local_window_seconds"),
        "local_screen_policy": row.get("library_screen_tag"),
    }
    write(data)


def defer_failed_edit(row, action_result, hours=6, source_offset=0):
    """Compatibility name: a valid rejected edit now creates a permanent skip."""
    if (action_result.get("passed") is not False
            or action_result.get("valid_evidence") is not True
            or action_result.get("response_status") != "completed"):
        return False
    windows = []
    for result in action_result.get("attempts", [action_result]):
        if result.get("valid_evidence") is not True or result.get("response_status") != "completed":
            continue
        try:
            start, end = float(result["segment_start"]), float(result["segment_end"])
            offset = float(source_offset)
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (start, end, offset)) or end <= start:
            continue
        windows.append({"stage": "action_segment_gate.py", "start_local": start,
                        "end_local": end, "source_offset": offset,
                        "start_original": start + offset, "end_original": end + offset,
                        "reason": result.get("reason", "Action rejection")})
    return exclude(row, "candidate_exhausted_bounded_action_edit_review",
                   action_result.get("reason", "Selected fight edit failed"), windows)


def merge(generated, latest=PATH):
    result = load(latest)
    for key, entry in load(generated)["entries"].items():
        old = result["entries"].get(key, {})
        if old.get("permanent_exclusion") is True:
            continue
        if entry.get("permanent_exclusion") is True or entry.get("updated_at", 0) >= old.get("updated_at", 0):
            result["entries"][key] = entry
    result["entries"] = {k: v for k, v in result["entries"].items() if retained(v)}
    write(result, latest)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "merge":
        raise SystemExit("Usage: python firefight_cache.py merge GENERATED LATEST")
    merge(sys.argv[2], sys.argv[3])
