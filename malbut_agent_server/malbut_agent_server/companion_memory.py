"""Summary-only daily memory; identity and consent are supplied by the app bridge.

This store never accepts conversation turns, evidence quotes, images or audio.
It is separate from the existing fact/story stores and their durable work queues.
"""

from calendar import monthrange
from contextlib import contextmanager
from datetime import datetime
import json
import math
import re
from pathlib import Path
import sqlite3
from threading import RLock
import time
from uuid import uuid4
from zoneinfo import ZoneInfo


CATEGORIES = frozenset({'daily', 'health', 'family'})
EVENT_STATES = frozenset({'planned', 'completed', 'cancelled', 'uncertain'})


def expires_at(observed_at):
    """Three calendar months from observation in the household's Korean time zone."""
    observed = datetime.fromtimestamp(observed_at, ZoneInfo('Asia/Seoul'))
    year, month = divmod(observed.year * 12 + observed.month - 1 + 3, 12)
    month += 1
    return observed.replace(year=year, month=month,
                            day=min(observed.day, monthrange(year, month)[1])).timestamp()


def validate_summary(item):
    if not isinstance(item, dict) or set(item) != {'summary', 'categories', 'event_state', 'event_date'}:
        raise ValueError('invalid daily summary')
    if not isinstance(item['summary'], str) or not 1 <= len(item['summary'].strip()) <= 280:
        raise ValueError('invalid summary length')
    categories = item['categories']
    if (not isinstance(categories, list) or not categories
            or any(type(c) is not str or c not in CATEGORIES for c in categories)
            or len(categories) != len(set(categories)) or 'daily' not in categories):
        raise ValueError('invalid summary categories')
    if type(item['event_state']) is not str or item['event_state'] not in EVENT_STATES:
        raise ValueError('invalid event state')
    date = item['event_date']
    if date is not None and (not isinstance(date, str) or len(date) != 10
                             or datetime.strptime(date, '%Y-%m-%d').date().isoformat() != date):
        raise ValueError('invalid event date')
    return dict(item, summary=item['summary'].strip(), categories=sorted(categories))


class CompanionMemory:
    def __init__(self, path, *, clock=time.time):
        self.clock, self._lock = clock, RLock()
        if str(path) != ':memory:':
            target = Path(path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise ValueError('refuse linked companion database')
            target.touch(mode=0o600, exist_ok=True)
            target.chmod(0o600)
            path = str(target)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA secure_delete=ON')
        # No persistent WAL or source-containing audit/FTS/embedding tables.
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS companion_policy (
                owner TEXT PRIMARY KEY, revision INTEGER NOT NULL, erase_revision INTEGER NOT NULL,
                categories TEXT NOT NULL, external_processing INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS companion_episodes (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, turn_id TEXT NOT NULL,
                summary TEXT NOT NULL, categories TEXT NOT NULL,
                event_state TEXT NOT NULL, event_date TEXT,
                observed_at REAL NOT NULL, expires_at REAL NOT NULL,
                UNIQUE(owner, turn_id, summary));
            CREATE INDEX IF NOT EXISTS companion_expiry ON companion_episodes(expires_at);
        ''')
        self.purge()

    @contextmanager
    def transaction(self):
        with self._lock, self.db:
            yield self.db

    def purge(self):
        with self.transaction() as db:
            db.execute('DELETE FROM companion_episodes WHERE expires_at<=?', (self.clock(),))

    def apply_policy(self, owner, revision, erase_revision, categories, external_processing):
        """Withdrawing a category removes its summaries, including mixed-category rows."""
        categories = set(categories)
        if (not isinstance(owner, str) or not owner or len(owner) > 200
                or type(revision) is not int or revision < 0
                or type(erase_revision) is not int or erase_revision < 0
                or not categories <= CATEGORIES or type(external_processing) is not bool):
            raise ValueError('invalid companion policy')
        allowed = categories if external_processing and 'daily' in categories else set()
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM companion_policy WHERE owner=?', (owner,)).fetchone()
            if prior and revision < prior['revision']:
                raise ValueError('stale companion policy')
            if prior and erase_revision < prior['erase_revision']:
                raise ValueError('stale erasure revision')
            if prior and revision == prior['revision']:
                if (erase_revision != prior['erase_revision']
                        or sorted(allowed) != json.loads(prior['categories'])
                        or external_processing != bool(prior['external_processing'])):
                    raise ValueError('conflicting companion policy revision')
                return
            if not prior or erase_revision > prior['erase_revision']:
                db.execute('DELETE FROM companion_episodes WHERE owner=?', (owner,))
            for row in db.execute('SELECT id,categories FROM companion_episodes WHERE owner=?', (owner,)):
                if not set(json.loads(row['categories'])) <= allowed:
                    db.execute('DELETE FROM companion_episodes WHERE id=?', (row['id'],))
            db.execute('INSERT OR REPLACE INTO companion_policy VALUES (?,?,?,?,?)',
                       (owner, revision, erase_revision, json.dumps(sorted(allowed)), int(external_processing)))

    def policy(self, owner):
        with self._lock:
            row = self.db.execute('SELECT * FROM companion_policy WHERE owner=?', (owner,)).fetchone()
            return dict(row, categories=json.loads(row['categories'])) if row else None

    def save(self, owner, revision, turn_id, observed_at, summaries):
        summaries = [validate_summary(item) for item in summaries]
        if (len(summaries) > 3 or not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 256
                or not math.isfinite(observed_at) or observed_at > self.clock() + 1):
            raise ValueError('invalid daily observation')
        expiry = expires_at(observed_at)
        with self.transaction() as db:
            policy = self.policy(owner)
            if not policy or policy['revision'] != revision or expiry <= self.clock():
                return 0
            count = 0
            for item in summaries:
                if not set(item['categories']) <= set(policy['categories']):
                    continue
                count += db.execute('INSERT OR IGNORE INTO companion_episodes VALUES (?,?,?,?,?,?,?,?,?)',
                                    (uuid4().hex, owner, turn_id, item['summary'],
                                     json.dumps(item['categories']), item['event_state'], item['event_date'],
                                     observed_at, expiry)).rowcount
            return count

    def recall(self, owner, limit=12, query=''):
        """Newest observations first; reading never extends retention."""
        self.purge()
        with self.transaction() as db:
            policy = self.policy(owner)
            if not policy:
                return []
            terms = set(re.findall(r'[가-힣a-z0-9]{2,}', query.casefold()))
            # Strip common Korean particles to match "영희는" against "영희와".
            terms |= {re.sub(r'(?:에게|하고|이랑|랑|와|과|은|는|을|를|의)$', '', t) for t in terms}
            terms = sorted(t for t in terms if len(t) >= 2)[:16]
            score = ' + '.join('CASE WHEN instr(lower(summary),?)>0 THEN 1 ELSE 0 END' for _ in terms) or '0'
            rows = db.execute('SELECT *,(' + score + ') AS relevance FROM companion_episodes '
                              'WHERE owner=? AND expires_at>? ORDER BY relevance DESC,observed_at DESC,id DESC LIMIT ?',
                              (*terms, owner, self.clock(), min(max(int(limit), 1), 50))).fetchall()
            return [{key: row[key] for key in ('id', 'summary', 'event_state', 'event_date',
                                              'observed_at', 'expires_at')}
                    for row in rows if set(json.loads(row['categories'])) <= set(policy['categories'])]

    def forget(self, owner, *, latest=False):
        with self.transaction() as db:
            if latest:
                return db.execute('DELETE FROM companion_episodes WHERE owner=? AND turn_id IN '
                                  '(SELECT turn_id FROM companion_episodes WHERE owner=? '
                                  'ORDER BY observed_at DESC,id DESC LIMIT 1)', (owner, owner)).rowcount
            return db.execute('DELETE FROM companion_episodes WHERE owner=?', (owner,)).rowcount

    def close(self):
        with self._lock:
            self.db.close()
