"""Submit fixed daily patrols through the ordinary, cancelable Manager action."""

from datetime import datetime, timezone
import sqlite3
import time
from uuid import uuid4

from malbut_interfaces.action import ExecuteMission
from malbut_interfaces.msg import SpeechInputStatus, SpeechPlaybackStatus, SpeechTranscript
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from unique_identifier_msgs.msg import UUID

from .models import SystemState
from .patrol_schedule import DailyPatrolSchedule


def utc_now():
    """Read wall time independently of the ROS simulation clock."""
    return datetime.now(timezone.utc)


class ScheduledPatrol:
    """Defer busy slots for 30 minutes and never preempt existing work."""

    def __init__(self, node):
        self.node = node
        self.closed = False
        self.failed = False
        self.goal_ids = set()
        self.speech_until = time.monotonic()
        self.schedule = None
        self.client = None
        self.timer = None
        descriptor = ParameterDescriptor(read_only=True)
        enabled = node.declare_parameter(
            'scheduled_patrol_enabled', False, descriptor).value
        path = node.declare_parameter(
            'scheduled_patrol_state_path',
            '~/.local/state/malbut/patrol-schedule.sqlite3', descriptor).value
        if not enabled or node.get_parameter('use_sim_time').value:
            return
        try:
            self.schedule = DailyPatrolSchedule(path, utc_now())
        except (OSError, sqlite3.Error) as error:
            self.failed = True
            node.get_logger().error(f'Scheduled patrol disabled: {error}')
            return
        self.client = ActionClient(
            node, ExecuteMission, '/malbut/mission/execute',
            callback_group=node._client_group)
        self.subscriptions = [
            node.create_subscription(
                SpeechInputStatus, '/malbut/speech/input_status', self._input, 10,
                callback_group=node._server_group),
            node.create_subscription(
                SpeechTranscript, '/malbut/speech/transcript', self._input, 10,
                callback_group=node._server_group),
            node.create_subscription(
                SpeechPlaybackStatus, '/malbut/speech/playback_status', self._playback, 10,
                callback_group=node._server_group),
        ]
        self.timer = node.create_timer(
            5.0, self.tick, callback_group=node._server_group,
            clock=Clock(clock_type=ClockType.STEADY_TIME))
        node.get_logger().info('Daily patrol enabled: 10:00, 16:00 Asia/Seoul')

    def _input(self, _message):
        # Includes listening and inference; keep no transcript or personal data.
        with self.node._effects_lock:
            self.speech_until = max(self.speech_until, time.monotonic() + 120)

    def _playback(self, _message):
        # Leave room for a follow-up after playback, including proactive speech.
        with self.node._effects_lock:
            self.speech_until = max(self.speech_until, time.monotonic() + 30)

    def available(self):
        """Check under the Manager effects lock, also at final admission."""
        node = self.node
        return (not self.closed and not self.failed and node._accepting_goals
                and node._state.system_state is SystemState.IDLE
                and not any(node._state.all()) and not node._speech.playbacks
                and time.monotonic() >= self.speech_until)

    def tick(self):
        """Claim and send at most one occurrence without blocking ROS callbacks."""
        with self.node._effects_lock:
            if self.closed or self.failed or self.schedule is None:
                return
            now = utc_now()
            if (self.schedule.due(now) is None or self.goal_ids
                    or not self.available() or not self.client.server_is_ready()):
                return
            try:
                slot = self.schedule.claim(now)
            except sqlite3.Error as error:
                self.failed = True
                self.node.get_logger().error(f'Scheduled patrol disabled: {error}')
                return
            if slot is None:
                return
            identity = uuid4()
            self.goal_ids.add(identity.hex)
            try:
                future = self.client.send_goal_async(
                    ExecuteMission.Goal(capability_id='patrol', arguments_yaml='{}'),
                    goal_uuid=UUID(uuid=list(identity.bytes)))
                future.add_done_callback(
                    lambda done: self._accepted(identity.hex, done))
                self.node.get_logger().info(f'Scheduled patrol requested: {slot.isoformat()}')
            except Exception as error:
                # A failed send is not proof that the request was not received.
                self.failed = True
                self.node.get_logger().error(f'Scheduled patrol dispatch unresolved: {error}')

    def _accepted(self, mission_id, future):
        with self.node._effects_lock:
            try:
                goal = future.result()
                if not goal.accepted:
                    self.goal_ids.discard(mission_id)
                    self.node.get_logger().warning('Scheduled patrol request rejected')
                    return
                goal.get_result_async().add_done_callback(
                    lambda done: self._finished(mission_id, done))
            except Exception as error:
                self.failed = True
                self.node.get_logger().error(f'Scheduled patrol dispatch unresolved: {error}')

    def _finished(self, mission_id, future):
        with self.node._effects_lock:
            try:
                result = future.result()
                self.goal_ids.discard(mission_id)
                self.node.get_logger().info(
                    f'Scheduled patrol finished: status={result.status}')
            except Exception as error:
                self.failed = True
                self.node.get_logger().error(f'Scheduled patrol result unresolved: {error}')

    def close(self):
        """Stop generating missions before Manager shutdown cancels live work."""
        with self.node._effects_lock:
            if self.closed:
                return
            self.closed = True
            if self.timer is not None:
                self.timer.cancel()
            if self.client is not None:
                self.client.destroy()
            if self.schedule is not None:
                self.schedule.close()
