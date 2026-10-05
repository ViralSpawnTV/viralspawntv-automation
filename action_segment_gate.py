"""Verify the actual fight edit, with one bounded attempt to trim idle edges."""
import base64
import copy
import json
import math
import os
import subprocess
from pathlib import Path

SAMPLES = 16
MIN_CORE_SECONDS = 19.0
CONTEXT_LIMITS = {"reload": 4.0, "armor": 4.0, "incoming_fire": 6.0, "outcome": 8.0}


def judge_samples(data, seconds):
    rows = data.get("samples", []) if isinstance(data, dict) else []
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            continue
        if row["id"] in by_id:
            return {"passed": False, "reason": "Duplicate sample IDs", "valid_evidence": False}
        by_id[row["id"]] = row
    if set(by_id) != set(range(SAMPLES)) or any(
        type(row.get("direct_gunfight")) is not bool
        or not isinstance(row.get("evidence"), str) or not row["evidence"].strip()
        for row in by_id.values()
    ):
        return {"passed": False, "reason": "Incomplete/invalid visual verdicts", "valid_evidence": False}
    direct = [by_id[i]["direct_gunfight"] for i in range(SAMPLES)]
    positions = [i for i, value in enumerate(direct) if value]
    width = seconds / SAMPLES
    meaningful = direct.copy()
    outcomes = []
    terminal = []
    for i in range(SAMPLES):
        kind = by_id[i].get("context_type", "none")
        if kind not in CONTEXT_LIMITS or not positions:
            continue
        # A result cannot validate earlier idle footage or unrelated future combat.
        nearby = [j for j in positions if j <= i] if kind == "outcome" else positions
        distance = min((abs(i - j) * width for j in nearby), default=math.inf)
        if distance <= CONTEXT_LIMITS[kind]:
            meaningful[i] = True
            if kind == "outcome":
                outcomes.append(i)
                if by_id[i].get("terminal_payoff") is True and not any(j > i for j in positions):
                    terminal.append(i)
    inactive_run = longest = 0
    for value in meaningful:
        inactive_run = 0 if value else inactive_run + 1
        longest = max(longest, inactive_run)
    useful = [i for i, value in enumerate(meaningful) if value]
    first = (positions[0] + 0.5) * width if positions else seconds
    tail = seconds - (useful[-1] + 0.5) * width if useful else seconds
    reasons = []
    if data.get("ranged_shooter_gameplay") is not True:
        reasons.append("Not confirmed ranged-shooter gameplay")
    if len(positions) < 4:
        reasons.append("Insufficient direct gunfight samples")
    if sum(meaningful) / SAMPLES < 0.55:
        reasons.append("Most samples are outside the confirmed fight")
    if first > 4:
        reasons.append("Combat starts too late")
    if longest * width > 6:
        reasons.append("Long inactive stretch")
    if tail > 3:
        reasons.append("Long post-combat ending")
    return {"passed": not reasons, "reason": "; ".join(reasons) or "Confirmed gunfight with bounded fight context",
            "valid_evidence": True, "active_samples": len(positions), "total_samples": SAMPLES,
            "combat_sample_fraction": len(positions) / SAMPLES, "fight_context_fraction": sum(meaningful) / SAMPLES,
            "estimated_longest_inactive_seconds": longest * width, "estimated_first_combat_seconds": first,
            "estimated_post_combat_seconds": tail, "useful_sample_ids": useful, "outcome_sample_ids": outcomes, "terminal_payoff_sample_ids": terminal,
            "model_evidence": data}


def propose_trim(result, start, end, payoff_time=None):
    """Propose only an edge trim; approval always requires new exact-frame evidence."""
    if not result.get("valid_evidence") or result.get("active_samples", 0) < 3:
        return None
    if result.get("model_evidence", {}).get("ranged_shooter_gameplay") is not True:
        return None
    useful = result.get("useful_sample_ids", [])
    if not useful:
        return None
    width = (end - start) / SAMPLES
    first = float(result["estimated_first_combat_seconds"])
    new_start = start + max(0.0, first - width / 2 - 0.4) if first > 4 else start
    last = start + (useful[-1] + 0.5) * width
    new_end = min(end, last + min(1.75, width / 2 + 0.4))
    if new_end - new_start < MIN_CORE_SECONDS or end - new_end + new_start - start < 2:
        return None
    if payoff_time is not None and math.isfinite(payoff_time) and payoff_time > new_end:
        # Replace a later estimated payoff only with explicit visible fight resolution.
        if not result.get("terminal_payoff_sample_ids"):
            return None
    return new_start, new_end


def trimmed_plan(plan, start, end):
    updated = copy.deepcopy(plan)
    delta = start - float(plan["segment_start"])
    updated["segment_start"] = start
    updated["segment_end"] = end
    for key in ("commentary", "impacts"):
        if key not in updated:
            continue
        beats = []
        for beat in updated[key]:
            beat = dict(beat)
            timestamp = float(beat.get("time", 0)) - delta
            if 0 <= timestamp < end - start:
                beat["time"] = timestamp
                beats.append(beat)
        updated[key] = beats
    return updated


def inspect_segment(client, video, start, end, folder):
    seconds = end - start
    content = [{"type": "input_text", "text": (
        "Verify this exact continuous gaming edit. Ignore titles, narration, facecam and overlays. "
        "direct_gunfight=true ONLY for visibly active ranged-weapon engagement: firing, recoil, "
        "muzzle flash, ammo decrease with enemy engagement, hit markers during shooting, or a clear "
        "weapon-fire exchange. Aiming alone and kill banners alone are false. "
        "For each interval also set context_type to exactly one of none/reload/armor/incoming_fire/outcome. "
        "Use reload or armor only for clearly visible defensive weapon/armor handling during the "
        "same ongoing fight; incoming_fire only for visible enemy attack/damage, not generic danger; "
        "outcome only for a brief visible kill, knock or team wipe that resolves the shown engagement. "
        "Walking, traversal, looting, spectating, menus, buy screens and unrelated celebration are none. "
        "Do not infer a fight from motion or a victory banner. Context is bounded by nearby confirmed "
        "gunfire by the code and cannot substitute for real shooting. When uncertain use false/none. "
        "Also set terminal_payoff boolean: true only for a visible result that resolves the "
        "shown fight, such as a confirmed team wipe or final elimination. Intermediate knocks "
        "and a persistent banner alone are not terminal payoff. "
        "Return JSON only: ranged_shooter_gameplay boolean and samples, exactly 16 objects with "
        "integer id 0-15, direct_gunfight boolean, context_type, terminal_payoff boolean, and concise visible evidence."
    )}]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        for i in range(SAMPLES):
            center = (i + 0.5) * seconds / SAMPLES
            content.append({"type": "input_text", "text": f"Interval {i}, edit time {center:.2f}s"})
            for j, delta in enumerate((-0.20, 0.20)):
                timestamp = start + max(0.02, min(seconds - 0.02, center + delta))
                path = folder / f"sample_{i:02d}_{j}.jpg"
                path.unlink(missing_ok=True)
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(timestamp), "-i", str(video),
                                "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(path)],
                               check=True, capture_output=True, timeout=20)
                if not path.is_file() or not path.stat().st_size:
                    raise RuntimeError(f"Missing action-verification frame {i}/{j}")
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                content.append({"type": "input_image", "image_url": "data:image/jpeg;base64," + encoded})
        response = client.responses.create(model=os.getenv("ACTION_GATE_MODEL", "gpt-5.6"),
                                           max_output_tokens=3000,
                                           input=[{"role": "user", "content": content}])
        text = response.output_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = judge_samples(json.loads(text), seconds)
    except Exception as exc:
        result = {"passed": False, "valid_evidence": False,
                  "reason": f"Action verification unavailable: {exc}"}
    result.update(segment_start=start, segment_end=end, segment_seconds=seconds)
    return result


def validate_action_segment(client, video, plan, work):
    start, end = float(plan["segment_start"]), float(plan["segment_end"])
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end - start < MIN_CORE_SECONDS:
        raise RuntimeError("Invalid or too-short segment for action verification")
    folder = Path(work) / "action_segment_gate"
    first = inspect_segment(client, video, start, end, folder / "original")
    result = first
    attempts = [first]
    if not first["passed"]:
        try:
            payoff = float(plan["payoff_time"]) if "payoff_time" in plan else None
        except (ValueError, TypeError):
            payoff = None
        proposed = propose_trim(first, start, end, payoff)
        if proposed:
            print(f"ACTION EDIT REPAIR: {start:.2f}-{end:.2f}s -> {proposed[0]:.2f}-{proposed[1]:.2f}s")
            repaired = inspect_segment(client, video, *proposed, folder / "trimmed")
            attempts.append(repaired)
            result = repaired
            if repaired["passed"]:
                plan.update(trimmed_plan(plan, *proposed))
                terminal = first.get("terminal_payoff_sample_ids", [])
                if payoff is not None and payoff > proposed[1] and terminal:
                    plan["payoff_time"] = start + (terminal[-1] + 0.5) * (end - start) / SAMPLES
                plan["action_edit_repair"] = {"original_start": start, "original_end": end,
                                             "trimmed_start": proposed[0], "trimmed_end": proposed[1]}
    result = dict(result, attempts=attempts)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    plan["action_segment_validation"] = result
    print(f"ACTION SEGMENT GATE: {'PASSED' if result['passed'] else 'REJECTED'} | {result['reason']}")
    if not result["passed"]:
        raise RuntimeError("Selected segment failed the mandatory gunfight gate: " + result["reason"])
    return result
