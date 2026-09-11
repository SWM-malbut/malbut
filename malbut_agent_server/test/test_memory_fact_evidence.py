"""Reject fact subjects, kinds and polarity invented around real words."""

import pytest

from test_personal_memory_flow import Flow, proposed


@pytest.fixture
def flow(tmp_path):
    """Use actual consent and Agent commit logic with fixed model outputs."""
    current = Flow(tmp_path / 'evidence.sqlite3')
    current.enable()
    yield current
    current.close()


def _fact(
    source, kind='pet', subject='강아지', attribute='name', value='두부'
):
    return {
        'kind': kind,
        'subject': subject,
        'attribute': attribute,
        'value': value,
        'evidence': source,
    }


@pytest.mark.parametrize(
    ('source', 'fields'),
    [
        ('우리 강아지 이름은 두부야', {'subject': '고양이'}),
        ('내 이름은 두부야', {'kind': 'pet', 'subject': '두부'}),
        ('우리 강아지 이름은 두부야', {'kind': 'name', 'subject': 'user'}),
        ('친구의 이름은 두부야', {'kind': 'name', 'subject': 'user'}),
        (
            '내 이름은 두부야',
            {'kind': 'name', 'subject': 'user', 'attribute': 'breed'},
        ),
        ('우리 강아지 이름은 두부야', {'attribute': 'breed'}),
        ('우리 강아지 품종은 두부야', {'attribute': 'secret_property'}),
        ('내 이름은 두부가 아니야', {'kind': 'name', 'subject': 'user'}),
        (
            '내 별명은 두부가 아니야',
            {'kind': 'nickname', 'subject': 'user', 'attribute': 'nickname'},
        ),
        ('우리 강아지 이름은 두부가 아니야', {}),
        ('우리 강아지 품종은 말티즈가 아니야',
         {'attribute': 'breed', 'value': '말티즈'}),
        ('우리 강아지 나이는 3살이 아니야',
         {'attribute': 'age', 'value': '3살'}),
        ('우리 강아지 이름은 두부가 아니라 초코야', {}),
        (
            '나는 커피를 싫어해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'likes',
                'value': '커피',
            },
        ),
        (
            '나는 커피를 좋아해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'dislikes',
                'value': '커피',
            },
        ),
        (
            '나는 커피를 안 좋아해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'likes',
                'value': '커피',
            },
        ),
        (
            '나는 커피를 좋아하지 않아',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'likes',
                'value': '커피',
            },
        ),
        (
            '나는 커피를 싫어해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'preference',
                'value': '커피',
            },
        ),
        (
            '나는 커피를 좋아하지만 차는 싫어해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'likes',
                'value': '차',
            },
        ),
        (
            '나는 커피를 좋아해. 바나나를 샀어',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'likes',
                'value': '바나나',
            },
        ),
    ],
)
def test_wrong_subject_kind_attribute_or_polarity_never_becomes_a_fact(
    flow,
    source,
    fields,
):
    """A source substring alone is insufficient to justify a labeled fact."""
    candidate = _fact(source, **fields)
    result = flow.say(source, proposed('remember', source, facts=[candidate]))
    assert flow.memory.list_for_user('alice') == []
    if '아니라' in source:
        # A direct correction remains an explicit memory control.
        assert result.decision.type == 'clarification'
    else:
        assert result.decision.type == 'message'
        assert result.decision.message == '대화 답변이에요.'


@pytest.mark.parametrize(
    ('source', 'fields'),
    [
        ('내 이름은 두부야', {'kind': 'name', 'subject': 'user'}),
        (
            '나를 두부라고 불러줘',
            {'kind': 'nickname', 'subject': 'user', 'attribute': 'nickname'},
        ),
        ('우리 강아지 이름은 두부야', {'subject': '반려견'}),
        ('우리 반려묘 이름은 두부야', {'subject': '고양이'}),
        ('강아지 이름을 초코로 정정해줘', {'value': '초코'}),
        ('우리 강아지 이름은 두부가 아니라 초코야', {'value': '초코'}),
        (
            '우리 강아지 품종은 말티즈야',
            {'attribute': 'breed', 'value': '말티즈'},
        ),
        ('우리 강아지 나이는 3살이야', {'attribute': 'age', 'value': '3살'}),
        (
            '우리 강아지 생일은 3월 1일이야',
            {'attribute': 'birthday', 'value': '3월 1일'},
        ),
        (
            '우리 강아지 털색은 갈색이야',
            {'attribute': 'color', 'value': '갈색'},
        ),
        (
            '나는 커피를 좋아해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'likes',
                'value': '커피',
            },
        ),
        (
            '나는 커피를 싫어해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'dislikes',
                'value': '커피',
            },
        ),
        (
            '나는 커피를 안 좋아해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'dislikes',
                'value': '커피',
            },
        ),
        (
            '나는 커피를 좋아하지 않아',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'dislikes',
                'value': '커피',
            },
        ),
        (
            '나는 커피를 좋아해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'preference',
                'value': '커피를 좋아해',
            },
        ),
        (
            '나는 커피를 싫어해',
            {
                'kind': 'preference',
                'subject': 'user',
                'attribute': 'preference',
                'value': '커피를 싫어해',
            },
        ),
    ],
)
def test_direct_labeled_information_and_preserved_preference_can_be_stored(
    flow,
    source,
    fields,
):
    """Useful supported statements remain available after evidence checks."""
    candidate = _fact(source, **fields)
    flow.say(source, proposed('remember', source, facts=[candidate]))
    records = flow.memory.list_for_user('alice')
    assert len(records) == 1
    assert records[0].metadata['fact']['value'] == candidate['value']


def test_correction_rejects_negated_value_and_commits_affirmed_value(flow):
    """Reject the old value and commit the new one from a correction."""
    flow.remember()
    original = flow.memory.list_for_user('alice')[0]
    source = '우리 강아지 이름은 두부가 아니라 초코야'
    rejected = flow.say(source, proposed(
        'correct', source, facts=[_fact(source)], targets=[original.id],
    ))
    assert rejected.decision.type == 'clarification'
    assert flow.memory.list_for_user('alice')[0].id == original.id

    corrected = flow.say(source, proposed(
        'correct', source, facts=[_fact(source, value='초코')],
        targets=[original.id],
    ))
    records = flow.memory.list_for_user('alice')
    assert corrected.decision.message == '요청한 내용으로 기억을 정정했어요.'
    assert len(records) == 1
    assert records[0].id != original.id
    assert records[0].metadata['fact']['value'] == '초코'


def test_forged_recall_on_unrelated_chat_cannot_list_all_memories(flow):
    """A model recall proposal does not broaden the user's current topic."""
    flow.remember()
    text = '오늘 날씨가 어때?'
    result = flow.say(text, proposed('recall', text, query=''))
    assert flow.provider.calls[-1]['memories'] == []
    assert '두부' not in result.decision.message
    assert len(flow.memory.list_for_user('alice')) == 1


def test_forged_recall_query_cannot_search_beyond_current_chat_scope(flow):
    """Ordinary chat ignores a model query for an unrelated saved fact."""
    flow.remember()
    text = '오늘 날씨가 어때?'
    result = flow.say(text, proposed('recall', text, query='강아지'))
    assert '두부' not in result.decision.message


def test_explicit_memory_recall_still_lists_requested_records(flow):
    """A direct request for memory management retains its separate lookup."""
    flow.remember()
    text = '내 기억을 보여줘'
    result = flow.say(text, proposed('recall', text, query=''))
    assert '두부' in result.decision.message
