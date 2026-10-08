"""Meter a refill run using the existing shared SDK guard, with discovery-only limits."""
import json
import os
import subprocess
import sys
from pathlib import Path

def main():
    lease=json.loads(Path('work/verified_review_lease.json').read_text());limit=lease['limit']
    ledger=Path('work/verified_discovery/ai_budget.json').resolve();ledger.parent.mkdir(parents=True,exist_ok=True)
    caps=dict(requests=limit,response_requests=limit,images=limit*70,
              reserved_output_tokens=limit*6000,text_bytes=limit*30000,
              transcription_seconds=0,speech_characters=0)
    ledger.write_text(json.dumps(dict(version=1,limits=caps,reserved={},
        actual_tokens=dict(input_tokens=0,output_tokens=0,total_tokens=0),calls=[])))
    runtime=Path('work/refill_runtime').resolve();runtime.mkdir(parents=True,exist_ok=True)
    (runtime/'sitecustomize.py').write_text("import os\ntry:\n import ai_budget\n ai_budget.is_transient = lambda exc: False\n ai_budget.install()\nexcept BaseException:\n os._exit(90)\n")
    env=os.environ.copy();env.update(VIRALSPAWN_AI_BUDGET_ACTIVE='1',VIRALSPAWN_AI_LEDGER=str(ledger),VIRALSPAWN_AI_STAGE='verified_discovery.py',
        PYTHONPATH=os.pathsep.join([str(runtime),str(Path.cwd()),env.get('PYTHONPATH','')]))
    return subprocess.run([sys.executable,'verified_discovery.py'],env=env).returncode
if __name__=='__main__':raise SystemExit(main())
