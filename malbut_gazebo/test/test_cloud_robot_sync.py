from pathlib import Path
import base64
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.request import HTTPCookieProcessor, build_opener

import pytest
import numpy as np

from malbut_gazebo.cloud_robot_sync import CloudRobotSync, TOKEN_PATTERN
from malbut_gazebo.map_lifecycle import MapGrid


def test_cloud_token_contract_matches_homecam_device_token():
    token = (
        "hc1.123e4567-e89b-42d3-a456-426614174000."
        + "a" * 64
    )
    assert TOKEN_PATTERN.fullmatch(token)
    assert not TOKEN_PATTERN.fullmatch("secret")


def test_cloud_token_reader_rejects_invalid_file(tmp_path: Path):
    token_path = tmp_path / "device.token"
    token_path.write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid format"):
        CloudRobotSync._read_token(token_path)


def test_cloud_localization_normalizes_ros_field_name():
    assert CloudRobotSync._normal_localization(
        {"localization": {"state": "ok", "tf_age_s": 0.025}},
        {},
    ) == {"state": "ok", "tfAgeS": 0.025}


def test_cloud_map_counter_prefers_navigation_sequence_over_stable_hash():
    assert CloudRobotSync._normal_map_counter({
        "seq": 17, "map_revision": "rev-c8e4c785849b",
    }, 3) == 17
    assert CloudRobotSync._normal_map_counter({
        "map_revision": "rev-c8e4c785849b",
    }, 3) == 3


def test_cloud_normalizes_one_common_drive_mode_and_legacy_navigation():
    active = CloudRobotSync._normal_drive_mode({
        "drive_mode": {
            "mode": "patrol", "state": "active",
            "session_id": "patrol_session_1", "message": "patrolling",
        },
    }, {})
    assert active == {
        "mode": "patrol", "state": "active",
        "sessionId": "patrol_session_1", "message": "patrolling",
    }
    assert CloudRobotSync._normal_drive_mode({
        "drive_mode": {
            "mode": "roaming", "state": "active",
            "session_id": "roaming_session_1", "message": "roaming",
            "detail": {"candidate_count": 12},
        },
    }, {})["detail"] == {"candidate_count": 12}
    assert CloudRobotSync._normal_drive_mode({}, {
        "state": "driving", "session_id": "navigation_session_1",
    }) == {
        "mode": "destination", "state": "active",
        "sessionId": "navigation_session_1", "message": None,
    }
    assert CloudRobotSync._normal_drive_mode({}, {}) == {
        "mode": "idle", "state": "idle",
        "sessionId": None, "message": None,
    }


def test_cloud_remap_command_requests_supervised_runtime_switch(
    tmp_path: Path,
):
    sync = CloudRobotSync.__new__(CloudRobotSync)
    sync.runtime_request_file = tmp_path / "mode-request"
    sync._local_status = lambda: {"_runtime_mode": "navigation"}

    result = sync._local_command("start", {})

    assert result == {
        "accepted": True,
        "message": "지도 생성 모드로 전환합니다.",
        "_runtime_request": "mapping",
    }


def test_cloud_navigation_command_uses_saved_map_and_one_local_session(
    tmp_path: Path,
):
    revision = tmp_path / "versions" / "rev-1"
    revision.mkdir(parents=True)
    for name in ("map.yaml", "map.pgm", "user-map.geojson"):
        (revision / name).write_text("{}", encoding="utf-8")
    (tmp_path / "active.json").write_text(json.dumps({
        "format": "malbut-map-store/v1",
        "revision": "rev-1",
        "map_id": "map-home",
        "map_revision": "map-revision-home",
        "map_yaml": "versions/rev-1/map.yaml",
        "map_image": "versions/rev-1/map.pgm",
        "user_map": "versions/rev-1/user-map.geojson",
    }), encoding="utf-8")

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_GET(self):
            assert self.path == "/api/editor-config"
            payload = json.dumps({"csrf_token": "csrf-test"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=test-session; Path=/")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            calls.append((self.path, body, self.headers.get("Cookie")))
            response = (
                {"preview_token": "preview_token_123"}
                if self.path.endswith("preview")
                else {"session_id": "navigation_session_1"}
            )
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sync = CloudRobotSync.__new__(CloudRobotSync)
        sync.map_store = tmp_path
        sync.local_url = f"http://127.0.0.1:{server.server_port}/"
        sync.local_opener = build_opener(HTTPCookieProcessor(CookieJar()))
        preview = sync._local_command(
            "navigation_preview", {"x": 1.25, "y": -0.5}
        )
        assert preview["preview_token"] == "preview_token_123"
        sync._local_command(
            "navigation_start", {"previewToken": preview["preview_token"]}
        )
        sync._local_command("drive_mode_start", {"mode": "patrol"})
        sync._local_command("drive_mode_stop", {
            "mode": "patrol", "sessionId": "patrol_session_1",
        })
        zones = {
            "type": "FeatureCollection",
            "format": "malbut-semantic-zones-v1",
            "map_id": "map-home",
            "map_revision": "map-revision-home",
            "features": [],
        }
        sync._local_command("zones_apply", zones)
        assert calls == [
            (
                "/api/navigation/preview",
                {
                    "map_id": "map-home",
                    "map_revision": "map-revision-home",
                    "x": 1.25,
                    "y": -0.5,
                },
                "session=test-session",
            ),
            (
                "/api/navigation/start",
                {"preview_token": "preview_token_123"},
                "session=test-session",
            ),
            (
                "/api/drive-mode/start",
                {"mode": "patrol"},
                "session=test-session",
            ),
            (
                "/api/drive-mode/stop",
                {"mode": "patrol", "session_id": "patrol_session_1"},
                "session=test-session",
            ),
            (
                "/api/apply-zones",
                zones,
                "session=test-session",
            ),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cloud_command_preserves_structured_local_safety_error(
    tmp_path: Path,
):
    """AWS UI must show the local safety reason instead of a generic 409."""
    revision = tmp_path / "versions" / "rev-1"
    revision.mkdir(parents=True)
    for name in ("map.yaml", "map.pgm", "user-map.geojson"):
        (revision / name).write_text("{}", encoding="utf-8")
    (tmp_path / "active.json").write_text(json.dumps({
        "format": "malbut-map-store/v1",
        "revision": "rev-1",
        "map_id": "map-home",
        "map_revision": "revision-home",
        "map_yaml": "versions/rev-1/map.yaml",
        "map_image": "versions/rev-1/map.pgm",
        "user_map": "versions/rev-1/user-map.geojson",
    }), encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_GET(self):
            payload = json.dumps({"csrf_token": "csrf-test"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            payload = json.dumps({
                "error_code": "ROBOT_OUTSIDE_COSTMAP",
                "message": "로봇의 현재 위치가 주행 가능 공간이 아닙니다.",
            }).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sync = CloudRobotSync.__new__(CloudRobotSync)
        sync.map_store = tmp_path
        sync.local_url = f"http://127.0.0.1:{server.server_port}/"
        sync.local_opener = build_opener(HTTPCookieProcessor(CookieJar()))

        with pytest.raises(RuntimeError, match=(
            "ROBOT_OUTSIDE_COSTMAP: 로봇의 현재 위치가 "
            "주행 가능 공간이 아닙니다"
        )):
            sync._local_command(
                "navigation_preview", {"x": 1.0, "y": 2.0}
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_finalized_map_upload_uses_friendly_preview_and_semantics(
    tmp_path: Path,
):
    revision = tmp_path / "versions" / "rev-1"
    revision.mkdir(parents=True)
    (revision / "map.yaml").write_text("{}", encoding="utf-8")
    (revision / "map.pgm").write_bytes(b"P5\n1 1\n255\n\xff")
    (revision / "user-map.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}),
        encoding="utf-8",
    )
    friendly = b"\x89PNG\r\n\x1a\nFRIENDLY"
    (revision / "preview.png").write_bytes(friendly)
    zones = {
        "type": "FeatureCollection",
        "format": "malbut-semantic-zones-v1",
        "map_id": "map-home",
        "map_revision": "map-revision-home",
        "features": [],
    }
    (revision / "map-home-zones.geojson").write_text(
        json.dumps(zones), encoding="utf-8"
    )
    (tmp_path / "active.json").write_text(json.dumps({
        "format": "malbut-map-store/v1",
        "revision": "rev-1",
        "created_at": "2026-08-14T00:00:00+00:00",
        "map_id": "map-home",
        "map_revision": "map-revision-home",
        "map_yaml": "versions/rev-1/map.yaml",
        "map_image": "versions/rev-1/map.pgm",
        "user_map": "versions/rev-1/user-map.geojson",
        "preview": "versions/rev-1/preview.png",
    }), encoding="utf-8")
    cells = np.zeros((2, 2), dtype=np.int16)
    cells.setflags(write=False)
    grid = MapGrid(2, 2, 0.05, 0.0, 0.0, 0.0, cells)

    class _Now:
        nanoseconds = 10_000_000_000

    class _Clock:
        @staticmethod
        def now():
            return _Now()

    uploaded = []
    sync = CloudRobotSync.__new__(CloudRobotSync)
    sync.map_store = tmp_path
    sync.last_uploaded_map = ""
    sync.last_map_upload_monotonic = 0.0
    sync.get_clock = lambda: _Clock()
    sync._cloud_json = lambda path, method, payload: uploaded.append(
        (path, method, payload)
    ) or {}

    sync._upload_map_if_needed(
        grid, {"_runtime_mode": "navigation"}
    )

    payload = uploaded[0][2]
    assert payload["finalized"] is True
    assert base64.b64decode(payload["previewBase64"]) == friendly
    assert payload["userMap"]["type"] == "FeatureCollection"
    assert payload["semanticZones"] == zones
