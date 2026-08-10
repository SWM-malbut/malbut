"""Tests for the local semantic editor HTTP security boundary."""

from functools import partial
import http.client
from http.server import ThreadingHTTPServer
import json
from threading import Thread

import pytest

from malbut_gazebo.user_map_editor import EditorRequestHandler


def _room() -> dict:
    return {
        "type": "Feature",
        "id": "room-1",
        "properties": {
            "role": "room",
            "room_id": "room-1",
            "name": "거실",
            "category": "living_room",
            "centroid": [2.5, 2.5],
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [0.0, 0.0], [5.0, 0.0], [5.0, 5.0],
                [0.0, 5.0], [0.0, 0.0],
            ]],
        },
    }


def _user_map() -> dict:
    room = _room()
    return {
        "type": "FeatureCollection",
        "format": "malbut-user-map-v1",
        "map_id": "home",
        "map_revision": "rev-current",
        "frame_id": "map",
        "source": {"resolution": 0.05},
        "room_segmentation": {"room_count": 1},
        "features": [
            {
                "type": "Feature",
                "id": "walkable-area",
                "properties": {"role": "walkable_area"},
                "geometry": room["geometry"],
            },
            room,
        ],
    }


class QuietEditorHandler(EditorRequestHandler):
    """Run the production handler without test log noise."""

    def log_message(self, _format, *_arguments) -> None:
        """Discard one request log line."""


@pytest.fixture
def editor_server(tmp_path):
    """Serve one temporary User Map and return its address and path."""
    map_path = tmp_path / "user-map.geojson"
    map_path.write_text(json.dumps(_user_map()), encoding="utf-8")
    QuietEditorHandler.map_path = map_path
    QuietEditorHandler.map_id = "home"
    QuietEditorHandler.map_revision = "rev-current"
    QuietEditorHandler.accepted_map_ids = ("legacy-home",)
    QuietEditorHandler.allowed_hosts = {"127.0.0.1", "localhost"}
    QuietEditorHandler.slam_map_path = None
    QuietEditorHandler.zone_mask_output = None
    QuietEditorHandler.zone_output = None
    handler = partial(QuietEditorHandler, directory=tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address, map_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
    status, headers, value = _request(address, "GET", "/api/editor-config")
    assert status == 200
    return headers["Set-Cookie"].split(";", 1)[0], value["csrf_token"]


def _authorized_headers(address, cookie, token):
    host = f"{address[0]}:{address[1]}"
    return {
        "Content-Type": "application/json",
        "Cookie": cookie,
        "X-CSRF-Token": token,
        "Origin": f"http://{host}",
    }


def test_editor_rejects_cross_origin_host_content_type_and_missing_csrf(
    editor_server,
):
    """Every mutation must cross all same-origin and CSRF gates."""
    address, _ = editor_server
    cookie, token = _session(address)
    valid = _authorized_headers(address, cookie, token)

    status, _, _ = _request(
        address,
        "POST",
        "/api/split-room",
        b"{}",
        {**valid, "Origin": "http://evil.example"},
    )
    assert status == 403
    status, _, _ = _request(
        address,
        "POST",
        "/api/split-room",
        b"{}",
        {**valid, "Host": "attacker.example"},
    )
    assert status == 403
    status, _, _ = _request(
        address,
        "POST",
        "/api/split-room",
        b"{}",
        {**valid, "Content-Type": "text/plain"},
    )
    assert status == 415
    status, _, _ = _request(
        address,
        "POST",
        "/api/split-room",
        b"{}",
        {name: value for name, value in valid.items()
         if name != "X-CSRF-Token"},
    )
    assert status == 403
    status, _, _ = _request(
        address, "OPTIONS", "/api/apply-zones"
    )
    assert status == 403


def test_editor_returns_413_after_consuming_a_common_oversized_body(
    editor_server,
):
    """A modest oversized upload must receive HTTP 413, not Broken pipe."""
    address, _ = editor_server
    cookie, token = _session(address)
    body = b"{" + b" " * 5_000_000

    status, _, value = _request(
        address,
        "POST",
        "/api/split-room",
        body,
        _authorized_headers(address, cookie, token),
    )

    assert status == 413
    assert value["error"] == "request body exceeds 5 MB"


def test_editor_persists_normalized_rooms_with_security_headers(editor_server):
    """Room edits must survive outside localStorage with safe target metadata."""
    address, map_path = editor_server
    cookie, token = _session(address)
    request = {
        "map_id": "legacy-home",
        "map_revision": "rev-current",
        "resolution": 0.05,
        "rooms": [_room()],
    }

    status, headers, value = _request(
        address,
        "POST",
        "/api/rooms",
        json.dumps(request).encode("utf-8"),
        _authorized_headers(address, cookie, token),
    )

    assert status == 200
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "Python" not in headers["Server"]
    room = value["rooms"][0]
    assert room["properties"]["representative_point"] == pytest.approx(
        [2.5, 2.5], abs=0.05
    )
    assert room["properties"]["clearance_m"] > 2.0
    stored = json.loads(map_path.read_text(encoding="utf-8"))
    assert stored["map_id"] == "home"
    assert stored["map_revision"] == "rev-current"
    assert stored["features"][-1] == room
