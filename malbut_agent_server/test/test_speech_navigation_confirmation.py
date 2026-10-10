"""Check destination matching and published, single-turn confirmation bindings."""

from threading import Event
from types import SimpleNamespace
import sqlite3

import pytest
import yaml

from malbut_agent_server import speech_dialogue
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.schemas import AgentDecision, ProviderResult
from malbut_agent_server.speech_dialogue import DialogueWorker
from malbut_agent_server.speech_mission_policy import configure_speech_missions
from malbut_agent_server.speech_missions import SpeechMissions
from malbut_agent_server.speech_navigation import NavigationTargets
from test_speech_mission_dialogue import Manager, collect


class Provider:
    """Force a canonical proposal so tests exercise the server's decision boundary."""

    def __init__(self):
        self.requests, self.runtimes = [], []
        self.entered, self.release = Event(), None

    def complete(self, request, memories, history, tools, conversation_summary=None):
        self.requests.append(request)
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(5)
        if request.utterance in {'아니', '오늘 뭐 했어'}:
            decision = AgentDecision(type='message', message='알겠어요.')
        else:
            decision = AgentDecision(
                type='tool_call', tool_name='request_navigation',
                arguments={'location': '거실'}, message='',
            )
        return ProviderResult(decision=decision, provider='fixed', model='fixed', latency_ms=0)


@pytest.fixture
def navigation(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(speech_dialogue, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    (tmp_path / 'home.pgm').write_bytes(b'P5\n1 1\n255\n\xff')
    selected = tmp_path / 'home.yaml'
    selected.write_text('image: home.pgm\nresolution: 0.05\n', encoding='utf-8')
    config = tmp_path / 'targets.yaml'
    catalog = {
        'map': str(selected), 'frame_id': 'map',
        'locations': {'거실': {'x': 1.25, 'y': -2.5, 'yaw': 0}},
    }
    config.write_text(yaml.safe_dump(catalog, allow_unicode=True), encoding='utf-8')
    manager, provider = Manager(), Provider()
    missions = SpeechMissions(manager, NavigationTargets(config))
    missions.observe_localization({'mode': 'LOCALIZATION', 'map': str(selected)})

    def factory():
        runtime = build_orchestrator(Settings(database_path=str(tmp_path / 'dialogue.db')))
        runtime.provider = provider
        configure_speech_missions(runtime, navigation_enabled=True)
        provider.runtimes.append(runtime)
        return runtime

    worker = DialogueWorker(factory, 'speaker', missions=missions)
    try:
        yield SimpleNamespace(
            worker=worker, manager=manager, provider=provider, missions=missions,
            clock=clock, config=config, catalog=catalog, selected=selected,
        )
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


@pytest.mark.parametrize('utterance', ['거실로 가', '기실로 가', '기실로가', '거슬로 가'])
def test_exact_or_unique_close_name_dispatches_without_confirmation(navigation, utterance):
    run = navigation
    reply = turn(run, utterance)
    assert '이동할까요' not in reply['text']
    assert len(run.manager.calls) == 1
    assert run.provider.requests[0].navigation_locations == ('거실',)
    assert run.manager.calls[0][1]['pose']['pose']['position']['x'] == 1.25


def test_distant_name_question_is_committed_then_confirmation_dispatches_once(navigation):
    run = navigation
    question = turn(run, '베란다로 가')
    assert '거실' in question['text'] and '이동할까요' in question['text']
    assert run.manager.calls == []
    snapshot = run.provider.runtimes[0].conversation_store.snapshot(
        'speaker', question['conversation_id'], limit=1,
    )
    assert snapshot.turns[0].assistant_content == question['text']
    answer = turn(run, '응')
    assert run.provider.requests[-1].navigation_confirmation == '거실'
    assert len(run.manager.calls) == 1
    run.worker.publish_reply(answer, lambda text: True)
    turn(run, '응')
    assert run.provider.requests[-1].navigation_confirmation == ''
    assert len(run.manager.calls) == 1


@pytest.mark.parametrize('invalidation', [
    'unpublished', 'failed_publication', 'expired', 'suspend', 'new_conversation',
    'decline', 'unrelated', 'map_transition', 'catalog_change',
])
def test_stale_or_absent_question_cannot_authorize_generic_answer(navigation, invalidation):
    run = navigation
    question = turn(run, '베란다로 가', publish=False)
    if invalidation == 'failed_publication':
        assert run.worker.publish_reply(question, lambda text: False) is None
    elif invalidation != 'unpublished':
        run.worker.publish_reply(question, lambda text: True)
    if invalidation == 'expired':
        run.clock[0] += 31
    elif invalidation == 'suspend':
        run.worker.suspend()
        run.worker.resume()
    elif invalidation in {'decline', 'unrelated'}:
        turn(run, '아니' if invalidation == 'decline' else '오늘 뭐 했어')
    elif invalidation == 'map_transition':
        run.missions.observe_localization({'mode': 'SWITCHING', 'map': str(run.selected)})
        run.missions.observe_localization({'mode': 'LOCALIZATION', 'map': str(run.selected)})
    elif invalidation == 'catalog_change':
        run.catalog['locations']['거실']['x'] = 9
        run.config.write_text(yaml.safe_dump(run.catalog, allow_unicode=True), encoding='utf-8')
    turn(run, '새 대화 시작하자. 응' if invalidation == 'new_conversation' else '응')
    assert run.provider.requests[-1].navigation_confirmation == ''
    assert run.manager.calls == []


@pytest.mark.parametrize('change', ['expiry', 'catalog', 'session'])
def test_confirmation_is_rechecked_at_dispatch(navigation, change):
    run = navigation
    turn(run, '베란다로 가')
    answer = turn(run, '응', publish=False)
    assert run.provider.requests[-1].navigation_confirmation == '거실'
    if change == 'expiry':
        run.clock[0] += 31
    elif change == 'catalog':
        run.catalog['locations']['거실']['x'] = 9
        run.config.write_text(yaml.safe_dump(run.catalog, allow_unicode=True), encoding='utf-8')
    else:
        turn(run, '오늘 뭐 했어')
    run.worker.publish_reply(answer, lambda text: True)
    assert run.manager.calls == []


def test_answer_received_before_question_publication_cannot_confirm(navigation):
    run = navigation
    question = turn(run, '베란다로 가', publish=False)
    run.provider.entered.clear()
    run.provider.release = Event()
    assert run.worker.submit('early-answer', '응')
    assert run.provider.entered.wait(5)
    run.worker.publish_reply(question, lambda text: True)
    run.provider.release.set()
    answer = collect(run.worker)
    run.worker.publish_reply(answer, lambda text: True)
    assert run.provider.requests[-1].navigation_confirmation == ''
    assert run.manager.calls == []


def test_cached_question_does_not_rearm_confirmation(navigation):
    run = navigation
    turn(run, '베란다로 가', uid='question')
    turn(run, '아니')
    turn(run, '베란다로 가', uid='question')
    turn(run, '응')
    assert run.provider.requests[-1].navigation_confirmation == ''
    assert run.manager.calls == []


@pytest.mark.parametrize('failure', ['slow_read', 'database_error'])
def test_failed_or_slow_postpublication_read_does_not_arm_confirmation(navigation, failure):
    run = navigation
    question = turn(run, '베란다로 가', publish=False)
    store = run.provider.runtimes[0].conversation_store
    snapshot = store.snapshot

    def read(*args, **kwargs):
        if failure == 'database_error':
            raise sqlite3.OperationalError('database locked')
        result = snapshot(*args, **kwargs)
        run.clock[0] += 31
        return result

    store.snapshot = read
    try:
        assert run.worker.publish_reply(question, lambda text: True)
    finally:
        store.snapshot = snapshot
    turn(run, '응')
    assert run.provider.requests[-1].navigation_confirmation == ''
    assert run.manager.calls == []


def test_failed_next_turn_still_consumes_confirmation(navigation):
    run = navigation
    turn(run, '베란다로 가')
    store = run.provider.runtimes[0].conversation_store
    resume = store.resume_or_create

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('database locked')

    store.resume_or_create = fail
    try:
        assert run.worker.submit('failed-answer', '응')
        reply = collect(run.worker)
        assert reply['kind'] == 'error'
        run.worker.publish_reply(reply, lambda text: True)
    finally:
        store.resume_or_create = resume
    turn(run, '응')
    assert run.provider.requests[-1].navigation_confirmation == ''
    assert run.manager.calls == []
