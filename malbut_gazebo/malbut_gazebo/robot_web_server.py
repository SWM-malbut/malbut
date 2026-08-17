#!/usr/bin/env python3
"""Expose bounded Nav2 navigation and live robot state to the map UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import math
from pathlib import Path
import secrets
import sys
from threading import BoundedSemaphore, Event, Lock, Thread
import time
from urllib.parse import urlparse

import cv2
import numpy as np
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.utilities import remove_ros_args
import tf2_ros

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import ComputePathToPose, NavigateToPose
from nav2_msgs.srv import GetCostmap
from nav_msgs.msg import Path as NavPath

from malbut_gazebo.user_map_editor import (
    EditorRequestHandler,
    RequestError,
    configure_editor_handler,
    editor_asset_root,
    parse_editor_arguments,
)
from malbut_gazebo.user_map_builder import _free_mask, load_slam_map


SEND_GOAL_TIMEOUT_S = 5.0
PLAN_RESULT_TIMEOUT_S = 8.0
SERVICE_TIMEOUT_S = 5.0
CANCEL_TIMEOUT_S = 2.0
PREVIEW_TTL_S = 30.0
MAX_SSE_CLIENTS = 4
ROBOT_RADIUS_M = 0.238
PATH_CLEARANCE_MARGIN_M = 0.05
REQUIRED_PATH_CLEARANCE_M = ROBOT_RADIUS_M + PATH_CLEARANCE_MARGIN_M
MAX_SNAP_DISTANCE_M = 0.5
MAX_GOAL_GAP_M = 0.05
MIN_NAVIGATION_TIMEOUT_S = 60.0
MAX_NAVIGATION_TIMEOUT_S = 300.0
NAVIGATION_TIMEOUT_BASE_S = 45.0
NAVIGATION_TIMEOUT_PER_M_S = 15.0
NAVIGATION_STALL_TIMEOUT_S = 45.0
NAVIGATION_PROGRESS_M = 0.10


class NavigationError(RuntimeError):
    """Represent one safe, client-facing navigation rejection."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        **details: object,
    ) -> None:
        """Store HTTP status, stable code, and optional structured details."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    def response(self) -> dict:
        """Return the JSON response body."""
        return {
            "error_code": self.code,
            "message": str(self),
            **self.details,
        }


@dataclass
class CostmapGrid:
    """Store the current Nav2 master costmap in map coordinates."""

    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    costs: np.ndarray

    def cell(self, x: float, y: float) -> tuple[int, int] | None:
        """Convert map coordinates to a valid costmap cell."""
        column = math.floor((x - self.origin_x) / self.resolution)
        row = math.floor((y - self.origin_y) / self.resolution)
        if not (0 <= column < self.width and 0 <= row < self.height):
            return None
        return row, column

    def point(self, row: int, column: int) -> tuple[float, float]:
        """Return the center of a costmap cell in map coordinates."""
        return (
            self.origin_x + (column + 0.5) * self.resolution,
            self.origin_y + (row + 0.5) * self.resolution,
        )


@dataclass
class PreviewRecord:
    """Bind a validated path to one browser session for a short time."""

    session_id: str
    expires_at: float
    map_revision: str
    zone_revision: str
    goal: PoseStamped
    path: object
    response: dict
    used: bool = False


@dataclass
class NavigationWatchdog:
    """Bound one navigation by elapsed time and meaningful progress."""

    started_at: float
    deadline: float
    last_progress_at: float
    checkpoint_distance_m: float | None

    @classmethod
    def create(
        cls,
        path_length_m: float,
        now: float | None = None,
    ) -> NavigationWatchdog:
        """Create a watchdog using a conservative path-length time budget."""
        current = time.monotonic() if now is None else now
        duration = max(
            MIN_NAVIGATION_TIMEOUT_S,
            min(
                MAX_NAVIGATION_TIMEOUT_S,
                NAVIGATION_TIMEOUT_BASE_S
                + max(0.0, path_length_m) * NAVIGATION_TIMEOUT_PER_M_S,
            ),
        )
        return cls(current, current + duration, current, path_length_m)

    def observe(self, distance_m: float, now: float | None = None) -> None:
        """Record progress only after the route distance meaningfully falls."""
        if not math.isfinite(distance_m) or distance_m <= MAX_GOAL_GAP_M:
            return
        current = time.monotonic() if now is None else now
        if (
            self.checkpoint_distance_m is None
            or distance_m
            <= self.checkpoint_distance_m - NAVIGATION_PROGRESS_M
        ):
            self.checkpoint_distance_m = distance_m
            self.last_progress_at = current

    def failure(self, now: float | None = None) -> tuple[str, str] | None:
        """Return the automatic failure reason when one bound is exceeded."""
        current = time.monotonic() if now is None else now
        if current >= self.deadline:
            return (
                "NAVIGATION_TIMEOUT",
                "최대 주행 시간을 초과해 자동으로 중지했습니다.",
            )
        if current - self.last_progress_at >= NAVIGATION_STALL_TIMEOUT_S:
            return (
                "NAVIGATION_STALLED",
                "목적지 방향으로 진행하지 못해 자동으로 중지했습니다.",
            )
        return None


def _point_in_ring(point: tuple[float, float], ring: list) -> bool:
    """Return whether a point is inside one GeoJSON linear ring."""
    x, y = point
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing:
                inside = not inside
        previous = current
    return inside


def _point_in_geometry(point: tuple[float, float], geometry: dict) -> bool:
    """Return whether a point is inside a Polygon or MultiPolygon."""
    geometry_type = geometry.get("type")
    polygons = (
        [geometry.get("coordinates", [])]
        if geometry_type == "Polygon"
        else geometry.get("coordinates", [])
        if geometry_type == "MultiPolygon"
        else []
    )
    for polygon in polygons:
        if not polygon or not _point_in_ring(point, polygon[0]):
            continue
        if any(_point_in_ring(point, hole) for hole in polygon[1:]):
            continue
        return True
    return False


def _quaternion_yaw(quaternion: object) -> float:
    """Convert a geometry quaternion to planar yaw."""
    return math.atan2(
        2.0 * (quaternion.w * quaternion.z + quaternion.x * quaternion.y),
        1.0 - 2.0 * (quaternion.y ** 2 + quaternion.z ** 2),
    )


def _set_pose_yaw(pose: PoseStamped, yaw: float) -> None:
    """Set a normalized planar quaternion on a PoseStamped."""
    pose.pose.orientation.x = 0.0
    pose.pose.orientation.y = 0.0
    pose.pose.orientation.z = math.sin(yaw / 2.0)
    pose.pose.orientation.w = math.cos(yaw / 2.0)


def _path_length(path: object) -> float:
    """Return the planar length of a nav_msgs/Path-like object."""
    total = 0.0
    for first, second in zip(path.poses, path.poses[1:]):
        total += math.hypot(
            second.pose.position.x - first.pose.position.x,
            second.pose.position.y - first.pose.position.y,
        )
    return total


def _path_max_cost(path: object, grid: CostmapGrid) -> int:
    """Return the greatest cost touched by a path, including between poses."""
    cells = _path_cells(path, grid)
    if cells is None:
        return 255
    return max(int(grid.costs[cell]) for cell in cells)


def _path_cells(
    path: object,
    grid: CostmapGrid,
) -> list[tuple[int, int]] | None:
    """Sample every costmap cell touched by a path; return None off-map."""
    if not path.poses:
        return None
    result: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def append_cell(x: float, y: float) -> bool:
        cell = grid.cell(x, y)
        if cell is None:
            return False
        if cell not in seen:
            result.append(cell)
            seen.add(cell)
        return True

    if len(path.poses) == 1:
        point = path.poses[0].pose.position
        return result if append_cell(point.x, point.y) else None

    pairs = zip(path.poses, path.poses[1:])
    for first, second in pairs:
        x1 = first.pose.position.x
        y1 = first.pose.position.y
        x2 = second.pose.position.x
        y2 = second.pose.position.y
        distance = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, math.ceil(distance / (grid.resolution * 0.5)))
        for index in range(steps + 1):
            ratio = index / steps
            if not append_cell(
                x1 + (x2 - x1) * ratio,
                y1 + (y2 - y1) * ratio,
            ):
                return None
    return result


def _slam_clearance_grid(slam_map_path: Path) -> CostmapGrid:
    """Build physical wall clearance directly from saved SLAM occupancy."""
    slam_map = load_slam_map(slam_map_path)
    transform = slam_map.transform
    if abs(transform.origin_yaw) > 1e-6:
        raise ValueError("rotated SLAM map origins are not supported")
    free = np.flipud((_free_mask(slam_map) > 0).astype(np.uint8))
    clearance = cv2.distanceTransform(
        free, cv2.DIST_L2, 5
    ) * transform.resolution
    height, width = clearance.shape
    return CostmapGrid(
        width,
        height,
        transform.resolution,
        transform.origin_x,
        transform.origin_y,
        clearance,
    )


def _path_min_clearance(
    path: object,
    grid: CostmapGrid,
    clearance: np.ndarray,
) -> float:
    """Return minimum obstacle clearance along the complete sampled path."""
    cells = _path_cells(path, grid)
    if cells is None:
        return 0.0
    return min(float(clearance[cell]) for cell in cells)


def _decimate_path(path: object, spacing: float = 0.1) -> list[list[float]]:
    """Reduce a path to UI points while preserving both endpoints."""
    if not path.poses:
        return []
    result = [[path.poses[0].pose.position.x, path.poses[0].pose.position.y]]
    last = result[0]
    for pose in path.poses[1:-1]:
        point = [pose.pose.position.x, pose.pose.position.y]
        if math.hypot(point[0] - last[0], point[1] - last[1]) >= spacing:
            result.append(point)
            last = point
    final = [path.poses[-1].pose.position.x, path.poses[-1].pose.position.y]
    if final != result[-1]:
        result.append(final)
    return [[round(x, 4), round(y, 4)] for x, y in result]


class RobotWebBridge(Node):
    """Own TF, lifecycle, costmap, and Nav2 action state."""

    def __init__(
        self,
        map_path: Path,
        map_id: str,
        map_revision: str,
        zone_path: Path | None,
        slam_map_path: Path,
    ) -> None:
        """Create all ROS clients and periodically refresh robot state."""
        super().__init__(
            "robot_web_bridge",
            automatically_declare_parameters_from_overrides=True,
        )
        self.map_path = map_path
        self.map_id = map_id
        self.map_revision = map_revision
        self.zone_path = zone_path
        self.static_clearance = _slam_clearance_grid(slam_map_path)
        self.tf_buffer = tf2_ros.Buffer(node=self)
        self.tf_listener = tf2_ros.TransformListener(
            self.tf_buffer, self, spin_thread=False
        )
        self.compute_path = ActionClient(
            self, ComputePathToPose, "/compute_path_to_pose"
        )
        if not self.has_parameter("navigation_action_name"):
            self.declare_parameter(
                "navigation_action_name", "/navigate_to_pose"
            )
        self.navigate = ActionClient(
            self,
            NavigateToPose,
            str(self.get_parameter("navigation_action_name").value),
        )
        self.get_costmap = self.create_client(
            GetCostmap, "/global_costmap/get_costmap"
        )
        self.create_subscription(
            NavPath, "/plan", self._receive_navigation_path, 10
        )
        lifecycle_names = {
            "amcl": "/amcl/get_state",
            "bt_navigator": "/bt_navigator/get_state",
            "planner_server": "/planner_server/get_state",
            "controller_server": "/controller_server/get_state",
            "global_costmap": "/global_costmap/global_costmap/get_state",
        }
        self.lifecycle_clients = {
            name: self.create_client(GetState, service)
            for name, service in lifecycle_names.items()
        }
        self.lifecycle_futures: dict[str, object] = {}
        self.lock = Lock()
        self.operation_lock = Lock()
        self.seq = 0
        self.pose: dict | None = None
        self.localization = {
            "state": "uninitialized",
            "tf_age_s": None,
            "message": "초기 위치를 기다리는 중입니다.",
        }
        self.lifecycle = {name: "unknown" for name in lifecycle_names}
        self.navigation_state = self._idle_navigation()
        self.navigation_goal_handle = None
        self.path_revision = 0
        self.auto_cancel_pending = False
        self.auto_cancel_code: str | None = None
        self.auto_cancel_message: str | None = None
        self.navigation_watchdog: NavigationWatchdog | None = None
        self.previews: dict[str, PreviewRecord] = {}
        self.create_timer(0.1, self._refresh_pose)
        self.create_timer(1.0, self._refresh_lifecycle)
        self.create_timer(1.0, self._monitor_navigation)

    @staticmethod
    def _idle_navigation() -> dict:
        return {
            "session_id": None,
            "state": "idle",
            "goal": None,
            "distance_remaining_m": None,
            "estimated_time_remaining_s": None,
            "navigation_time_s": 0.0,
            "number_of_recoveries": 0,
            "progress_ratio": None,
            "message": None,
            "message_code": None,
            "path": None,
            "path_revision": 0,
            "path_source": None,
        }

    def _receive_navigation_path(self, path: NavPath) -> None:
        """Publish Nav2's latest global-costmap route to web clients."""
        if not path.poses:
            return
        payload = {
            "length_m": round(_path_length(path), 3),
            "points": _decimate_path(path),
        }
        with self.lock:
            if self.navigation_state["state"] != "driving":
                return
            self.path_revision += 1
            self.navigation_state.update({
                "path": payload,
                "path_revision": self.path_revision,
                "path_source": "live_global_costmap",
                "path_length_m": payload["length_m"],
            })

    def _refresh_pose(self) -> None:
        try:
            transform = self.tf_buffer.lookup_transform(
                "map", "base_footprint", rclpy.time.Time()
            )
            now_ns = self.get_clock().now().nanoseconds
            stamp_ns = (
                transform.header.stamp.sec * 1_000_000_000
                + transform.header.stamp.nanosec
            )
            age = max(0.0, (now_ns - stamp_ns) / 1_000_000_000.0)
            yaw = _quaternion_yaw(transform.transform.rotation)
            pose = {
                "x": round(transform.transform.translation.x, 4),
                "y": round(transform.transform.translation.y, 4),
                "yaw": round(yaw, 5),
                "stamp": round(stamp_ns / 1_000_000_000.0, 6),
                "age_s": round(age, 3),
            }
            amcl_active = self.lifecycle.get("amcl") == "active"
            if age > 2.0 or self.lifecycle.get("amcl") not in {
                "active", "unknown"
            }:
                state = "lost"
                message = "로봇 위치를 확인할 수 없습니다."
            elif age > 0.5 or not amcl_active:
                state = "degraded"
                message = "로봇 위치 신뢰도가 낮습니다."
            else:
                state = "ok"
                message = None
            with self.lock:
                self.pose = pose
                self.localization = {
                    "state": state,
                    "tf_age_s": round(age, 3),
                    "message": message,
                }
            if state == "lost":
                self._request_auto_cancel(
                    "LOCALIZATION_LOST",
                    "로봇 위치가 끊겨 주행을 자동 취소합니다.",
                )
        except Exception as error:  # tf2 exception types vary by distro
            with self.lock:
                state = "lost" if self.pose is not None else "uninitialized"
                self.localization = {
                    "state": state,
                    "tf_age_s": None,
                    "message": (
                        "로봇 위치가 끊겼습니다."
                        if state == "lost"
                        else "초기 위치를 기다리는 중입니다."
                    ),
                    "detail": str(error),
                }
            if state == "lost":
                self._request_auto_cancel(
                    "LOCALIZATION_LOST",
                    "로봇 위치가 끊겨 주행을 자동 취소합니다.",
                )

    def _request_auto_cancel(self, code: str, message: str) -> None:
        """Cancel an active goal without blocking the ROS executor timer."""
        with self.lock:
            if (
                self.auto_cancel_pending
                or self.navigation_state["state"] != "driving"
                or self.navigation_goal_handle is None
            ):
                return
            self.auto_cancel_pending = True
            self.auto_cancel_code = code
            self.auto_cancel_message = message
            self.navigation_state.update({
                "state": "canceling",
                "message": message,
                "message_code": code,
            })
        Thread(target=self._auto_cancel_worker, daemon=True).start()

    def _auto_cancel_worker(self) -> None:
        """Wait a bounded time for an automatic navigation cancellation."""
        try:
            with self.lock:
                goal_handle = self.navigation_goal_handle
            if goal_handle is not None:
                self._wait(
                    goal_handle.cancel_goal_async(),
                    CANCEL_TIMEOUT_S,
                    "주행 자동 취소",
                )
        except NavigationError as error:
            with self.lock:
                self.navigation_state.update({
                    "state": "failed",
                    "message": str(error),
                    "message_code": error.code,
                })
        finally:
            with self.lock:
                self.auto_cancel_pending = False

    def _monitor_navigation(self) -> None:
        """Cancel a navigation that exceeds its time or progress bounds."""
        with self.lock:
            if (
                self.navigation_state["state"] != "driving"
                or self.navigation_watchdog is None
            ):
                return
            failure = self.navigation_watchdog.failure()
        if failure is not None:
            self._request_auto_cancel(*failure)

    def _refresh_lifecycle(self) -> None:
        for name, client in self.lifecycle_clients.items():
            pending = self.lifecycle_futures.get(name)
            if pending is not None and not pending.done():
                continue
            if not client.service_is_ready():
                with self.lock:
                    self.lifecycle[name] = "unavailable"
                continue
            future = client.call_async(GetState.Request())
            self.lifecycle_futures[name] = future
            future.add_done_callback(
                lambda completed, node_name=name: self._lifecycle_result(
                    node_name, completed
                )
            )

    def _lifecycle_result(self, name: str, future: object) -> None:
        try:
            label = future.result().current_state.label or "unknown"
        except Exception:
            label = "unavailable"
        with self.lock:
            self.lifecycle[name] = label

    def snapshot(self) -> dict:
        """Return the latest immutable browser state."""
        with self.lock:
            self.seq += 1
            return {
                "seq": self.seq,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "map_id": self.map_id,
                "map_revision": self.map_revision,
                "pose": dict(self.pose) if self.pose else None,
                "localization": dict(self.localization),
                "nav2": dict(self.lifecycle),
                "navigation": dict(self.navigation_state),
            }

    def _wait(self, future: object, timeout: float, operation: str) -> object:
        event = Event()
        future.add_done_callback(lambda _future: event.set())
        if not event.wait(timeout):
            raise NavigationError(
                504, "NAV2_TIMEOUT", f"{operation} 응답 시간이 초과됐습니다."
            )
        try:
            return future.result()
        except Exception as error:
            raise NavigationError(
                502, "NAV2_CALL_FAILED", f"{operation} 호출에 실패했습니다."
            ) from error

    def _require_ready(self) -> dict:
        with self.lock:
            lifecycle = dict(self.lifecycle)
            localization = dict(self.localization)
            pose = dict(self.pose) if self.pose else None
        if any(value != "active" for value in lifecycle.values()):
            raise NavigationError(
                503,
                "NAV2_NOT_ACTIVE",
                "Nav2가 아직 주행 가능한 상태가 아닙니다.",
                nav2=lifecycle,
            )
        if localization.get("state") != "ok" or pose is None:
            raise NavigationError(
                409,
                "LOCALIZATION_NOT_READY",
                localization.get("message") or "로봇 위치가 준비되지 않았습니다.",
                localization=localization,
            )
        return pose

    def _load_user_map(self) -> dict:
        try:
            value = json.loads(self.map_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NavigationError(
                503, "MAP_UNAVAILABLE", "사용자 지도를 읽을 수 없습니다."
            ) from error
        if value.get("map_id") != self.map_id:
            raise NavigationError(
                409, "MAP_REVISION_MISMATCH", "현재 지도 식별자가 변경됐습니다."
            )
        return value

    def _load_zones(self) -> tuple[list[dict], str]:
        if self.zone_path is None or not self.zone_path.is_file():
            return [], "none"
        try:
            payload = self.zone_path.read_bytes()
            value = json.loads(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NavigationError(
                503, "ZONE_UNAVAILABLE", "적용된 Zone을 읽을 수 없습니다."
            ) from error
        if value.get("map_id") != self.map_id:
            raise NavigationError(
                409,
                "MAP_REVISION_MISMATCH",
                "적용된 Zone과 현재 지도의 ID가 다릅니다.",
            )
        return value.get("features", []), hashlib.sha256(payload).hexdigest()

    def _costmap(self) -> CostmapGrid:
        if not self.get_costmap.service_is_ready():
            raise NavigationError(
                503, "NAV2_NOT_ACTIVE", "전역 costmap 서비스가 준비되지 않았습니다."
            )
        response = self._wait(
            self.get_costmap.call_async(GetCostmap.Request()),
            SERVICE_TIMEOUT_S,
            "전역 costmap",
        )
        costmap = response.map
        metadata = costmap.metadata
        expected = int(metadata.size_x) * int(metadata.size_y)
        costs = np.asarray(costmap.data, dtype=np.uint8)
        if expected <= 0 or costs.size != expected:
            raise NavigationError(
                503, "COSTMAP_INVALID", "전역 costmap 데이터가 올바르지 않습니다."
            )
        origin_yaw = _quaternion_yaw(metadata.origin.orientation)
        if abs(origin_yaw) > 1e-6:
            raise NavigationError(
                503, "COSTMAP_INVALID", "회전된 costmap 원점은 지원하지 않습니다."
            )
        return CostmapGrid(
            int(metadata.size_x),
            int(metadata.size_y),
            float(metadata.resolution),
            float(metadata.origin.position.x),
            float(metadata.origin.position.y),
            costs.reshape((int(metadata.size_y), int(metadata.size_x))),
        )

    def _static_clearance_for(self, grid: CostmapGrid) -> np.ndarray:
        """Return SLAM clearance after verifying costmap alignment."""
        static = self.static_clearance
        if (
            static.width != grid.width
            or static.height != grid.height
            or not math.isclose(
                static.resolution, grid.resolution, abs_tol=1e-9
            )
            or not math.isclose(static.origin_x, grid.origin_x, abs_tol=1e-6)
            or not math.isclose(static.origin_y, grid.origin_y, abs_tol=1e-6)
        ):
            raise NavigationError(
                503,
                "COSTMAP_INVALID",
                "SLAM 지도와 전역 costmap 좌표가 일치하지 않습니다.",
            )
        return static.costs

    @staticmethod
    def _floor_geometry(user_map: dict) -> dict:
        for feature in user_map.get("features", []):
            if feature.get("properties", {}).get("role") == "walkable_area":
                return feature.get("geometry", {})
        raise NavigationError(
            503, "MAP_INVALID", "사용자 지도에 주행 가능 공간이 없습니다."
        )

    @staticmethod
    def _restricted(point: tuple[float, float], zones: list[dict]) -> bool:
        return any(
            zone.get("properties", {}).get("behavior") == "restricted"
            and _point_in_geometry(point, zone.get("geometry", {}))
            for zone in zones
        )

    def _resolve_goal(
        self,
        requested: tuple[float, float],
        pose: dict,
        grid: CostmapGrid,
        floor: dict,
        floor_clearance: np.ndarray,
        zones: list[dict],
    ) -> tuple[tuple[float, float], float, int]:
        traversable = grid.costs < 253
        clearance = cv2.distanceTransform(
            traversable.astype(np.uint8), cv2.DIST_L2, 5
        ) * grid.resolution
        _, labels = cv2.connectedComponents(
            traversable.astype(np.uint8), connectivity=8
        )
        robot_cell = grid.cell(pose["x"], pose["y"])
        if robot_cell is None or not traversable[robot_cell]:
            raise NavigationError(
                409, "ROBOT_OUTSIDE_COSTMAP", "로봇의 현재 위치가 주행 가능 공간이 아닙니다."
            )
        robot_component = labels[robot_cell]

        def candidate(row: int, column: int) -> bool:
            point = grid.point(row, column)
            return (
                traversable[row, column]
                and clearance[row, column] >= ROBOT_RADIUS_M
                and floor_clearance[row, column]
                >= REQUIRED_PATH_CLEARANCE_M
                and labels[row, column] == robot_component
                and _point_in_geometry(point, floor)
                and not self._restricted(point, zones)
            )

        requested_cell = grid.cell(*requested)
        if requested_cell is None:
            raise NavigationError(
                422, "GOAL_OUTSIDE_MAP", "지도 밖은 목적지로 선택할 수 없습니다."
            )
        requested_cost = int(grid.costs[requested_cell])
        if self._restricted(requested, zones):
            raise NavigationError(
                422, "GOAL_IN_RESTRICTED_ZONE", "진입 금지 구역 안입니다."
            )
        if not _point_in_geometry(requested, floor):
            initial_code = "GOAL_UNEXPLORED"
            initial_message = "탐색되지 않은 공간입니다."
        elif requested_cost >= 253:
            initial_code = "GOAL_IN_OBSTACLE"
            initial_message = "벽이나 장애물 위는 목적지로 선택할 수 없습니다."
        elif (
            clearance[requested_cell] < ROBOT_RADIUS_M
            or floor_clearance[requested_cell]
            < REQUIRED_PATH_CLEARANCE_M
        ):
            initial_code = "GOAL_TOO_CLOSE_TO_WALL"
            initial_message = "로봇이 정차하기에 벽과 너무 가깝습니다."
        elif labels[requested_cell] != robot_component:
            initial_code = "GOAL_UNREACHABLE"
            initial_message = "현재 위치에서 연결되지 않은 공간입니다."
        else:
            initial_code = "PLAN_FAILED"
            initial_message = "목적지로 이동할 수 없습니다."
        if candidate(*requested_cell):
            return requested, 0.0, requested_cost

        radius = math.ceil(MAX_SNAP_DISTANCE_M / grid.resolution)
        row0, column0 = requested_cell
        choices = []
        for row in range(
            max(0, row0 - radius),
            min(grid.height, row0 + radius + 1),
        ):
            for column in range(
                max(0, column0 - radius), min(grid.width, column0 + radius + 1)
            ):
                point = grid.point(row, column)
                distance = math.hypot(
                    point[0] - requested[0], point[1] - requested[1]
                )
                if distance <= MAX_SNAP_DISTANCE_M and candidate(row, column):
                    choices.append((
                        distance, -clearance[row, column], row, column
                    ))
        if not choices:
            raise NavigationError(422, initial_code, initial_message)
        distance, _, row, column = min(choices)
        return grid.point(row, column), distance, int(grid.costs[row, column])

    def _plan(self, goal: PoseStamped) -> object:
        request = ComputePathToPose.Goal()
        request.goal = goal
        request.use_start = False
        send_future = self.compute_path.send_goal_async(request)
        goal_handle = self._wait(
            send_future, SEND_GOAL_TIMEOUT_S, "경로 계획 요청"
        )
        if goal_handle is None or not goal_handle.accepted:
            raise NavigationError(
                422, "PLAN_FAILED", "목적지까지 경로를 만들 수 없습니다."
            )
        wrapped = self._wait(
            goal_handle.get_result_async(),
            PLAN_RESULT_TIMEOUT_S,
            "경로 계획 결과",
        )
        if wrapped.status != GoalStatus.STATUS_SUCCEEDED:
            raise NavigationError(
                422, "PLAN_FAILED", "목적지까지 경로를 만들 수 없습니다."
            )
        return wrapped.result.path

    def preview(self, request: dict, session_id: str) -> dict:
        """Validate, optionally snap, and plan one clicked destination."""
        with self.operation_lock:
            pose = self._require_ready()
            with self.lock:
                if self.navigation_state["state"] in {
                    "driving", "canceling"
                }:
                    raise NavigationError(
                        409,
                        "NAVIGATION_IN_PROGRESS",
                        "이동 중에는 새 목적지를 선택할 수 없습니다.",
                    )
            if request.get("map_id") != self.map_id:
                raise NavigationError(
                    409, "MAP_REVISION_MISMATCH", "현재 지도와 요청 지도가 다릅니다."
                )
            if request.get("map_revision", "") != self.map_revision:
                raise NavigationError(
                    409, "MAP_REVISION_MISMATCH", "지도가 변경되어 다시 선택해야 합니다."
                )
            try:
                requested = (float(request["x"]), float(request["y"]))
            except (KeyError, TypeError, ValueError) as error:
                raise NavigationError(
                    422, "INVALID_GOAL", "유효한 지도 좌표가 필요합니다."
                ) from error
            if not all(math.isfinite(value) for value in requested):
                raise NavigationError(
                    422, "INVALID_GOAL", "유효한 지도 좌표가 필요합니다."
                )
            user_map = self._load_user_map()
            zones, zone_revision = self._load_zones()
            grid = self._costmap()
            floor = self._floor_geometry(user_map)
            floor_clearance = self._static_clearance_for(grid)
            resolved, snap_distance, cost = self._resolve_goal(
                requested,
                pose,
                grid,
                floor,
                floor_clearance,
                zones,
            )
            goal = PoseStamped()
            goal.header.frame_id = "map"
            goal.header.stamp = self.get_clock().now().to_msg()
            goal.pose.position.x = resolved[0]
            goal.pose.position.y = resolved[1]
            _set_pose_yaw(goal, pose["yaw"])
            path = self._plan(goal)
            if not path.poses:
                raise NavigationError(
                    422, "PLAN_FAILED", "계산된 경로가 비어 있습니다."
                )
            path_max_cost = _path_max_cost(path, grid)
            path_min_clearance = _path_min_clearance(
                path, grid, floor_clearance
            )
            if (
                path_max_cost >= 253
                or path_min_clearance < REQUIRED_PATH_CLEARANCE_M
            ):
                raise NavigationError(
                    422,
                    "PATH_TOO_NARROW",
                    "경로 중 로봇 폭보다 좁거나 장애물에 너무 가까운 통로가 있습니다.",
                    max_cost=path_max_cost,
                    min_clearance_m=round(path_min_clearance, 3),
                    required_m=round(REQUIRED_PATH_CLEARANCE_M, 3),
                )
            endpoint = path.poses[-1].pose.position
            gap = math.hypot(
                endpoint.x - resolved[0], endpoint.y - resolved[1]
            )
            if gap > MAX_GOAL_GAP_M:
                raise NavigationError(
                    422,
                    "GOAL_GAP_TOO_LARGE",
                    "선택한 지점까지 정확히 도달할 수 없습니다.",
                    gap_m=round(gap, 3),
                )
            if len(path.poses) >= 2:
                first = path.poses[-2].pose.position
                last = path.poses[-1].pose.position
                if math.hypot(last.x - first.x, last.y - first.y) > 1e-6:
                    _set_pose_yaw(
                        goal,
                        math.atan2(last.y - first.y, last.x - first.x),
                    )
            length = _path_length(path)
            avoid_on_path = any(
                zone.get("properties", {}).get("behavior") == "avoid"
                and any(
                    _point_in_geometry(
                        (item.pose.position.x, item.pose.position.y),
                        zone.get("geometry", {}),
                    )
                    for item in path.poses
                )
                for zone in zones
            )
            token = secrets.token_urlsafe(32)
            expires_at = time.monotonic() + PREVIEW_TTL_S
            response = {
                "preview_token": token,
                "expires_in_s": PREVIEW_TTL_S,
                "requested": {"x": requested[0], "y": requested[1]},
                "resolved": {
                    "x": round(resolved[0], 4),
                    "y": round(resolved[1], 4),
                    "yaw": round(_quaternion_yaw(goal.pose.orientation), 5),
                },
                "snapped": snap_distance > 1e-6,
                "snap_distance_m": round(snap_distance, 3),
                "path": {
                    "length_m": round(length, 3),
                    "points": _decimate_path(path),
                },
                "checks": [
                    {"id": "map_revision", "pass": True},
                    {"id": "localization", "pass": True},
                    {"id": "explored_free_space", "pass": True},
                    {"id": "restricted_zone", "pass": True},
                    {"id": "costmap_cost", "pass": True, "cost": cost},
                    {"id": "footprint_clearance", "pass": True},
                    {"id": "reachable_component", "pass": True},
                    {"id": "plan", "pass": True},
                    {
                        "id": "path_clearance",
                        "pass": True,
                        "max_cost": path_max_cost,
                        "min_clearance_m": round(path_min_clearance, 3),
                        "required_m": round(REQUIRED_PATH_CLEARANCE_M, 3),
                    },
                    {"id": "goal_gap", "pass": True, "gap_m": round(gap, 3)},
                ],
                "warnings": (
                    [{
                        "code": "AVOID_ZONE_ON_PATH",
                        "message": "우회 권장 구역을 지납니다.",
                    }]
                    if avoid_on_path else []
                ),
            }
            self.previews[token] = PreviewRecord(
                session_id,
                expires_at,
                self.map_revision,
                zone_revision,
                goal,
                path,
                response,
            )
            self.previews = {
                key: value
                for key, value in self.previews.items()
                if value.expires_at > time.monotonic() and not value.used
            }
            return response

    def start(self, request: dict, session_id: str) -> dict:
        """Start one previously validated preview, after revalidation."""
        with self.operation_lock:
            pose = self._require_ready()
            token = request.get("preview_token")
            record = self.previews.get(token)
            if (
                record is None
                or record.used
                or record.expires_at <= time.monotonic()
                or record.session_id != session_id
            ):
                raise NavigationError(
                    409, "PREVIEW_EXPIRED", "목적지를 다시 선택해 주세요."
                )
            with self.lock:
                if self.navigation_state["state"] == "driving":
                    raise NavigationError(
                        409, "NAVIGATION_IN_PROGRESS", "이미 이동 중입니다."
                    )
            zones, zone_revision = self._load_zones()
            if (
                record.map_revision != self.map_revision
                or record.zone_revision != zone_revision
            ):
                raise NavigationError(
                    409, "MAP_REVISION_MISMATCH", "지도 또는 Zone이 변경됐습니다."
                )
            user_map = self._load_user_map()
            grid = self._costmap()
            floor = self._floor_geometry(user_map)
            floor_clearance = self._static_clearance_for(grid)
            original_goal = (
                record.goal.pose.position.x,
                record.goal.pose.position.y,
            )
            resolved, _, _ = self._resolve_goal(
                original_goal,
                pose,
                grid,
                floor,
                floor_clearance,
                zones,
            )
            if math.hypot(
                resolved[0] - original_goal[0],
                resolved[1] - original_goal[1],
            ) > 1e-6:
                raise NavigationError(
                    409, "PATH_INVALIDATED", "주변 상황이 바뀌어 경로를 다시 선택해야 합니다."
                )
            refreshed_path = self._plan(record.goal)
            if not refreshed_path.poses:
                raise NavigationError(
                    409, "PATH_INVALIDATED", "현재 경로를 다시 계산할 수 없습니다."
                )
            path_max_cost = _path_max_cost(refreshed_path, grid)
            path_min_clearance = _path_min_clearance(
                refreshed_path, grid, floor_clearance
            )
            if (
                path_max_cost >= 253
                or path_min_clearance < REQUIRED_PATH_CLEARANCE_M
            ):
                raise NavigationError(
                    409,
                    "PATH_INVALIDATED",
                    "현재 경로가 로봇 폭보다 좁거나 장애물에 너무 가까워졌습니다.",
                    max_cost=path_max_cost,
                    min_clearance_m=round(path_min_clearance, 3),
                    required_m=round(REQUIRED_PATH_CLEARANCE_M, 3),
                )
            endpoint = refreshed_path.poses[-1].pose.position
            gap = math.hypot(
                endpoint.x - original_goal[0],
                endpoint.y - original_goal[1],
            )
            if gap > MAX_GOAL_GAP_M:
                raise NavigationError(
                    409,
                    "PATH_INVALIDATED",
                    "현재 경로가 선택한 목적지까지 정확히 이어지지 않습니다.",
                    gap_m=round(gap, 3),
                )
            refreshed_length = _path_length(refreshed_path)
            refreshed_payload = {
                "length_m": round(refreshed_length, 3),
                "points": _decimate_path(refreshed_path),
            }
            record.path = refreshed_path
            record.response["path"] = refreshed_payload
            goal = NavigateToPose.Goal()
            goal.pose = record.goal
            session = secrets.token_hex(8)
            send_future = self.navigate.send_goal_async(
                goal, feedback_callback=self._navigation_feedback
            )
            goal_handle = self._wait(
                send_future, SEND_GOAL_TIMEOUT_S, "주행 시작 요청"
            )
            if goal_handle is None or not goal_handle.accepted:
                raise NavigationError(
                    502, "NAVIGATION_REJECTED", "Nav2가 주행 요청을 거절했습니다."
                )
            record.used = True
            resolved = record.response["resolved"]
            with self.lock:
                self.navigation_goal_handle = goal_handle
                self.path_revision += 1
                self.auto_cancel_code = None
                self.auto_cancel_message = None
                self.navigation_watchdog = NavigationWatchdog.create(
                    refreshed_length
                )
                self.navigation_state = {
                    **self._idle_navigation(),
                    "session_id": session,
                    "state": "driving",
                    "goal": resolved,
                    "distance_remaining_m": (
                        record.response["path"]["length_m"]
                    ),
                    "progress_ratio": 0.0,
                    "path_length_m": record.response["path"]["length_m"],
                    "path": refreshed_payload,
                    "path_revision": self.path_revision,
                    "path_source": "start_replan",
                }
            goal_handle.get_result_async().add_done_callback(
                self._navigation_result
            )
            return {
                "session_id": session,
                "state": "driving",
                "goal": resolved,
                "path": refreshed_payload,
            }

    def _navigation_feedback(self, message: object) -> None:
        feedback = message.feedback
        eta = (
            feedback.estimated_time_remaining.sec
            + feedback.estimated_time_remaining.nanosec / 1_000_000_000.0
        )
        nav_time = (
            feedback.navigation_time.sec
            + feedback.navigation_time.nanosec / 1_000_000_000.0
        )
        with self.lock:
            if self.navigation_state["state"] != "driving":
                return
            path_length = max(
                float(self.navigation_state.get("path_length_m") or 0.0),
                1e-6,
            )
            distance = float(feedback.distance_remaining)
            if self.navigation_watchdog is not None:
                self.navigation_watchdog.observe(distance)
            self.navigation_state.update({
                "distance_remaining_m": round(distance, 3),
                "estimated_time_remaining_s": (
                    round(eta, 1) if eta > 0 else None
                ),
                "navigation_time_s": round(nav_time, 1),
                "number_of_recoveries": int(feedback.number_of_recoveries),
                "progress_ratio": round(
                    max(0.0, min(1.0, 1.0 - distance / path_length)), 3
                ),
            })

    def _navigation_result(self, future: object) -> None:
        try:
            status = future.result().status
        except Exception:
            status = GoalStatus.STATUS_ABORTED
        states = {
            GoalStatus.STATUS_SUCCEEDED: ("succeeded", None, None),
            GoalStatus.STATUS_CANCELED: ("canceled", None, None),
            GoalStatus.STATUS_ABORTED: (
                "failed", "주행을 완료하지 못했습니다.", "NAVIGATION_ABORTED"
            ),
        }
        state, message, code = states.get(
            status,
            (
                "failed",
                "주행 결과를 확인할 수 없습니다.",
                "NAVIGATION_UNKNOWN",
            ),
        )
        with self.lock:
            if (
                status == GoalStatus.STATUS_CANCELED
                and self.auto_cancel_code is not None
            ):
                state = "failed"
                message = self.auto_cancel_message
                code = self.auto_cancel_code
            self.navigation_state.update({
                "state": state,
                "message": message,
                "message_code": code,
                "progress_ratio": (
                    1.0 if state == "succeeded"
                    else self.navigation_state.get("progress_ratio")
                ),
                "distance_remaining_m": (
                    0.0 if state == "succeeded"
                    else self.navigation_state.get("distance_remaining_m")
                ),
            })
            self.navigation_goal_handle = None
            self.navigation_watchdog = None
            self.auto_cancel_pending = False
            self.auto_cancel_code = None
            self.auto_cancel_message = None

    def cancel(self, request: dict) -> dict:
        """Cancel the active session; terminal sessions are idempotent."""
        with self.operation_lock:
            requested_session = request.get("session_id")
            with self.lock:
                current = dict(self.navigation_state)
            if requested_session != current.get("session_id"):
                raise NavigationError(
                    404, "NAVIGATION_NOT_FOUND", "해당 주행 세션을 찾을 수 없습니다."
                )
            if (
                current.get("state") != "driving"
                or self.navigation_goal_handle is None
            ):
                return {
                    "session_id": requested_session,
                    "state": current.get("state"),
                    "already_terminal": True,
                }
            response = self._wait(
                self.navigation_goal_handle.cancel_goal_async(),
                CANCEL_TIMEOUT_S,
                "주행 취소",
            )
            return_code = int(response.return_code)
            return {
                "session_id": requested_session,
                "state": "canceling",
                "already_terminal": return_code == 3,
            }


class RobotRequestHandler(EditorRequestHandler):
    """Add robot state streaming and navigation commands to the editor."""

    bridge: RobotWebBridge | None = None
    stream_slots = BoundedSemaphore(MAX_SSE_CLIENTS)

    def editor_config(self, session_id: str) -> dict:
        """Advertise live robot and navigation capabilities."""
        config = super().editor_config(session_id)
        config.update({
            "robot_stream_enabled": self.bridge is not None,
            "navigation_enabled": (
                self.bridge is not None and self.map_path is not None
            ),
        })
        return config

    def do_GET(self) -> None:
        """Serve the robot SSE stream or a regular editor resource."""
        path = urlparse(self.path).path
        if path not in {"/api/robot/status", "/api/robot/stream"}:
            super().do_GET()
            return
        if not self._host_allowed():
            self._json_response(403, {"error": "request Host is not allowed"})
            return
        if self.bridge is None:
            self._json_response(503, {"error": "robot bridge is unavailable"})
            return
        if path == "/api/robot/status":
            self._json_response(
                200, self.bridge.snapshot(), {"Cache-Control": "no-store"}
            )
            return
        if not self.stream_slots.acquire(blocking=False):
            self._json_response(429, {"error": "too many robot streams"})
            return
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream; charset=utf-8"
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last_path_revision = None
            while True:
                snapshot = self.bridge.snapshot()
                navigation = snapshot.get("navigation", {})
                path_revision = navigation.get("path_revision")
                if path_revision == last_path_revision:
                    navigation.pop("path", None)
                else:
                    last_path_revision = path_revision
                payload = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                event = f"event: robot\ndata: {payload}\n\n".encode("utf-8")
                self.wfile.write(event)
                self.wfile.flush()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.stream_slots.release()

    def do_POST(self) -> None:
        """Handle bounded navigation commands or editor mutations."""
        path = urlparse(self.path).path
        if path not in {
            "/api/navigation/preview",
            "/api/navigation/start",
            "/api/navigation/cancel",
        }:
            super().do_POST()
            return
        if self.bridge is None:
            self._json_response(503, {"error": "robot bridge is unavailable"})
            return
        try:
            request = self._read_json_request()
            session_id = self._session_id()
            if path == "/api/navigation/preview":
                response = self.bridge.preview(request, session_id)
                status = 200
            elif path == "/api/navigation/start":
                response = self.bridge.start(request, session_id)
                status = 202
            else:
                response = self.bridge.cancel(request)
                status = 200
        except RequestError as error:
            self._json_response(error.status, {"error": str(error)})
            return
        except NavigationError as error:
            self._json_response(error.status, error.response())
            return
        self._json_response(status, response)


def _spin_executor(executor: MultiThreadedExecutor) -> None:
    """Exit quietly when SIGINT has already invalidated the ROS context."""
    try:
        executor.spin()
    except Exception:
        if rclpy.ok():
            raise


def main() -> int:
    """Run the ROS bridge executor and same-origin web server."""
    non_ros_arguments = remove_ros_args(args=sys.argv)[1:]
    arguments = parse_editor_arguments(non_ros_arguments)
    if arguments.map_path is None:
        print("ERROR: robot_web_server requires --map")
        return 1
    if arguments.slam_map is None:
        print("ERROR: robot_web_server requires --slam-map")
        return 1
    root = editor_asset_root()
    if not root.is_dir():
        print(f"ERROR: editor assets are missing: {root}")
        return 1
    if not configure_editor_handler(RobotRequestHandler, arguments):
        return 1
    rclpy.init(args=sys.argv)
    bridge = RobotWebBridge(
        arguments.map_path,
        str(RobotRequestHandler.map_id),
        RobotRequestHandler.map_revision,
        RobotRequestHandler.zone_output,
        arguments.slam_map,
    )
    RobotRequestHandler.bridge = bridge
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(bridge)
    executor_thread = Thread(
        target=_spin_executor, args=(executor,), daemon=True
    )
    executor_thread.start()
    handler = partial(RobotRequestHandler, directory=root)
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer((arguments.host, arguments.port), handler)
    server.daemon_threads = True
    url = f"http://{arguments.host}:{arguments.port}/?map=user-map.geojson"
    print(f"Malbut Robot Web: {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        executor.shutdown(timeout_sec=2.0)
        executor_thread.join(timeout=2.0)
        bridge.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
