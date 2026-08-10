"""Tests for user-isolated SQLite memory retrieval."""

import stat
import time

import pytest

from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.schemas import ValidationError


def test_korean_memory_retrieval_and_user_isolation() -> None:
    """Korean particles should not prevent recall or cross user scope."""
    store = SQLiteMemoryStore(':memory:')
    try:
        store.add('user-a', '반려견 이름은 초코')
        store.add('user-b', '반려견 이름은 보리')

        result_a = store.search(
            'user-a',
            '우리 강아지 이름이 뭐였지?',
        )
        result_b = store.search(
            'user-b',
            '우리 강아지 이름이 뭐였지?',
        )

        assert [item.content for item in result_a] == [
            '반려견 이름은 초코'
        ]
        assert [item.content for item in result_b] == [
            '반려견 이름은 보리'
        ]
    finally:
        store.close()


def test_expired_memory_is_not_returned() -> None:
    """Expired records may remain stored but must not enter a prompt."""
    store = SQLiteMemoryStore(':memory:')
    try:
        store.add(
            'user-a',
            '반려견 이름은 오래된이름',
            expires_at=time.time() - 1,
        )
        assert store.search(
            'user-a',
            '반려견 이름이 뭐였지?',
        ) == []
        assert store.purge_expired() == 1
    finally:
        store.close()


def test_irrelevant_memory_is_not_returned() -> None:
    """Recency alone must not pull unrelated facts into context."""
    store = SQLiteMemoryStore(':memory:')
    try:
        store.add('user-a', '초코의 예방접종은 금요일')
        assert store.search('user-a', '오늘 날씨가 어때?') == []
    finally:
        store.close()


def test_falsey_non_object_metadata_is_rejected() -> None:
    """An empty list must not silently become an empty JSON object."""
    store = SQLiteMemoryStore(':memory:')
    try:
        with pytest.raises(ValidationError):
            store.add('user-a', '기억', metadata=[])
    finally:
        store.close()


def test_file_permissions_and_scoped_delete(tmp_path) -> None:
    """Persistent memory is private and deletion checks its owner."""
    database = tmp_path / 'memory.sqlite3'
    store = SQLiteMemoryStore(str(database))
    try:
        record = store.add('user-a', '삭제할 기억')
        mode = stat.S_IMODE(database.stat().st_mode)
        assert mode == 0o600
        assert store.delete('user-b', record.id) is False
        assert store.delete('user-a', record.id) is True
    finally:
        store.close()
