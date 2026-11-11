"""Occasion-qualified facts must not become durable automatic preferences."""
import copy

import pytest

from malbut_agent_server.automatic_memory_policy import (
    automatic_candidate, stable_automatic_proposal,
    temporary_automatic_preference,
)
from malbut_agent_server.memory_source_review import attach_source_review
from test_automatic_memory_policy import apply, flow, freeze, frozen_rows
from test_automatic_memory_extraction_worker import (
    frozen_response, lab, status, until,
)
from test_personal_memory_flow import proposed


def preference(text, value, attribute='preference', evidence=None):
    return {'kind': 'preference', 'subject': 'user', 'attribute': attribute,
            'value': value, 'evidence': text if evidence is None else evidence}


@pytest.mark.parametrize('text,value,attribute,evidence', [
    ('나는 이번엔 오래 걷고 싶지는 않아', '오래 걷고 싶지는 않아',
     'dislikes', '오래 걷고 싶지는 않아'),
    ('나는 오늘은 커피를 싫어해', '커피', 'dislikes', '커피를 싫어해'),
    ('나는 지금 차를 마시고 싶어', '차를 마시고 싶어', 'preference', None),
    ('이번 여행에서는 나는 오래 걷는 것을 싫어해', '오래 걷는 것',
     'dislikes', None),
    ('나는 당분간 단 음식을 안 좋아해', '단 음식', 'dislikes', None),
    ('나는 잠깐 조용한 놀이를 선호해', '조용한 놀이', 'likes', None),
    ('나는 오늘은 피곤한데 오래 걷는 걸 싫어해', '오래 걷는 걸',
     'dislikes', None),
    ('나는 이번엔 커피, 차를 좋아해', '커피, 차를 좋아해',
     'preference', None),
    ('나는 오늘은 커피를 싫어하지만 친구는 평소 커피를 싫어해',
     '커피', 'dislikes', None),
    ('오늘은 평소와 달리 커피가 싫어', '커피', 'dislikes', None),
    ('오늘은 나는 평소와 달리 커피가 싫어', '커피', 'dislikes', None),
    ('오늘은 나는 평소에 마시던 커피가 싫어', '커피', 'dislikes', None),
])
def test_temporary_scope_survives_clipped_evidence(
    text, value, attribute, evidence,
):
    fact = preference(text, value, attribute, evidence)
    assert temporary_automatic_preference(fact, text)
    candidate = proposed('remember', text, facts=[fact])
    before = copy.deepcopy(candidate)
    assert stable_automatic_proposal(candidate, text)['facts'] == []
    assert candidate == before


@pytest.mark.parametrize('text,value,attribute', [
    ('나는 평소 오래 걷는 걸 싫어해', '오래 걷는 걸', 'dislikes'),
    ('나는 원래 커피를 좋아해', '커피', 'likes'),
    ('나는 보통 차를 선호해', '차', 'likes'),
    ('나는 커피를 좋아해', '커피', 'likes'),
    ('주말에는 좀 쉬고 싶어', '주말에는 좀 쉬고 싶어', 'preference'),
    ('이번에 새로 말하지만 나는 원래 커피를 좋아해', '커피', 'likes'),
    ('오늘 알게 됐는데 나는 평소 차를 선호해', '차', 'likes'),
    ('오늘 내 취향을 말하면 나는 평소 커피를 좋아해', '커피', 'likes'),
    ('이번에는 내 평소 취향을 알려줄게 나는 원래 보리차를 좋아해',
     '보리차', 'likes'),
])
@pytest.mark.parametrize('clipped', [False, True])
def test_ordinary_and_explicit_habitual_preferences_remain_eligible(
    text, value, attribute, clipped,
):
    fact = preference(text, value, attribute, value if clipped else None)
    candidate = proposed('remember', text, facts=[fact])
    assert not temporary_automatic_preference(fact, text)
    assert stable_automatic_proposal(candidate, text) == candidate


@pytest.mark.parametrize('same_object', [False, True])
@pytest.mark.parametrize('self_prefix', ['', '나는 '])
def test_mixed_source_uses_each_fact_clause_and_polarity(
    same_object, self_prefix,
):
    second = '커피를 싫어해' if same_object else '차를 마시고 싶어'
    text = self_prefix + '평소 커피를 좋아하지만 오늘은 ' + second
    standing = preference(text, '커피', 'likes')
    current = preference(
        text, '커피' if same_object else second,
        'dislikes' if same_object else 'preference',
    )
    candidate = proposed('remember', text, facts=[standing, current])
    assert stable_automatic_proposal(candidate, text)['facts'] == [standing]


@pytest.mark.parametrize('separator', ['. ', ', ', ' 그리고 '])
@pytest.mark.parametrize('frequency', ['오늘도', '매일'])
def test_third_party_habitual_relative_cannot_reset_user_scope(
    separator, frequency,
):
    text = ('나는 이번엔 커피를 좋아해' + separator
            + '평소 커피를 좋아하는 민서는 ' + frequency + ' 마신대')
    fact = preference(text, '커피', 'likes', '커피')
    assert temporary_automatic_preference(fact, text)
    candidate = proposed('remember', text, facts=[fact])
    assert stable_automatic_proposal(candidate, text)['facts'] == []


def test_semantic_approval_cannot_bypass_temporary_scope(flow):
    text = '나는 이번엔 오래 걷고 싶지는 않아'
    fact = preference(text, '오래 걷고 싶지는 않아', 'dislikes',
                      '오래 걷고 싶지는 않아')
    job = freeze(flow, text, [fact])
    attach_source_review(job.snapshot.source, [fact])
    before = frozen_rows(flow)
    assert not automatic_candidate(job.request, job.snapshot, job.result)
    assert apply(flow, job) == {'state': 'discarded', 'memory_ids': []}
    assert flow.memory.list_for_user('alice') == []
    assert frozen_rows(flow) == before


@pytest.mark.parametrize('mode', [
    'temporary', 'habitual', 'habitual_after_context', 'mixed', 'mixed_same',
])
def test_worker_filters_before_review_without_changing_reply(lab, mode):
    flow, extractor, reviewer, entered, release = lab
    if mode == 'temporary':
        text = '나는 이번엔 오래 걷고 싶지는 않아'
        facts = [preference(text, '오래 걷고 싶지는 않아', 'dislikes')]
        expected = []
    elif mode in {'habitual', 'habitual_after_context'}:
        text = ('나는 평소 오래 걷는 걸 싫어해' if mode == 'habitual' else
                '이번에 새로 말하지만 나는 원래 오래 걷는 걸 싫어해')
        facts = [preference(text, '오래 걷는 걸', 'dislikes')]
        expected = ['오래 걷는 걸']
    else:
        second = ('커피를 싫어해' if mode == 'mixed_same' else
                  '차를 마시고 싶어')
        text = '나는 평소 커피를 좋아하지만 오늘은 ' + second
        facts = [preference(text, '커피', 'likes'),
                 preference(text, '커피' if mode == 'mixed_same' else second,
                            'dislikes' if mode == 'mixed_same' else 'preference')]
        expected = ['커피']

    def extraction(result):
        result.memory_proposal = proposed('remember', text, facts=facts)

    extractor.change = extraction
    reply = flow.say(text, message='그렇군요. 이야기 잘 들었어요.')
    request = flow.requests[-1]
    assert entered.wait(2)
    before = reply.to_dict()
    persisted = frozen_response(flow, request)
    release.set()
    until(lambda: status(flow, request)['state'] in {'saved', 'discarded'})
    records = flow.memory.list_for_user('alice')
    assert [record.metadata['fact']['value'] for record in records] == expected
    assert len(extractor.calls) == len(flow.provider.calls) == 1
    if mode == 'temporary':
        assert reviewer.calls == []
    assert reply.to_dict() == before
    assert frozen_response(flow, request) == persisted
