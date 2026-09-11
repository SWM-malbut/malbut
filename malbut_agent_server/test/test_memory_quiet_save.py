"""Automatic memory candidates never interrupt an ordinary conversation."""

import logging
import sqlite3

import pytest

from malbut_agent_server.providers.base import ProviderError
from test_personal_memory_flow import Flow, pet_fact, proposed


@pytest.fixture
def flow(tmp_path):
    """Use real consent and transactional commits in an isolated database."""
    current = Flow(tmp_path / 'quiet-save.sqlite3')
    current.enable()
    yield current
    current.close()


def assert_normal_reply(flow, result, message):
    """Preserve the ordinary answer in the response, history and replay."""
    assert result.decision.type == 'message'
    assert result.decision.message == message
    turns = flow.conversations.list_turns('alice', 'room')
    assert turns[-1].assistant_content == message
    replay = flow.runtime.handle(flow.requests[-1])
    assert replay.decision.type == 'message'
    assert replay.decision.message == message
    assert flow.runtime.personal_memory.pending_question(
        'alice', 'room'
    ) is None


@pytest.mark.parametrize('text', [
    '우리 강아지 이름은 두부야',
    '우리 강아지 이름은 두부야. 기억력이 좋아',
])
def test_successful_automatic_save_and_repeat_preserve_reply(flow, text):
    """Storing and deduplicating facts are both quiet background effects."""
    proposal = proposed('remember', text, facts=[pet_fact(text)])
    message = '두부와 오늘은 어떤 시간을 보냈나요?'
    for _index in range(2):
        result = flow.say(text, proposal, message=message)
        assert_normal_reply(flow, result, message)
        records = flow.memory.list_for_user('alice')
        assert len(records) == 1
        assert records[0].metadata['fact']['value'] == '두부'


@pytest.mark.parametrize('text', [
    '기억에 남는 영화가 있어?',
    '개인화가 무슨 뜻이야?',
    '정확히는 오늘 조금 피곤해',
    '산책이 아니라 대화를 하고 싶어',
    '내 이름 기억하니?',
    '내 이름 기억해?',
    '내 이름을 기억하고 있어?',
    '사진을 삭제하는 방법을 알려줘',
    '기억해 주는 사람이 있어서 좋아',
    '우리 다른 주제로 바꿔줘',
    '이 문장의 오타를 수정해줘',
    '나는 그때 일을 잘 기억해. 좋은 추억이었지',
])
def test_memory_vocabulary_without_proposal_keeps_conversation(
    flow, text, caplog,
):
    """Missing optional proposals do not turn ordinary talk into errors."""
    caplog.set_level(logging.INFO, logger='malbut_agent_server.personal_memory')
    message = '그 이야기를 조금 더 들려줄래요?'
    state = flow.memory.policy_state('alice')
    result = flow.say(text, message=message)
    assert_normal_reply(flow, result, message)
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice')['enabled'] == state['enabled']
    assert ('memory_policy reason=proposal_missing response=conversation'
            in caplog.text)
    assert text not in caplog.text
    assert 'request-' not in caplog.text
    assert len(flow.provider.calls) == 1


@pytest.mark.parametrize('message', [
    '기억을 저장했어요. 삭제도 완료했어요.', '조금 더 이야기해 주세요.',
])
@pytest.mark.parametrize(('text', 'expected'), [
    ('이름을 기억해줘', '기억해 둘 내용을 한 가지만 다시 알려줄래요?'),
    ('이름을 저장해 주세요', '기억해 둘 내용을 한 가지만 다시 알려줄래요?'),
    ('이름 기억을 삭제해줘', '어떤 내용을 지울지 다시 알려줄래요?'),
    ('그 기억을 지워줘', '어떤 내용을 지울지 다시 알려줄래요?'),
    ('이름 기억을 정정해줘', '어떤 내용을 어떻게 바꿀지 다시 알려줄래요?'),
    ('기억한 이름을 바꿔줘', '어떤 내용을 어떻게 바꿀지 다시 알려줄래요?'),
    ('내 이름을 기억해줘. 내가 말한 이름을 기억해?',
     '기억해 둘 내용을 한 가지만 다시 알려줄래요?'),
])
def test_missing_explicit_proposal_asks_naturally_without_false_success(
    flow, text, expected, message, caplog,
):
    """Explicit changes still need a real result, not a model's promise."""
    caplog.set_level(logging.INFO, logger='malbut_agent_server.personal_memory')
    flow.remember()
    before = flow.memory.list_for_user('alice')
    calls = len(flow.provider.calls)
    result = flow.say(text, message=message)
    assert result.decision.type == 'clarification'
    assert result.decision.message == expected
    assert flow.memory.list_for_user('alice') == before
    assert flow.runtime.personal_memory.pending_question('alice', 'room') is None
    assert len(flow.provider.calls) == calls + 1
    assert 'memory_policy reason=proposal_missing response=followup' in caplog.text
    assert text not in caplog.text
    turns = flow.conversations.list_turns('alice', 'room')
    assert turns[-1].assistant_content == expected
    assert flow.runtime.handle(flow.requests[-1]).decision.message == expected
    assert result.to_dict()['execution']['authorized'] is False
    assert result.to_dict()['execution']['proposal_authorized'] is False
    yes = flow.say('네', message='어떤 이야기를 나눌까요?')
    assert_normal_reply(flow, yes, '어떤 이야기를 나눌까요?')
    assert flow.memory.list_for_user('alice') == before


def test_memory_chatter_cannot_leak_uncommitted_completion_claim(flow, caplog):
    """Quiet wording retains useful conversation but strips false receipts."""
    caplog.set_level(logging.INFO, logger='malbut_agent_server.personal_memory')
    text = '정확히는 오늘 조금 피곤해'
    result = flow.say(text, message='기억에 저장했어요. 잠시 쉬어도 괜찮아요.')
    assert_normal_reply(flow, result, '잠시 쉬어도 괜찮아요.')
    assert flow.memory.list_for_user('alice') == []
    assert 'memory_policy reason=unverified_completion' in caplog.text
    assert text not in caplog.text


def test_quiet_reply_preserves_no_save_restriction(flow):
    """A storage restriction is not an absent management-proposal error."""
    revision = flow.memory.policy_state('alice')['revision']
    message = '다른 이야기를 나눠볼까요?'
    result = flow.say('그 이야기는 저장하지 말아줘', message=message)
    assert_normal_reply(flow, result, message)
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice')['revision'] > revision


@pytest.mark.parametrize('candidate', [
    'not a proposal',
    {'operation': 'remember'},
    proposed('remember', '우리 강아지 이름은 두부야', facts=[{'value': '두부'}]),
])
def test_malformed_provider_candidate_remains_fail_closed(flow, candidate):
    """Quiet save policy does not bypass the provider-envelope boundary."""
    state = flow.memory.policy_state('alice')
    with pytest.raises(ProviderError, match='invalid metadata'):
        flow.say('우리 강아지 이름은 두부야', candidate)
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice') == state
    assert all(turn.request_id != flow.requests[-1].request_id
               for turn in flow.conversations.snapshot('alice', 'room').turns)


@pytest.mark.parametrize('candidate', [
    proposed('remember', '우리 강아지 이름은 두부야'),
    proposed('remember', '우리 강아지 이름은 두부야', facts=[
        pet_fact('우리 강아지 이름은 두부야', subject='고양이'),
    ]),
    proposed('remember', '우리 강아지 이름은 두부야', facts=[
        pet_fact('우리 강아지 이름은 두부야'),
    ], targets=['unavailable-memory']),
])
def test_rejected_automatic_candidate_preserves_reply(flow, candidate):
    """Empty and unsupported candidates do not produce memory notices."""
    state = flow.memory.policy_state('alice')
    message = '두부는 어떤 놀이를 좋아하나요?'
    result = flow.say('우리 강아지 이름은 두부야', candidate, message=message)
    assert_normal_reply(flow, result, message)
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice') == state


def test_invalid_batch_does_not_save_earlier_valid_candidate(flow):
    """A failed fact check rejects the entire automatic candidate batch."""
    first = '내 이름은 수연이야'
    second = '우리 강아지 이름은 두부야'
    text = first + '. ' + second
    candidates = [
        {
            'kind': 'name', 'subject': 'user', 'attribute': 'name',
            'value': '수연', 'evidence': first,
        },
        pet_fact(second, subject='고양이'),
    ]
    state = flow.memory.policy_state('alice')
    message = '두부 이야기도 더 들려주세요.'
    result = flow.say(
        text, proposed('remember', text, facts=candidates), message=message,
    )
    assert_normal_reply(flow, result, message)
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice') == state


def test_automatic_conflict_rolls_back_batch_without_pending_question(flow):
    """A conflict rolls back the batch and cannot consume a later yes."""
    flow.remember()
    records = flow.memory.list_for_user('alice')
    state = flow.memory.policy_state('alice')
    first = '내 이름은 수연이야'
    second = '우리 강아지 이름은 보리야'
    text = first + '. ' + second
    candidates = [
        {
            'kind': 'name', 'subject': 'user', 'attribute': 'name',
            'value': '수연', 'evidence': first,
        },
        pet_fact(second, '보리'),
    ]
    message = '오늘 반려견과 어떤 시간을 보냈나요?'
    result = flow.say(
        text, proposed('remember', text, facts=candidates), message=message,
    )
    assert_normal_reply(flow, result, message)
    assert flow.memory.list_for_user('alice') == records
    assert flow.memory.policy_state('alice') == state
    assert not flow.memory.invalidated_ids('alice')
    yes = flow.say('네', message='어떤 이야기를 더 나눌까요?')
    assert_normal_reply(flow, yes, '어떤 이야기를 더 나눌까요?')
    assert flow.memory.list_for_user('alice') == records


@pytest.mark.parametrize('instruction', ['기억해줘', '저장해줘'])
def test_explicit_save_keeps_verified_acknowledgement(flow, instruction):
    """An explicit save still reports the fact actually committed."""
    text = '우리 강아지 이름은 두부야. ' + instruction
    result = flow.say(text, proposed(
        'remember', text, facts=[pet_fact(text)],
    ))
    assert '기억했어요' in result.decision.message
    stored = flow.memory.list_for_user('alice')[0]
    assert stored.metadata['fact']['value'] == '두부'


def test_explicit_conflict_still_requires_confirmation(flow):
    """Quiet automatic behavior preserves explicit replacement controls."""
    flow.remember()
    old = flow.memory.list_for_user('alice')[0]
    text = '우리 강아지 이름은 보리야. 기억해줘'
    result = flow.say(text, proposed(
        'remember', text, facts=[pet_fact(text, '보리')],
    ))
    assert result.decision.type == 'clarification'
    assert flow.memory.list_for_user('alice')[0].id == old.id
    assert flow.runtime.personal_memory.pending_question(
        'alice', 'room'
    ) is not None
    confirmed = flow.say('네')
    assert '정정했어요' in confirmed.decision.message
    stored = flow.memory.list_for_user('alice')[0]
    assert stored.metadata['fact']['value'] == '보리'


def test_automatic_storage_failure_rolls_back_answer_and_all_facts(
    flow, monkeypatch,
):
    """Quiet UX must not swallow database errors or permit a partial commit."""
    original = flow.memory.upsert_fact
    state = flow.memory.policy_state('alice')
    calls = []

    def fail_second_fact(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            raise sqlite3.OperationalError('injected second fact failure')
        return original(*args, **kwargs)

    monkeypatch.setattr(flow.memory, 'upsert_fact', fail_second_fact)
    first = '내 이름은 수연이야'
    second = '우리 강아지 이름은 두부야'
    text = first + '. ' + second
    candidates = [
        {
            'kind': 'name', 'subject': 'user', 'attribute': 'name',
            'value': '수연', 'evidence': first,
        },
        pet_fact(second),
    ]
    with pytest.raises(sqlite3.OperationalError, match='second fact failure'):
        flow.say(text, proposed('remember', text, facts=candidates))
    assert len(calls) == 2
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice') == state
    assert all(turn.request_id != flow.requests[-1].request_id
               for turn in flow.conversations.snapshot('alice', 'room').turns)
