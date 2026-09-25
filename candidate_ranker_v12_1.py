import json, re, sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from openai import OpenAI
from playwright.sync_api import sync_playwright

MANIFEST = Path("work/v11_candidate_manifest.json")
OUT = Path("work/v12_ranked_candidates.json")
SHORTS_HISTORY = Path("history.json")
SHORTS_REJECTED_HISTORY = Path("shorts_rejected_history.json")

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


def normalize_clip_id(value):
    return str(value or "").strip().casefold()

def normalize_clip_url(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        netloc = parts.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = re.sub(r"/+", "/", parts.path).rstrip("/").casefold()
        return urlunsplit(((parts.scheme or "https").lower(), netloc, path, "", ""))
    except Exception:
        return raw.rstrip("/").casefold()

def load_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: could not parse {path}: {exc}")
        return {}

def extract_identity_sets(data, keys):
    ids, urls = set(), set()
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = []
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                rows.extend(value)
    else:
        rows = []

    for item in rows:
        if isinstance(item, str):
            if item.strip().lower().startswith(("http://", "https://")):
                curl = normalize_clip_url(item)
                if curl:
                    urls.add(curl)
            else:
                cid = normalize_clip_id(item)
                if cid:
                    ids.add(cid)
            continue
        if not isinstance(item, dict):
            continue
        cid = normalize_clip_id(item.get("clip_id") or item.get("id"))
        curl = normalize_clip_url(
            item.get("clip_url") or item.get("source_url")
            or item.get("url") or item.get("source")
        )
        if cid:
            ids.add(cid)
        if curl:
            urls.add(curl)
    return ids, urls

def load_blocked_short_identities():
    used_ids, used_urls = extract_identity_sets(
        load_json(SHORTS_HISTORY), ["used_clips", "clips", "history"]
    )
    rejected_ids, rejected_urls = extract_identity_sets(
        load_json(SHORTS_REJECTED_HISTORY),
        ["rejected_clips", "rejected", "clips", "used_clips"]
    )
    print(f"Previously published Shorts IDs loaded: {len(used_ids)}")
    print(f"Permanently rejected Shorts IDs loaded: {len(rejected_ids)}")
    return used_ids | rejected_ids, used_urls | rejected_urls

def candidate_is_blocked(candidate, blocked_ids, blocked_urls):
    cid = normalize_clip_id(candidate.get("clip_id"))
    curl = normalize_clip_url(
        candidate.get("clip_url") or candidate.get("source_url")
        or candidate.get("url") or candidate.get("source")
    )
    return (cid and cid in blocked_ids) or (curl and curl in blocked_urls)

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
    all_candidates = data.get("candidates", [])
    blocked_ids, blocked_urls = load_blocked_short_identities()

    # Remove permanent used/rejected clips BEFORE MAX_INSPECT.
    fresh_candidates = []
    skipped_history = 0
    seen_ids = set()
    seen_urls = set()

    for candidate in all_candidates:
        if not isinstance(candidate, dict):
            continue
        cid = normalize_clip_id(candidate.get("clip_id"))
        curl = normalize_clip_url(
            candidate.get("clip_url") or candidate.get("source_url")
            or candidate.get("url") or candidate.get("source")
        )
        if (cid and cid in seen_ids) or (curl and curl in seen_urls):
            continue
        if candidate_is_blocked(candidate, blocked_ids, blocked_urls):
            skipped_history += 1
            continue
        fresh_candidates.append(candidate)
        if cid:
            seen_ids.add(cid)
        if curl:
            seen_urls.add(curl)

    candidates = fresh_candidates[:MAX_INSPECT]
    print(
        f"V12.1 freshness filter: {len(all_candidates)} discovered -> "
        f"{len(fresh_candidates)} fresh -> {len(candidates)} inspected."
    )
    print(f"Skipped {skipped_history} previously published/rejected Shorts before MAX_INSPECT.")

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
            "version":"12.2-fresh-history-filter","source_candidate_count":len(all_candidates),"fresh_candidate_count":len(fresh_candidates),"history_skipped_before_inspection":skipped_history,
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
        row = dict(inspected[idx])
        if candidate_is_blocked(row, blocked_ids, blocked_urls):
            print(f"FINAL SKIP previously published/rejected Short: {row.get('clip_id')}")
            continue
        used_ids.add(idx)
        row["v12_metadata_score"] = round(score, 1)
        row["v12_metadata_reason"] = norm(item.get("reason",""))
        ranked.append(row)
        if len(ranked) >= MAX_RANKED:
            break

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version":"12.2-fresh-history-filter",
        "source_candidate_count":len(all_candidates),
        "fresh_candidate_count":len(fresh_candidates),
        "history_skipped_before_inspection":skipped_history,
        "metadata_inspected_count":len(candidates),
        "survived_deterministic_screen":len(inspected),
        "ranked_count":len(ranked),
        "min_metadata_score":MIN_SCORE,
        "candidates":ranked
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"V12.2: {len(all_candidates)} discovered -> {len(fresh_candidates)} fresh -> "
        f"{len(candidates)} inspected -> {len(inspected)} deterministic survivors -> "
        f"{len(ranked)} ranked."
    )
    if not ranked:
        raise RuntimeError("No candidates promoted by V12.1 metadata ranker.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("V12.1 RANKER FAILED:", e)
        sys.exit(1)
