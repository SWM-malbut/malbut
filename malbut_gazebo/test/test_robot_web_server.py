"""Tests for the same-origin robot web bridge contract."""

from functools import partial
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Lock, Thread
import time

import cv2
import numpy as np
import pytest

import malbut_gazebo.robot_web_server as robot_web_server_module

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path as NavPath
from std_msgs.msg import String

from malbut_gazebo.robot_web_server import (
    CostmapGrid,
    NavigationWatchdog,
    NavigationError,
    PreviewRecord,
    REQUIRED_PATH_CLEARANCE_M,
    RobotRequestHandler,
    LIFECYCLE_QUERY_TIMEOUT_S,
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


class _CountingNavigationClient:
    """Count forbidden physical goal attempts in fail-closed tests."""

    def __init__(self):
        """Create a client with no goal attempts."""
        self.send_calls = 0

    def send_goal_async(self, *_args, **_kwargs):
        """Record an unexpected attempt without contacting ROS."""
        self.send_calls += 1
        raise AssertionError("navigation goal must not be sent")


def test_navigation_progress_uses_start_route_and_is_monotonic():
    """Live replans must not reset progress or claim arrival early."""
    assert _navigation_progress_ratio(10.0, 9.9) == 0.01
    assert _navigation_progress_ratio(10.0, 6.0, 0.01) == 0.4
    assert _navigation_progress_ratio(10.0, 7.0, 0.4) == 0.4
    assert _navigation_progress_ratio(10.0, 0.0, 0.4) == 0.99


def test_navigation_progress_ignores_the_first_unknown_distance():
    """
    Nav2 sends distance_remaining 0 before it has computed one.

    Reading that as arrival pinned the bar at 99% for the whole trip,
    because progress may never move backwards afterwards.
    """
    assert _navigation_progress_ratio(13.198, 0.0, 0.0) == 0.0
    assert _navigation_progress_ratio(13.198, 12.095, 0.0) == 0.084


@pytest.mark.parametrize(
    ("lifecycle", "localization", "pose", "expected_code"),
    [
        (
            {"amcl": "inactive"},
            {"state": "ok"},
            {"x": 0.0, "y": 0.0, "yaw": 0.0},
            "NAV2_NOT_ACTIVE",
        ),
        (
            {"amcl": "active", "collision_monitor": "inactive"},
            {"state": "ok"},
            {"x": 0.0, "y": 0.0, "yaw": 0.0},
            "NAV2_NOT_ACTIVE",
        ),
        (
            {"amcl": "active"},
            {"state": "verifying"},
            {"x": 0.0, "y": 0.0, "yaw": 0.0},
            "LOCALIZATION_NOT_READY",
        ),
    ],
)
def test_start_checks_nav2_and_localization_before_any_goal_send(
    lifecycle, localization, pose, expected_code
):
    """An unready runtime must fail before reading or sending a preview."""
    bridge = object.__new__(RobotWebBridge)
    bridge.lock = Lock()
    bridge.operation_lock = Lock()
    bridge.lifecycle = lifecycle
    bridge.localization = localization
    bridge.pose = pose
    bridge.navigate = _CountingNavigationClient()

    with pytest.raises(NavigationError) as caught:
        bridge.start({"preview_token": "anything"}, "session")

    assert caught.value.code == expected_code
    assert bridge.navigate.send_calls == 0


def test_future_dated_tf_fails_closed_before_any_goal_send():
    """Reject a TF far ahead of ROS time instead of reporting age zero."""
    bridge = object.__new__(RobotWebBridge)
    bridge.lock = Lock()
    bridge.operation_lock = Lock()
    bridge.lifecycle = {"amcl": "active", "controller": "active"}
    bridge.validation_state = "ok"
    bridge.pose = {"x": 0.0, "y": 0.0, "yaw": 0.0}
    bridge.localization = {"state": "ok", "tf_age_s": 0.0}
    bridge.navigate = _CountingNavigationClient()
    bridge.tf_buffer = SimpleNamespace(
        lookup_transform=lambda *_args: SimpleNamespace(
            header=SimpleNamespace(
                stamp=SimpleNamespace(sec=103, nanosec=0)
            ),
            transform=SimpleNamespace(
                translation=SimpleNamespace(x=1.0, y=2.0),
                rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
            ),
        )
    )
    bridge.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(nanoseconds=100_000_000_000)
    )
    cancel_codes = []
    stop_codes = []
    bridge._request_auto_cancel = (
        lambda code, _message: cancel_codes.append(code)
    )
    bridge._request_autonomous_stop = (
        lambda code, _message: stop_codes.append(code)
    )

    bridge._refresh_pose()

    assert bridge.pose is None
    assert bridge.localization["state"] == "lost"
    assert bridge.localization["tf_age_s"] == 0.0
    assert cancel_codes == ["LOCALIZATION_LOST"]
    assert stop_codes == ["LOCALIZATION_LOST"]
    with pytest.raises(NavigationError) as caught:
        bridge.start({"preview_token": "anything"}, "session")
    assert caught.value.code == "LOCALIZATION_NOT_READY"
    assert bridge.navigate.send_calls == 0


def test_expired_preview_stops_before_any_goal_send():
    """Never turn an expired capability into a new NavigateToPose goal."""
    bridge = object.__new__(RobotWebBridge)
    bridge.operation_lock = Lock()
    bridge._require_ready = lambda: {"x": 0.0, "y": 0.0, "yaw": 0.0}
    bridge._require_autonomous_idle = lambda: None
    bridge.navigate = _CountingNavigationClient()
    bridge.previews = {
        "expired": PreviewRecord(
            session_id="session",
            expires_at=time.monotonic() - 1.0,
            map_revision="rev-current",
            zone_revision="zone-current",
            user_map_digest=None,
            goal=PoseStamped(),
            path=NavPath(),
            response={},
        )
    }

    with pytest.raises(NavigationError) as caught:
        bridge.start({"preview_token": "expired"}, "session")

    assert caught.value.code == "PREVIEW_EXPIRED"
    assert bridge.navigate.send_calls == 0


def test_canceling_navigation_blocks_preview_start_race():
    """Do not send a new goal while an earlier goal is still canceling."""
    bridge = object.__new__(RobotWebBridge)
    bridge.lock = Lock()
    bridge.operation_lock = Lock()
    bridge._require_ready = lambda: {"x": 0.0, "y": 0.0, "yaw": 0.0}
    bridge._require_autonomous_idle = lambda: None
    bridge.navigation_state = {"state": "canceling"}
    bridge.navigate = _CountingNavigationClient()
    bridge.previews = {
        "valid": PreviewRecord(
            session_id="session",
            expires_at=time.monotonic() + 30.0,
            map_revision="rev-current",
            zone_revision="zone-current",
            user_map_digest=None,
            goal=PoseStamped(),
            path=NavPath(),
            response={},
        )
    }

    with pytest.raises(NavigationError) as caught:
        bridge.start({"preview_token": "valid"}, "session")

    assert caught.value.code == "NAVIGATION_IN_PROGRESS"
    assert bridge.navigate.send_calls == 0


def test_planning_safety_loss_blocks_final_goal_send(monkeypatch):
    """Recheck mutable lifecycle gates after planning and before Nav2."""
    bridge = object.__new__(RobotWebBridge)
    bridge.lock = Lock()
    bridge.operation_lock = Lock()
    bridge.lifecycle = {
        "amcl": "active",
        "collision_monitor": "active",
        "controller_server": "active",
    }
    bridge.localization = {"state": "ok", "tf_age_s": 0.02}
    bridge.pose = {"x": 0.0, "y": 0.0, "yaw": 0.0}
    bridge.navigation_state = {"state": "idle"}
    bridge._require_autonomous_idle = lambda: None
    bridge.navigate = _CountingNavigationClient()
    bridge.map_revision = "rev-current"
    goal = PoseStamped()
    path = NavPath()
    path.poses = [PoseStamped()]
    bridge.previews = {
        "valid": PreviewRecord(
            session_id="session",
            expires_at=time.monotonic() + 30.0,
            map_revision="rev-current",
            zone_revision="zone-current",
            user_map_digest=None,
            goal=goal,
            path=path,
            response={"resolved": {"x": 0.0, "y": 0.0}},
        )
    }
    bridge._load_zones = lambda: ([], "zone-current")
    bridge._load_user_map_snapshot = lambda: ({}, None)
    bridge._costmap = lambda: object()
    bridge._floor_geometry = lambda _user_map: object()
    bridge._static_clearance_for = lambda _grid: object()
    bridge._resolve_goal = lambda *_args: ((0.0, 0.0), 0.0, 0)

    def plan_then_lose_safety(_goal):
        bridge.lifecycle["collision_monitor"] = "inactive"
        return path

    bridge._plan = plan_then_lose_safety
    monkeypatch.setattr(
        robot_web_server_module, "_path_max_cost", lambda *_args: 0
    )
    monkeypatch.setattr(
        robot_web_server_module, "_path_min_clearance", lambda *_args: 1.0
    )
    monkeypatch.setattr(
        robot_web_server_module, "_path_length", lambda *_args: 1.0
    )
    monkeypatch.setattr(
        robot_web_server_module, "_decimate_path", lambda *_args: []
    )

    with pytest.raises(NavigationError) as caught:
        bridge.start({"preview_token": "valid"}, "session")

    assert caught.value.code == "NAV2_NOT_ACTIVE"
    assert bridge.navigate.send_calls == 0
    assert bridge.previews["valid"].used is False


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
        self.device_id = "malbut-sim-01"
        self.simulation = True

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
            "scenario": {
                "mode": "idle",
                "active": False,
                "target_mode": None,
                "actor_visible": False,
            },
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

    def run_scenario_command(self, command: str) -> dict:
        """Record one exclusive scenario command."""
        self.calls.append(("scenario", command, None))
        return {
            "accepted": True,
            "message": "scenario command accepted",
            "scenario": {
                "mode": "transitioning",
                "active": False,
                "target_mode": "patrolling",
            },
        }

    def run_demo_person_command(self, command: str) -> dict:
        """Record one explicit simulation-person command."""
        self.calls.append(("demo-person", command, None))
        return {
            "accepted": True,
            "message": f"person {command}",
            "person": {"visible": command == "show"},
        }


class QuietRobotHandler(RobotRequestHandler):
    """Disable request logs during contract tests."""

    def log_message(self, _format, *_arguments) -> None:
        """Discard one request log line."""


def test_user_map_digest_binds_preview_to_one_exact_semantic_snapshot(
    tmp_path,
):
    """Reject malformed, stale, or post-preview User Map content."""
    path = tmp_path / "user-map.geojson"
    value = {
        "type": "FeatureCollection",
        "map_id": "home",
        "map_revision": "rev-current",
        "features": [],
    }
    payload = json.dumps(value, sort_keys=True).encode("utf-8")
    path.write_bytes(payload)
    bridge = RobotWebBridge.__new__(RobotWebBridge)
    bridge.map_path = path
    bridge.map_id = "home"
    bridge.map_revision = "rev-current"

    loaded, digest = bridge._load_user_map_snapshot()

    assert loaded == value
    assert digest == hashlib.sha256(payload).hexdigest()
    assert bridge._require_user_map_digest(digest, digest) == digest
    with pytest.raises(NavigationError) as invalid:
        bridge._require_user_map_digest("not-a-digest", digest)
    assert invalid.value.code == "INVALID_SEMANTIC_BINDING"
    with pytest.raises(NavigationError) as stale:
        bridge._require_user_map_digest("0" * 64, digest)
    assert stale.value.code == "SEMANTIC_REVISION_MISMATCH"

    value["features"].append({"changed": True})
    path.write_text(json.dumps(value), encoding="utf-8")
    _, changed_digest = bridge._load_user_map_snapshot()
    assert changed_digest != digest


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
        assert config["device_id"] == "malbut-sim-01"
        assert config["simulation"] is True
        headers = _headers(address, cookie, config["csrf_token"])
        status, response_headers, value = _request(
            address, "GET", "/api/robot/status"
        )
        assert status == 200
        assert response_headers["Cache-Control"] == "no-store"
        assert value["pose"] == {"x": 1.0, "y": 2.0, "yaw": 0.5}
        assert value["scenario"]["mode"] == "idle"
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
            "/api/scenario/start-patrol",
            "{}",
            headers,
        )
        assert status == 200
        assert value["scenario"] == {
            "mode": "transitioning",
            "active": False,
            "target_mode": "patrolling",
        }
        assert bridge.calls[-1] == ("scenario", "start-patrol", None)

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
            "/api/scenario/toggle-person",
            "{}",
            headers,
        )
        assert status == 200
        assert value["accepted"] is True
        assert bridge.calls[-1] == ("scenario", "toggle-person", None)

        status, _, value = _request(
            address,
            "POST",
            "/api/demo/person/show",
            "{}",
            headers,
        )
        assert status == 200
        assert value["accepted"] is True
        assert bridge.calls[-1] == ("demo-person", "show", None)

        status, _, value = _request(
            address,
            "POST",
            "/api/demo/person/hide",
            "{}",
            headers,
        )
        assert status == 200
        assert value["person"]["visible"] is False
        assert bridge.calls[-1] == ("demo-person", "hide", None)

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


class _StuckFuture:
    """A service call whose reply never arrives."""

    def __init__(self):
        self.removed = False

    def done(self) -> bool:
        return False

    def add_done_callback(self, _callback) -> None:
        return None


class _RecordingClient:
    def __init__(self, ready: bool = True):
        self.ready = ready
        self.calls = 0
        self.removed = []

    def service_is_ready(self) -> bool:
        return self.ready

    def call_async(self, _request):
        self.calls += 1
        return _StuckFuture()

    def remove_pending_request(self, future) -> None:
        future.removed = True
        self.removed.append(future)


def _lifecycle_bridge(client):
    bridge = RobotWebBridge.__new__(RobotWebBridge)
    bridge.lifecycle_clients = {"amcl": client}
    bridge.lifecycle_futures = {}
    bridge.lifecycle_future_deadlines = {}
    bridge.lifecycle = {"amcl": "unknown"}
    bridge.lock = Lock()
    return bridge


def test_lifecycle_query_retries_after_a_reply_never_arrives():
    """
    A dropped reply used to pin one node's state at "unknown" forever.

    The pose view treats anything but "active" as degraded and hides the
    robot, so one lost service reply removed the robot from the map for
    the rest of the session.
    """
    client = _RecordingClient()
    bridge = _lifecycle_bridge(client)

    bridge._refresh_lifecycle()
    first = bridge.lifecycle_futures["amcl"]
    bridge._refresh_lifecycle()

    assert client.calls == 1, "기한 안에는 다시 묻지 않는다"

    bridge.lifecycle_future_deadlines["amcl"] = 0.0
    bridge._refresh_lifecycle()

    assert first.removed is True
    assert client.calls == 2
    assert bridge.lifecycle_futures["amcl"] is not first


def test_lifecycle_query_marks_an_absent_service_unavailable():
    bridge = _lifecycle_bridge(_RecordingClient(ready=False))

    bridge._refresh_lifecycle()

    assert bridge.lifecycle["amcl"] == "unavailable"


def test_lifecycle_query_timeout_is_bounded():
    assert 0.0 < LIFECYCLE_QUERY_TIMEOUT_S <= 30.0
