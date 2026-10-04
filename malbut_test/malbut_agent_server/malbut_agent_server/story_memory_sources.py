"""Exact, owner-scoped references to retained conversation text.

This internal prototype resolver is not an authorization boundary: its caller
must authenticate user_id and enforce story-memory consent before using text.
It never widens the granted span or reads adjacent turns. Offsets count Python
Unicode characters, not UTF-8 bytes. A whole-message digest invalidates every
old span after a source correction, including changes outside that span.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
import sqlite3
from typing import Any, Dict, Optional


class SourceLookupError(ValueError):
    """The requested completed source or span is unavailable."""


def _identifier(value: str, name: str) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 128
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f'{name} must be a nonempty identifier')
    return value


@dataclass(frozen=True)
class SourceRef:
    """Immutable identity, exact span and revision of one completed message."""

    conversation_id: str
    session_instance_id: str
    generation: int
    turn_id: str
    role: str
    start: int
    end: int
    sha256: str

    def __post_init__(self) -> None:
        for name in ('conversation_id', 'session_instance_id', 'turn_id'):
            _identifier(getattr(self, name), name)
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError('generation must be a positive integer')
        if self.role not in ('user', 'assistant'):
            raise ValueError('role must be user or assistant')
        if (
            type(self.start) is not int or type(self.end) is not int
            or self.start < 0 or self.end <= self.start
        ):
            raise ValueError('source span must have 0 <= start < end')
        if not isinstance(self.sha256, str) or not re.fullmatch(
            r'[0-9a-f]{64}', self.sha256,
        ):
            raise ValueError('sha256 must be a lowercase message digest')

    def to_dict(self) -> Dict[str, Any]:
        """Return only source metadata, never a second copy of source text."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SourceRef':
        """Validate a persisted source reference without coercing its types."""
        return cls(**data)

    @property
    def key(self) -> str:
        """Return a canonical key including scope and source revision."""
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(',', ':'),
        )


@dataclass(frozen=True)
class SourceText:
    """An exact excerpt for inspection; stored conversation remains canonical."""

    ref: SourceRef
    text: str
    created_at: float
    completed_at: float


def _message(
    connection: sqlite3.Connection,
    user_id: str,
    conversation_id: str,
    session_instance_id: str,
    generation: int,
    turn_id: str,
    role: str,
) -> Optional[tuple]:
    _identifier(user_id, 'user_id')
    if role not in ('user', 'assistant'):
        raise ValueError('role must be user or assistant')
    # Current session generation is intentionally absent. Reset/close/expiry
    # preserve completed turns; deleting and recreating a session changes its
    # instance identity. SQL parameters never contain a caller-selected column.
    row = connection.execute(
        '''SELECT user_content, assistant_content, created_at, completed_at
           FROM conversation_turns
           WHERE user_id = ? AND conversation_id = ?
             AND session_instance_id = ? AND generation = ?
             AND turn_id = ? AND status = 'completed' ''',
        (user_id, conversation_id, session_instance_id, generation, turn_id),
    ).fetchone()
    if row is None:
        return None
    content = row[0 if role == 'user' else 1]
    if not isinstance(content, str) or not content:
        return None
    if any(
        not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in (row[2], row[3])
    ):
        return None
    return content, float(row[2]), float(row[3])


def make_source_ref(
    connection: sqlite3.Connection,
    user_id: str,
    conversation_id: str,
    session_instance_id: str,
    generation: int,
    turn_id: str,
    role: str,
    start: int = 0,
    end: Optional[int] = None,
) -> SourceRef:
    """Capture a span from an existing completed turn in a trusted workflow."""
    for name, value in (
        ('conversation_id', conversation_id),
        ('session_instance_id', session_instance_id), ('turn_id', turn_id),
    ):
        _identifier(value, name)
    if type(generation) is not int or generation < 1:
        raise ValueError('generation must be a positive integer')
    message = _message(
        connection, user_id, conversation_id, session_instance_id,
        generation, turn_id, role,
    )
    if message is None:
        raise SourceLookupError('completed source is unavailable')
    content = message[0]
    ref = SourceRef(
        conversation_id, session_instance_id, generation, turn_id, role,
        start, len(content) if end is None else end,
        hashlib.sha256(content.encode('utf-8')).hexdigest(),
    )
    if ref.end > len(content):
        raise SourceLookupError('source span exceeds the message')
    return ref


def read_source(
    connection: sqlite3.Connection, user_id: str, ref: SourceRef,
) -> Optional[SourceText]:
    """Read a valid exact excerpt or fail closed for changed/missing sources.

    Use the same SQLite transaction as the story policy and commit checks to
    prevent a source change between validation and persistence. Database errors
    are propagated, never represented as a successful empty source.
    """
    if not isinstance(ref, SourceRef):
        raise ValueError('ref must be a SourceRef')
    message = _message(
        connection, user_id, ref.conversation_id, ref.session_instance_id,
        ref.generation, ref.turn_id, ref.role,
    )
    if message is None:
        return None
    content, created_at, completed_at = message
    if (
        ref.end > len(content)
        or hashlib.sha256(content.encode('utf-8')).hexdigest() != ref.sha256
    ):
        return None
    return SourceText(ref, content[ref.start:ref.end], created_at, completed_at)
