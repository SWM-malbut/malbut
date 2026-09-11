"""Offline queue tests with real transactions and independent DB handles."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

import pytest

from malbut_agent_server.automatic_memory_jobs import AutomaticMemoryJobs
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.schemas import (
    AgentDecision, AgentRequest, ProviderResult, RobotState, ValidationError,
)


class Harness:
    """Stage completed turns without providers or a background worker."""

    def __init__(self, path, max_pending=64):
        self.path = str(path)
        self.now = 1000.0
        self.store = SQLiteConversationStore(self.path, clock=lambda: self.now)
        self.jobs = AutomaticMemoryJobs(
            self.store, max_pending=max_pending, clock=lambda: self.now,
        )
        self.count = 0

    @contextmanager
    def transaction(self):
        with self.store._lock:
            conn = self.store._connection
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def stage(self, user='alice', conversation='room', callback=None,
              *, extract=False):
        self.count += 1
        self.store.create(user, conversation)
        request = AgentRequest(
            request_id=f'request-{self.count}', user_id=user,
            conversation_id=conversation, turn_id=f'turn-{self.count}',
            utterance='난 김민재야', robot_state=RobotState(), available_tools=(),
        )
        token = self.store.begin_turn(
            user, conversation, request.turn_id, request.request_id,
            f'fingerprint-{self.count}', request.utterance,
        ).token
        snapshot = SimpleNamespace(
            pending=None,
            source={
                'user_id': user, 'conversation_id': conversation,
                'session_instance_id': token.session_instance_id,
                'generation': token.generation, 'turn_id': token.turn_id,
                'request_id': token.request_id, 'text': request.utterance,
                'unused_history': 'private-history-must-not-be-retained',
            },
            state={'enabled': True, 'revision': 7, 'legacy_cutoff': 0.0},
        )
        decision = AgentDecision(type='message', message='private-response')
        result = SimpleNamespace(
            decision=decision, raw_decision=decision,
            provider_result=ProviderResult(
                decision=decision, provider='offline', model='offline',
                latency_ms=0.0, memory_supported=True,
                memory_proposal={
                    'operation': 'remember', 'facts': [{
                        'kind': 'name', 'subject': 'user', 'attribute': 'name',
                        'value': '김민재', 'evidence': request.utterance,
                    }], 'target_ids': [], 'query': '',
                    'evidence': request.utterance,
                },
            ),
        )
        accepted = []

        def finish(conn):
            if callback:
                callback(conn, request, token, snapshot, result)
            accepted.append(self.jobs.enqueue(
                conn, request, token, snapshot, result, extract=extract,
            ))
            return decision.message, {}

        self.store.complete_turn(
            token, decision.message, {}, commit_callback=finish,
        )
        return request, token, snapshot, result, accepted[0]

    def row(self, user_id, request_id):
        row = self.store._connection.execute(
            'SELECT * FROM automatic_memory_jobs '
            'WHERE user_id=? AND request_id=?',
            (user_id, request_id),
        ).fetchone()
        return dict(row) if row else None


@pytest.fixture
def queue(tmp_path):
    harness = Harness(tmp_path / 'jobs.sqlite3')
    yield harness
    harness.store.close()


def test_queue_payload_is_minimal_durable_and_metadata_is_content_free(queue):
    request, _token, snapshot, result, accepted = queue.stage()
    assert accepted
    row = queue.row(request.user_id, request.request_id)
    payload = json.loads(row['payload_json'])
    assert set(payload) == {'source', 'proposal', 'state'}
    assert set(payload['source']) == {
        'user_id', 'conversation_id', 'session_instance_id', 'generation',
        'turn_id', 'request_id', 'text',
    }
    assert payload['state'] == snapshot.state
    assert 'private-response' not in row['payload_json']
    assert 'private-history' not in row['payload_json']
    result.provider_result.memory_proposal['facts'][0]['value'] = 'changed'
    other_store = SQLiteConversationStore(queue.path, clock=lambda: queue.now)
    try:
        other = AutomaticMemoryJobs(other_store, clock=lambda: queue.now)
        job = other.claim_next()
        assert job['payload']['proposal']['facts'][0]['value'] == '김민재'
        assert job['state'] == 'running'
        assert 'payload_json' not in job
        assert other.claim_next() is None
    finally:
        other_store.close()
    metadata = queue.jobs.metadata(request.user_id, request.request_id)
    assert set(metadata) == {
        'state', 'created_at', 'expires_at', 'started_at', 'finished_at',
        'review_ms', 'reason', 'review_input_tokens', 'review_output_tokens',
        'review_total_tokens',
        'extraction_ms', 'extraction_input_tokens', 'extraction_output_tokens',
        'extraction_total_tokens',
    }
    assert metadata['state'] == 'running'
    assert queue.jobs.metadata('another-user', request.request_id) == {}


def test_extraction_job_reserves_source_without_foreground_proposal(queue):
    def answer_only(_conn, _request, _token, _snapshot, result):
        result.provider_result.memory_proposal = None
        result.provider_result.memory_supported = False

    request, *_rest, accepted = queue.stage(
        callback=answer_only, extract=True,
    )
    assert accepted
    row = queue.row(request.user_id, request.request_id)
    payload = json.loads(row['payload_json'])
    assert set(payload) == {'version', 'mode', 'source', 'state', 'proposal'}
    assert payload['version'] == 2 and payload['mode'] == 'extract'
    assert payload['proposal'] is None
    assert 'private-response' not in row['payload_json']
    assert 'private-history' not in row['payload_json']
    job = queue.jobs.claim_next()
    with queue.transaction() as conn:
        assert queue.jobs.valid(conn, job)
        assert queue.jobs.finish(
            conn, job, 'saved', review_ms=30,
            review_usage={'input_tokens': 40, 'output_tokens': 20,
                          'total_tokens': 60},
            extraction_ms=12.5,
            extraction_usage={'input_tokens': 10, 'output_tokens': 5,
                              'total_tokens': 15},
        )
    metadata = queue.jobs.metadata(request.user_id, request.request_id)
    assert metadata['extraction_ms'] == 12.5
    assert metadata['extraction_total_tokens'] == 15
    assert metadata['review_total_tokens'] == 60
    row = queue.row(request.user_id, request.request_id)
    assert row['payload_json'] is None


@pytest.mark.parametrize('all_legacy', [True, False])
def test_sequence_migration_preserves_identity_without_collisions(
    queue, all_legacy,
):
    first, *_ = queue.stage()
    second, *_ = queue.stage()
    second_sequence = queue.row('alice', second.request_id)['queue_sequence']
    with queue.transaction() as conn:
        if all_legacy:
            conn.execute('UPDATE automatic_memory_jobs '
                         'SET queue_sequence=NULL')
        else:
            conn.execute('UPDATE automatic_memory_jobs '
                         'SET queue_sequence=NULL WHERE request_id=?',
                         (first.request_id,))
    other_store = SQLiteConversationStore(queue.path, clock=lambda: queue.now)
    try:
        AutomaticMemoryJobs(other_store, clock=lambda: queue.now)
        migrated = [queue.row('alice', request.request_id)['queue_sequence']
                    for request in (first, second)]
        assert len(set(migrated)) == 2
        assert migrated[0] > second_sequence
        if all_legacy:
            assert migrated[1] > migrated[0]
        else:
            assert migrated[1] == second_sequence
        # Reopening is idempotent; the existing handle also uses the counter.
        AutomaticMemoryJobs(other_store, clock=lambda: queue.now)
        assert migrated == [
            queue.row('alice', request.request_id)['queue_sequence']
            for request in (first, second)
        ]
        third, *_ = queue.stage()
        assert queue.row('alice', third.request_id)['queue_sequence'] > max(
            migrated,
        )
    finally:
        other_store.close()


@pytest.mark.parametrize('bad', [-1, True, float('nan'), float('inf')])
def test_bad_extraction_metrics_cannot_finish_claim(queue, bad):
    queue.stage(extract=True)
    job = queue.jobs.claim_next()
    with queue.transaction() as conn:
        with pytest.raises(ValueError):
            queue.jobs.finish(conn, job, 'saved', extraction_ms=bad)
        with pytest.raises(ValueError):
            queue.jobs.finish(conn, job, 'saved',
                              extraction_usage={'input_tokens': bad})
        assert queue.jobs.valid(conn, job)


@pytest.mark.parametrize('field,value', [
    ('text', '난 다른사람이야'),
    ('session_instance_id', 'another-session'),
    ('generation', 999),
])
def test_claim_checks_source_against_durable_origin(queue, field, value):
    queue.stage(extract=True)
    job = queue.jobs.claim_next()
    with queue.transaction() as conn:
        assert queue.jobs.valid(conn, job)
        job['payload']['source'][field] = value
        assert not queue.jobs.valid(conn, job)


def test_two_instances_claim_each_job_once_without_running_takeover(queue):
    queue.stage()
    queue.stage(user='bob')
    other_store = SQLiteConversationStore(queue.path, clock=lambda: queue.now)
    other = AutomaticMemoryJobs(other_store, clock=lambda: queue.now)
    barrier = threading.Barrier(2)

    def claim(jobs):
        barrier.wait(timeout=5)
        return jobs.claim_next()

    try:
        with ThreadPoolExecutor(max_workers=2) as worker:
            first = worker.submit(claim, queue.jobs)
            second = worker.submit(claim, other)
            claimed = [first.result(timeout=10), second.result(timeout=10)]
        assert len({job['request_id'] for job in claimed}) == 2
        assert len({job['claim'] for job in claimed}) == 2
        assert queue.jobs.claim_next() is None
        assert other.claim_next() is None
    finally:
        other_store.close()


def test_budget_is_global_and_enqueue_does_not_cancel_previous_jobs(tmp_path):
    queue = Harness(tmp_path / 'bounded.sqlite3', max_pending=2)
    try:
        first = queue.stage()
        queue.jobs.claim_next()
        second = queue.stage(user='bob')
        third = queue.stage(user='carol')
        assert first[-1] and second[-1] and not third[-1]
        assert queue.jobs.metadata('alice', first[0].request_id)['state'] == (
            'running'
        )
        assert queue.jobs.metadata('bob', second[0].request_id)['state'] == (
            'queued'
        )
    finally:
        queue.store.close()


def test_per_user_registration_fifo_does_not_block_other_users(queue):
    # Deliberately reverse lexical request ordering with identical timestamps.
    queue.count = 1
    first = queue.stage()
    queue.count = 9
    second = queue.stage()
    third = queue.stage(user='bob')
    other_store = SQLiteConversationStore(queue.path, clock=lambda: queue.now)
    other = AutomaticMemoryJobs(other_store, clock=lambda: queue.now)
    try:
        a = queue.jobs.claim_next()
        assert a['request_id'] == first[0].request_id
        b = other.claim_next()
        assert b['request_id'] == third[0].request_id
        assert other.claim_next() is None
        assert queue.jobs.metadata('alice', second[0].request_id)['state'] == (
            'queued'
        )
        with queue.transaction() as conn:
            assert queue.jobs.valid(conn, a)
            assert queue.jobs.finish(conn, a, 'saved')
        followup = other.claim_next()
        assert followup['request_id'] == second[0].request_id
        assert followup['queue_sequence'] > a['queue_sequence']
    finally:
        other_store.close()


def test_restart_does_not_overtake_running_job_but_expiry_unblocks_tail(queue):
    first = queue.stage()
    running = queue.jobs.claim_next()
    queue.now += 1
    second = queue.stage()
    other_store = SQLiteConversationStore(queue.path, clock=lambda: queue.now)
    other = AutomaticMemoryJobs(other_store, clock=lambda: queue.now)
    try:
        assert other.claim_next() is None
        queue.now = running['expires_at']
        tail = other.claim_next()
        assert tail['request_id'] == second[0].request_id
        assert other.metadata('alice', first[0].request_id)['state'] == (
            'discarded'
        )
    finally:
        other_store.close()


@pytest.mark.parametrize('terminal', ['saved', 'discarded', 'failed'])
def test_finish_cas_redacts_payload_and_cannot_be_repeated(queue, terminal):
    request, token, snapshot, result, _accepted = queue.stage()
    job = queue.jobs.claim_next()
    with queue.transaction() as conn:
        assert queue.jobs.valid(conn, job)
        assert not queue.jobs.finish(conn, dict(job, claim='wrong'), terminal)
        assert queue.jobs.finish(conn, job, terminal, review_ms=2.5)
        assert not queue.jobs.finish(conn, job, 'saved')
        assert not queue.jobs.valid(conn, job)
        assert not queue.jobs.enqueue(conn, request, token, snapshot, result)
    row = queue.row(request.user_id, request.request_id)
    assert row['state'] == terminal
    assert row['payload_json'] is None and row['claim'] is None
    assert row['finished_at'] == queue.now and row['review_ms'] == 2.5
    assert all(row[f'review_{name}_tokens'] is None
               for name in ('input', 'output', 'total'))


@pytest.mark.parametrize('usage,expected', [
    ({'input_tokens': 23, 'output_tokens': 5, 'total_tokens': 28},
     (23, 5, 28)),
    ({'input_tokens': None, 'output_tokens': 0, 'total_tokens': None},
     (None, 0, None)),
    ({'input_tokens': 4}, (4, None, None)),
    ({}, (None, None, None)),
    (None, (None, None, None)),
])
def test_review_token_usage_is_preserved_without_inventing_unknowns(
    queue, usage, expected,
):
    queue.stage()
    job = queue.jobs.claim_next()
    with queue.transaction() as conn:
        assert queue.jobs.finish(
            conn, job, 'saved', review_ms=3.25, review_usage=usage,
        )
    metadata = queue.jobs.metadata(job['user_id'], job['request_id'])
    assert tuple(metadata[f'review_{name}_tokens']
                 for name in ('input', 'output', 'total')) == expected
    assert metadata['review_ms'] == 3.25
    assert queue.row(job['user_id'], job['request_id'])['payload_json'] is None


@pytest.mark.parametrize('usage', [
    [], 'private-token-error', {'input_tokens': -1}, {'output_tokens': True},
    {'total_tokens': 2.5}, {'input_tokens': float('nan')},
    {'total_tokens': 2 ** 63}, {'private_model_text': 'not retained'},
])
def test_invalid_review_usage_preserves_claim_and_caller_transaction(
    queue, usage,
):
    queue.stage()
    job = queue.jobs.claim_next()
    with pytest.raises(ValueError):
        with queue.transaction() as conn:
            conn.execute("UPDATE automatic_memory_jobs SET reason='stale'")
            queue.jobs.finish(conn, job, 'saved', review_usage=usage)
    row = queue.row(job['user_id'], job['request_id'])
    assert row['state'] == 'running' and row['claim'] == job['claim']
    assert row['reason'] is None and row['review_input_tokens'] is None
    assert row['payload_json'] is not None


def test_token_usage_migration_is_additive_idempotent_and_retains_old_jobs(
    tmp_path,
):
    store = SQLiteConversationStore(str(tmp_path / 'pre-usage.sqlite3'))
    try:
        conn = store._connection
        conn.execute('''CREATE TABLE automatic_memory_jobs (
            user_id TEXT, request_id TEXT, conversation_id TEXT,
            session_instance_id TEXT, generation INTEGER, source_turn_id TEXT,
            ordinal INTEGER, expected_session_revision INTEGER, state TEXT,
            claim TEXT, payload_json TEXT, created_at REAL, expires_at REAL,
            started_at REAL, finished_at REAL, review_ms REAL, reason TEXT,
            PRIMARY KEY (user_id, request_id))''')
        conn.execute('''INSERT INTO automatic_memory_jobs (
            user_id, request_id, state, payload_json, created_at, expires_at)
            VALUES ('alice', 'legacy', 'queued', '{}', 1000, 1120)''')
        conn.commit()
        for _iteration in range(2):
            jobs = AutomaticMemoryJobs(store, clock=lambda: 1000)
            metadata = jobs.metadata('alice', 'legacy')
            assert metadata['state'] == 'queued'
            assert all(metadata[f'review_{name}_tokens'] is None
                       for name in ('input', 'output', 'total'))
        row = conn.execute('SELECT * FROM automatic_memory_jobs').fetchone()
        assert row['payload_json'] == '{}'
        assert conn.execute(
            'SELECT COUNT(*) FROM automatic_memory_jobs',
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_expired_queued_and_running_jobs_are_redacted_not_reclaimed(queue):
    first = queue.stage()
    job = queue.jobs.claim_next()
    second = queue.stage(user='bob')
    queue.now += 120
    with queue.transaction() as conn:
        assert not queue.jobs.valid(conn, job)
    assert queue.jobs.claim_next() is None
    for request in (first[0], second[0]):
        row = queue.row(request.user_id, request.request_id)
        assert row['state'] == 'discarded' and row['reason'] == 'expired'
        assert row['payload_json'] is None and row['claim'] is None
    with queue.transaction() as conn:
        assert not queue.jobs.finish(conn, job, 'saved')


def test_invalidate_user_fences_running_and_queued_with_exception(queue):
    first = queue.stage()
    running = queue.jobs.claim_next()
    second = queue.stage(conversation='other-room')
    third = queue.stage(user='bob')
    with queue.transaction() as conn:
        assert queue.jobs.invalidate_user(
            conn, 'alice', except_request_id=second[0].request_id,
        ) == 1
        assert not queue.jobs.valid(conn, running)
        assert not queue.jobs.finish(conn, running, 'saved')
        assert queue.jobs.invalidate_user(
            conn, 'alice', reason='private-error-source-must-not-be-retained',
        ) == 1
    assert queue.row('alice', first[0].request_id)['payload_json'] is None
    assert queue.row('alice', second[0].request_id)['payload_json'] is None
    assert queue.jobs.metadata('alice', second[0].request_id)['reason'] == (
        'invalidated'
    )
    assert queue.jobs.metadata('bob', third[0].request_id)['state'] == 'queued'


def test_enqueue_and_terminal_changes_follow_caller_rollback(queue):
    captured = []

    def fail(conn, request, token, snapshot, result):
        captured.append(request)
        assert queue.jobs.enqueue(conn, request, token, snapshot, result)
        raise RuntimeError('test rollback')

    with pytest.raises(RuntimeError, match='test rollback'):
        queue.stage(callback=fail)
    assert queue.jobs.metadata('alice', captured[0].request_id) == {}
    queue.stage(user='bob')
    job = queue.jobs.claim_next()
    with pytest.raises(RuntimeError):
        with queue.transaction() as conn:
            assert queue.jobs.finish(conn, job, 'saved')
            raise RuntimeError('rollback finish')
    assert queue.jobs.metadata(job['user_id'], job['request_id'])['state'] == (
        'running'
    )
    row = queue.row(job['user_id'], job['request_id'])
    assert row['payload_json'] is not None


@pytest.mark.parametrize('change', [
    'generation', 'closed', 'expired',
])
def test_valid_requires_current_live_source_session(queue, change):
    queue.stage()
    job = queue.jobs.claim_next()
    with queue.transaction() as conn:
        assert queue.jobs.valid(conn, job)
        if change == 'generation':
            conn.execute(
                f'UPDATE conversation_sessions SET {change}={change}+1',
            )
        elif change == 'closed':
            conn.execute("UPDATE conversation_sessions SET status='closed'")
        else:
            conn.execute(
                'UPDATE conversation_sessions SET expires_at=?', (queue.now,),
            )
        assert not queue.jobs.valid(conn, job)


def test_valid_preserves_job_during_ordinary_turn_in_other_session(queue):
    queue.stage()
    job = queue.jobs.claim_next()
    queue.store.create('alice', 'other-room')
    queue.store.begin_turn('alice', 'other-room', 'new-turn', 'new-request',
                           'fingerprint', '다음 이야기')
    with queue.transaction() as conn:
        assert queue.jobs.valid(conn, job)


def test_valid_rejects_mutated_identity_but_allows_later_ordinary_turn(queue):
    queue.stage()
    job = queue.jobs.claim_next()
    with queue.transaction() as conn:
        assert not queue.jobs.valid(conn, dict(job, ordinal=99))
        conn.execute('UPDATE conversation_sessions SET revision=revision+1')
        conn.execute('''INSERT INTO conversation_turns (
            user_id, conversation_id, session_instance_id, turn_id, request_id,
            request_fingerprint, generation, ordinal, status, user_content,
            assistant_content, response_json, created_at, completed_at)
            SELECT user_id, conversation_id, session_instance_id, 'later-turn',
                'later-request', 'later-fingerprint', generation, ordinal+1,
                'completed', 'later', 'reply', '{}', created_at, completed_at
            FROM conversation_turns
            WHERE request_id=?''', (job['request_id'],))
        assert queue.jobs.valid(conn, job)


def test_session_delete_cascades_private_jobs(queue):
    request, *_rest = queue.stage()
    queue.jobs.claim_next()
    queue.store.delete(request.user_id, request.conversation_id)
    assert queue.jobs.metadata(request.user_id, request.request_id) == {}


def _pending_robot_question(queue, user):
    """Populate the real pending-row fields used by the queue fence."""
    queue.store.create(user, 'robot-room')
    with queue.transaction() as conn:
        conn.execute('''INSERT INTO confirmation_intents (
            schema_version, confirmation_request_id, user_id, conversation_id,
            session_instance_id, generation, revision, ordinal, turn_id,
            agent_request_id, decision_id, tool_name, arguments_digest,
            target_binding_digest, proposal_fingerprint, issued_at, expires_at,
            state, disposition, result_code, record_json,
            created_at, updated_at)
            SELECT 1, ?, user_id, conversation_id, session_instance_id,
                generation, 1, 1, 'robot-turn',
                'robot-request', 'robot-decision',
                'navigate', 'arguments', 'target', 'proposal', ?, ?,
                'pending', 'pending', 'confirmation_pending', '{}', ?, ?
            FROM conversation_sessions
            WHERE user_id=? AND conversation_id='robot-room' ''', (
                f'robot-confirmation-{user}', queue.now, queue.now + 1000,
                queue.now, queue.now, user,
            ))


def test_robot_confirmation_blocks_enqueue_and_invalidates_claim(queue):
    queue.stage()
    job = queue.jobs.claim_next()
    _pending_robot_question(queue, 'alice')
    with queue.transaction() as conn:
        assert not queue.jobs.valid(conn, job)
    staged = queue.stage(conversation='other-room')
    assert not staged[-1]
    assert queue.jobs.metadata('alice', staged[0].request_id) == {}


def test_other_users_robot_confirmation_does_not_block_queue(queue):
    _pending_robot_question(queue, 'bob')
    assert queue.stage()[-1]
    job = queue.jobs.claim_next()
    with queue.transaction() as conn:
        assert queue.jobs.valid(conn, job)


def test_malformed_proposals_and_invalid_finish_inputs_do_not_mutate(queue):
    def corrupt(_conn, _request, _token, _snapshot, result):
        result.provider_result.memory_proposal['unexpected'] = 'private'

    with pytest.raises(ValidationError):
        queue.stage(callback=corrupt)
    queue.stage(user='bob')
    job = queue.jobs.claim_next()
    with queue.transaction() as conn:
        for state in ('queued', 'running', 'unknown', [], None):
            with pytest.raises(ValueError):
                queue.jobs.finish(conn, job, state)
        for review_ms in (-1, True, float('nan'), float('inf')):
            with pytest.raises(ValueError):
                queue.jobs.finish(conn, job, 'saved', review_ms)
        assert queue.jobs.valid(conn, job)


def test_caller_methods_require_transaction(queue):
    request, token, snapshot, result, _accepted = queue.stage()
    job = queue.jobs.claim_next()
    conn = queue.store._connection
    for invoke in (
        lambda: queue.jobs.enqueue(conn, request, token, snapshot, result),
        lambda: queue.jobs.valid(conn, job),
        lambda: queue.jobs.finish(conn, job, 'saved'),
        lambda: queue.jobs.invalidate_user(conn, 'alice'),
    ):
        with pytest.raises(RuntimeError, match='require a transaction'):
            invoke()
