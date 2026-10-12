"""The Agent process runs the fall rules beside, not inside, the dialogue (real ROS).

Each test starts `ros_communication` as its own process, as Bringup does, so the
signal handling and the two executors are the real ones.
"""

import json
import os
import signal
import subprocess
import sys
from threading import Thread
import time
import uuid

import pytest

rclpy = pytest.importorskip('rclpy')
interfaces = pytest.importorskip('malbut_interfaces.msg')
if not hasattr(interfaces, 'FallControlHeartbeat'):
    pytest.skip('malbut_interfaces without fall control messages', allow_module_level=True)

from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse  # noqa: E402
from rclpy.callback_groups import ReentrantCallbackGroup  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy  # noqa: E402
from std_msgs.msg import String  # noqa: E402
from malbut_interfaces.action import ConfirmSituation, ExecuteMission  # noqa: E402
from malbut_interfaces.msg import (  # noqa: E402
    FallControlHeartbeat, FallRuntimeStatus, MissionStatus, SystemState,
)

IDS = ['--fall-runtime-id', 'rules-id', '--fall-bridge-runtime-id', 'bridge-id',
       '--fall-vlm-runtime-id', 'vlm-id']


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class Probe(Node):
    """Stand in for the fall runtime and the Manager around one Agent process."""

    def __init__(self, *, manager=False):
        super().__init__('agent_fall_rules_probe')
        self.beats, self.goals, self.cancels = [], [], []
        self.create_subscription(FallControlHeartbeat, '/malbut/falls/control/heartbeat',
                                 lambda _: self.beats.append(time.monotonic()), 10)
        # The bound fall runtime reports every second; the rules answer with heartbeats.
        self.statuses = self.create_publisher(FallRuntimeStatus, '/malbut/falls/status', 10)
        self.sequence = 0
        self.create_timer(0.5, self.report)
        self.events = self.create_publisher(String, '/malbut/falls/runtime/events', 50)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        self.state = self.create_publisher(SystemState, '/malbut/state', latched)
        self.state.publish(SystemState())  # An idle Manager: the rules may send work.
        if manager:
            self.server = ActionServer(
                self, ExecuteMission, '/malbut/mission/execute', execute_callback=self.execute,
                goal_callback=lambda _: GoalResponse.ACCEPT, cancel_callback=self.cancel,
                callback_group=ReentrantCallbackGroup())

    def report(self):
        self.sequence += 1
        self.statuses.publish(FallRuntimeStatus(runtime_id='vlm-id', sequence=self.sequence))

    def cancel(self, _):
        self.cancels.append(time.monotonic())
        return CancelResponse.ACCEPT

    def execute(self, handle):
        self.goals.append(handle.request.capability_id)
        deadline = time.monotonic() + 20
        while not handle.is_cancel_requested and time.monotonic() < deadline:
            time.sleep(0.02)
        handle.canceled()
        return ExecuteMission.Result()

    def question(self):
        self.events.publish(String(data=json.dumps(dict(
            kind='question_requested', boot_id='boot-1', runtime_id='vlm-id',
            incident_id='incident', question_id='question', subject_key='person',
            evidence_revision=1, video_assessment='suspected_fall'))))

    def names(self):
        return {name for name, _ in self.get_node_names_and_namespaces()}


@pytest.fixture
def agent(tmp_path):
    """Start one Agent process beside a probe on a private ROS domain."""
    domain = str(100 + uuid.uuid4().int % 100)
    previous = os.environ.get('ROS_DOMAIN_ID')
    os.environ['ROS_DOMAIN_ID'] = domain
    rclpy.init()
    executor = MultiThreadedExecutor(num_threads=4)
    started = []

    def start(probe, *arguments, db_path=None):
        executor.add_node(probe)
        if not started:
            Thread(target=executor.spin, daemon=True).start()
        process = subprocess.Popen(
            [sys.executable, '-m', 'malbut_agent_server.ros_communication', *arguments,
             '--db-path', str(db_path or tmp_path / 'receipts.sqlite3'),
             '--conversation-db', str(tmp_path / 'dialogue.sqlite3')],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=dict(os.environ, ROS_DOMAIN_ID=domain))
        started.append((probe, process))
        return process

    yield start
    for probe, process in started:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        probe.destroy_node()
    executor.shutdown(timeout_sec=3)
    rclpy.shutdown()
    if previous is None:
        os.environ.pop('ROS_DOMAIN_ID', None)
    else:
        os.environ['ROS_DOMAIN_ID'] = previous


def stop(process):
    process.send_signal(signal.SIGINT)
    return process.wait(timeout=10)


def test_with_speech_off_only_the_rules_run_without_a_confirmation_server(agent):
    probe = Probe()
    process = agent(probe, '--no-speech', '--fall-rules', *IDS)
    assert wait_for(lambda: len(probe.beats) >= 2)
    assert 'malbut_fall_rules' in probe.names()
    assert 'malbut_agent_communication' not in probe.names()
    # The Manager then reports the Agent unavailable: the question fails at once.
    # (The rules themselves only watch that action as a client.)
    confirm = ActionClient(probe, ConfirmSituation, '/malbut/agent/confirm_situation')
    assert not confirm.wait_for_server(timeout_sec=3.0)
    confirm.destroy()
    assert stop(process) == 0


def test_a_dialogue_that_cannot_start_leaves_the_rules_running(agent, tmp_path):
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('a file where the receipt directory should be')
    probe = Probe()
    process = agent(probe, '--provider', 'mock', '--fall-rules', *IDS,
                    db_path=blocked / 'receipts.sqlite3')
    assert wait_for(lambda: len(probe.beats) >= 3)
    assert 'malbut_fall_rules' in probe.names()
    assert wait_for(lambda: 'malbut_agent_communication' not in probe.names(), 5)
    assert process.poll() is None
    assert stop(process) == 0
    assert 'agent_communication_failed: FileExistsError' in process.stdout.read()


def test_a_signal_cancels_the_fall_mission_the_rules_started(agent):
    probe = Probe(manager=True)
    process = agent(probe, '--no-speech', '--fall-rules', *IDS)
    assert wait_for(lambda: len(probe.beats) >= 1)
    probe.question()
    assert wait_for(lambda: probe.goals == ['fall_confirmation'])
    assert not probe.cancels
    assert stop(process) == 0
    assert probe.cancels  # Sent before the process ended, as the coordinator did.


def test_driven_by_hand_the_rules_wait_say_so_and_unlock_when_stopped(agent):
    """2026-10-12: 직접 움직이기 is never taken; the map screen hears the check waits."""
    probe = Probe(manager=True)
    decisions, statuses = [], []
    probe.create_subscription(String, '/malbut/falls/runtime/decision',
                              lambda m: decisions.append(json.loads(m.data)), 10)
    probe.create_subscription(String, '/malbut/falls/check/status',
                              lambda m: statuses.append((time.monotonic(), json.loads(m.data))),
                              10)
    probe.state.publish(SystemState(active_foreground_missions=[MissionStatus(
        mission_id='m', capability_id='manual_drive', priority=MissionStatus.HIGH,
        state=MissionStatus.RUNNING)]))
    process = agent(probe, '--no-speech', '--fall-rules', *IDS)
    assert wait_for(lambda: len(probe.beats) >= 1)
    probe.question()
    assert wait_for(lambda: statuses and statuses[-1][1]['phase'] == 'wait')
    assert statuses[-1][1] == dict(active=True, phase='wait', waiting_for='manual_drive',
                                   alerted=False, target=None)
    first = len(statuses)
    # The history notes the wait once it lasts three seconds.
    assert wait_for(lambda: any(d['action'] == 'check_waiting' for d in decisions), 6)
    assert [(d['action'], d['reason']) for d in decisions] == [('check_waiting', 'manual_drive')]
    time.sleep(1.5)
    assert len(statuses) - first >= 4  # Every second while it lasts.
    assert not probe.goals  # Nothing is sent while driven by hand.
    probe.state.publish(SystemState())
    assert wait_for(lambda: probe.goals == ['fall_confirmation'])
    assert stop(process) == 0
    assert statuses[-1][1]['active'] is False  # Stopping unlocks the moves at once.
