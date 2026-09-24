"""Storage races and retention for asynchronous semantic compaction."""

from dataclasses import replace
import json
import threading

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.personal_memory import PersonalMemory
from malbut_agent_server.summarization import SummaryResult
from test_conversation import FakeClock, _begin, _complete, _response


def _result(content='이전 대화의 의미 요약'):
    return SummaryResult(content, '{}', 'openai-semantic-v1')


def _seed(store, count=12):
    store.create('user-a', 'conversation-a')
    for number in range(1, count + 1):
        _complete(store, number)


def test_semantic_context_keeps_all_raw_until_compaction_without_old_caps():
    """Foreground commits never call the old synchronous summarizer."""
    class UnavailableSummarizer:
        def update(self, **kwargs):
            pytest.fail('semantic foreground must not summarize')

    store = SQLiteConversationStore(
        ':memory:', semantic_context=True, max_turns_per_session=10,
        summarizer=UnavailableSummarizer(),
    )
    try:
        _seed(store)
        begin = _begin(store, 13)
        assert begin.summary is None
        assert [turn.ordinal for turn in begin.history] == list(range(1, 13))
        before = store.get('user-a', 'conversation-a')
        content = '원문보다 길어도 저장 계층은 자르지 않음. ' * 600
        assert store.apply_compaction(
            begin.token, begin.summary, begin.history[:5], _result(content),
        )
        assert store.get('user-a', 'conversation-a') == before
        summary = store.get_summary('user-a', 'conversation-a')
        assert summary.content == content
        assert summary.source_end_ordinal == 5
        assert len(store.list_turns('user-a', 'conversation-a')) == 12
        store.complete_turn(begin.token, '답변 13', _response(13))
        next_begin = _begin(store, 14)
        assert next_begin.summary == summary
        assert [turn.ordinal for turn in next_begin.history] == list(range(6, 14))
        store.fail_turn(next_begin.token)
    finally:
        store.close()


def test_late_compaction_preserves_new_turn_and_pending_foreground_token():
    """A real background commit can overlap the next ordinary inference."""
    store = SQLiteConversationStore(':memory:', semantic_context=True)
    release = threading.Event()
    started = threading.Event()
    applied = []
    try:
        _seed(store)
        begin = _begin(store, 13)

        def finish_background():
            started.set()
            assert release.wait(5)
            applied.append(store.apply_compaction(
                begin.token, begin.summary, begin.history[:6], _result(),
            ))

        worker = threading.Thread(target=finish_background)
        worker.start()
        assert started.wait(5)
        store.complete_turn(begin.token, '답변 13', _response(13))
        incoming = _begin(store, 14)
        before = store.get('user-a', 'conversation-a')
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert applied == [True]
        assert store.get('user-a', 'conversation-a') == before
        store.complete_turn(incoming.token, '답변 14', _response(14))
        next_begin = _begin(store, 15)
        assert [turn.ordinal for turn in next_begin.history] == list(range(7, 15))
        assert not store.apply_compaction(
            begin.token, begin.summary, begin.history[:6], _result('늦은 중복'),
        )
        store.fail_turn(next_begin.token)
    finally:
        release.set()
        store.close()


@pytest.mark.parametrize('lifecycle', ['reset', 'delete', 'expire', 'close'])
def test_lifecycle_invalidates_compaction_and_retains_committed_sources(lifecycle):
    """Lifecycle fencing keeps old output from resurrecting stale context."""
    clock = FakeClock()
    store = SQLiteConversationStore(
        ':memory:', semantic_context=True, ttl_seconds=60, clock=clock,
    )
    try:
        _seed(store, 3)
        begin = _begin(store, 4)
        assert store.apply_compaction(
            begin.token, None, begin.history[:1], _result(),
        )
        store.complete_turn(begin.token, '답변 4', _response(4))
        later = _begin(store, 5)
        if lifecycle == 'reset':
            store.reset('user-a', 'conversation-a')
        elif lifecycle == 'delete':
            store.delete('user-a', 'conversation-a')
            store.create('user-a', 'conversation-a')
        elif lifecycle == 'expire':
            clock.advance(60)
        else:
            store.close_session('user-a', 'conversation-a')
        assert not store.apply_compaction(
            later.token, later.summary, later.history[:1], _result('오래된 작업'),
        )
        assert store._connection.execute(
            'SELECT COUNT(*) FROM conversation_turns',
        ).fetchone()[0] == (0 if lifecycle == 'delete' else 4)
        assert store._connection.execute(
            'SELECT COUNT(*) FROM conversation_summaries',
        ).fetchone()[0] == (0 if lifecycle == 'delete' else 1)
    finally:
        store.close()


def test_legacy_summary_is_rebuilt_from_full_raw_and_survives_restart(tmp_path):
    """Migration does not reuse old extractive text or omit its raw sources."""
    database = str(tmp_path / 'conversation.sqlite3')
    legacy = SQLiteConversationStore(database)
    _seed(legacy)
    legacy_summary = legacy.get_summary('user-a', 'conversation-a')
    assert legacy_summary is not None
    legacy.close()
    store = SQLiteConversationStore(database, semantic_context=True)
    try:
        begin = _begin(store, 13)
        assert begin.summary is None
        assert len(begin.history) == 12
        assert store.apply_compaction(
            begin.token, legacy_summary, begin.history[:4], _result(),
        )
        store.complete_turn(begin.token, '답변 13', _response(13))
        saved = store.get_summary('user-a', 'conversation-a')
    finally:
        store.close()
    restarted = SQLiteConversationStore(database, semantic_context=True)
    try:
        assert restarted.get_summary('user-a', 'conversation-a') == saved
        begin = _begin(restarted, 14)
        assert [turn.ordinal for turn in begin.history] == list(range(5, 14))
        assert len(restarted.list_turns('user-a', 'conversation-a')) == 13
        restarted.fail_turn(begin.token)
    finally:
        restarted.close()


def test_expired_session_quota_does_not_delete_raw_or_summary():
    """Only active sessions consume the semantic admission quota."""
    clock = FakeClock()
    store = SQLiteConversationStore(
        ':memory:', semantic_context=True, ttl_seconds=60,
        max_sessions_per_user=1, clock=clock,
    )
    try:
        _seed(store, 2)
        begin = _begin(store, 3)
        assert store.apply_compaction(
            begin.token, None, begin.history[:1], _result(),
        )
        store.complete_turn(begin.token, '답변 3', _response(3))
        clock.advance(60)
        store.create('user-a', 'next-conversation')
        assert store.get('user-a', 'conversation-a').status == 'expired'
        assert store._connection.execute(
            'SELECT COUNT(*) FROM conversation_turns',
        ).fetchone()[0] == 3
        assert store._connection.execute(
            'SELECT COUNT(*) FROM conversation_summaries',
        ).fetchone()[0] == 1
        assert store.delete('user-a', 'conversation-a')
        assert store._connection.execute(
            'SELECT COUNT(*) FROM conversation_turns',
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_policy_guard_and_redacted_rebuild_keep_all_eligible_turns():
    """A policy change rejects old output; sanitized identities can be rebuilt."""
    store = SQLiteConversationStore(':memory:', semantic_context=True)
    memory = SQLiteMemoryStore(':memory:')
    personal = PersonalMemory(memory, store)
    try:
        _seed(store)
        begin = _begin(store, 13)
        revision = memory.policy_state('user-a')['revision']
        memory.invalidate_answers('user-a')

        def guard(connection):
            assert connection is store._connection and connection.in_transaction
            return memory.policy_state('user-a', connection)['revision'] == revision

        assert not store.apply_compaction(
            begin.token, None, begin.history[:3], _result(), guard=guard,
        )
        revision = memory.policy_state('user-a')['revision']
        assert not store.apply_compaction(
            begin.token, None, begin.history[1:3], _result(), guard=guard,
        )
        assert not store.apply_compaction(
            begin.token, None,
            [replace(begin.history[0], turn_id='forged-turn')], _result(),
        )
        assert store.apply_compaction(
            begin.token, None, begin.history[:3], _result(), guard=guard,
        )
        summary = store.get_summary('user-a', 'conversation-a')
        state = memory.set_personalization('user-a', False, {
            'conversation_id': 'conversation-a',
            'session_instance_id': begin.token.session_instance_id,
            'generation': begin.token.generation,
            'turn_id': 'turn-13', 'request_id': 'request-13',
            'text': '개인화를 중단해줘',
        })
        revision = state['revision']
        eligible, clean_summary, _deps = personal._context(
            'user-a', begin.token, state, begin.history[3:], summary,
            store._connection,
        )
        assert clean_summary is None
        assert len(eligible) == 12
        assert all(not turn.user_content for turn in eligible)
        assert store.apply_compaction(
            begin.token, summary, eligible[:4], replace(
                _result('삭제 정보 없이 재요약'),
                state_json=json.dumps({'memory_policy_state': state}),
            ),
            guard=guard,
        )
        rebuilt = store.get_summary('user-a', 'conversation-a')
        assert rebuilt.source_end_ordinal == 4
        reused, clean_summary, _deps = personal._context(
            'user-a', begin.token, state, eligible[4:], rebuilt,
            store._connection,
        )
        assert clean_summary == rebuilt
        assert [turn.ordinal for turn in reused] == list(range(5, 13))
        changed = memory.invalidate_answers('user-a')
        rebuilt_history, clean_summary, _deps = personal._context(
            'user-a', begin.token, changed, reused, rebuilt, store._connection,
        )
        assert clean_summary is None
        assert len(rebuilt_history) == 12
        assert store.list_turns('user-a', 'conversation-a')[0].user_content
        store.complete_turn(begin.token, '답변 13', _response(13))
    finally:
        memory.close()
        store.close()
