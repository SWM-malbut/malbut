"""Exhaust real durable jobs; unrelated recall survives without stale corrections."""

import pytest

from malbut_agent_server.story_memory_service import StoryMemoryService
from test_story_runtime_store import RuntimeHarness


@pytest.fixture
def runtime(tmp_path):
    harness = RuntimeHarness(tmp_path / 'failures.sqlite3')
    harness.allow()
    yield harness
    harness.close()


def save(runtime, text, title, room='seed'):
    _, job = runtime.claim_turn(text, room=room, answer='알겠어요.')
    update = runtime.update(job, title=title, text=text)
    update['aliases'] = []
    assert runtime.store.commit(job, [update])
    return next(s['story_id'] for s in runtime.store.list_stories('alice') if s['title'] == title)


def exhaust(runtime, text, room='failure', answer='알 수 없어요.', user='alice'):
    token, job = runtime.claim_turn(text, room=room, answer=answer, user=user)
    for attempt in range(3):
        if attempt:
            runtime.now += 10
            job = runtime.store.claim(user)
        assert job is not None
        runtime.store.fail(job, RuntimeError('synthetic extraction failure'))
    assert runtime.store.stats(user)['failed'] >= 1
    return token


def service(runtime):
    return StoryMemoryService(runtime.conversations, runtime.memories, None,
                              runtime_store=runtime.store, autostart=False)


def test_unrelated_failure_with_shared_category_survives_restart(runtime):
    pottery = save(runtime, '은하 공방 수업은 금요일 오후 4시야.', '은하 공방 수업')
    exhaust(runtime, '민트 요가 수업은 언제 신청했다고 했지?',
            answer='은하 공방 수업은 알지만 민트 요가 정보는 없어요.')
    runtime.reopen()
    current = service(runtime)
    try:
        context = current.context('alice', '은하 공방 예약의 정확한 원문은?')
        assert [s['story_id'] for s in context['stories']] == [pottery]
        assert context['evidence'] and context['failed'] == 1
        assert context['status'] == 'partial_update_failed'
        assert current.validate_readset('alice', context['revision'], context['readset'])
        assert not current.context('bob', '은하 공방')['stories']
        current.disable('alice')
        assert not current.context('alice', '은하 공방')['stories']
    finally:
        current.close()


def test_failed_correction_hides_only_target_and_invalidates_earlier_readset(runtime):
    pottery = save(runtime, '은하 공방은 금요일 오후 4시야.', '은하 공방')
    hiking = save(runtime, '솔봉 등산 모임은 토요일 오전 8시야.', '솔봉 등산 모임')
    current = service(runtime)
    try:
        before = current.context('alice', '은하 공방')
        other = current.context('alice', '솔봉 등산 모임')
        exhaust(runtime, '은하 공방 시간을 오후 6시로 정정할게.', room='seed')
        assert runtime.store.failed_story_ids('alice') == {pottery}
        assert not current.context('alice', '은하 공방 원문')['stories']
        assert not current.context('alice', '은하 공방 원문')['evidence']
        assert [s['story_id'] for s in current.context('alice', '솔봉 등산 모임')['stories']] == [hiking]
        assert not current.validate_readset('alice', before['revision'], before['readset'])
        assert current.validate_readset('alice', other['revision'], other['readset'])
        # Explicitly retry and commit the real correction; quarantine lifts.
        assert runtime.store.retry_failed('alice') == 1
        job = runtime.store.claim('alice')
        update = runtime.update(job, title='은하 공방', text='은하 공방은 오후 6시야.', story_id=pottery)
        assert runtime.store.commit(job, [update])
        assert not runtime.store.failed_story_ids('alice')
        assert '6시' in current.context('alice', '은하 공방')['stories'][0]['current'][0]['text']
    finally:
        current.close()


@pytest.mark.parametrize('correction', [
    '그거 오후 6시로 바꿨어.', '오후 6시로 바꿨어.', '오후 6시로 바꾸기로 했어.',
    '오후 6시로 바뀐 거야.',
])
def test_deictic_correction_uses_its_session_not_other_stories(runtime, correction):
    pottery = save(runtime, '은하 공방 수업은 오후 4시야.', '은하 공방', room='pottery')
    save(runtime, '솔봉 등산은 오전 8시야.', '솔봉 등산', room='hiking')
    exhaust(runtime, correction, room='pottery')
    assert runtime.store.failed_story_ids('alice') == {pottery}


def test_unresolvable_correction_remains_conservative(runtime):
    pottery = save(runtime, '은하 공방 수업은 오후 4시야.', '은하 공방')
    hiking = save(runtime, '솔봉 등산은 오전 8시야.', '솔봉 등산')
    exhaust(runtime, '그 약속은 취소했어.', room='new-room')
    assert runtime.store.failed_story_ids('alice') == {pottery, hiking}


def test_failed_batch_keeps_individual_turn_scopes(runtime):
    pottery = save(runtime, '은하 공방 수업은 오후 4시야.', '은하 공방')
    hiking = save(runtime, '솔봉 등산은 오전 8시야.', '솔봉 등산')
    for text in ['은하 공방은 오후 6시로 변경했어.', '민트 요가 신청 시점은?']:
        token = runtime.complete(text, room='new-room', answer='알겠어요.')
        runtime.store.enqueue_completed('alice', token.request_id)
    for _ in range(3):
        runtime.now += 10
        job = runtime.store.claim('alice', batch_size=3)
        assert len(job['batch_job_ids']) == 2
        runtime.store.fail(job, RuntimeError('batch failure'))
    assert runtime.store.failed_story_ids('alice') == {pottery}
    assert [s['story_id'] for s in runtime.store.candidates(
        'alice', '솔봉 등산', exclude_ids={pottery})] == [hiking]


def test_failure_during_context_assembly_removes_claim_and_evidence(runtime, monkeypatch):
    save(runtime, '은하 공방은 오후 4시야.', '은하 공방')
    current = service(runtime)
    original = runtime.store.evidence
    def fail_while_reading(user, identifier):
        evidence = original(user, identifier)
        exhaust(runtime, '은하 공방은 오후 6시로 정정했어.')
        return evidence
    monkeypatch.setattr(runtime.store, 'evidence', fail_while_reading)
    try:
        context = current.context('alice', '은하 공방 원문은?')
        assert context['stories'] == context['evidence'] == context['readset'] == []
    finally:
        current.close()


def test_comparison_includes_sources_when_change_was_only_saved_as_episode(runtime):
    identifier = save(runtime, '푸른정원 전시 첫 방문은 혼잡해서 지쳤어.', '푸른정원 전시')
    _, job = runtime.claim_turn('푸른정원 전시 두 번째 방문은 한산해서 편안했어.',
                                room='second-visit', answer='편안했군요.')
    update = runtime.update(job, title='푸른정원 전시', story_id=identifier,
                            text='두 번째 방문은 한산해서 편안했다.')
    prior = next(s for s in job['stories'] if s['story_id'] == identifier)
    update['current'] = prior['current']
    assert runtime.store.commit(job, [update])
    current = service(runtime)
    try:
        context = current.context('alice', '푸른정원 전시의 첫 방문과 두 번째 방문을 비교해줘.')
        assert '두 번째' not in str(context['stories'][0]['current'])
        evidence = ' '.join(item['text'] for item in context['evidence'])
        assert '혼잡해서 지쳤어' in evidence and '한산해서 편안했어' in evidence
    finally:
        current.close()
