"""Durable local-screen candidate index. No paid model dependencies."""
import json
import os
import re
import time
from pathlib import Path

LIBRARY = Path('data/clip_library.json')
STATE = Path('data/clip_library_state.json')
LOCAL_POLICY = 'local-gameplay-gunfire-v2'


def read(path, default):
    p = Path(path)
    if not p.exists():
        return default
    value = json.loads(p.read_text())
    if not isinstance(value, dict):
        raise ValueError(f'Invalid object: {path}')
    return value


def write(path, data):
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(tmp, p)


def blocked_ids():
    # Cover the existing histories' list/string/object representations.
    ids = set()
    for path in ['history.json', 'shorts_rejected_history.json']:
        p = Path(path)
        if not p.exists():
            continue
        data = json.loads(p.read_text())
        def visit(value):
            if isinstance(value, str):
                ids.update(re.findall(r'clip_[A-Za-z0-9]+', value))
            elif isinstance(value, list):
                for v in value: visit(v)
            elif isinstance(value, dict):
                for k, v in value.items():
                    visit(k); visit(v)
        visit(data)
    # Use the existing cache's permanent/expiry semantics.
    from firefight_cache import load, retained
    for cid, entry in load()['entries'].items():
        if entry.get('passed') is False and retained(entry):
            ids.add(cid)
    return ids


def ready(library, blocked=None):
    from local_combat_screen import verified
    blocked = blocked_ids() if blocked is None else blocked
    return [row for cid, row in library.get('clips', {}).items()
            if cid not in blocked and verified(row)]


def merge(source_library, source_state):
    library = read(LIBRARY, {'version': 1, 'clips': {}})
    generated = read(source_library, {'clips': {}})
    for cid, row in generated['clips'].items():
        if row.get('library_scanned_at', 0) >= library['clips'].get(cid, {}).get('library_scanned_at', 0):
            library['clips'][cid] = row
    # Only this discovery workflow writes cursor state; the concurrency group
    # prevents two collectors racing. Production reads the index only.
    state = read(source_state, {})
    write(LIBRARY, library); write(STATE, state)


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 4 or sys.argv[1] != 'merge':
        raise SystemExit('Usage: python clip_library.py merge LIBRARY STATE')
    merge(sys.argv[2], sys.argv[3])
