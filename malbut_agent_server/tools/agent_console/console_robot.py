"""Exercise the production navigation dialogue without a robot executor."""

from dataclasses import replace
import hashlib
from uuid import uuid4

from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.named_target import BoundNamedTarget
from malbut_agent_server.robot_state_source import StaticSimulationRobotStateSource
from malbut_agent_server.schemas import RobotState
from malbut_agent_server.text_turn import TextTurnService


ROOMS = {
    '거실': 'living_room', '주방': 'kitchen', '침실': 'bedroom',
    '현관': 'entrance', '충전소': 'charging_station',
}


class _SimulationTargets:
    def resolve(self, location):
        if location not in ROOMS:
            raise ValueError('모의 지도에 없는 목적지입니다.')
        return BoundNamedTarget(
            location, ROOMS[location],
            hashlib.sha256(('console-simulation:' + location).encode()).hexdigest(),
        )


class RobotProbe:
    """Reuse confirmation policy with fixed simulated state and no action creation."""

    def __init__(self, settings):
        self.user_id = settings.user_id
        self.runtime = build_orchestrator(
            replace(settings, tool_mode='proposal'), http_server=False,
            robot_state_source=StaticSimulationRobotStateSource(RobotState(
                battery_percent=90, navigation_available=True,
                localization_ok=True, emergency_stop=False,
            )),
        )
        self.service = TextTurnService(
            self.runtime, _SimulationTargets(), create_robot_actions=False,
        )
        self.conversation_id = None
        self.reset()

    def reset(self):
        session = self.runtime.conversation_store.resume_or_create(
            self.user_id, self.conversation_id, start_new=True,
        )
        self.conversation_id = session.conversation_id
        return self.conversation_id

    def turn(self, text):
        key = uuid4().hex
        result = self.service.handle(user_id=self.user_id, value={
            'request_id': 'robot-' + key, 'turn_id': 'turn-' + key,
            'conversation_id': self.conversation_id, 'text': text,
        })
        execution = result['execution']
        if (execution.get('physical_authorized') is not False
                or execution.get('nav2_start_count') != 0):
            raise RuntimeError('로봇 체험의 실행 금지 조건이 변경됐습니다.')
        result['simulated'] = True
        return result

    def close(self):
        self.runtime.close()
