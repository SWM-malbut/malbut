"""Voice commands cross the real Manager Action using isolated fake actuators."""

import json
import math
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest


rclpy = pytest.importorskip('rclpy', reason='ROS 2 is not installed')

from action_msgs.msg import GoalStatus  # noqa: E402
from malbut_interfaces.action import FollowPerson, Patrol  # noqa: E402
from malbut_interfaces.msg import SpeechRequest, SpeechTranscript  # noqa: E402
from nav2_msgs.action import NavigateToPose  # noqa: E402
from rclpy.action import ActionClient, ActionServer, CancelResponse  # noqa: E402
from rclpy.callback_groups import ReentrantCallbackGroup  # noqa: E402
from rclpy.executors import MultiThreadedExecutor, SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy  # noqa: E402
from std_msgs.msg import String  # noqa: E402
import yaml  # noqa: E402

from malbut_agent_server import ros_communication  # noqa: E402
from malbut_agent_server.config import Settings  # noqa: E402
from malbut_agent_server.factory import build_orchestrator  # noqa: E402
from malbut_agent_server.schemas import AgentDecision, ProviderResult  # noqa: E402
from malbut_agent_server.speech_navigation import NavigationTargets  # noqa: E402
from malbut_system_manager.system_manager_node import SystemManagerNode  # noqa: E402


TIMEOUT_S = 10.0
MANIFESTS = Path(__file__).resolve().parents[2] / 'malbut_interfaces/capabilities'
ACTION_TYPES = {
    'follow_person': FollowPerson, 'patrol': Patrol,
    'navigate_to_pose': NavigateToPose,
}


class _Provider:
    """Fixed decisions test transport, never the model's language interpretation."""

    def __init__(self):
        self.calls = []
        self.histories = []
        self.overrides = {}

    def complete(self, request, memories, conversation_turns, tools,
                 conversation_summary=None):
        del memories, conversation_summary
        self.calls.append(request)
        self.histories.append(list(conversation_turns))
        selected = {
            '따라와': ('request_follow_person', {}),
            '멈춰': ('cancel_voice_mission', {}),
            '거실로 가': ('request_navigation', {'location': '거실'}),
            '거실로 가볼까?': ('request_navigation', {'location': '거실'}),
            '우리 거실로 가볼까': ('request_navigation', {'location': '거실'}),
            '거실로 와바라': ('request_navigation', {'location': '거실'}),
            '거실': ('request_navigation', {'location': '거실'}),
            '꼼꼼히 순찰해': ('request_patrol', {'thoroughness': 'thorough'}),
        }
        noncommands = {
            '오늘 날씨는 어때': ('message', '날씨에 관한 대화 응답입니다.'),
            '거실로 갈 수 있어?': ('message', '등록된 목적지 이동 기능이 있어요.'),
            '거실로 가보지 마': ('message', '이동하지 않을게요.'),
            '"거실로 가볼까?"라는 문장을 설명해': ('message', '거실 이동을 제안하는 문장이에요.'),
            '와바라': ('clarification', '어느 등록된 목적지로 오면 될까요?'),
            '이리 오너라': ('clarification', '어느 등록된 목적지로 오면 될까요?'),
        }
        if request.utterance in self.overrides:
            decision = self.overrides[request.utterance]
        elif request.utterance in noncommands:
            kind, message = noncommands[request.utterance]
            decision = AgentDecision(type=kind, message=message)
        else:
            tool, arguments = selected[request.utterance]
            assert tool in {spec.name for spec in tools}
            decision = AgentDecision(
                type='tool_call', tool_name=tool, arguments=arguments,
                message='모델이 만든 실행 완료 주장', expires_in_ms=5000,
            )
        return ProviderResult(
            decision=decision,
            provider='ros-speech-mission-fixture', model='fixed', latency_ms=0.0,
        )


class _Actuators(Node):
    """Only namespaced test Actions; no publisher can command robot motion."""

    def __init__(self):
        super().__init__('speech_mission_test_actuators')
        self.goals = {name: [] for name in ACTION_TYPES}
        self.cancel_seen = Event()
        self.allow_cancel = Event()
        self.release = Event()
        self.servers = []
        for name, action_type in ACTION_TYPES.items():
            self.servers.append(ActionServer(
                self, action_type, '/speech_mission_test/' + name,
                execute_callback=lambda handle, name=name: self.execute(name, handle),
                cancel_callback=lambda _handle: CancelResponse.ACCEPT,
                callback_group=ReentrantCallbackGroup(),
            ))

    def execute(self, capability, handle):
        self.goals[capability].append(handle.request)
        if capability == 'follow_person':
            deadline = time.monotonic() + TIMEOUT_S * 3
            while time.monotonic() < deadline and not self.release.is_set():
                if handle.is_cancel_requested:
                    self.cancel_seen.set()
                    if self.allow_cancel.wait(TIMEOUT_S):
                        handle.canceled()
                        return FollowPerson.Result(
                            success=False, final_state='STOPPED',
                            message='test follow canceled',
                        )
                    break
                handle.publish_feedback(FollowPerson.Feedback(
                    state='TRACKING', target_visible=True,
                ))
                time.sleep(0.02)
            handle.abort()
            return FollowPerson.Result(message='test fixture released')
        handle.succeed()
        if capability == 'patrol':
            return Patrol.Result(success=True, message='test patrol complete',
                                 coverage_ratio=1.0, viewpoints_visited=1)
        return NavigateToPose.Result()

    def destroy_node(self):
        for server in self.servers:
            server.destroy()
        return super().destroy_node()


@pytest.fixture
def speech_graph(tmp_path, monkeypatch):
    """Production Agent owner thread plus real Manager and fake downstream ROS."""
    monkeypatch.setenv('ROS_DOMAIN_ID', '196')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init()
    owner = SingleThreadedExecutor()
    background = MultiThreadedExecutor(num_threads=6)
    background_thread = Thread(target=background.spin, daemon=True)
    provider = _Provider()
    settings = Settings(
        user_id='speech-mission-test-user',
        database_path=str(tmp_path / 'dialogue.sqlite3'),
    )
    run = SimpleNamespace(
        agent=None, manager=None, actuators=None, provider=provider,
        events=[], manager_goals=[], replies=[], responses=[], receipts=[],
    )
    sender = Node('speech_mission_test_sender')
    owner.add_node(sender)
    transcripts = sender.create_publisher(
        SpeechTranscript, '/malbut/speech/transcript', 10,
    )
    sender.create_subscription(
        SpeechRequest, '/malbut/speech/response',
        lambda message: run.replies.append(message.text), 10,
    )
    state_qos = QoSProfile(depth=1)
    state_qos.reliability = ReliabilityPolicy.RELIABLE
    state_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
    localization = sender.create_publisher(String, '/malbut/localization/state', state_qos)
    original_goal = SystemManagerNode._goal
    original_receive = ros_communication.receive_transcript

    def record_goal(manager, request):
        run.manager_goals.append((request.capability_id, yaml.safe_load(request.arguments_yaml)))
        return original_goal(manager, request)

    def record_receipt(store, utterance_id, text, logger):
        outcome = original_receive(store, utterance_id, text, logger)
        run.receipts.append((utterance_id, outcome))
        return outcome

    monkeypatch.setattr(SystemManagerNode, '_goal', record_goal)
    monkeypatch.setattr(ros_communication, 'receive_transcript', record_receipt)

    def spin_until(predicate, timeout=TIMEOUT_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            owner.spin_once(timeout_sec=0.02)
        raise AssertionError('Voice/Manager/actuator evidence did not arrive')

    def create_runtime():
        runtime = build_orchestrator(settings, http_server=False)
        runtime.provider = provider
        return runtime

    def start(navigation_targets=None):
        assert run.agent is None, 'one isolated graph per test'
        directory = tmp_path / 'manifests'
        directory.mkdir()
        for name in ACTION_TYPES:
            value = yaml.safe_load((MANIFESTS / (name + '.yaml')).read_text())
            value['command']['name'] = '/speech_mission_test/' + name
            (directory / (name + '.yaml')).write_text(yaml.safe_dump(value))
        run.actuators = _Actuators()
        run.manager = SystemManagerNode(manifest_directory=str(directory))
        # Standalone Manager deliberately has localization_control disabled:
        # no SLAM/AMCL process or real map loading is started by this fixture.
        assert run.manager._localization is None
        background.add_node(run.actuators)
        background.add_node(run.manager)
        background_thread.start()
        run.agent = ros_communication.create_communication_node(
            speech_db_path=str(tmp_path / 'receipts.sqlite3'),
            dialogue_settings=settings, dialogue_factory=create_runtime,
            enable_manager_commands=True, navigation_targets=navigation_targets,
            on_event=run.events.append, goal_response_timeout_s=2.0,
        )
        original_publish = run.agent.dialogue.publish_reply

        def record_publish(response, publish):
            result = original_publish(response, publish)
            if result is not None:
                run.responses.append(result)
            return result

        monkeypatch.setattr(run.agent.dialogue, 'publish_reply', record_publish)
        owner.add_node(run.agent)
        spin_until(lambda: transcripts.get_subscription_count() == 1
                   and run.agent._speech.get_subscription_count() == 1)
        spin_until(run.agent.missions._client.server_is_ready)
        for name, action_type in ACTION_TYPES.items():
            probe = ActionClient(sender, action_type, '/speech_mission_test/' + name)
            try:
                spin_until(probe.server_is_ready)
            finally:
                probe.destroy()
        assert run.manager_goals == []

    def send(text, utterance_id=None):
        uid = utterance_id or str(uuid4())
        transcripts.publish(SpeechTranscript(utterance_id=uid, text=text))
        return uid

    def say(text):
        uid = send(text)
        spin_until(lambda: any(item['utterance_id'] == uid and item['kind'] != 'progress'
                               for item in run.responses))
        reply = next(item for item in run.responses
                     if item['utterance_id'] == uid and item['kind'] != 'progress')
        spin_until(lambda: reply['text'] in run.replies)
        assert reply['text'] != '모델이 만든 실행 완료 주장'
        return uid, reply['text']

    def observe_map(map_path):
        localization.publish(String(data=json.dumps({
            'mode': 'LOCALIZATION', 'map': str(map_path), 'message': 'test selected map',
        })))
        spin_until(lambda: run.agent.speech_missions._active_map == str(map_path))

    run.start, run.send, run.say = start, send, say
    run.spin_until, run.observe_map = spin_until, observe_map
    try:
        yield run
    finally:
        if run.actuators is not None:
            run.actuators.allow_cancel.set()
            run.actuators.release.set()
        if run.manager is not None:
            run.manager.begin_shutdown()
            try:
                spin_until(lambda: run.manager.public_context_count == 0, timeout=3.0)
            except AssertionError:
                run.manager.force_shutdown()
        if run.agent is not None:
            owner.remove_node(run.agent)
            run.agent.destroy_node()
        background.shutdown(timeout_sec=3.0)
        if background_thread.ident is not None:
            background_thread.join(timeout=3.0)
        for node in (run.manager, run.actuators):
            if node is not None:
                node.destroy_node()
        owner.remove_node(sender)
        sender.destroy_node()
        owner.shutdown(timeout_sec=2.0)
        if rclpy.ok():
            rclpy.shutdown()


def test_transcript_follow_and_next_spoken_cancel_cross_manager_once(speech_graph):
    run = speech_graph
    run.start()
    uid, _ = run.say('따라와')
    run.spin_until(lambda: len(run.actuators.goals['follow_person']) == 1)
    assert run.manager_goals == [('follow_person', {
        'target_mode': 0, 'target_person_id': '', 'desired_distance_m': 1.0,
    })]
    goal = run.actuators.goals['follow_person'][0]
    assert goal.target_mode == FollowPerson.Goal.VISIBLE_PERSON
    assert goal.target_person_id == '' and goal.desired_distance_m == pytest.approx(1.0)
    mission_id = next(event['request_id'] for event in run.events
                      if event['capability_id'] == 'follow_person')
    run.spin_until(lambda: any(event['kind'] == 'accepted' for event in run.events))
    assert not run.agent.missions.snapshot(mission_id)['terminal']

    # An already spoken transcript cannot rerun inference or submit another Goal.
    run.send('따라와', uid)
    run.spin_until(lambda: (uid, 'duplicate') in run.receipts)
    assert len(run.provider.calls) == len(run.manager_goals) == 1

    _, reply = run.say('멈춰')
    assert '취소' in reply
    run.spin_until(run.actuators.cancel_seen.is_set)
    run.spin_until(lambda: any(event['kind'] == 'cancel_accepted' for event in run.events))
    assert not run.agent.missions.snapshot(mission_id)['terminal']
    assert not any(event['kind'] == 'canceled' for event in run.events)
    run.actuators.allow_cancel.set()
    run.spin_until(lambda: run.agent.missions.snapshot(mission_id)['terminal'])
    assert run.agent.missions.snapshot(mission_id)['ros_status'] == GoalStatus.STATUS_CANCELED
    assert len(run.manager_goals) == len(run.actuators.goals['follow_person']) == 1
    assert len(run.provider.calls) == 2


def _registered_navigation(tmp_path):
    image = tmp_path / 'fixture.pgm'
    image.write_bytes(b'P5\n1 1\n255\n\xff')
    selected = tmp_path / 'fixture.yaml'
    selected.write_text(yaml.safe_dump({
        'image': image.name, 'resolution': 0.05, 'origin': [0.0, 0.0, 0.0],
        'negate': 0, 'occupied_thresh': 0.65, 'free_thresh': 0.196,
    }))
    config = tmp_path / 'destinations.yaml'
    config.write_text(yaml.safe_dump({
        'map': str(selected), 'frame_id': 'map',
        'locations': {'거실': {'x': 1.25, 'y': -2.5, 'yaw': math.pi / 2}},
    }, allow_unicode=True))
    return NavigationTargets(config), selected


@pytest.mark.parametrize('utterance', [
    '거실로 가', '거실로 가볼까?', '우리 거실로 가볼까', '거실로 와바라',
])
def test_named_navigation_requires_selected_map_and_preserves_registered_pose(
    speech_graph, tmp_path, utterance,
):
    run = speech_graph
    targets, selected = _registered_navigation(tmp_path)
    run.start(targets)
    _, reply = run.say(utterance)
    assert '지도' in reply
    assert run.manager_goals == []

    run.observe_map(selected)
    run.say(utterance)
    run.spin_until(lambda: len(run.actuators.goals['navigate_to_pose']) == 1)
    goal = run.actuators.goals['navigate_to_pose'][0]
    assert run.manager_goals == [('navigate_to_pose',
                                 targets.resolve('거실', str(selected)).arguments)]
    assert goal.pose.header.frame_id == 'map'
    assert goal.pose.pose.position.x == 1.25
    assert goal.pose.pose.position.y == -2.5
    assert goal.pose.pose.position.z == 0.0
    assert goal.pose.pose.orientation.x == goal.pose.pose.orientation.y == 0.0
    assert goal.pose.pose.orientation.z == pytest.approx(math.sin(math.pi / 4))
    assert goal.pose.pose.orientation.w == pytest.approx(math.cos(math.pi / 4))
    assert goal.behavior_tree == ''
    run.spin_until(lambda: any(event['kind'] == 'succeeded' for event in run.events))


@pytest.mark.parametrize('utterance', [
    '거실로 갈 수 있어?', '거실로 가보지 마', '"거실로 가볼까?"라는 문장을 설명해',
    '오늘 날씨는 어때', '와바라', '이리 오너라',
])
def test_message_and_clarification_transport_never_creates_manager_goals(
    speech_graph, tmp_path, utterance,
):
    run = speech_graph
    targets, selected = _registered_navigation(tmp_path)
    run.start(targets)
    run.observe_map(selected)
    _, reply = run.say(utterance)
    assert len(run.provider.calls) == 1
    assert reply
    assert run.manager_goals == []
    assert run.events == []
    assert all(not goals for goals in run.actuators.goals.values())


def test_model_proposal_after_destination_clarification_uses_same_conversation(
    speech_graph, tmp_path,
):
    run = speech_graph
    targets, selected = _registered_navigation(tmp_path)
    run.start(targets)
    run.observe_map(selected)
    run.say('와바라')
    assert run.manager_goals == []
    run.say('거실')
    run.spin_until(lambda: len(run.actuators.goals['navigate_to_pose']) == 1)
    assert [turn.user_content for turn in run.provider.histories[-1]] == ['와바라']
    assert run.provider.calls[0].conversation_id == run.provider.calls[1].conversation_id
    assert run.manager_goals == [('navigate_to_pose', targets.resolve(
        '거실', str(selected)).arguments)]
    run.spin_until(lambda: any(event['kind'] == 'succeeded' for event in run.events))


def test_patrol_thoroughness_reaches_downstream_through_manager(speech_graph):
    run = speech_graph
    run.start()
    run.say('꼼꼼히 순찰해')
    run.spin_until(lambda: len(run.actuators.goals['patrol']) == 1)
    assert run.manager_goals == [('patrol', {'thoroughness': 2})]
    assert run.actuators.goals['patrol'][0].thoroughness == Patrol.Goal.THOROUGH
    run.spin_until(lambda: any(event['kind'] == 'succeeded' for event in run.events))


@pytest.mark.parametrize('tool,arguments', [
    ('request_navigation', {'location': '거실', 'pose': {'x': 42}}),
    ('request_follow_person', {'target_person_id': 'invented-speaker-id'}),
    ('request_patrol', {'thoroughness': 'fast'}),
    ('cancel_voice_mission', {'request_id': 'somebody-else'}),
])
def test_invalid_structured_proposal_never_reaches_manager(
    speech_graph, tmp_path, tool, arguments,
):
    run = speech_graph
    targets, selected = _registered_navigation(tmp_path)
    run.provider.overrides['따라와'] = AgentDecision(
        type='tool_call', tool_name=tool, arguments=arguments,
        message='모델이 만든 실행 완료 주장',
    )
    run.start(targets)
    run.observe_map(selected)
    run.say('따라와')
    assert len(run.provider.calls) == 1
    assert run.manager_goals == []
    assert run.events == []
    assert all(not goals for goals in run.actuators.goals.values())


def test_model_cannot_reenable_navigation_when_target_configuration_is_disabled(speech_graph):
    run = speech_graph
    run.provider.overrides['거실로 가'] = AgentDecision(
        type='tool_call', tool_name='request_navigation', arguments={'location': '거실'},
        message='모델이 만든 실행 완료 주장',
    )
    run.start()
    run.say('거실로 가')
    assert len(run.provider.calls) == 1
    assert 'request_navigation' not in run.provider.calls[0].available_tools
    assert run.manager_goals == []
    assert run.events == []
    assert all(not goals for goals in run.actuators.goals.values())
