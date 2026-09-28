"""Exercise the offline provider's bounded Manager voice proposals."""

import pytest

from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.schemas import AgentRequest, RobotState
from malbut_agent_server.speech_mission_policy import configure_speech_missions
from malbut_agent_server.tools import SPEECH_MISSION_TOOLS, select_tool_specs


def request(text, available=SPEECH_MISSION_TOOLS):
    return AgentRequest(
        request_id='request', user_id='speaker', conversation_id='conversation',
        turn_id='turn', utterance=text, robot_state=RobotState(),
        available_tools=available,
    )


def decide(text, available=SPEECH_MISSION_TOOLS, *, exposed=None):
    return MockProvider().complete(
        request(text, available), [], [],
        select_tool_specs(available if exposed is None else exposed),
    ).decision


@pytest.mark.parametrize('text,tool,arguments', [
    ('제이크야 나 따라와', 'request_follow_person', {}),
    ('나 좀 따라와', 'request_follow_person', {}),
    ('follow me', 'request_follow_person', {}),
    ('순찰해 줘', 'request_patrol', {'thoroughness': 'normal'}),
    ('가볍게 순찰해줘', 'request_patrol', {'thoroughness': 'light'}),
    ('집안을 꼼꼼히 순찰해', 'request_patrol', {'thoroughness': 'thorough'}),
    ('start a thorough patrol', 'request_patrol', {'thoroughness': 'thorough'}),
    ('멈춰', 'cancel_voice_mission', {}),
    ('따라오지 마', 'cancel_voice_mission', {}),
    ('순찰을 취소해', 'cancel_voice_mission', {}),
    ('거실로 가', 'request_navigation', {'location': '거실'}),
    ('거실로 가볼까?', 'request_navigation', {'location': '거실'}),
    ('거실로 가 볼까요?', 'request_navigation', {'location': '거실'}),
    ('주방으로 갈까?', 'request_navigation', {'location': '주방'}),
    ('거실로 이동해 볼까요?', 'request_navigation', {'location': '거실'}),
    ('부엌으로 이동해줘', 'request_navigation', {'location': '주방'}),
    ('서재로 가 줘', 'request_navigation', {'location': '서재'}),
    ('go to kitchen', 'request_navigation', {'location': 'kitchen'}),
])
def test_offline_voice_commands_select_strict_manager_payloads(text, tool, arguments):
    decision = decide(text)
    assert decision.type == 'tool_call'
    assert decision.tool_name == tool
    assert decision.arguments == arguments
    assert decision.reason == 'voice_manager_request'
    assert '완료' not in decision.message


@pytest.mark.parametrize('text', [
    '"따라와"라고 말해', '만약 내가 걸으면 따라와', '엄마를 따라와',
    '따라오지 말아줘', '순찰해라고 말해줘', '나중에 순찰해',
    '따라와 그리고 순찰해', '순찰하고 나를 따라와', '날씨 조회를 취소해',
    '상황 대응을 취소해', '순찰을 취소하지 마',
    '거실로 가고 주방으로 이동해', '서재로 가면 어떻게 돼?',
    '거실로 갈 수 있어?', '거실로 이동 가능해?', '거실로 안 가볼까?',
    '거실로 가지 말까?', '거실로 가볼까 말까?', '“거실로 가볼까?”',
    '거실로 가볼까라고 말해줘', '내일 거실로 가볼까?',
])
def test_non_commands_never_select_manager_tool(text):
    assert decide(text).tool_name not in SPEECH_MISSION_TOOLS


@pytest.mark.parametrize('available,exposed', [
    ((), SPEECH_MISSION_TOOLS), (SPEECH_MISSION_TOOLS, ()),
    (('get_weather',), ('get_weather',)),
])
def test_hidden_tools_cannot_be_selected(available, exposed):
    assert decide('따라와', available, exposed=exposed).tool_name is None


def test_legacy_navigation_and_weather_keep_their_original_tools():
    assert decide('거실로 가줘', ('navigate',)).tool_name == 'navigate'
    assert decide('오늘 날씨 알려줘', ('get_weather',)).tool_name == 'get_weather'


def test_capability_answer_lists_only_exposed_voice_tools():
    result = decide('할 수 있는 기능이 뭐야?', ('request_follow_person',))
    assert result.type == 'message'
    assert '사람 따라가기' in result.message
    assert '순찰' not in result.message


def test_real_mock_runtime_returns_committed_voice_proposals_without_ros_or_network():
    runtime = configure_speech_missions(build_orchestrator(Settings(database_path=':memory:')))
    try:
        session = runtime.conversation_store.create('speaker')
        for number, text in enumerate(('따라와', '멈춰', '순찰해줘')):
            result = runtime.handle(AgentRequest(
                request_id=f'request-{number}', user_id='speaker',
                conversation_id=session.conversation_id, turn_id=f'turn-{number}',
                utterance=text, robot_state=RobotState(),
                available_tools=runtime.speech_mission_tools,
            ))
            assert result.decision.type == 'tool_call'
            assert result.safety.code == 'manager_request'
            assert not result.state_trusted
        stored = runtime.conversation_store.snapshot('speaker', session.conversation_id)
        assert len(stored.turns) == 3
    finally:
        runtime.close()
