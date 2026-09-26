"""Original-source recovery stays ordered, private, and session-local."""

from dataclasses import replace

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.conversation_recall import recall_originals, recall_requested
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.personal_memory import PersonalMemory
from malbut_agent_server.schemas import AgentRequest
from malbut_agent_server.semantic_summary import count_tokens
from malbut_agent_server.summarization import SummaryResult
from test_conversation import _begin, _response


@pytest.mark.parametrize('text,expected', [
    ('두번째안이 뭐였지?', True), ('두 번째 안으로 이어가자', True),
    ('첫 번째 방법을 다시 설명해줘', True), ('세번째 후보는 뭐야?', True),
    ('4번째 선택지를 알려줘', True), ('두 번째 거 보여줘', True),
    ('처음 제안한 거는 뭐였지?', True), ('처음 추천했던 방법을 알려줘', True),
    ('처음 보여준 거 다시 볼래', True),
    ('두 번 말해줘', False), ('두 번째 버튼을 눌러', False),
    ('두 번째 안전장치를 켜줘', False), ('처음 추천해줘', False),
    ('그거 보여줘', False),
])
def test_recall_activation_requires_an_earlier_option_reference(text, expected):
    assert recall_requested(text) is expected


@pytest.mark.parametrize('query', [
    '두번째안이 뭐였지?', '두 번째 안으로 이어가자',
    '2번째방법을 알려줘', '둘째 후보는 뭐였지?',
    '처음 제안한 거는 뭐였지?', '처음 추천했던 방법을 알려줘',
    '처음 보여준 거 다시 볼래',
])
def test_ordinal_reference_restores_original_options_before_later_noise(query):
    store = SQLiteConversationStore(':memory:', semantic_context=True)
    memory = SQLiteMemoryStore(':memory:')
    personal = PersonalMemory(memory, store)
    try:
        store.create('user-a', 'conversation-a')
        original = ('나들이 후보 세 가지를 알려줘.',
                    '첫째는 도서관, 둘째는 박물관, 셋째는 수목원이에요.')
        texts = [original,
                 ('12번째 방법을 다시 알려줘. 후보를 보여준 뒤 이어가자. ' * 50,
                  '다른 이야기예요.'),
                 ('나들이 이야기는 나중에 이어가자.', '네.')]
        for number, (user, assistant) in enumerate(texts, 1):
            turn = _begin(store, number, content=user)
            store.complete_turn(turn.token, assistant, _response(number))
        pending = _begin(store, 4)
        assert store.apply_compaction(
            pending.token, None, pending.history[:2],
            SummaryResult('나들이 장소를 검토함. 후보 순서는 빠진 요약.', '{}',
                          'openai-semantic-v1'),
        )
        store.fail_turn(pending.token)
        request = AgentRequest.from_dict({
            'request_id': 'request-4', 'turn_id': 'turn-4', 'user_id': 'user-a',
            'conversation_id': 'conversation-a', 'available_tools': [],
            'robot_state': {}, 'utterance': query,
        })
        pending = _begin(store, 4, content=query)
        snap = personal.snapshot(request, pending.token, pending.history, pending.summary)
        saved_summary = store.get_summary('user-a', 'conversation-a')
        budget = count_tokens('\n'.join(original))
        recall_originals(personal, request, pending.token, snap, token_budget=budget)
        assert [turn.ordinal for turn in snap.history] == [1, 3]
        assert snap.history[0].assistant_content == original[1]
        assert snap.context['conversation_recall']['restored_ordinals'] == [1]
        assert not snap.context['conversation_recall']['complete_archive']
        assert count_tokens(snap.history[0].user_content + '\n'
                            + snap.history[0].assistant_content) <= budget
        assert store.get_summary('user-a', 'conversation-a') == saved_summary
        store.fail_turn(pending.token)
    finally:
        memory.close()
        store.close()


def test_compacted_original_recall_redaction_and_reset_are_isolated():
    store = SQLiteConversationStore(':memory:', semantic_context=True)
    memory = SQLiteMemoryStore(':memory:')
    personal = PersonalMemory(memory, store)
    try:
        store.create('user-a', 'conversation-a')
        texts = ['민수에게 전달한 문구는 초록 우산은 입구 오른쪽에 있어 입니다.',
                 '관계없는 별도 대화입니다. ' * 100,
                 '정정: 우산은 입구 왼쪽에 있어요.']
        for number, text in enumerate(texts, 1):
            turn = _begin(store, number, content=text)
            store.complete_turn(turn.token, '알겠어요.', _response(number))
        pending = _begin(store, 4)
        store.apply_compaction(pending.token, None, pending.history[:2],
                               SummaryResult('민수에게 우산 위치를 알림.', '{}',
                                             'openai-semantic-v1'))
        store.fail_turn(pending.token)
        request = AgentRequest.from_dict({
            'request_id': 'request-4', 'turn_id': 'turn-4', 'user_id': 'user-a',
            'conversation_id': 'conversation-a', 'available_tools': [],
            'robot_state': {}, 'utterance': '아까 민수에게 말한 문구 원문을 알려줘',
        })
        pending = _begin(store, 4, content=request.utterance)
        snap = personal.snapshot(request, pending.token, pending.history, pending.summary)
        assert [t.ordinal for t in snap.history] == [3]
        saved_summary = store.get_summary('user-a', 'conversation-a')
        recall_originals(personal, request, pending.token, snap, token_budget=150)
        assert [t.ordinal for t in snap.history] == [1, 3]
        assert snap.history[0].user_content == texts[0]
        assert snap.history[-1].user_content == texts[2]
        assert snap.context['conversation_recall']['restored_ordinals'] == [1]
        assert store.get_summary('user-a', 'conversation-a') == saved_summary

        # Memory-dependent text must not return when personalization is disabled.
        store._connection.execute('''INSERT INTO memory_turn_state
            SELECT user_id, request_id, conversation_id, session_instance_id,
            generation, turn_id, 0, '["private-memory"]', created_at
            FROM conversation_turns WHERE request_id='request-1' ''')
        store._connection.commit()
        snap = personal.snapshot(request, pending.token, pending.history, pending.summary)
        snap.summary = saved_summary
        snap.history = [replace(pending.history[0])]
        recall_originals(personal, request, pending.token, snap)
        assert all('초록 우산' not in t.user_content for t in snap.history)

        store.fail_turn(pending.token)
        store.reset('user-a', 'conversation-a')
        pending = _begin(store, 5, content=request.utterance)
        snap = personal.snapshot(request, pending.token, pending.history, pending.summary)
        recall_originals(personal, request, pending.token, snap)
        assert snap.history == [] and snap.summary is None
        store.fail_turn(pending.token)
    finally:
        memory.close()
        store.close()
