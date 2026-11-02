"""Turn once toward a fresh microphone angle using the existing managed Spin."""

import json
import math
import signal
import time
from uuid import uuid4

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
        self.stationary_since = None
        self.runtime_idle = False
        self.inflight = self.cancel_requested = self.closed = False
        self.goal_handle = None
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
        age = self.get_clock().now().nanoseconds / 1e9 - stamp.sec - stamp.nanosec / 1e9
        v = message.twist.twist
        values = (v.linear.x, v.linear.y, v.angular.z)
        valid = 0 <= age <= 1.5 and all(math.isfinite(x) for x in values)
        if (not valid
                or math.hypot(*values[:2]) > .02 or abs(values[2]) > .03):
            self.stationary_since = None
        elif self.stationary_since is None:
            self.stationary_since = now
        self.odom_at = now if valid else -math.inf

    def _available(self, now):
        state = self.state
        if (self.closed or state is None or not self.reader.connected
                or self.count_publishers('/malbut/state') != 1
                or self.count_publishers('/malbut/runtime/state') != 1
                or not 0 <= now - self.odom_at <= 1.5
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
            if (not allowed or now >= self.deadline
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
            self.goal_handle.get_result_async().add_done_callback(self._finished)
            if self.cancel_requested or self.clock() >= self.accept_deadline:
                self.cancel_requested = False
                self.cancel()
        except Exception:
            self.cancel_requested = True
            self.get_logger().error('sound_turn_acceptance_unconfirmed')

    def _finished(self, future):
        try:
            result = future.result()
            self.get_logger().info(f'sound_turn_finished status={result.status}')
        except Exception:
            self.cancel()
            self.get_logger().error('sound_turn_result_unconfirmed')
            return
        self._done()

    def _done(self):
        self.inflight, self.goal_handle = False, None
        self.stationary_since = None
        self.next_turn = self.clock() + 3

    def cancel(self):
        """Cancel only this node's mission, retaining ownership until its result."""
        if not self.inflight:
            return
        if self.goal_handle is not None and not self.cancel_requested:
            try:
                self.goal_handle.cancel_goal_async()
            except Exception:
                self.get_logger().error('sound_turn_cancel_unconfirmed')
        self.cancel_requested = True

    def close(self):
        """Stop receiving directions and request cancellation of the owned turn."""
        if not self.closed:
            self.closed = True
            self.timer.cancel()
            self.reader.close()
            self.cancel()


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
