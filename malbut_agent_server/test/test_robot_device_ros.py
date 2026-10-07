"""Exercise generated operator/Manager contracts on an isolated ROS graph."""

import hashlib
import json
import time
from uuid import uuid4

import pytest

rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')
from malbut_interfaces.action import DeviceOperation, ExecuteMission  # noqa: E402
from malbut_interfaces.srv import StopMovement  # noqa: E402
from rclpy.action import ActionServer  # noqa: E402
from rclpy.context import Context  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402

from malbut_agent_server.manager_client import ManagerClient  # noqa: E402
from malbut_agent_server.robot_device_client import RobotDeviceClient  # noqa: E402


def spin_until(executor, predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not predicate():
        executor.spin_once(timeout_sec=0.02)
    assert predicate(), 'typed ROS result did not arrive'


def test_generated_device_action_and_conditional_stop_round_trip():
    context = Context()
    rclpy.init(context=context)
    node = Node('agent_device_contract_test', context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    requests, outcomes = [], []

    def execute(handle):
        requests.append(handle.request)
        handle.succeed()
        return DeviceOperation.Result(success=True, code='completed',
                                      result_json='{"runtime":{"ready":true}}', message='observed')

    def stop(request, response):
        requests.append(request)
        response.stopped = False
        response.code = 'preemption_confirmation_required'
        response.unresolved_mission_ids = ['new-unconfirmed']
        return response

    server = ActionServer(node, DeviceOperation, '/malbut/device/operate', execute)
    service = node.create_service(StopMovement, '/malbut/mission/stop_movement', stop)
    client = RobotDeviceClient(node)
    try:
        spin_until(executor, lambda: client.action.server_is_ready() and client.stop_client.service_is_ready())
        client.send('typed-status', 'status', {}, outcomes.append)
        spin_until(executor, lambda: len(outcomes) == 1)
        assert outcomes[0]['success'] and outcomes[0]['result']['runtime']['ready']
        assert requests[0].request_id == 'typed-status'
        client.stop('typed-stop', outcomes.append, confirmed_ids=['known'])
        spin_until(executor, lambda: len(outcomes) == 2)
        assert requests[1].require_preemption_confirmation is True
        assert requests[1].confirmed_preemption_mission_ids == ['known']
        assert outcomes[1]['result']['unresolved_mission_ids'] == ['new-unconfirmed']
    finally:
        client.close()
        server.destroy()
        node.destroy_service(service)
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown(context=context)


def test_generated_manager_goal_keeps_journal_uuid_and_confirmation():
    context = Context()
    rclpy.init(context=context)
    node = Node('agent_manager_contract_test', context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    observed, events, goal_id = [], [], uuid4()

    def execute(handle):
        observed.append(handle.request)
        handle.succeed()
        return ExecuteMission.Result(mission_id=bytes(handle.goal_id.uuid).hex(),
                                     result_yaml='{"observed":true}', message='done')

    server = ActionServer(node, ExecuteMission, '/malbut/mission/execute', execute)
    client = ManagerClient(node, on_event=events.append)
    try:
        spin_until(executor, client._client.server_is_ready)
        client.submit('patrol', {'thoroughness': 1}, 'typed-mission', goal_uuid=goal_id,
                      require_preemption_confirmation=True,
                      confirmed_preemption_mission_ids=['known'],
                      expected_localization_runtime_id='runtime',
                      expected_localization_transition_id=42,
                      require_movement_epoch=True, movement_runtime_id='lifetime', movement_epoch=7)
        spin_until(executor, lambda: any(event['terminal'] for event in events))
        assert observed[0].require_preemption_confirmation is True
        assert observed[0].confirmed_preemption_mission_ids == ['known']
        assert observed[0].expected_localization_runtime_id == 'runtime'
        assert observed[0].expected_localization_transition_id == 42
        assert observed[0].require_movement_epoch is True
        assert observed[0].movement_runtime_id == 'lifetime' and observed[0].movement_epoch == 7
        assert events[-1]['mission_id'] == goal_id.hex
        assert events[-1]['state'] == 'SUCCEEDED'
        assert json.loads(events[-1]['result_yaml']) == {'observed': True}
    finally:
        client.close()
        server.destroy()
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown(context=context)


def test_resident_voice_profile_routes_committed_query_without_manager(tmp_path):
    from malbut_interfaces.msg import SpeechRequest, SpeechTranscript
    from malbut_agent_server.config import Settings
    from malbut_agent_server.factory import build_orchestrator
    from malbut_agent_server.ros_communication import create_communication_node
    from malbut_agent_server.schemas import AgentDecision, ProviderResult
    from malbut_agent_server.resident_weather_query import ResidentWeatherQuery

    class Provider:
        def complete(self, request, memories, history, tools, conversation_summary=None):
            names = {tool.name for tool in tools}
            assert 'stop_robot_movement' in names and 'cancel_voice_mission' not in names
            return ProviderResult(decision=AgentDecision(
                type='tool_call', tool_name='get_robot_status', arguments={}, message=''),
                provider='fixed', model='fixed', latency_ms=0)

    settings = Settings(database_path=str(tmp_path / 'dialogue.sqlite3'))

    def runtime_factory():
        runtime = build_orchestrator(settings, http_server=False)
        runtime.provider = Provider()
        return runtime

    rclpy.init()
    owner = SingleThreadedExecutor()
    sender = Node('resident_voice_contract_sender')
    owner.add_node(sender)
    operations, replies = [], []

    def execute(handle):
        operations.append(handle.request.operation)
        handle.succeed()
        value = ({'runtime': {'state': 'STOPPED'}, 'system': {}, 'battery': None}
                 if handle.request.operation == 'status' else {'href': '/robots/synthetic/result'})
        return DeviceOperation.Result(success=True, code='completed',
                                      result_json=json.dumps(value), message='observed')

    server = ActionServer(sender, DeviceOperation, '/malbut/device/operate', execute)
    sender.create_subscription(SpeechRequest, '/malbut/speech/response',
                               replies.append, 10)
    transcripts = sender.create_publisher(SpeechTranscript, '/malbut/speech/transcript', 10)
    agent = create_communication_node(
        speech_db_path=str(tmp_path / 'receipts.sqlite3'), dialogue_settings=settings,
        dialogue_factory=runtime_factory, enable_manager_commands=True,
        enable_device_operations=True)
    owner.add_node(agent)
    try:
        assert isinstance(agent.weather_query, ResidentWeatherQuery)
        spin_until(owner, lambda: transcripts.get_subscription_count() == 1 and
                   agent.speech_missions.device.action.server_is_ready())
        transcripts.publish(SpeechTranscript(utterance_id='typed-voice-status', text='로봇 상태 알려 줘'))
        spin_until(owner, lambda: any('배터리 값은 아직 확인되지 않았어요' in message.text
                                     for message in replies))
        spin_until(owner, lambda: operations.count('result_publish') == 2)
        assert operations.count('status') == 1
        assert not agent.missions._client.server_is_ready()
        notification = next(message for message in replies
                            if message.request_type == SpeechRequest.NOTIFICATION)
        expected = 'speech-request-' + hashlib.sha256(b'typed-voice-status').hexdigest()
        assert notification.request_id == expected
        assert notification.interim is False
        acknowledgement = next(message for message in replies
                               if message.request_type == SpeechRequest.DIALOGUE
                               and not message.interim)
        assert acknowledgement.request_id == 'typed-voice-status'
        assert acknowledgement.playback_id != notification.playback_id
    finally:
        owner.remove_node(agent)
        agent.destroy_node()
        server.destroy()
        owner.shutdown()
        sender.destroy_node()
        rclpy.shutdown()
