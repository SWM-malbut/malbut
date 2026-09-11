"""Delayed inserts preserve live answers without weakening erasure lineage."""

import json
import sqlite3
import time

import pytest

from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.schemas import ValidationError
from test_automatic_memory_policy import apply, freeze, frozen_rows
from test_memory_policy import fact, source
from test_personal_memory_flow import Flow, pet_fact, proposed


@pytest.fixture
def flow(tmp_path):
    """Keep storage and conversation provenance in one isolated database."""
    current = Flow(tmp_path / 'automatic-lineage.sqlite3')
    current.enable()
    yield current
    current.close()


def test_delayed_insert_preserves_current_answers_and_advances_global(flow):
    job = freeze(flow)
    current = flow.say('그 아이 이야기를 더 하자', message='귀여운 친구겠네요.')
    current_request = flow.requests[-1]
    state = flow.memory.policy_state('alice')
    revision = flow.memory.revision
    before = frozen_rows(flow)
    assert apply(flow, job)['state'] == 'saved'
    assert flow.memory.policy_state('alice') == state
    assert flow.memory.revision == revision + 1
    assert frozen_rows(flow) == before
    assert flow.runtime.handle(current_request).to_dict() == current.to_dict()
    flow.runtime.personal_memory.assert_fresh('alice', job.request.request_id)


def test_two_delayed_inserts_can_share_the_same_invalidation_epoch(flow):
    first = freeze(flow)
    text = '우리 고양이 이름은 나비야'
    second = freeze(flow, text, [pet_fact(text, '나비', '고양이')])
    state = flow.memory.policy_state('alice')
    revision = flow.memory.revision
    assert apply(flow, first)['state'] == 'saved'
    assert apply(flow, second)['state'] == 'saved'
    assert flow.memory.policy_state('alice') == state
    assert flow.memory.revision == revision + 2
    assert len(flow.memory.list_for_user('alice')) == 2


@pytest.mark.parametrize('operation', ['add', 'upsert', 'barrier'])
def test_default_writes_and_explicit_barrier_still_invalidate(flow, operation):
    response = flow.say('오늘은 좋은 하루야')
    request = flow.requests[-1]
    state = flow.memory.policy_state('alice')
    revision = flow.memory.revision
    if operation == 'add':
        flow.memory.add('alice', '검증된 진단 정보')
    elif operation == 'upsert':
        flow.memory.upsert_fact('alice', fact(), source())
    else:
        changed = flow.memory.invalidate_answers('alice')
        assert changed == flow.memory.policy_state('alice')
    assert flow.memory.policy_state('alice')['revision'] == (
        state['revision'] + 1
    )
    assert flow.memory.revision == revision + 1
    with pytest.raises(ValidationError, match='memory_changed'):
        flow.runtime.personal_memory.assert_fresh('alice', request.request_id)
    assert response.decision.message == '대화 답변이에요.'


def test_invalidation_barrier_is_atomic_and_visible_to_other_instances(flow):
    other = SQLiteMemoryStore(flow.path)
    conn = sqlite3.connect(flow.path)
    conn.row_factory = sqlite3.Row
    state = other.policy_state('alice')
    revision = other.revision
    try:
        conn.execute('BEGIN IMMEDIATE')
        changed = flow.memory.invalidate_answers('alice', connection=conn)
        assert changed['revision'] == state['revision'] + 1
        assert other.policy_state('alice') == state
        conn.rollback()
        assert other.policy_state('alice') == state
        assert other.revision == revision
        conn.execute('BEGIN IMMEDIATE')
        flow.memory.invalidate_answers('alice', connection=conn)
        conn.commit()
        assert other.policy_state('alice') == changed
        assert other.revision == revision + 1
        assert other.list_for_user('alice') == []
    finally:
        conn.close()
        other.close()


def test_inflight_descendant_merges_late_lineage_from_another_runtime(flow):
    job = freeze(flow)
    other = Flow(flow.path)
    try:
        def complete_older_save():
            assert apply(other, job)['state'] == 'saved'

        response = flow.say(
            '그 아이 이야기를 계속하자', callback=complete_older_save,
            message='귀여운 친구겠네요.',
        )
        request = flow.requests[-1]
        assert flow.provider.calls[-1]['memories'] == []
        record = other.memory.list_for_user('alice')[0]
        row = other.conversations._connection.execute(
            '''SELECT dependencies_json FROM memory_turn_state
            WHERE user_id=? AND request_id=?''',
            ('alice', request.request_id),
        ).fetchone()
        assert record.id in json.loads(row[0])
        assert flow.runtime.handle(request).to_dict() == response.to_dict()
        other.memory.remove_facts('alice', [record.id])
        flow.say('다른 이야기를 하자')
        previous = next(turn for turn in flow.provider.calls[-1]['history']
                        if turn.request_id == request.request_id)
        assert previous.user_content == previous.assistant_content == ''
    finally:
        other.close()


def test_delayed_backfill_never_crosses_user_session_or_origin_boundary(flow):
    job = freeze(flow)
    flow.say('이 방에서는 다른 이야기를 하자', conversation='other-room')
    other_room = flow.requests[-1]
    flow.say('별도 사용자의 대화야', user='bob', conversation='room')
    other_user = flow.requests[-1]
    assert apply(flow, job)['state'] == 'saved'
    record = flow.memory.list_for_user('alice')[0]
    conn = flow.conversations._connection
    for request in (flow.requests[0], other_room, other_user):
        row = conn.execute(
            '''SELECT dependencies_json FROM memory_turn_state
            WHERE user_id=? AND request_id=?''',
            (request.user_id, request.request_id),
        ).fetchone()
        assert record.id not in json.loads(row[0])


@pytest.mark.parametrize('operation', ['remove', 'expire', 'user_delete'])
def test_late_origin_erasure_redacts_descendants_but_not_new_dialogue(
    flow, operation,
):
    job = freeze(flow)
    descendant = flow.say(
        '그 아이 이야기를 더 하자', message='귀여운 친구겠네요.',
    )
    request = flow.requests[-1]
    conn = flow.conversations._connection
    stamp = conn.execute(
        '''SELECT dependencies_json FROM memory_turn_state
        WHERE user_id=? AND request_id=?''',
        ('alice', request.request_id),
    ).fetchone()
    assert json.loads(stamp[0]) == []
    assert apply(flow, job)['state'] == 'saved'
    record = flow.memory.list_for_user('alice')[0]
    before = frozen_rows(flow)
    if operation == 'user_delete':
        text = '우리 강아지 이름을 삭제해줘'
        flow.say(text, proposed('forget', text, targets=[record.id]))
    else:
        other = SQLiteMemoryStore(flow.path)
        try:
            if operation == 'remove':
                other.remove_facts('alice', [record.id])
            else:
                with other._transaction(write=True) as active:
                    active.execute(
                        'UPDATE memories SET expires_at=? WHERE id=?',
                        (time.time() - 1, record.id),
                    )
                assert other.purge_expired() == 1
        finally:
            other.close()
        assert frozen_rows(flow) == before
    assert flow.memory.list_for_user('alice') == []
    flow.now = time.time() + 1
    flow.say('새로운 주제로 이야기하자', message='새 대화가 시작됐어요.')
    supplied = flow.provider.calls[-1]['history']
    assert any(turn.turn_id == request.turn_id for turn in supplied)
    for turn in supplied:
        if turn.turn_id in {job.request.turn_id, request.turn_id}:
            assert turn.user_content == turn.assistant_content == ''
    assert all(turn.assistant_content != descendant.decision.message
               for turn in supplied)
    flow.say('이 새 주제를 계속하자', message='새로운 이야기를 이어가요.')
    assert any(turn.assistant_content == '새 대화가 시작됐어요.'
               for turn in flow.provider.calls[-1]['history'])


def test_automatic_mode_cannot_correct_or_approve_consent(flow):
    record = flow.memory.upsert_fact('alice', fact(), source())['records'][0]
    state = flow.memory.policy_state('alice')
    revision = flow.memory.revision
    with pytest.raises(ValidationError, match='cannot correct'):
        flow.memory.upsert_fact(
            'alice', fact('민수'), source('내 이름은 민수야', 'second'),
            correct_ids=[record.id], _automatic_insert=True,
        )
    job = freeze(flow)
    job.snapshot.pending = {'kind': 'consent'}
    conn = flow.conversations._connection
    conn.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(ValidationError, match='cannot manage'):
            flow.runtime.personal_memory._apply(
                job.request, job.token, job.snapshot, job.result, conn,
                _automatic_insert=True,
            )
    finally:
        conn.rollback()
    assert flow.memory.policy_state('alice') == state
    assert flow.memory.revision == revision
    assert [item.id for item in flow.memory.list_for_user('alice')] == [
        record.id,
    ]


@pytest.mark.parametrize('mode', [None, 1, 'true'])
def test_automatic_mode_requires_an_explicit_internal_boolean(flow, mode):
    with pytest.raises(ValidationError, match='must be boolean'):
        flow.memory.upsert_fact(
            'alice', fact(), source(), _automatic_insert=mode,
        )
    with pytest.raises(ValidationError, match='must be boolean'):
        flow.memory.add('alice', '정보', _automatic_insert=mode)
    assert flow.memory.list_for_user('alice') == []
