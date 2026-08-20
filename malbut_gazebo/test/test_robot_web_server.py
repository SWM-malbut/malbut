"""Tests for the same-origin robot web bridge contract."""

from functools import partial
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Lock
from threading import Thread
import time

import cv2
import numpy as np
import pytest

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path as NavPath
from std_msgs.msg import String

from malbut_gazebo.robot_web_server import (
    CostmapGrid,
    NavigationWatchdog,
    NavigationError,
    REQUIRED_PATH_CLEARANCE_M,
    RobotRequestHandler,
    RobotWebBridge,
    _drive_mode_from_navigation,
    _navigation_progress_ratio,
    _path_min_clearance,
    _path_max_cost,
    _point_in_geometry,
)


class _ImmediateFuture:
    def __init__(self, value):
        self.value = value

    def add_done_callback(self, callback):
        callback(self)

    def result(self):
        return self.value


class _DriveClient:
    def service_is_ready(self):
        return True

    def wait_for_service(self, timeout_sec):
        return timeout_sec > 0

    def call_async(self, _request):
        return _ImmediateFuture(SimpleNamespace(success=True, message="ok"))


class _PendingFuture:
    def __init__(self):
        self.callbacks = []
        self.value = None

    def add_done_callback(self, callback):
        self.callbacks.append(callback)

    def result(self):
        return self.value

    def complete(self, value):
        self.value = value
        for callback in self.callbacks:
            callback(self)


class _FollowGoalHandle:
    accepted = True

    def __init__(self):
        self.result_future = _PendingFuture()
        self.cancel_calls = 0

    def get_result_async(self):
        return self.result_future

    def cancel_goal_async(self):
        self.cancel_calls += 1
        return _ImmediateFuture(SimpleNamespace(goals_canceling=[self]))


class _FollowClient:
    def __init__(self):
        self.goal_handle = _FollowGoalHandle()

    def server_is_ready(self):
        return True

    def wait_for_server(self, timeout_sec):
        return timeout_sec > 0

    def send_goal_async(self, _goal):
        return _ImmediateFuture(self.goal_handle)


def test_navigation_progress_uses_start_route_and_is_monotonic():
    """Live replans must not reset progress or claim arrival early."""
    assert _navigation_progress_ratio(10.0, 9.9) == 0.01
    assert _navigation_progress_ratio(10.0, 6.0, 0.01) == 0.4
    assert _navigation_progress_ratio(10.0, 7.0, 0.4) == 0.4
    assert _navigation_progress_ratio(10.0, 0.0, 0.4) == 0.99


def test_destination_navigation_reports_one_common_drive_mode():
    """Direct destination travel must participate in common arbitration."""
    assert _drive_mode_from_navigation({
        "state": "driving", "session_id": "navigation_session_1",
    }) == {
        "mode": "destination", "state": "active",
        "session_id": "navigation_session_1", "message": None,
    }
    assert _drive_mode_from_navigation({
        "state": "canceling", "session_id": "navigation_session_1",
        "message": "stopping",
    })["state"] == "stopping"
    assert _drive_mode_from_navigation({"state": "succeeded"})["mode"] == "idle"


def test_autonomous_drive_mode_owns_one_session_and_rejects_conflicts(tmp_path):
    """Patrol and roaming must never own Nav2 at the same time."""
    user_map = tmp_path / "user-map.geojson"
    user_map.write_text(json.dumps({
        "type": "FeatureCollection",
        "map_id": "home",
        "features": [{
            "type": "Feature",
            "id": "room-1",
            "properties": {
                "role": "room", "name": "거실",
                "representative_point": [1.0, 2.0],
            },
            "geometry": {"type": "Polygon", "coordinates": []},
        }],
    }), encoding="utf-8")
    bridge = object.__new__(RobotWebBridge)
    bridge.lock = Lock()
    bridge.operation_lock = Lock()
    bridge.map_path = user_map
    bridge.map_id = "home"
    bridge.patrol_route_file = tmp_path / "room-patrol.yaml"
    bridge.navigation_state = {"state": "idle"}
    bridge.autonomous_drive = bridge._idle_autonomous_drive()
    bridge.drive_seen_active = False
    bridge.drive_started_monotonic = 0.0
    bridge.drive_emergency_stop_pending = False
    bridge.drive_status = {"patrol": {}, "roaming": {}}
    bridge.follow_person = _FollowClient()
    bridge.follow_goal_handle = None
    bridge.follow_cancel_requested = False
    bridge.drive_clients = {
        mode: {action: _DriveClient() for action in (
            "start", "pause", "resume", "stop",
        )}
        for mode in ("patrol", "roaming")
    }
    bridge._require_ready = lambda: {"x": 0.0, "y": 0.0, "yaw": 0.0}

    started = bridge.drive_mode_command("start", {"mode": "patrol"})
    session_id = started["session_id"]
    assert session_id
    assert Path(bridge.patrol_route_file).is_file()
    status = String()
    status.data = json.dumps({
        "state": "navigating", "detail": "moving",
        "waypoint_index": 0, "waypoint_count": 1,
    })
    bridge._receive_drive_status("patrol", status)
    assert bridge.autonomous_drive["state"] == "active"
    assert bridge.autonomous_drive["session_id"] == session_id

    with pytest.raises(NavigationError) as conflict:
        bridge.drive_mode_command("start", {"mode": "roaming"})
    assert conflict.value.code == "DRIVE_MODE_IN_PROGRESS"
    conflicting_status = String()
    conflicting_status.data = json.dumps({
        "state": "navigating", "detail": "unexpected roaming",
    })
    bridge._receive_drive_status("roaming", conflicting_status)
    assert bridge.autonomous_drive["state"] == "failed"
    assert bridge.autonomous_drive["detail"] == {
        "active_modes": ["patrol", "roaming"],
    }
    conflicting_status.data = json.dumps({"state": "idle"})
    bridge._receive_drive_status("roaming", conflicting_status)
    assert bridge.autonomous_drive["mode"] == "patrol"
    assert bridge.autonomous_drive["state"] == "active"
    with pytest.raises(NavigationError) as stale:
        bridge.drive_mode_command("pause", {
            "mode": "patrol", "session_id": "wrong-session",
        })
    assert stale.value.code == "DRIVE_MODE_NOT_FOUND"

    bridge.drive_mode_command("pause", {
        "mode": "patrol", "session_id": session_id,
    })
    status.data = json.dumps({"state": "paused", "detail": "paused"})
    bridge._receive_drive_status("patrol", status)
    assert bridge.autonomous_drive["state"] == "paused"
    bridge.drive_mode_command("stop", {
        "mode": "patrol", "session_id": session_id,
    })
    status.data = json.dumps({"state": "idle", "detail": "stopped"})
    bridge._receive_drive_status("patrol", status)
    assert bridge.autonomous_drive["mode"] == "idle"


def test_person_following_reports_recovery_and_stops_on_safety_loss(tmp_path):
    """One person action must expose recovery and cancel on safety loss."""
    bridge = object.__new__(RobotWebBridge)
    bridge.lock = Lock()
    bridge.operation_lock = Lock()
    bridge.map_path = tmp_path / "user-map.geojson"
    bridge.map_id = "home"
    bridge.patrol_route_file = None
    bridge.navigation_state = {"state": "idle"}
    bridge.autonomous_drive = bridge._idle_autonomous_drive()
    bridge.drive_seen_active = False
    bridge.drive_started_monotonic = 0.0
    bridge.drive_emergency_stop_pending = False
    bridge.drive_status = {"patrol": {}, "roaming": {}}
    bridge.drive_clients = {
        mode: {action: _DriveClient() for action in (
            "start", "pause", "resume", "stop",
        )}
        for mode in ("patrol", "roaming")
    }
    bridge.follow_person = _FollowClient()
    bridge.follow_goal_handle = None
    bridge.follow_cancel_requested = False
    bridge._require_ready = lambda: {"x": 0.0, "y": 0.0, "yaw": 0.0}

    started = bridge.drive_mode_command(
        "start", {"mode": "person_following"}
    )
    session_id = started["session_id"]
    assert started["state"] == "starting"
    waiting = String()
    waiting.data = json.dumps({
        "state": "IDLE", "target_visible": False,
    })
    bridge._receive_person_status(waiting)
    assert bridge.autonomous_drive["state"] == "active"
    assert "찾고" in bridge.autonomous_drive["message"]

    tracking = String()
    tracking.data = json.dumps({
        "state": "TRACKING", "target_visible": True,
        "current_distance_m": 1.2,
    })
    bridge._receive_person_status(tracking)
    assert bridge.autonomous_drive["detail"]["tracking_state"] == "TRACKING"
    with pytest.raises(NavigationError) as conflict:
        bridge.drive_mode_command("start", {"mode": "patrol"})
    assert conflict.value.code == "DRIVE_MODE_IN_PROGRESS"

    bridge._request_autonomous_stop(
        "LOCALIZATION_LOST", "위치가 끊겨 안전 중지합니다."
    )
    deadline = time.monotonic() + 1.0
    while bridge.drive_emergency_stop_pending and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bridge.follow_person.goal_handle.cancel_calls == 1
    assert bridge.autonomous_drive["state"] == "stopping"
    assert bridge.autonomous_drive["detail"]["stop_code"] == (
        "LOCALIZATION_LOST"
    )

    bridge.follow_person.goal_handle.result_future.complete(SimpleNamespace(
        status=GoalStatus.STATUS_CANCELED,
        result=SimpleNamespace(success=False, message="canceled"),
    ))
    assert bridge.autonomous_drive["mode"] == "idle"
    assert bridge.follow_goal_handle is None
    assert session_id


class FakeBridge:
    """Record navigation calls without requiring a ROS graph."""

    def __init__(self) -> None:
        """Create an empty call log."""
        self.calls = []

    def snapshot(self) -> dict:
        """Return one healthy robot state event."""
        return {
            "seq": 1,
            "map_id": "home",
            "map_revision": "rev-current",
            "pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
            "localization": {"state": "ok", "tf_age_s": 0.02},
            "nav2": {"amcl": "active"},
            "navigation": {"state": "idle"},
        }

    def preview(self, request: dict, session_id: str) -> dict:
        """Return a deterministic preview or one unsafe-goal error."""
        self.calls.append(("preview", request, session_id))
        if request.get("x") == 999:
            raise NavigationError(
                422, "GOAL_OUTSIDE_MAP", "지도 밖입니다."
            )
        return {
            "preview_token": "preview",
            "resolved": {"x": request["x"], "y": request["y"]},
            "path": {"length_m": 1.0, "points": [[0, 0], [1, 1]]},
        }

    def start(self, request: dict, session_id: str) -> dict:
        """Record and accept a start request."""
        self.calls.append(("start", request, session_id))
        return {"session_id": "nav-1", "state": "driving"}

    def cancel(self, request: dict) -> dict:
        """Record and accept a cancel request."""
        self.calls.append(("cancel", request, None))
        return {
            "session_id": request["session_id"],
            "state": "canceled",
            "already_terminal": False,
        }

    def drive_mode_command(self, action: str, request: dict) -> dict:
        """Record and accept one common autonomous-mode command."""
        self.calls.append((f"drive-{action}", request, None))
        return {
            "mode": request["mode"],
            "state": "active" if action == "start" else action,
            "session_id": request.get("session_id", "drive-1"),
        }


class QuietRobotHandler(RobotRequestHandler):
    """Disable request logs during contract tests."""

    def log_message(self, _format, *_arguments) -> None:
        """Discard one request log line."""


def _request(address, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*address, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    result = (
        response.status,
        dict(response.getheaders()),
        json.loads(payload) if payload else None,
    )
    connection.close()
    return result


def _session(address):
    status, headers, value = _request(
        address, "GET", "/api/editor-config"
    )
    assert status == 200
    return headers["Set-Cookie"].split(";", 1)[0], value


def _headers(address, cookie, csrf_token):
    host = f"{address[0]}:{address[1]}"
    return {
        "Content-Type": "application/json",
        "Cookie": cookie,
        "X-CSRF-Token": csrf_token,
        "Origin": f"http://{host}",
    }


def test_geometry_and_costmap_coordinate_contract():
    """Goal checks must use map coordinates and respect polygon holes."""
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]],
        ],
    }
    assert _point_in_geometry((0.5, 0.5), geometry)
    assert not _point_in_geometry((1.5, 1.5), geometry)
    assert not _point_in_geometry((5.0, 5.0), geometry)
    grid = CostmapGrid(
        3, 2, 0.5, -1.0, 2.0, np.zeros((2, 3), dtype=np.uint8)
    )
    assert grid.cell(-0.75, 2.25) == (0, 0)
    assert grid.cell(0.49, 2.99) == (1, 2)
    assert grid.cell(0.5, 2.5) is None
    assert grid.point(1, 2) == (0.25, 2.75)


def test_path_cost_contract_rejects_inscribed_bottlenecks():
    """Sampling must catch an unsafe cell between sparse path poses."""
    costs = np.zeros((3, 5), dtype=np.uint8)
    costs[1, 2] = 253
    grid = CostmapGrid(5, 3, 0.5, 0.0, 0.0, costs)
    path = NavPath()
    for x in (0.25, 2.25):
        pose = PoseStamped()
        pose.pose.position.x = x
        pose.pose.position.y = 0.75
        path.poses.append(pose)
    assert _path_max_cost(path, grid) == 253

    for pose in path.poses:
        pose.pose.position.y = 0.25
    assert _path_max_cost(path, grid) == 0


def test_path_clearance_rejects_real_bottleneck_without_cost_253():
    """A physically tight corridor must fail even when cost 253 is absent."""
    costs = np.full((12, 30), 224, dtype=np.uint8)
    grid = CostmapGrid(30, 12, 0.05, 0.0, 0.0, costs)
    free = np.zeros((12, 30), dtype=np.uint8)
    free[1:11, :] = 1
    clearance = cv2.distanceTransform(free, cv2.DIST_L2, 5) * 0.05
    path = NavPath()
    for x in (0.125, 1.375):
        pose = PoseStamped()
        pose.pose.position.x = x
        pose.pose.position.y = 0.275
        path.poses.append(pose)

    assert _path_max_cost(path, grid) == 224
    assert np.isclose(_path_min_clearance(path, grid, clearance), 0.25)
    assert (
        _path_min_clearance(path, grid, clearance)
        < REQUIRED_PATH_CLEARANCE_M
    )

    safe_costs = np.full((16, 30), 224, dtype=np.uint8)
    safe_grid = CostmapGrid(30, 16, 0.05, 0.0, 0.0, safe_costs)
    safe_free = np.zeros((16, 30), dtype=np.uint8)
    safe_free[1:15, :] = 1
    safe_clearance = (
        cv2.distanceTransform(safe_free, cv2.DIST_L2, 5) * 0.05
    )
    for pose in path.poses:
        pose.pose.position.y = 0.375
    assert (
        _path_min_clearance(path, safe_grid, safe_clearance)
        >= REQUIRED_PATH_CLEARANCE_M
    )


def test_navigation_watchdog_bounds_time_and_stalled_progress():
    """Navigation must stop on timeout or no meaningful progress."""
    watchdog = NavigationWatchdog.create(8.0, now=100.0)
    assert watchdog.failure(now=144.9) is None
    assert watchdog.failure(now=145.0)[0] == "NAVIGATION_STALLED"

    watchdog = NavigationWatchdog.create(8.0, now=100.0)
    watchdog.observe(7.95, now=120.0)
    assert watchdog.last_progress_at == 100.0
    watchdog.observe(7.89, now=125.0)
    assert watchdog.last_progress_at == 125.0
    assert watchdog.failure(now=169.9) is None
    assert watchdog.failure(now=170.0)[0] == "NAVIGATION_STALLED"

    watchdog = NavigationWatchdog.create(100.0, now=100.0)
    assert watchdog.deadline == 400.0
    watchdog.last_progress_at = 399.5
    assert watchdog.failure(now=400.0)[0] == "NAVIGATION_TIMEOUT"


def test_navigation_endpoints_are_same_origin_and_session_bound(tmp_path):
    """Preview, start, and cancel must cross the editor security boundary."""
    bridge = FakeBridge()
    QuietRobotHandler.bridge = bridge
    QuietRobotHandler.map_path = tmp_path / "map.geojson"
    QuietRobotHandler.map_id = "home"
    QuietRobotHandler.map_revision = "rev-current"
    QuietRobotHandler.allowed_hosts = {"127.0.0.1", "localhost"}
    QuietRobotHandler.slam_map_path = None
    QuietRobotHandler.zone_mask_output = None
    QuietRobotHandler.zone_output = None
    handler = partial(QuietRobotHandler, directory=tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = server.server_address
        cookie, config = _session(address)
        assert config["robot_stream_enabled"] is True
        assert config["navigation_enabled"] is True
        headers = _headers(address, cookie, config["csrf_token"])
        status, response_headers, value = _request(
            address, "GET", "/api/robot/status"
        )
        assert status == 200
        assert response_headers["Cache-Control"] == "no-store"
        assert value["pose"] == {"x": 1.0, "y": 2.0, "yaw": 0.5}
        preview = {
            "map_id": "home", "map_revision": "rev-current",
            "x": 1.0, "y": 2.0,
        }
        status, _, value = _request(
            address,
            "POST",
            "/api/navigation/preview",
            json.dumps(preview),
            headers,
        )
        assert status == 200
        assert value["preview_token"] == "preview"
        session_id = bridge.calls[-1][2]
        assert session_id and session_id in cookie

        status, _, value = _request(
            address,
            "POST",
            "/api/navigation/start",
            json.dumps({"preview_token": "preview"}),
            headers,
        )
        assert status == 202
        assert value["state"] == "driving"
        assert bridge.calls[-1][2] == session_id

        status, _, value = _request(
            address,
            "POST",
            "/api/navigation/cancel",
            json.dumps({"session_id": "nav-1"}),
            headers,
        )
        assert status == 200
        assert value["state"] == "canceled"

        status, _, value = _request(
            address,
            "POST",
            "/api/drive-mode/start",
            json.dumps({"mode": "patrol"}),
            headers,
        )
        assert status == 202
        assert value["session_id"] == "drive-1"
        assert bridge.calls[-1][0] == "drive-start"

        status, _, value = _request(
            address,
            "POST",
            "/api/drive-mode/stop",
            json.dumps({"mode": "patrol", "session_id": "drive-1"}),
            headers,
        )
        assert status == 200
        assert value["state"] == "stop"

        status, _, value = _request(
            address,
            "POST",
            "/api/navigation/preview",
            json.dumps(preview),
            {**headers, "Origin": "http://evil.example"},
        )
        assert status == 403
        assert "Origin" in value["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_navigation_error_returns_stable_code(tmp_path):
    """Unsafe goals must expose a stable UI translation code."""
    bridge = FakeBridge()
    QuietRobotHandler.bridge = bridge
    QuietRobotHandler.map_path = tmp_path / "map.geojson"
    QuietRobotHandler.map_id = "home"
    QuietRobotHandler.map_revision = "rev-current"
    QuietRobotHandler.allowed_hosts = {"127.0.0.1", "localhost"}
    QuietRobotHandler.slam_map_path = None
    QuietRobotHandler.zone_mask_output = None
    QuietRobotHandler.zone_output = None
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(QuietRobotHandler, directory=tmp_path),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = server.server_address
        cookie, config = _session(address)
        status, _, value = _request(
            address,
            "POST",
            "/api/navigation/preview",
            json.dumps({"x": 999, "y": 0}),
            _headers(address, cookie, config["csrf_token"]),
        )
        assert status == 422
        assert value == {
            "error_code": "GOAL_OUTSIDE_MAP",
            "message": "지도 밖입니다.",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
