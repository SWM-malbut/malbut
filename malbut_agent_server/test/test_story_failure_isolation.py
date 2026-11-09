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


def test_revoked_topic_match_cannot_suppress_conservative_fallback(runtime):
    pottery = save(runtime, '은하 공방은 오후 4시야.', '은하 공방')
    hiking = save(runtime, '솔봉 등산은 오전 8시야.', '솔봉 등산')
    exhaust(runtime, '은하 공방은 오후 6시로 변경했어.', room='new')
    assert runtime.store.failed_story_ids('alice') == {pottery}

    conn = runtime.conversations._connection
    with conn:
        conn.execute('UPDATE conversation_turns SET user_content=? WHERE user_id=? AND user_content=?',
                     ('원문 정정', 'alice', '은하 공방은 오후 4시야.'))
    assert runtime.store.failed_story_ids('alice') == {hiking}


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


@pytest.mark.parametrize('question,answer,claim', [
    ('내 이름이 뭐였지?', '나예솔님이에요.', False),
    ('저장해 둔 내 호칭이 뭐였지?', '저장된 호칭은 모래별이에요.', True),
])
def test_unrelated_fact_question_does_not_hide_story_after_reopen(runtime, question, answer, claim):
    notebook = save(runtime, '내 순찰 수첩 표지색은 청록색이야.', '순찰 수첩')
    token = runtime.complete(question, room='chat', answer=answer)
    assert runtime.store.enqueue_completed('alice', token.request_id)
    if claim:
        assert runtime.store.claim('alice') is not None
    runtime.reopen()
    current = service(runtime)
    try:
        result = current.context('alice', '순찰 수첩 표지색의 정확한 원문은?')
        assert result['pending'] == 1
        assert [story['story_id'] for story in result['stories']] == [notebook]
        assert any('청록색' in item['text'] for item in result['evidence'])
        assert current.validate_readset('alice', result['revision'], result['readset'])
    finally:
        current.close()


def test_pending_fact_answer_holds_only_its_story_candidates(runtime):
    save(runtime, '순찰 수첩 표지색은 청록색이야.', '순찰 수첩')
    pottery = save(runtime, '은하 공방 시간은 오후 네 시야.', '은하 공방')
    token = runtime.complete('내 이름이 뭐였지?', room='chat',
                             answer='순찰 수첩에 쓴 이름은 나예솔이에요.')
    assert runtime.store.enqueue_completed('alice', token.request_id)
    current = service(runtime)
    try:
        notebook = current.context('alice', '순찰 수첩 원문')
        assert notebook['stories'] == notebook['evidence'] == notebook['readset'] == []
        other = current.context('alice', '은하 공방 원문')
        assert [story['story_id'] for story in other['stories']] == [pottery]
    finally:
        current.close()


@pytest.mark.parametrize('text', [
    '내 이름이 뭐였지? 그리고 순찰 수첩 표지색은 빨간색으로 바꿨어.',
    '그때 말했던 색은 그걸로 바꿨어.',
    '빨강으로 바꾼 표지에는 점이 몇 개야?',
    '내 이름이 뭐였지? 순찰 수첩 이야기는 지워 줘.',
])
def test_pending_correction_after_read_fences_other_replies(runtime, text):
    save(runtime, '순찰 수첩 표지색은 청록색이야.', '순찰 수첩')
    runtime.allow('bob')
    bob_token = runtime.complete(text, room='chat', user='bob', answer='알겠어요.')
    assert runtime.store.enqueue_completed('bob', bob_token.request_id)
    current = service(runtime)
    try:
        before = current.context('alice', '순찰 수첩 원문')
        assert before['readset'] and current.validate_readset(
            'alice', before['revision'], before['readset'])
        token = runtime.complete(text, room='chat', answer='알겠어요.')
        assert runtime.store.enqueue_completed('alice', token.request_id)
        assert not current.validate_readset('alice', before['revision'], before['readset'],
                                            request_id='other-reply')
        assert current.validate_readset('alice', before['revision'], before['readset'],
                                        request_id=token.request_id)
        after = current.context('alice', '순찰 수첩 원문')
        assert after['stories'] == after['evidence'] == after['readset'] == []
    finally:
        current.close()


def queue_patrol(runtime, utterance, *, tool='request_patrol', allowed=True, legacy=False):
    token = runtime.begin(utterance, room='other-room').token
    answer = 'Manager에 작업 실행을 요청할게요.'
    public = {'decision': {'type': 'tool_call', 'tool_name': tool, 'message': answer},
              'safety': {'allowed': allowed, 'code': 'manager_request' if allowed else 'denied'}}
    response = public if legacy else {'schema_version': 2, 'public': public}
    runtime.conversations.complete_turn(token, answer, response)
    assert runtime.store.enqueue_completed('alice', token.request_id)


@pytest.mark.parametrize('claim', [False, True])
def test_pending_drink_question_and_home_patrol_leave_exhibition_available(runtime, claim):
    exhibition = save(runtime, '순찰 로봇 전시회는 금요일에 가기로 했어.', '순찰 전시')
    patrol = save(runtime, '집 안을 가볍게 둘러보는 건 오후에 하기로 했어.', '집 안 둘러보기')
    manager = save(runtime, '외출 준비는 Manager에 요청하기로 했어.', '외출 준비')
    drink = runtime.complete('내가 좋아하는 음료가 뭐였더라?', answer='좋아하는 음료는 오미자차예요.')
    assert runtime.store.enqueue_completed('alice', drink.request_id)
    queue_patrol(runtime, '집 안을 가볍게 한 바퀴 둘러봐 줘.')
    if claim:
        job = runtime.store.claim('alice', batch_size=2)
        assert len(job['batch_job_ids']) == 2
        assert {s['story_id'] for s in job['stories']} == {patrol, manager}
    runtime.reopen()
    current = service(runtime)
    try:
        assert runtime.store.pending_story_ids('alice') == {patrol, manager}
        result = current.context('alice', '내가 순찰 로봇 전시회에 무슨 요일에 가기로 했었지?')
        assert result['pending'] == 2
        assert [s['story_id'] for s in result['stories']] == [exhibition]
        assert current.validate_readset('alice', result['revision'], result['readset'])
        correction = runtime.complete('그 전시는 토요일로 바꿨어.', room='third-room')
        assert runtime.store.enqueue_completed('alice', correction.request_id)
        assert runtime.store.pending_story_ids('alice') is None
        assert not current.validate_readset('alice', result['revision'], result['readset'])
    finally:
        current.close()


@pytest.mark.parametrize('text,tool,allowed,legacy,scoped', [
    ('집을 한 바퀴 순찰해 줘.', 'request_patrol', True, False, True),
    ('별일 없는지 집을 구석구석 꼼꼼히 순찰해 줘.', 'request_patrol', True, False, True),
    ('이제 집 안 순찰을 시작해 줘.', 'request_patrol', True, False, True),
    ('지금은 간단히 집 안을 한 번 순찰해 줘.', 'request_patrol', True, False, True),
    ('순찰해 주세요.', 'request_patrol', True, False, True),
    ('집 안을 한 바퀴 둘러봐 줘.', 'request_patrol', True, False, True),
    ('둘러봐 줘.', 'request_patrol', True, False, False),
    ('집 안 순찰해 줘.', 'request_navigation', True, False, False),
    ('집 안 순찰해 줘.', 'request_patrol', False, False, False),
    ('집 안 순찰해 줘.', 'request_patrol', True, True, False),
    ('그날은 토요일이야 집 안을 한 바퀴 둘러봐 줘.', 'request_patrol', True, False, False),
    ('집 안 순찰해 줘 전시는 토요일로 바꿨어.', 'request_patrol', True, False, False),
    ('집 안 순찰할 때 작은방을 빼 줘.', 'request_patrol', True, False, False),
    ('집 안 순찰해 줘 이름은 모래별이야.', 'request_patrol', True, False, False),
])
def test_pending_patrol_needs_standalone_request_and_approved_receipt(
        runtime, text, tool, allowed, legacy, scoped):
    save(runtime, '도예 예약은 금요일이야.', '도예 예약')
    current = service(runtime)
    try:
        before = current.context('alice', '도예 예약')
        assert before['readset']
        queue_patrol(runtime, text, tool=tool, allowed=allowed, legacy=legacy)
        assert runtime.store.pending_story_ids('alice') == (set() if scoped else None)
        assert current.validate_readset('alice', before['revision'], before['readset']) is scoped
    finally:
        current.close()
