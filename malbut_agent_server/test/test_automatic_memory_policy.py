"""Delayed automatic effects cannot rewrite or authorize a frozen reply."""

import copy
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from malbut_agent_server.automatic_memory_policy import (
    apply_automatic, automatic_candidate,
)
from malbut_agent_server.conversation import BeginTurnToken
from malbut_agent_server.memory_source_review import attach_source_review
from malbut_agent_server.personal_memory import MemorySnapshot
from malbut_agent_server.schemas import AgentDecision, ValidationError
from test_personal_memory_flow import Flow, origin, pet_fact, proposed


@pytest.fixture
def flow(tmp_path):
    """Keep all delayed writes inside this test's isolated SQLite database."""
    current = Flow(tmp_path / 'automatic-policy.sqlite3')
    current.enable()
    yield current
    current.close()


def freeze(flow, text='우리 강아지 이름은 두부야', facts=None):
    """Commit a normal reply, then reconstruct its private candidate job."""
    frozen = flow.say(text, message='이야기해 주셔서 반가워요.')
    request = flow.requests[-1]
    with flow.conversations._lock:
        conn = flow.conversations._connection
        row = conn.execute(
            '''SELECT * FROM conversation_turns
            WHERE user_id=? AND request_id=?''',
            (request.user_id, request.request_id),
        ).fetchone()
        stamp = conn.execute(
            '''SELECT * FROM memory_turn_state
            WHERE user_id=? AND request_id=?''',
            (request.user_id, request.request_id),
        ).fetchone()
    session = flow.conversations.get('alice', 'room')
    token = BeginTurnToken(
        user_id=request.user_id, conversation_id=request.conversation_id,
        session_instance_id=row['session_instance_id'],
        turn_id=request.turn_id, request_id=request.request_id,
        request_fingerprint=row['request_fingerprint'],
        generation=row['generation'], revision=session.revision - 1,
        ordinal=row['ordinal'],
    )
    source = {
        'user_id': request.user_id,
        'conversation_id': request.conversation_id,
        'session_instance_id': token.session_instance_id,
        'generation': token.generation,
        'turn_id': request.turn_id,
        'request_id': request.request_id,
        'text': text,
    }
    records = flow.memory.list_for_user(request.user_id)
    snapshot = MemorySnapshot(
        state=flow.memory.policy_state(request.user_id), memories=records,
        history=[], summary=None,
        dependencies=set(json.loads(stamp['dependencies_json'])),
        context={'memories': [{'id': item.id} for item in records]},
        pending=None, source=source,
    )
    result = copy.copy(frozen)
    result.provider_result = copy.deepcopy(frozen.provider_result)
    result.provider_result.memory_proposal = proposed(
        'remember', text, facts=[pet_fact(text)] if facts is None else facts,
    )
    return SimpleNamespace(
        request=request, token=token, snapshot=snapshot,
        result=result, frozen=frozen,
    )


def apply(flow, job):
    """Supply the transaction owned by the delayed worker."""
    with flow.conversations._lock:
        conn = flow.conversations._connection
        conn.execute('BEGIN IMMEDIATE')
        try:
            outcome = apply_automatic(
                flow.runtime.personal_memory, job.request, job.token,
                job.snapshot, job.result, conn,
            )
            conn.commit()
            return outcome
        except Exception:
            conn.rollback()
            raise


def frozen_rows(flow):
    """Read reply bytes and stamps without normalizing response JSON."""
    with flow.conversations._lock:
        return [tuple(row) for row in flow.conversations._connection.execute(
            '''SELECT request_id, assistant_content, response_json
            FROM conversation_turns ORDER BY ordinal''',
        ).fetchall()]


@pytest.mark.parametrize('suffix', [
    '. 기억해줘', '. 저장해줘', '. 정정해줘', '. 삭제해줘',
    '. 기억력이 좋아', '. 개인화 켜줘', '. 저장하지 마', '?',
    '. 누구였지', '. 어떤 이름일까',
])
def test_explicit_management_and_questions_are_not_candidates(flow, suffix):
    job = freeze(flow)
    text = job.request.utterance + suffix
    job.request = replace(job.request, utterance=text)
    job.snapshot.source['text'] = text
    job.result.provider_result.memory_proposal = proposed(
        'remember', text, facts=[pet_fact(text)],
    )
    assert not automatic_candidate(job.request, job.snapshot, job.result)


@pytest.mark.parametrize('change', [
    lambda j: j.snapshot.state.update(enabled=False),
    lambda j: setattr(j.snapshot, 'pending', {'kind': 'consent'}),
    lambda j: setattr(j.result, 'decision', AgentDecision(
        type='clarification', message='확인해 주세요.',
    )),
    lambda j: setattr(j.result, 'raw_decision', AgentDecision(
        type='tool_call', message='이동', tool_name='navigate',
    )),
    lambda j: setattr(j.result.provider_result, 'memory_supported', False),
    lambda j: setattr(j.result.provider_result, 'memory_proposal', {}),
    lambda j: j.result.provider_result.memory_proposal.update(
        operation='recall',
    ),
    lambda j: j.result.provider_result.memory_proposal.update(facts=[]),
    lambda j: j.result.provider_result.memory_proposal.update(query='강아지'),
    lambda j: j.result.provider_result.memory_proposal.update(
        target_ids=['x'],
    ),
    lambda j: j.result.provider_result.memory_proposal.update(
        evidence='이전 발화',
    ),
    lambda j: j.snapshot.source.update(user_id='bob'),
    lambda j: j.snapshot.source.update(text='이전 발화'),
])
def test_only_current_nonactuating_automatic_envelopes_are_eligible(
    flow, change,
):
    job = freeze(flow)
    change(job)
    before = frozen_rows(flow)
    assert not automatic_candidate(job.request, job.snapshot, job.result)
    assert apply(flow, job) == {'state': 'discarded', 'memory_ids': []}
    assert flow.memory.list_for_user('alice') == []
    assert frozen_rows(flow) == before


@pytest.mark.parametrize('fields', [
    {'attribute': 'secret'}, {'subject': 'user'},
    {'kind': 'name'}, {'value': '없는 이름'}, {'evidence': '없는 원문'},
])
def test_structurally_unsupported_facts_are_not_enqueued(flow, fields):
    job = freeze(flow)
    job.result.provider_result.memory_proposal['facts'][0].update(fields)
    assert not automatic_candidate(job.request, job.snapshot, job.result)


@pytest.mark.parametrize('text', [
    '친구가 우리 강아지 이름은 두부야 라고 했어',
    '만약 우리 강아지 이름은 두부야 라고 한다면',
    '예시: 우리 강아지 이름은 두부야',
])
def test_noncurrent_or_nondirect_statements_are_not_candidates(flow, text):
    job = freeze(flow, text)
    assert not automatic_candidate(job.request, job.snapshot, job.result)


def test_save_refreshes_origin_stamp_but_never_frozen_reply(flow):
    job = freeze(flow)
    before = frozen_rows(flow)
    message = job.result.decision
    proposal = copy.deepcopy(job.result.provider_result.memory_proposal)
    assert automatic_candidate(job.request, job.snapshot, job.result)
    outcome = apply(flow, job)
    assert outcome['state'] == 'saved'
    stored = flow.memory.list_for_user('alice')
    assert outcome['memory_ids'] == [stored[0].id]
    assert stored[0].metadata['source']['request_id'] == job.request.request_id
    assert frozen_rows(flow) == before
    assert job.result.decision is message
    assert job.result.provider_result.memory_proposal == proposal
    flow.runtime.personal_memory.assert_fresh(
        'alice', job.request.request_id,
    )
    assert job.frozen.to_dict()['decision']['message'] == message.message
    with flow.conversations._lock:
        stamp = flow.conversations._connection.execute(
            '''SELECT revision, dependencies_json FROM memory_turn_state
            WHERE user_id=? AND request_id=?''',
            ('alice', job.request.request_id),
        ).fetchone()
    assert stamp['revision'] == flow.memory.policy_state('alice')['revision']
    assert stored[0].id in json.loads(stamp['dependencies_json'])
    flow.memory.remove_facts('alice', [stored[0].id])
    with pytest.raises(ValidationError, match='memory_changed'):
        flow.runtime.personal_memory.assert_fresh(
            'alice', job.request.request_id,
        )


def test_duplicate_is_saved_without_revision_or_response_change(flow):
    flow.remember()
    job = freeze(flow)
    state = flow.memory.policy_state('alice')
    before = frozen_rows(flow)
    ids = [item.id for item in flow.memory.list_for_user('alice')]
    assert apply(flow, job) == {'state': 'saved', 'memory_ids': ids}
    assert flow.memory.policy_state('alice') == state
    assert frozen_rows(flow) == before


def test_semantic_candidate_requires_exact_review_before_delayed_save(flow):
    text = '난 김민재야'
    fact = {
        'kind': 'name', 'subject': 'user', 'attribute': 'name',
        'value': '김민재', 'evidence': text,
    }
    job = freeze(flow, text, [fact])
    assert automatic_candidate(job.request, job.snapshot, job.result)
    assert apply(flow, job)['state'] == 'discarded'
    attach_source_review(job.snapshot.source, [fact])
    before = frozen_rows(flow)
    assert apply(flow, job)['state'] == 'saved'
    assert frozen_rows(flow) == before


def test_conflict_discards_entire_batch_and_creates_no_question(flow):
    flow.remember()
    previous = flow.memory.list_for_user('alice')
    first, second = '내 이름은 현재야', '우리 강아지 이름은 보리야'
    facts = [
        {'kind': 'name', 'subject': 'user', 'attribute': 'name',
         'value': '현재', 'evidence': first},
        pet_fact(second, '보리'),
    ]
    job = freeze(flow, first + '. ' + second, facts)
    state, before = flow.memory.policy_state('alice'), frozen_rows(flow)
    assert apply(flow, job) == {'state': 'discarded', 'memory_ids': []}
    assert flow.memory.list_for_user('alice') == previous
    assert flow.memory.policy_state('alice') == state
    assert frozen_rows(flow) == before
    assert flow.runtime.personal_memory.pending_question(
        'alice', 'room',
    ) is None


def test_partial_semantic_approval_cannot_save_any_of_the_batch(flow):
    first, second = '난 김민재야', '우리 강아지 이름은 두부야'
    facts = [
        {'kind': 'name', 'subject': 'user', 'attribute': 'name',
         'value': '김민재', 'evidence': first},
        pet_fact(second, subject='고양이'),
    ]
    job = freeze(flow, first + '. ' + second, facts)
    assert automatic_candidate(job.request, job.snapshot, job.result)
    attach_source_review(job.snapshot.source, facts[:1])
    state = flow.memory.policy_state('alice')
    assert apply(flow, job)['state'] == 'discarded'
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice') == state


def test_changed_personalization_discards_frozen_candidate(flow):
    job = freeze(flow)
    flow.memory.set_personalization('alice', False, origin('개인화 꺼줘'))
    before = frozen_rows(flow)
    assert apply(flow, job)['state'] == 'discarded'
    assert flow.memory.list_for_user('alice') == []
    assert frozen_rows(flow) == before


def test_other_memory_change_discards_without_rebasing_origin(flow):
    job = freeze(flow)
    flow.memory.add('alice', '독립적인 기억 변경')
    state = flow.memory.policy_state('alice')
    assert apply(flow, job)['state'] == 'discarded'
    assert flow.memory.policy_state('alice') == state
    with pytest.raises(ValidationError, match='memory_changed'):
        flow.runtime.personal_memory.assert_fresh(
            'alice', job.request.request_id,
        )


def test_pending_memory_question_is_not_consumed_or_replaced(flow):
    job = freeze(flow)
    personal = flow.runtime.personal_memory
    with flow.conversations._lock:
        conn = flow.conversations._connection
        personal._question(
            conn, job.token, 'consent',
            job.result.provider_result.memory_proposal, job.snapshot.source,
        )
        conn.commit()
    pending = personal.pending_question('alice', 'room')
    assert pending is not None
    assert apply(flow, job)['state'] == 'discarded'
    assert personal.pending_question('alice', 'room') == pending
    assert flow.memory.list_for_user('alice') == []


def test_rebound_session_token_cannot_save_into_original_turn(flow):
    job = freeze(flow)
    job.token = replace(job.token, session_instance_id='another-session')
    assert apply(flow, job)['state'] == 'discarded'
    assert flow.memory.list_for_user('alice') == []


def test_missing_origin_stamp_cannot_leave_untracked_delayed_fact(flow):
    job = freeze(flow)
    with flow.conversations._lock:
        flow.conversations._connection.execute(
            'DELETE FROM memory_turn_state WHERE user_id=? AND request_id=?',
            ('alice', job.request.request_id),
        )
        flow.conversations._connection.commit()
    assert apply(flow, job)['state'] == 'discarded'
    assert flow.memory.list_for_user('alice') == []


def test_unexpected_question_is_rolled_back_with_all_effects(
    flow, monkeypatch,
):
    job = freeze(flow)
    personal = flow.runtime.personal_memory
    original = personal._apply
    state = flow.memory.policy_state('alice')

    def apply_with_question(request, token, snapshot, result, conn, **kwargs):
        decision, added = original(
            request, token, snapshot, result, conn, **kwargs,
        )
        personal._question(
            conn, token, 'conflict', result.provider_result.memory_proposal,
            snapshot.source,
        )
        return decision, added

    monkeypatch.setattr(personal, '_apply', apply_with_question)
    assert apply(flow, job)['state'] == 'discarded'
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice') == state
    assert personal.pending_question('alice', 'room') is None


def test_database_failure_rolls_back_effects_and_preserves_response(
    flow, monkeypatch,
):
    job = freeze(flow)
    personal = flow.runtime.personal_memory
    original = personal._apply
    before, state = frozen_rows(flow), flow.memory.policy_state('alice')

    def fail_after_effect(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError('injected delayed failure')

    monkeypatch.setattr(personal, '_apply', fail_after_effect)
    with pytest.raises(sqlite3.OperationalError, match='delayed failure'):
        apply(flow, job)
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice') == state
    assert frozen_rows(flow) == before


def test_apply_requires_caller_transaction(flow):
    job = freeze(flow)
    with pytest.raises(ValidationError, match='write transaction'):
        apply_automatic(
            flow.runtime.personal_memory, job.request, job.token,
            job.snapshot, job.result, flow.conversations._connection,
        )
