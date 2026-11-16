"""Per-person turn timing shared by the robot's speech and developer controls."""

from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import unicodedata


@dataclass(frozen=True)
class ListeningProfile:
    user_id: str = ''
    mode: str = 'default'
    revision: int = 0


class ListeningProfiles:
    def __init__(self, path=None, on_change=None):
        self.on_change = on_change
        self._current = None
        state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
        self.path = Path(path) if path else state / 'malbut' / 'listening.sqlite3'

    @staticmethod
    def _read(db):
        row = db.execute("""
            SELECT a.user_id, COALESCE(p.mode, 'default'), a.revision
            FROM active a LEFT JOIN preferences p ON p.user_id = a.user_id
        """).fetchone()
        return ListeningProfile(*row) if row else ListeningProfile()

    def current(self):
        if self._current is None:
            # Do not retry broken storage on the audio loop. A successful
            # settings write refreshes the cached state after a storage repair.
            self._current = ListeningProfile()
            if self.path.is_file():
                with closing(sqlite3.connect(self.path, timeout=0.1)) as db:
                    self._current = self._read(db)
        return self._current

    def update(self, user_id, mode=None, *, expected_revision=None):
        if not isinstance(user_id, str) or (user_id and not re.fullmatch(
                r'[A-Za-z0-9][A-Za-z0-9._:@+-]{0,254}', user_id)):
            raise ValueError('Invalid listening profile user')
        if mode is not None and (not user_id or mode not in ('default', 'longer')):
            raise ValueError('Invalid listening mode')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=0.5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS active (id INTEGER PRIMARY KEY, '
                       'user_id TEXT NOT NULL, revision INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS preferences (user_id TEXT PRIMARY KEY, '
                       'mode TEXT NOT NULL)')
            active = self._read(db)
            if mode is not None:
                if active.user_id != user_id or (expected_revision is not None
                                                 and expected_revision != active.revision):
                    raise ValueError('현재 대화 설정이 바뀌었습니다. 다시 시도해 주세요.')
                if mode == active.mode:
                    return active
                db.execute('INSERT OR REPLACE INTO preferences VALUES (?, ?)', (user_id, mode))
            elif active.user_id == user_id:
                return active
            db.execute('INSERT OR REPLACE INTO active VALUES (1, ?, ?)',
                       (user_id, active.revision + 1))
            updated = self._read(db)
        self._current = updated
        if self.on_change is not None:
            self.on_change(updated)
        return updated


_LONGER = re.compile(r'(?:(?:(?:내가|제가)?말할때|(?:내|제)말을?|대답하기전에),?)?'
                     r'(?:조금|좀)더기다려(?:줘|줘요|주세요)')
_DEFAULT = re.compile(r'(?:(?:내|제)?말(?:을)?기다리는시간|말끝대기시간|대기시간)(?:을|은)?'
                      r'기본(?:값)?으로(?:바꿔|돌려|설정해)(?:줘|줘요|주세요)')


def requested_mode(text):
    phrase = ''.join(unicodedata.normalize('NFC', text).split()).rstrip('.!?。')
    for wake in ('제이크야', '제이크', '말벗아'):
        if phrase.startswith(wake):
            phrase = phrase[len(wake):].lstrip(',')
            break
    if _LONGER.fullmatch(phrase) or phrase in ('조금더기다리기로바꿔줘', '조금더기다리기로설정해줘'):
        return 'longer'
    if _DEFAULT.fullmatch(phrase) or phrase in ('기본시간만큼기다려줘', '기본시간만큼기다려주세요'):
        return 'default'
    return None
