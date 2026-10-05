"""Start the existing pipeline with shared API limits in every Python child."""
import json
import os
import subprocess
import sys
from pathlib import Path

from ai_budget import LIMITS


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    ledger = root / "work" / "ai_budget.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"version": 1, "limits": LIMITS,
        "reserved": {}, "actual_tokens": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "calls": []}, indent=2))
    runtime = root / "work" / "ai_runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    # Python loads this startup hook before any pipeline/helper imports OpenAI.
    (runtime / "sitecustomize.py").write_text(
        "import os\nif os.environ.get('VIRALSPAWN_AI_BUDGET_ACTIVE') == '1':\n"
        "    try:\n        from ai_budget import install\n        install()\n"
        "    except BaseException:\n        import sys\n"
        "        sys.stderr.write('AI budget startup failed; refusing unmetered pipeline\\n')\n"
        "        os._exit(90)\n")
    env = os.environ.copy()
    env["VIRALSPAWN_AI_BUDGET_ACTIVE"] = "1"
    env["VIRALSPAWN_AI_LEDGER"] = str(ledger)
    env["PYTHONPATH"] = os.pathsep.join([str(runtime), str(root), env.get("PYTHONPATH", "")])
    result = subprocess.run([sys.executable, "v12_1_pipeline.py"], env=env)
    data = json.loads(ledger.read_text())
    print("SHARED AI BUDGET:", json.dumps({"reserved": data["reserved"],
                                           "actual_tokens": data["actual_tokens"]}))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
