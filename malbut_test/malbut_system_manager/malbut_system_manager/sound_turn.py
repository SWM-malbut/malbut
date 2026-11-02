"""Turn once toward a fresh microphone angle using the existing managed Spin."""

import json
import math
import signal
import time
from uuid import uuid4

from action_msgs.msg import GoalStatus
from malbut_interfaces.action import ExecuteMission
from malbut_interfaces.msg import SystemState
from nav_msgs.msg import Odometry
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from std_msgs.msg import String
from unique_identifier_msgs.msg import UUID

from malbut_system_manager.sound_direction import SerialDirections


class SoundTurn(Node):
    """Own only a bounded rotation; never start speech, tracking or navigation."""

    def __init__(self, *, reader=None, clock=time.monotonic):
        """Connect the direction reader to the existing motion owner."""
        super().__init__('sound_turn')
        self.clock = clock
        port = self.declare_parameter('port', '/dev/ring_mic').value
        self.forward = self.declare_parameter('forward_degrees', 150.).value
        if not math.isfinite(self.forward) or not 0 <= self.forward < 360:
            raise ValueError('forward_degrees must be within [0, 360)')
        self.state = None
        self.odom_at = -math.inf
        self.odom_stamp = 0
        self.stationary_since = None
        self.runtime_idle = False
        self.inflight = self.cancel_requested = self.closed = False
        self.goal_handle = None
        self.result_future = self.cancel_future = None
        self.result_retry_at = self.cancel_retry_at = 0.
        self.cancel_confirmed = False
        self.mission_id = ''
        self.next_turn = 0.
        self.client = ActionClient(self, ExecuteMission, '/malbut/mission/execute')
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(SystemState, '/malbut/state', self._state, qos)
        self.create_subscription(String, '/malbut/runtime/state', self._runtime, qos)
        self.create_subscription(Odometry, '/odom', self._odom, qos_profile_sensor_data)
        self.reader = reader if reader is not None else SerialDirections(port, clock=clock)
        self.timer = self.create_timer(.05, self.tick)
        self.get_logger().info('sound_turn_ready: hardware angle -> managed spin only')
        self.get_logger().info('XFM reports hardware wake angles; arbitrary sound is unverified')

    def _state(self, message):
        self.state = message

    def _runtime(self, message):
        try:
            value = json.loads(message.data)
            self.runtime_idle = (value.get('ready') is True and not value.get('error')
                                 and value.get('active_groups') == []
                                 and value.get('capabilities') == [])
        except (ValueError, AttributeError):
            self.runtime_idle = False

    def _odom(self, message):
        now = self.clock()
        if not 0 <= now - self.odom_at <= 1.5:
            self.stationary_since = None
        stamp = message.header.stamp
        stamp_ns = stamp.sec * 10**9 + stamp.nanosec
        age = (self.get_clock().now().nanoseconds - stamp_ns) / 1e9
        v = message.twist.twist
        values = (v.linear.x, v.linear.y, v.angular.z)
        valid = (stamp_ns > self.odom_stamp and 0 <= age <= 1.5
                 and all(math.isfinite(x) for x in values))
        if (not valid
                or math.hypot(*values[:2]) > .02 or abs(values[2]) > .03):
            self.stationary_since = None
        elif self.stationary_since is None:
            self.stationary_since = now
        self.odom_at = now if valid else -math.inf
        if valid:
            self.odom_stamp = stamp_ns

    def _available(self, now):
        state = self.state
        if (self.closed or state is None or not self.reader.connected
                or self.count_publishers('/malbut/state') != 1
                or self.count_publishers('/malbut/runtime/state') != 1
                or not 0 <= now - self.odom_at <= 1.5
                or not 0 <= (self.get_clock().now().nanoseconds - self.odom_stamp) / 1e9 <= 1.5
                or state.control_mode != state.AUTONOMOUS
                or state.system_state not in (state.IDLE, state.EXECUTING_MISSION)):
            return False
        missions = (*state.active_foreground_missions, *state.active_background_missions,
                    *state.pending_missions, *state.suspended_missions)
        return all(self.inflight and m.mission_id == self.mission_id for m in missions)

    def tick(self):
        """Consume directions even while busy so old sounds never trigger later."""
        now = self.clock()
        sample = self.reader.take(now, forward_degrees=self.forward)
        allowed = self._available(now)
        if self.inflight:
            self._request_result()
            if (self.cancel_requested or not allowed or now >= self.deadline
                    or self.goal_handle is None and now >= self.accept_deadline):
                self.cancel()
            return
        if (not allowed or sample is None or now < self.next_turn
                or self.state.system_state != self.state.IDLE
                or not self.runtime_idle
                or self.stationary_since is None or now - self.stationary_since < .5
                or sample[0] < self.stationary_since or abs(sample[1]) < math.radians(8)
                or not self.client.server_is_ready()):
            return
        identity = uuid4()
        self.mission_id = identity.hex
        self.inflight, self.cancel_requested, self.goal_handle = True, False, None
        self.result_future = self.cancel_future = None
        self.result_retry_at = self.cancel_retry_at = 0.
        self.cancel_confirmed = False
        self.deadline, self.accept_deadline = now + 55, now + 5
        goal = ExecuteMission.Goal(capability_id='spin', idle_only=True,
                                   arguments_yaml=json.dumps(dict(
                                       target_yaw=sample[1],
                                       time_allowance=dict(sec=20, nanosec=0))))
        try:
            future = self.client.send_goal_async(goal, goal_uuid=UUID(uuid=list(identity.bytes)))
            future.add_done_callback(self._accepted)
            self.get_logger().info(f'sound_turn_requested degrees={math.degrees(sample[1]):.1f}')
        except Exception:
            self.cancel_requested = True  # Unknown dispatch must not cause a second turn.
            self.get_logger().error('sound_turn_dispatch_unconfirmed')

    def _accepted(self, future):
        try:
            self.goal_handle = future.result()
            if not self.goal_handle.accepted:
                self._done()
                return
            self._request_result()
            if self.cancel_requested or self.clock() >= self.accept_deadline:
                self.cancel()
        except Exception:
            self.cancel()
            self.get_logger().error('sound_turn_acceptance_unconfirmed')

    def _request_result(self):
        if (not self.inflight or self.goal_handle is None or self.result_future is not None
                or self.clock() < self.result_retry_at):
            return
        self.result_retry_at = self.clock() + 1.
        try:
            self.result_future = self.goal_handle.get_result_async()
            self.result_future.add_done_callback(self._finished)
        except Exception:
            self.result_future = None
            self.cancel()
            self.get_logger().error('sound_turn_result_unconfirmed', throttle_duration_sec=5.)

    def _finished(self, future):
        if future is not self.result_future or not self.inflight:
            return
        try:
            result = future.result()
            if result.status not in (GoalStatus.STATUS_SUCCEEDED, GoalStatus.STATUS_CANCELED,
                                     GoalStatus.STATUS_ABORTED):
                raise RuntimeError('mission has no confirmed terminal result')
            self.get_logger().info(f'sound_turn_finished status={result.status}')
        except Exception:
            self.result_future = None
            self.result_retry_at = self.clock() + 1.
            self.cancel()
            self.get_logger().error('sound_turn_result_unconfirmed', throttle_duration_sec=5.)
            return
        self._done()

    def _done(self):
        self.inflight, self.goal_handle = False, None
        self.result_future = self.cancel_future = None
        self.stationary_since = None
        self.next_turn = self.clock() + 3
        if self.closed:
            self.timer.cancel()

    def cancel(self):
        """Cancel only this node's mission, retaining ownership until its result."""
        if not self.inflight:
            return
        self.cancel_requested = True
        if (self.goal_handle is None or self.cancel_confirmed
                or self.clock() < self.cancel_retry_at):
            return
        if self.cancel_future is not None:
            if self.cancel_future.done():
                self._canceled(self.cancel_future)
                return
            expired, self.cancel_future = self.cancel_future, None
            # Detach first: cancel() invokes callbacks and releases rclpy's
            # pending-request bookkeeping, but does not cancel the ROS goal.
            expired.cancel()
            self.get_logger().error('sound_turn_cancel_timeout', throttle_duration_sec=5.)
        self.cancel_retry_at = self.clock() + 1.
        try:
            self.cancel_future = self.goal_handle.cancel_goal_async()
            self.cancel_future.add_done_callback(self._canceled)
        except Exception:
            self.cancel_future = None
            self.get_logger().error('sound_turn_cancel_unconfirmed', throttle_duration_sec=5.)

    def _canceled(self, future):
        if future is not self.cancel_future or not self.inflight:
            return
        self.cancel_future = None
        self.cancel_retry_at = self.clock() + 1.
        try:
            response = future.result()
            self.cancel_confirmed = (response.return_code == response.ERROR_NONE and any(
                bytes(goal.goal_id.uuid).hex() == self.mission_id
                for goal in response.goals_canceling))
        except Exception:
            self.cancel_confirmed = False
        if not self.cancel_confirmed:
            self.get_logger().error('sound_turn_cancel_unconfirmed', throttle_duration_sec=5.)

    def close(self):
        """Stop receiving directions and request cancellation of the owned turn."""
        if not self.closed:
            self.closed = True
            self.reader.close()
            self.cancel()
            if not self.inflight:
                self.timer.cancel()


def main(args=None):
    """Run independently of STT, Agent and person detection."""
    def stop(_signal, _frame):
        raise KeyboardInterrupt

    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    previous_term = signal.signal(signal.SIGTERM, stop)
    node = None
    try:
        node = SoundTurn()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.close()
            deadline = time.monotonic() + 5
            while node.inflight and rclpy.ok() and time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=.05)
            if node.inflight:
                node.get_logger().error('sound_turn_stop_unconfirmed; check manager state')
            node.client.destroy()
            node.destroy_node()
        rclpy.try_shutdown()
        signal.signal(signal.SIGTERM, previous_term)
