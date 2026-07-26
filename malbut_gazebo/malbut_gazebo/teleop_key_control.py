#!/usr/bin/env python3
"""Small keyboard teleop for the ROSOrin Mecanum command interface."""

import select
import sys
import termios
import tty

from geometry_msgs.msg import Twist
import rclpy
from rclpy.node import Node


HELP = """
Malbut Mecanum teleop
---------------------
  w / s : forward / backward
  a / d : strafe left / right
  q / e : rotate left / right
  space : stop
  Ctrl-C: quit
"""


def _read_key(settings, timeout=0.1):
    tty.setraw(sys.stdin.fileno())
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if ready else ''
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)


class TeleopControl(Node):
    """Publish geometry_msgs/Twist commands on the canonical /cmd_vel topic."""

    def __init__(self):
        super().__init__('malbut_mecanum_teleop')
        self.declare_parameter('linear_speed', 0.3)
        self.declare_parameter('angular_speed', 0.8)
        self.declare_parameter('cmd_vel_topic', '/cmd_vel')
        topic = self.get_parameter('cmd_vel_topic').value
        self.linear_speed = float(
            self.get_parameter('linear_speed').value
        )
        self.angular_speed = float(
            self.get_parameter('angular_speed').value
        )
        self.publisher = self.create_publisher(Twist, topic, 10)

    def command_for(self, key):
        """Return the Mecanum command associated with one keyboard key."""
        command = Twist()
        if key == 'w':
            command.linear.x = self.linear_speed
        elif key == 's':
            command.linear.x = -self.linear_speed
        elif key == 'a':
            command.linear.y = self.linear_speed
        elif key == 'd':
            command.linear.y = -self.linear_speed
        elif key == 'q':
            command.angular.z = self.angular_speed
        elif key == 'e':
            command.angular.z = -self.angular_speed
        return command


def main(args=None):
    if not sys.stdin.isatty():
        raise RuntimeError('teleop_key_control requires an interactive terminal')

    rclpy.init(args=args)
    node = TeleopControl()
    settings = termios.tcgetattr(sys.stdin)
    try:
        print(HELP)
        while rclpy.ok():
            key = _read_key(settings)
            if key == '\x03':
                break
            node.publisher.publish(node.command_for(key))
            rclpy.spin_once(node, timeout_sec=0.0)
    finally:
        node.publisher.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)


if __name__ == '__main__':
    main()
