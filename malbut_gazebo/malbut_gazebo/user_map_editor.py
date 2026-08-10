#!/usr/bin/env python3
"""Serve the browser-based semantic zone editor from the package share."""

from __future__ import annotations

import argparse
from copy import deepcopy
from functools import partial
import hashlib
import hmac
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from threading import Lock
from urllib.parse import urlparse

from ament_index_python.packages import get_package_share_directory

from malbut_gazebo.room_editor import (
    merge_room_features,
    normalize_room_feature,
    split_room_feature,
)
from malbut_gazebo.user_map_builder import load_slam_map
from malbut_gazebo.zone_filter_mask import (
    build_filter_mask,
    validate_zone_collection,
    write_filter_mask,
)


MAX_REQUEST_BYTES = 5_000_000
MAX_REJECT_DRAIN_BYTES = 8_000_000
SESSION_COOKIE = "malbut_editor_session"


class RequestError(ValueError):
    """Describe one HTTP request rejection with its response status."""

    def __init__(self, status: int, message: str) -> None:
        """Store an HTTP status and safe client-facing message."""
        super().__init__(message)
        self.status = status


def parse_editor_arguments(
    arguments: list[str] | None = None,
) -> argparse.Namespace:
    """Parse the HTTP editor arguments for editor and robot servers."""
    parser = argparse.ArgumentParser(
        description="Serve the Malbut semantic zone editor."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="additional HTTP Host name accepted by the editor",
    )
    parser.add_argument(
        "--map",
        dest="map_path",
        type=Path,
        help="User Map GeoJSON to open when the editor loads.",
    )
    parser.add_argument(
        "--slam-map",
        type=Path,
        help="Source SLAM map YAML used to apply Zones to Nav2.",
    )
    parser.add_argument(
        "--zone-mask-output",
        type=Path,
        help="Output Nav2 Zone mask YAML; defaults beside the User Map.",
    )
    parser.add_argument(
        "--zone-reload-service",
        default="/zone_filter_mask_server/load_map",
        help="Running Nav2 filter map service to reload after applying.",
    )
    parsed = parser.parse_args(arguments)
    if not 0 < parsed.port < 65536:
        parser.error("--port must be between 1 and 65535")
    if parsed.map_path is not None:
        parsed.map_path = parsed.map_path.expanduser().resolve()
        if not parsed.map_path.is_file():
            parser.error(f"--map does not exist: {parsed.map_path}")
    if parsed.slam_map is not None:
        parsed.slam_map = parsed.slam_map.expanduser().resolve()
        if not parsed.slam_map.is_file():
            parser.error(f"--slam-map does not exist: {parsed.slam_map}")
        if parsed.map_path is None:
            parser.error("--slam-map requires --map")
    if parsed.zone_mask_output is not None:
        if parsed.slam_map is None:
            parser.error("--zone-mask-output requires --slam-map")
        parsed.zone_mask_output = (
            parsed.zone_mask_output.expanduser().resolve()
        )
    return parsed


def apply_zone_configuration(
    value: dict,
    expected_map_id: str,
    slam_map_path: Path,
    mask_output: Path,
    zone_output: Path,
    expected_map_revision: str = "",
    accepted_map_ids: tuple[str, ...] = (),
) -> tuple[Path, Path, Path]:
    """Persist the current Zones and build their aligned Nav2 mask."""
    slam_map = load_slam_map(slam_map_path, expected_map_id)
    revision = expected_map_revision or slam_map.map_revision
    zones = validate_zone_collection(
        value,
        expected_map_id,
        revision,
        accepted_map_ids,
    )
    mask = build_filter_mask(slam_map, zones)
    yaml_path, image_path = write_filter_mask(
        mask_output, mask, slam_map
    )
    canonical = deepcopy(value)
    canonical["map_id"] = expected_map_id
    canonical["map_revision"] = revision
    _write_json_atomic(zone_output, canonical)
    return zone_output, yaml_path, image_path


def _write_json_atomic(path: Path, value: dict) -> None:
    """Replace a JSON file only after its complete payload is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def persist_room_configuration(
    value: dict,
    expected_map_id: str,
    map_path: Path,
    expected_map_revision: str = "",
    accepted_map_ids: tuple[str, ...] = (),
    resolution: float = 0.05,
) -> list[dict]:
    """Validate and atomically persist browser-edited Room Features."""
    if not isinstance(value, dict):
        raise ValueError("Room request must contain an object")
    if value.get("map_id") not in {expected_map_id, *accepted_map_ids}:
        raise ValueError("Room map_id does not match the User Map")
    revision = value.get("map_revision")
    if (
        revision is not None
        and expected_map_revision
        and revision != expected_map_revision
    ):
        raise ValueError("Room map_revision does not match the User Map")
    rooms = value.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        raise ValueError("Room request must contain a non-empty rooms array")
    if len(rooms) > 512:
        raise ValueError("Room request contains too many Rooms")
    normalized = [
        normalize_room_feature(room, resolution) for room in rooms
    ]
    room_ids = [room["id"] for room in normalized]
    if len(room_ids) != len(set(room_ids)):
        raise ValueError("Room IDs must be unique")
    try:
        user_map = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read User Map: {error}") from error
    if user_map.get("map_id") not in {expected_map_id, *accepted_map_ids}:
        raise ValueError("stored User Map identity changed")
    features = user_map.get("features")
    if not isinstance(features, list):
        raise ValueError("stored User Map features must be an array")
    user_map["map_id"] = expected_map_id
    if expected_map_revision:
        user_map["map_revision"] = expected_map_revision
    user_map["features"] = [
        feature
        for feature in features
        if feature.get("properties", {}).get("role") != "room"
    ] + normalized
    segmentation = user_map.setdefault("room_segmentation", {})
    segmentation.update({
        "method": segmentation.get("method", "user_edited"),
        "room_count": len(normalized),
        "edited": True,
    })
    _write_json_atomic(map_path, user_map)
    return normalized


def reload_nav2_filter(service_name: str, mask_yaml: Path) -> bool:
    """Reload a running Nav2 mask server, returning False when unavailable."""
    request = json.dumps({"map_url": str(mask_yaml)})
    try:
        result = subprocess.run(
            [
                "ros2", "service", "call", service_name,
                "nav2_msgs/srv/LoadMap", request,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = result.stdout + result.stderr
    return result.returncode == 0 and "result=0" in output


class EditorRequestHandler(SimpleHTTPRequestHandler):
    """Serve editor assets and, optionally, one selected User Map."""

    map_path: Path | None = None
    map_id: str | None = None
    map_revision = ""
    accepted_map_ids: tuple[str, ...] = ()
    slam_map_path: Path | None = None
    zone_mask_output: Path | None = None
    zone_output: Path | None = None
    zone_reload_service = "/zone_filter_mask_server/load_map"
    zone_apply_lock = Lock()
    room_write_lock = Lock()
    session_secret = secrets.token_bytes(32)
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    server_version = "MalbutEditor"
    sys_version = ""

    def end_headers(self) -> None:
        """Add browser hardening headers to every editor response."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        try:
            hostname = urlparse(f"//{host}").hostname
        except ValueError:
            return False
        return bool(
            hostname and hostname.lower() in self.allowed_hosts
        )

    def _session_id(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel is not None else ""

    def _csrf_token(self, session_id: str) -> str:
        return hmac.new(
            self.session_secret,
            session_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _validate_post_headers(self) -> None:
        if not self._host_allowed():
            raise RequestError(403, "request Host is not allowed")
        host = self.headers.get("Host", "").lower()
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.netloc.lower() != host
            ):
                raise RequestError(403, "request Origin is not allowed")
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise RequestError(415, "Content-Type must be application/json")
        session_id = self._session_id()
        provided_token = self.headers.get("X-CSRF-Token", "")
        if (
            not session_id
            or not provided_token
            or not hmac.compare_digest(
                provided_token, self._csrf_token(session_id)
            )
        ):
            raise RequestError(403, "valid CSRF token is required")

    def _read_json_request(self) -> dict:
        self._validate_post_headers()
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise RequestError(400, "invalid Content-Length") from error
        if content_length <= 0:
            raise RequestError(400, "request body is required")
        if content_length > MAX_REQUEST_BYTES:
            remaining = min(content_length, MAX_REJECT_DRAIN_BYTES)
            while remaining > 0:
                chunk = self.rfile.read(min(65_536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            if content_length > MAX_REJECT_DRAIN_BYTES:
                self.close_connection = True
            raise RequestError(413, "request body exceeds 5 MB")
        try:
            value = json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError as error:
            raise RequestError(422, str(error)) from error
        if not isinstance(value, dict):
            raise RequestError(422, "JSON request must contain an object")
        return value

    def do_GET(self) -> None:
        """Return the selected map at a stable editor-local URL."""
        if not self._host_allowed():
            self._json_response(403, {"error": "request Host is not allowed"})
            return
        if urlparse(self.path).path == "/api/editor-config":
            session_id = self._session_id() or secrets.token_urlsafe(24)
            self._json_response(200, self.editor_config(session_id), {
                "Set-Cookie": (
                    f"{SESSION_COOKIE}={session_id}; Path=/; "
                    "HttpOnly; SameSite=Strict"
                ),
                "Cache-Control": "no-store",
            })
            return
        if (
            urlparse(self.path).path == "/user-map.geojson"
            and self.map_path is not None
        ):
            payload = self.map_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/geo+json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def editor_config(self, session_id: str) -> dict:
        """Return capabilities exposed by this server implementation."""
        return {
            "map_id": self.map_id,
            "map_revision": self.map_revision,
            "zone_apply_enabled": all((
                self.map_id,
                self.slam_map_path,
                self.zone_mask_output,
                self.zone_output,
            )),
            "room_persistence_enabled": self.map_path is not None,
            "robot_stream_enabled": False,
            "navigation_enabled": False,
            "csrf_token": self._csrf_token(session_id),
        }

    def _json_response(
        self,
        status: int,
        value: dict,
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:
        """Reject cross-origin preflight; the editor is same-origin only."""
        self._json_response(
            403, {"error": "cross-origin requests are disabled"}
        )

    def do_POST(self) -> None:
        """Execute bounded Room geometry operations for the editor."""
        request_path = urlparse(self.path).path
        if request_path not in {
            "/api/split-room",
            "/api/merge-rooms",
            "/api/apply-zones",
            "/api/rooms",
        }:
            self._json_response(404, {"error": "unknown endpoint"})
            return
        try:
            request = self._read_json_request()
            if request_path == "/api/apply-zones":
                if not all((
                    self.map_id,
                    self.slam_map_path,
                    self.zone_mask_output,
                    self.zone_output,
                )):
                    raise ValueError(
                        "Zone 적용 서버가 SLAM 지도와 함께 실행되지 않았습니다."
                    )
                with self.zone_apply_lock:
                    zone_path, yaml_path, image_path = (
                        apply_zone_configuration(
                            request,
                            self.map_id,
                            self.slam_map_path,
                            self.zone_mask_output,
                            self.zone_output,
                            self.map_revision,
                            self.accepted_map_ids,
                        )
                    )
                    reloaded = reload_nav2_filter(
                        self.zone_reload_service, yaml_path
                    )
                self._json_response(200, {
                    "zones_geojson": str(zone_path),
                    "mask_yaml": str(yaml_path),
                    "mask_image": str(image_path),
                    "nav2_reloaded": reloaded,
                })
                return
            if request_path == "/api/rooms":
                if self.map_id is None or self.map_path is None:
                    raise ValueError("Room 저장 서버가 지도와 함께 실행되지 않았습니다.")
                with self.room_write_lock:
                    rooms = persist_room_configuration(
                        request,
                        self.map_id,
                        self.map_path,
                        self.map_revision,
                        self.accepted_map_ids,
                        float(request.get("resolution", 0.05)),
                    )
                self._json_response(200, {"rooms": rooms})
                return
            resolution = float(request.get("resolution", 0.05))
            if request_path == "/api/split-room":
                response = {"rooms": split_room_feature(
                    request["room"],
                    request.get("lines", request.get("line")),
                    resolution=resolution,
                    minimum_room_area=float(
                        request.get("minimum_room_area", 1.0)
                    ),
                )}
            else:
                response = {"room": merge_room_features(
                    request["rooms"],
                    resolution=resolution,
                )}
        except RequestError as error:
            self._json_response(error.status, {"error": str(error)})
            return
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            self._json_response(422, {"error": str(error)})
            return
        self._json_response(200, response)


def configure_editor_handler(
    handler_type: type[EditorRequestHandler],
    arguments: argparse.Namespace,
) -> bool:
    """Configure one request-handler class from validated CLI arguments."""
    handler_type.map_path = arguments.map_path
    handler_type.zone_reload_service = arguments.zone_reload_service
    allowed_hosts = {
        arguments.host.strip("[]").lower(),
        *(host.strip("[]").lower() for host in arguments.allowed_host),
    }
    if arguments.host in {"127.0.0.1", "::1", "localhost"}:
        allowed_hosts.update({"127.0.0.1", "localhost", "::1"})
    handler_type.allowed_hosts = allowed_hosts
    if arguments.map_path is not None:
        try:
            user_map = json.loads(arguments.map_path.read_text(
                encoding="utf-8"
            ))
            handler_type.map_id = user_map["map_id"]
            handler_type.map_revision = str(
                user_map.get("map_revision", "")
            )
            handler_type.accepted_map_ids = tuple(
                identity
                for identity in user_map.get("legacy_map_ids", [])
                if isinstance(identity, str)
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
            print(f"ERROR: invalid User Map: {arguments.map_path}")
            return False
    handler_type.slam_map_path = arguments.slam_map
    if arguments.slam_map is not None:
        try:
            slam_map = load_slam_map(
                arguments.slam_map, handler_type.map_id
            )
        except ValueError as error:
            print(f"ERROR: invalid SLAM map: {error}")
            return False
        if (
            handler_type.map_revision
            and handler_type.map_revision != slam_map.map_revision
        ):
            print("ERROR: User Map revision does not match the SLAM map")
            return False
        handler_type.map_revision = slam_map.map_revision
        handler_type.accepted_map_ids = tuple(dict.fromkeys((
            *handler_type.accepted_map_ids,
            *slam_map.legacy_map_ids,
        )))
        handler_type.zone_mask_output = (
            arguments.zone_mask_output
            or arguments.map_path.with_name("zone-filter.yaml")
        )
        handler_type.zone_output = arguments.map_path.with_name(
            f"{handler_type.map_id}-zones.geojson"
        )
    return True


def editor_asset_root() -> Path:
    """Return the installed semantic-editor asset directory."""
    return Path(get_package_share_directory(
        "malbut_gazebo"
    )) / "web" / "semantic_zone_editor"


def main() -> int:
    """Serve only the installed static editor directory."""
    arguments = parse_editor_arguments()
    editor_root = editor_asset_root()
    if not editor_root.is_dir():
        print(f"ERROR: editor assets are missing: {editor_root}")
        return 1
    if not configure_editor_handler(EditorRequestHandler, arguments):
        return 1
    handler = partial(EditorRequestHandler, directory=editor_root)
    server = ThreadingHTTPServer((arguments.host, arguments.port), handler)
    url = f"http://{arguments.host}:{arguments.port}/"
    if arguments.map_path is not None:
        url += "?map=user-map.geojson"
    print(f"Semantic Zone Editor: {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
