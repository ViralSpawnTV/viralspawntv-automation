import json, re, sys
from pathlib import Path
from openai import OpenAI
from playwright.sync_api import sync_playwright

MANIFEST = Path("work/v11_candidate_manifest.json")
OUT = Path("work/longform/ranked_sources.json")
MAX_INSPECT = 50
MAX_SELECT = 14

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36")

BLOCKED = {"casino","gambling","slots","roulette","blackjack","sportsbook",
           "sports betting","betting","crypto","prediction market"}

def inspect(page, c):
    try:
        page.goto(c["clip_url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(900)
        title = page.title() or ""
        desc = ""
        n = page.locator('meta[name="description"]')
        if n.count():
            desc = n.first.get_attribute("content") or ""
        text = (title+" "+desc).lower()
        if any(x in text for x in BLOCKED):
            return None
        return {**c, "page_title": title, "page_description": desc}
    except Exception:
        return {**c, "page_title":"", "page_description":""}

def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    candidates = data.get("candidates", [])[:MAX_INSPECT]
    inspected = []

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(user_agent=USER_AGENT, viewport={"width":1280,"height":800})
        for c in candidates:
            row = inspect(page, c)
            if row: inspected.append(row)
        b.close()

    compact = [{"index":i,"game":x.get("game"),"channel":x.get("channel"),
                "title":x.get("page_title"),"description":x.get("page_description")}
               for i,x in enumerate(inspected)]

    prompt = f"""
Rank gaming clips for an original narrated ViralSpawnTV long-form compilation.
We need a varied 8-12 minute episode built around exciting/funny/strange gaming moments.

Prefer clips likely to contain a clear visual event: clutch, fail, ambush, rage,
boss fight, high-kill play, surprise, comeback, funny accident, impressive skill.
Penalize generic talking, podcasts, movie watching, IRL, politics, crypto, gambling,
or metadata with no likely gaming event.

Variety matters after quality: avoid selecting too many clips from one creator or game.
Return ONLY JSON:
{{"ranked":[{{"index":0,"score":80,"reason":"brief reason"}}]}}
Return up to {MAX_SELECT} candidates. Include plausible candidates scoring 30+.

CANDIDATES:
{json.dumps(compact, ensure_ascii=False)}
"""
    client = OpenAI()
    r = client.responses.create(model="gpt-5.6", input=prompt)
    raw = re.sub(r"^```json\s*|\s*```$","",r.output_text.strip())
    ranked_json = json.loads(raw)

    chosen, creator_n, game_n = [], {}, {}
    for row in ranked_json.get("ranked", []):
        try:
            idx, score = int(row["index"]), int(row["score"])
            c = inspected[idx]
        except Exception:
            continue
        if score < 30: continue
        creator = c.get("channel","").lower()
        game = c.get("game","").lower()
        if creator_n.get(creator,0) >= 2 or game_n.get(game,0) >= 3:
            continue
        creator_n[creator] = creator_n.get(creator,0)+1
        game_n[game] = game_n.get(game,0)+1
        chosen.append({**c,"longform_rank_score":score,
                       "longform_rank_reason":row.get("reason","")})
        if len(chosen) >= MAX_SELECT: break

    if len(chosen) < 5:
        raise RuntimeError(f"Only {len(chosen)} usable long-form candidates.")

    OUT.write_text(json.dumps({"version":1,"candidates":chosen},indent=2,
                              ensure_ascii=False),encoding="utf-8")
    print(f"Long-form ranker selected {len(chosen)} sources.")
    for i,c in enumerate(chosen,1):
        print(f"{i}. {c['longform_rank_score']} | {c.get('game')} | {c.get('channel')} | {c.get('page_title','')[:80]}")

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("LONGFORM RANKER FAILED:",e); sys.exit(1)
