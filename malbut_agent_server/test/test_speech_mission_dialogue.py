"""Exercise committed voice proposals and ROS-thread mission dispatch."""

from threading import Event, get_ident
import time

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.orchestrator import MemoryChangedError
from malbut_agent_server.schemas import AgentDecision, ProviderResult
from malbut_agent_server.speech_dialogue import DialogueWorker
from malbut_agent_server.speech_mission_policy import configure_speech_missions
from malbut_agent_server.speech_missions import SpeechMissions


class Manager:
    """Record transport calls without importing ROS or starting hardware."""

    def __init__(self):
        self.calls, self.cancels, self.records = [], [], {}

    def submit(self, capability_id, arguments, request_id):
        self.calls.append((capability_id, arguments, request_id, get_ident()))
        self.records[request_id] = {
            'request_id': request_id, 'capability_id': capability_id,
            'kind': 'accepted', 'state': 'RUNNING', 'accepted': True, 'terminal': False,
        }
        return request_id

    def snapshot(self, request_id):
        return dict(self.records[request_id])

    def cancel(self, request_id):
        self.cancels.append(request_id)
        return {**self.records[request_id], 'kind': 'cancel_requested'}


class Provider:
    """Choose fixed actions while using real policy and conversation storage."""

    def __init__(self):
        self.entered, self.release, self.requests = Event(), None, []

    def complete(self, request, memories, history, tools, conversation_summary=None):
        self.requests.append(request)
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(5)
        tool, arguments = {
            '취소해': ('cancel_voice_mission', {}),
            '순찰해': ('request_patrol', {'thoroughness': 'normal'}),
        }.get(request.utterance, ('request_follow_person', {}))
        return ProviderResult(
            decision=AgentDecision(type='tool_call', tool_name=tool,
                                   arguments=arguments, message='', confidence=1.0),
            provider='fixed', model='fixed', latency_ms=0.0,
        )


def collect(worker):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        replies = [item for item in worker.drain() if item['kind'] != 'progress']
        if replies:
            assert len(replies) == 1
            return replies[0]
        time.sleep(0.002)
    raise AssertionError('dialogue did not finish')


@pytest.fixture
def dialogue(tmp_path):
    manager, provider = Manager(), Provider()
    missions, clock, runtimes = SpeechMissions(manager), [time.time()], []

    def factory():
        runtime = build_orchestrator(Settings(database_path=str(tmp_path / 'dialogue.db')))
        runtime.provider = provider
        runtime._state_clock = lambda: clock[0]
        configure_speech_missions(runtime)
        runtimes.append(runtime)
        return runtime

    worker = DialogueWorker(factory, 'speaker', missions=missions)
    try:
        yield worker, manager, provider, missions, clock, runtimes
    finally:
        if provider.release is not None:
            provider.release.set()
        worker.close()


def test_committed_follow_sent_once_on_owner_thread_and_cancel_does_not_wait(dialogue):
    worker, manager, provider, missions, _, runtimes = dialogue
    assert worker.submit('follow', '따라와')
    reply = collect(worker)
    assert reply['kind'] == 'answer'
    assert manager.calls == []
    assert 'request_follow_person' in provider.requests[0].available_tools
    assert runtimes[0].conversation_store.snapshot(
        'speaker', reply['conversation_id'], limit=1).turns
    assert worker.publish_reply(reply, lambda text: True)
    assert manager.calls[0][0] == 'follow_person'
    assert manager.calls[0][3] == get_ident()
    worker.publish_reply(reply, lambda text: True)
    assert len(manager.calls) == 1
    missions.handle(manager.snapshot(manager.calls[0][2]))
    assert worker.submit('cancel', '취소해')
    cancel = collect(worker)
    assert worker.publish_reply(cancel, lambda text: True)
    assert manager.cancels == [manager.calls[0][2]]


@pytest.mark.parametrize('invalidator', [
    'suspend', 'close', 'expired', 'memory', 'new_turn', 'slow_snapshot',
])
def test_invalidated_proposal_never_reaches_manager(dialogue, invalidator):
    worker, manager, _, _, clock, runtimes = dialogue
    assert worker.submit('follow', '따라와')
    reply = collect(worker)
    if invalidator == 'suspend':
        worker.suspend()
        worker.resume()
    elif invalidator == 'close':
        worker.close()
    elif invalidator == 'expired':
        clock[0] += 20
    elif invalidator == 'memory':
        def invalid_memory():
            raise MemoryChangedError('changed')
        reply._memory_validator = invalid_memory
    elif invalidator == 'slow_snapshot':
        snapshot = runtimes[0].conversation_store.snapshot

        def slow_snapshot(*args, **kwargs):
            value = snapshot(*args, **kwargs)
            clock[0] += 20
            return value
        runtimes[0].conversation_store.snapshot = slow_snapshot
    else:
        assert worker.submit('new-turn', '취소해')
        collect(worker)
    worker.publish_reply(reply, lambda text: True)
    assert manager.calls == []


def test_situation_preempts_inflight_action_without_late_dispatch(dialogue):
    worker, manager, provider, _, _, _ = dialogue
    provider.release = Event()
    assert worker.submit('first', '따라와')
    assert provider.entered.wait(5)
    worker.suspend()
    worker.resume()
    assert worker.submit('next', '순찰해')
    provider.release.set()
    reply = collect(worker)
    assert reply['utterance_id'] == 'next'
    worker.publish_reply(reply, lambda text: True)
    assert [item[0] for item in manager.calls] == ['patrol']


def test_failed_speech_publication_does_not_resend_mission(dialogue):
    worker, manager, _, _, _, _ = dialogue
    assert worker.submit('follow', '따라와')
    reply = collect(worker)
    assert worker.publish_reply(reply, lambda text: False) is None
    assert worker.publish_reply(reply, lambda text: True)
    assert len(manager.calls) == 1


@pytest.mark.parametrize('text,tool,arguments,capability,payload', [
    ('집 안 탐색해서 새집이라는 이름으로 지도 저장해 줘',
     'request_mapping', {'map_name': '새집'}, 'autoslam', {'map_name': '새집'}),
    ('구집이 아니라 새집이라는 이름으로 지도 만들어줘',
     'request_mapping', {'map_name': '새집'}, 'autoslam', {'map_name': '새집'}),
    ('새집, 정확히는 우리 집이라는 이름으로 지도 만들어줘',
     'request_mapping', {'map_name': '우리 집'}, 'autoslam', {'map_name': '우리 집'}),
    ('가볍게가 아니라 꼼꼼하게 순찰해 줘',
     'request_patrol', {'thoroughness': 'thorough'}, 'patrol', {'thoroughness': 2}),
    ('자동이 아니라 전역 탐색으로 위치 찾아줘',
     'request_relocalization', {'method': 'global_search'}, 'relocalize', {'method': 2}),
])
def test_memory_followup_does_not_replace_robot_save_or_correction(
    dialogue, monkeypatch, text, tool, arguments, capability, payload,
):
    worker, manager, provider, _, _, runtimes = dialogue
    monkeypatch.setattr(provider, 'complete', lambda *a, **kw: ProviderResult(
        decision=AgentDecision(type='tool_call', tool_name=tool, arguments=arguments, message=''),
        provider='fixed', model='fixed', latency_ms=0.0,
    ))
    assert worker.submit('corrected-robot-request', text)
    reply = collect(worker)
    assert reply['kind'] == 'answer'
    assert worker.publish_reply(reply, lambda text: True)
    assert [(call[0], call[1]) for call in manager.calls] == [(capability, payload)]
    assert runtimes[0].memory_store.list_for_user('speaker') == []


def test_map_save_name_question_survives_memory_commit(dialogue, monkeypatch):
    worker, manager, provider, _, _, runtimes = dialogue
    question = '새 지도를 어떤 이름으로 저장할까요?'
    monkeypatch.setattr(provider, 'complete', lambda *a, **kw: ProviderResult(
        decision=AgentDecision(type='clarification', message=question),
        provider='fixed', model='fixed', latency_ms=0.0,
    ))
    assert worker.submit('map-without-name', '집 안 탐색해서 지도 저장해줘')
    reply = collect(worker)
    assert worker.publish_reply(reply, lambda text: True)['text'] == question
    assert not manager.calls
    assert runtimes[0].personal_memory.pending_question('speaker', reply['conversation_id']) is None


@pytest.mark.parametrize('text', [
    '내 이름 기억을 수정하고 순찰해 줘',
    '내 이름을 수연으로 바꿔주고 순찰해 줘',
])
def test_real_memory_management_still_blocks_partial_robot_execution(dialogue, monkeypatch, text):
    worker, manager, provider, _, _, _ = dialogue
    monkeypatch.setattr(provider, 'complete', lambda *a, **kw: ProviderResult(
        decision=AgentDecision(type='tool_call', tool_name='request_patrol',
                               arguments={'thoroughness': 'normal'}, message=''),
        provider='fixed', model='fixed', latency_ms=0.0,
    ))
    assert worker.submit('mixed-request', text)
    reply = collect(worker)
    assert '로봇 실행과 기억 관리' in worker.publish_reply(reply, lambda text: True)['text']
    assert not manager.calls


@pytest.mark.parametrize('name', ['/tmp/home', '../home', '새집.yaml', '집/거실', '🙂'])
def test_invalid_map_name_gets_actionable_reply_before_preparation(dialogue, monkeypatch, name):
    worker, manager, provider, _, _, runtimes = dialogue
    monkeypatch.setattr(provider, 'complete', lambda *a, **kw: ProviderResult(
        decision=AgentDecision(type='tool_call', tool_name='request_mapping',
                               arguments={'map_name': name}, message=''),
        provider='fixed', model='fixed', latency_ms=0.0,
    ))
    assert worker.submit('invalid-name', f'이 이름 그대로 지도를 만들어줘: {name}')
    reply = collect(worker)
    assert reply['kind'] == 'answer'
    published = worker.publish_reply(reply, lambda text: True)
    assert '지도 이름' in published['text']
    assert '경로와 확장자' in published['text']
    assert not manager.calls
    history = runtimes[0].conversation_store.snapshot('speaker', reply['conversation_id'], limit=1)
    assert history.turns[-1].assistant_content == published['text']
