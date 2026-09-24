"""Verify settings, consent and response scope through real turn commits."""

import pytest

from malbut_agent_server.conversation_preferences import (
    DEFAULTS, preference_request,
)
from malbut_agent_server.schemas import ValidationError
from test_personal_memory_flow import Flow, pet_fact, proposed


@pytest.fixture
def flow(tmp_path):
    current = Flow(tmp_path / 'preferences.sqlite3')
    yield current
    current.close()


def settings(flow):
    return flow.provider.calls[-1]['context']['response_settings']


@pytest.mark.parametrize('address', ['현재', '유지하', '달래'])
def test_four_initial_choices_are_separate_from_consent_and_persist(flow, address):
    prompt = flow.say('초기 설정')
    for question in ('어떤 말투', '어떻게 불러', '어느 정도', '대화를 어떻게'):
        assert question in prompt.decision.message
    result = flow.say(
        f'초기 설정 말투=친근한 반말, 호칭={address}, 답변 길이=자세하게, '
        '대화 중 적극성=주로 들어줘'
    )
    assert '초기 설정에 저장' in result.decision.message
    expected = dict(tone='친근한 반말', address=address, length='자세하게',
                    initiative='주로 들어줘')
    assert flow.runtime.personal_memory.initial_settings('alice') == expected
    assert flow.memory.policy_state('alice')['enabled'] is False
    flow.say('안녕')
    assert settings(flow) == dict(expected, response_mode='natural')
    reopened = Flow(flow.path)
    try:
        reopened.counter = flow.counter
        reopened.say('계속 이야기하자')
        assert settings(reopened) == dict(expected, response_mode='natural')
    finally:
        reopened.close()


def test_session_override_and_one_answer_do_not_change_defaults(flow):
    flow.say('반말로 말해줘. 짧게 답해줘. 주로 들어줘')
    assert settings(flow) == dict(DEFAULTS, tone='친근한 반말',
                                  length='짧고 간단하게', initiative='주로 들어줘',
                                  response_mode='natural')
    flow.say('이번 답변만 자세하게 설명해줘')
    assert settings(flow)['length'] == '자세하게'
    flow.say('계속해줘')
    assert settings(flow)['length'] == '짧고 간단하게'
    assert flow.runtime.personal_memory.initial_settings('alice') == DEFAULTS
    flow.conversations.reset('alice', 'room')
    flow.say('안녕')
    assert settings(flow) == dict(DEFAULTS, response_mode='natural')


@pytest.mark.parametrize('text, next_tone', [
    ('기본 설정은 바꾸지 말고 이번 답변만 반말로 해줘', '편안한 존댓말'),
    ('초기 설정은 변경하지 않고 이번 답변만 반말로 해줘', '편안한 존댓말'),
    ('기본 설정은 그대로 두고 앞으로 반말로 말해줘', '친근한 반말'),
    ('초기 설정은 유지하고 이번 답변만 반말로 말해줘', '편안한 존댓말'),
])
def test_preserving_defaults_does_not_turn_temporary_request_into_saved_choice(
    flow, text, next_tone,
):
    flow.say(text)
    assert flow.runtime.personal_memory.initial_settings('alice') == DEFAULTS
    assert settings(flow)['tone'] == '친근한 반말'
    flow.say('계속 이야기하자')
    assert settings(flow)['tone'] == next_tone
    flow.conversations.reset('alice', 'room')
    flow.say('새 이야기를 하자')
    assert settings(flow)['tone'] == DEFAULTS['tone']


@pytest.mark.parametrize('text, retained, once', [
    ('앞으로 반말로 말해줘. 이번 답변만 짧게 답해줘.',
     {'tone': '친근한 반말'}, {'length': '짧고 간단하게'}),
    ('이번 답변만 짧게 답해줘. 앞으로 반말로 말해줘.',
     {'tone': '친근한 반말'}, {'length': '짧고 간단하게'}),
    ('반말로 말해줘. 이번 답변만 짧게 답해줘.',
     {'tone': '친근한 반말'}, {'length': '짧고 간단하게'}),
    ('앞으로 존댓말로 말해줘. 이번 답변만 반말로 말해줘.',
     {'tone': '편안한 존댓말'}, {'tone': '친근한 반말'}),
    ('이번 답변만 반말로 말해줘. 앞으로 존댓말로 말해줘.',
     {'tone': '편안한 존댓말'}, {'tone': '친근한 반말'}),
    ('앞으로 나를 민수라고 불러줘. 이번 답변만 나를 친구라고 불러줘.',
     {'address': '민수'}, {'address': '친구'}),
    ('나를 계속이라고 불러줘. 이번 답변만 짧게 답해줘.',
     {'address': '계속'}, {'length': '짧고 간단하게'}),
])
def test_mixed_scopes_keep_each_setting_for_its_requested_duration(
    flow, text, retained, once,
):
    baseline = dict(DEFAULTS, length='자세하게')
    flow.runtime.personal_memory.set_initial_settings('alice', baseline)
    flow.say(text)
    expected_session = dict(baseline, **retained, response_mode='natural')
    assert settings(flow) == dict(expected_session, **once)
    flow.say('계속 이야기하자')
    assert settings(flow) == expected_session
    assert flow.runtime.personal_memory.initial_settings('alice') == baseline
    flow.conversations.reset('alice', 'room')
    flow.say('새 이야기를 하자')
    assert settings(flow) == dict(baseline, response_mode='natural')


def test_session_settings_restore_without_memory_consent(flow):
    flow.say('나를 현재라고 불러줘. 지금은 해결책 말고 그냥 들어줘')
    assert settings(flow)['address'] == '현재'
    assert settings(flow)['initiative'] == '주로 들어줘'
    other = Flow(flow.path)
    try:
        other.counter = flow.counter
        other.say('내 이야기를 계속할게')
        assert settings(other)['address'] == '현재'
        assert settings(other)['initiative'] == '주로 들어줘'
        assert settings(other)['response_mode'] == 'listen_only'
        assert not other.provider.calls[-1]['memories']
        assert other.provider.calls[-1]['history']
    finally:
        other.close()


def test_listening_mode_persists_but_single_answer_advice_expires(flow):
    flow.say('지금은 해결책 말고 그냥 들어줘')
    flow.say('오늘 좀 힘들었어')
    assert settings(flow)['response_mode'] == 'listen_only'
    flow.say('이번 한 번만 조언해줘')
    assert settings(flow)['response_mode'] == 'advice_first'
    flow.say('더 이야기할게')
    assert settings(flow)['response_mode'] == 'listen_only'
    flow.say('이제 해결 방법부터 알려줘')
    flow.say('다음 방법도 알려줘')
    assert settings(flow)['response_mode'] == 'advice_first'
    flow.conversations.reset('alice', 'room')
    flow.say('안녕')
    assert settings(flow)['response_mode'] == 'natural'


def test_failed_turn_does_not_change_session_or_defaults(flow):
    flow.say('안녕')

    def fail():
        raise RuntimeError('inference failed')

    with pytest.raises(RuntimeError, match='inference failed'):
        flow.say('반말로 말해줘', callback=fail)
    flow.say('계속해줘')
    assert settings(flow) == dict(DEFAULTS, response_mode='natural')
    with pytest.raises(RuntimeError, match='inference failed'):
        flow.say('이번 답변만 자세하게 설명해줘', callback=fail)
    flow.say('이번 답변만 자세하게 설명해줘')
    assert settings(flow)['length'] == '자세하게'
    flow.say('계속해줘')
    assert settings(flow)['length'] == DEFAULTS['length']


def test_settings_rollback_with_turn_and_delete_with_session(flow, monkeypatch):
    personal = flow.runtime.personal_memory
    original_commit = personal.commit

    def fail_commit(*args):
        original_commit(*args)
        raise RuntimeError('commit failed')

    with monkeypatch.context() as patch:
        patch.setattr(personal, 'commit', fail_commit)
        with pytest.raises(RuntimeError, match='commit failed'):
            flow.say('초기 설정 말투=친근한 반말')
    assert personal.initial_settings('alice') == DEFAULTS
    flow.say('반말로 말해줘')
    flow.conversations.delete('alice', 'room')
    with flow.conversations._lock:
        count = flow.conversations._connection.execute(
            'SELECT COUNT(*) FROM session_response_settings'
        ).fetchone()[0]
    assert count == 0


def test_style_command_cannot_be_saved_as_personal_fact(flow):
    flow.enable()
    text = '나를 현재라고 불러줘'
    fact = dict(kind='nickname', subject='user', attribute='nickname',
                value='현재', evidence=text)
    flow.say(text, proposed('remember', text, facts=[fact]),
             message='호칭을 기억에 저장했어요. 알겠어요.')
    assert flow.memory.list_for_user('alice') == []
    assert '저장했어요' not in flow.runtime.handle(flow.requests[-1]).decision.message
    assert settings(flow)['address'] == '현재'


@pytest.mark.parametrize('value', ['민수', '친구'])
def test_mixed_scope_addresses_cannot_be_saved_as_personal_facts(flow, value):
    flow.enable()
    text = '앞으로 나를 민수라고 불러줘. 이번 답변만 나를 친구라고 불러줘.'
    fact = dict(kind='nickname', subject='user', attribute='nickname',
                value=value, evidence=text)
    flow.say(text, proposed('remember', text, facts=[fact]))
    assert flow.memory.list_for_user('alice') == []
    assert settings(flow)['address'] == '친구'
    flow.say('계속 이야기하자')
    assert settings(flow)['address'] == '민수'


@pytest.mark.parametrize('delayed', [False, True])
def test_mixed_style_and_personal_fact_only_save_the_fact(flow, delayed):
    flow.enable()
    address = '나를 현재라고 불러줘'
    pet = '우리 강아지 이름은 두부야'
    text = f'{address}. 짧게 답해줘. {pet}'
    facts = [
        dict(kind='nickname', subject='user', attribute='nickname',
             value='현재', evidence=address),
        pet_fact(pet),
    ]
    if delayed:
        from test_automatic_memory_policy import apply, freeze
        job = freeze(flow, text, facts=facts)
        apply(flow, job)
    else:
        flow.say(text, proposed('remember', text, facts=facts))
    records = flow.memory.list_for_user('alice')
    assert len(records) == 1
    assert records[0].metadata['fact']['value'] == '두부'
    assert records[0].kind == 'pet'
    assert settings(flow)['address'] == '현재'
    assert settings(flow)['length'] == '짧고 간단하게'


def test_defaults_validation_and_user_session_isolation(flow):
    personal = flow.runtime.personal_memory
    for patch in ({'memory_enabled': True}, {'tone': '아무거나'},
                  {'address': 'x' * 51}, {'length': False}):
        with pytest.raises(ValidationError):
            personal.set_initial_settings('alice', patch)
    personal.set_initial_settings('alice', {'tone': '친근한 반말'})
    flow.say('안녕')
    assert settings(flow)['tone'] == '친근한 반말'
    personal.set_initial_settings('alice', {'tone': '편안한 존댓말'})
    flow.say('계속하자')
    assert settings(flow)['tone'] == '친근한 반말'
    flow.say('안녕', conversation='new-room')
    assert settings(flow) == dict(DEFAULTS, response_mode='natural')
    flow.say('안녕', user='bob')
    assert settings(flow) == dict(DEFAULTS, response_mode='natural')
    flow.say('초기 설정 말투=존대 아니면 반말')
    assert personal.initial_settings('alice')['tone'] == DEFAULTS['tone']


def test_memory_choices_require_informed_consent_and_remain_separate(flow):
    first = flow.say('기억 사용에 동의하기')
    assert first.decision.type == 'clarification'
    assert not flow.memory.policy_state('alice')['enabled']
    flow.say('기억 사용에 동의하기')
    assert flow.memory.policy_state('alice')['enabled']
    flow.say('사용하지 않기')
    assert not flow.memory.policy_state('alice')['enabled']
    assert flow.runtime.personal_memory.initial_settings('alice') == DEFAULTS


def test_reported_negated_or_hypothetical_style_is_not_a_setting():
    for text in ('친구가 반말로 말해줘라고 했어', '반말로 말하지 마', '반말로 하지 마',
                 '만약 반말로 말해줘라고 하면?', '"자세하게"가 무슨 뜻이야?',
                 '해결책을 알려줘라고 요청한 건 아니야', '조언해줘 하지 마',
                 '만약 앞으로 존댓말로 말해줘. 이번 답변만 반말로 말해줘라고 하면?',
                 '"앞으로 반말로 말해줘. 이번 답변만 짧게 답해줘"라고 했어',
                 '앞으로 반말로 말하지 마. 이번 답변만 짧게 답하지 마'):
        assert preference_request(text) is None


@pytest.mark.parametrize('text', [
    '기본 설정 화면에는 반말로 해달라는 문구가 있어.',
    '수민이가 내게 반말로 말해 달래.',
    '반말로 해줘라는 건 내 요청이 아니야.',
    '「반말로 말해줘」라는 문장의 뜻을 설명해 줘.',
])
def test_reported_style_request_does_not_change_saved_or_session_settings(flow, text):
    flow.say(text)
    assert settings(flow) == dict(DEFAULTS, response_mode='natural')
    assert flow.runtime.personal_memory.initial_settings('alice') == DEFAULTS
    flow.say('계속 이야기하자')
    assert settings(flow) == dict(DEFAULTS, response_mode='natural')


@pytest.mark.parametrize('ending', ['해줘', '해'])
def test_direct_natural_default_tone_request_remains_supported(flow, ending):
    flow.say(f'기본 설정을 반말로 {ending}')
    assert flow.runtime.personal_memory.initial_settings('alice') == dict(
        DEFAULTS, tone='친근한 반말',
    )
    flow.say('계속 이야기하자')
    assert settings(flow)['tone'] == '친근한 반말'


@pytest.mark.parametrize('text, next_tone', [
    ('수민이에게 반말로 말해줘', '편안한 존댓말'),
    ('친구한테 반말로 말해줘', '편안한 존댓말'),
    ('나에게 반말로 말해줘', '친근한 반말'),
    ('저한테 반말로 말해줘', '친근한 반말'),
    ('수민이에게 선물을 줬어. 나에게 반말로 말해줘', '친근한 반말'),
    ('앞으로 친구한테 반말로 말해줘', '편안한 존댓말'),
    ('이 대화에서는 수민이에게 반말로 말해줘', '편안한 존댓말'),
    ('항상 친구한테 반말로 말해줘', '편안한 존댓말'),
    ('모든 대화에서 친구한테 반말로 말해줘', '편안한 존댓말'),
])
def test_other_recipient_style_does_not_set_user_tone_even_with_time_scope(
    flow, text, next_tone,
):
    flow.say(text)
    assert settings(flow)['tone'] == '친근한 반말'
    flow.say('계속 이야기하자')
    assert settings(flow)['tone'] == next_tone
    assert flow.runtime.personal_memory.initial_settings('alice') == DEFAULTS


def test_artifact_style_does_not_change_later_reply_preferences(flow):
    flow.say('신입생 대상 소개 글을 자세하게 써줘')
    assert settings(flow)['length'] == '자세하게'
    flow.say('다른 이야기를 하자')
    assert settings(flow)['length'] == DEFAULTS['length']
    flow.say('편하게 반말 해줘')
    flow.say('계속 이야기하자')
    assert settings(flow)['tone'] == '친근한 반말'


@pytest.mark.parametrize('text', [
    '이제 만들 초대문 본문만은 친구들에게 보내는 글이라 편한 반말로 해 주세요. '
    '그 앞뒤에서 저한테 설명한다면 존댓말을 쓰고요. '
    '이 말투는 이번 초대문에만 적용하면 돼요.',
    '이 글에만 반말로 해 주세요.',
    '본문만 반말로 해 주세요.',
    '반말로 해 주세요. 이 말투는 이번 초대문에만 적용하면 돼요.',
])
def test_explicit_artifact_only_tone_is_not_retained_as_session_tone(flow, text):
    assert preference_request(text)['scope'] == 'answer'
    flow.say(text)
    assert settings(flow)['tone'] == '친근한 반말'
    flow.say('다른 모임 이름 후보도 알려주세요.')
    assert settings(flow)['tone'] == DEFAULTS['tone']
    assert any(turn.user_content == text
               for turn in flow.provider.calls[-1]['history'])
    assert flow.runtime.personal_memory.initial_settings('alice') == DEFAULTS
