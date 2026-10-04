"""Approved-file music rotation and FFmpeg mixing for ViralSpawnTV."""
import hashlib
import json
import os
import random
import subprocess
from pathlib import Path


def choose_music(root, seconds):
    root = Path(root)
    folder = Path(os.getenv("MUSIC_DIR", str(root / "assets" / "music")))
    manifest = json.loads((root / "music_manifest.json").read_text())
    tracks = []
    for row in manifest["tracks"]:
        path = folder / row["filename"]
        if path.is_file():
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise RuntimeError(f"Music file differs from supplied track: {path.name}")
            tracks.append((row, path))
    if not tracks:
        raise RuntimeError(f"No approved MP3s found in {folder}. Extract the music pack there.")
    tracks.sort(key=lambda pair: pair[0]["filename"])
    random.Random(13013).shuffle(tracks)
    state_path = root / "music_rotation_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    # GitHub run number provides rotation even before state is committed.
    cursor = int(state.get("cursor", max(0, int(os.getenv("GITHUB_RUN_NUMBER", "1")) - 1)))
    index = cursor % len(tracks)
    if len(tracks) > 1 and tracks[index][0]["filename"] == state.get("last_track"):
        cursor += 1
        index = cursor % len(tracks)
    row, path = tracks[index]
    length = float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path)], text=True))
    rng = random.Random(f"{row['sha256']}:{cursor}:{os.getenv('GITHUB_RUN_ID', '')}")
    offset = round(rng.uniform(0, max(0, length - seconds - 1)), 3)
    return {"path": str(path), "filename": row["filename"], "offset": offset,
            "state_path": str(state_path), "next_cursor": cursor + 1}


def music_filter(index, seconds, beats):
    gain = float(os.getenv("MUSIC_VOLUME", "0.12"))
    if not 0 <= gain <= 1:
        raise ValueError("MUSIC_VOLUME must be between 0 and 1")
    chain = (f"[{index}:a]aresample=48000,asetpts=PTS-STARTPTS,"
             f"atrim=duration={seconds:.3f},volume={gain:.4f}")
    for beat in beats:
        start = float(beat["time"])
        end = start + float(beat["duration"]) + 0.12
        chain += f",volume=0.35:enable='between(t,{start:.3f},{end:.3f})'"
    chain += (f",afade=t=in:st=0:d=0.25,"
              f"afade=t=out:st={max(0, seconds - 0.6):.3f}:d=0.6[musicbed]")
    return chain


def commit_music(selection):
    path = Path(selection["state_path"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"cursor": selection["next_cursor"],
                                     "last_track": selection["filename"]}, indent=2))
    temporary.replace(path)
