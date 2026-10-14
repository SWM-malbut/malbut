"""Durable evidence uses real SQLite and the real bounded provider payload."""

import hashlib
import json
import sqlite3
from unittest.mock import patch

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.memory_trace import observe_inputs, record_trace
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.schemas import AgentRequest
from test_memory_providers import _request, _response
from test_story_memory_service import FakeExtractor
from test_story_runtime_store import RuntimeHarness
from malbut_agent_server.story_extractor import StoryExtractionError
from malbut_agent_server.story_memory_service import StoryMemoryService


def events(path, request=None):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM memory_trace_events' +
                            (' WHERE request_id=?' if request else '') + ' ORDER BY sequence',
                            (request,) if request else ()).fetchall()
        return [{**dict(row), 'data': json.loads(row['data_json'])} for row in rows]


def test_memory_receipt_commits_and_rolls_back_with_the_fact(tmp_path):
    path = str(tmp_path / 'memory.sqlite3')
    store = SQLiteMemoryStore(path)
    store.add('alice', '저장한 가상 이름', metadata={'source': {'request_id': 'saved'}})
    with pytest.raises(RuntimeError):
        with store._transaction(write=True) as conn:
            store.add('alice', '롤백할 비밀', connection=conn,
                      metadata={'source': {'request_id': 'rolled-back'}})
            raise RuntimeError('abort')
    store.close()
    rows = events(path)
    assert [row['request_id'] for row in rows] == ['saved']
    assert rows[0]['event'] == 'fact_committed'
    assert '저장한 가상 이름' not in json.dumps(rows, ensure_ascii=False)
    assert '롤백할 비밀' not in json.dumps(rows, ensure_ascii=False)


def test_trace_failure_does_not_commit_or_break_owner_transaction():
    conn = sqlite3.connect(':memory:')
    conn.execute('BEGIN')
    assert not record_trace(conn, 'invalid', 'alice', data={'bad': object()})
    assert conn.in_transaction
    conn.rollback()
    conn.close()


def test_actual_openai_transport_payload_is_observed_after_memory_truncation(tmp_path):
    store = SQLiteMemoryStore(str(tmp_path / 'memories.sqlite3'))
    memories = [store.add('private-user', '가상 기억' * 600) for _ in range(8)]
    observed, sent = [], []
    def transport(_url, _headers, payload, _timeout):
        sent.append(payload)
        return _response(None)
    provider = OpenAIResponsesProvider('offline-test-key', 'offline-model', transport=transport)
    with observe_inputs(lambda event, data: observed.append((event, data))):
        provider.complete(_request(), memories, [], [], memory_context={'enabled': False})
    assert len(observed) == len(sent) == 1
    event, receipt = observed[0]
    # The provider sends this exact prepared text, not the pre-budget candidates.
    text = sent[0]['input']
    context = json.loads(text[text.index('{'):])
    assert event == 'model_input'
    assert receipt['input_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert receipt['fact_ids'] == [item['id'] for item in context['memory_context_untrusted']]
    assert len(receipt['fact_ids']) < len(memories)
    assert '가상 기억' not in json.dumps(receipt, ensure_ascii=False)
    store.close()


def test_restart_recall_has_source_commit_and_actual_model_input_receipts(tmp_path):
    path = str(tmp_path / 'conversation.sqlite3')
    sent = []
    def transport(_url, _headers, payload, _timeout):
        sent.append(payload)
        return _response(None)
    def runtime():
        backend = OpenAIResponsesProvider('offline-test-key', 'offline-model', transport=transport)
        with patch('malbut_agent_server.factory.build_provider', return_value=backend):
            return build_orchestrator(Settings(provider='mock', database_path=path),
                                      story_extractor=FakeExtractor())
    def turn(current, request_id, room, text):
        current.conversation_store.create('alice', room)
        return current.handle(AgentRequest.from_dict({
            'request_id': request_id, 'turn_id': request_id, 'conversation_id': room,
            'user_id': 'alice', 'utterance': text, 'robot_state': {}, 'available_tools': []}))
    first = runtime()
    try:
        first.story_memory.enable('alice')
        turn(first, 'source', 'first-room', '전시에서 조용한 그림을 보니 편안했어.')
        assert first.checkpoint_story_memory('alice', timeout=5)
        story_id = first.story_memory.list_stories('alice')[0]['story_id']
    finally:
        first.close()
    second = runtime()
    try:
        turn(second, 'recall', 'second-room', '전시에서 어떤 기분이었다고 했지?')
        rows = events(path, 'recall')
        assert [r['event'] for r in rows[:4]] == [
            'memory_retrieved', 'model_input', 'provider_completed', 'turn_committed']
        assert rows[1]['data']['stories'] == [{'story_id': story_id, 'version': 1}]
        assert rows[1]['data']['history_messages'] == 0
        assert any(r['event'] == 'story_committed' for r in events(path, 'source'))
        assert '편안했어' not in json.dumps(events(path), ensure_ascii=False)
    finally:
        second.close()


def test_failure_and_explicit_recovery_keep_durable_safe_attempt_history(tmp_path):
    runtime = RuntimeHarness(tmp_path / 'failures.sqlite3')
    runtime.allow()
    token, job = runtime.claim_turn('전시 기억을 정리해줘. 합성 원문은 로그에 복제하지 마.')
    class BadExtractor:
        def extract(self, stories, sources):
            raise StoryExtractionError('entry actor does not match its sources')
    service = StoryMemoryService(runtime.conversations, runtime.memories, BadExtractor(),
                                runtime_store=runtime.store, autostart=False)
    try:
        for attempt in range(3):
            if attempt:
                runtime.now += 10
                job = runtime.store.claim('alice')
            service._process(job)
        rows = events(runtime.path, token.request_id)
        failures = [r for r in rows if r['event'] == 'story_extraction_failed']
        assert [r['data']['attempt'] for r in failures] == [1, 2, 3]
        assert [r['data']['retry_scheduled'] for r in failures] == [True, True, False]
        assert all(r['data']['error_code'] == 'actor_source_mismatch' for r in failures)
        assert all(r['conversation_id'] == token.conversation_id for r in failures)
        assert '합성 원문' not in json.dumps(rows, ensure_ascii=False)
        assert runtime.store.retry_failed('alice') == 1
        job = runtime.store.claim('alice')
        assert runtime.store.commit(job, [runtime.update(job)])
        rows = events(runtime.path, token.request_id)
        assert [r['event'] for r in rows][-3:] == [
            'story_retry_requested', 'story_extraction_started', 'story_committed']
        assert len([r for r in rows if r['event'] == 'story_extraction_failed']) == 3
    finally:
        service.close()
        runtime.close()


def test_failure_trace_is_attributed_to_each_batch_member_without_raw_error(tmp_path):
    runtime = RuntimeHarness(tmp_path / 'batch.sqlite3')
    try:
        runtime.allow()
        tokens = [runtime.complete(text) for text in ['전시 안내 첫 이야기', '전시 안내 두 번째 이야기']]
        for token in tokens:
            runtime.store.enqueue_completed('alice', token.request_id)
        job = runtime.store.claim('alice', batch_size=3)
        runtime.store.fail(job, 'synthetic-private-error-body')
        for token in tokens:
            rows = events(runtime.path, token.request_id)
            assert [r['event'] for r in rows] == [
                'turn_committed', 'story_queued', 'story_extraction_started', 'story_extraction_failed']
            assert rows[-1]['data']['error_code'] == 'processing_failed'
            assert 'synthetic-private-error-body' not in json.dumps(rows)
    finally:
        runtime.close()
