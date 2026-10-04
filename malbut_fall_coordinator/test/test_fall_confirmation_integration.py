"""Exercise real DDS and Action futures without a model, microphone or speaker."""

import json
from pathlib import Path
from threading import Thread
import time

import pytest
import yaml

rclpy = pytest.importorskip('rclpy')
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse  # noqa: E402
from rclpy.callback_groups import ReentrantCallbackGroup  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from std_msgs.msg import String  # noqa: E402
from malbut_interfaces.action import ConfirmSituation, ExecuteMission  # noqa: E402
from malbut_system_manager.system_manager_node import SystemManagerNode  # noqa: E402
from malbut_fall_coordinator.node import FallCoordinatorNode  # noqa: E402

CONFIRM_SITUATION_ACTION = '/malbut/agent/confirm_situation'


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), 'ROS confirmation did not reach the expected state'


class FakeAgent(Node):
    def __init__(self, action=CONFIRM_SITUATION_ACTION, name='fake_confirmation_agent'):
        super().__init__(name)
        self.requests = []
        self.cancelled = []
        self.hold = set()
        self.abort = set()
        # Keep configurable local replies alongside upstream retry/order hooks.
        self.results = {}
        self.reject_once = False
        self.on_start = lambda: None
        self.server = ActionServer(
            self, ConfirmSituation, action,
            execute_callback=self.execute, callback_group=ReentrantCallbackGroup(),
            goal_callback=self.goal,
            cancel_callback=lambda _: CancelResponse.ACCEPT)

    def goal(self, _):
        if self.reject_once:
            self.reject_once = False
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def execute(self, handle):
        self.on_start()
        request = handle.request
        self.requests.append(request)
        deadline = time.monotonic() + 6
        while request.request_id in self.hold and time.monotonic() < deadline:
            if handle.is_cancel_requested:
                self.cancelled.append(request.request_id)
                handle.canceled()
                return ConfirmSituation.Result()
            time.sleep(0.01)
        if request.request_id in self.abort:
            handle.abort()
            return ConfirmSituation.Result()
        handle.succeed()
        assessment, help_needed = self.results.get(request.request_id, ('resolved', False))
        return ConfirmSituation.Result(situation_assessment=assessment, help_needed=help_needed)

    def destroy_node(self):
        self.hold.clear()
        self.server.destroy()
        return super().destroy_node()


@pytest.fixture
def graph(tmp_path):
    rclpy.init(args=['--ros-args', '-p', 'runtime_id:=test-coordinator',
                     '-p', 'bridge_runtime_id:=test-bridge', '-p', 'vlm_runtime_id:=test-vlm'])
    manifest = Path(__file__).parents[2] / 'malbut_interfaces/capabilities/fall_confirmation.yaml'
    document = yaml.safe_load(manifest.read_text())
    (tmp_path / 'fall.yaml').write_text(yaml.safe_dump(document))
    # Exercise real manager arbitration against another BASE mission, without
    # launching navigation or producing any velocity commands.
    document['capability']['id'] = 'test_drive'
    document['command']['name'] = '/test_fall_drive'
    document['execution'].update(priority='NORMAL', resources=['BASE'])
    (tmp_path / 'drive.yaml').write_text(yaml.safe_dump(document))
    manager = SystemManagerNode(manifest_directory=str(tmp_path))
    coordinator = FallCoordinatorNode()
    link = coordinator.confirmation
    agent = FakeAgent()
    drive = FakeAgent('/test_fall_drive', 'fake_drive')
    agent.drive = drive
    runtime = Node('fake_fall_runtime')
    events = runtime.create_publisher(String, '/malbut/falls/runtime/events', 50)
    decisions = []
    runtime.create_subscription(String, '/malbut/falls/runtime/decision',
                                lambda msg: decisions.append(json.loads(msg.data)), 10)
    client = ActionClient(runtime, ExecuteMission, '/malbut/mission/execute')
    agent.missions = client
    nodes = (manager, coordinator, agent, drive, runtime)
    executor = MultiThreadedExecutor(num_threads=6)
    for node in nodes:
        executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    wait_for(lambda: events.get_subscription_count() > 0 and link.client.server_is_ready())

    def publish(**changes):
        data = dict(kind='question_requested', boot_id='test-boot', runtime_id='test-vlm',
                    incident_id='incident', question_id='question', subject_key='person',
                    evidence_revision=1, video_assessment='suspected_fall') | changes
        events.publish(String(data=json.dumps(data)))

    yield link, agent, decisions, publish
    coordinator.close()
    agent.hold.clear()
    drive.hold.clear()
    manager.begin_shutdown()
    wait_for(lambda: manager.downstream_execution_count == 0)
    executor.shutdown(timeout_sec=3)
    thread.join(timeout=3)
    client.destroy()
    for node in nodes:
        node.destroy_node()
    rclpy.shutdown()


def test_urgent_confirmation_waits_for_conflicting_motion_to_stop(graph):
    link, agent, decisions, publish = graph
    agent.drive.hold.add('drive')
    future = agent.missions.send_goal_async(ExecuteMission.Goal(
        capability_id='test_drive', arguments_yaml=json.dumps(dict(
            request_id='drive', situation_type='test', summary='test'))))
    wait_for(future.done)
    handle = future.result()
    assert handle.accepted
    result = handle.get_result_async()
    wait_for(lambda: len(agent.drive.requests) == 1)
    stopped_before_confirmation = []
    agent.on_start = lambda: stopped_before_confirmation.append('drive' in agent.drive.cancelled)
    publish()
    wait_for(lambda: decisions and result.done())
    assert stopped_before_confirmation == [True]
    assert result.result().result.message == 'mission preempted by a replacement request'
    assert decisions[0]['action'] == 'confirmation_result'
    assert len(agent.drive.requests) == 1


def test_temporarily_busy_agent_retries_through_manager_without_false_failure(graph):
    link, agent, decisions, publish = graph
    agent.reject_once = True
    publish()
    wait_for(lambda: len(decisions) == 1)
    assert len(agent.requests) == 1
    assert decisions[0]['action'] == 'confirmation_result'


def test_vlm_event_is_sent_via_manager_action_and_only_final_result_is_forwarded(graph):
    link, agent, decisions, publish = graph
    publish()
    wait_for(lambda: len(decisions) == 1)
    assert len(agent.requests) == 1
    assert agent.requests[0].request_id == 'question'
    assert agent.requests[0].situation_type == 'fall'
    assert '낙상이 의심' in agent.requests[0].summary
    assert decisions[0] == dict(
        action='confirmation_result', boot_id='test-boot', incident_id='incident',
        question_id='question', evidence_revision=1, subject_key='person',
        situation_assessment='resolved', help_needed=False)
    publish()
    wait_for(lambda: len(decisions) == 2)
    assert len(agent.requests) == 1
    assert decisions[0] == decisions[1]


def test_new_revision_waits_for_current_conversation_and_preserves_its_result(graph):
    link, agent, decisions, publish = graph
    agent.hold.add('question')
    publish()
    wait_for(lambda: len(agent.requests) == 1)
    publish(question_id='new-question', evidence_revision=2)
    wait_for(lambda: link.coordinator.revisions.get('incident') == 2)
    assert link.request.request_id == 'question'
    assert not agent.cancelled and not decisions
    agent.hold.remove('question')
    wait_for(lambda: len(decisions) == 1)
    assert decisions[0]['question_id'] == 'question'
    assert decisions[0]['evidence_revision'] == 1
    assert len(agent.requests) == 1 and not agent.cancelled
    publish(question_id='new-question', evidence_revision=2)
    wait_for(lambda: len(decisions) == 2)
    assert decisions[1]['question_id'] == 'new-question'
    assert decisions[1]['evidence_revision'] == 2


def test_aborted_agent_is_runtime_failure_without_fabricated_user_judgment(graph):
    link, agent, decisions, publish = graph
    agent.abort.add('question')
    publish()
    wait_for(lambda: len(decisions) == 1)
    assert decisions[0]['action'] == 'confirmation_failed'
    assert 'help_needed' not in decisions[0]
    assert 'situation_assessment' not in decisions[0]


def test_accepted_goal_result_timeout_releases_queue_without_inventing_silence(graph):
    link, agent, decisions, publish = graph
    now = [100.0]
    link.clock = lambda: now[0]
    agent.hold.add('question')
    publish()
    wait_for(lambda: link.handle is not None)
    publish(incident_id='second', question_id='next-question')
    wait_for(lambda: len(link.coordinator.requests) == 2)
    now[0] += 609
    link.tick()
    assert not decisions
    now[0] += 1
    link.tick()
    wait_for(lambda: len(decisions) == 2)
    assert decisions[0]['action'] == 'confirmation_failed'
    assert 'help_needed' not in decisions[0]
    assert 'situation_assessment' not in decisions[0]
    assert decisions[1]['question_id'] == 'next-question'
    assert decisions[1]['action'] == 'confirmation_result'


@pytest.mark.parametrize('peer', ['client', 'agent_presence'])
def test_continuous_server_loss_fails_but_short_discovery_gap_does_not(graph, monkeypatch, peer):
    link, agent, decisions, publish = graph
    now = [100.0]
    link.clock = lambda: now[0]
    agent.hold.add('question')
    publish()
    wait_for(lambda: link.handle is not None)
    server = getattr(link, peer)
    monkeypatch.setattr(server, 'server_is_ready', lambda: False)
    link.tick()
    now[0] += 4
    monkeypatch.setattr(server, 'server_is_ready', lambda: True)
    link.tick()
    assert link.server_missing_since is None
    assert not decisions
    monkeypatch.setattr(server, 'server_is_ready', lambda: False)
    link.tick()
    now[0] += 5
    link.tick()
    wait_for(lambda: len(decisions) == 1)
    assert decisions[0]['action'] == 'confirmation_failed'
    assert 'help_needed' not in decisions[0]
    assert 'situation_assessment' not in decisions[0]
    monkeypatch.setattr(server, 'server_is_ready', lambda: True)
    publish(incident_id='second', question_id='next-question')
    wait_for(lambda: len(decisions) == 2)
    assert decisions[1]['question_id'] == 'next-question'


@pytest.mark.parametrize('assessment', ['resolved', 'unknown', 'confirmed_incident'])
@pytest.mark.parametrize('help_needed', [False, True])
def test_real_cloud_only_monitor_event_reaches_agent_without_pose_and_returns_safely(
        graph, monkeypatch, assessment, help_needed):
    import asyncio

    pytest.importorskip('malbut_agent_server.application.cloud_fall_monitor')
    from malbut_agent_server.application.cloud_fall_monitor import CloudFallMonitor
    from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
    from malbut_agent_server.domain.fall_monitoring import (
        CloudFallReply, FallRuntimePolicy, IncidentState, RgbFrame, VideoAssessment,
    )
    from malbut_agent_server.fall_runtime import apply_decision, event_metadata

    class Provider:
        execution_target = 'cloud'
        calls = 0

        async def analyze(self, request):
            self.calls += 1
            return CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'no Pose/box available')

    now = [100.0]
    provider = Provider()
    monitor = CloudFallMonitor(
        device_id='test-robot', boot_id='test-boot', provider=provider,
        clock=lambda: now[0],
        policy=FallRuntimePolicy.agreed(
            retry_interval_s=3, max_person_observation_age_s=2, clip_window_s=5,
            max_frame_age_s=2, max_calls_per_minute=20, max_incidents=10, max_images=12),
        buffer=FallFrameBuffer(retention_s=10, max_bytes=10000, max_frames=64))
    monitor.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    now[0] = 160.0
    monitor.ingest_rgb(RgbFrame(160.0, b'\xff\xd8test\xff\xd9'))
    assert asyncio.run(monitor.run_once())
    events = monitor.drain_events()
    q = next(e for e in events if e.kind == 'question_requested')
    assert q.confirmation_scope == 'scene' and q.subject_key is None
    link, agent, decisions, publish = graph
    sent_missions = []
    send_goal = link.client.send_goal_async

    def send_via_manager(goal, **kwargs):
        sent_missions.append(goal)
        return send_goal(goal, **kwargs)

    monkeypatch.setattr(link.client, 'send_goal_async', send_via_manager)
    agent.results[q.question_id] = (assessment, help_needed)
    for item in events:
        publish(**event_metadata(item))
    wait_for(lambda: len(decisions) == 1)
    assert len(sent_missions) == 1
    assert sent_missions[0].capability_id == 'fall_confirmation'
    assert json.loads(sent_missions[0].arguments_yaml)['request_id'] == q.question_id
    assert len(agent.requests) == 1
    assert '특정인을 지목하지 말고' in agent.requests[0].summary
    assert decisions[0]['subject_key'] is None
    assert decisions[0]['situation_assessment'] == assessment
    assert decisions[0]['help_needed'] is help_needed
    assert apply_decision(monitor, json.dumps(decisions[0]))
    incident = monitor.incident(q.incident_id)
    expected = IncidentState.HELP_REQUIRED if help_needed else IncidentState.RECHECK_REQUIRED
    assert incident.state is expected and incident.subject_key is None
    assert incident.close_reason is None
    # Retry transport delivery, not a second question or analysis.
    publish(**event_metadata(q))
    wait_for(lambda: len(decisions) == 2)
    assert decisions[1] == decisions[0]
    assert apply_decision(monitor, json.dumps(decisions[1]))
    assert len(sent_missions) == len(agent.requests) == provider.calls == 1
    asyncio.run(monitor.close())
