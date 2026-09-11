"""Semantic reviews are scoped evidence, not permission to mutate or execute.
"""

import copy

import pytest

from malbut_agent_server.memory_source_review import (
    MemorySourceReviewer, attach_source_review, source_review_matches,
)
from malbut_agent_server.conversation import ConversationChangedError
from malbut_agent_server.orchestrator import MemoryChangedError
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.schemas import (
    AgentDecision, ProviderResult, ProviderUsage,
)
from test_personal_memory_flow import Flow, origin, proposed


def name_fact(text, value='김민재'):
    """A model candidate, independent of lexical or semantic implementation."""
    return {'kind': 'name', 'subject': 'user', 'attribute': 'name',
            'value': value, 'evidence': text}


class ReviewProvider(AgentProvider):
    """Script the reviewer separately from the primary extraction provider."""

    supports_memory = True

    def __init__(self):
        self.calls = []
        self.accept = True
        self.callback = None
        self.rewrite = None
        self.tool = False

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None, *, memory_context=None):
        self.calls.append((request, copy.deepcopy(memory_context)))
        assert memories == conversation_turns == tools == []
        assert conversation_summary is None
        assert request.available_tools == ()
        assert not request.robot_state.to_dict().get('device_id')
        assert memory_context['memories'] == []
        assert memory_context['pending_question'] is None
        assert memory_context['mode'] == 'source_review'
        if self.callback:
            self.callback()
        facts = (
            copy.deepcopy(memory_context['candidate_facts'])
            if self.accept else []
        )
        proposal = proposed('remember', request.utterance, facts=facts)
        if self.rewrite:
            self.rewrite(proposal)
        decision = AgentDecision(type='message', message='review-only')
        if self.tool:
            decision = AgentDecision(
                type='tool_call', message='unsafe',
                tool_name='navigate', arguments={'location': '거실'},
            )
        return ProviderResult(
            decision=decision, provider='fixture', model='source-review',
            latency_ms=1, memory_supported=True, memory_proposal=proposal,
            usage=ProviderUsage(
                input_tokens=3, output_tokens=2, total_tokens=5,
            ),
        )


@pytest.fixture
def setup(tmp_path):
    """Use actual stores, consent and commit; only model outputs are
    controlled.
    """
    flow = Flow(tmp_path / 'semantic.sqlite3')
    reviewer = ReviewProvider()
    flow.runtime.memory_source_reviewer = MemorySourceReviewer(reviewer)
    yield flow, reviewer
    flow.close()


@pytest.mark.parametrize('text', [
    '난 김민재야', '나는 김민재야', '저는 김민재입니다',
    '김민재라고 해. 반가워!',
])
def test_semantic_self_introduction_stores_without_changing_reply(setup, text):
    flow, reviewer = setup
    flow.enable()
    reviewer.callback = lambda: assert_no_transaction(flow)
    reply = '민재님, 만나서 반가워요!'
    result = flow.say(
        text, proposed('remember', text, facts=[name_fact(text)]),
        message=reply,
    )
    assert len(reviewer.calls) == 1
    assert result.decision.message == reply
    assert result.decision.type == 'message'
    record = flow.memory.list_for_user('alice')[0]
    assert record.metadata['fact']['value'] == '김민재'
    assert record.metadata['source']['text'] == text
    assert '_semantic_review' not in str(result.to_persisted_dict())
    flow.runtime.handle(flow.requests[-1])
    assert len(reviewer.calls) == 1
    assert len(flow.memory.list_for_user('alice')) == 1


def assert_no_transaction(flow):
    """No remote inference is allowed under the SQLite commit transaction."""
    assert flow.conversations._connection.in_transaction is False


def test_lexically_supported_fact_needs_no_review(setup):
    flow, reviewer = setup
    flow.enable()
    text = '내 이름은 김민재야'
    flow.say(text, proposed('remember', text, facts=[name_fact(text)]))
    assert reviewer.calls == []
    assert len(flow.memory.list_for_user('alice')) == 1


def test_unenabled_automatic_candidate_is_not_reviewed_or_saved(setup):
    flow, reviewer = setup
    text = '난 김민재야'
    result = flow.say(
        text, proposed('remember', text, facts=[name_fact(text)]),
    )
    assert result.decision.message == '대화 답변이에요.'
    assert reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []


@pytest.mark.parametrize('text', [
    '난 김민재가 아니야', '난 학생이야', '친구의 이름은 김민재야',
])
def test_reviewer_rejection_preserves_reply_without_saving(setup, text):
    flow, reviewer = setup
    flow.enable()
    reviewer.accept = False
    fact = name_fact(text, '학생' if '학생' in text else '김민재')
    result = flow.say(text, proposed('remember', text, facts=[fact]))
    assert len(reviewer.calls) == 1
    assert flow.memory.list_for_user('alice') == []
    assert result.decision.message == '대화 답변이에요.'


@pytest.mark.parametrize('text', [
    '친구가 "난 김민재야"라고 했어', '만약 난 김민재라면',
    '난 김민재야. 저장하지 마',
])
def test_excluded_sources_never_reach_reviewer(setup, text):
    flow, reviewer = setup
    flow.enable()
    flow.say(text, proposed('remember', text, facts=[name_fact(text)]))
    assert reviewer.calls == []
    assert flow.memory.list_for_user('alice') == []


@pytest.mark.parametrize('change', [
    lambda p: p['facts'][0].update(value='다른이름'),
    lambda p: p.update(target_ids=['foreign-memory']),
    lambda p: p.update(operation='correct'),
    lambda p: p.update(evidence='unrelated source'),
    lambda p: p['facts'].append(copy.deepcopy(p['facts'][0])),
    lambda p: p['facts'][0].update(authorized=True),
])
def test_reviewer_cannot_rewrite_candidate_or_grant_authority(setup, change):
    flow, reviewer = setup
    flow.enable()
    reviewer.rewrite = change
    text = '난 김민재야'
    result = flow.say(
        text, proposed('remember', text, facts=[name_fact(text)]),
    )
    assert flow.memory.list_for_user('alice') == []
    assert result.decision.message == '대화 답변이에요.'


def test_tool_result_and_timeout_do_not_escape_review_boundary(setup):
    flow, reviewer = setup
    flow.enable()
    text = '난 김민재야'
    for fail in ('tool', 'timeout'):
        reviewer.tool = fail == 'tool'
        if fail == 'timeout':
            def timeout():
                raise TimeoutError('must not be surfaced')
            reviewer.callback = timeout
        result = flow.say(
            text, proposed('remember', text, facts=[name_fact(text)]),
        )
        assert flow.memory.list_for_user('alice') == []
        assert result.decision.type == 'message'
        assert result.decision.message == '대화 답변이에요.'


def test_explicit_failed_review_explains_no_save(setup):
    flow, reviewer = setup
    flow.enable()
    reviewer.accept = False
    text = '난 김민재야. 기억해줘'
    result = flow.say(
        text, proposed('remember', text, facts=[name_fact(text)]),
    )
    assert result.decision.type == 'clarification'
    assert flow.memory.list_for_user('alice') == []


def test_reviewed_explicit_save_survives_restart_then_consent_without_llm(
    setup,
):
    flow, reviewer = setup
    text = '난 김민재야. 기억해줘'
    result = flow.say(
        text, proposed('remember', text, facts=[name_fact(text)]),
    )
    assert result.decision.type == 'clarification'
    assert len(reviewer.calls) == 1
    assert flow.memory.list_for_user('alice') == []
    path = flow.path
    flow.close()
    restored = Flow(path)
    restored.counter = 100
    restored.runtime.memory_source_reviewer = MemorySourceReviewer(reviewer)
    try:
        result = restored.say('네')
        assert '기억했어요' in result.decision.message
        assert restored.provider.calls == []
        assert len(reviewer.calls) == 1
        assert (
            restored.memory.list_for_user('alice')[0].metadata['fact']['value']
            == '김민재'
        )
    finally:
        restored.close()


def test_consent_revocation_during_review_blocks_commit(setup):
    flow, reviewer = setup
    flow.enable()
    reviewer.callback = lambda: flow.memory.set_personalization(
        'alice', False, origin('개인화 꺼줘'),
    )
    text = '난 김민재야'
    with pytest.raises(MemoryChangedError):
        flow.say(text, proposed('remember', text, facts=[name_fact(text)]))
    assert flow.memory.list_for_user('alice') == []
    assert not flow.memory.policy_state('alice')['enabled']


@pytest.mark.parametrize('field,value', [
    ('user_id', 'bob'), ('text', '나는 다른 사람이야'),
    ('request_id', 'different-request'),
    ('session_instance_id', 'different-session'),
])
def test_review_stamp_cannot_be_reused_for_another_source(field, value):
    source = dict(origin('난 김민재야'), user_id='alice')
    fact = name_fact(source['text'])
    attach_source_review(source, [fact])
    assert source_review_matches(fact, source)
    changed = dict(source, **{field: value})
    assert not source_review_matches(fact, changed)
    assert not source_review_matches(dict(fact, value='다른이름'), source)


@pytest.mark.parametrize('message', [
    '기억했어요. 민재님, 반가워요!',
    '기억할게요. 민재님, 반가워요!',
    '저장해 둘게요. 민재님, 반가워요!',
])
def test_auto_reply_removes_storage_claims_not_conversation(setup, message):
    flow, _reviewer = setup
    flow.enable()
    text = '난 김민재야'
    result = flow.say(
        text, proposed('remember', text, facts=[name_fact(text)]),
        message=message,
    )
    assert result.decision.message == '민재님, 반가워요!'
    assert len(flow.memory.list_for_user('alice')) == 1


def test_auto_reply_with_only_a_claim_uses_neutral_nonstorage_reply(setup):
    flow, _reviewer = setup
    flow.enable()
    text = '난 김민재야'
    result = flow.say(
        text, proposed('remember', text, facts=[name_fact(text)]),
        message='기억했어요!',
    )
    assert result.decision.message == '말씀해 주셔서 고마워요.'


def test_reset_during_semantic_review_cannot_save_into_new_generation(setup):
    flow, reviewer = setup
    flow.enable()
    reviewer.callback = lambda: flow.conversations.reset('alice', 'room')
    text = '난 김민재야'
    with pytest.raises(ConversationChangedError):
        flow.say(text, proposed('remember', text, facts=[name_fact(text)]))
    assert flow.memory.list_for_user('alice') == []


def test_semantic_review_usage_is_added_before_persistence(setup):
    flow, _reviewer = setup
    flow.enable()
    original = flow.provider.complete

    def measured(*args, **kwargs):
        response = original(*args, **kwargs)
        response.usage = ProviderUsage(
            input_tokens=10, output_tokens=4, total_tokens=14,
        )
        return response

    flow.provider.complete = measured
    text = '난 김민재야'
    result = flow.say(
        text, proposed('remember', text, facts=[name_fact(text)]),
    )
    expected = ProviderUsage(input_tokens=13, output_tokens=6, total_tokens=19)
    assert result.provider_result.usage == expected
    assert result.provider_result.latency_ms > 0
    cached = flow.runtime.handle(flow.requests[-1])
    assert cached.provider_result.usage == expected


def test_semantic_partial_acceptance_does_not_partially_save_batch(setup):
    flow, reviewer = setup
    flow.enable()
    text = '난 김민재야. 박지민은 다른 사람이야'
    reviewer.rewrite = lambda p: p.update(facts=p['facts'][:1])
    facts = [
        name_fact('난 김민재야'), name_fact('박지민은 다른 사람이야', '박지민'),
    ]
    result = flow.say(text, proposed('remember', text, facts=facts))
    assert len(reviewer.calls) == 1
    assert result.decision.message == '대화 답변이에요.'
    assert flow.memory.list_for_user('alice') == []
