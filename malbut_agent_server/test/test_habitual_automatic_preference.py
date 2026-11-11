"""Explicit habitual facts remain source-grounded after automatic extraction."""

import copy

import pytest

from malbut_agent_server.automatic_memory_policy import (
    grounded_habitual_proposal, stable_automatic_proposal,
)
from test_automatic_memory_extraction_worker import (
    frozen_response, lab, status, until,
)
from test_personal_memory_flow import proposed
from test_temporary_automatic_preference import preference


@pytest.mark.parametrize('mode', ['null', 'normalized', 'mixed', 'new_disclosure'])
def test_worker_recovers_current_source_habit_without_rewriting_reply(lab, mode):
    flow, extractor, reviewer, entered, release = lab
    if mode in {'null', 'mixed'}:
        text = '나는 평소에는 보리차를 좋아하지만, 오늘은 커피가 마시고 싶어.'
        facts = (None if mode == 'null' else [
            preference(text, '커피가 마시고 싶어', 'preference'),
        ])
        expected = ['보리차']
    elif mode == 'normalized':
        text = '나는 평소에는 오래 걷는 걸 싫어해.'
        facts = [preference(text, '오래 걷는 것', 'dislikes')]
        expected = ['오래 걷는 걸']
    else:
        text = '이번에 새로 말하는 사실인데, 나는 원래 따뜻한 차를 좋아해.'
        facts = None
        expected = ['따뜻한 차']

    observed = []

    def extraction(result):
        result.memory_proposal = (
            None if facts is None else proposed('remember', text, facts=facts)
        )
        observed.append(copy.deepcopy(result.memory_proposal))

    extractor.change = extraction
    reply = flow.say(text, message='그렇군요. 이야기 잘 들었어요.')
    request = flow.requests[-1]
    assert entered.wait(2)
    before, persisted = reply.to_dict(), frozen_response(flow, request)
    release.set()
    until(lambda: status(flow, request)['state'] in {'saved', 'discarded'})
    records = flow.memory.list_for_user('alice')
    assert [record.metadata['fact']['value'] for record in records] == expected
    assert len(extractor.calls) == len(flow.provider.calls) == 1
    assert observed == [None if facts is None else proposed('remember', text, facts=facts)]
    assert reply.to_dict() == before
    assert frozen_response(flow, request) == persisted
    for record in records:
        fact = record.metadata['fact']
        assert fact['value'] in fact['evidence']
        assert fact['evidence'] in text
        assert record.metadata['source']['text'] == text
        assert record.metadata['source']['request_id'] == request.request_id


@pytest.mark.parametrize('text', [
    '나는 평소에는 보리차를 좋아해.',
    '저는 원래 따뜻한 차를 좋아합니다.',
    '나는 평소에는 오래 걷는 걸 싫어해.',
    '이번에 새로 말하는 사실인데, 나는 원래 따뜻한 차를 좋아해.',
])
def test_recovered_facts_are_literal_and_have_current_source_evidence(text):
    proposal = grounded_habitual_proposal(None, text)
    assert len(proposal['facts']) == 1
    fact = proposal['facts'][0]
    assert fact['value'] in fact['evidence'] in text
    assert stable_automatic_proposal(proposal, text) == proposal


@pytest.mark.parametrize('text', [
    '나는 이번에는 오래 걷는 걸 싫어해.',
    '나는 오늘 보리차를 좋아해.',
    '나는 평소에도 이번에는 오래 걷는 걸 싫어해.',
    '나는 평소와 달리 커피를 좋아해.',
    '나는 평소에 마시던 커피를 좋아해.',
    '민서는 평소에는 보리차를 좋아해.',
    '나는 평소에는 민서가 보리차를 좋아해.',
    '나는 평소에는 민서는 보리차를 좋아해.',
    '나는 평소에는 오후에 민서가 보리차를 좋아해.',
    '나는 평소에는 민서가 좋아하는 보리차를 좋아해.',
    '나는 평소에는 커피를 좋아하는 사람을 싫어해.',
    '나는 평소에는 커피를 좋아하지 않아.',
    '나는 평소에는 커피를 안 좋아해.',
    '나는 평소에는 커피를 싫어하지 않아.',
    '나는 평소에는 커피를 좋아해도 되는지 모르겠어.',
    '아마 나는 평소에는 보리차를 좋아해.',
    '잘 모르겠지만, 나는 평소에는 보리차를 좋아해.',
    '나는 평소에는 보리차를 좋아해. 확실하지는 않아.',
    '나는 평소에는 보리차를 좋아해 보인대.',
    '나는 평소에는 보리차를 좋아해라고 말한 적 없어.',
    '나는 평소에는 보리차를 좋아해, 라는 말이 아냐.',
    '민서 말로는, 나는 평소에는 보리차를 좋아해.',
    '민서가 말했어. 나는 평소에는 보리차를 좋아해.',
    '"나는 평소에는 보리차를 좋아해"라고 민서가 말했어.',
    '만약 나는 평소에는 보리차를 좋아해.',
    '예를 들면 나는 평소에는 보리차를 좋아해.',
])
def test_no_local_recovery_from_temporary_or_unasserted_self_habits(text):
    assert grounded_habitual_proposal(None, text) is None


def test_exact_source_repair_preserves_other_source_backed_candidates():
    text = '내 이름은 민재야. 나는 평소에는 오래 걷는 걸 싫어해. 나는 산책을 좋아해.'
    unrelated = preference(text, '산책', 'likes')
    malformed = preference(text, '오래 걷는 것을 싫어함', 'dislikes')
    name = dict(kind='name', subject='user', attribute='name',
                value='민재', evidence='내 이름은 민재야.')
    original = proposed('remember', text, facts=[malformed, unrelated, name])
    before = copy.deepcopy(original)
    result = grounded_habitual_proposal(original, text)
    assert result['facts'][:2] == [unrelated, name]
    assert result['facts'][2]['value'] == '오래 걷는 걸'
    assert original == before


@pytest.mark.parametrize('habitual, other, attribute', [
    ('드라마 음악', '드라마', 'dislikes'),
    ('드라마 음악', '드라마', 'likes'),
    ('보리차', '차', 'likes'),
    ('차', '보리차', 'likes'),
    ('보리차', '차', 'dislikes'),
])
@pytest.mark.parametrize('whole_source_evidence', [False, True])
def test_recovery_preserves_overlapping_objects_from_other_clauses(
    habitual, other, attribute, whole_source_evidence,
):
    first = '나는 평소에는 %s를 좋아해.' % habitual
    second = '나는 %s를 %s해.' % (
        other, '싫어' if attribute == 'dislikes' else '좋아',
    )
    text = first + ' ' + second
    unrelated = preference(text if whole_source_evidence else second,
                           other, attribute)
    original = proposed('remember', text, facts=[
        preference(text if whole_source_evidence else first, habitual, 'likes'),
        unrelated,
    ])
    before = copy.deepcopy(original)
    result = grounded_habitual_proposal(original, text)
    assert result['facts'][0] == unrelated
    assert result['facts'][1]['value'] == habitual
    assert len(stable_automatic_proposal(result, text)['facts']) == 2
    assert original == before


def test_worker_keeps_opposite_preference_for_overlapping_object(lab):
    flow, extractor, reviewer, entered, release = lab
    text = '나는 평소에는 드라마 음악을 좋아해. 나는 드라마를 싫어해.'
    extractor.change = lambda result: setattr(result, 'memory_proposal', proposed(
        'remember', text, facts=[preference(text, '드라마 음악', 'likes'),
                                 preference(text, '드라마', 'dislikes')],
    ))
    flow.say(text, message='그렇군요.')
    request = flow.requests[-1]
    assert entered.wait(2)
    release.set()
    until(lambda: status(flow, request)['state'] in {'saved', 'discarded'})
    facts = [record.metadata['fact'] for record in flow.memory.list_for_user('alice')]
    assert {(fact['attribute'], fact['value']) for fact in facts} == {
        ('likes', '드라마 음악'), ('dislikes', '드라마'),
    }


def test_empty_habitual_object_does_not_create_an_invalid_candidate():
    assert grounded_habitual_proposal(None, '나는 평소에는  좋아해.') is None


@pytest.mark.parametrize('object_prefix', ['', '보리차'])
def test_long_whitespace_without_preference_predicate_is_not_recovered(object_prefix):
    text = '나는 평소에는 ' + object_prefix + ' ' * 4000 + '모름.'
    assert grounded_habitual_proposal(None, text) is None


def test_recovery_uses_bounded_clause_evidence_for_long_source():
    text = '나는 평소에는 보리차를 좋아해. ' + '이어서 대화해. ' * 250
    result = grounded_habitual_proposal(None, text)
    assert result['evidence'] in text
    assert len(result['evidence']) <= 2000
    assert [fact['value'] for fact in result['facts']] == ['보리차']
    assert stable_automatic_proposal(result, text) == result


def test_oversized_habitual_object_is_not_recovered():
    text = '나는 평소에는 ' + '보리차' * 700 + '를 좋아해.'
    assert grounded_habitual_proposal(None, text) is None


@pytest.mark.parametrize('text', [
    '나는 평소에는 보리차를 좋아하지만, 오늘은 커피를 좋아해.',
    '나는 평소에는 보리차를 좋아하지만, 오늘은 보리차를 싫어해.',
])
def test_habitual_recovery_still_leaves_only_durable_side_of_mixed_source(text):
    current = preference(text, '커피' if '커피' in text else '보리차',
                         'likes' if '커피' in text else 'dislikes')
    result = grounded_habitual_proposal(proposed('remember', text, facts=[current]), text)
    facts = stable_automatic_proposal(result, text)['facts']
    assert len(facts) == 1
    assert facts[0]['value'] == '보리차'
    assert facts[0]['attribute'] == 'likes'


@pytest.mark.parametrize('control', [
    '내 취향 기억 삭제해줘', '내 취향을 커피로 정정해줘', '개인화 꺼줘',
])
def test_control_during_extraction_cannot_resurrect_recovered_preference(lab, control):
    flow, extractor, reviewer, entered, release = lab
    extractor.change = lambda result: setattr(result, 'memory_proposal', None)
    flow.say('나는 평소에는 보리차를 좋아해.', message='그렇군요.')
    request = flow.requests[-1]
    assert entered.wait(2)
    flow.say(control)
    assert status(flow, request)['state'] == 'discarded'
    release.set()
    flow.runtime.stop_background_memory()
    assert len(extractor.calls) == 1
    assert reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []
