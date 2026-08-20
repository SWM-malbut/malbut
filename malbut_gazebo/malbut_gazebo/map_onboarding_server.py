"""First-run web workflow for autonomous household map creation."""

from __future__ import annotations

import argparse
from functools import partial
import json
import math
from pathlib import Path
import sys
import time
from threading import BoundedSemaphore, Lock, Thread
from urllib.parse import urlparse

from action_msgs.msg import GoalStatus
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseStamped
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import OccupancyGrid, Path as NavPath
import rclpy
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from rclpy.utilities import remove_ros_args
from slam_toolbox.srv import SerializePoseGraph
import tf2_ros

from malbut_gazebo.map_lifecycle import (
    MapGrid,
    find_frontiers,
    load_active_revision,
    map_grid_from_message,
    map_statistics,
    persist_map_revision,
    render_map_png,
)
from malbut_gazebo.runtime_control import write_runtime_request
from malbut_gazebo.user_map_editor import (
    EditorRequestHandler,
    RequestError,
)


MAX_SSE_CLIENTS = 16
GOAL_RESPONSE_TIMEOUT_S = 5.0
GOAL_EXECUTION_TIMEOUT_S = 90.0
NO_FRONTIER_REVIEW_DELAY_S = 12.0
# 도달에 성공한 프론티어가 이 거리 안에서 다시 뽑히면 그 방문이 새 공간을
# 드러내지 못했다는 뜻이다. 블랙리스트 근접 판정과 같은 값을 쓴다.
UNPRODUCTIVE_FRONTIER_RADIUS_M = 0.75
# 한 번의 근접 재선정은 정상적인 점진 탐색일 수 있으므로 두 번째부터 버린다.
UNPRODUCTIVE_FRONTIER_VISITS = 2


def _path_length(path: NavPath) -> float:
    """Return the world-frame length of one Nav2 path."""
    return sum(
        math.hypot(
            current.pose.position.x - previous.pose.position.x,
            current.pose.position.y - previous.pose.position.y,
        )
        for previous, current in zip(path.poses, path.poses[1:])
    )


def _decimate_path(
    path: NavPath, spacing: float = 0.1,
) -> list[list[float]]:
    """Keep a bounded, UI-friendly version of a Nav2 global path."""
    if not path.poses:
        return []
    result = [[
        path.poses[0].pose.position.x,
        path.poses[0].pose.position.y,
    ]]
    last = result[0]
    for stamped in path.poses[1:-1]:
        point = [stamped.pose.position.x, stamped.pose.position.y]
        if math.hypot(point[0] - last[0], point[1] - last[1]) >= spacing:
            result.append(point)
            last = point
    final = [
        path.poses[-1].pose.position.x,
        path.poses[-1].pose.position.y,
    ]
    if final != result[-1]:
        result.append(final)
    return [[round(x, 4), round(y, 4)] for x, y in result]


class MappingError(RuntimeError):
    """One safe client-facing map workflow rejection."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Store an HTTP status, stable code, and safe Korean message."""
        super().__init__(message)
        self.status = status
        self.code = code

    def response(self) -> dict:
        """Return the stable JSON error contract."""
        return {"error": str(self), "code": self.code}


class MapOnboardingBridge(Node):
    """Explore frontiers, expose progress, and persist map revisions."""

    def __init__(
        self,
        store: Path,
        *,
        auto_start: bool = False,
        replace_existing: bool = False,
        save_posegraph: bool = False,
        runtime_request_file: Path | None = None,
    ) -> None:
        """Connect to SLAM, Nav2, TF, and the revision store."""
        super().__init__(
            "map_onboarding_bridge",
            automatically_declare_parameters_from_overrides=True,
        )
        self.store = store.expanduser().resolve()
        self.lock = Lock()
        self.grid: MapGrid | None = None
        self.map_png: bytes | None = None
        self.map_revision = 0
        self.pose: dict | None = None
        self.last_valid_pose: dict | None = None
        self.localization = {
            "state": "uninitialized",
            "tf_age_s": None,
        }
        self.target: dict | None = None
        self.path: dict | None = None
        self.path_revision = 0
        self.frontier_count = 0
        self.blacklisted: list[tuple[float, float]] = []
        self.goal_handle = None
        self.goal_sequence = 0
        self.goal_requested_at: float | None = None
        self.goal_started_at: float | None = None
        self.no_frontier_since: float | None = None
        self.completed_target: tuple[float, float] | None = None
        self.unproductive_visits = 0
        self.save_thread: Thread | None = None
        self.active_revision = load_active_revision(self.store)
        self.previous_revision = self.active_revision
        self.replace_existing = replace_existing
        self.save_posegraph = save_posegraph
        self.runtime_request_file = runtime_request_file
        if self.active_revision is not None and not replace_existing:
            self.state = "ready"
            self.message = "저장된 우리 집 지도를 사용하고 있습니다."
        else:
            self.state = "idle"
            self.message = "우리 집 지도 만들기를 시작할 수 있습니다."
        self.last_error: dict | None = None
        self.tf_buffer = tf2_ros.Buffer(node=self)
        self.tf_listener = tf2_ros.TransformListener(
            self.tf_buffer, self, spin_thread=False
        )
        map_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            OccupancyGrid, "/map", self._receive_map, map_qos
        )
        self.create_subscription(
            NavPath, "/plan", self._receive_navigation_path, 10
        )
        self.navigate = ActionClient(
            self, NavigateToPose, "/navigate_to_pose"
        )
        lifecycle_names = {
            "bt_navigator": "/bt_navigator/get_state",
            "planner_server": "/planner_server/get_state",
            "controller_server": "/controller_server/get_state",
            "global_costmap": "/global_costmap/global_costmap/get_state",
            "local_costmap": "/local_costmap/local_costmap/get_state",
        }
        self.lifecycle_clients = {
            name: self.create_client(GetState, service)
            for name, service in lifecycle_names.items()
        }
        self.lifecycle = {name: "unknown" for name in lifecycle_names}
        self.lifecycle_futures: dict[str, object] = {}
        self.serialize_posegraph = self.create_client(
            SerializePoseGraph, "/slam_toolbox/serialize_map"
        )
        self.wall_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.create_timer(
            0.2, self._refresh_pose, clock=self.wall_clock
        )
        self.create_timer(1.0, self._tick, clock=self.wall_clock)
        self.create_timer(
            1.0, self._refresh_lifecycle, clock=self.wall_clock
        )
        if auto_start:
            self.state = "waiting_for_map"
            self.message = "SLAM 지도를 기다리고 있습니다."

    def _request_runtime_mode(
        self, mode: str, *, delay_seconds: float = 0.0
    ) -> None:
        """Notify the optional supervisor without invalidating saved work."""
        try:
            write_runtime_request(
                self.runtime_request_file,
                mode,
                delay_seconds=delay_seconds,
            )
        except OSError as error:
            self.get_logger().warning(
                f"runtime mode request could not be written: {error}"
            )

    def _receive_map(self, message: OccupancyGrid) -> None:
        """Keep the latest SLAM grid for rate-limited rendering."""
        try:
            grid = map_grid_from_message(message)
        except ValueError as error:
            self.get_logger().warning(str(error))
            return
        with self.lock:
            self.grid = grid

    def _receive_navigation_path(self, path: NavPath) -> None:
        """Keep Nav2's current global route for local and cloud UIs."""
        if not path.poses:
            return
        payload = {
            "length_m": round(_path_length(path), 3),
            "points": _decimate_path(path),
            "source": "live_global_costmap",
        }
        with self.lock:
            if self.state != "navigating" or self.target is None:
                return
            self.path_revision += 1
            payload["revision"] = self.path_revision
            self.path = payload

    def _refresh_pose(self) -> None:
        try:
            transform = self.tf_buffer.lookup_transform(
                "map", "base_footprint", rclpy.time.Time()
            )
        except (
            tf2_ros.ConnectivityException,
            tf2_ros.ExtrapolationException,
            tf2_ros.LookupException,
        ):
            with self.lock:
                self.pose = None
                self.localization = {
                    "state": "lost", "tf_age_s": None
                }
            return
        rotation = transform.transform.rotation
        yaw = math.atan2(
            2.0 * (rotation.w * rotation.z + rotation.x * rotation.y),
            1.0 - 2.0 * (
                rotation.y * rotation.y + rotation.z * rotation.z
            ),
        )
        translation = transform.transform.translation
        stamp = transform.header.stamp
        stamp_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
        age_s = (self.get_clock().now().nanoseconds - stamp_ns) / 1e9
        if age_s > 1.0 or age_s < -2.0:
            with self.lock:
                self.pose = None
                self.localization = {
                    "state": "stale", "tf_age_s": round(age_s, 3)
                }
            return
        with self.lock:
            self.pose = {
                "x": round(float(translation.x), 4),
                "y": round(float(translation.y), 4),
                "yaw": round(yaw, 5),
            }
            self.last_valid_pose = dict(self.pose)
            self.localization = {
                "state": "ok", "tf_age_s": round(age_s, 3)
            }

    def _refresh_lifecycle(self) -> None:
        """Poll lifecycle state without blocking the executor or HTTP."""
        for name, client in self.lifecycle_clients.items():
            with self.lock:
                pending = name in self.lifecycle_futures
            if pending or not client.service_is_ready():
                continue
            future = client.call_async(GetState.Request())
            with self.lock:
                self.lifecycle_futures[name] = future
            future.add_done_callback(
                lambda completed, node_name=name: self._lifecycle_result(
                    node_name, completed
                )
            )

    def _lifecycle_result(self, name: str, future: object) -> None:
        """Cache one lifecycle response and release its request slot."""
        try:
            label = str(future.result().current_state.label)
        except Exception:
            label = "unknown"
        with self.lock:
            self.lifecycle[name] = label
            self.lifecycle_futures.pop(name, None)

    def _tick(self) -> None:
        with self.lock:
            grid = self.grid
            pose = dict(self.pose) if self.pose is not None else None
            state = self.state
            goal_handle = self.goal_handle
            requested_at = self.goal_requested_at
            started_at = self.goal_started_at
            lifecycle_ready = all(
                value == "active" for value in self.lifecycle.values()
            )
        if grid is not None:
            try:
                encoded = render_map_png(grid)
            except OSError as error:
                self.get_logger().warning(str(error))
            else:
                with self.lock:
                    if encoded != self.map_png:
                        self.map_png = encoded
                        self.map_revision += 1
        now = time.monotonic()
        if state not in {
            "waiting_for_map", "waiting_for_navigation", "exploring",
            "navigating",
        }:
            return
        if grid is None:
            self._set_state(
                "waiting_for_map", "주변을 인식할 때까지 기다리고 있습니다."
            )
            return
        if (
            pose is None
            or not lifecycle_ready
            or not self.navigate.server_is_ready()
        ):
            self._set_state(
                "waiting_for_navigation",
                "로봇 위치와 자율주행 기능을 준비하고 있습니다.",
            )
            return
        if goal_handle is None and requested_at is not None:
            if now - requested_at >= GOAL_RESPONSE_TIMEOUT_S:
                self._goal_failed("Nav2가 목적지 요청에 응답하지 않았습니다.")
            return
        if goal_handle is not None:
            if (
                started_at is not None
                and now - started_at >= GOAL_EXECUTION_TIMEOUT_S
            ):
                goal_handle.cancel_goal_async()
                self._goal_failed("한 구역 탐색 시간이 초과되어 다음 구역으로 이동합니다.")
            return
        candidates = find_frontiers(
            grid,
            (pose["x"], pose["y"]),
            blacklisted=tuple(self.blacklisted),
        )
        with self.lock:
            self.frontier_count = len(candidates)
        if not candidates:
            if self.no_frontier_since is None:
                self.no_frontier_since = now
            if now - self.no_frontier_since >= NO_FRONTIER_REVIEW_DELAY_S:
                self._set_state(
                    "review",
                    "탐색할 공간을 더 찾지 못했습니다. 지도를 확인하고 완료해 주세요.",
                )
            return
        self.no_frontier_since = None
        if self._discard_unproductive_frontier(candidates[0]):
            return
        self._send_goal(candidates[0])

    def _discard_unproductive_frontier(self, frontier: object) -> bool:
        """
        Blacklist a frontier that a completed visit failed to resolve.

        Only failed goals were blacklisted before, so a frontier the robot
        reaches without revealing new space is selected again forever and
        exploration ping-pongs between two approach points.
        """
        with self.lock:
            completed = self.completed_target
            if completed is None:
                return False
            distance = math.hypot(
                float(frontier.x) - completed[0],
                float(frontier.y) - completed[1],
            )
            if distance >= UNPRODUCTIVE_FRONTIER_RADIUS_M:
                self.completed_target = None
                self.unproductive_visits = 0
                return False
            self.unproductive_visits += 1
            if self.unproductive_visits < UNPRODUCTIVE_FRONTIER_VISITS:
                self.completed_target = None
                return False
            self.blacklisted.append(completed)
            self.blacklisted = self.blacklisted[-32:]
            self.completed_target = None
            self.unproductive_visits = 0
            self.state = "exploring"
            self.message = "이미 확인한 구역이라 다음 공간을 찾습니다."
        return True

    def _set_state(self, state: str, message: str) -> None:
        with self.lock:
            self.state = state
            self.message = message

    def _send_goal(self, frontier: object) -> None:
        goal = NavigateToPose.Goal()
        goal.pose = PoseStamped()
        goal.pose.header.frame_id = "map"
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x = frontier.x
        goal.pose.pose.position.y = frontier.y
        goal.pose.pose.orientation.z = math.sin(frontier.yaw / 2.0)
        goal.pose.pose.orientation.w = math.cos(frontier.yaw / 2.0)
        with self.lock:
            self.goal_sequence += 1
            sequence = self.goal_sequence
            self.goal_requested_at = time.monotonic()
            self.goal_started_at = None
            self.target = {
                "x": round(frontier.x, 4),
                "y": round(frontier.y, 4),
                "yaw": round(frontier.yaw, 5),
            }
            self.path = None
            self.state = "navigating"
            self.message = "새로운 공간을 확인하러 이동하고 있습니다."
        future = self.navigate.send_goal_async(goal)
        future.add_done_callback(
            lambda completed: self._goal_response(sequence, completed)
        )

    def _goal_response(self, sequence: int, future: object) -> None:
        with self.lock:
            if sequence != self.goal_sequence:
                return
        try:
            handle = future.result()
        except Exception as error:
            self._goal_failed(f"목적지 요청 실패: {error}")
            return
        if not handle.accepted:
            self._goal_failed("선택한 탐색 지점에 갈 수 없습니다.")
            return
        with self.lock:
            if sequence != self.goal_sequence:
                handle.cancel_goal_async()
                return
            self.goal_handle = handle
            self.goal_requested_at = None
            self.goal_started_at = time.monotonic()
        result = handle.get_result_async()
        result.add_done_callback(
            lambda completed: self._goal_result(sequence, completed)
        )

    def _goal_result(self, sequence: int, future: object) -> None:
        with self.lock:
            if sequence != self.goal_sequence:
                return
        try:
            status = int(future.result().status)
        except Exception as error:
            self._goal_failed(f"주행 결과 확인 실패: {error}")
            return
        if status == GoalStatus.STATUS_SUCCEEDED:
            with self.lock:
                # 도달한 지점을 기억해 둔다. 다음 선정이 이 근처로 돌아오면
                # 그 방문이 아무 공간도 드러내지 못한 것이므로 버려야 한다.
                if self.target is not None:
                    self.completed_target = (
                        float(self.target["x"]), float(self.target["y"])
                    )
                self.goal_handle = None
                self.goal_requested_at = None
                self.goal_started_at = None
                self.target = None
                self.path = None
                self.state = "exploring"
                self.message = "주변 지도를 갱신하고 있습니다."
            return
        self._goal_failed("해당 구역 주행을 완료하지 못해 다른 길을 찾습니다.")

    def _goal_failed(self, message: str) -> None:
        with self.lock:
            if self.target is not None:
                self.blacklisted.append((
                    float(self.target["x"]), float(self.target["y"])
                ))
                self.blacklisted = self.blacklisted[-32:]
            self.goal_sequence += 1
            self.goal_handle = None
            self.goal_requested_at = None
            self.goal_started_at = None
            self.target = None
            self.path = None
            if self.state not in {"canceled", "saving", "ready"}:
                self.state = "exploring"
                self.message = message

    def start(self, request: dict) -> dict:
        """Start or retry autonomous exploration."""
        replace = bool(request.get("replace", False))
        with self.lock:
            if self.active_revision is not None and not (
                replace or self.replace_existing
            ):
                raise MappingError(
                    409,
                    "MAP_ALREADY_EXISTS",
                    "저장된 지도가 있습니다. 다시 만들기를 먼저 선택해 주세요.",
                )
            if self.state in {"saving", "navigating", "exploring"}:
                raise MappingError(
                    409, "MAPPING_ALREADY_RUNNING", "이미 지도를 만들고 있습니다."
                )
            self.blacklisted.clear()
            self.last_error = None
            self.no_frontier_since = None
            self.state = "waiting_for_map"
            self.message = "SLAM 지도를 기다리고 있습니다."
        return self.snapshot()

    def cancel(self, _request: dict) -> dict:
        """Stop motion and discard only the unfinished candidate map."""
        with self.lock:
            if self.state == "saving":
                raise MappingError(
                    409, "SAVE_IN_PROGRESS", "지도 저장이 끝날 때까지 기다려 주세요."
                )
            handle = self.goal_handle
            self.goal_sequence += 1
            self.goal_handle = None
            self.goal_requested_at = None
            self.goal_started_at = None
            self.target = None
            self.path = None
            self.state = "canceled"
            self.message = "지도 만들기를 중단했습니다. 이전 지도는 그대로 유지됩니다."
        if handle is not None:
            handle.cancel_goal_async()
        if self.active_revision is not None:
            self._request_runtime_mode("navigation", delay_seconds=2.0)
        return self.snapshot()

    def finish(self, _request: dict) -> dict:
        """Stop exploration and atomically persist the current SLAM map."""
        with self.lock:
            if self.state not in {
                "exploring", "navigating", "review",
                "waiting_for_navigation",
            }:
                raise MappingError(
                    409, "MAPPING_NOT_RUNNING", "저장할 지도 만들기가 진행 중이 아닙니다."
                )
            if self.grid is None:
                raise MappingError(
                    409, "MAP_NOT_READY", "아직 저장할 지도가 없습니다."
                )
            if map_statistics(self.grid)["free_area_m2"] < 1.0:
                raise MappingError(
                    422,
                    "MAP_TOO_SMALL",
                    "조금 더 이동해 주행 가능한 공간을 확보해 주세요.",
                )
            if self.last_valid_pose is None:
                raise MappingError(
                    409,
                    "LOCALIZATION_NOT_READY",
                    "로봇 위치를 확인한 뒤 지도를 저장해 주세요.",
                )
            grid = self.grid
            initial_pose = dict(self.last_valid_pose)
            handle = self.goal_handle
            self.goal_sequence += 1
            self.goal_handle = None
            self.goal_requested_at = None
            self.goal_started_at = None
            self.target = None
            self.path = None
            self.state = "saving"
            self.message = "지도를 안전하게 저장하고 있습니다."
        if handle is not None:
            handle.cancel_goal_async()
        self.save_thread = Thread(
            target=self._save_worker,
            args=(grid, initial_pose),
            daemon=True,
        )
        self.save_thread.start()
        return self.snapshot()

    def _write_posegraph(self, base_path: Path) -> bool:
        if not self.serialize_posegraph.wait_for_service(timeout_sec=1.0):
            return False
        request = SerializePoseGraph.Request()
        request.filename = str(base_path)
        future = self.serialize_posegraph.call_async(request)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not future.done():
            time.sleep(0.05)
        if not future.done():
            return False
        try:
            return int(future.result().result) == 0
        except Exception:
            return False

    def _save_worker(
        self, grid: MapGrid, initial_pose: dict | None
    ) -> None:
        try:
            manifest = persist_map_revision(
                grid,
                self.store,
                initial_pose=initial_pose,
                posegraph_writer=(
                    self._write_posegraph if self.save_posegraph else None
                ),
            )
        except (OSError, TypeError, ValueError) as error:
            with self.lock:
                self.last_error = {
                    "code": "MAP_SAVE_FAILED", "message": str(error)
                }
                self.state = "failed"
                self.message = "지도를 저장하지 못했습니다. 다시 시도해 주세요."
            return
        with self.lock:
            self.active_revision = manifest
            self.state = "ready"
            self.message = (
                "우리 집 지도를 저장했습니다. "
                "다음 실행부터 자동으로 사용합니다."
            )
        self._request_runtime_mode("navigation", delay_seconds=1.0)

    def snapshot(self) -> dict:
        """Return one immutable browser status payload."""
        with self.lock:
            grid = self.grid
            active = (
                dict(self.active_revision) if self.active_revision else None
            )
            return {
                "state": self.state,
                "message": self.message,
                "pose": dict(self.pose) if self.pose is not None else None,
                "localization": dict(self.localization),
                "nav2": dict(self.lifecycle),
                "target": (
                    dict(self.target) if self.target is not None else None
                ),
                "path": (
                    dict(self.path) if self.path is not None else None
                ),
                "frontier_count": self.frontier_count,
                "map_revision": self.map_revision,
                "map": None if grid is None else {
                    "width": grid.width,
                    "height": grid.height,
                    "resolution": grid.resolution,
                    "origin_x": grid.origin_x,
                    "origin_y": grid.origin_y,
                    "origin_yaw": grid.origin_yaw,
                    **map_statistics(grid),
                },
                "active_revision": active,
                "previous_revision": (
                    None if self.previous_revision is None
                    else self.previous_revision.get("revision")
                ),
                "last_error": self.last_error,
            }

    def png_snapshot(self) -> tuple[bytes | None, int]:
        """Return the current friendly map rendering and revision number."""
        with self.lock:
            return self.map_png, self.map_revision


class MapOnboardingRequestHandler(EditorRequestHandler):
    """Serve the mapping UI and same-origin workflow endpoints."""

    bridge: MapOnboardingBridge | None = None
    stream_slots = BoundedSemaphore(MAX_SSE_CLIENTS)

    def editor_config(self, session_id: str) -> dict:
        """Advertise that the first-run mapping workflow is available."""
        config = super().editor_config(session_id)
        config["mapping_enabled"] = self.bridge is not None
        return config

    def do_GET(self) -> None:
        """Serve a map snapshot, status payload, or bounded SSE stream."""
        path = urlparse(self.path).path
        if path not in {
            "/api/mapping/status",
            "/api/mapping/map.png",
            "/api/mapping/stream",
        }:
            super().do_GET()
            return
        if not self._host_allowed():
            self._json_response(403, {"error": "request Host is not allowed"})
            return
        if self.bridge is None:
            self._json_response(
                503, {"error": "mapping bridge is unavailable"}
            )
            return
        if path == "/api/mapping/status":
            self._json_response(200, self.bridge.snapshot(), {
                "Cache-Control": "no-store"
            })
            return
        if path == "/api/mapping/map.png":
            payload, revision = self.bridge.png_snapshot()
            if payload is None:
                self._json_response(404, {"error": "map is not ready"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("ETag", f'"map-{revision}"')
            self.end_headers()
            self.wfile.write(payload)
            return
        if not self.stream_slots.acquire(blocking=False):
            self._json_response(429, {"error": "too many mapping streams"})
            return
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream; charset=utf-8"
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                payload = json.dumps(
                    self.bridge.snapshot(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self.wfile.write(
                    f"event: mapping\ndata: {payload}\n\n".encode("utf-8")
                )
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.stream_slots.release()

    def do_POST(self) -> None:
        """Run start, finish, and cancel commands behind CSRF checks."""
        path = urlparse(self.path).path
        operations = {
            "/api/mapping/start": "start",
            "/api/mapping/finish": "finish",
            "/api/mapping/cancel": "cancel",
        }
        operation = operations.get(path)
        if operation is None:
            super().do_POST()
            return
        if self.bridge is None:
            self._json_response(
                503, {"error": "mapping bridge is unavailable"}
            )
            return
        try:
            request = self._read_json_request()
            response = getattr(self.bridge, operation)(request)
        except RequestError as error:
            self._json_response(error.status, {"error": str(error)})
            return
        except MappingError as error:
            self._json_response(error.status, error.response())
            return
        self._json_response(202 if operation == "finish" else 200, response)


def _parse_boolean(value: str) -> bool:
    """Parse one launch-friendly boolean CLI value."""
    return value.lower() in {"1", "true", "yes", "on"}


def _optional_path(value: str) -> Path | None:
    """Parse an optional launch path without treating empty as cwd."""
    return Path(value) if value.strip() else None


def _arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the Malbut first-run map workflow."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument(
        "--store",
        type=Path,
        default=Path.home() / ".local" / "share" / "malbut" / "maps",
    )
    parser.add_argument("--auto-start", type=_parse_boolean, default=False)
    parser.add_argument(
        "--replace-existing", type=_parse_boolean, default=False
    )
    parser.add_argument(
        "--save-posegraph", type=_parse_boolean, default=False
    )
    parser.add_argument("--runtime-request-file", type=_optional_path)
    parsed = parser.parse_args(arguments)
    if not 0 < parsed.port < 65536:
        parser.error("--port must be between 1 and 65535")
    return parsed


def _spin(executor: MultiThreadedExecutor) -> None:
    try:
        executor.spin()
    except Exception:
        if rclpy.ok():
            raise


def main() -> int:
    """Run the ROS mapping bridge and first-run browser UI."""
    arguments = _arguments(remove_ros_args(args=sys.argv)[1:])
    root = Path(get_package_share_directory(
        "malbut_gazebo"
    )) / "web" / "map_onboarding"
    if not root.is_dir():
        print(f"ERROR: mapping assets are missing: {root}")
        return 1
    allowed_hosts = {
        arguments.host.strip("[]").lower(),
        *(value.strip("[]").lower() for value in arguments.allowed_host),
    }
    if arguments.host in {"127.0.0.1", "::1", "localhost"}:
        allowed_hosts.update({"127.0.0.1", "localhost", "::1"})
    MapOnboardingRequestHandler.allowed_hosts = allowed_hosts
    rclpy.init(args=sys.argv)
    bridge = MapOnboardingBridge(
        arguments.store,
        auto_start=arguments.auto_start,
        replace_existing=arguments.replace_existing,
        save_posegraph=arguments.save_posegraph,
        runtime_request_file=arguments.runtime_request_file,
    )
    MapOnboardingRequestHandler.bridge = bridge
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(bridge)
    executor_thread = Thread(target=_spin, args=(executor,), daemon=True)
    executor_thread.start()
    from http.server import ThreadingHTTPServer
    handler = partial(MapOnboardingRequestHandler, directory=root)
    server = ThreadingHTTPServer((arguments.host, arguments.port), handler)
    server.daemon_threads = True
    print(f"Malbut Map Setup: http://{arguments.host}:{arguments.port}/")
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
