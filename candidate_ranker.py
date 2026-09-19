import json
import re
import sys
from pathlib import Path

from openai import OpenAI
from playwright.sync_api import sync_playwright


MANIFEST = Path("work/v11_candidate_manifest.json")
RANKED = Path("work/v12_ranked_candidates.json")
MAX_INSPECT = 36
MAX_RANKED = 15

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

BAD_TERMS = {
    "casino", "gambling", "slots", "roulette", "blackjack",
    "sportsbook", "betting", "stake", "crypto", "prediction market",
}

ACTION_TERMS = {
    "clutch", "1v", "kill", "kills", "win", "wins", "ace", "rage",
    "insane", "crazy", "sniper", "snipe", "headshot", "fight",
    "final", "boss", "record", "speedrun", "comeback", "fail",
    "funny", "reaction", "elim", "elimination", "wiped", "squad",
    "ranked", "overtime", "last second", "1 hp",
}


def page_metadata(page, candidate):
    try:
        page.goto(
            candidate["clip_url"],
            wait_until="domcontentloaded",
            timeout=45000,
        )
        page.wait_for_timeout(1200)

        title = page.title() or ""
        description = ""

        node = page.locator('meta[name="description"]')
        if node.count():
            description = node.first.get_attribute("content") or ""

        text = f"{title} {description}".strip()
        lowered = text.lower()

        bad_hits = sorted(term for term in BAD_TERMS if term in lowered)
        action_hits = sorted(term for term in ACTION_TERMS if term in lowered)

        return {
            **candidate,
            "page_title": title,
            "page_description": description,
            "metadata_text": text[:1200],
            "bad_hits": bad_hits,
            "action_hits": action_hits,
        }
    except Exception as exc:
        return {
            **candidate,
            "page_title": "",
            "page_description": "",
            "metadata_text": "",
            "bad_hits": [],
            "action_hits": [],
            "metadata_error": str(exc),
        }


def ai_rank(client, inspected):
    compact = []
    for i, item in enumerate(inspected):
        compact.append({
            "index": i,
            "game": item.get("game"),
            "channel": item.get("channel"),
            "title": item.get("page_title"),
            "description": item.get("page_description"),
            "action_hits": item.get("action_hits"),
            "bad_hits": item.get("bad_hits"),
        })

    prompt = f"""
You are cheaply pre-ranking source clips for ViralSpawnTV, a gaming Shorts
channel. This is NOT the expensive visual/transcript viral gate.

Rank candidates using ONLY game/category + clip page title/description.

Prefer metadata suggesting:
- clutch, high-kill, final-circle, boss, ace, comeback, rage, funny fail
- impressive play, challenge, surprise, intense fight, record, reaction
- a clear event with likely setup and payoff

Strongly penalize:
- generic conversation/podcast/movie/politics/IRL material
- crypto/prediction markets
- gambling/casino
- titles with no identifiable gaming event

Do not overvalue creator fame.
Do not force game diversity over quality.

Return ONLY JSON:
{{
  "ranked": [
    {{
      "index": 0,
      "score": 0,
      "reason": "short reason"
    }}
  ]
}}

Return at most {MAX_RANKED} candidates, strongest first.
Only include candidates with a plausible gaming-Short score of 45 or higher.

CANDIDATES:
{json.dumps(compact, ensure_ascii=False)}
"""

    response = client.responses.create(
        model="gpt-5.6",
        input=prompt,
    )
    raw = response.output_text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def main():
    if not MANIFEST.exists():
        raise RuntimeError("Missing V11 discovery manifest.")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    source = manifest.get("candidates", [])[:MAX_INSPECT]

    if not source:
        raise RuntimeError("Discovery manifest contains no candidates.")

    inspected = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )

        for candidate in source:
            row = page_metadata(page, candidate)

            # Hard reject obvious bad metadata before paying for AI ranking.
            if row.get("bad_hits"):
                print(
                    f"Metadata reject: {row.get('game')} / "
                    f"{row.get('channel')} / {row.get('bad_hits')}"
                )
                continue

            inspected.append(row)

        browser.close()

    if not inspected:
        raise RuntimeError("No candidates survived cheap metadata inspection.")

    client = OpenAI()
    ranking = ai_rank(client, inspected)

    ranked = []
    seen = set()

    for result in ranking.get("ranked", []):
        try:
            idx = int(result["index"])
            score = int(result["score"])
        except Exception:
            continue

        if idx < 0 or idx >= len(inspected) or score < 45:
            continue

        candidate = inspected[idx]
        clip_id = candidate.get("clip_id")
        if not clip_id or clip_id in seen:
            continue
        seen.add(clip_id)

        ranked.append({
            **candidate,
            "v12_metadata_score": score,
            "v12_metadata_reason": result.get("reason", ""),
        })

        if len(ranked) >= MAX_RANKED:
            break

    if not ranked:
        raise RuntimeError("AI pre-ranker found no plausible gaming candidates.")

    output = {
        "version": 12,
        "source_candidate_count": len(manifest.get("candidates", [])),
        "metadata_inspected_count": len(inspected),
        "ranked_count": len(ranked),
        "candidates": ranked,
    }

    RANKED.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("================================================")
    print("V12 CHEAP BATCH RANKING COMPLETE")
    print("================================================")
    for i, row in enumerate(ranked[:10], start=1):
        print(
            f"{i}. score={row['v12_metadata_score']} | "
            f"{row['game']} | {row['channel']} | "
            f"{row.get('page_title', '')[:90]}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"V12 RANKER FAILED: {exc}")
        sys.exit(1)
