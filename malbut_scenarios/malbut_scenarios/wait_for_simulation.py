"""Wait until the simulated robot and its primary sensors are publishing."""

from __future__ import annotations

import time

from nav_msgs.msg import Odometry
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image, LaserScan


class SimulationReadiness(Node):
    """Observe simulation topics without relying on ROS CLI discovery."""

    def __init__(self) -> None:
        super().__init__('scenario_simulation_readiness')
        self.declare_parameter('timeout_s', 90.0)
        self.timeout_s = float(self.get_parameter('timeout_s').value)
        if self.timeout_s <= 0.0:
            raise ValueError('timeout_s must be positive')

        self.received = {
            '/odom': False,
            '/scan': False,
            '/camera/color/image_raw': False,
        }
        self.create_subscription(
            Odometry,
            '/odom',
            lambda _message: self._mark_ready('/odom'),
            qos_profile_sensor_data,
        )
        self.create_subscription(
            LaserScan,
            '/scan',
            lambda _message: self._mark_ready('/scan'),
            qos_profile_sensor_data,
        )
        self.create_subscription(
            Image,
            '/camera/color/image_raw',
            lambda _message: self._mark_ready('/camera/color/image_raw'),
            qos_profile_sensor_data,
        )

    @property
    def ready(self) -> bool:
        """Return whether every required simulation stream was observed."""
        return all(self.received.values())

    def _mark_ready(self, topic: str) -> None:
        if not self.received[topic]:
            self.received[topic] = True
            self.get_logger().info(f'received required topic {topic}')


def main(args=None) -> int:
    """Return success only after robot motion and sensor topics are ready."""
    rclpy.init(args=args)
    node = SimulationReadiness()
    deadline = time.monotonic() + node.timeout_s
    try:
        while rclpy.ok() and not node.ready and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.2)
        if node.ready:
            node.get_logger().info('simulation is ready')
            return 0
        missing = [
            topic for topic, received in node.received.items() if not received
        ]
        node.get_logger().error(
            'simulation readiness timed out; missing: ' + ', '.join(missing)
        )
        return 1
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
