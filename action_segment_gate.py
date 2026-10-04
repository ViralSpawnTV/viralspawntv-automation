"""Mandatory visual gunfight gate for the exact selected edit segment."""
import base64
import json
import math
import os
import subprocess
from pathlib import Path

SAMPLES = 16


def judge_samples(data, seconds):
    """Fail closed on missing/invalid evidence; scores alone cannot pass."""
    rows = data.get("samples", [])
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            continue
        if row["id"] in by_id:
            return {"passed": False, "reason": "Duplicate sample IDs"}
        by_id[row["id"]] = row
    if set(by_id) != set(range(SAMPLES)):
        return {"passed": False, "reason": "Incomplete visual sample verdicts"}
    active = [by_id[i].get("direct_gunfight") is True for i in range(SAMPLES)]
    inactive_run = longest = 0
    for value in active:
        inactive_run = 0 if value else inactive_run + 1
        longest = max(longest, inactive_run)
    positions = [i for i, value in enumerate(active) if value]
    width = seconds / SAMPLES
    first = (positions[0] + 0.5) * width if positions else seconds
    tail = seconds - (positions[-1] + 0.5) * width if positions else seconds
    fraction = sum(active) / SAMPLES
    dead_estimate = longest * width
    reasons = []
    if data.get("ranged_shooter_gameplay") is not True:
        reasons.append("Not confirmed ranged-shooter gameplay")
    if fraction < 0.5:
        reasons.append("Fewer than half the sampled intervals show direct gunfight evidence")
    if first > 4:
        reasons.append("Combat starts too late")
    if dead_estimate > 6:
        reasons.append("Long inactive stretch")
    if tail > 5:
        reasons.append("Long post-combat/menu ending")
    return {"passed": not reasons, "reason": "; ".join(reasons) or "Sustained direct gunfight evidence",
            "active_samples": sum(active), "total_samples": SAMPLES,
            "combat_sample_fraction": fraction, "estimated_longest_inactive_seconds": dead_estimate,
            "estimated_first_combat_seconds": first, "estimated_post_combat_seconds": tail,
            "model_evidence": data}


def validate_action_segment(client, video, plan, work):
    start = float(plan["segment_start"])
    end = float(plan["segment_end"])
    seconds = end - start
    if not math.isfinite(seconds) or not math.isfinite(start) or start < 0 or seconds <= 0:
        raise RuntimeError("Invalid segment for action verification")
    folder = Path(work) / "action_segment_gate"
    folder.mkdir(parents=True, exist_ok=True)
    report_path = folder / "result.json"
    content = [{"type": "input_text", "text": (
        "You verify ONLY visible sustained direct gunfights in a gaming Short. "
        "The supplied pairs cover the EXACT segment to be rendered. Ignore clip titles, "
        "narration, subtitles, streamer reactions, facecam, donations and animated borders. "
        "Each pair represents one interval. direct_gunfight is true only for visible "
        "active firearm/ranged-weapon engagement: firing, recoil/muzzle flashes, enemy damage "
        "during shooting, or a clear exchange of weapon fire. Aiming without firing, "
        "walking/running, looting, spectating, melee/MOBA combat, death/victory banners, "
        "menus and results screens are false. Do not infer action from a victory or loud reaction. "
        "When unclear, mark false. Return JSON only with ranged_shooter_gameplay (boolean), "
        "samples (exactly 16 objects with integer id 0-15, direct_gunfight boolean, "
        "and evidence describing what is actually visible)."
    )}]
    try:
        for i in range(SAMPLES):
            center = (i + 0.5) * seconds / SAMPLES
            content.append({"type": "input_text", "text": f"Interval {i}, around edit time {center:.2f}s"})
            for j, delta in enumerate((-0.20, 0.20)):
                timestamp = start + max(0.02, min(seconds - 0.02, center + delta))
                path = folder / f"sample_{i:02d}_{j}.jpg"
                # Clear a stale image so failed extraction cannot reuse old evidence.
                if path.exists():
                    path.unlink()
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(timestamp),
                                "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-2",
                                "-q:v", "4", str(path)], check=True, capture_output=True)
                if not path.is_file() or not path.stat().st_size:
                    raise RuntimeError(f"Missing action-verification frame {i}/{j}")
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + encoded})
        response = client.responses.create(model=os.getenv("ACTION_GATE_MODEL", "gpt-5.6"),
                                           input=[{"role": "user", "content": content}])
        text = response.output_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = judge_samples(json.loads(text), seconds)
    except Exception as exc:
        result = {"passed": False, "reason": f"Action verification unavailable: {exc}"}
    result.update({"segment_start": start, "segment_end": end, "segment_seconds": seconds})
    report_path.write_text(json.dumps(result, indent=2) + "\n")
    plan["action_segment_validation"] = result
    print(f"ACTION SEGMENT GATE: {'PASSED' if result['passed'] else 'REJECTED'} | {result['reason']}")
    if not result["passed"]:
        raise RuntimeError("Selected segment failed the mandatory gunfight gate: " + result["reason"])
    return result
