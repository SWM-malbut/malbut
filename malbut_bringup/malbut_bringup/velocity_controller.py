"""Stop the vendor chassis when velocity commands expire."""

import math
import time

from controller.odom_publisher_node import Controller
from geometry_msgs.msg import Twist
from rcl_interfaces.msg import ParameterDescriptor
import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.executors import ExternalShutdownException


def _finite_velocity(message):
    return all(math.isfinite(value) for value in (
        message.linear.x, message.linear.y, message.angular.z))


class VelocityController(Controller):
    """Retain vendor kinematics and expire commands on a steady clock."""

    def __init__(self):
        # The vendor constructor initializes rclpy and binds our callbacks.
        super().__init__('odom_publisher')
        self.cmd_vel_timeout = float(self.declare_parameter(
            'cmd_vel_timeout', 0.5, ParameterDescriptor(read_only=True)).value)
        if not math.isfinite(self.cmd_vel_timeout) or self.cmd_vel_timeout <= 0:
            raise ValueError('cmd_vel_timeout must be positive and finite')
        self.last_cmd_vel = None
        # Use the same default mutually exclusive group as the subscriptions.
        self.create_timer(0.02, self._expire_velocity,
                          clock=Clock(clock_type=ClockType.STEADY_TIME))

    def cmd_vel_callback(self, message):
        """Forward valid commands and remember successful motion inputs."""
        if not _finite_velocity(message):
            message = Twist()
            self.get_logger().warning('Non-finite velocity command received; stopping chassis')
        super().cmd_vel_callback(message)
        moving = any((message.linear.x, message.linear.y, message.angular.z))
        self.last_cmd_vel = time.monotonic() if moving else None

    def app_cmd_vel_callback(self, message):
        """Preserve vendor app limits after rejecting non-finite inputs."""
        if not _finite_velocity(message):
            self.cmd_vel_callback(message)
            return
        super().app_cmd_vel_callback(message)

    def acker_cmd_vel_callback(self, message):
        """Validate before the vendor's steering arithmetic and servo output."""
        if not _finite_velocity(message):
            self.cmd_vel_callback(message)
            return
        super().acker_cmd_vel_callback(message)

    def _expire_velocity(self):
        if self.last_cmd_vel is None:
            return
        if time.monotonic() - self.last_cmd_vel < self.cmd_vel_timeout:
            return
        super().cmd_vel_callback(Twist())
        self.last_cmd_vel = None
        self.get_logger().warning('Velocity command timed out; stopping chassis')

    def shutdown(self, signum, frame):
        """Send zero before the vendor closes the ROS context."""
        try:
            self.cmd_vel_callback(Twist())
        finally:
            super().shutdown(signum, frame)


def main():
    """Run the replacement vendor controller with the default single executor."""
    node = VelocityController()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        try:
            if rclpy.ok():
                node.cmd_vel_callback(Twist())
        finally:
            node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()
