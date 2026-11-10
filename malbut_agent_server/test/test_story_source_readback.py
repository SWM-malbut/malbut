"""Source identities for a directly routed story question and its later invalidation."""

import hashlib
import json
from unittest.mock import patch

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.schemas import AgentDecision, ProviderResult, SpeechAgentRequest, ValidationError
from test_story_orchestration import MemoryAwareProvider


class ReadbackSession:
    def __init__(self, path):
        self.backend = MemoryAwareProvider()
        with patch('malbut_agent_server.factory.build_provider', return_value=self.backend):
            self.runtime = build_orchestrator(Settings(provider='mock', database_path=str(path)),
                                              story_extractor=None)
        self.runtime.story_provider.on_demand_speech = True
        self.store = self.runtime.story_memory.store

    def turn(self, utterance, room, *, answer=None, user='alice', key=None):
        self.runtime.conversation_store.create(user, room)
        key = key or room
        request = SpeechAgentRequest.from_dict({
            'conversation_id': room, 'user_id': user, 'request_id': 'request-' + key,
            'turn_id': 'turn-' + key, 'utterance': utterance, 'robot_state': {}, 'available_tools': [],
        })
        if answer is None:
            result = self.runtime.handle(request)
        else:
            reply = ProviderResult(AgentDecision('message', answer), 'offline', 'test', 0, memory_supported=True)
            with patch.object(self.backend, 'complete', return_value=reply):
                result = self.runtime.handle(request)
        return request, result

    def seed(self, title, claim):
        self.store.set_enabled('alice', True, external_consent=True)
        source, _ = self.turn(claim, 'source')
        story_id = self.settle(title, claim)
        return story_id, source

    def settle(self, title, claim, story_id=None):
        job = self.store.claim('alice')
        source = next(item for item in job['sources'] if item['role'] == 'user' and item['text'] == claim)
        entry = {'kind': 'experience', 'status': 'stated', 'actor': 'user', 'text': claim,
                 'evidence': [{'source_id': source['id'], 'quote': claim}]}
        self.store.commit(job, [{'story_id': story_id, 'title': title, 'current': [entry], 'episode': [entry]}])
        return self.store.list_stories('alice')[0]['story_id']

    def job(self, request_id, user='alice'):
        with self.store._transaction() as conn:
            return conn.execute('SELECT * FROM story_runtime_jobs WHERE user_id=? AND request_id=? '
                                'ORDER BY sequence DESC LIMIT 1', (user, request_id)).fetchone()


@pytest.fixture
def session(tmp_path):
    example = ReadbackSession(tmp_path / 'story.sqlite3')
    yield example
    example.runtime.close()


TOPIC = '자전거 모임'
ORIGINAL = '자전거 모임의 별표 이름은 보름잔이야.'
ASK = '예전에 자전거 모임의 별표 이름을 뭐로 정했는지 기억나?'
CHANGE = '자전거 모임의 별표 이름은 이제 보름잔에서 노을고리로 바뀌었어.'


@pytest.mark.parametrize('title,source,question,answer', [
    (TOPIC, ORIGINAL, ASK, '기억에 따르면 자전거 모임의 별표 이름은 ‘보름잔’이었어요.'),
    (TOPIC, ORIGINAL, ASK, '그날 골라 두신 건 달이 둥근 잔, 보름잔이랍니다.'),
    (TOPIC, ORIGINAL, '전에 말한 자전거 모임 별표 이름은 뭐였지?', '‘보름잔이’로 정했어요.'),
    ('도예 모임', '도예 모임의 작품 이름은 솔방울이야.', '예전에 도예 모임의 작품 이름을 뭐로 정했는지 기억나?',
     '오래전 공방에서 마음에 들어 하신 이름으로는 솔방울이 떠오릅니다.'),
    ('연극 모임', '연극 모임의 공연 이름은 낮잠이야.', '예전에 연극 모임의 공연 이름을 뭐로 정했는지 기억나?',
     '확인해 보니 공연 제목은 낮잠이었어요.'),
])
def test_source_read_uses_original_user_ref_independent_of_answer(session, title, source, question, answer):
    story_id, origin = session.seed(title, source)
    request, result = session.turn(question, 'read', answer=answer)
    assert result.decision.message == answer
    receipt = session.job(request.request_id)
    assert receipt['state'] == 'done'
    assert receipt['attempts'] == 0 and receipt['processed'] == 1
    binding = result.to_persisted_dict()['story_binding']['readset']
    assert len(binding) == 1 and binding[0]['story_id'] == story_id and binding[0]['entry_hashes']
    original, = json.loads(receipt['read_sources_json'])
    ref = original['ref']
    assert (ref['conversation_id'], ref['turn_id'], ref['role']) == ('source', origin.turn_id, 'user')
    assert ref['generation'] == 1 and ref['session_instance_id'] and (ref['start'], ref['end']) == (0, len(source))
    assert ref['sha256'] == hashlib.sha256(source.encode()).hexdigest()
    assert all(item['text'] != answer for item in session.store.evidence('alice', story_id))
    assert not session.store.enqueue_completed('alice', request.request_id)
    assert session.store.claim('alice') is None


@pytest.mark.parametrize('utterance', [
    '자전거 모임의 별표 이름은 노을고리로 바뀌었고 예전에 자전거 모임 이름을 뭐로 정했는지 기억나?',
    '전에 말한 자전거 모임 별표 이름은 뭐였지? 새 이름은 노을고리야.',
    '친구가 “전에 말한 자전거 모임 별표 이름은 뭐였지?”라고 물었어.',
    '예전에 민지의 자전거 모임 별표 이름을 뭐로 정했는지 기억나?',
    '자전거 모임 별표 이름이 뭐였는지 기억하지 마.',
    CHANGE,
])
def test_non_read_turn_remains_extractable(session, utterance):
    session.seed(TOPIC, ORIGINAL)
    request, _ = session.turn(utterance, 'mixed')
    assert session.job(request.request_id)['state'] == 'queued'
    job = session.store.claim('alice')
    assert any(s['role'] == 'user' and s['text'] == utterance for s in job['sources'])


def test_recovery_does_not_trust_persisted_readset_alone(session):
    session.seed(TOPIC, ORIGINAL)
    request, result = session.turn(ASK, 'read')
    mixed, _ = session.turn('새 모임을 만들었고 원래 이름이 궁금해.', 'mixed')
    with session.store._transaction(True) as conn:
        # Simulate a pre-checkpoint crash on an older version. Both real lineage
        # and a copied serialized binding still cannot grant a read-only receipt.
        conn.execute('DELETE FROM story_runtime_jobs WHERE request_id=?', (mixed.request_id,))
        conn.execute('UPDATE conversation_turns SET response_json=? WHERE request_id=?',
                     (json.dumps(result.to_persisted_dict()), mixed.request_id))
    session.store.record_reply('alice', mixed.request_id, [result.story_binding.readset[0][0]],
                               result.story_binding.policy_revision)
    session.store.recover('alice')
    assert session.job(mixed.request_id)['state'] == 'queued'
    assert session.job(request.request_id)['state'] == 'done'


@pytest.mark.parametrize('change', ['correct', 'delete', 'revoke'])
def test_source_changes_between_commit_and_after_turn(session, tmp_path, change):
    story_id, _ = session.seed(TOPIC, ORIGINAL)
    with patch.object(session.runtime.story_memory, 'after_turn'):
        read, _ = session.turn(ASK, 'read', answer='누군가가 말한 다른 이름이에요.')
    assert session.job(read.request_id)['state'] == 'done'
    if change == 'correct':
        new, _ = session.turn(CHANGE, 'change')
        assert session.job(new.request_id)['state'] == 'queued'
        session.settle(TOPIC, CHANGE, story_id)
    elif change == 'delete':
        session.store.delete_story('alice', story_id)
    else:
        session.store.set_enabled('alice', False)
    session.runtime.close()
    again = ReadbackSession(tmp_path / 'story.sqlite3')
    try:
        again.runtime.story_memory.after_turn('alice', read.request_id)
        again.store.recover('alice')
        remaining = again.store.claim('alice')
        if change == 'delete':
            # Erasure removed both the question and its dependent paraphrase.
            # A generic acknowledgement in the original source room can survive.
            assert [(item['role'], item['text']) for item in remaining['sources']] == [('assistant', '알겠어요.')]
            again.store.commit(remaining, [])
            assert again.store.claim('alice') is None
            assert again.store.list_stories('alice') == []
        else:
            assert remaining is None
        assert '누군가' not in repr(again.store.evidence('alice', story_id))
        if change == 'correct':
            assert again.store.list_stories('alice')[0]['current'][0]['text'] == CHANGE
        with pytest.raises(ValidationError):
            again.runtime.handle(read)
    finally:
        again.runtime.close()


def test_inherited_context_or_model_chosen_lookup_does_not_mark_read_only(session):
    story_id, _ = session.seed(TOPIC, ORIGINAL)
    session.turn(ASK, 'read')
    inherited, result = session.turn('이제 좀 쉬고 싶어.', 'read', key='next')
    assert session.store.reply_record('alice', inherited.request_id)['has_story_dependencies']
    assert not result.story_binding.readset
    assert session.job(inherited.request_id)['state'] == 'queued'
    session.store.commit(session.store.claim('alice'), [])

    complete = session.backend.complete

    def choose_lookup(request, memories, conversation_turns, tools, **kwargs):
        if any(tool.name == 'recall_story_memory' for tool in tools):
            decision = AgentDecision('tool_call', '기억을 확인할게요.', tool_name='recall_story_memory', arguments={})
            return ProviderResult(decision, 'offline', 'test', 0)
        return complete(request, memories, conversation_turns, tools, **kwargs)

    with patch.object(session.backend, 'complete', side_effect=choose_lookup) as model:
        request, result = session.turn('자전거 모임 이야기를 다시 들려줘.', 'model-choice')
    assert model.call_count == 2 and result.story_binding.readset[0][0] == story_id
    assert session.job(request.request_id)['state'] == 'queued'


def test_wrong_owner_cannot_receive_read_checkpoint(session):
    session.seed(TOPIC, ORIGINAL)
    session.store.set_enabled('bob', True, external_consent=True)
    request, result = session.turn(ASK, 'other-owner', user='bob')
    assert not result.story_binding.readset
    assert session.job(request.request_id, user='bob')['state'] == 'queued'
    assert ORIGINAL not in result.decision.message


def test_same_claim_with_new_source_revision_during_inference(session, tmp_path):
    story_id, _ = session.seed(TOPIC, ORIGINAL)
    other = ReadbackSession(tmp_path / 'story.sqlite3')
    complete = session.backend.complete

    def replace_source(*args, **kwargs):
        result = complete(*args, **kwargs)
        other.turn(ORIGINAL, 'another-source')
        other.settle(TOPIC, ORIGINAL, story_id)
        return result

    try:
        with patch.object(session.backend, 'complete', side_effect=replace_source):
            with pytest.raises(ValidationError, match='memory[ _]changed'):
                session.turn(ASK, 'stale')
        assert session.job('request-stale') is None
        current = other.store.list_stories('alice')[0]['current'][0]
        assert current['text'] == ORIGINAL
        assert json.loads(current['source_keys'][0])['turn_id'] == 'turn-another-source'
    finally:
        other.runtime.close()
