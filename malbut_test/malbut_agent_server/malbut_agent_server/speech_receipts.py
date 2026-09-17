"""Persist transcript receipts without storing the spoken text."""

import hashlib
from pathlib import Path
import sqlite3
import time
from typing import Optional


class SpeechReceiptStore:
    """Accept each utterance ID once, including across process restarts."""

    def __init__(self, db_path: str) -> None:
        """Open a receipt file and add its table non-destructively."""
        if db_path != ':memory:':
            path = Path(db_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            db_path = str(path)
        self._connection = sqlite3.connect(db_path, timeout=5.0)
        try:
            self._connection.execute(
                '''CREATE TABLE IF NOT EXISTS speech_receipts (
                    utterance_id TEXT NOT NULL PRIMARY KEY,
                    text_sha256 TEXT NOT NULL,
                    received_at REAL NOT NULL
                )'''
            )
            self._connection.commit()
        except Exception:
            self._connection.close()
            raise

    @staticmethod
    def _digest(utterance_id: str, text: str) -> str:
        if not isinstance(utterance_id, str) or not utterance_id.strip():
            raise ValueError('utterance_id must not be blank')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('text must not be blank')
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def lookup(self, utterance_id: str, text: str) -> Optional[str]:
        """Check an existing receipt without consuming an unprocessed ID."""
        digest = self._digest(utterance_id, text)
        previous = self._connection.execute(
            'SELECT text_sha256 FROM speech_receipts WHERE utterance_id = ?',
            (utterance_id,),
        ).fetchone()
        if previous is None:
            return None
        return 'duplicate' if previous[0] == digest else 'conflict'

    def receive(self, utterance_id: str, text: str) -> str:
        """Commit a new receipt or identify a duplicate or conflicting ID."""
        digest = self._digest(utterance_id, text)
        with self._connection:
            inserted = self._connection.execute(
                '''INSERT INTO speech_receipts
                   (utterance_id, text_sha256, received_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(utterance_id) DO NOTHING''',
                (utterance_id, digest, time.time()),
            ).rowcount
            if inserted:
                outcome = 'received'
            else:
                previous = self._connection.execute(
                    '''SELECT text_sha256 FROM speech_receipts
                       WHERE utterance_id = ?''',
                    (utterance_id,),
                ).fetchone()[0]
                outcome = 'duplicate' if previous == digest else 'conflict'
        return outcome

    def close(self) -> None:
        """Close the receipt database connection."""
        self._connection.close()
