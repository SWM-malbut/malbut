"""Separate extraction begins only after an ordinary response is frozen."""

import copy
import json
import threading
from urllib.request import Request, urlopen

import pytest

from malbut_agent_server.automatic_memory_worker import AutomaticMemoryWorker
from malbut_agent_server.http_server import make_server
from malbut_agent_server.memory_source_review import MemorySourceReviewer
from malbut_agent_server.orchestrator import MemoryChangedError
from malbut_agent_server.schemas import (
    AgentDecision, ProviderResult, ProviderUsage, RobotState,
)
from test_automatic_memory_worker import status, until
from test_memory_source_review import ReviewProvider, name_fact
from test_personal_memory_flow import Flow, origin, pet_fact, proposed


class Extractor:
    """A controlled extraction-only interface with no conversation inputs."""

    def __init__(self):
        self.calls = []
        self.callback = None
        self.change = None

    def extract(self, request):
        self.calls.append(copy.deepcopy(request))
        assert request.available_tools == ()
        assert request.robot_state == RobotState()
        if self.callback:
            self.callback()
        result = ProviderResult(
            decision=AgentDecision(
                type='message', message='private extraction',
            ),
            provider='fixture', model='extract-only', latency_ms=999.0,
            memory_supported=True,
            memory_proposal=proposed(
                'remember', request.utterance,
                facts=[name_fact(request.utterance)],
            ),
            usage=ProviderUsage(
                input_tokens=13, output_tokens=7, total_tokens=20,
            ),
        )
        if self.change:
            self.change(result)
        return result


@pytest.fixture
def lab(tmp_path):
    flow = Flow(tmp_path / 'extraction-worker.sqlite3')
    flow.enable()
    extractor, reviewer = Extractor(), ReviewProvider()
    entered, release = threading.Event(), threading.Event()

    def hold_extraction():
        assert not flow.runtime._handle_lock._is_owned()
        assert not flow.conversations._lock._is_owned()
        assert not flow.conversations._connection.in_transaction
        entered.set()
        assert release.wait(5), 'test did not release its extractor'

    extractor.callback = hold_extraction
    runtime = flow.runtime
    runtime.automatic_memory_extractor = extractor
    runtime.memory_source_reviewer = MemorySourceReviewer(reviewer)
    runtime.automatic_memory_worker = AutomaticMemoryWorker(
        runtime, runtime.automatic_memory_jobs,
    )
    yield flow, extractor, reviewer, entered, release
    release.set()
    runtime.close()


def intro(flow):
    """The foreground provider supplies no reusable extraction candidate."""
    return flow.say('난 김민재야', message='민재님, 반가워요!')


def frozen_response(flow, request):
    with flow.conversations._lock:
        return flow.conversations._connection.execute(
            '''SELECT response_json FROM conversation_turns
            WHERE user_id=? AND request_id=?''',
            (request.user_id, request.request_id),
        ).fetchone()[0]


def test_reply_is_frozen_before_extraction_and_never_rewritten(lab):
    flow, extractor, reviewer, entered, release = lab
    response = intro(flow)
    request = flow.requests[-1]
    assert entered.wait(2)
    before = response.to_dict()
    persisted = frozen_response(flow, request)
    assert before['decision']['message'] == '민재님, 반가워요!'
    assert status(flow, request)['state'] == 'running'
    assert flow.memory.list_for_user('alice') == []
    assert sum(item.request_id == request.request_id
               for item in extractor.calls) == 1
    assert reviewer.calls == []
    assert flow.runtime.handle(request).to_dict() == before
    assert len(flow.provider.calls) == 1
    assert flow.provider.calls[-1]['context']['mode'] == 'answer_only'
    with flow.conversations._lock:
        payload = json.loads(flow.conversations._connection.execute(
            '''SELECT payload_json FROM automatic_memory_jobs
            WHERE user_id=? AND request_id=?''',
            (request.user_id, request.request_id),
        ).fetchone()[0])
    assert payload['version'] == 2
    assert payload['mode'] == 'extract'
    assert payload['proposal'] is None
    assert payload['source']['text'] == request.utterance
    release.set()
    until(lambda: status(flow, request)['state'] == 'saved')
    assert len(extractor.calls) == len(reviewer.calls) == 1
    assert len(flow.memory.list_for_user('alice')) == 1
    assert frozen_response(flow, request) == persisted
    assert flow.runtime.handle(request).to_dict() == before
    metadata = status(flow, request)
    assert metadata['extraction_ms'] >= 0
    assert metadata['extraction_total_tokens'] == 20
    assert metadata['review_total_tokens'] == 5
    assert metadata['review_ms'] >= 0


@pytest.mark.parametrize('next_text', [
    '내 이름 기억 삭제해줘', '내 이름을 보리로 정정해줘', '개인화 꺼줘',
])
def test_management_fences_extraction_before_semantic_review(lab, next_text):
    flow, extractor, reviewer, entered, release = lab
    intro(flow)
    request = flow.requests[-1]
    assert entered.wait(2)
    flow.say(next_text)
    assert status(flow, request)['state'] == 'discarded'
    release.set()
    flow.runtime.stop_background_memory()
    assert sum(item.request_id == request.request_id
               for item in extractor.calls) == 1
    assert reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []


def test_ordinary_next_turn_preserves_unfinished_extraction(lab):
    flow, extractor, reviewer, entered, release = lab
    first = intro(flow)
    first_request = flow.requests[-1]
    assert entered.wait(2)
    second = flow.say('오늘 기분이 좋아', message='기분 좋은 하루였군요.')
    second_request = flow.requests[-1]
    first_frozen, second_frozen = first.to_dict(), second.to_dict()
    first_json = frozen_response(flow, first_request)
    second_json = frozen_response(flow, second_request)
    assert status(flow, first_request)['state'] == 'running'
    assert status(flow, second_request)['state'] == 'queued'
    assert len(extractor.calls) == 1
    release.set()
    until(lambda: status(flow, first_request)['state'] == 'saved')
    until(lambda: status(flow, second_request)['state'] == 'discarded')
    assert len(extractor.calls) == 2
    assert len(reviewer.calls) == 1
    assert len(flow.memory.list_for_user('alice')) == 1
    assert first.to_dict() == first_frozen
    assert second.to_dict() == second_frozen
    assert flow.runtime.handle(first_request).to_dict() == first_frozen
    assert flow.runtime.handle(second_request).to_dict() == second_frozen
    assert frozen_response(flow, first_request) == first_json
    assert frozen_response(flow, second_request) == second_json


def test_two_queued_facts_save_in_source_order_with_frozen_replay(lab):
    flow, extractor, reviewer, entered, release = lab

    def extract_pet_when_present(result):
        text = result.memory_proposal['evidence']
        if '강아지' in text:
            result.memory_proposal = proposed(
                'remember', text, facts=[pet_fact(text)],
            )

    extractor.change = extract_pet_when_present
    first = intro(flow)
    first_request = flow.requests[-1]
    assert entered.wait(2)
    second = flow.say('우리 강아지 이름은 두부야', message='두부도 반가워요!')
    second_request = flow.requests[-1]
    requests = [first_request, second_request]
    frozen = [first.to_dict(), second.to_dict()]
    persisted = [frozen_response(flow, request) for request in requests]
    assert status(flow, first_request)['state'] == 'running'
    assert status(flow, second_request)['state'] == 'queued'
    assert [request.request_id for request in extractor.calls] == [
        first_request.request_id,
    ]
    release.set()
    until(lambda: all(status(flow, request)['state'] == 'saved'
                      for request in requests))
    assert [request.request_id for request in extractor.calls] == [
        request.request_id for request in requests
    ]
    assert len(reviewer.calls) == 1
    records = flow.memory.list_for_user('alice')
    assert len(records) == 2
    by_value = {record.metadata['fact']['value']: record for record in records}
    for value, request in zip(('김민재', '두부'), requests):
        source = by_value[value].metadata['source']
        assert source['request_id'] == request.request_id
        assert source['turn_id'] == request.turn_id
        assert source['text'] == request.utterance
    for request, answer, raw in zip(requests, frozen, persisted):
        assert flow.runtime.handle(request).to_dict() == answer
        assert frozen_response(flow, request) == raw


def test_other_runtime_foreground_survives_add_during_model_inference(lab):
    flow, _extractor, _reviewer, entered, release = lab
    first = intro(flow)
    first_request = flow.requests[-1]
    assert entered.wait(2)
    first_frozen = first.to_dict()
    other = Flow(flow.path)
    other.counter = 100
    foreground_entered = threading.Event()
    foreground_release = threading.Event()
    responses, errors = [], []

    def hold_foreground():
        foreground_entered.set()
        assert foreground_release.wait(5), 'foreground was not released'

    def foreground():
        try:
            responses.append(other.say(
                '오늘 기분이 좋아', callback=hold_foreground,
                message='좋은 하루를 보내셨군요.',
            ))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=foreground)
    thread.start()
    try:
        assert foreground_entered.wait(2)
        assert thread.is_alive() and responses == [] and errors == []
        release.set()
        until(lambda: status(flow, first_request)['state'] == 'saved')
        assert thread.is_alive() and responses == [] and errors == []
        assert len(flow.memory.list_for_user('alice')) == 1
        foreground_release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert errors == [] and len(responses) == 1
        second = responses[0]
        assert second.decision.message == '좋은 하루를 보내셨군요.'
        assert other.runtime.handle(other.requests[-1]).to_dict() == (
            second.to_dict()
        )
        assert first.to_dict() == first_frozen
        assert flow.runtime.handle(first_request).to_dict() == first_frozen
    finally:
        release.set()
        foreground_release.set()
        thread.join(3)
        other.runtime.close()


@pytest.mark.parametrize('control', ['no_match_delete', 'disable_reenable'])
def test_management_barrier_cannot_be_undone_by_later_ordinary_chat(
    lab, control,
):
    flow, extractor, reviewer, entered, release = lab
    intro(flow)
    request = flow.requests[-1]
    assert entered.wait(2)
    assert flow.memory.list_for_user('alice') == []
    if control == 'no_match_delete':
        text = '내 이름 기억을 삭제해줘'
        answer = flow.say(text, proposed('forget', text, query='이름'))
        assert answer.decision.type == 'clarification'
        assert '찾지 못했어요' in answer.decision.message
    else:
        flow.say('개인화 꺼줘')
        assert not flow.memory.policy_state('alice')['enabled']
        flow.enable()
        assert flow.memory.policy_state('alice')['enabled']
    assert status(flow, request)['state'] == 'discarded'
    flow.say('오늘 기분이 좋아')
    release.set()
    flow.runtime.stop_background_memory()
    assert sum(item.request_id == request.request_id
               for item in extractor.calls) == 1
    assert reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []


def test_no_match_delete_fences_older_provider_before_job_exists(lab):
    flow, _extractor, _reviewer, _entered, _release = lab
    older = Flow(flow.path)
    older.counter = 100
    extractor, reviewer = Extractor(), ReviewProvider()
    older.runtime.automatic_memory_extractor = extractor
    older.runtime.memory_source_reviewer = MemorySourceReviewer(reviewer)
    older.runtime.automatic_memory_worker = AutomaticMemoryWorker(
        older.runtime, older.runtime.automatic_memory_jobs,
    )
    entered, release = threading.Event(), threading.Event()
    responses, errors = [], []

    def hold_primary():
        entered.set()
        assert release.wait(5), 'older primary provider was not released'

    def request_older_turn():
        try:
            responses.append(older.say(
                '난 김민재야', callback=hold_primary,
                conversation='older-room', message='민재님, 반가워요!',
            ))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=request_older_turn)
    thread.start()
    try:
        assert entered.wait(2)
        request = older.requests[-1]
        assert status(older, request) == {}
        epoch = flow.memory.policy_state('alice')['revision']
        text = '내 이름 기억을 삭제해줘'
        answer = flow.say(text, proposed('forget', text, query='이름'))
        assert '찾지 못했어요' in answer.decision.message
        assert flow.memory.policy_state('alice')['revision'] > epoch
        assert flow.memory.list_for_user('alice') == []
        release.set()
        thread.join(3)
        assert not thread.is_alive() and responses == []
        assert len(errors) == 1 and isinstance(errors[0], MemoryChangedError)
        assert status(older, request) == {}
        assert len(older.provider.calls) == 1
        assert extractor.calls == reviewer.calls == []
        assert flow.memory.list_for_user('alice') == []
    finally:
        release.set()
        thread.join(3)
        older.runtime.close()


def test_management_replay_does_not_reapply_barrier_to_new_job(lab):
    flow, extractor, reviewer, entered, release = lab
    text = '내 이름 기억을 삭제해줘'
    management = flow.say(text, proposed('forget', text, query='이름'))
    management_request = flow.requests[-1]
    management_frozen = management.to_dict()
    assert '찾지 못했어요' in management.decision.message
    intro(flow)
    request = flow.requests[-1]
    assert entered.wait(2)
    epoch = flow.memory.policy_state('alice')['revision']
    assert status(flow, request)['state'] == 'running'
    assert flow.runtime.handle(management_request).to_dict() == (
        management_frozen
    )
    assert flow.memory.policy_state('alice')['revision'] == epoch
    assert status(flow, request)['state'] == 'running'
    assert len(extractor.calls) == 1 and reviewer.calls == []
    release.set()
    until(lambda: status(flow, request)['state'] == 'saved')
    assert flow.memory.policy_state('alice')['revision'] == epoch
    assert len(extractor.calls) == len(reviewer.calls) == 1
    assert flow.runtime.handle(management_request).to_dict() == (
        management_frozen
    )


@pytest.mark.parametrize('restriction,cancels', [
    ('기억하지마', True), ('삭제하지마', False),
    ('내 이름 기억하고 있어?', False), ('내 이름 기억하니?', False),
    ('내가 말한 이름을 기억해?', False),
    ('내 이름 기억하니? 내 별명은 별이야. 기억해줘.', True),
    ('내 이름 기억하고 있어? 새 이름도 저장해줘.', True),
    ('내 이름 기억해? 이 이름도 기억해줘.', True),
])
def test_storage_prohibition_fences_but_deletion_prohibition_preserves(
    lab, restriction, cancels,
):
    flow, _extractor, reviewer, entered, release = lab
    text = '우리 강아지 이름은 두부야. 기억해줘'
    flow.say(text, proposed('remember', text, facts=[pet_fact(text)]))
    existing = flow.memory.list_for_user('alice')[0]
    intro(flow)
    request = flow.requests[-1]
    assert entered.wait(2)
    epoch = flow.memory.policy_state('alice')['revision']
    flow.say(restriction)
    current_epoch = flow.memory.policy_state('alice')['revision']
    if cancels:
        assert current_epoch > epoch
        assert status(flow, request)['state'] == 'discarded'
    else:
        assert current_epoch == epoch
        assert status(flow, request)['state'] == 'running'
    assert flow.memory.list_for_user('alice') == [existing]
    release.set()
    expected = 'discarded' if cancels else 'saved'
    until(lambda: status(flow, request)['state'] == expected)
    flow.runtime.stop_background_memory()
    records = flow.memory.list_for_user('alice')
    assert existing.id in {record.id for record in records}
    assert len(records) == (1 if cancels else 2)
    assert len(reviewer.calls) == (0 if cancels else 1)
    assert flow.memory.policy_state('alice')['revision'] == current_epoch


@pytest.mark.parametrize('change', ['disable', 'reset', 'delete'])
def test_external_invalidation_between_extraction_and_review(lab, change):
    flow, _extractor, reviewer, entered, release = lab
    intro(flow)
    request = flow.requests[-1]
    assert entered.wait(2)
    if change == 'disable':
        flow.memory.set_personalization('alice', False, origin('개인화 꺼줘'))
    elif change == 'reset':
        flow.conversations.reset('alice', 'room')
    else:
        flow.conversations.delete('alice', 'room')
    release.set()
    until(lambda: status(flow, request).get('state') == 'discarded'
          if change != 'delete' else not status(flow, request))
    flow.runtime.stop_background_memory()
    assert reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []


def test_explicit_save_uses_synchronous_primary_candidate(lab):
    flow, extractor, _reviewer, _entered, _release = lab
    text = '내 이름은 김민재야. 기억해줘'
    result = flow.say(text, proposed(
        'remember', text, facts=[name_fact(text)],
    ))
    assert '기억했어요' in result.decision.message
    assert len(flow.memory.list_for_user('alice')) == 1
    assert extractor.calls == []
    assert status(flow) == {}
    assert 'mode' not in flow.provider.calls[-1]['context']


def test_actual_http_answer_arrives_while_extractor_is_blocked(lab):
    flow, extractor, reviewer, entered, release = lab
    flow.provider.message = '민재님, 반가워요!'
    server = make_server(
        '127.0.0.1', 0, flow.runtime,
        auth_token='fixture-only-token', allowed_user_id='alice',
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        body = {
            'user_id': 'alice', 'conversation_id': 'room',
            'request_id': 'http-extract', 'turn_id': 'http-extract',
            'utterance': '난 김민재야', 'robot_state': {},
            'available_tools': [],
        }
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
        assert len(extractor.calls) == 1
        assert reviewer.calls == []
        assert flow.provider.calls[-1]['context']['mode'] == 'answer_only'
        queued = flow.runtime.automatic_memory_jobs.metadata(
            'alice', 'http-extract',
        )
        assert queued['state'] == 'running'
    finally:
        release.set()
        server.shutdown()
        thread.join(3)
        server.server_close()


@pytest.mark.parametrize('change', ['new_turn', 'reset'])
def test_semantic_review_survives_chat_but_not_source_reset(
    lab, change,
):
    flow, extractor, reviewer, _entered, _release = lab
    extractor.callback = None
    entered, release = threading.Event(), threading.Event()

    def hold_review():
        assert not flow.runtime._handle_lock._is_owned()
        assert not flow.conversations._lock._is_owned()
        entered.set()
        assert release.wait(5), 'test did not release its semantic reviewer'

    reviewer.callback = hold_review
    response = intro(flow)
    request = flow.requests[-1]
    frozen = response.to_dict()
    try:
        assert entered.wait(2)
        assert len(extractor.calls) == len(reviewer.calls) == 1
        assert response.decision.message == '민재님, 반가워요!'
        if change == 'new_turn':
            flow.say('오늘은 다른 이야기를 나누자')
        else:
            flow.conversations.reset('alice', 'room')
        release.set()
        expected = 'saved' if change == 'new_turn' else 'discarded'
        until(lambda: status(flow, request)['state'] == expected)
        flow.runtime.stop_background_memory()
        assert len(flow.memory.list_for_user('alice')) == (
            1 if change == 'new_turn' else 0
        )
        if change == 'new_turn':
            assert flow.runtime.handle(request).to_dict() == frozen
    finally:
        release.set()


def test_null_extraction_discards_without_review_or_replay_call(lab):
    flow, extractor, reviewer, entered, release = lab
    extractor.change = lambda result: setattr(result, 'memory_proposal', None)
    response = intro(flow)
    request = flow.requests[-1]
    assert entered.wait(2)
    persisted = frozen_response(flow, request)
    release.set()
    until(lambda: status(flow, request)['state'] == 'discarded')
    assert reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []
    assert flow.runtime.handle(request).decision == response.decision
    assert frozen_response(flow, request) == persisted
    assert len(extractor.calls) == 1
    assert status(flow, request)['extraction_total_tokens'] == 20


@pytest.mark.parametrize('kind', ['tool_call', 'refusal'])
def test_nonmessage_primary_output_never_creates_extraction_job(
    lab, monkeypatch, kind,
):
    flow, extractor, reviewer, _entered, _release = lab
    complete = flow.provider.complete

    def nonmessage(*args, **kwargs):
        result = complete(*args, **kwargs)
        result.decision = AgentDecision(
            type=kind, message='처리할 수 없어요.',
            tool_name='navigate' if kind == 'tool_call' else None,
            arguments={'location': '거실'} if kind == 'tool_call' else {},
        )
        return result

    monkeypatch.setattr(flow.provider, 'complete', nonmessage)
    result = intro(flow)
    assert result.raw_decision.type == kind
    assert result.decision.type == 'refusal'
    assert flow.provider.calls[-1]['context']['mode'] == 'answer_only'
    assert status(flow) == {}
    assert extractor.calls == reviewer.calls == []


@pytest.mark.parametrize('change', ['version', 'source'])
def test_malformed_queued_job_never_calls_extractor(lab, change):
    flow, extractor, reviewer, _entered, _release = lab
    worker = flow.runtime.automatic_memory_worker
    start = worker.start
    worker.start = lambda: None
    intro(flow)
    request = flow.requests[-1]
    with flow.conversations._lock:
        conn = flow.conversations._connection
        payload = json.loads(conn.execute(
            '''SELECT payload_json FROM automatic_memory_jobs
            WHERE user_id=? AND request_id=?''',
            (request.user_id, request.request_id),
        ).fetchone()[0])
        if change == 'version':
            payload['version'] = 999
        else:
            payload['source']['user_id'] = 'another-user'
        conn.execute(
            '''UPDATE automatic_memory_jobs SET payload_json=?
            WHERE user_id=? AND request_id=?''',
            (json.dumps(payload), request.user_id, request.request_id),
        )
        conn.commit()
    worker.start = start
    flow.runtime.start_background_memory()
    until(lambda: status(flow, request)['state'] in {'failed', 'discarded'})
    assert extractor.calls == reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []


@pytest.mark.parametrize('change', [
    lambda r: setattr(r, 'decision', AgentDecision(
        type='tool_call', message='이동', tool_name='navigate',
        arguments={'location': '거실'},
    )),
    lambda r: setattr(r, 'memory_supported', False),
    lambda r: setattr(r, 'memory_proposal', {'operation': 'remember'}),
    lambda r: r.memory_proposal.update(operation='correct'),
    lambda r: setattr(r, 'decision', AgentDecision(
        type='clarification', message='저장할까요?',
    )),
])
def test_invalid_extraction_never_reaches_reviewer_or_changes_reply(
    lab, change,
):
    flow, extractor, reviewer, entered, release = lab
    extractor.change = change
    response = intro(flow)
    assert entered.wait(2)
    request = flow.requests[-1]
    before = response.to_dict()
    release.set()
    until(lambda: status(flow, request)['state'] in {'failed', 'discarded'})
    assert reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []
    assert flow.runtime.handle(request).to_dict() == before
    assert len(extractor.calls) == 1


def test_extraction_exception_is_terminal_and_replay_does_not_resend(lab):
    flow, extractor, reviewer, _entered, release = lab

    def failure():
        assert release.wait(5)
        raise TimeoutError('private extractor failure')

    extractor.callback = failure
    response = intro(flow)
    request = flow.requests[-1]
    release.set()
    until(lambda: status(flow, request)['state'] == 'failed')
    assert status(flow, request)['extraction_ms'] >= 0
    assert reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []
    assert flow.runtime.handle(request).decision == response.decision
    assert len(extractor.calls) == 1


@pytest.mark.parametrize('already_claimed', [False, True])
def test_restart_extracts_queued_only_and_never_resends_running_job(
    lab, already_claimed,
):
    flow, _extractor, _reviewer, _entered, _release = lab
    flow.runtime.automatic_memory_worker.start = lambda: None
    response = intro(flow)
    request, path = flow.requests[-1], flow.path
    if already_claimed:
        assert flow.runtime.automatic_memory_jobs.claim_next() is not None
    flow.runtime.close()
    restored = Flow(path)
    extractor, reviewer = Extractor(), ReviewProvider()
    runtime = restored.runtime
    runtime.automatic_memory_extractor = extractor
    runtime.memory_source_reviewer = MemorySourceReviewer(reviewer)
    runtime.automatic_memory_worker = AutomaticMemoryWorker(
        runtime, runtime.automatic_memory_jobs,
    )
    try:
        if already_claimed:
            jobs = runtime.automatic_memory_jobs
            old_clock = jobs._clock
            jobs._clock = lambda: old_clock() + 121
        runtime.start_background_memory()
        expected = 'discarded' if already_claimed else 'saved'
        until(lambda: status(restored, request)['state'] == expected)
        expected_calls = 0 if already_claimed else 1
        assert len(extractor.calls) == len(reviewer.calls) == expected_calls
        assert runtime.handle(request).decision == response.decision
        assert len(extractor.calls) == expected_calls
    finally:
        runtime.close()
