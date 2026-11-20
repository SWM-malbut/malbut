"""Token budget, dead-instruction gating, and prompt-cache profile invariants."""

import json

import tiktoken

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.memory_contract import (
    MEMORY_CORE_INSTRUCTIONS,
    MEMORY_INSTRUCTIONS,
    SOURCE_REVIEW_INSTRUCTIONS,
)
from malbut_agent_server.prompting import (
    COMPANION_GUEST_INSTRUCTIONS,
    COMPANION_VERIFIED_INSTRUCTIONS,
    CONVERSATION_OPENER_INSTRUCTIONS,
    SCENE_IMAGE_INSTRUCTIONS,
    SCENE_MAP_MOTION_INSTRUCTIONS,
    WEATHER_SYSTEM_INSTRUCTIONS,
)
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
)
from malbut_agent_server.schemas import (
    AgentRequest,
    RobotState,
    SpeechAgentRequest,
)
from malbut_agent_server.tools import (
    HOMECAM_QUERY_TOOLS,
    PATROL_QUERY_TOOLS,
    PERSONAL_RECALL_TOOL,
    SPEECH_MISSION_TOOLS,
    select_tool_specs,
)


_ENCODING = tiktoken.get_encoding('o200k_base')


def _count_tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(_ENCODING.encode(text))


def _payload_tokens(payload: dict) -> int:
    if isinstance(payload.get('input'), list):
        dev = payload['input'][0]['content'][0]['text']
        user = payload['input'][1]
    else:
        dev = payload.get('instructions', '')
        user = payload.get('input', '')
    return (
        _count_tokens(payload.get('tools', []))
        + _count_tokens(dev)
        + _count_tokens(payload.get('text', {}))
        + _count_tokens(user)
    )


def _developer_text(payload: dict) -> str:
    if isinstance(payload.get('input'), list):
        return payload['input'][0]['content'][0]['text']
    return payload.get('instructions', '')


def _unpack_cached_context(payload: dict) -> dict:
    combined = ''.join(
        block['text'] for block in payload['input'][1]['content']
    )
    return json.loads(combined.split('\n', 1)[1])


def _live_tools():
    names = (
        list(SPEECH_MISSION_TOOLS)
        + ['get_weather', 'set_weather_location']
        + list(HOMECAM_QUERY_TOOLS)
        + list(PATROL_QUERY_TOOLS)
    )
    return names, select_tool_specs(names) + [PERSONAL_RECALL_TOOL]


def _speech_request(
    utterance: str,
    *,
    available_tools=(),
    scene_context=None,
    user_id: str = 'speaker',
    confirmed_homecam_read: bool = False,
) -> SpeechAgentRequest:
    return SpeechAgentRequest(
        request_id='req-1',
        user_id=user_id,
        conversation_id='conv-1',
        turn_id='turn-1',
        utterance=utterance,
        robot_state=RobotState(),
        available_tools=tuple(available_tools),
        scene_context=scene_context,
        confirmed_homecam_read=confirmed_homecam_read,
    )


def test_prompt_cache_key_isolates_distinct_profiles_and_stabilizes_same_profile() -> None:
    """Consecutive turns in one profile share one key; distinct profiles isolate."""
    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna',
        semantic_context=True, reasoning_effort='low', prompt_cache_enabled=True,
    )
    try:
        names, tools = _live_tools()
        scene = {
            'map': {'revision': 'm1', 'landmarks': [{'location': '거실', 'x': 1.0, 'y': 2.0}]},
            'image': {'available': False},
        }
        mem = {'mode': 'answer_only', 'enabled': True, 'response_settings': {}}
        turn1 = provider.build_payload(
            _speech_request('안녕 오늘 기분 어때?', available_tools=names, scene_context=scene, user_id='private-owner'),
            [], [], tools, memory_context=mem,
        )
        turn2 = provider.build_payload(
            _speech_request('저녁 뭐 먹을까?', available_tools=names, scene_context=scene, user_id='private-owner'),
            [], [], tools, memory_context=mem,
        )
        weather1 = provider.build_payload(
            _speech_request('오늘 날씨 어때?', available_tools=names, scene_context=scene, user_id='private-owner'),
            [], [], tools, memory_context=mem,
        )
        followup = provider.build_payload(
            _speech_request('오늘 날씨 어때?', available_tools=(), scene_context=scene, user_id='private-owner'),
            [], [], [], memory_context=mem,
            weather_context={'status': 'fresh', 'location': '수원시 우만동', 'current': {'condition': '맑음', 'temperature_c': 14.0}},
        )

        assert turn1['prompt_cache_key'] == turn2['prompt_cache_key']
        assert len({
            turn1['prompt_cache_key'],
            weather1['prompt_cache_key'],
            followup['prompt_cache_key'],
        }) == 3
        assert 'private-owner' not in turn1['prompt_cache_key']
        assert len(turn1['prompt_cache_key']) <= 64
    finally:
        provider.close()


def test_conversation_summary_sits_in_cached_leading_prefix_and_invalidates_on_change() -> None:
    """Summary lives before history for cache hits and invalidates when updated."""
    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna',
        semantic_context=True, prompt_cache_enabled=True,
    )
    try:
        summary_v1 = ConversationSummary(
            's1', 'speaker', 'conv-1', 'inst-1', 1, 1,
            '사용자는 어제 등산을 다녀왔고 아메리카노를 좋아함.',
            1, 10, 10, 'a' * 64, 'openai-semantic-v1', False, 1.0, 2.0,
        )
        summary_v2 = ConversationSummary(
            's1', 'speaker', 'conv-1', 'inst-1', 1, 2,
            '사용자는 오늘 라떼를 마셨고 등산을 좋아함.',
            1, 12, 12, 'b' * 64, 'openai-semantic-v1', False, 1.0, 5.0,
        )
        turn1 = ConversationTurn(
            'conv-1', 'speaker', 'inst-1', 't1', 'r1', 'fp1', 1, 11,
            '안녕', '안녕하세요!', {'decision': {'type': 'message', 'message': '안녕하세요!'}}, 3.0, 4.0,
        )
        turn2 = ConversationTurn(
            'conv-1', 'speaker', 'inst-1', 't2', 'r2', 'fp2', 1, 12,
            '커피 추천해줘', '아메리카노 어때요?', {'decision': {'type': 'message', 'message': '아메리카노 어때요?'}}, 5.0, 6.0,
        )
        mem = {'mode': 'answer_only', 'enabled': True}

        p1 = provider.build_payload(
            _speech_request('다음 질문'), [], [turn1], [],
            conversation_summary=summary_v1, memory_context=mem,
        )
        p2 = provider.build_payload(
            _speech_request('또 다음 질문'), [], [turn1, turn2], [],
            conversation_summary=summary_v1, memory_context=mem,
        )
        p3 = provider.build_payload(
            _speech_request('또 다음 질문'), [], [turn1, turn2], [],
            conversation_summary=summary_v2, memory_context=mem,
        )

        # Summary must sit in leading block[0], not the per-turn tail block.
        assert 'conversation_summary_untrusted' in p1['input'][1]['content'][0]['text']
        assert 'conversation_summary_untrusted' not in p1['input'][1]['content'][-1]['text']
        # Growing history preserves leading summary block byte-for-byte.
        assert p1['input'][1]['content'][0]['text'] == p2['input'][1]['content'][0]['text']
        # Updated summary changes leading block so old summary cannot be reused.
        assert p2['input'][1]['content'][0]['text'] != p3['input'][1]['content'][0]['text']
        assert _unpack_cached_context(p3)['conversation_summary_untrusted']['content'] == summary_v2.content
    finally:
        provider.close()


def test_dead_instructions_are_excluded_from_irrelevant_turn_profiles() -> None:
    """Mode-specific rules are omitted whenever that mode is inactive."""
    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna',
        semantic_context=True, reasoning_effort='low', prompt_cache_enabled=True,
    )
    try:
        names, tools = _live_tools()
        normal_scene = {
            'map': {'revision': 'm1', 'landmarks': [{'location': '거실', 'x': 1.0, 'y': 2.0}]},
            'image': {'available': False},
        }
        # 1. Foreground memory dialogue omits source_review rules; source_review keeps full MEMORY_INSTRUCTIONS.
        fg = provider.build_payload(
            _speech_request('난 민재야', available_tools=names, scene_context=normal_scene),
            [], [], tools, memory_context={'enabled': True},
        )
        fg_dev = _developer_text(fg)
        assert MEMORY_CORE_INSTRUCTIONS in fg_dev
        assert SOURCE_REVIEW_INSTRUCTIONS not in fg_dev
        assert COMPANION_VERIFIED_INSTRUCTIONS not in fg_dev
        assert COMPANION_GUEST_INSTRUCTIONS not in fg_dev
        assert CONVERSATION_OPENER_INSTRUCTIONS not in fg_dev
        assert SCENE_IMAGE_INSTRUCTIONS in fg_dev
        assert SCENE_MAP_MOTION_INSTRUCTIONS in fg_dev

        sr = provider.build_payload(
            AgentRequest('r', 'u', 'c', 't', '난 민재야', RobotState(), ()),
            [], [], [], memory_context={'mode': 'source_review', 'enabled': True, 'candidate_facts': []},
        )
        assert MEMORY_INSTRUCTIONS in _developer_text(sr)

        # 2. Companion guest turn includes only guest scene rules.
        guest = provider.build_payload(
            _speech_request('안녕하세요', available_tools=(), scene_context={'companion_guest': True}),
            [], [], [], memory_context={'mode': 'answer_only', 'enabled': False},
        )
        guest_dev = _developer_text(guest)
        assert COMPANION_GUEST_INSTRUCTIONS in guest_dev
        assert COMPANION_VERIFIED_INSTRUCTIONS not in guest_dev
        assert SCENE_MAP_MOTION_INSTRUCTIONS not in guest_dev
        assert WEATHER_SYSTEM_INSTRUCTIONS not in guest_dev

        # 3. Homecam follow-up omits scene and weather rules while keeping homecam trust boundary.
        homecam = provider.build_payload(
            _speech_request('홈캠 상태 어때?', available_tools=(), scene_context=normal_scene, confirmed_homecam_read=True),
            [], [], [], memory_context={
                'mode': 'answer_only', 'enabled': True,
                'homecam_query_result_untrusted': {'tool': 'get_homecam_status', 'data': {'online': True}},
            },
        )
        homecam_dev = _developer_text(homecam)
        assert 'homecam_query_result_untrusted' in homecam_dev
        assert SCENE_IMAGE_INSTRUCTIONS not in homecam_dev
        assert SCENE_MAP_MOTION_INSTRUCTIONS not in homecam_dev
        assert WEATHER_SYSTEM_INSTRUCTIONS not in homecam_dev
        assert homecam['reasoning']['effort'] == 'none'
    finally:
        provider.close()


def test_grounded_weather_followup_uses_compact_instructions_and_none_effort() -> None:
    """Speech weather Call 2 keeps all weather rules, drops scene bloat, and uses effort=none."""
    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna',
        semantic_context=True, reasoning_effort='low', prompt_cache_enabled=True,
    )
    try:
        scene = {
            'map': {'revision': 'm1', 'landmarks': [{'location': '거실', 'x': 1.0, 'y': 2.0}]},
            'image': {'available': False},
        }
        weather = {
            'status': 'fresh',
            'location': '경기도 수원시 우만동',
            'current': {'condition': '맑음', 'temperature_c': 14.2, 'time': '2026-11-20T20:00'},
            'remaining_today': {'condition': '맑음', 'precipitation_probability_max_pct': 10},
        }
        payload = provider.build_payload(
            _speech_request('오늘 날씨 어때?', available_tools=(), scene_context=scene),
            [], [], [],
            memory_context={'mode': 'answer_only', 'enabled': True},
            weather_context=weather,
        )
        dev = _developer_text(payload)
        assert WEATHER_SYSTEM_INSTRUCTIONS in dev
        assert SCENE_IMAGE_INSTRUCTIONS not in dev
        assert SCENE_MAP_MOTION_INSTRUCTIONS not in dev
        assert payload['reasoning']['effort'] == 'none'
    finally:
        provider.close()


def test_live_speech_profiles_stay_within_token_budgets() -> None:
    """Guard live speech static prompt budgets against silent token regression."""
    provider = OpenAIResponsesProvider(
        'offline-key', 'gpt-5.6-luna',
        semantic_context=True, reasoning_effort='low', prompt_cache_enabled=True,
    )
    try:
        names, tools = _live_tools()
        scene = {
            'map': {'revision': 'm1', 'landmarks': [{'location': '거실', 'x': 1.0, 'y': 2.0}]},
            'image': {'available': False},
        }
        weather_ctx = {
            'status': 'fresh',
            'location': '경기도 수원시 우만동',
            'current': {'condition': '맑음', 'temperature_c': 14.2},
        }
        homecam_ctx = {
            'mode': 'answer_only',
            'enabled': True,
            'homecam_query_result_untrusted': {'tool': 'get_homecam_status', 'data': {'online': True}},
        }

        budgets = [
            ('full_speech_memory', 9200, provider.build_payload(
                _speech_request('안녕 오늘 기분 어때?', available_tools=names, scene_context=scene),
                [], [], tools, memory_context={'enabled': True, 'response_settings': {}},
            )),
            ('full_speech_answer_only', 8000, provider.build_payload(
                _speech_request('안녕 오늘 기분 어때?', available_tools=names, scene_context=scene),
                [], [], tools, memory_context={'mode': 'answer_only', 'enabled': True, 'response_settings': {}},
            )),
            ('weather_call_1', 750, provider.build_payload(
                _speech_request('오늘 날씨 어때?', available_tools=names, scene_context=scene),
                [], [], tools, memory_context={'mode': 'answer_only', 'enabled': True, 'response_settings': {}},
            )),
            ('weather_call_2', 2000, provider.build_payload(
                _speech_request('오늘 날씨 어때?', available_tools=(), scene_context=scene),
                [], [], [], memory_context={'mode': 'answer_only', 'enabled': True},
                weather_context=weather_ctx,
            )),
            ('homecam_followup', 2100, provider.build_payload(
                _speech_request('홈캠 상태 어때?', available_tools=(), scene_context=scene, confirmed_homecam_read=True),
                [], [], [], memory_context=homecam_ctx,
            )),
            ('recall_followup', 2900, provider.build_payload(
                _speech_request('내 이름 뭐야?', available_tools=(), scene_context=scene),
                [], [], [], memory_context={'mode': 'answer_only', 'enabled': True},
            )),
            ('guest_chat', 2800, provider.build_payload(
                _speech_request('안녕하세요', available_tools=(), scene_context={'companion_guest': True}),
                [], [], [], memory_context={'mode': 'answer_only', 'enabled': False},
            )),
        ]
        for label, max_tokens, payload in budgets:
            actual = _payload_tokens(payload)
            assert actual <= max_tokens, (
                f'{label} exceeded token budget: {actual} > {max_tokens}'
            )
    finally:
        provider.close()
