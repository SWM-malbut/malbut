"""New tools delegate through unchanged ExecuteMission, using existing Nav2 only."""

import math
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
import yaml

from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.speech_missions import SpeechMissions
from malbut_agent_server.speech_navigation import NavigationTargetError, NavigationTargets
from malbut_agent_server.tools import validate_tool_arguments
from navigation_user_map import room, write_user_map
from test_speech_missions import FakeManager


@pytest.fixture
def navigation(tmp_path):
    _, path, image, _ = write_user_map(
        tmp_path, rooms=[room('거실', 1, 1), room('주방', 2, 2, 'room-2')])
    manager = FakeManager()
    missions = SpeechMissions(manager)
    missions.observe_localization({'mode': 'LOCALIZATION', 'map': str(path)})
    return missions, manager, path, image


@pytest.mark.parametrize('tool,args,capability', [
    ('request_rotation', {'direction': 'left', 'angle_deg': 90.0}, 'spin'),
    ('request_rotation', {'direction': 'right', 'angle_deg': 360}, 'spin'),
    ('request_backup', {'distance_m': 0.3}, 'backup'),
])
def test_relative_motion_uses_manager_not_raw_nav2_or_velocity(tool, args, capability):
    manager = FakeManager()
    missions = SpeechMissions(manager)
    missions.dispatch(missions.prepare('new-move', tool, args))
    assert len(manager.submissions) == 1
    sent_capability, payload, _ = manager.submissions[0]
    assert sent_capability == capability
    assert payload['time_allowance']['sec'] > 0
    if capability == 'spin':
        assert payload['target_yaw'] == pytest.approx(
            math.radians(args['angle_deg']) * (1 if args['direction'] == 'left' else -1))
    else:
        assert payload['speed'] == 0.1 and payload['target']['x'] == 0.3


@pytest.mark.parametrize('tool,args', [
    ('request_rotation', {'direction': 'left', 'angle_deg': True}),
    ('request_rotation', {'direction': 'left', 'angle_deg': float('nan')}),
    ('request_rotation', {'direction': 'left', 'angle_deg': 361}),
    ('request_rotation', {'direction': 'up', 'angle_deg': 90}),
    ('request_backup', {'distance_m': -0.1}),
    ('request_backup', {'distance_m': 0}),
    ('request_backup', {'distance_m': float('inf')}),
    ('request_backup', {'distance_m': 0.1, 'speed': 10}),
    ('request_waypoints', {'map_revision': 'rev', 'waypoints': []}),
])
def test_invalid_model_parameters_never_dispatch(tool, args):
    manager = FakeManager()
    missions = SpeechMissions(manager)
    with pytest.raises((ValidationError, ValueError)):
        missions.prepare('bad', tool, args)
    assert manager.submissions == []


def test_map_point_reuses_existing_navigation_manifest(navigation):
    missions, manager, _, _ = navigation
    args = {'map_revision': missions.map_context()['revision'], 'x': 1, 'y': 1, 'yaw_deg': 90}
    proposal = missions.prepare('point', 'request_navigation_point', args)
    assert not proposal.blocked
    missions.dispatch(proposal)
    capability, payload, _ = manager.submissions[0]
    assert capability == 'navigate_to_pose'
    assert payload['pose']['header']['frame_id'] == 'map'
    assert payload['pose']['pose']['orientation']['z'] == pytest.approx(math.sqrt(0.5))


def test_scene_grounded_goal_does_not_require_user_supplied_numbers(navigation):
    from malbut_agent_server.safety import SafetyPolicy
    from malbut_agent_server.schemas import AgentDecision, RobotState, SpeechAgentRequest
    from malbut_agent_server.speech_mission_policy import SpeechMissionPolicy

    missions, manager, _, _ = navigation
    tool = 'request_navigation_point'
    scene = {'map': missions.map_context(), 'image': {'available': True},
             'pose': {'frame_id': 'map', 'x': 0.5, 'y': 0.5, 'yaw_deg': 45, 'age_s': 0.1}}
    original = SpeechAgentRequest(
        'scene-goal', 'speaker', 'conversation', 'turn', '사진 보고 거실 쪽으로 조금 가줘',
        RobotState(), (tool,), scene_context=scene, navigation_locations=(),
    )
    # A model estimate, not an existing landmark or a number in the utterance.
    # This exercises server acceptance, not the accuracy of live LLM inference.
    arguments = {'map_revision': scene['map']['revision'], 'x': 0.8, 'y': 0.8, 'yaw_deg': 45}
    decision = AgentDecision('tool_call', '', tool_name=tool, arguments=arguments)
    assert SpeechMissionPolicy(SafetyPolicy(), (tool,)).evaluate(original, decision).allowed
    proposal = missions.prepare(original.request_id, tool, arguments)
    assert not proposal.blocked
    missions.dispatch(proposal)
    assert manager.submissions[0][0] == 'navigate_to_pose'
    position = manager.submissions[0][1]['pose']['pose']['position']
    assert (position['x'], position['y']) == (0.8, 0.8)


def test_waypoints_one_goal_ordered_and_cancelled_as_one_mission(navigation):
    missions, manager, _, _ = navigation
    args = {'map_revision': missions.map_context()['revision'], 'waypoints': [
        {'location': '주방', 'x': None, 'y': None, 'yaw_deg': None},
        {'location': None, 'x': 0.5, 'y': 0.5, 'yaw_deg': None},
        {'location': '거실', 'x': None, 'y': None, 'yaw_deg': None},
    ]}
    proposal = missions.prepare('route', 'request_waypoints', args)
    assert not proposal.blocked
    missions.dispatch(proposal)
    assert len(manager.submissions) == 1
    capability, payload, request_id = manager.submissions[0]
    assert capability == 'follow_waypoints'
    assert [p['pose']['position']['x'] for p in payload['poses']] == [2.0, 0.5, 1.0]
    missions.dispatch(missions.prepare('stop-route', 'cancel_voice_mission', {}))
    assert manager.cancellations == [request_id]


def test_map_revision_checked_before_preparation_and_again_before_dispatch(navigation):
    missions, manager, path, _ = navigation
    args = {'map_revision': missions.map_context()['revision'], 'x': 1, 'y': 1, 'yaw_deg': None}
    prepared = missions.prepare('prepared', 'request_navigation_point', args)
    document = yaml.safe_load(path.read_text())
    document['origin'][0] += 0.5
    path.write_text(yaml.safe_dump(document))
    missions.dispatch(prepared)
    assert manager.submissions == []
    assert missions.prepare('late', 'request_navigation_point', args).blocked


def test_blocked_unknown_and_outside_points_are_not_nav2_goals(navigation):
    missions, manager, path, image = navigation
    for x, y in ((100, 100), (-4.99, -4.99)):
        args = {'map_revision': missions.map_context()['revision'], 'x': x, 'y': y, 'yaw_deg': None}
        missions.dispatch(missions.prepare('outside-' + str(x), 'request_navigation_point', args))
    image.write_bytes(b'P5\n100 100\n255\n' + b'\x00' * 10000)
    args = {'map_revision': missions.map_context()['revision'], 'x': 0, 'y': 0, 'yaw_deg': None}
    missions.dispatch(missions.prepare('wall', 'request_navigation_point', args))
    assert manager.submissions == []
    with pytest.raises(NavigationTargetError):
        NavigationTargets().resolve_point(0, 0, None, str(path))


def test_all_new_moves_keep_existing_fall_guard(navigation):
    missions, manager, _, _ = navigation
    missions._move_refusal = lambda: 'fall check'
    for index, (tool, args) in enumerate([
        ('request_rotation', {'direction': 'left', 'angle_deg': 90}),
        ('request_backup', {'distance_m': 0.2}),
        ('request_navigation_point', {'map_revision': 'old', 'x': 0, 'y': 0, 'yaw_deg': None}),
        ('request_waypoints', {'map_revision': 'old', 'waypoints': [
            {'location': '거실', 'x': None, 'y': None, 'yaw_deg': None}]}),
    ]):
        assert missions.prepare(str(index), tool, args).blocked
    assert manager.submissions == []


def test_manifests_target_only_existing_actions_and_waypoints_stop_on_failure():
    root = Path(__file__).parents[2]
    for capability, action in [('spin', 'Spin'), ('backup', 'BackUp'),
                               ('follow_waypoints', 'FollowWaypoints')]:
        data = yaml.safe_load((root / 'malbut_interfaces/capabilities' / (capability + '.yaml')).read_text())
        assert data['command'] == {'kind': 'ACTION', 'name': '/' + capability,
                                   'type': 'nav2_msgs/action/' + action}
        assert data['execution']['resources'] == ['BASE']
        assert data['execution']['priority'] == 'NORMAL'
    config = yaml.safe_load((root / 'malbut_bringup/config/nav2_params.yaml').read_text())
    assert config['waypoint_follower']['ros__parameters']['stop_on_failure'] is True
    source = (root / 'malbut_bringup/malbut_bringup/runtime_control.py').read_text()
    assert "'follow_waypoints': ('motion', 'planning')" in source


def test_schema_limits_waypoint_count_and_rejects_mixed_targets(navigation):
    missions, _, _, _ = navigation
    point = {'location': '거실', 'x': None, 'y': None, 'yaw_deg': None}
    with pytest.raises(ValidationError):
        validate_tool_arguments('request_waypoints', {'map_revision': 'rev', 'waypoints': [point] * 17})
    point['x'] = 1
    args = {'map_revision': missions.map_context()['revision'], 'waypoints': [point]}
    assert missions.prepare('mixed', 'request_waypoints', args).blocked


def test_registered_ros_goals_match_installed_humble_interfaces():
    pytest.importorskip('rclpy')
    pytest.importorskip('nav2_msgs.action')
    from malbut_system_manager.manifest_registry import ManifestRegistry

    root = Path(__file__).parents[2]
    registry = ManifestRegistry(root / 'malbut_interfaces/capabilities')
    examples = {
        'spin': {'target_yaw': 1.0, 'time_allowance': {'sec': 10, 'nanosec': 0}},
        'backup': {'target': {'x': 0.2}, 'speed': 0.1,
                   'time_allowance': {'sec': 10, 'nanosec': 0}},
        'follow_waypoints': {'poses': [{'header': {'frame_id': 'map'},
                                      'pose': {'orientation': {'w': 1.0}}}]},
    }
    for name, args in examples.items():
        manifest = registry.get(name)
        _, message = registry.parse_arguments(manifest, yaml.safe_dump(args))
        assert isinstance(message, manifest.interface_type.Goal)


@pytest.mark.parametrize('tool,parameters,capability', [
    ('request_rotation', {'direction': 'left', 'angle_deg': 90}, 'spin'),
    ('request_backup', {'distance_m': 0.3}, 'backup'),
    ('request_navigation_point', {'x': 1, 'y': 1, 'yaw_deg': None}, 'navigate_to_pose'),
    ('request_waypoints', {'waypoints': [
        {'location': '거실', 'x': None, 'y': None, 'yaw_deg': None},
        {'location': '주방', 'x': None, 'y': None, 'yaw_deg': None},
    ]}, 'follow_waypoints'),
])
def test_worker_to_provider_to_existing_manager_preserves_scene(
        navigation, tmp_path, tool, parameters, capability):
    from malbut_agent_server.config import Settings
    from malbut_agent_server.factory import build_orchestrator
    from malbut_agent_server.schemas import AgentDecision, ProviderResult
    from malbut_agent_server.speech_dialogue import DialogueWorker
    from malbut_agent_server.speech_mission_policy import configure_speech_missions
    from malbut_agent_server.speech_scene import TurnImage

    missions, manager, _, _ = navigation
    scene = {'map': missions.map_context(), 'pose': None, 'image': {'available': True}}
    image = TurnImage(b'transient-turn-frame', lambda: True)
    requests = []
    args = dict(parameters)
    if tool in {'request_navigation_point', 'request_waypoints'}:
        args['map_revision'] = scene['map']['revision']

    class Provider:
        def complete(self, request, memories, history, tools, conversation_summary=None):
            requests.append(request)
            return ProviderResult(
                decision=AgentDecision('tool_call', '', tool_name=tool, arguments=args),
                provider='offline', model='fixed', latency_ms=0,
            )

    def factory():
        runtime = build_orchestrator(Settings(database_path=str(tmp_path / 'worker.db')))
        runtime.provider = Provider()
        return configure_speech_missions(runtime)

    worker = DialogueWorker(factory, 'speaker', missions=missions,
                            scene_source=SimpleNamespace(snapshot=lambda: (scene, image)))
    try:
        deadline = time.monotonic() + 5
        assert worker.submit('voice-command', '지정한 이동을 해 줘')
        reply = None
        while time.monotonic() < deadline and reply is None:
            for item in worker.drain():
                if item['kind'] == 'answer':
                    reply = item
            time.sleep(0.002)
        assert reply is not None
        assert requests[0].turn_image is image
        assert requests[0].scene_context['map']['revision'] == args.get(
            'map_revision', scene['map']['revision'])
        assert manager.submissions == []  # Inference itself never actuates.
        assert worker.publish_reply(reply, lambda text: True)
        assert manager.submissions[0][0] == capability
        worker.publish_reply(reply, lambda text: True)
        assert len(manager.submissions) == 1
    finally:
        worker.close()
