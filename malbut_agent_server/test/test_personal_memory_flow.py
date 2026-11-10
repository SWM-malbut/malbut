"""Exercise consent and memory changes through real conversation commits."""

import copy
import json
import sqlite3
import time

import pytest

from malbut_agent_server.conversation import (
    ConversationChangedError, ConversationStateError, SQLiteConversationStore,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import (
    AgentOrchestrator, MemoryChangedError,
)
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    AgentDecision, AgentRequest, ProviderResult,
)


def proposed(operation, text, *, facts=(), targets=(), query=''):
    """Build an untrusted proposal independently of policy implementation."""
    return {
        'operation': operation, 'facts': list(facts),
        'target_ids': list(targets), 'query': query, 'evidence': text,
    }


def pet_fact(text, value='두부', subject='강아지'):
    """Describe one pet fact grounded in its exact source utterance."""
    return {
        'kind': 'pet', 'subject': subject, 'attribute': 'name',
        'value': value, 'evidence': text,
    }


class ScriptProvider(AgentProvider):
    """Expose received context and emit controlled untrusted proposals."""

    supports_memory = True

    def __init__(self):
        """Keep test-controlled responses separate from observed inputs."""
        self.proposal = None
        self.callback = None
        self.message = '대화 답변이에요.'
        self.calls = []

    def complete(
        self, request, memories, conversation_turns, tools,
        conversation_summary=None, *, memory_context=None,
    ):
        """Record only supplied facts, without simulating store behavior."""
        self.calls.append({
            'request': request,
            'memories': copy.deepcopy(memories),
            'history': copy.deepcopy(conversation_turns),
            'summary': copy.deepcopy(conversation_summary),
            'context': copy.deepcopy(memory_context),
        })
        if self.callback:
            self.callback()
        return ProviderResult(
            decision=AgentDecision(type='message', message=self.message),
            provider='fixture', model='script-memory', latency_ms=0.0,
            memory_proposal=copy.deepcopy(self.proposal),
            memory_supported=True,
        )


class Flow:
    """Run actual Agent turns against a shared persistent database."""

    def __init__(self, path, provider=None):
        """Create runtime stores with an adjustable session clock."""
        self.path = str(path)
        self.now = time.time()
        self.memory = SQLiteMemoryStore(self.path)
        self.conversations = SQLiteConversationStore(
            self.path, ttl_seconds=60, clock=lambda: self.now,
        )
        self.provider = provider or ScriptProvider()
        self.runtime = AgentOrchestrator(
            self.provider, self.memory, self.conversations, SafetyPolicy(),
        )
        self.counter = 0
        self.requests = []

    def close(self):
        """Close only this test's database handles."""
        self.conversations.close()
        self.memory.close()

    def say(
        self, text, proposal=None, *, user='alice', conversation='room',
        callback=None, message='대화 답변이에요.',
    ):
        """Submit one new turn with an optional scripted provider response."""
        self.conversations.create(user, conversation)
        self.counter += 1
        if isinstance(self.provider, ScriptProvider):
            self.provider.proposal = proposal
            self.provider.callback = callback
            self.provider.message = message
        request = AgentRequest.from_dict({
            'request_id': f'request-{self.counter}', 'user_id': user,
            'conversation_id': conversation, 'turn_id': f'turn-{self.counter}',
            'utterance': text, 'robot_state': {}, 'available_tools': [],
        })
        self.requests.append(request)
        return self.runtime.handle(request)

    def enable(self, user='alice', conversation='room'):
        """Grant consent through the actual question and answer flow."""
        prompt = self.say('개인화 켜줘', user=user, conversation=conversation)
        assert prompt.decision.type == 'clarification'
        self.say('네', user=user, conversation=conversation)
        assert self.memory.policy_state(user)['enabled'] is True

    def remember(self, value='두부', subject='강아지', user='alice'):
        """Save a current direct statement using an untrusted proposal."""
        text = f'우리 {subject} 이름은 {value}야'
        return self.say(text, proposed(
            'remember', text, facts=[pet_fact(text, value, subject)],
        ), user=user)


@pytest.fixture
def flow(tmp_path):
    """Keep runtime lifetime bounded to one test database."""
    current = Flow(tmp_path / 'dialogue.sqlite3')
    yield current
    current.close()


def supplied_text(call):
    """Collect model-readable history, summary and memory content."""
    values = [record.content for record in call['memories']]
    values.extend(
        part for turn in call['history']
        for part in (turn.user_content, turn.assistant_content)
    )
    if call['summary']:
        values.append(call['summary'].content)
    values.append(json.dumps(call['context'], ensure_ascii=False))
    return '\n'.join(values)


def origin(text, turn='external'):
    """Build a trusted source for an independent concurrent writer."""
    return {
        'conversation_id': 'control-room', 'session_instance_id': 'control',
        'generation': 1, 'turn_id': turn, 'request_id': turn, 'text': text,
    }


def test_pet_name_save_checks_copula_and_preserves_a_confirmed_final_i(flow):
    flow.enable()

    def save(text, value):
        return flow.say(text, proposed('remember', text, facts=[pet_fact(text, value, '고양이')]))

    text = '우리 고양이 이름은 방울밤이야. 고양이 이름으로 기억해 줘.'
    assert save(text, '방울밤이').decision.type == 'clarification'
    assert flow.memory.list_for_user('alice') == []
    assert flow.provider.calls[-1]['context']['pet_name_spelling']['name_before_copula'] == '방울밤'
    assert '기억했어요' in save(text, '방울밤').decision.message
    assert flow.memory.list_for_user('alice')[0].metadata['fact']['value'] == '방울밤'
    record = flow.memory.list_for_user('alice')[0]
    flow.memory.remove_facts('alice', [record.id])

    assert '기억했어요' in save('우리 고양이 이름은 송이입니다. 기억해 줘.', '송이').decision.message
    assert '기억했어요' in save('우리 고양이 이름은 송이야. 기억해 줘.', '송이').decision.message
    assert flow.memory.list_for_user('alice')[0].metadata['fact']['value'] == '송이'


def test_explicit_pet_correction_binds_the_exact_live_slot_despite_an_unknown_model_id(flow):
    flow.enable()
    flow.remember('두부', subject='고양이')
    flow.remember('두부', subject='강아지')
    text = '우리 반려묘 이름을 두부에서 송이로 정정해 줘.'
    wrong_id = '102a4d06-2cec-455f-9cfc-4ec7506c3774'
    before = len(flow.provider.calls)
    answer = flow.say(text, proposed('correct', text, targets=[wrong_id], facts=[pet_fact(text, '송이', '반려묘')]))
    assert '정정했어요' in answer.decision.message
    assert len(flow.provider.calls) == before + 1
    assert [r.metadata['fact']['subject'] for r in flow.provider.calls[-1]['memories']] == ['고양이']
    facts = [r.metadata['fact'] for r in flow.memory.list_for_user('alice')]
    assert {(f['subject'], f['value']) for f in facts} == {('고양이', '송이'), ('강아지', '두부')}


@pytest.mark.parametrize('case', [
    'old mismatch', 'wrong value', 'wrong subject', 'other active id', 'other user id',
    'multiple targets', 'ambiguous slots', 'quoted', 'negative', 'other owner', 'indirect new name',
])
def test_explicit_pet_correction_never_widens_authority(flow, case):
    flow.enable()
    flow.remember('보리', subject='고양이')
    flow.remember('두부', subject='강아지')
    flow.enable(user='bob')
    flow.remember('가을', user='bob', subject='고양이')
    saved = {r.metadata['fact']['subject']: r for r in flow.memory.list_for_user('alice')}
    cat, dog = saved['고양이'], saved['강아지']
    other = flow.memory.list_for_user('bob')[0]
    if case == 'ambiguous slots':
        flow.remember('여름', subject='반려묘')
    text = '우리 고양이 이름을 보리에서 모카로 정정해 줘.'
    if case == 'old mismatch':
        text = text.replace('보리', '옛별')
    elif case == 'indirect new name':
        text = text.replace('모카로', '원래대로')
    elif case == 'negative':
        text = text.replace('정정해 줘.', '정정하지 마.')
    elif case == 'quoted':
        text = '예시로 “' + text + '”라고 읽어 줘.'
    elif case == 'other owner':
        text = text.replace('우리', '친구네')
    targets = {'other active id': [dog.id], 'other user id': [other.id],
               'multiple targets': [cat.id, dog.id]}.get(case, ['unknown-id'])
    value = {'wrong value': '보리', 'indirect new name': '원래대'}.get(case, '모카')
    fact = pet_fact(text, value, '강아지' if case == 'wrong subject' else '고양이')
    initial = set((r.id, r.content) for u in ('alice', 'bob') for r in flow.memory.list_for_user(u))
    flow.say(text, proposed('correct', text, targets=targets, facts=[fact]))
    assert {(r.id, r.content) for u in ('alice', 'bob') for r in flow.memory.list_for_user(u)} == initial


def test_pet_name_lookup_scopes_both_inputs_and_deletion(flow):
    flow.enable()
    flow.remember('초코', subject='강아지')
    cat = '우리 고양이 이름은 보리야. 기억해 줘.'
    flow.say(cat, proposed('remember', cat, facts=[pet_fact(cat, '보리', '고양이')]))
    # Some older unstructured records contain multiple facts in one sentence.
    flow.memory.add('alice', '고양이 이름은 가을 강아지 이름은 초코', kind='pet')
    lookup = '전에 말한 우리 반려묘 이름이 뭐였지?'
    flow.say(lookup, conversation='read')
    call = flow.provider.calls[-1]
    assert [r.metadata['fact']['value'] for r in call['memories']] == ['보리']
    assert [r['fact']['value'] for r in call['context']['memories']] == ['보리']
    assert '초코' not in supplied_text(call)
    stored = call['memories'][0]
    text = '저장한 우리 고양이 이름만 삭제해 줘.'
    flow.say(text, proposed('forget', text, query='고양이 이름', targets=[stored.id]))
    flow.memory.add('alice', '고양이 이름은 가을, 강아지 이름은 초코', kind='fact')
    count = len(flow.provider.calls)
    answer = flow.say('우리 고양이 이름을 기억해?', conversation='read')
    assert '고양이 이름을 확인할 수 없' in answer.decision.message
    assert len(flow.provider.calls) == count
    assert '보리' not in answer.decision.message and '초코' not in answer.decision.message
    flow.say('우리 반려견 이름 좀 알려 줘', conversation='dog')
    assert [r.metadata['fact']['value'] for r in flow.provider.calls[-1]['memories']] == ['초코']
    flow.memory.add('alice', '고양이 이름은 해솔', kind='fact')
    flow.say('우리 고양이 이름이 뭐였어?', conversation='legacy')
    assert [r.content for r in flow.provider.calls[-1]['memories']] == ['고양이 이름은 해솔']


@pytest.mark.parametrize('subject', ['첫째 고양이', '둘째반려묘', '세 번째 고양이'])
def test_general_pet_question_finds_single_numbered_pet_only(flow, subject):
    flow.enable()
    flow.remember('초코', subject='첫째 강아지')
    flow.say('친구네 고양이 이름은 별이래.')
    flow.memory.add('alice', '고양이 이름은 옛별', kind='pet')
    flow.remember('두부', subject=subject)
    before = len(flow.provider.calls)
    flow.say('우리 반려묘 이름이 뭐야?')
    assert len(flow.provider.calls) == before + 1
    call = flow.provider.calls[-1]
    assert [r.metadata['fact']['subject'] for r in call['memories']] == [subject]
    assert [r['fact']['value'] for r in call['context']['memories']] == ['두부']
    assert all(word not in supplied_text(call) for word in ('초코', '별이래', '옛별'))
    flow.memory.remove_facts('alice', [call['memories'][0].id])
    assert '확인할 수 없' in flow.say('우리 고양이 이름이 뭐야?').decision.message


def test_generic_pet_request_cannot_pick_between_two_pets_of_the_same_species(flow):
    flow.enable()
    flow.remember('두부', subject='첫째 고양이')
    flow.remember('모카', subject='둘째 반려묘')
    flow.remember('초코', subject='강아지')
    count = len(flow.provider.calls)
    answer = flow.say('우리 고양이 이름이 뭐야?', conversation='lookup')
    assert answer.decision.type == 'clarification' and '고양이' in answer.decision.message
    assert len(flow.provider.calls) == count and '초코' not in answer.decision.message
    initial = {r.id: r.content for r in flow.memory.list_for_user('alice')}
    first_cat = next(key for key, content in initial.items() if '두부' in content)
    edit = '우리 고양이 이름을 두부에서 송이로 정정해 줘.'
    answer = flow.say(edit, proposed('correct', edit, targets=[first_cat],
                                     facts=[pet_fact(edit, '송이', '고양이')]))
    assert answer.decision.type == 'clarification'
    assert {r.id: r.content for r in flow.memory.list_for_user('alice')} == initial


def test_pet_deletion_fences_only_older_evidence_of_the_same_pet(flow):
    flow.enable()
    flow.memory.add('alice', '고양이 이름은 옛별', kind='pet')
    flow.memory.add('alice', '반려견 이름은 햇살', kind='fact')
    flow.remember('하루', subject='고양이')
    record = next(r for r in flow.memory.list_for_user('alice') if r.metadata.get('fact'))
    flow.memory.remove_facts('alice', [record.id])
    unrelated = flow.memory.add('alice', '나는 모과를 좋아해', kind='preference')
    flow.memory.remove_facts('alice', [unrelated.id])
    assert '확인할 수 없' in flow.say('우리 반려묘 이름을 기억해?', conversation='cat').decision.message
    flow.say('우리 강아지 이름이 뭐였어?', conversation='dog')
    assert [r.content for r in flow.provider.calls[-1]['memories']] == ['반려견 이름은 햇살']
    flow.remember('새봄', subject='고양이')
    flow.say('우리 고양이 이름은 뭐야?', conversation='new-cat')
    assert [r.metadata['fact']['value'] for r in flow.provider.calls[-1]['memories']] == ['새봄']


@pytest.mark.parametrize('text', [
    '우리 고양이 이름 기억해.', '예시로 “우리 고양이 이름을 기억해?”라고 말해 봐.',
    '우리 고양이 이름하고 강아지 이름이 뭐였지?', '친구 고양이 이름 좀 알려 줘.',
    '우리 고양이 이름은 말하지 마.', '우리 고양이 이름을 수정해 줘.',
])
def test_other_pet_requests_remain_on_their_normal_route(flow, text):
    flow.enable()
    flow.say(text, conversation='control')
    context = flow.provider.calls[-1]['context']
    assert 'pet_name_evidence_only' not in context and 'pet_name_spelling' not in context


def test_explicit_save_asks_consent_then_recalls_in_new_session(flow):
    """Consent approves the original save without another model call."""
    text = '우리 강아지 이름은 두부야. 기억해줘'
    response = flow.say(text, proposed(
        'remember', text, facts=[pet_fact(text)],
    ))
    original_request = flow.requests[-1]
    assert response.decision.type == 'clarification'
    assert '자동으로 저장' in response.decision.message
    assert flow.memory.list_for_user('alice') == []
    call_count = len(flow.provider.calls)
    confirmed = flow.say('네')
    assert '기억했어요' in confirmed.decision.message
    assert len(flow.provider.calls) == call_count
    stored = flow.memory.list_for_user('alice')[0]
    assert (
        stored.metadata['source']['request_id'] == original_request.request_id
    )
    assert stored.metadata['source']['text'] == text
    question = '강아지 이름을 기억하고 있어?'
    recall = flow.say(question, proposed(
        'recall', question, query='강아지 이름',
    ), conversation='later-session')
    assert '두부' in recall.decision.message
    assert flow.provider.calls[-1]['memories'][0].id == stored.id


def test_default_off_does_not_store_ordinary_statement(flow):
    """A personal fact without consent remains ordinary conversation."""
    result = flow.remember()
    assert result.decision.type == 'message'
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice')['enabled'] is False
    assert flow.provider.calls[-1]['memories'] == []


@pytest.mark.parametrize('text', [
    '친구가 우리 강아지 이름은 두부야 라고 말했어',
    '예시로 "우리 강아지 이름은 두부야"라고 적어줘',
    '만약 우리 강아지 이름은 두부야 라고 한다면?',
])
def test_quoted_or_hypothetical_facts_are_not_stored(flow, text):
    """Even a structurally valid proposal needs a direct current source."""
    flow.enable()
    flow.say(text, proposed('remember', text, facts=[pet_fact(text)]))
    assert flow.memory.list_for_user('alice') == []


def test_source_before_consent_is_suppressed_after_deletion(flow):
    """The pre-consent source turn also receives the new fact dependency."""
    text = '우리 강아지 이름은 두부야. 기억해줘'
    flow.say(text, proposed('remember', text, facts=[pet_fact(text)]))
    source_request = flow.requests[-1]
    flow.say('네')
    record = flow.memory.list_for_user('alice')[0]
    delete_text = '강아지 이름 기억 삭제해줘'
    flow.say(delete_text, proposed(
        'forget', delete_text, targets=[record.id], query='강아지 이름',
    ))
    flow.say('앞에서 이야기한 내용이 있니?')
    assert '두부' not in supplied_text(flow.provider.calls[-1])
    with pytest.raises(MemoryChangedError):
        flow.runtime.handle(source_request)


def test_correction_and_deletion_remove_old_raw_and_summary_context(flow):
    """Fact dependencies cover source turns, derived answers and summaries."""
    flow.enable()
    flow.remember()
    source_request = flow.requests[-1]
    old = flow.memory.list_for_user('alice')[0]
    flow.say('강아지 이름은?', message='강아지 이름은 두부예요.')
    derived_request = flow.requests[-1]
    for index in range(12):
        flow.say(f'오늘 할 이야기 {index}')
    assert flow.conversations.get_summary('alice', 'room') is not None
    text = '강아지 이름을 보리로 정정해줘'
    corrected = flow.say(text, proposed(
        'correct', text, targets=[old.id], query='강아지 이름',
        facts=[pet_fact(text, '보리')],
    ))
    assert '정정했어요' in corrected.decision.message
    current = flow.memory.list_for_user('alice')[0]
    assert current.id != old.id
    flow.say('오늘은 다른 이야기를 하자')
    assert '두부' not in supplied_text(flow.provider.calls[-1])
    assert flow.provider.calls[-1]['summary'] is not None
    for request in (source_request, derived_request):
        with pytest.raises(MemoryChangedError):
            flow.runtime.handle(request)
    text = '강아지 이름 기억을 삭제해줘'
    flow.say(text, proposed(
        'forget', text, targets=[current.id], query='강아지 이름',
    ))
    flow.say('앞의 대화에 무슨 내용이 있었지?')
    assert '보리' not in supplied_text(flow.provider.calls[-1])
    assert flow.memory.list_for_user('alice') == []


def test_user_isolation_includes_freshness_and_candidates(flow):
    """Another user's changes never populate or invalidate this user's turn."""
    flow.enable()
    flow.remember()
    alice = flow.say('강아지 이름이 뭐지?')
    alice_revision = flow.memory.policy_state('alice')['revision']
    flow.enable(user='bob')
    flow.remember('초코', user='bob')
    assert flow.memory.policy_state('alice')['revision'] == alice_revision
    assert alice.to_dict()['request_id'] == alice.request_id
    flow.say('강아지 이름 기억 조회', proposed(
        'recall', '강아지 이름 기억 조회', query='강아지 이름',
    ), user='bob')
    assert '두부' not in supplied_text(flow.provider.calls[-1])
    assert '초코' in supplied_text(flow.provider.calls[-1])
    alice_id = flow.memory.list_for_user('alice')[0].id
    result = flow.say('강아지 이름 기억 삭제', proposed(
        'forget', '강아지 이름 기억 삭제', targets=[alice_id],
        query='강아지 이름',
    ), user='bob')
    assert result.decision.type == 'clarification'
    assert flow.memory.list_for_user('alice')[0].id == alice_id


@pytest.mark.parametrize('when', ['during_provider', 'before_commit'])
def test_concurrent_disable_blocks_candidate_and_answer(
    flow, monkeypatch, when,
):
    """Inference and the final commit boundary both recheck consent."""
    flow.enable()
    other = SQLiteMemoryStore(flow.path)

    def disable():
        other.set_personalization('alice', False, origin('개인화 중단'))

    if when == 'before_commit':
        complete = flow.conversations.complete_turn

        def changed(*args, **kwargs):
            disable()
            return complete(*args, **kwargs)

        monkeypatch.setattr(flow.conversations, 'complete_turn', changed)
    text = '우리 강아지 이름은 두부야'
    try:
        with pytest.raises(MemoryChangedError):
            flow.say(text, proposed('remember', text, facts=[pet_fact(text)]),
                     callback=disable if when == 'during_provider' else None)
        assert flow.memory.list_for_user('alice') == []
        assert not flow.memory.policy_state('alice')['enabled']
        snapshot = flow.conversations.snapshot('alice', 'room')
        assert all(turn.request_id != flow.requests[-1].request_id
                   for turn in snapshot.turns)
    finally:
        other.close()


def test_database_failure_rolls_back_fact_and_completed_answer(
    flow, monkeypatch,
):
    """A failure after applying effects leaves no unreported memory write."""
    flow.enable()
    prior_revision = flow.memory.policy_state('alice')['revision']

    def fail_summary(*args, **kwargs):
        raise sqlite3.OperationalError('injected commit failure')

    monkeypatch.setattr(
        flow.conversations, '_advance_summary_locked', fail_summary,
    )
    with pytest.raises(sqlite3.OperationalError, match='injected'):
        flow.remember()
    assert flow.memory.list_for_user('alice') == []
    assert flow.memory.policy_state('alice')['revision'] == prior_revision
    assert all(turn.request_id != flow.requests[-1].request_id
               for turn in flow.conversations.snapshot('alice', 'room').turns)


def test_restart_keeps_consent_memories_and_stale_cache_checks(flow):
    """A new runtime can recall persisted facts and refuses outdated replay."""
    flow.enable()
    flow.remember()
    old_request = flow.requests[-1]
    stored = flow.memory.list_for_user('alice')[0]
    reopened = Flow(flow.path)
    reopened.counter = 100
    try:
        text = '강아지 이름 기억 조회'
        result = reopened.say(text, proposed('recall', text, query='강아지 이름'),
                              conversation='after-restart')
        assert '두부' in result.decision.message
        assert reopened.memory.policy_state('alice')['enabled']
        reopened.memory.remove_facts('alice', [stored.id])
        with pytest.raises(MemoryChangedError):
            reopened.runtime.handle(old_request)
    finally:
        reopened.close()


@pytest.mark.parametrize('change', ['reset', 'expire'])
def test_invalidated_consent_question_cannot_enable_personalization(
    flow, change,
):
    """A bare yes only answers a currently valid question in its generation."""
    flow.say('개인화 켜줘')
    if change == 'reset':
        flow.conversations.reset('alice', 'room')
        flow.say('네')
    else:
        flow.now += 61
        request = AgentRequest.from_dict({
            'request_id': 'expired-answer', 'user_id': 'alice',
            'conversation_id': 'room', 'turn_id': 'expired-turn',
            'utterance': '네', 'robot_state': {}, 'available_tools': [],
        })
        with pytest.raises(ConversationStateError):
            flow.runtime.handle(request)
        flow.say('네', conversation='new-room')
    assert not flow.memory.policy_state('alice')['enabled']
    assert flow.memory.list_for_user('alice') == []


def test_reset_during_inference_blocks_late_automatic_store(flow):
    """Conversation generation validation happens before applying effects."""
    flow.enable()
    text = '우리 강아지 이름은 두부야'
    with pytest.raises(ConversationChangedError):
        flow.say(text, proposed('remember', text, facts=[pet_fact(text)]),
                 callback=lambda: flow.conversations.reset('alice', 'room'))
    assert flow.memory.list_for_user('alice') == []


def test_ambiguous_fact_change_waits_for_yes_before_replacing(flow):
    """An explicit conflicting save does not overwrite an existing fact."""
    flow.enable()
    flow.remember()
    old = flow.memory.list_for_user('alice')[0]
    text = '우리 강아지 이름은 보리야. 기억해줘'
    conflict = flow.say(text, proposed(
        'remember', text, facts=[pet_fact(text, '보리')],
    ))
    assert conflict.decision.type == 'clarification'
    assert flow.memory.list_for_user('alice')[0].id == old.id
    confirmed = flow.say('네')
    assert '정정했어요' in confirmed.decision.message
    new = flow.memory.list_for_user('alice')[0]
    assert new.metadata['fact']['value'] == '보리'
    assert old.id in flow.memory.invalidated_ids('alice')


def test_ambiguous_delete_uses_numbered_target_without_guessing(flow):
    """A numbered answer affects exactly one offered user-owned memory."""
    flow.enable()
    flow.remember('두부', subject='첫째 강아지')
    flow.remember('보리', subject='둘째 강아지')
    text = '강아지 이름 기억 삭제해줘'
    question = flow.say(text, proposed('forget', text, query='강아지 이름'))
    assert question.decision.type == 'clarification'
    assert len(flow.memory.list_for_user('alice')) == 2
    with sqlite3.connect(flow.path) as connection:
        pending = json.loads(connection.execute(
            'SELECT proposal_json FROM memory_questions WHERE user_id=?',
            ('alice',),
        ).fetchone()[0])
    selected = pending['target_ids'][0]
    result = flow.say('1')
    assert '삭제했어요' in result.decision.message
    assert len(flow.memory.list_for_user('alice')) == 1
    assert selected in flow.memory.invalidated_ids('alice')


def test_new_direct_statement_can_be_saved_after_old_fact_deleted(flow):
    """Deletion blocks stale sources without forbidding new explicit facts."""
    flow.enable()
    flow.remember()
    old = flow.memory.list_for_user('alice')[0]
    text = '강아지 이름 기억 삭제해줘'
    flow.say(text, proposed('forget', text, targets=[old.id], query='강아지 이름'))
    flow.remember()
    new = flow.memory.list_for_user('alice')[0]
    assert new.id != old.id
    assert new.metadata['fact']['value'] == '두부'


def test_stale_completed_object_and_retry_cannot_release_old_text(flow):
    """Response serialization and durable replay both verify current policy."""
    flow.enable()
    flow.remember()
    response = flow.say('강아지 이름을 알려줘', message='강아지 이름은 두부예요.')
    request = flow.requests[-1]
    call_count = len(flow.provider.calls)
    replay = flow.runtime.handle(request)
    assert replay.to_dict()['decision']['message'] == '강아지 이름은 두부예요.'
    assert len(flow.provider.calls) == call_count
    stored = flow.memory.list_for_user('alice')[0]
    flow.memory.remove_facts('alice', [stored.id])
    with pytest.raises(MemoryChangedError):
        response.to_dict()
    with pytest.raises(MemoryChangedError):
        flow.runtime.handle(request)
    assert len(flow.provider.calls) == call_count


def test_failed_consent_commit_keeps_original_question_pending(
    flow, monkeypatch,
):
    """Consent and the delayed original save cannot commit independently."""
    text = '우리 강아지 이름은 두부야. 기억해줘'
    flow.say(text, proposed('remember', text, facts=[pet_fact(text)]))
    advance = flow.conversations._advance_summary_locked

    def fail_summary(*args, **kwargs):
        raise sqlite3.OperationalError('consent commit failure')

    monkeypatch.setattr(
        flow.conversations, '_advance_summary_locked', fail_summary,
    )
    with pytest.raises(sqlite3.OperationalError, match='consent commit'):
        flow.say('네')
    assert not flow.memory.policy_state('alice')['enabled']
    assert flow.memory.list_for_user('alice') == []
    monkeypatch.setattr(flow.conversations, '_advance_summary_locked', advance)
    flow.say('네')
    assert flow.memory.policy_state('alice')['enabled']
    stored = flow.memory.list_for_user('alice')[0]
    assert stored.metadata['fact']['value'] == '두부'


def test_old_source_is_not_extracted_from_history_after_later_consent(flow):
    """Later consent does not authorize backfilling old utterances."""
    old_text = '우리 강아지 이름은 두부야'
    flow.say(old_text)
    flow.enable()
    flow.say('안녕', proposed(
        'remember', '안녕', facts=[pet_fact(old_text)],
    ))
    assert flow.memory.list_for_user('alice') == []


def test_intervening_turn_expires_pending_consent_answer(flow):
    """A later bare yes cannot silently answer an obsolete memory question."""
    flow.say('개인화 켜줘')
    flow.say('오늘 날씨 이야기를 하자')
    flow.say('네')
    assert not flow.memory.policy_state('alice')['enabled']


def test_legacy_personalized_cache_requires_new_consent(flow):
    """Missing migration stamps never authorize replay of personalized text."""
    flow.enable()
    flow.memory.add('alice', '강아지 이름은 두부')
    flow.say('강아지 이름이 뭐였지?', message='강아지 이름은 두부예요.')
    old_request = flow.requests[-1]
    with sqlite3.connect(flow.path) as connection:
        connection.execute('DELETE FROM memory_policy_state')
        connection.execute('DELETE FROM memory_turn_state')
    assert flow.memory.policy_state('alice')['enabled'] is False
    with pytest.raises(MemoryChangedError):
        flow.runtime.handle(old_request)
    flow.say('오늘은 다른 이야기를 해보자')
    assert '두부' not in supplied_text(flow.provider.calls[-1])


def test_unrelated_ordinary_history_is_preserved(flow):
    """Deleting one fact need not discard earlier unrelated conversation."""
    flow.enable()
    flow.say('오늘은 산책 대신 집에서 쉬자', message='집에서 쉬는 이야기예요.')
    flow.remember()
    stored = flow.memory.list_for_user('alice')[0]
    text = '강아지 이름 기억 삭제해줘'
    flow.say(text, proposed(
        'forget', text, targets=[stored.id], query='강아지 이름',
    ))
    flow.say('방금 하던 이야기를 이어가자')
    model_text = supplied_text(flow.provider.calls[-1])
    assert '집에서 쉬자' in model_text
    assert '두부' not in model_text


def test_mock_korean_end_to_end_memory_smoke(tmp_path):
    """The existing development provider exercises the real Korean route."""
    flow = Flow(tmp_path / 'mock.sqlite3', provider=MockProvider())
    try:
        initial = flow.say('우리 강아지 이름은 두부야. 기억해줘')
        assert initial.decision.type == 'clarification'
        flow.say('네')
        assert len(flow.memory.list_for_user('alice')) == 1
        remembered = flow.say('강아지 이름이 뭐였지?', conversation='later')
        assert '두부' in remembered.decision.message
    finally:
        flow.close()
