"""Tests for bounded prompt construction."""

import json

import pytest

from malbut_agent_server.conversation import ConversationTurn
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.prompting import (
    DEFAULT_RECENT_CONVERSATION_TURNS,
    MAX_CONVERSATION_MESSAGE_CHARS,
    MAX_MEMORY_CONTEXT_CHARS,
    SYSTEM_INSTRUCTIONS,
    build_model_input,
)
from malbut_agent_server.schemas import AgentRequest


def test_secret_refusal_precedes_compound_action_clarification() -> None:
    """Prompt priority cannot downgrade a secret request to clarification."""
    danger = SYSTEM_INSTRUCTIONS.index(
        '- 위험·저수준·규칙 우회·비밀 노출:'
    )
    multiple = SYSTEM_INSTRUCTIONS.index(
        '- 둘 이상의 행동: clarification.'
    )
    assert danger < multiple
    assert '다른 행동이 함께' in SYSTEM_INSTRUCTIONS


def test_memory_context_has_a_total_character_budget() -> None:
    """Long stored records must not overflow a small-model context."""
    request = AgentRequest.from_dict(
        {
            'request_id': 'prompt-test',
            'user_id': 'test-user',
            'conversation_id': 'prompt-conversation',
            'turn_id': 'turn-1',
            'utterance': '기억을 알려줘',
            'robot_state': {},
            'available_tools': [],
        }
    )
    memories = [
        MemoryRecord(
            id=f'memory-{index}',
            user_id='test-user',
            kind='fact',
            content='가' * 4000,
            source='test',
            confidence=1,
            created_at=0,
            expires_at=None,
            metadata={},
        )
        for index in range(5)
    ]
    model_input = build_model_input(request, memories)
    assert model_input.count('가') <= MAX_MEMORY_CONTEXT_CHARS
    assert '"truncated":true' in model_input


def test_conversation_context_is_latest_ten_and_data_only() -> None:
    """Prompt history is bounded, ordered, and excludes private IDs."""
    request = AgentRequest.from_dict(
        {
            'request_id': 'prompt-history-request',
            'user_id': 'private-user',
            'conversation_id': 'private-conversation',
            'turn_id': 'private-turn',
            'utterance': '그거 다시 말해줘',
            'robot_state': {},
            'available_tools': [],
        }
    )
    turns = [
        ConversationTurn(
            conversation_id='private-conversation',
            user_id='private-user',
            session_instance_id='private-session-instance',
            turn_id=f'turn-{number}',
            request_id=f'request-{number}',
            request_fingerprint=f'fingerprint-{number}',
            generation=1,
            ordinal=number,
            user_content=f'사용자 {number} ' + '가' * 500,
            assistant_content=f'로봇 {number} ' + '나' * 500,
            response={},
            created_at=float(number),
            completed_at=float(number),
        )
        for number in range(
            1,
            DEFAULT_RECENT_CONVERSATION_TURNS + 3,
        )
    ]

    model_input = build_model_input(request, [], turns)
    payload = json.loads(model_input.split('\n', 1)[1])
    history = payload['conversation_history_untrusted']
    assert len(history) == DEFAULT_RECENT_CONVERSATION_TURNS
    assert [item['turn_id'] for item in history] == [
        f'turn-{number}'
        for number in range(
            3,
            DEFAULT_RECENT_CONVERSATION_TURNS + 3,
        )
    ]
    assert all(
        len(item['user']) <= MAX_CONVERSATION_MESSAGE_CHARS
        and len(item['assistant'])
        <= MAX_CONVERSATION_MESSAGE_CHARS
        for item in history
    )
    assert payload['current_user_utterance'] == '그거 다시 말해줘'
    assert 'private-user' not in model_input
    assert 'private-conversation' not in model_input
    assert 'private-turn' not in model_input


@pytest.mark.parametrize(
    ('keyword', 'value', 'message'),
    (
        ('max_model_input_chars', True, 'integer of at least 4096'),
        ('max_model_input_chars', 4095, 'integer of at least 4096'),
        ('recent_turn_limit', True, 'recent_turn_limit must be between'),
        ('recent_turn_limit', 0, 'recent_turn_limit must be between'),
        ('recent_turn_limit', 51, 'recent_turn_limit must be between'),
    ),
)
def test_prompt_limits_reject_unbounded_or_ambiguous_values(
    keyword: str,
    value,
    message: str,
) -> None:
    """Prompt limits reject booleans and values outside hard bounds."""
    request = AgentRequest.from_dict(
        {
            'request_id': 'invalid-prompt-limit',
            'user_id': 'test-user',
            'conversation_id': 'prompt-conversation',
            'turn_id': 'turn-1',
            'utterance': '안녕',
            'robot_state': {},
            'available_tools': [],
        }
    )
    with pytest.raises(ValueError, match=message):
        build_model_input(request, [], **{keyword: value})


def test_prompt_truncates_oversized_forbidden_zone_labels() -> None:
    """Untrusted zone labels cannot consume the model-input budget."""
    request = AgentRequest.from_dict(
        {
            'request_id': 'zone-prompt-limit',
            'user_id': 'test-user',
            'conversation_id': 'prompt-conversation',
            'turn_id': 'turn-1',
            'utterance': '상태 알려줘',
            'robot_state': {
                'forbidden_zones': ['가' * 200],
            },
            'available_tools': [],
        }
    )

    payload = json.loads(build_model_input(request, []).split('\n', 1)[1])
    zones = payload['robot_state_untrusted']['forbidden_zones']
    assert zones == ['가' * 80]
    assert payload['context_truncated'] is True
