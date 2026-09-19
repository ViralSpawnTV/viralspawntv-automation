import json, re, sys
from pathlib import Path
from openai import OpenAI
from playwright.sync_api import sync_playwright

MANIFEST = Path("work/v11_candidate_manifest.json")
OUT = Path("work/v12_ranked_candidates.json")

MAX_INSPECT = 60
MAX_RANKED = 15
MIN_SCORE = 40

BLOCKED = {
    "casino","gambling","roulette","blackjack","sportsbook","betting","slots",
    "slot machine","stake","crypto","prediction market"
}
# Deterministic pre-download commercial-music warning terms.
MUSIC_BLOCKED = {
    "song","music","remix","lyrics","singing","karaoke","official audio",
    "music video","soundtrack"
}
ACTION = {
    "clutch","1v","kill","kills","win","ace","rage","insane","crazy","sniper",
    "headshot","fight","final","boss","record","speedrun","comeback","fail",
    "funny","reaction","elim","wiped","squad","ranked","overtime","last second",
    "1 hp","quad","triple","double","ambush","rocket","movement"
}

def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def hits(text, words):
    t = text.lower()
    return sorted([w for w in words if w in t])

def inspect_metadata(page, candidate):
    page.goto(candidate["clip_url"], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(800)
    title = norm(page.title())
    desc = ""
    for selector in [
        'meta[name="description"]',
        'meta[property="og:description"]',
        'meta[name="twitter:description"]'
    ]:
        try:
            v = page.locator(selector).first.get_attribute("content")
            if v:
                desc = norm(v)
                break
        except Exception:
            pass
    text = norm(f"{title} {desc}")
    row = dict(candidate)
    row.update({
        "page_title": title,
        "page_description": desc,
        "metadata_text": text,
        "bad_hits": hits(text, BLOCKED),
        "music_hits": hits(text, MUSIC_BLOCKED),
        "action_hits": hits(text, ACTION),
    })
    return row

def main():
    if not MANIFEST.exists():
        raise RuntimeError("Missing work/v11_candidate_manifest.json")
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    candidates = data.get("candidates", [])[:MAX_INSPECT]

    inspected = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for i, c in enumerate(candidates, 1):
            try:
                row = inspect_metadata(page, c)
                if row["bad_hits"]:
                    print(f"[{i}/{len(candidates)}] HARD REJECT gambling/blocked metadata: {c['clip_id']} {row['bad_hits']}")
                    continue
                if row["music_hits"]:
                    print(f"[{i}/{len(candidates)}] HARD REJECT music metadata: {c['clip_id']} {row['music_hits']}")
                    continue
                inspected.append(row)
            except Exception as e:
                print(f"[{i}/{len(candidates)}] metadata inspect failed: {c.get('clip_id')} {e}")
        browser.close()

    if not inspected:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({
            "version":"12.1","source_candidate_count":len(data.get("candidates",[])),
            "metadata_inspected_count":len(candidates),"ranked_count":0,"candidates":[]
        }, indent=2), encoding="utf-8")
        raise RuntimeError("No candidates survived deterministic metadata screening.")

    compact = []
    for i, r in enumerate(inspected):
        compact.append({
            "id": i,
            "game": r.get("game",""),
            "channel": r.get("channel",""),
            "title": r.get("page_title",""),
            "description": r.get("page_description",""),
            "action_hits": r.get("action_hits",[])
        })

    client = OpenAI()
    prompt = f"""
Rank gaming clips for a vertical YouTube Shorts pipeline using ONLY the metadata below.
Do not invent what happens in the video. Metadata is a cheap first-pass filter; the real
video will be analyzed later.

Prefer metadata that plausibly signals:
- a clear gaming action, fail, clutch, surprise, reaction, funny moment, impressive play,
  close call, win/loss, or obvious payoff
- enough context to turn into a short piece of original commentary
- actual gameplay rather than unrelated conversation

Do NOT force game diversity over quality.
Return up to {MAX_RANKED} candidates.
Score 0-100. Include candidates scoring at least {MIN_SCORE}; when metadata is sparse but
still plausibly gaming, a score in the 40s is acceptable because the downstream multimodal
gate will make the real quality decision.

Return ONLY JSON:
{{"ranked":[{{"id":0,"score":72,"reason":"brief reason"}}]}}

CANDIDATES:
{json.dumps(compact, ensure_ascii=False)}
"""
    resp = client.responses.create(model="gpt-5.6", input=prompt)
    raw = re.sub(r"^```json\s*|\s*```$", "", resp.output_text.strip())
    ranked_json = json.loads(raw).get("ranked", [])

    ranked = []
    used_ids = set()
    for item in sorted(ranked_json, key=lambda x: float(x.get("score",0)), reverse=True):
        try:
            idx = int(item["id"])
            score = float(item.get("score",0))
        except Exception:
            continue
        if idx < 0 or idx >= len(inspected) or idx in used_ids or score < MIN_SCORE:
            continue
        used_ids.add(idx)
        row = dict(inspected[idx])
        row["v12_metadata_score"] = round(score, 1)
        row["v12_metadata_reason"] = norm(item.get("reason",""))
        ranked.append(row)
        if len(ranked) >= MAX_RANKED:
            break

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version":"12.1",
        "source_candidate_count":len(data.get("candidates",[])),
        "metadata_inspected_count":len(candidates),
        "survived_deterministic_screen":len(inspected),
        "ranked_count":len(ranked),
        "min_metadata_score":MIN_SCORE,
        "candidates":ranked
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"V12.1: {len(data.get('candidates',[]))} discovered -> {len(candidates)} inspected -> "
          f"{len(inspected)} deterministic survivors -> {len(ranked)} ranked.")
    if not ranked:
        raise RuntimeError("No candidates promoted by V12.1 metadata ranker.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("V12.1 RANKER FAILED:", e)
        sys.exit(1)
