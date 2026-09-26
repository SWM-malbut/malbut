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
        return ConfirmSituation.Result(situation_assessment='resolved', help_needed=False)

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


def test_new_revision_cancels_previous_conversation_and_ignores_its_result(graph):
    link, agent, decisions, publish = graph
    agent.hold.add('question')
    publish()
    wait_for(lambda: len(agent.requests) == 1)
    publish(kind='incident_updated', evidence_revision=2)
    publish(question_id='new-question', evidence_revision=2)
    wait_for(lambda: 'question' in agent.cancelled and len(decisions) == 1)
    assert decisions[0]['question_id'] == 'new-question'
    assert decisions[0]['evidence_revision'] == 2


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
