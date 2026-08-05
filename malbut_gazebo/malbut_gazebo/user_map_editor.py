#!/usr/bin/env python3
"""Serve the browser-based semantic zone editor from the package share."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
from threading import Lock
from urllib.parse import urlparse

from ament_index_python.packages import get_package_share_directory

from malbut_gazebo.room_editor import (
    merge_room_features,
    split_room_feature,
)
from malbut_gazebo.user_map_builder import load_slam_map
from malbut_gazebo.zone_filter_mask import (
    build_filter_mask,
    validate_zone_collection,
    write_filter_mask,
)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the Malbut semantic zone editor."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
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
    arguments = parser.parse_args()
    if not 0 < arguments.port < 65536:
        parser.error("--port must be between 1 and 65535")
    if arguments.map_path is not None:
        arguments.map_path = arguments.map_path.expanduser().resolve()
        if not arguments.map_path.is_file():
            parser.error(f"--map does not exist: {arguments.map_path}")
    if arguments.slam_map is not None:
        arguments.slam_map = arguments.slam_map.expanduser().resolve()
        if not arguments.slam_map.is_file():
            parser.error(f"--slam-map does not exist: {arguments.slam_map}")
        if arguments.map_path is None:
            parser.error("--slam-map requires --map")
    if arguments.zone_mask_output is not None:
        if arguments.slam_map is None:
            parser.error("--zone-mask-output requires --slam-map")
        arguments.zone_mask_output = (
            arguments.zone_mask_output.expanduser().resolve()
        )
    return arguments


def apply_zone_configuration(
    value: dict,
    expected_map_id: str,
    slam_map_path: Path,
    mask_output: Path,
    zone_output: Path,
) -> tuple[Path, Path, Path]:
    """Persist the current Zones and build their aligned Nav2 mask."""
    zones = validate_zone_collection(value, expected_map_id)
    slam_map = load_slam_map(slam_map_path, expected_map_id)
    mask = build_filter_mask(slam_map, zones)
    yaml_path, image_path = write_filter_mask(
        mask_output, mask, slam_map
    )
    zone_output.parent.mkdir(parents=True, exist_ok=True)
    zone_output.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return zone_output, yaml_path, image_path


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
    slam_map_path: Path | None = None
    zone_mask_output: Path | None = None
    zone_output: Path | None = None
    zone_reload_service = "/zone_filter_mask_server/load_map"
    zone_apply_lock = Lock()

    def do_GET(self) -> None:
        """Return the selected map at a stable editor-local URL."""
        if urlparse(self.path).path == "/api/editor-config":
            self._json_response(200, {
                "zone_apply_enabled": all((
                    self.map_id,
                    self.slam_map_path,
                    self.zone_mask_output,
                    self.zone_output,
                )),
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

    def _json_response(self, status: int, value: dict) -> None:
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        """Execute bounded Room geometry operations for the editor."""
        request_path = urlparse(self.path).path
        if request_path not in {
            "/api/split-room",
            "/api/merge-rooms",
            "/api/apply-zones",
        }:
            self._json_response(404, {"error": "unknown endpoint"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 5_000_000:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(content_length))
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
        except (
            json.JSONDecodeError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            self._json_response(422, {"error": str(error)})
            return
        self._json_response(200, response)


def main() -> int:
    """Serve only the installed static editor directory."""
    arguments = _parse_arguments()
    editor_root = Path(get_package_share_directory(
        "malbut_gazebo"
    )) / "web" / "semantic_zone_editor"
    if not editor_root.is_dir():
        print(f"ERROR: editor assets are missing: {editor_root}")
        return 1
    EditorRequestHandler.map_path = arguments.map_path
    EditorRequestHandler.zone_reload_service = arguments.zone_reload_service
    if arguments.map_path is not None:
        try:
            user_map = json.loads(arguments.map_path.read_text(
                encoding="utf-8"
            ))
            EditorRequestHandler.map_id = user_map["map_id"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError):
            print(f"ERROR: invalid User Map: {arguments.map_path}")
            return 1
    EditorRequestHandler.slam_map_path = arguments.slam_map
    if arguments.slam_map is not None:
        EditorRequestHandler.zone_mask_output = (
            arguments.zone_mask_output
            or arguments.map_path.with_name("zone-filter.yaml")
        )
        EditorRequestHandler.zone_output = arguments.map_path.with_name(
            f"{EditorRequestHandler.map_id}-zones.geojson"
        )
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
