"""Bounded volatile extraction must not delay replies or outlive consent/deletion."""

import threading
from types import SimpleNamespace

import pytest

from malbut_agent_server.companion_dialogue import CompanionDialogue
from malbut_agent_server.companion_extractor import trivial_utterance
from test_companion_memory import NOW, Provider, SESSION, SUMMARY, wait_memory
from test_companion_memory import setup as setup


@pytest.fixture
def blocked(setup):
    service, client, wall, mono = setup
    entered, release = threading.Event(), threading.Event()
    calls = []

    def extract(text, *_args):
        calls.append(text)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3), 'test must release the fake provider'
        return [dict(SUMMARY, summary='요약 ' + str(len(calls)))]

    service.extractor = SimpleNamespace(extract=extract)
    yield service, client, wall, mono, entered, release, calls
    release.set()
    wait_memory(service)


def test_answer_returns_while_extraction_is_blocked_and_source_never_enters_db(blocked, tmp_path):
    service, _, _, _, entered, release, _ = blocked
    dialogue = CompanionDialogue(service)
    dialogue.begin(SESSION)
    text = '원문은 파일에 남기지 않고 내일 영희와 산책할 거야'
    result = dialogue.answer(Provider(), SESSION, 'first', text, None)
    assert result and entered.wait(1) and not release.is_set()
    assert service.memory.recall('owner') == []
    assert text.encode() not in (tmp_path / 'memory.sqlite3').read_bytes()
    release.set()
    wait_memory(service)
    assert service.memory.recall('owner')[0]['summary'] == '요약 1'
    assert text.encode() not in (tmp_path / 'memory.sqlite3').read_bytes()
    assert not list(tmp_path.glob('*-wal'))
    assert not service._memory_jobs and service._memory_active is None


def test_fifo_keeps_correction_after_first_event_and_rejects_duplicate_turns(blocked):
    service, _, _, _, entered, release, calls = blocked
    binding = service.binding(SESSION)
    assert service.enqueue_remember(binding, 'first', '내일 영희와 산책할 거야', NOW - 1)
    assert entered.wait(1)
    assert service.enqueue_remember(binding, 'second', '그 산책 약속은 취소했어', NOW)
    assert not service.enqueue_remember(binding, 'second', '그 산책 약속은 취소했어', NOW)
    assert calls == ['내일 영희와 산책할 거야']
    release.set()
    wait_memory(service)
    assert calls == ['내일 영희와 산책할 거야', '그 산책 약속은 취소했어']
    assert [row['summary'] for row in service.memory.recall('owner')] == ['요약 2', '요약 1']
    assert not service.enqueue_remember(binding, 'first', '내일 영희와 산책할 거야', NOW)


@pytest.mark.parametrize('limit', ['jobs', 'characters'])
def test_pending_and_running_sources_share_one_hard_capacity(blocked, limit):
    service, _, _, _, entered, _, _ = blocked
    binding = service.binding(SESSION)
    text = '내용' if limit == 'jobs' else '가' * 8000
    assert service.enqueue_remember(binding, '0', text, NOW)
    assert entered.wait(1)
    count = 4 if limit == 'jobs' else 2
    for i in range(1, count):
        assert service.enqueue_remember(binding, str(i), text, NOW)
    assert not service.enqueue_remember(binding, 'overflow', '새로운 사건', NOW)
    assert len(service._memory_jobs) == count - 1
    assert service._memory_active is not None


@pytest.mark.parametrize('cancel', ['end', 'new_session', 'forget', 'withdraw', 'app_erase',
                                   'offline', 'deadline', 'retention'])
def test_cancellation_drops_queued_sources_and_fences_inflight_result(blocked, cancel):
    service, client, wall, mono, entered, release, calls = blocked
    binding = service.binding(SESSION)
    assert service.enqueue_remember(binding, 'first', '내일 산책할 거야', NOW)
    assert entered.wait(1)
    assert service.enqueue_remember(binding, 'second', '내일 친구도 만날 거야', NOW)
    if cancel == 'end':
        service.end(SESSION)
    elif cancel == 'new_session':
        service.begin('patrol-chat-' + 'b' * 32)
    elif cancel == 'forget':
        assert service.forget(binding) == 1
    elif cancel in ('withdraw', 'app_erase'):
        client.data.update(revision=2, **({'externalProcessing': False} if cancel == 'withdraw'
                                        else {'eraseRevision': 1}))
        service.sync()
    elif cancel == 'offline':
        client.sync = lambda _report: (_ for _ in ()).throw(OSError('offline'))
        with pytest.raises(OSError):
            service.sync()
    elif cancel == 'deadline':
        mono[0] += 60
        service.sync()  # Renew freshness only; this must not renew source retention.
    else:
        from malbut_agent_server.companion_memory import expires_at
        wall[0] = expires_at(NOW)
    release.set()
    wait_memory(service)
    assert calls == ['내일 산책할 거야']
    assert service.memory.recall('owner') == []


def test_forget_latest_pending_event_does_not_delete_older_summary(blocked):
    service, _, _, _, entered, release, _ = blocked
    service.memory.save('owner', 1, 'old', NOW - 60, [SUMMARY])
    binding = service.binding(SESSION)
    assert service.enqueue_remember(binding, 'new', '방금 친구를 만났어', NOW)
    assert entered.wait(1)
    assert service.forget(binding, latest=True) == 1
    release.set()
    wait_memory(service)
    assert [row['summary'] for row in service.memory.recall('owner')] == [SUMMARY['summary']]


def test_close_clears_queued_text_and_waits_for_inflight_call_without_committing(blocked):
    service, _, _, _, entered, release, calls = blocked
    binding = service.binding(SESSION)
    assert service.enqueue_remember(binding, 'first', '내일 산책할 거야', NOW)
    assert entered.wait(1)
    assert service.enqueue_remember(binding, 'second', '친구도 만날 거야', NOW)
    saved = []
    original_save = service.memory.save
    service.memory.save = lambda *args, **kwargs: saved.append(args) or original_save(*args, **kwargs)
    close = threading.Thread(target=service.close)
    close.start()
    assert service._stop.wait(1)
    with service.lock:
        assert not service._memory_jobs and service.binding(SESSION) is None
    assert close.is_alive()
    release.set()
    close.join(2)
    assert not close.is_alive() and not service._memory_thread.is_alive()
    assert not saved and len(calls) == 1
    assert not service.enqueue_remember(binding, 'late', '늦은 발화', NOW)


@pytest.mark.parametrize('text', ['네.', '응', '안녕하세요!', '고마워요'])
def test_trivial_whole_utterances_never_start_an_extraction_worker(setup, text):
    service, _, _, _ = setup
    assert trivial_utterance(text)
    assert not service.enqueue_remember(service.binding(SESSION), 'trivial', text, NOW)
    assert service._memory_thread is None


@pytest.mark.parametrize('text', ['네, 어제 산책했어', '안녕하세요. 오늘 친구 만났어',
                                 '아니, 내일 약속 취소했어', '피곤해'])
def test_small_talk_prefix_or_short_event_is_not_filtered(text):
    assert not trivial_utterance(text)


def test_idle_only_slows_when_no_summary_or_encounter_needs_delivery(setup):
    service, _, wall, _ = setup
    assert service._poll_delay() == 3
    service.end()
    assert service._poll_delay() == 6
    service.memory.save('owner', 1, 'old', NOW, [SUMMARY])
    assert service._poll_delay() == 3  # Even without a current/confirmed speaker.
    from malbut_agent_server.companion_memory import expires_at
    wall[0] = expires_at(NOW)
    service.memory.purge()
    assert service._poll_delay() == 6
    service._wake.clear()
    service.begin('patrol-chat-' + 'b' * 32)
    assert service._wake.is_set() and service._poll_delay() == 3
    assert service.binding(service.session) is None
    assert [service._poll_delay(n) for n in range(1, 7)] == [3, 6, 12, 24, 30, 30]


def test_stale_cloud_response_cannot_confirm_restarted_same_session(setup):
    service, client, _, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = client.sync

    def sync(report):
        result = original(report)
        entered.set()
        assert release.wait(3)
        return result

    client.sync = sync
    worker = threading.Thread(target=service.sync)
    worker.start()
    try:
        assert entered.wait(1)
        service.end(SESSION)
        service.begin(SESSION)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and service.binding(SESSION) is None
