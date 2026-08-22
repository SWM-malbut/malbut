#!/usr/bin/env python3
"""Retire foreground clusters that stop moving into learned background."""

from __future__ import annotations

from dataclasses import dataclass
import math

from malbut_tracking.costmap_tracking import ObstacleCluster, Point2D


@dataclass
class _Settled:
    """One place a cluster has occupied without moving."""

    x: float
    y: float
    first_seen_s: float
    last_seen_s: float


class BackgroundMemory:
    """
    Forget objects that joined the room and then stayed put.

    The static distance field is built once from the saved map, so anything
    the owner moved in afterwards - a chair, a drying rack, an opened door -
    stays a foreground cluster forever. A follower that acquires targets from
    LiDAR alone would keep proposing those, so retire a cluster once it has
    held one spot long enough, and release it as soon as it moves again.
    """

    def __init__(
        self,
        *,
        settle_seconds: float = 8.0,
        settle_radius_m: float = 0.25,
        forget_seconds: float = 20.0,
    ) -> None:
        """Configure how long stillness must last before it counts."""
        if settle_seconds <= 0.0:
            raise ValueError('settle seconds must be positive')
        if settle_radius_m <= 0.0:
            raise ValueError('settle radius must be positive')
        if forget_seconds <= 0.0:
            raise ValueError('forget seconds must be positive')
        self.settle_seconds = float(settle_seconds)
        self.settle_radius_m = float(settle_radius_m)
        self.forget_seconds = float(forget_seconds)
        self._places: list[_Settled] = []

    def observe(
        self, clusters: list[ObstacleCluster], now_s: float
    ) -> None:
        """Record where clusters were seen at one scan time."""
        for cluster in clusters:
            place = self._match(cluster.position)
            if place is None:
                self._places.append(_Settled(
                    x=float(cluster.position.x),
                    y=float(cluster.position.y),
                    first_seen_s=float(now_s),
                    last_seen_s=float(now_s),
                ))
                continue
            place.last_seen_s = float(now_s)
        self._expire(now_s)

    def is_background(self, position: Point2D, now_s: float) -> bool:
        """Report whether one measurement sits on learned background."""
        place = self._match(position)
        if place is None:
            return False
        return now_s - place.first_seen_s >= self.settle_seconds

    def filter_moving(
        self, clusters: list[ObstacleCluster], now_s: float
    ) -> list[ObstacleCluster]:
        """Return only the clusters that are not learned background."""
        return [
            cluster for cluster in clusters
            if not self.is_background(cluster.position, now_s)
        ]

    def _match(self, position: Point2D) -> _Settled | None:
        for place in self._places:
            distance = math.hypot(
                float(position.x) - place.x, float(position.y) - place.y
            )
            if distance <= self.settle_radius_m:
                return place
        return None

    def _expire(self, now_s: float) -> None:
        # 사라진 자리는 잊는다. 치워진 의자가 영원히 배경으로 남으면 그
        # 자리를 지나는 사람까지 무시하게 된다.
        self._places = [
            place for place in self._places
            if now_s - place.last_seen_s <= self.forget_seconds
        ]


def select_acquisition_turn(
    candidates: list[ObstacleCluster],
    robot: Point2D,
    robot_yaw_rad: float,
    *,
    camera_half_fov_rad: float,
    maximum_distance_m: float,
    minimum_extent_m: float = 0.15,
    maximum_extent_m: float = 1.20,
) -> float | None:
    """
    Return the turn that would bring one LiDAR candidate into the camera.

    The camera owns confirmation, so nothing here decides that a cluster is
    a person. It only decides where the camera should look next: the
    nearest person-sized candidate that the camera cannot already see.
    Candidates already inside the field of view need no turn, because the
    detector has had its chance at them.
    """
    if maximum_distance_m <= 0.0:
        raise ValueError('maximum distance must be positive')
    if camera_half_fov_rad <= 0.0:
        raise ValueError('camera half field of view must be positive')
    best_turn: float | None = None
    best_distance = math.inf
    for cluster in candidates:
        if not minimum_extent_m <= cluster.extent_m <= maximum_extent_m:
            continue
        offset_x = float(cluster.position.x) - float(robot.x)
        offset_y = float(cluster.position.y) - float(robot.y)
        distance = math.hypot(offset_x, offset_y)
        if distance > maximum_distance_m or distance <= 0.0:
            continue
        bearing = math.atan2(offset_y, offset_x)
        turn = math.atan2(
            math.sin(bearing - robot_yaw_rad),
            math.cos(bearing - robot_yaw_rad),
        )
        if abs(turn) <= camera_half_fov_rad:
            continue
        if distance < best_distance:
            best_distance = distance
            best_turn = turn
    return best_turn
