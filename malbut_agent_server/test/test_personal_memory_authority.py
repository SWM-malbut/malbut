"""Adversarial memory authority and stale-context regression scenarios."""

import json

import pytest

from test_personal_memory_flow import (
    Flow, pet_fact, proposed, supplied_text,
)


@pytest.fixture
def flow(tmp_path):
    """Exercise public Agent turns in an isolated durable SQLite file."""
    current = Flow(tmp_path / 'authority.sqlite3')
    yield current
    current.close()


def _records(flow):
    return [(item.id, item.content)
            for item in flow.memory.list_for_user('alice')]


@pytest.mark.parametrize('operation', ['forget', 'correct'])
def test_forged_mutation_cannot_replace_a_recall_request(flow, operation):
    """A genuine memory query is not permission to delete or correct it."""
    flow.enable()
    flow.remember()
    record = flow.memory.list_for_user('alice')[0]
    before = _records(flow)
    text = '강아지 이름 기억을 보여줘. 보리였나?'
    proposal = proposed(
        operation, text, targets=[record.id], query='강아지',
        facts=[pet_fact(text, '보리')] if operation == 'correct' else (),
    )
    result = flow.say(text, proposal, message='기억을 변경했어요.')
    assert _records(flow) == before
    assert '기억을 변경했어요.' != result.decision.message


@pytest.mark.parametrize(('text', 'operation'), [
    ('강아지 이름 기억을 삭제하지 마', 'forget'),
    ('강아지 이름 기억은 지우지 말아줘', 'forget'),
    ('개인화를 중단하지 마', 'disable'),
])
def test_negated_management_cannot_mutate_records_or_consent(
    flow, text, operation,
):
    """Negative instructions retain both the original facts and consent."""
    flow.enable()
    flow.remember()
    record = flow.memory.list_for_user('alice')[0]
    before = _records(flow)
    state = flow.memory.policy_state('alice')
    flow.say(text, proposed(operation, text, targets=[record.id]))
    assert _records(flow) == before
    assert flow.memory.policy_state('alice') == state


def test_same_user_cat_id_is_not_authority_to_delete_cat_for_dog_request(flow):
    """An ID in the supplied candidates must still match the chosen subject."""
    flow.enable()
    flow.remember('두부', '강아지')
    flow.remember('나비', '고양이')
    cat = next(item for item in flow.memory.list_for_user('alice')
               if '고양이' in item.content)
    text = '강아지 이름 기억 삭제해줘'
    flow.say(text, proposed('forget', text, targets=[cat.id], query='고양이'))
    assert any(item.id == cat.id
               for item in flow.memory.list_for_user('alice'))


@pytest.mark.parametrize('older_summary', [False, True])
def test_preconsent_duplicate_is_removed_from_later_model_context(
    flow, older_summary,
):
    """Deletion covers the earlier untracked duplicate and derived summary."""
    flow.remember()
    assert flow.memory.list_for_user('alice') == []
    if older_summary:
        for index in range(12):
            flow.say(f'오늘 할 이야기 {index}')
        assert flow.conversations.get_summary('alice', 'room') is not None
    flow.enable()
    flow.remember()
    stored = flow.memory.list_for_user('alice')[0]
    text = '강아지 이름 기억 삭제해줘'
    flow.say(text, proposed('forget', text, targets=[stored.id]))
    flow.say('앞에서 무슨 이야기를 나눴지?')
    assert not flow.memory.list_for_user('alice')
    assert '두부' not in supplied_text(flow.provider.calls[-1])


def test_old_consent_question_expires_after_other_conversation_stop(flow):
    """A user policy change expires consent questions in another session."""
    flow.say('개인화 켜줘', conversation='room-a')
    flow.say('개인화를 중단해줘', conversation='room-b')
    assert flow.memory.policy_state('alice')['enabled'] is False
    flow.say('네', conversation='room-a')
    assert flow.memory.policy_state('alice')['enabled'] is False


def test_old_conflict_question_expires_after_other_conversation_change(flow):
    """A later yes cannot restore a correction from an old policy version."""
    flow.enable()
    flow.remember()
    previous = flow.memory.list_for_user('alice')[0]
    text = '우리 강아지 이름은 초코야'
    question = flow.say(text, proposed(
        'remember', text, facts=[pet_fact(text, '초코')],
    ))
    assert question.decision.type == 'clarification'
    text = '강아지 이름을 보리로 정정해줘'
    flow.say(text, proposed(
        'correct', text, facts=[pet_fact(text, '보리')],
        targets=[previous.id], query='강아지',
    ), conversation='room-b')
    before = _records(flow)
    assert any('보리' in content for _key, content in before)
    flow.say('네', conversation='room')
    assert _records(flow) == before


@pytest.mark.parametrize('answer', ['아니요', '취소할게', '안녕'])
def test_target_question_can_be_cancelled_or_replaced_with_unrelated_turn(
    flow, answer,
):
    """An ambiguous delete must not trap subsequent unrelated conversations."""
    flow.enable()
    flow.remember('두부', '강아지')
    flow.remember('나비', '고양이')
    text = '그 기억 삭제해줘'
    question = flow.say(text, proposed('forget', text))
    assert question.decision.type == 'clarification'
    before = _records(flow)
    result = flow.say(answer)
    assert _records(flow) == before
    assert '번호를 말씀' not in result.decision.message
    flow.say('다른 이야기를 하자')
    assert flow.provider.calls[-1]['request'].utterance == '다른 이야기를 하자'
    assert flow.provider.calls[-1]['context']['pending_question'] is None


@pytest.mark.parametrize(('text', 'message', 'proposal'), [
    ('안녕', '그 기억을 삭제했어요.', None),
    ('내 이름은 현재야', '이름을 저장했어요.', None),
    ('우리 강아지 이름은 두부야', '이름을 저장했어요.', proposed(
        'remember', '우리 강아지 이름은 두부야',
        facts=[pet_fact('우리 강아지 이름은 두부야')],
    )),
])
def test_uncommitted_model_completion_claim_is_not_returned(
    flow, text, message, proposal,
):
    """Model wording cannot confirm memory changes without a DB effect."""
    result = flow.say(text, proposal, message=message)
    assert not flow.memory.list_for_user('alice')
    assert result.decision.message != message
    persisted = flow.conversations.list_turns('alice', 'room')
    assert persisted[-1].assistant_content != message


def test_model_memory_projection_excludes_private_ids_and_raw_source(flow):
    """The provider gets relevant facts, never raw record metadata or IDs."""
    flow.enable()
    flow.remember()
    record = flow.memory.list_for_user('alice')[0]
    source = record.metadata['source']
    text = '강아지 이름 기억 조회'
    flow.say(text, proposed('recall', text, query='강아지'))
    context = flow.provider.calls[-1]['context']
    assert context['memories']
    projection = json.dumps(context['memories'], ensure_ascii=False)
    assert 'user_id' not in projection
    assert 'metadata' not in projection
    assert 'source' not in projection
    assert 'evidence' not in projection
    for key in ('session_instance_id', 'request_id', 'conversation_id'):
        assert key not in projection
        assert source[key] not in projection
    assert record.id in projection
    assert record.content in projection
