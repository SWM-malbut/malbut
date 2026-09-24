"""Exercise real DDS and Action futures without a model, microphone or speaker."""

import json
from threading import Thread
import time

import pytest

rclpy = pytest.importorskip('rclpy')
from rclpy.action import ActionServer, CancelResponse  # noqa: E402
from rclpy.callback_groups import ReentrantCallbackGroup  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from std_msgs.msg import String  # noqa: E402
from malbut_interfaces.action import ConfirmSituation  # noqa: E402

from malbut_system_manager.fall_confirmation_link import (  # noqa: E402
    CONFIRM_SITUATION_ACTION, FallConfirmationLink,
)


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), 'ROS confirmation did not reach the expected state'


class FakeAgent(Node):
    def __init__(self):
        super().__init__('fake_confirmation_agent')
        self.requests = []
        self.cancelled = []
        self.hold = set()
        self.abort = set()
        self.server = ActionServer(
            self, ConfirmSituation, CONFIRM_SITUATION_ACTION,
            execute_callback=self.execute, callback_group=ReentrantCallbackGroup(),
            cancel_callback=lambda _: CancelResponse.ACCEPT)

    def execute(self, handle):
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
def graph():
    rclpy.init()
    manager = Node('test_confirmation_manager')
    link = FallConfirmationLink(manager, runtime_id='test-vlm')
    agent = FakeAgent()
    runtime = Node('fake_fall_runtime')
    events = runtime.create_publisher(String, '/malbut/falls/runtime/events', 50)
    decisions = []
    runtime.create_subscription(String, '/malbut/falls/runtime/decision',
                                lambda msg: decisions.append(json.loads(msg.data)), 10)
    executor = MultiThreadedExecutor(num_threads=4)
    for node in (manager, agent, runtime):
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
    link.close()
    agent.hold.clear()
    executor.shutdown(timeout_sec=3)
    thread.join(timeout=3)
    link.destroy()
    for node in (manager, agent, runtime):
        node.destroy_node()
    rclpy.shutdown()


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


def test_continuous_server_loss_fails_but_short_discovery_gap_does_not(graph, monkeypatch):
    link, agent, decisions, publish = graph
    now = [100.0]
    link.clock = lambda: now[0]
    agent.hold.add('question')
    publish()
    wait_for(lambda: link.handle is not None)
    monkeypatch.setattr(link.client, 'server_is_ready', lambda: False)
    link.tick()
    now[0] += 4
    monkeypatch.setattr(link.client, 'server_is_ready', lambda: True)
    link.tick()
    assert link.server_missing_since is None
    assert not decisions
    monkeypatch.setattr(link.client, 'server_is_ready', lambda: False)
    link.tick()
    now[0] += 5
    link.tick()
    wait_for(lambda: len(decisions) == 1)
    assert decisions[0]['action'] == 'confirmation_failed'
    assert 'help_needed' not in decisions[0]
    assert 'situation_assessment' not in decisions[0]
    monkeypatch.setattr(link.client, 'server_is_ready', lambda: True)
    publish(incident_id='second', question_id='next-question')
    wait_for(lambda: len(decisions) == 2)
    assert decisions[1]['question_id'] == 'next-question'
