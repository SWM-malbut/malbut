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
    'suspend', 'close', 'expired', 'memory', 'new_turn', 'slow_snapshot', 'cancel_request',
])
def test_invalidated_proposal_never_reaches_manager(dialogue, invalidator):
    worker, manager, _, _, clock, runtimes = dialogue
    assert worker.submit('follow', '따라와')
    reply = collect(worker)
    if invalidator == 'suspend':
        worker.suspend()
        worker.resume()
    elif invalidator == 'cancel_request':
        assert worker.cancel_request('follow')
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
