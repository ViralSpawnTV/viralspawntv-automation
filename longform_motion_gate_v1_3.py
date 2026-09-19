import re, subprocess, sys
from pathlib import Path

def freeze_seconds(path, minimum_freeze=0.75):
    cmd = [
        "ffmpeg","-hide_banner","-i",str(path),
        "-vf",f"freezedetect=n=0.003:d={minimum_freeze}",
        "-an","-f","null","-"
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    text = (p.stderr or "") + (p.stdout or "")
    vals = re.findall(r"freeze_duration:\s*([0-9.]+)", text)
    return sum(float(x) for x in vals), [float(x) for x in vals]

def validate_motion(path, max_single_freeze=1.5, max_total_freeze=3.0):
    total, freezes = freeze_seconds(path)
    worst = max(freezes, default=0.0)
    ok = worst <= max_single_freeze and total <= max_total_freeze
    return ok, {"total_freeze_seconds":round(total,3),
                "worst_freeze_seconds":round(worst,3),
                "freeze_events":len(freezes)}

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python longform_motion_gate_v1_3.py VIDEO")
        sys.exit(2)
    ok, info = validate_motion(Path(sys.argv[1]))
    print(info)
    sys.exit(0 if ok else 23)
