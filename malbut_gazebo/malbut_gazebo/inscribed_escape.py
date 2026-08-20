#!/usr/bin/env python3
"""Back the robot out of a costmap cell it can no longer plan from."""

from __future__ import annotations

import math

from geometry_msgs.msg import Twist
from nav2_msgs.msg import Costmap
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from sensor_msgs.msg import LaserScan
import tf2_ros


# Nav2 marks every cell within the robot's inscribed radius of an obstacle
# with this cost, and the planners refuse to start from it.
INSCRIBED_COST = 253
COSTMAP_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)


def cost_at(costmap: Costmap, x: float, y: float) -> int | None:
    """Return the cost under one world point, or None when off the grid."""
    metadata = costmap.metadata
    resolution = metadata.resolution
    if resolution <= 0.0:
        return None
    column = int((x - metadata.origin.position.x) / resolution)
    row = int((y - metadata.origin.position.y) / resolution)
    if not (0 <= column < metadata.size_x and 0 <= row < metadata.size_y):
        return None
    return int(costmap.data[row * metadata.size_x + column])


def nearest_obstacle_angle(
    ranges: list, angle_min: float, angle_increment: float
) -> tuple[float, float] | None:
    """Return the bearing and range of the closest valid laser return."""
    best_range = math.inf
    best_angle = 0.0
    for index, value in enumerate(ranges):
        if value is None or not math.isfinite(value) or value <= 0.0:
            continue
        if value < best_range:
            best_range = value
            best_angle = angle_min + index * angle_increment
    if not math.isfinite(best_range):
        return None
    return best_angle, best_range


def clearance_towards(
    ranges: list,
    angle_min: float,
    angle_increment: float,
    heading: float,
    half_width: float,
) -> float:
    """Return the smallest laser range inside one heading sector."""
    smallest = math.inf
    for index, value in enumerate(ranges):
        if value is None or not math.isfinite(value) or value <= 0.0:
            continue
        angle = angle_min + index * angle_increment
        difference = math.atan2(
            math.sin(angle - heading), math.cos(angle - heading)
        )
        if abs(difference) <= half_width:
            smallest = min(smallest, value)
    return smallest


def plan_escape(
    cost: int | None,
    obstacle_angle: float | None,
    forward_clearance: float,
    backward_clearance: float,
    *,
    speed: float,
    required_clearance: float,
) -> float:
    """
    Return the linear velocity that leaves an inscribed cell, or zero.

    The collision monitor projects the commanded footprint and slows it to
    a stop whenever that projection overlaps an obstacle. A robot already
    parked inside the inflation therefore has every direction refused,
    including the one that would free it, so Nav2's backup recovery runs
    forever without moving. Only drive away from the closest obstacle, and
    only when that side is measurably open.
    """
    if cost is None or cost < INSCRIBED_COST:
        return 0.0
    if obstacle_angle is None:
        return 0.0
    # 가장 가까운 장애물이 앞쪽이면 뒤로, 뒤쪽이면 앞으로 뺀다.
    away_is_backward = math.cos(obstacle_angle) >= 0.0
    clearance = backward_clearance if away_is_backward else forward_clearance
    if clearance < required_clearance:
        return 0.0
    return -speed if away_is_backward else speed


class InscribedEscape(Node):
    """Free a robot the collision monitor and planner have both given up on."""

    def __init__(self):
        """Watch the local costmap and drive out of inscribed cells."""
        super().__init__("inscribed_escape")
        self.speed = float(
            self.declare_parameter("escape_speed_mps", 0.10).value
        )
        self.required_clearance = float(
            self.declare_parameter("required_clearance_m", 0.35).value
        )
        self.stuck_seconds = float(
            self.declare_parameter("stuck_seconds", 6.0).value
        )
        self.maximum_escape_seconds = float(
            self.declare_parameter("maximum_escape_seconds", 3.0).value
        )
        self.sector_half_width = float(
            self.declare_parameter("sector_half_width_rad", 0.52).value
        )
        self.map_frame = str(
            self.declare_parameter("map_frame", "map").value
        )
        self.base_frame = str(
            self.declare_parameter("base_frame", "base_footprint").value
        )
        self.costmap: Costmap | None = None
        self.scan: LaserScan | None = None
        self.inscribed_since: float | None = None
        self.escape_until: float | None = None
        self.buffer = tf2_ros.Buffer(node=self)
        self.listener = tf2_ros.TransformListener(
            self.buffer, self, spin_thread=False
        )
        self.publisher = self.create_publisher(Twist, "cmd_vel", 1)
        # 계획을 거부하는 쪽은 전역 코스트맵이다. 로컬은 정적 레이어의
        # 팽창을 같은 값으로 담지 않아 갇힌 셀이 253 미만으로 보인다.
        self.create_subscription(
            Costmap,
            str(self.declare_parameter(
                "costmap_topic", "global_costmap/costmap_raw"
            ).value),
            self._receive_costmap,
            COSTMAP_QOS,
        )
        self.create_subscription(LaserScan, "scan", self._receive_scan, 1)
        self.create_timer(0.2, self._step)

    def _receive_costmap(self, message: Costmap) -> None:
        self.costmap = message

    def _receive_scan(self, message: LaserScan) -> None:
        self.scan = message

    def _now(self) -> float:
        return self.get_clock().now().nanoseconds / 1e9

    def _robot_xy(self) -> tuple[float, float] | None:
        try:
            transform = self.buffer.lookup_transform(
                self.map_frame, self.base_frame, rclpy.time.Time()
            )
        except tf2_ros.TransformException:
            return None
        return (
            transform.transform.translation.x,
            transform.transform.translation.y,
        )

    def _step(self) -> None:
        now = self._now()
        if self.escape_until is not None:
            if now >= self.escape_until:
                self.publisher.publish(Twist())
                self.escape_until = None
                self.inscribed_since = None
            return
        if self.costmap is None or self.scan is None:
            return
        position = self._robot_xy()
        if position is None:
            return
        cost = cost_at(self.costmap, position[0], position[1])
        if cost is None or cost < INSCRIBED_COST:
            self.inscribed_since = None
            return
        if self.inscribed_since is None:
            self.inscribed_since = now
            return
        # 잠깐 스치는 경우까지 개입하지 않는다. Nav2 자체 복구가 통하는
        # 상황이라면 그동안 빠져나온다.
        if now - self.inscribed_since < self.stuck_seconds:
            return
        obstacle = nearest_obstacle_angle(
            list(self.scan.ranges),
            self.scan.angle_min,
            self.scan.angle_increment,
        )
        forward = clearance_towards(
            list(self.scan.ranges), self.scan.angle_min,
            self.scan.angle_increment, 0.0, self.sector_half_width,
        )
        backward = clearance_towards(
            list(self.scan.ranges), self.scan.angle_min,
            self.scan.angle_increment, math.pi, self.sector_half_width,
        )
        velocity = plan_escape(
            cost,
            obstacle[0] if obstacle is not None else None,
            forward,
            backward,
            speed=self.speed,
            required_clearance=self.required_clearance,
        )
        if velocity == 0.0:
            return
        command = Twist()
        command.linear.x = velocity
        self.publisher.publish(command)
        self.escape_until = now + self.maximum_escape_seconds
        self.get_logger().warning(
            "robot is inside an inscribed cell that no plan can start from; "
            f"driving {velocity:.2f} m/s to leave it"
        )


def main():
    """Run the inscribed-cell escape helper."""
    rclpy.init()
    node = InscribedEscape()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
