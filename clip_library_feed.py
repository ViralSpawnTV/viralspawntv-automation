"""Feed local-screen library candidates to the existing paid approval pipeline."""
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import kick_game_discovery as discovery
from clip_library import LIBRARY, blocked_ids, read, ready, write


def main():
    library = read(LIBRARY, {'clips':{}})
    blocked = blocked_ids()
    history = read('history.json', {'used_clips':[]})
    used, creator_counts, game_counts = discovery.history_state(history)
    blocked |= used
    candidates = ready(library, blocked)
    candidates.sort(key=lambda r:(r.get('local_firefight_score',0),r.get('library_scanned_at',0)),reverse=True)
    selected=[];by_creator=Counter();by_game=Counter()
    for row in candidates:
        creator=str(row.get('channel','')).strip().lower();game=str(row.get('game','')).strip().lower()
        if creator_counts.get(creator,0)>=discovery.MAX_PUBLIC_UPLOADS_PER_CREATOR_24H:continue
        if game_counts.get(game,0)>=discovery.MAX_PUBLIC_UPLOADS_PER_GAME_24H:continue
        if by_creator[creator]>=3 or by_game[game]>=24:continue
        if not discovery.is_eligible(row):continue
        selected.append(row);by_creator[creator]+=1;by_game[game]+=1
        if len(selected)>=100:break
    if not selected and os.getenv('LIBRARY_ALLOW_LIVE_DISCOVERY','0')=='1':
        return subprocess.call([sys.executable,'kick_game_discovery.py'])
    write('work/v11_candidate_manifest.json',{'version':'local-library-v1',
        'selection_mode':'locally_screened_library_not_visual_approval',
        'candidate_count':len(selected),'candidates':selected})
    print(f'LIBRARY FEED: {len(selected)} unpublished eligible candidates; action approval still required.')
    if not selected:
        print('Library empty or all entries used/rejected/diversity-limited. Run Build Clip Library; no paid calls made.')
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
