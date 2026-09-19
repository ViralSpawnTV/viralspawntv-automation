import json, re, subprocess, sys
from pathlib import Path
from openai import OpenAI

ROOT = Path("work/longform")
META = ROOT / "acquired_sources.json"
OUT = ROOT / "screened_sources.json"

BLOCKED = [
    "casino","gambling","sports betting","sportsbook","roulette",
    "blackjack","slot machine","slots","betting"
]

def run(cmd):
    return subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)

def main():
    client = OpenAI()
    data = json.loads(META.read_text(encoding="utf-8"))
    passed, rejected = [], []

    for i, s in enumerate(data.get("sources", []), 1):
        src = Path(s["local_path"])
        wav = ROOT / f"gate_{i:02d}.wav"

        run(["ffmpeg","-y","-i",str(src),"-vn","-ac","1","-ar","16000",str(wav)])

        with open(wav, "rb") as f:
            tr = client.audio.transcriptions.create(
                model="whisper-1", file=f, response_format="verbose_json"
            )
        wav.unlink(missing_ok=True)

        text = getattr(tr, "text", "") or ""
        lower = text.lower()
        hard = [x for x in BLOCKED if x in lower]

        prompt = f"""
Screen this gaming clip transcript before commercial YouTube production.

Reject if there is meaningful evidence of:
1. recognizable/commercial music, a song, singing, rapping, or lyrics;
2. gambling/casino/sports-betting promotion or substantive gambling content.

Do NOT reject ordinary game sound effects, streamer speech, crowd noise,
or generic ambience when there is no evidence of identifiable music.

Return ONLY JSON:
{{"pass":true,"music_risk":false,"gambling_risk":false,
  "confidence":0.95,"reason":"brief reason"}}

GAME: {s.get("game")}
TITLE: {s.get("page_title")}
TRANSCRIPT:
{text[:5000]}
"""
        r = client.responses.create(model="gpt-5.6", input=prompt)
        raw = re.sub(r"^```json\s*|\s*```$", "", r.output_text.strip())
        verdict = json.loads(raw)

        ok = bool(verdict.get("pass")) and not hard
        row = {**s, "gate_transcript": text, "source_gate": verdict}

        if ok:
            passed.append(row)
            print(f"PASS {i}: {s.get('game')} / {s.get('channel')}")
        else:
            rejected.append(row)
            print(f"REJECT {i}: {s.get('game')} / {s.get('channel')} / {verdict.get('reason')}")

    if len(passed) < 5:
        raise RuntimeError(f"Only {len(passed)} sources passed source screening.")

    OUT.write_text(json.dumps({
        "passed_sources": passed,
        "rejected_sources": rejected
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Source screening complete: {len(passed)} pass, {len(rejected)} reject.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("LONGFORM SOURCE GATE FAILED:", e)
        sys.exit(1)
