"""Read flushed addon records and reconcile them with visible live events."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


EVENT_RE = re.compile(r'FH1\|(\d+)\|(start|cast_ok|cast_fail|loot|error|combat_on|combat_off)\|([^"\r\n]*)')
EVENT2_RE = re.compile(r'FH2\|(\d+)\|(\d+)\|(\d+)\|([a-z_]+)\|([^"\r\n]*)')


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
        found = [(None, None, *match) for match in EVENT_RE.findall(data)]
        found += EVENT2_RE.findall(data)
        for session, sequence, timestamp, kind, payload in found:
            parts = payload.split('|')
            event = {'time': int(timestamp), 'kind': kind, 'source': str(path)}
            if session is not None:
                event.update(session=int(session), sequence=int(sequence))
            if kind == 'loot' and len(parts) >= 3:
                try:
                    event.update(item_id=int(parts[0]), quantity=int(parts[1]), name=parts[2])
                except ValueError:
                    continue
            elif kind == 'error':
                event['message'] = parts[0]
            elif kind == 'achievement' and len(parts) >= 4:
                try:
                    event.update(achievement_id=int(parts[0]), completed=int(parts[1]),
                                 total=int(parts[2]), name=parts[3],
                                 criterion=parts[4] if len(parts) > 4 else '')
                except ValueError:
                    continue
            events.append(event)
    return sorted(events, key=lambda e: e['time'])


def merge_events(disk_events: list[dict], live_events: list[dict]) -> list[dict]:
    """Deduplicate FH2 records when they later appear on disk."""
    merged = {}
    for event in disk_events + live_events:
        key = ((event['session'], event['sequence'])
               if 'session' in event else
               (event['source'], event['time'], event['kind'],
                event.get('item_id'), event.get('message')))
        merged[key] = event
    return sorted(merged.values(), key=lambda e: (e['time'], e.get('sequence', 0)))


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
    achievements = {}
    for event in events:
        if event['kind'] == 'achievement':
            achievements[event['achievement_id']] = event
    return {
        'cast_ok': sum(e['kind'] == 'cast_ok' for e in events),
        'cast_fail': sum(e['kind'] == 'cast_fail' for e in events),
        'loot_events': len(catches),
        'catches': len(catch_times),
        'items': fish,
        'achievements': achievements,
        'last_sync': max((e['time'] for e in events), default=0),
    }
