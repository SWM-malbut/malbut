"""Classify interruptions in ordered dialogue context without recording turns."""

import threading
from types import SimpleNamespace

import pytest

from malbut_agent_server import speech_dialogue
from malbut_agent_server.speech_dialogue import (
    DialogueWorker, validate_interruption_input,
)
from test_speech_dialogue import RuntimeFactory, collect, wait_until


def make_worker(classify, *, capacity=10):
    factory = RuntimeFactory()

    def runtime_factory():
        runtime = factory()
        runtime.speech_addressee = SimpleNamespace(classify=classify)
        return runtime

    return DialogueWorker(runtime_factory, 'speaker', capacity=capacity), factory


def test_classification_uses_same_ordered_context_without_a_user_turn():
    calls = []

    def classify(text, snapshot):
        calls.append((text, snapshot, threading.get_ident()))
        return 'addressed'

    worker, factory = make_worker(classify)
    original = '  끼어드는 말\n'
    try:
        assert worker.submit('before', '앞선 대화')
        assert worker.submit_interruption('interruption', 'playback', original)
        assert worker.submit('after', '다음 대화')
        replies = collect(worker, 3)
        assert [reply['kind'] for reply in replies] == ['answer', 'addressee', 'answer']
        assert replies[1] == {
            'kind': 'addressee', 'utterance_id': 'interruption',
            'playback_id': 'playback', 'decision': 'addressed',
        }
        assert calls[0][0] == original
        assert [turn.user_content for turn in calls[0][1].turns] == ['앞선 대화']
        assert calls[0][1].session.conversation_id == replies[0]['conversation_id']
        assert calls[0][2] == factory.created[0] != threading.get_ident()
        assert [request.utterance for request in factory.handled] == ['앞선 대화', '다음 대화']
        snapshot = factory.runtime.conversation_store.snapshot(
            'speaker', replies[0]['conversation_id'],
        )
        assert [turn.user_content for turn in snapshot.turns] == ['앞선 대화', '다음 대화']
        spoken = []
        assert worker.publish_reply(replies[1], spoken.append) is None
        assert spoken == []
    finally:
        worker.close()


def test_snapshot_is_bounded_to_ten_turns():
    snapshots = []
    worker, factory = make_worker(lambda text, snapshot: snapshots.append(snapshot) or 'unknown')
    try:
        for index in range(12):
            assert worker.submit(f'normal-{index}', f'문장 {index}')
            collect(worker, 1)
        assert worker.submit_interruption('check', 'reply', '누구에게')
        collect(worker, 1)
        assert len(snapshots[0].turns) == 10
        assert len(factory.handled) == 12
    finally:
        worker.close()


def test_positive_classification_waits_for_normal_transcript_before_recording_turn():
    worker, factory = make_worker(lambda *_: 'addressed')
    try:
        assert worker.submit_interruption('same-id', 'reply', '사용자 질문')
        assert collect(worker, 1)[0]['decision'] == 'addressed'
        assert factory.handled == []
        assert worker.submit('same-id', '사용자 질문')
        reply = collect(worker, 1)[0]
        assert reply['kind'] == 'answer'
        assert len(factory.handled) == 1
        snapshot = factory.runtime.conversation_store.snapshot(
            'speaker', reply['conversation_id'],
        )
        assert [turn.user_content for turn in snapshot.turns] == ['사용자 질문']
    finally:
        worker.close()


@pytest.mark.parametrize('outcome', ['not_addressed', 'unknown', 'invalid', None, True])
def test_classification_decisions_are_strict_and_never_generate_speech(outcome):
    worker, factory = make_worker(lambda *_: outcome)
    try:
        assert worker.submit_interruption('uid', 'pid', '발화')
        reply = collect(worker, 1)[0]
        assert reply['decision'] == (outcome if outcome in ('not_addressed', 'unknown')
                                     else 'unknown')
        assert 'text' not in reply
        assert factory.handled == []
    finally:
        worker.close()


@pytest.mark.parametrize('failure', ['classifier', 'snapshot'])
def test_classification_errors_return_correlated_unknown(failure):
    def fail(*_args, **_kwargs):
        raise RuntimeError('PRIVATE PROVIDER DETAILS')

    worker, factory = make_worker(fail)
    try:
        wait_until(lambda: factory.runtime is not None)
        if failure == 'snapshot':
            factory.runtime.conversation_store.snapshot = fail
        assert worker.submit_interruption('uid', 'pid', '발화')
        assert collect(worker, 1) == [{
            'kind': 'addressee', 'utterance_id': 'uid',
            'playback_id': 'pid', 'decision': 'unknown',
        }]
        assert factory.handled == []
    finally:
        worker.close()


def test_pending_duplicates_and_conflicts_do_not_repeat_classification():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def classify(text, _snapshot):
        calls.append(text)
        entered.set()
        assert release.wait(5)
        return 'addressed'

    worker, _factory = make_worker(classify, capacity=1)
    try:
        assert worker.submit_interruption('uid', 'pid', '원문')
        assert entered.wait(5)
        assert worker.submit_interruption('uid', 'pid', '원문')
        assert not worker.submit_interruption('other', 'pid', '처리 용량 초과')
        assert not worker.submit_interruption('uid', 'different', '충돌')
        release.set()
        assert collect(worker, 1)[0]['decision'] == 'unknown'
        assert not worker.submit_interruption('uid', 'pid', '원문')
        assert calls == ['원문']
    finally:
        release.set()
        worker.close()


def test_completed_duplicate_replays_the_result_without_classifying_again():
    calls = []
    worker, _factory = make_worker(lambda text, _: calls.append(text) or 'not_addressed')
    try:
        assert worker.submit_interruption('uid', 'pid', '원문')
        first = collect(worker, 1)
        assert worker.submit_interruption('uid', 'pid', '원문')
        assert worker.drain() == first
        assert calls == ['원문']
    finally:
        worker.close()


def test_cached_results_still_obey_unread_capacity():
    worker, _factory = make_worker(lambda *_: 'addressed', capacity=1)
    try:
        assert worker.submit_interruption('uid', 'pid', '원문')
        collect(worker, 1)
        assert worker.submit_interruption('uid', 'pid', '원문')
        assert not worker.submit_interruption('uid', 'pid', '원문')
        assert len(worker.drain()) == 1
        assert worker.has_capacity()
    finally:
        worker.close()


def test_conflict_invalidates_an_unread_positive_result():
    worker, _factory = make_worker(lambda *_: 'addressed')
    try:
        assert worker.submit_interruption('uid', 'pid', '원문')
        wait_until(lambda: bool(worker._results))
        assert not worker.submit_interruption('uid', 'pid', '달라진 원문')
        assert collect(worker, 1)[0]['decision'] == 'unknown'
    finally:
        worker.close()


def test_completed_duplicate_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(speech_dialogue, 'MAX_INTERRUPTION_IDS', 2)
    worker, _factory = make_worker(lambda *_: 'unknown')
    try:
        for uid in ('one', 'two', 'three'):
            assert worker.submit_interruption(uid, 'pid', '발화')
            collect(worker, 1)
        assert list(worker._interruptions) == ['two', 'three']
    finally:
        worker.close()


def test_cache_never_evicts_an_inflight_request(monkeypatch):
    monkeypatch.setattr(speech_dialogue, 'MAX_INTERRUPTION_IDS', 1)
    entered, release = threading.Event(), threading.Event()

    def classify(*_args):
        entered.set()
        assert release.wait(5)
        return 'unknown'

    worker, _factory = make_worker(classify)
    try:
        assert worker.submit_interruption('one', 'pid', '발화')
        assert entered.wait(5)
        assert not worker.submit_interruption('two', 'pid', '다른 발화')
        release.set()
        assert collect(worker, 1)[0]['utterance_id'] == 'one'
    finally:
        release.set()
        worker.close()


def test_startup_failure_returns_unknown_for_queued_interruption():
    release = threading.Event()

    def fail():
        assert release.wait(5)
        raise RuntimeError('PRIVATE STARTUP DETAILS')

    worker = DialogueWorker(fail, 'speaker')
    try:
        assert worker.submit_interruption('uid', 'pid', '발화')
        release.set()
        assert collect(worker, 1) == [{
            'kind': 'addressee', 'utterance_id': 'uid',
            'playback_id': 'pid', 'decision': 'unknown',
        }]
        assert not worker.submit_interruption('late', 'pid', '발화')
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize('uid,pid,text', [
    ('', 'pid', '발화'), ('uid', '', '발화'), ('uid', ' \n', '발화'),
    ('uid', 'pid', ''), ('uid', 'pid', 'x' * 2001),
    ('x' * 257, 'pid', '발화'), ('uid', 'x' * 257, '발화'),
    ('uid', '\ud800', '발화'), ('uid', 'pid', '\ud800'),
])
def test_invalid_interruption_input_is_rejected(uid, pid, text):
    with pytest.raises(ValueError):
        validate_interruption_input(uid, pid, text)
