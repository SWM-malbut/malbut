"""Existing fact controls must not reintroduce revoked sources as stories."""

import json

import pytest

from malbut_agent_server.personal_memory import PersonalMemory
from malbut_agent_server.story_runtime_store import StoryRuntimeStore
from test_personal_memory_flow import Flow
from test_story_runtime_store import RuntimeHarness


@pytest.fixture
def runtime(tmp_path):
    current = RuntimeHarness(tmp_path / 'story-facts.sqlite3')
    yield current
    current.close()


def fact_source(runtime, token):
    return {
        key: getattr(token, key) for key in (
            'conversation_id', 'session_instance_id', 'generation',
            'turn_id', 'request_id',
        )
    } | {'text': runtime.row(token)['user_content']}


def save_fact(runtime, token, value='전시'):
    source = fact_source(runtime, token)
    runtime.memories.set_personalization(token.user_id, True, source)
    return runtime.memories.upsert_fact(token.user_id, {
        'kind': 'preference', 'subject': 'user', 'attribute': 'likes',
        'value': value, 'evidence': source['text'],
    }, source)['records'][0]


def test_fact_deletion_hides_story_and_rejects_completed_job_replay(runtime):
    runtime.allow()
    token, job = runtime.claim_turn('나는 전시를 좋아해.')
    update = runtime.update(job)
    assert runtime.store.commit(job, [update])
    story = runtime.store.list_stories('alice')[0]
    fact = save_fact(runtime, token)
    original = runtime.row(token)
    assert runtime.store.commit(job, [update])

    runtime.memories.remove_facts('alice', [fact.id])

    assert runtime.store.list_stories('alice') == []
    assert runtime.store.candidates('alice', '전시') == []
    assert runtime.store.evidence('alice', story['story_id']) == []
    assert runtime.store.job_source(job) is None
    assert runtime.store.commit(job, [update]) is False
    assert runtime.row(token) == original


@pytest.mark.parametrize('read_existing_story', [False, True])
def test_fact_deletion_fences_inflight_source_or_candidate(runtime, read_existing_story):
    runtime.allow()
    if read_existing_story:
        token, _done, _story = runtime.saved('나는 전시를 좋아해.')
        fact = save_fact(runtime, token)
        _new_token, job = runtime.claim_turn('전시 안내를 다시 논의하자.')
        assert job['stories']
    else:
        token, job = runtime.claim_turn('나는 전시를 좋아해.')
        fact = save_fact(runtime, token)
    update = runtime.update(job)

    runtime.memories.remove_facts('alice', [fact.id])

    assert runtime.store.job_source(job) is None
    assert runtime.store.commit(job, [update]) is False


def test_fact_correction_blocks_old_source_but_accepts_corrected_turn(runtime):
    runtime.allow()
    token, _job, old_story = runtime.saved('나는 전시를 좋아해.')
    fact = save_fact(runtime, token)
    corrected = runtime.complete('정확히는 전시보다 정원을 좋아해.', room='new-room')
    source = fact_source(runtime, corrected)
    changed = runtime.memories.upsert_fact('alice', {
        'kind': 'preference', 'subject': 'user', 'attribute': 'likes',
        'value': '정원', 'evidence': source['text'],
    }, source, correct_ids=[fact.id])
    assert changed['invalidated_ids'] == [fact.id]
    assert runtime.store.evidence('alice', old_story['story_id']) == []
    assert runtime.store.enqueue_completed('alice', corrected.request_id)
    job = runtime.store.claim('alice')
    assert job is not None
    assert runtime.store.commit(job, [runtime.update(job, title='정원', text='정원을 좋아한다.')])
    assert runtime.store.candidates('alice', '정원')
    assert runtime.row(token)['user_content'] == '나는 전시를 좋아해.'


@pytest.mark.parametrize('already_queued', [False, True])
def test_recovery_does_not_requeue_revoked_unprocessed_source(runtime, already_queued):
    runtime.allow()
    token = runtime.complete('나는 전시를 좋아해.')
    fact = save_fact(runtime, token)
    if already_queued:
        assert runtime.store.enqueue_completed('alice', token.request_id)
    runtime.memories.remove_facts('alice', [fact.id])

    for _ in range(3):
        assert runtime.store.recover('alice') == 0
        assert runtime.store.claim('alice') is None
        stats = runtime.store.stats('alice')
        assert (stats['queued'], stats['running'], stats['failed']) == (0, 0, 0)
    assert runtime.row(token)['user_content'] == '나는 전시를 좋아해.'


def test_revoked_batch_member_releases_unrelated_work_without_lease_wait(runtime):
    runtime.allow()
    revoked = runtime.complete('나는 전시를 좋아해.')
    fact = save_fact(runtime, revoked)
    retained = runtime.complete('정원의 해바라기를 봤어.', room='garden')
    for token in (revoked, retained):
        assert runtime.store.enqueue_completed('alice', token.request_id)
    old_job = runtime.store.claim('alice', batch_size=2)
    assert len(old_job['batch_job_ids']) == 2

    runtime.memories.remove_facts('alice', [fact.id])
    runtime.store.recover('alice')

    assert runtime.store.job_source(old_job) is None
    assert runtime.store.commit(old_job, []) is False
    fresh = runtime.store.claim('alice', batch_size=2)
    assert fresh is not None
    assert fresh['request_id'] == retained.request_id
    assert len(fresh['batch_job_ids']) == 1
    assert runtime.store.commit(fresh, [runtime.update(fresh, title='정원', text='해바라기를 보았다.')])
    assert runtime.store.recover('alice') == 0


@pytest.mark.parametrize('enabled', [False, True])
def test_revoked_unprocessed_fact_does_not_block_unrelated_story_deletion(runtime, enabled):
    runtime.allow()
    _token, _job, story = runtime.saved()
    revoked = runtime.complete('나는 정원을 좋아해.', room='garden')
    fact = save_fact(runtime, revoked, '정원')
    assert runtime.store.enqueue_completed('alice', revoked.request_id)
    runtime.memories.remove_facts('alice', [fact.id])
    if not enabled:
        runtime.store.set_enabled('alice', False)

    assert runtime.store.delete_story('alice', story['story_id'], require_settled=True)['deleted']
    assert runtime.row(revoked)['user_content'] == '나는 정원을 좋아해.'


def test_fact_tombstone_blocks_marked_paraphrase_but_not_other_user_or_session(runtime):
    personal = PersonalMemory(runtime.memories, runtime.conversations)
    runtime.allow()
    token, _job, _story = runtime.saved('나는 전시를 좋아해.')
    fact = save_fact(runtime, token)
    repeated, job = runtime.claim_turn('그 이야기를 이어가자.', answer='그 관람을 즐기셨군요.')
    assistant = runtime.source(job, role='assistant', containing='관람')
    assert runtime.store.commit(job, [runtime.update(
        job, title='관람', text=assistant['text'], source=assistant, actor='assistant',
    )])
    separate, job = runtime.claim_turn('정원에서 해바라기를 봤어.', room='garden')
    assert runtime.store.commit(job, [runtime.update(job, title='정원', text='해바라기를 보았다.')])
    runtime.allow('bob')
    bob, _job, bob_story = runtime.saved('나는 전시를 좋아해.', user='bob')
    originals = [runtime.row(item) for item in (token, repeated, separate, bob)]

    conn = runtime.conversations._connection
    with conn:
        personal._mark_prior_sources(conn, 'alice', [fact])
        runtime.memories.remove_facts('alice', [fact.id], connection=conn)

    assert [item['title'] for item in runtime.store.list_stories('alice')] == ['정원']
    assert runtime.store.evidence('bob', bob_story['story_id'])
    assert [runtime.row(item) for item in (token, repeated, separate, bob)] == originals


@pytest.mark.parametrize('field', ['conversation_id', 'session_instance_id', 'generation',
                                  'turn_id', 'request_id'])
def test_source_tombstone_requires_complete_identity_match(runtime, field):
    runtime.allow()
    token, _job, story = runtime.saved()
    key = fact_source(runtime, token)
    key.pop('text')
    key[field] = key[field] + 1 if field == 'generation' else key[field] + '-other'
    conn = runtime.conversations._connection
    with conn:
        conn.execute('INSERT INTO memory_tombstones VALUES (?,?,?,?)',
                     ('alice', 'different-source', json.dumps(key), runtime.now))
    assert runtime.store.evidence('alice', story['story_id'])


def test_story_disable_reply_marker_does_not_delete_retained_story(runtime):
    PersonalMemory(runtime.memories, runtime.conversations)
    runtime.allow()
    token, _job, story = runtime.saved()
    conn = runtime.conversations._connection
    with conn:
        conn.execute('INSERT INTO memory_turn_state VALUES (?,?,?,?,?,?,?,?,?)', (
            'alice', token.request_id, token.conversation_id, token.session_instance_id,
            token.generation, token.turn_id, 0, '[]', runtime.now,
        ))
        runtime.store._mark_reply_stale(conn, 'alice', token.request_id)
    assert runtime.store.evidence('alice', story['story_id'])


@pytest.mark.parametrize('story_enabled', [None, False, True])
def test_fact_disable_reports_separate_story_setting_without_changing_consent(tmp_path, story_enabled):
    flow = Flow(tmp_path / 'control.sqlite3')
    try:
        flow.enable()
        stories = None
        if story_enabled is not None:
            stories = StoryRuntimeStore(flow.conversations, flow.memory)
            stories.set_enabled('alice', story_enabled, external_consent=story_enabled)
            before = stories.policy('alice')
        result = flow.say('기억 사용 중단해줘')
        assert flow.memory.policy_state('alice')['enabled'] is False
        assert '개인 사실 기억' in result.decision.message
        assert ('이야기 기억 설정은 계속 켜져' in result.decision.message) is (story_enabled is True)
        if stories is not None:
            assert stories.policy('alice') == before
    finally:
        flow.close()
