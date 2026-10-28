"""Published name questions bind field input to one current conversation turn."""

import json
import sqlite3
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from malbut_agent_server import speech_dialogue
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.orchestrator import OrchestrationResult
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.schemas import (
    AgentDecision, AgentRequest, ProviderResult, RobotState, SpeechAgentRequest, ValidationError,
)
from malbut_agent_server.speech_dialogue import DialogueWorker
from malbut_agent_server.speech_mapping import mapping_name_binding, quoted_mapping_name
from malbut_agent_server.speech_mission_policy import configure_speech_missions
from malbut_agent_server.speech_missions import SpeechMissions
from malbut_agent_server.tools import select_tool_specs
from test_speech_mission_dialogue import Manager, collect


def request(text='"작업실로"', pending=True):
    return SpeechAgentRequest(
        request_id='request', user_id='speaker', conversation_id='conversation', turn_id='turn',
        utterance=text, robot_state=RobotState(),
        available_tools=('request_mapping', 'request_navigation', 'request_patrol'),
        mapping_name_requested=pending,
    )


def test_internal_context_survives_copy_but_cannot_be_supplied_over_http():
    source = request()
    assert SpeechAgentRequest.from_dict(source.to_dict()) == source
    with pytest.raises(ValidationError):
        AgentRequest.from_dict(source.to_dict())
    with pytest.raises(ValidationError):
        replace(source, mapping_name_requested='true')


def test_typed_question_survives_persistence_and_rejects_non_questions():
    question = AgentDecision(type='clarification', message='지도 이름은요?',
                             clarification_for='mapping_name')
    assert OrchestrationResult._decision_from_dict(question.to_dict()) == question
    for change in ({'type': 'message'}, {'clarification_for': 'navigation'},
                   {'clarification_for': True}):
        with pytest.raises(ValidationError):
            replace(question, **change).validate()


@pytest.mark.parametrize('text,literal', [
    ('"작업실로"', '작업실로'), ('“거실로”', '거실로'), ("'집으로 해'", '집으로 해'),
    ('"순찰해"', '순찰해'), ('작업실로', None), ('작업실로 이동해', None),
    ('"작업실로"를 번역해', None), ('"거실"로 가', None), ('"../집"', None),
    ('"집.yaml"', None), ('" 집 "', None), ('""', None), ('“거실"', None),
])
def test_only_whole_valid_quoted_field_is_literal(text, literal):
    assert quoted_mapping_name(request(text)) == literal
    assert quoted_mapping_name(request(text, pending=False)) is None


@pytest.mark.parametrize('text,expected', [
    ('주방으로', '주방'), ('거실로', '거실'), ('현관으로', '현관'),
    ('주방으로 가', None), ('주방으로가', None), ('주방으로 가볼까', None),
    ('주방으로 오너라', None), ('지도는 됐고 주방으로', None),
    ('주방으로라는 말 번역해', None), ('아니 주방으로 가지 마', None),
    ('순찰해', None), ('그만해', None), ('작업실로', None), ('거실', None),
    ('"주방으로"', '주방으로'),
])
def test_whole_registered_place_particle_answer_is_a_field(text, expected):
    source = replace(request(text), navigation_locations=('주방', '거실', '현관'))
    assert mapping_name_binding(source) == expected
    assert mapping_name_binding(replace(source, mapping_name_requested=False)) is None


def test_name_binding_never_guesses_between_colliding_registered_names():
    source = replace(request('주방으로'), navigation_locations=('주방', '주방으'))
    assert mapping_name_binding(source) is None
    source = replace(source, navigation_locations=('주방', '주방으로'))
    assert mapping_name_binding(source) is None


@pytest.mark.parametrize('text,pending,forced', [
    ('"작업실로"', True, True), ('"거실"', True, True),
    ('작업실로', True, False), ('거실로 가', True, False),
    ('"작업실로"', False, False), ('"../집"', True, False),
    ('"작업실로"를 번역해', True, False),
])
def test_provider_receives_explicit_state_and_only_constrains_literal_field(text, pending, forced):
    source = request(text, pending)
    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(payload)
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [{
            'type': 'output_text', 'text': json.dumps(dict(
                type='clarification', message='지도 이름을 알려주세요.', reason='missing_name',
                confidence=1., clarification_for='mapping_name',
            )),
        }]}]}

    provider = OpenAIResponsesProvider(api_key='test-only', model='test', transport=transport)
    result = provider.complete(source, [], [], select_tool_specs(source.available_tools))
    assert result.decision.clarification_for == 'mapping_name'
    assert json.loads(captured['input'].split('\n', 1)[1])['mapping_context'] == {'awaiting_name': pending}
    assert 'clarification_for' in captured['text']['format']['schema']['required']
    if forced:
        assert captured['tool_choice'] == {'type': 'function', 'name': 'request_mapping'}
        assert len(captured['tools']) == 1
        assert captured['tools'][0]['parameters']['properties']['map_name']['enum'] == [text[1:-1]]
    else:
        assert captured['tool_choice'] == 'auto'
        assert len(captured['tools']) == 3
    public = source.to_dict()
    public.pop('mapping_name_requested', None)
    plain = provider.build_payload(AgentRequest.from_dict(public), [], [], [])
    assert 'clarification_for' not in plain['text']['format']['schema']['properties']


def test_provider_binds_registered_place_particle_without_changing_global_tool_schema():
    source = replace(request('주방으로'), navigation_locations=('주방', '거실'))
    tools = select_tool_specs(source.available_tools)
    provider = OpenAIResponsesProvider(api_key='test-only', model='test')
    payload = provider.build_payload(source, [], [], tools)
    assert payload['tool_choice'] == {'type': 'function', 'name': 'request_mapping'}
    assert payload['tools'][0]['parameters']['properties']['map_name']['enum'] == ['주방']
    later = provider.build_payload(replace(source, mapping_name_requested=False), [], [], tools)
    assert later['tool_choice'] == 'auto'
    assert all('enum' not in t['parameters']['properties'].get('map_name', {}) for t in later['tools'])


class Provider:
    def __init__(self):
        self.requests, self.runtimes = [], []
        self.entered, self.release = Event(), None

    def complete(self, request, memories, history, tools, conversation_summary=None):
        self.requests.append(request)
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(5)
        if request.utterance == '지도 만들어줘' or (
                request.utterance == '응' and request.mapping_name_requested):
            decision = AgentDecision(type='clarification', message='지도 이름은 무엇으로 할까요?',
                                     clarification_for='mapping_name')
        elif request.mapping_name_requested and request.utterance == '"작업실로"':
            decision = AgentDecision(type='tool_call', message='', tool_name='request_mapping',
                                     arguments={'map_name': '작업실로'})
        else:
            decision = AgentDecision(type='message', message='알겠어요.')
        return ProviderResult(decision=decision, provider='fixed', model='fixed', latency_ms=0)


@pytest.fixture
def mapping(tmp_path, monkeypatch):
    clock = [100.]
    monkeypatch.setattr(speech_dialogue, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    manager, provider = Manager(), Provider()
    missions = SpeechMissions(manager)

    def factory():
        runtime = build_orchestrator(Settings(database_path=str(tmp_path / 'dialogue.db')))
        runtime.provider = provider
        configure_speech_missions(runtime)
        provider.runtimes.append(runtime)
        return runtime

    worker = DialogueWorker(factory, 'speaker', missions=missions)
    try:
        yield SimpleNamespace(worker=worker, manager=manager, provider=provider, clock=clock)
    finally:
        if provider.release is not None:
            provider.release.set()
        worker.close()


def turn(run, text, *, publish=True, uid=None):
    assert run.worker.submit(uid or str(len(run.provider.requests)), text)
    reply = collect(run.worker)
    assert reply['kind'] == 'answer', reply
    if publish:
        assert run.worker.publish_reply(reply, lambda text: True)
    return reply


def test_question_reasks_after_ack_then_dispatches_exact_name_once(mapping):
    run = mapping
    turn(run, '지도 만들어줘')
    assert not run.provider.requests[-1].mapping_name_requested
    turn(run, '응')
    assert run.provider.requests[-1].mapping_name_requested
    answer = turn(run, '"작업실로"')
    assert run.provider.requests[-1].mapping_name_requested
    assert run.manager.calls[0][:2] == ('autoslam', {'map_name': '작업실로'})
    run.worker.publish_reply(answer, lambda text: True)
    turn(run, '"작업실로"')
    assert not run.provider.requests[-1].mapping_name_requested
    assert len(run.manager.calls) == 1


@pytest.mark.parametrize('invalidation', [
    'unpublished', 'failed_publication', 'expired', 'suspend', 'new_conversation',
    'decline', 'unrelated', 'cached_question',
])
def test_stale_question_does_not_turn_a_place_name_into_mapping(mapping, invalidation):
    run = mapping
    question = turn(run, '지도 만들어줘', publish=False, uid='question')
    if invalidation == 'failed_publication':
        assert run.worker.publish_reply(question, lambda text: False) is None
    elif invalidation != 'unpublished':
        run.worker.publish_reply(question, lambda text: True)
    if invalidation == 'expired':
        run.clock[0] += 121
    elif invalidation == 'suspend':
        run.worker.suspend()
        run.worker.resume()
    elif invalidation in {'decline', 'unrelated', 'cached_question'}:
        turn(run, '취소해' if invalidation == 'decline' else '오늘 뭐 했어')
        if invalidation == 'cached_question':
            turn(run, '지도 만들어줘', uid='question')
    if invalidation == 'new_conversation':
        turn(run, '새 대화 시작하자')
    turn(run, '"작업실로"')
    assert not run.provider.requests[-1].mapping_name_requested
    assert run.manager.calls == []


def test_answer_before_question_publication_cannot_fill_name(mapping):
    run = mapping
    question = turn(run, '지도 만들어줘', publish=False)
    run.provider.entered.clear()
    run.provider.release = Event()
    assert run.worker.submit('early', '"작업실로"')
    assert run.provider.entered.wait(5)
    run.worker.publish_reply(question, lambda text: True)
    run.provider.release.set()
    run.worker.publish_reply(collect(run.worker), lambda text: True)
    assert not run.provider.requests[-1].mapping_name_requested
    assert run.manager.calls == []


def test_failed_next_turn_still_consumes_name_question(mapping):
    run = mapping
    turn(run, '지도 만들어줘')
    store = run.provider.runtimes[0].conversation_store
    resume = store.resume_or_create

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('database locked')

    store.resume_or_create = fail
    try:
        assert run.worker.submit('failed-answer', '"작업실로"')
        reply = collect(run.worker)
        assert reply['kind'] == 'error'
        run.worker.publish_reply(reply, lambda text: True)
    finally:
        store.resume_or_create = resume
    turn(run, '"작업실로"')
    assert not run.provider.requests[-1].mapping_name_requested
    assert run.manager.calls == []


@pytest.mark.parametrize('change', ['expiry', 'session'])
def test_mapping_question_is_rechecked_before_manager_dispatch(mapping, change):
    run = mapping
    turn(run, '지도 만들어줘')
    reply = turn(run, '"작업실로"', publish=False)
    if change == 'expiry':
        run.clock[0] += 121
    else:
        turn(run, '다른 얘기 하자')
    run.worker.publish_reply(reply, lambda text: True)
    assert run.manager.calls == []


@pytest.mark.parametrize('failure', ['slow_read', 'database_error'])
def test_failed_publication_binding_does_not_arm_question(mapping, failure):
    run = mapping
    question = turn(run, '지도 만들어줘', publish=False)
    store = run.provider.runtimes[0].conversation_store
    snapshot = store.snapshot

    def read(*args, **kwargs):
        if failure == 'database_error':
            raise sqlite3.OperationalError('database locked')
        result = snapshot(*args, **kwargs)
        run.clock[0] += 121
        return result

    store.snapshot = read
    try:
        run.worker.publish_reply(question, lambda text: True)
    finally:
        store.snapshot = snapshot
    turn(run, '"작업실로"')
    assert not run.provider.requests[-1].mapping_name_requested
    assert run.manager.calls == []


@pytest.mark.parametrize('tool,args', [
    ('request_navigation', {'location': '거실'}),
    ('request_mapping', {'map_name': '작업실'}),
    ('request_patrol', {'thoroughness': 'light'}),
])
def test_policy_blocks_model_that_ignores_literal_field_binding(mapping, tool, args):
    turn(mapping, '지도 만들어줘')
    runtime = mapping.provider.runtimes[0]
    result = runtime.safety_policy.evaluate(request(), AgentDecision(
        type='tool_call', message='', tool_name=tool, arguments=args,
    ))
    assert not result.allowed and result.code == 'mapping_name_mismatch'
