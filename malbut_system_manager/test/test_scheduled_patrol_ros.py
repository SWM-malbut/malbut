"""Run scheduled patrols through real ROS Manager and cancelable fake patrol."""

from datetime import datetime
from threading import Event, Thread
from types import SimpleNamespace
import time

from action_msgs.msg import GoalStatus
from action_msgs.srv import CancelGoal
from malbut_interfaces.action import ExecuteMission, Patrol
from malbut_interfaces.msg import SpeechInputStatus, SpeechTranscript
import pytest
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from malbut_system_manager.models import MissionRecord
from malbut_system_manager.patrol_schedule import TIMEZONE
from malbut_system_manager import scheduled_patrol
from malbut_system_manager.system_manager_node import SystemManagerNode


def _wait(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < deadline, 'ROS condition timed out'
        time.sleep(.01)


def _result(future):
    _wait(future.done)
    return future.result()


class PatrolServer(Node):
    """Record starts and wait for either success or downstream cancellation."""

    def __init__(self):
        super().__init__('test_scheduled_patrol_server')
        self.requests = []
        self.release = Event()
        self.canceled = Event()
        self.server = ActionServer(
            self, Patrol, '/patrol', execute_callback=self._execute,
            goal_callback=lambda _: GoalResponse.ACCEPT,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup())

    def _execute(self, goal):
        self.requests.append(goal.request)
        deadline = time.monotonic() + 10
        while not self.release.is_set() and not goal.is_cancel_requested:
            if time.monotonic() >= deadline:
                goal.abort()
                return Patrol.Result(success=False)
            time.sleep(.01)
        if goal.is_cancel_requested:
            self.canceled.set()
            goal.canceled()
            return Patrol.Result(success=False, message='canceled')
        goal.succeed()
        return Patrol.Result(success=True, message='returned to origin')

    def destroy_node(self):
        """Destroy the fake action server before its ROS node."""
        self.server.destroy()
        return super().destroy_node()


@pytest.fixture
def robot(tmp_path, monkeypatch):
    """Use an isolated receipt database and a mutable clock, never real motion."""
    now = [datetime(2026, 11, 2, 9, 59, tzinfo=TIMEZONE)]
    monkeypatch.setattr(scheduled_patrol, 'utc_now', lambda: now[0])
    rclpy.init(args=['--ros-args', '-p', 'scheduled_patrol_enabled:=true',
                     '-p', f'scheduled_patrol_state_path:={tmp_path}/receipts.db'])
    server = PatrolServer()
    manager = SystemManagerNode()
    adapter = manager._scheduled_patrol
    adapter.timer.cancel()
    executor = MultiThreadedExecutor(num_threads=6)
    executor.add_node(manager)
    executor.add_node(server)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        assert adapter.client.wait_for_server(timeout_sec=8)
        mission = MissionRecord('discover', manager._registry.get('patrol'), {})
        assert manager._executor_bridge._client_for(mission).wait_for_server(timeout_sec=8)
        yield manager, server, now
    finally:
        server.release.set()
        manager.begin_shutdown()
        _wait(lambda: manager.downstream_execution_count == 0)
        executor.shutdown(timeout_sec=8)
        thread.join(8)
        manager.destroy_node()
        server.destroy_node()
        rclpy.shutdown()


def _ten(now):
    now[0] = now[0].replace(hour=10, minute=0)


def test_scheduled_patrol_uses_normal_goal_defaults_and_finishes_once(robot):
    """The same action owns analysis/return behavior; scheduler only starts it."""
    manager, server, now = robot
    adapter = manager._scheduled_patrol
    _ten(now)
    adapter.tick()
    _wait(lambda: len(server.requests) == 1)
    assert server.requests[0].thoroughness == Patrol.Goal.NORMAL
    assert len(list(manager._state.all())) == 1
    mission_id = next(iter(manager._state.all())).mission_id
    assert mission_id in manager._contexts
    adapter.tick()
    server.release.set()
    _wait(lambda: not adapter.goal_ids)
    _wait(lambda: not list(manager._state.all()))
    adapter.tick()
    assert len(server.requests) == 1


@pytest.mark.parametrize('blocker', [
    'booting', 'emergency', 'charging', 'pending', 'suspended', 'speech', 'playback',
])
def test_busy_schedule_waits_without_preempting_then_runs(robot, blocker):
    """Busy gates retain the slot without admitting a replacement mission."""
    manager, server, now = robot
    adapter = manager._scheduled_patrol
    with manager._effects_lock:
        if blocker == 'booting':
            manager._state.ready = False
        elif blocker == 'emergency':
            manager._state.emergency = True
        elif blocker == 'charging':
            manager._state.recharging = True
        elif blocker in ('pending', 'suspended'):
            mission = MissionRecord('existing', manager._registry.get('patrol'), {})
            getattr(manager._state, blocker)['existing'] = mission
        elif blocker == 'speech':
            adapter._input(SpeechInputStatus(state='started', utterance_id='test'))
            adapter._input(SpeechTranscript(utterance_id='test', text='test'))
        else:
            manager._speech.playbacks['test'] = object()
        _ten(now)
        adapter.tick()
        assert not adapter.goal_ids and not server.requests
        manager._state.ready = True
        manager._state.emergency = manager._state.recharging = False
        manager._state.pending.clear()
        manager._state.suspended.clear()
        manager._speech.playbacks.clear()
        adapter.speech_until = 0
    now[0] = now[0].replace(minute=10)
    adapter.tick()
    _wait(lambda: len(server.requests) == 1)


def test_manual_patrol_is_not_replaced_by_scheduled_patrol(robot):
    """Even equal LOW priority work must finish before the scheduled patrol."""
    manager, server, now = robot
    client = ActionClient(server, ExecuteMission, '/malbut/mission/execute')
    try:
        assert client.wait_for_server(timeout_sec=8)
        goal = _result(client.send_goal_async(ExecuteMission.Goal(
            capability_id='patrol', arguments_yaml='{}')))
        result = goal.get_result_async()
        _wait(lambda: len(server.requests) == 1)
        _ten(now)
        manager._scheduled_patrol.tick()
        assert not server.canceled.is_set()
        assert not manager._scheduled_patrol.goal_ids
        server.release.set()
        assert _result(result).status == GoalStatus.STATUS_SUCCEEDED
        manager._scheduled_patrol.tick()
        _wait(lambda: len(server.requests) == 2)
    finally:
        client.destroy()


def test_scheduled_patrol_can_be_canceled_by_public_mission_id(robot):
    """Web/voice cancellation uses the same public action UUID as manual work."""
    manager, server, now = robot
    _ten(now)
    manager._scheduled_patrol.tick()
    _wait(lambda: len(server.requests) == 1)
    mission_id = next(iter(manager._state.all())).mission_id
    client = server.create_client(CancelGoal, '/malbut/mission/execute/_action/cancel_goal')
    assert client.wait_for_service(timeout_sec=8)
    request = CancelGoal.Request()
    request.goal_info.goal_id.uuid = list(bytes.fromhex(mission_id))
    response = _result(client.call_async(request))
    assert response.return_code == CancelGoal.Response.ERROR_NONE
    _wait(server.canceled.is_set)
    _wait(lambda: not manager._scheduled_patrol.goal_ids)
    manager._scheduled_patrol.tick()
    assert len(server.requests) == 1


@pytest.mark.parametrize('blocker', ['speech', 'mission'])
def test_busy_race_is_rechecked_at_admission(robot, monkeypatch, blocker):
    """Work arriving after send cannot be preempted by the scheduled goal."""
    manager, server, now = robot
    adapter = manager._scheduled_patrol
    send = adapter.client.send_goal_async

    def concurrent_work(*args, **kwargs):
        if blocker == 'speech':
            adapter.speech_until = time.monotonic() + 120
        else:
            manager._state.add_pending(MissionRecord(
                'manual-race', manager._registry.get('patrol'), {}))
        return send(*args, **kwargs)

    monkeypatch.setattr(adapter.client, 'send_goal_async', concurrent_work)
    _ten(now)
    adapter.tick()
    _wait(lambda: not adapter.goal_ids)
    assert not server.requests
    if blocker == 'mission':
        assert 'manual-race' in manager._state.pending
        manager._state.pending.clear()


def test_busy_deadline_expires_without_a_late_start(robot):
    """A conversation longer than the grace window skips this occurrence."""
    manager, server, now = robot
    adapter = manager._scheduled_patrol
    adapter.speech_until = time.monotonic() + 120
    _ten(now)
    adapter.tick()
    now[0] = now[0].replace(minute=30)
    adapter.speech_until = 0
    adapter.tick()
    assert not adapter.goal_ids and not server.requests


@pytest.mark.parametrize('enabled, simulated', [(False, False), (True, True)])
def test_disabled_or_simulated_manager_never_opens_schedule(tmp_path, enabled, simulated):
    """Standalone opt-out and simulation cannot accidentally use wall-time motion."""
    path = tmp_path / 'unused.db'
    rclpy.init(args=['--ros-args', '-p', f'scheduled_patrol_enabled:={str(enabled).lower()}',
                     '-p', f'use_sim_time:={str(simulated).lower()}',
                     '-p', f'scheduled_patrol_state_path:={path}'])
    manager = SystemManagerNode()
    try:
        adapter = manager._scheduled_patrol
        assert adapter.schedule is None and adapter.timer is None
        adapter.tick()
        assert not path.exists()
    finally:
        manager.destroy_node()
        rclpy.shutdown()


def test_receipt_failure_prevents_motion(robot, monkeypatch):
    """Never send if the durable duplicate guard cannot commit."""
    import sqlite3
    manager, server, now = robot
    adapter = manager._scheduled_patrol

    def failed_commit(_now):
        raise sqlite3.OperationalError('disk full')

    monkeypatch.setattr(adapter.schedule, 'claim', failed_commit)
    _ten(now)
    adapter.tick()
    assert adapter.failed
    assert not adapter.goal_ids and not server.requests


@pytest.mark.parametrize('blocker', ['patrol', 'playback'])
def test_conversation_positioning_cannot_preempt_busy_manager(robot, blocker):
    """Admission rechecks availability, even after the Agent observed idle."""
    manager, server, _ = robot
    client = ActionClient(server, ExecuteMission, '/malbut/mission/execute')
    try:
        assert client.wait_for_server(timeout_sec=8)
        if blocker == 'patrol':
            patrol = _result(client.send_goal_async(ExecuteMission.Goal(
                capability_id='patrol', arguments_yaml='{}')))
            patrol_result = patrol.get_result_async()
            _wait(lambda: bool(server.requests))
        else:
            with manager._effects_lock:
                manager._speech.playbacks['test-conversation-blocker'] = SimpleNamespace(
                    mission_id='other-conversation')
        approach = _result(client.send_goal_async(ExecuteMission.Goal(
            capability_id='follow_person',
            arguments_yaml='desired_distance_m: 1.5\napproach_only: true')))
        result = _result(approach.get_result_async())
        assert result.status == GoalStatus.STATUS_ABORTED
        assert 'manager is busy' in result.result.message
        assert not server.canceled.is_set()
        if blocker == 'patrol':
            server.release.set()
            assert _result(patrol_result).status == GoalStatus.STATUS_SUCCEEDED
    finally:
        with manager._effects_lock:
            manager._speech.playbacks.pop('test-conversation-blocker', None)
        client.destroy()
