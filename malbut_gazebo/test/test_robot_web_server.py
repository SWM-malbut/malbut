"""Tests for the same-origin robot web bridge contract."""

from functools import partial
import http.client
from http.server import ThreadingHTTPServer
import json
from threading import Thread

import cv2
import numpy as np

from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path as NavPath

from malbut_gazebo.robot_web_server import (
    CostmapGrid,
    NavigationWatchdog,
    NavigationError,
    REQUIRED_PATH_CLEARANCE_M,
    RobotRequestHandler,
    _path_min_clearance,
    _path_max_cost,
    _point_in_geometry,
)


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
