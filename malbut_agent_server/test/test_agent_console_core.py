"""Offline integration checks through the real Agent and SQLite stores."""

import pytest

import test_agent_console_support  # noqa: F401

from console_core import ConsoleCore
from malbut_agent_server.config import Settings
from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.tools import TOOL_SPECS


def test_memory_preferences_sessions_and_restart(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / 'demo.sqlite3'),
        user_id='terminal-demo-user', tool_mode='simulation',
    )
    core = ConsoleCore(settings)
    try:
        first_session = core.conversation_id
        requested = core.chat('우리 강아지 이름은 초코야. 기억해줘')
        assert requested['decision_type'] == 'clarification'
        assert core.memories()['items'] == []
        assert core.memories()['policy']['enabled'] is False
        core.chat('네')
        assert core.memories()['policy']['enabled'] is True
        assert '초코' in core.memories()['items'][0]['content']
        core.chat('초기 설정 말투=친근한 반말, 호칭=현재')
        core.chat('이번 대화는 자세하게 설명해줘')
        assert core.status()['session_preferences']['length'] == '자세하게'
        core.new_conversation()
        assert core.conversation_id != first_session
        assert core.status()['session_preferences']['length'] == '상황에 맞게'
        assert core.status()['session_preferences']['tone'] == '친근한 반말'
        assert '초코' in core.chat('우리 강아지 이름이 뭐야?')['text']
        retained_session = core.conversation_id
    finally:
        core.close()

    core = ConsoleCore(settings)
    try:
        assert core.conversation_id == retained_session
        assert core.memories()['default_preferences']['address'] == '현재'
        recalled = core.chat('우리 강아지 이름이 뭐야?')
        assert '초코' in recalled['text']
        core.validate_reply(recalled)
        core.chat('강아지 이름을 두부로 정정해줘')
        assert '두부' in core.memories()['items'][0]['content']
        with pytest.raises(ValidationError, match='memory_changed'):
            core.validate_reply(recalled)
        core.chat('강아지 이름을 잊어줘')
        assert core.memories()['items'] == []
        core.chat('새 대화를 시작해줘. 안녕')
        assert core.conversation_id != retained_session
    finally:
        core.close()


def test_tool_boundaries_and_input_validation():
    core = ConsoleCore(Settings(tool_mode='simulation'))
    try:
        capabilities = core.tools()['capabilities']
        assert {item['name'] for item in capabilities} == set(TOOL_SPECS)
        assert {item['name'] for item in capabilities
                if item['console_status'] == 'simulation'} == {
            'navigate', 'detect_pet', 'capture_photo', 'send_notification', 'get_robot_status',
        }
        result = core.query_tool('navigate', {'location': '거실'})
        assert result['status'] == 'succeeded'
        assert result['result']['simulated'] is True
        assert result['result']['accepted'] is False
        assert result['result']['nav2_goal_published'] is False
        assert core.query_tool('capture_photo', {})['result']['image_created'] is False
        notification = core.query_tool('send_notification', {
            'message': '테스트', 'image_id': None,
        })
        assert notification['result']['delivered'] is False
        assert core.query_tool('get_weather', {})['status'] == 'rejected'
        assert core.query_tool('navigate', {})['error']['code'] == 'invalid_arguments'
        assert core.query_tool('missing', {})['error']['code'] == 'unknown_tool'
        with pytest.raises(ValidationError):
            core.chat('')
        assert core.status()['turn_count'] == 0
        response = core.chat('거실로 가줘')
        assert response['metadata']['execution']['authorized'] is False
        assert response['physical_authorized'] is False
    finally:
        core.close()
