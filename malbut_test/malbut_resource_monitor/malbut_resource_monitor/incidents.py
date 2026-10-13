"""Read completed STT evidence bundles without importing STT or connecting to ROS."""

from collections import OrderedDict
import json
from pathlib import Path
import re
from threading import Lock


DOWNLOADS = ('incident.json', 'transactions.json', 'events.jsonl', 'rosout.jsonl')
MAX_JSON_BYTES = 32 * 1024 * 1024


def reject_nonfinite(value):
    raise ValueError('Non-finite JSON value: ' + value)


class IncidentStore:
    def __init__(self, root, *, legacy_roots=()):
        self.root = Path(root).expanduser().resolve()
        self.roots = tuple(dict.fromkeys(
            [self.root, *(Path(p).expanduser().resolve() for p in legacy_roots)]))
        self._cache = OrderedDict()
        self._lock = Lock()

    def directory(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'incident-[A-Za-z0-9_-]+', name):
            raise ValueError('Invalid incident')
        for root in self.roots:
            path = root / name
            if path.is_symlink() or path.resolve().parent != root:
                raise ValueError('Incident outside evidence root')
            if path.is_dir():
                return path
        raise ValueError('Incident missing')

    def file(self, name, filename):
        if filename not in DOWNLOADS:
            raise ValueError('Unknown evidence file')
        directory = self.directory(name)
        path = directory / filename
        if path.is_symlink() or path.resolve().parent != directory or not path.is_file():
            raise ValueError('Evidence outside incident or missing')
        return path

    def json(self, name, filename):
        path = self.file(name, filename)
        with path.open('rb') as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError('Evidence JSON exceeds viewer limit')
        value = json.loads(raw, parse_constant=reject_nonfinite)
        if not isinstance(value, dict):
            raise ValueError('Evidence JSON must be an object')
        return value

    def paths(self):
        paths = {}
        for root in self.roots:
            for path in root.glob('incident-*'):
                if path.is_dir() and not path.is_symlink():
                    paths.setdefault(path.name, path)
        return [paths[name] for name in sorted(paths, reverse=True)]

    def summary(self, name):
        paths = [self.file(name, f) for f in ('incident.json', 'transactions.json')]
        signature = tuple((p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
        with self._lock:
            cached = self._cache.get(name)
            if cached and cached[0] == signature:
                self._cache.move_to_end(name)
                return cached[1]
        manifest = self.json(name, 'incident.json')
        transactions = self.json(name, 'transactions.json').get('transactions')
        triggers = manifest.get('triggers')
        if (not isinstance(transactions, list) or not isinstance(triggers, list)
                or not triggers or any(not isinstance(t, dict) for t in triggers + transactions)):
            raise ValueError('Malformed incident manifest or transactions')
        for turn in transactions:
            if (not isinstance(turn.get('utterance_id'), str)
                    or not isinstance(turn.get('stages'), list)
                    or any(not isinstance(stage, dict) for stage in turn['stages'])
                    or not isinstance(turn.get('observed'), dict)):
                raise ValueError('Malformed transaction')
        if not isinstance(manifest.get('metadata') or {}, dict):
            raise ValueError('Malformed incident metadata')
        value = dict(
            id=name, triggers=triggers,
            utterance_ids=[t['utterance_id'] for t in transactions
                           if isinstance(t.get('utterance_id'), str)],
            synthetic=bool((manifest.get('metadata') or {}).get('synthetic')),
            post_window_complete=manifest.get('post_window_complete') is True,
            audio_saved=manifest.get('audio_saved'), events=manifest.get('events'),
            capture_error=manifest.get('capture_error'),
            buffer_evictions_total=manifest.get('buffer_evictions_total', 0),
            skipped_triggers_total=manifest.get('skipped_triggers_total', 0))
        with self._lock:
            self._cache[name] = (signature, value)
            while len(self._cache) > 256:
                self._cache.popitem(last=False)
        return value

    def list(self, query=''):
        query = query.strip().casefold()
        if len(query) > 300:
            raise ValueError('Search is too long')
        items, total, unreadable = [], 0, 0
        for path in self.paths():
            try:
                item = self.summary(path.name)
                haystack = json.dumps([item['id'], item['triggers'], item['utterance_ids']],
                                      ensure_ascii=False).casefold()
                if query and query not in haystack:
                    continue
                total += 1
                if len(items) < 200:
                    items.append(item)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                unreadable += 1
        return dict(items=items, total=total, truncated=total > len(items),
                    unreadable=unreadable, root=str(self.root),
                    legacy_roots=[str(p) for p in self.roots[1:] if p.is_dir()])

    def detail(self, name):
        self.summary(name)  # Validate both documents before serving.
        return dict(id=name, manifest=self.json(name, 'incident.json'),
                    trace=self.json(name, 'transactions.json'))

    def events(self, name, *, source='all', query='', offset=0, limit=200):
        if source not in ('all', 'program', 'pipeline', 'vad', 'audio'):
            raise ValueError('Unknown event source')
        if not 0 <= offset <= 1000000 or not 1 <= limit <= 200 or len(query) > 300:
            raise ValueError('Invalid event range or search')
        rows, matching, malformed, partial = [], 0, 0, False
        query = query.casefold()
        with self.file(name, 'events.jsonl').open('rb') as stream:
            line_number = 0
            while True:
                raw = stream.readline(65538)
                if not raw:
                    break
                line_number += 1
                if len(raw) > 65537:
                    while raw and not raw.endswith(b'\n'):
                        raw = stream.readline(65538)
                    malformed += 1
                    continue
                if not raw.endswith(b'\n'):
                    partial = True
                    break
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict) or not isinstance(record.get('event'), str):
                        raise ValueError('Not an event')
                    # Also reject NaN/Infinity before sending strict JSON to the browser.
                    json.dumps(record, allow_nan=False)
                except (ValueError, UnicodeError):
                    malformed += 1
                    continue
                event = record['event']
                category = ('program' if event == 'program_log'
                            else 'vad' if event == 'vad_frame'
                            or event.startswith(('shadow_vad', 'silero_gate'))
                            else 'audio' if event == 'microphone_chunk' else 'pipeline')
                if source != 'all' and source != category:
                    continue
                if query and query not in raw.decode('utf-8').casefold():
                    continue
                if offset <= matching < offset + limit:
                    rows.append(dict(line=line_number, record=record,
                                     raw=raw.decode('utf-8').removesuffix('\n')))
                matching += 1
        return dict(rows=rows, matching=matching, offset=offset, limit=limit,
                    has_more=offset + len(rows) < matching, malformed=malformed,
                    partial_line=partial)
