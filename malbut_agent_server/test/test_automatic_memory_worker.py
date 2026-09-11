"""Real queue/SQLite/HTTP boundaries with a controllably delayed reviewer."""

import json
import sqlite3
import threading
import time
from urllib.request import Request, urlopen

import pytest

from malbut_agent_server.automatic_memory_worker import AutomaticMemoryWorker
from malbut_agent_server.http_server import make_server
from malbut_agent_server.memory_source_review import MemorySourceReviewer
from test_memory_source_review import ReviewProvider, name_fact
from test_personal_memory_flow import Flow, proposed


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('bounded asynchronous operation did not finish')


@pytest.fixture
def lab(tmp_path):
    flow = Flow(tmp_path / 'background.sqlite3')
    flow.enable()
    reviewer = ReviewProvider()
    entered, release = threading.Event(), threading.Event()

    def hold_review():
        entered.set()
        assert release.wait(5), 'test did not release its reviewer'

    reviewer.callback = hold_review
    runtime = flow.runtime
    runtime.memory_source_reviewer = MemorySourceReviewer(reviewer)
    runtime.automatic_memory_worker = AutomaticMemoryWorker(
        runtime, runtime.automatic_memory_jobs,
    )
    yield flow, reviewer, entered, release
    release.set()
    runtime.close()


def intro(flow, explicit=False):
    text = '난 김민재야' + ('. 기억해줘' if explicit else '')
    return flow.say(text, proposed('remember', text, facts=[name_fact(text)]),
                    message='민재님, 반가워요!')


def status(flow, request=None):
    request = request or flow.requests[-1]
    return flow.runtime.automatic_memory_jobs.metadata(
        request.user_id, request.request_id,
    )


def test_reply_and_replay_finish_while_review_is_still_blocked(lab):
    flow, reviewer, entered, release = lab
    result = intro(flow)
    assert entered.wait(2)
    request = flow.requests[-1]
    before = result.to_dict()
    conn = flow.conversations._connection
    with flow.conversations._lock:
        frozen = conn.execute(
            'SELECT status,response_json FROM conversation_turns '
            'WHERE user_id=? AND request_id=?',
            (request.user_id, request.request_id),
        ).fetchone()
    assert frozen['status'] == 'completed'
    assert status(flow)['state'] == 'running'
    assert flow.memory.list_for_user('alice') == []
    assert flow.runtime.handle(request).to_dict() == before
    assert len(flow.provider.calls) == 1
    assert len(reviewer.calls) == 1
    release.set()
    until(lambda: status(flow)['state'] == 'saved')
    assert len(flow.memory.list_for_user('alice')) == 1
    assert flow.runtime.handle(request).to_dict() == before
    with flow.conversations._lock:
        current = conn.execute(
            'SELECT response_json FROM conversation_turns '
            'WHERE user_id=? AND request_id=?',
            (request.user_id, request.request_id),
        ).fetchone()[0]
    assert current == frozen['response_json']


@pytest.mark.parametrize('next_text', [
    '내 이름 기억 삭제해줘', '내 이름을 보리로 정정해줘', '개인화 꺼줘',
])
def test_management_fences_late_save_even_when_no_memory_exists(
    lab, next_text,
):
    flow, _reviewer, entered, release = lab
    intro(flow)
    request = flow.requests[-1]
    assert entered.wait(2)
    answer = flow.say(next_text)
    assert answer.decision.message
    assert status(flow, request)['state'] == 'discarded'
    release.set()
    flow.runtime.stop_background_memory()
    assert flow.memory.list_for_user('alice') == []


def test_ordinary_next_turn_preserves_unfinished_legacy_review(lab):
    flow, reviewer, entered, release = lab
    first = intro(flow)
    first_request = flow.requests[-1]
    assert entered.wait(2)
    first_frozen = first.to_dict()
    second = flow.say('오늘 기분이 좋아', message='좋은 하루를 보내셨군요.')
    second_request = flow.requests[-1]
    second_frozen = second.to_dict()
    assert status(flow, first_request)['state'] == 'running'
    assert len(reviewer.calls) == 1
    release.set()
    until(lambda: status(flow, first_request)['state'] == 'saved')
    assert len(flow.memory.list_for_user('alice')) == 1
    assert first.to_dict() == first_frozen
    assert second.to_dict() == second_frozen
    assert flow.runtime.handle(first_request).to_dict() == first_frozen
    assert flow.runtime.handle(second_request).to_dict() == second_frozen


def test_explicit_save_waits_for_verification_and_actual_storage(lab):
    flow, _reviewer, entered, release = lab
    results = []
    thread = threading.Thread(target=lambda: results.append(intro(flow, True)))
    thread.start()
    try:
        assert entered.wait(2)
        assert thread.is_alive() and results == []
        assert flow.memory.list_for_user('alice') == []
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert '기억했어요' in results[0].decision.message
        assert len(flow.memory.list_for_user('alice')) == 1
        assert status(flow) == {}
    finally:
        release.set()
        thread.join(3)


@pytest.mark.parametrize('change', ['reset', 'delete', 'other_user'])
def test_session_invalidation_and_user_isolation(lab, change):
    flow, _reviewer, entered, release = lab
    intro(flow)
    request = flow.requests[-1]
    assert entered.wait(2)
    if change == 'reset':
        flow.conversations.reset('alice', 'room')
    elif change == 'delete':
        flow.conversations.delete('alice', 'room')
    else:
        flow.say('안녕', user='bob', conversation='other')
    release.set()
    if change == 'delete':
        assert status(flow, request) == {}
        flow.runtime.stop_background_memory()
    else:
        expected = 'saved' if change == 'other_user' else 'discarded'
        until(lambda: status(flow, request)['state'] == expected)
    assert len(flow.memory.list_for_user('alice')) == (
        1 if change == 'other_user' else 0
    )
    assert flow.memory.list_for_user('bob') == []


def test_close_waits_for_worker_before_closing_sqlite(lab):
    flow, _reviewer, entered, release = lab
    intro(flow)
    assert entered.wait(2)
    thread = threading.Thread(target=flow.runtime.close)
    thread.start()
    until(lambda: flow.runtime.automatic_memory_worker.closed)
    assert thread.is_alive()
    assert flow.memory.list_for_user('alice') == []
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert not flow.runtime.automatic_memory_worker._thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        flow.memory.list_for_user('alice')
    flow.runtime.close()


def test_db_failure_cannot_rewrite_delivered_reply(lab, monkeypatch):
    flow, _reviewer, entered, release = lab
    result = intro(flow)
    assert entered.wait(2)
    before = result.to_dict()

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic')

    monkeypatch.setattr(flow.memory, 'upsert_fact', fail)
    release.set()
    until(lambda: status(flow)['state'] == 'failed')
    assert flow.memory.list_for_user('alice') == []
    assert result.to_dict() == before
    assert flow.runtime.handle(flow.requests[-1]).to_dict() == before


def test_actual_http_response_arrives_before_semantic_review(lab):
    flow, reviewer, entered, release = lab
    runtime = flow.runtime
    text = '난 김민재야'
    flow.provider.proposal = proposed(
        'remember', text, facts=[name_fact(text)],
    )
    flow.provider.message = '민재님, 반가워요!'
    server = make_server(
        '127.0.0.1', 0, runtime,
        auth_token='fixture-only-token', allowed_user_id='alice',
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        body = {'user_id': 'alice', 'conversation_id': 'room',
                'request_id': 'http-intro', 'turn_id': 'http-intro',
                'utterance': text, 'robot_state': {}, 'available_tools': []}
        request = Request(
            f'http://127.0.0.1:{server.server_port}/v1/agent/respond',
            json.dumps(body).encode(),
            {'Content-Type': 'application/json',
             'Authorization': 'Bearer fixture-only-token'},
        )
        with urlopen(request, timeout=2) as response:
            answer = json.load(response)
        assert entered.wait(2)
        assert answer['decision']['message'] == '민재님, 반가워요!'
        assert not answer['execution']['authorized']
        assert flow.memory.list_for_user('alice') == []
        assert len(reviewer.calls) == 1
    finally:
        release.set()
        server.shutdown()
        thread.join(3)
        server.server_close()


@pytest.mark.parametrize('already_claimed', [False, True])
def test_restart_recovers_queued_but_never_replays_running_review(
    lab, already_claimed,
):
    flow, _reviewer, _entered, _release = lab
    # Freeze scheduling to simulate a crash after the durable reply commit.
    flow.runtime.automatic_memory_worker.start = lambda: None
    result = intro(flow)
    request = flow.requests[-1]
    if already_claimed:
        assert flow.runtime.automatic_memory_jobs.claim_next() is not None
    path = flow.path
    flow.runtime.close()
    restored = Flow(path)
    reviewer = ReviewProvider()
    restored.runtime.memory_source_reviewer = MemorySourceReviewer(reviewer)
    restored.runtime.automatic_memory_worker = AutomaticMemoryWorker(
        restored.runtime, restored.runtime.automatic_memory_jobs,
    )
    try:
        if already_claimed:
            jobs = restored.runtime.automatic_memory_jobs
            old_clock = jobs._clock
            jobs._clock = lambda: old_clock() + 121
        restored.runtime.start_background_memory()
        expected = 'discarded' if already_claimed else 'saved'
        until(lambda: status(restored, request)['state'] == expected)
        assert len(reviewer.calls) == (0 if already_claimed else 1)
        assert len(restored.memory.list_for_user('alice')) == (
            0 if already_claimed else 1
        )
        assert restored.runtime.handle(request).decision == result.decision
    finally:
        restored.runtime.close()
