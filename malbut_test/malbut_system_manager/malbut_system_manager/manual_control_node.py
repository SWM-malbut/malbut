"""Enter manual_drive on teleop input and end it after a quiet period."""

import time

from geometry_msgs.msg import Twist
from malbut_interfaces.action import ExecuteMission
from malbut_interfaces.msg import SystemState
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Joy
from std_msgs.msg import Empty


EXECUTE_MISSION_ACTION = '/malbut/mission/execute'
# Vendor joystick axes used for driving: lx, ly, rx.
DRIVE_AXES = (0, 1, 2)


class ManualActivity:
    """Decide from operator input whether manual control is still in use."""

    def __init__(self, idle_timeout_s: float, joy_deadzone: float) -> None:
        self.idle_timeout_s = idle_timeout_s
        self.joy_deadzone = joy_deadzone
        self.last_active = float('-inf')

    def teleop(self, now: float, message: Twist) -> bool:
        """Record a velocity command; return whether it asks for motion."""
        moving = any((message.linear.x, message.linear.y, message.angular.z))
        if moving:
            self.last_active = now
        return moving

    def joy(self, now: float, axes) -> None:
        """Count a held stick; the vendor node publishes only on change."""
        if any(abs(axes[index]) >= self.joy_deadzone
               for index in DRIVE_AXES if index < len(axes)):
            self.last_active = now

    def started(self, now: float) -> None:
        """Give a newly started session the full quiet period."""
        self.last_active = max(self.last_active, now)

    def idle(self, now: float) -> bool:
        """Return whether no operator input arrived for the quiet period."""
        return now - self.last_active >= self.idle_timeout_s


class ManualControl(Node):
    """Keep one manual_drive request alive only while an operator drives."""

    def __init__(self) -> None:
        super().__init__('manual_control')
        idle_timeout_s = float(self.declare_parameter('idle_timeout_s', 5.0).value)
        self.retry_delay_s = float(self.declare_parameter('retry_delay_s', 2.0).value)
        self.capability_id = self.declare_parameter('capability_id', 'manual_drive').value
        if idle_timeout_s <= 0.0 or self.retry_delay_s <= 0.0:
            raise ValueError('idle_timeout_s and retry_delay_s must be positive')
        self.activity = ManualActivity(
            idle_timeout_s,
            float(self.declare_parameter('joy_deadzone', 0.1).value),
        )
        self.manual = False
        self.request_active = False
        self.last_request = float('-inf')
        self.last_preempt = float('-inf')
        self.client = ActionClient(self, ExecuteMission, EXECUTE_MISSION_ACTION)
        self.preempt = self.create_publisher(
            Empty, self.declare_parameter('preempt_topic', '/preempt_teleop').value, 1)
        state_qos = QoSProfile(depth=1)
        state_qos.reliability = ReliabilityPolicy.RELIABLE
        state_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.create_subscription(SystemState, '/malbut/state', self._state, state_qos)
        self.create_subscription(
            Twist, self.declare_parameter('teleop_topic', '/cmd_vel_teleop').value,
            self._teleop, 10)
        self.create_subscription(
            Joy, self.declare_parameter('joy_topic', '/ros_robot_controller/joy').value,
            lambda message: self.activity.joy(time.monotonic(), message.axes), 10)
        self.create_timer(0.2, self._check)

    def _state(self, message: SystemState) -> None:
        manual = message.control_mode == SystemState.MANUAL
        if manual and not self.manual:
            self.activity.started(time.monotonic())
        self.manual = manual

    def _teleop(self, message: Twist) -> None:
        now = time.monotonic()
        if not self.activity.teleop(now, message) or self.manual:
            return
        # Operator input takes the base; the manager cancels NORMAL motion.
        if (self.request_active or now - self.last_request < self.retry_delay_s
                or not self.client.server_is_ready()):
            return
        goal = ExecuteMission.Goal()
        goal.capability_id = self.capability_id
        goal.arguments_yaml = '{}'
        self.request_active = True
        self.last_request = now
        self.client.send_goal_async(goal).add_done_callback(self._accepted)

    def _accepted(self, future) -> None:
        try:
            handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self.get_logger().warning(f'manual_drive request failed: {error}')
            self.request_active = False
            return
        if not handle.accepted:
            self.request_active = False
            return
        handle.get_result_async().add_done_callback(self._finished)

    def _finished(self, future) -> None:
        self.request_active = False
        try:
            message = future.result().result.message
        except Exception:  # noqa: B902 - rclpy future boundary
            return
        if message:
            self.get_logger().info(f'manual_drive ended: {message}')

    def _check(self) -> None:
        now = time.monotonic()
        if (self.manual and self.activity.idle(now)
                and now - self.last_preempt >= self.activity.idle_timeout_s):
            # Ends AssistedTeleop for any requester; Nav2 stops the base.
            self.last_preempt = now
            self.preempt.publish(Empty())
            self.get_logger().info('No manual input; ending manual control')


def main(args=None) -> None:
    """Run the manual control watchdog."""
    rclpy.init(args=args)
    node = None
    try:
        node = ManualControl()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
