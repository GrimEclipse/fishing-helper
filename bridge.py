"""Read the addon's flushed SavedVariables; never touch the live game process."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


EVENT_RE = re.compile(r'FH1\|(\d+)\|(start|cast_ok|cast_fail|loot|error|combat_on|combat_off)\|([^"\r\n]*)')


def find_saved_variables(wow_root: str) -> list[Path]:
    root = Path(wow_root).expanduser()
    if not root.is_dir():
        return []
    return sorted(root.glob('_*/WTF/Account/*/SavedVariables/FishingHelperBridge.lua'))


def read_events(wow_root: str) -> list[dict]:
    events = []
    for path in find_saved_variables(wow_root):
        try:
            data = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError):
            continue
        for timestamp, kind, payload in EVENT_RE.findall(data):
            parts = payload.split('|')
            event = {'time': int(timestamp), 'kind': kind, 'source': str(path)}
            if kind == 'loot' and len(parts) >= 3:
                try:
                    event.update(item_id=int(parts[0]), quantity=int(parts[1]), name=parts[2])
                except ValueError:
                    continue
            elif kind == 'error':
                event['message'] = parts[0]
            events.append(event)
    return sorted(events, key=lambda e: e['time'])


def summarize(events: list[dict]) -> dict:
    last_start = max((i for i, e in enumerate(events) if e['kind'] == 'start'), default=-1)
    if last_start >= 0: events = events[last_start + 1:]
    catches = [e for e in events if e['kind'] == 'loot']
    catch_times = []
    for event in catches:
        if not catch_times or event['time'] - catch_times[-1] > 3:
            catch_times.append(event['time'])
    fish = Counter()
    for e in catches:
        fish[(e['item_id'], e['name'])] += e['quantity']
    return {
        'cast_ok': sum(e['kind'] == 'cast_ok' for e in events),
        'cast_fail': sum(e['kind'] == 'cast_fail' for e in events),
        'loot_events': len(catches),
        'catches': len(catch_times),
        'items': fish,
        'last_sync': max((e['time'] for e in events), default=0),
    }
