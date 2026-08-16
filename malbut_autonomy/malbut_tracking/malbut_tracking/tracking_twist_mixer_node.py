"""Combine Nav2 translation with independent target-facing body yaw."""

import json
import math
import sys

from geometry_msgs.msg import Twist
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String

from .twist_mixing import (
    HeadingControllerSettings,
    limit_angular_acceleration,
    target_angular_velocity,
)


class TrackingTwistMixerNode(Node):
    """Own the sole pre-safety Twist output used by the tracking demo."""

    def __init__(self) -> None:
        """Create command/status subscriptions and a fixed-rate output loop."""
        super().__init__('tracking_twist_mixer')
        self._declare_parameters()
        self._validate_parameters()
        self._settings = HeadingControllerSettings(
            proportional_gain=float(
                self.get_parameter('heading_proportional_gain').value
            ),
            rate_feedforward_gain=float(
                self.get_parameter('heading_rate_feedforward_gain').value
            ),
            deadband_rad=float(
                self.get_parameter('heading_deadband_rad').value
            ),
            maximum_speed_rps=float(
                self.get_parameter('maximum_angular_speed_rps').value
            ),
            maximum_acceleration_rps2=float(
                self.get_parameter('maximum_angular_acceleration_rps2').value
            ),
        )
        self._nav_command = Twist()
        self._last_nav_command_s: float | None = None
        self._last_status_s: float | None = None
        self._tracking_visible = False
        self._heading_error_rad = 0.0
        self._target_yaw_rate_rps = 0.0
        self._output_angular_rps = 0.0
        self._last_output_s = self._now_seconds()
        self._publisher = self.create_publisher(
            Twist,
            str(self.get_parameter('output_topic').value),
            10,
        )
        self._nav_subscription = self.create_subscription(
            Twist,
            str(self.get_parameter('nav_input_topic').value),
            self._on_nav_command,
            10,
        )
        self._status_subscription = self.create_subscription(
            String,
            str(self.get_parameter('tracking_status_topic').value),
            self._on_tracking_status,
            10,
        )
        frequency_hz = float(self.get_parameter('output_frequency_hz').value)
        self._timer = self.create_timer(1.0 / frequency_hz, self._publish)
        self.get_logger().info(
            'Tracking Twist mixer ready: Nav2 owns vx/vy; person bearing owns wz.'
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter(
            'nav_input_topic', '/cmd_vel'
        )
        self.declare_parameter(
            'tracking_status_topic', '/tracking/person/status'
        )
        self.declare_parameter('output_topic', '/cmd_vel_tracking_raw')
        self.declare_parameter('output_frequency_hz', 20.0)
        self.declare_parameter('nav_command_timeout_s', 0.25)
        self.declare_parameter('tracking_status_timeout_s', 0.30)
        self.declare_parameter('heading_proportional_gain', 1.25)
        self.declare_parameter('heading_rate_feedforward_gain', 0.15)
        self.declare_parameter('heading_deadband_rad', 0.04)
        self.declare_parameter('maximum_angular_speed_rps', 0.40)
        self.declare_parameter('maximum_angular_acceleration_rps2', 1.20)

    def _validate_parameters(self) -> None:
        for name in (
            'output_frequency_hz',
            'nav_command_timeout_s',
            'tracking_status_timeout_s',
            'heading_proportional_gain',
            'maximum_angular_speed_rps',
            'maximum_angular_acceleration_rps2',
        ):
            if float(self.get_parameter(name).value) <= 0.0:
                raise ValueError(f'{name} must be positive')
        if float(self.get_parameter('heading_deadband_rad').value) < 0.0:
            raise ValueError('heading_deadband_rad must be non-negative')

    def _on_nav_command(self, message: Twist) -> None:
        self._nav_command = message
        self._last_nav_command_s = self._now_seconds()

    def _on_tracking_status(self, message: String) -> None:
        try:
            status = json.loads(message.data)
            state = str(status.get('state', ''))
            visible = bool(status.get('target_visible', False))
            heading_error = float(status.get('heading_error_rad', 0.0))
            target_rate = float(status.get('target_yaw_rate_rps', 0.0))
        except (TypeError, ValueError, json.JSONDecodeError):
            self.get_logger().warning('Ignoring invalid tracking status JSON')
            return
        if not math.isfinite(heading_error) or not math.isfinite(target_rate):
            return
        self._tracking_visible = state == 'TRACKING' and visible
        self._heading_error_rad = heading_error
        self._target_yaw_rate_rps = target_rate
        self._last_status_s = self._now_seconds()

    def _publish(self) -> None:
        now_s = self._now_seconds()
        elapsed_s = max(0.0, now_s - self._last_output_s)
        self._last_output_s = now_s
        command = Twist()
        status_fresh = (
            self._last_status_s is not None
            and now_s - self._last_status_s
            <= float(self.get_parameter('tracking_status_timeout_s').value)
        )
        tracking_visible = status_fresh and self._tracking_visible
        nav_fresh = (
            self._last_nav_command_s is not None
            and now_s - self._last_nav_command_s
            <= float(self.get_parameter('nav_command_timeout_s').value)
        )
        if not nav_fresh:
            requested_angular = (
                target_angular_velocity(
                    self._heading_error_rad,
                    self._target_yaw_rate_rps,
                    self._settings,
                )
                if tracking_visible
                else 0.0
            )
            self._output_angular_rps = limit_angular_acceleration(
                self._output_angular_rps,
                requested_angular,
                elapsed_s,
                self._settings.maximum_acceleration_rps2,
            )
            command.angular.z = self._output_angular_rps
            self._publisher.publish(command)
            return
        command.linear = self._nav_command.linear
        if tracking_visible:
            requested_angular = target_angular_velocity(
                self._heading_error_rad,
                self._target_yaw_rate_rps,
                self._settings,
            )
        else:
            requested_angular = float(self._nav_command.angular.z)
        self._output_angular_rps = limit_angular_acceleration(
            self._output_angular_rps,
            requested_angular,
            elapsed_s,
            self._settings.maximum_acceleration_rps2,
        )
        command.angular.z = self._output_angular_rps
        self._publisher.publish(command)

    def _now_seconds(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9


def main(args=None) -> None:
    """Run the tracking Twist mixer until ROS shuts down."""
    rclpy.init(args=args)
    node = TrackingTwistMixerNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
