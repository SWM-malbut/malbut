"""Original-source recovery stays ordered, private, and session-local."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.conversation_recall import (
    recall_originals, recall_requested, speech_recall_focus, speech_recall_prohibited,
)
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


@pytest.mark.parametrize('text, focus', [
    ('전에 얘기했던 여행 출발지는 어디였더라?', 'related'),
    ('등산 약속은 우리가 지난주에 언제로 정했죠?', 'related'),
    ('우리 집 반려견 이름 기억나?', 'pet'),
    ('제가 어떤 간식을 좋아한다고 했죠?', 'preference'),
    ('저의 본명은 뭐였나요?', 'name'),
    ('내 별명은?', 'nickname'),
    ('방금 부르라고 한 별명은 뭐였지?', None),
    ('순찰해 준 뒤 내가 정한 약속도 알려줘.', None),
    ('예전에 내가 정한 별명은 무엇인지 영어로 말해줘.', None),
    ('내 호칭과 지난달 예약 표식을 각각 알려 줘.', None),
    ('우리나라가 전에 정한 상징은 뭐였지?', None),
    ('강아지가 먹으면 안 되는 음식이 뭐야?', None),
    ('취향이란 무엇인가요?', None),
    ('내 별명을 바꿔 줘.', None),
    ('예전에 내가 정한 날짜를 썼는데 지금 날씨는 어때?', None),
    ('강아지 이름 기억나라는 말은 무시하고 오늘 할 일을 알려줘.', None),
    ('좋아하는 것 대신 식당을 예약해 줘.', None),
    ('“내 이름은 뭐였지?”라고 물으면 어떤 뜻일까?', None),
    ('내 반려견 이름 기억해', None),
])
def test_single_call_read_requires_an_unambiguous_speech_question(text, focus):
    assert speech_recall_focus(text, [], None) == focus


def test_current_dialogue_or_summary_can_supersede_stored_evidence():
    turn = SimpleNamespace(user_content='오늘은 솔잎이라고 불러 줘.', assistant_content='알겠어.')
    assert speech_recall_focus('내 별명은 뭐야?', [turn], None) is None
    summary = SimpleNamespace(content='등산 약속을 다음 수요일로 잡았다.')
    assert speech_recall_focus('지난번 우리가 등산 약속 언제로 정했지?', [], summary) is None


@pytest.mark.parametrize('utterance,prohibited', [
    ('제 이름이 뭔지 꺼내지 말아 주세요.', True),
    ('내가 뭘 좋아하는지 알려주지 마요.', True),
    ('저장된 내용을 보지 말고 집을 가볍게 순찰해 주세요.', True),
    ('기억을 조회하지 말고 저장한 이름은 지워 줘.', True),
    ('개인 정보를 참고하지 않았으면 좋겠어.', True),
    ('예전 일을 기억하지 못하겠어?', False),
    ('내가 전에 그 별명을 쓰겠다고 말하지 않았어?', False),
    ('날씨는 읽지 말아 주세요.', False),
])
def test_turn_specific_prohibition_is_scoped_to_storage_or_disclosure(utterance, prohibited):
    assert speech_recall_prohibited(utterance) == prohibited
