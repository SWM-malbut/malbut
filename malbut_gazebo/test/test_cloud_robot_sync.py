from pathlib import Path
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.request import HTTPCookieProcessor, build_opener

import pytest

from malbut_gazebo.cloud_robot_sync import CloudRobotSync, TOKEN_PATTERN


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
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
