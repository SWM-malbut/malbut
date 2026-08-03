#!/usr/bin/env python3
"""Serve the browser-based semantic zone editor from the package share."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlparse

from ament_index_python.packages import get_package_share_directory

from malbut_gazebo.room_editor import (
    merge_room_features,
    split_room_feature,
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
    arguments = parser.parse_args()
    if not 0 < arguments.port < 65536:
        parser.error("--port must be between 1 and 65535")
    if arguments.map_path is not None:
        arguments.map_path = arguments.map_path.expanduser().resolve()
        if not arguments.map_path.is_file():
            parser.error(f"--map does not exist: {arguments.map_path}")
    return arguments


class EditorRequestHandler(SimpleHTTPRequestHandler):
    """Serve editor assets and, optionally, one selected User Map."""

    map_path: Path | None = None

    def do_GET(self) -> None:
        """Return the selected map at a stable editor-local URL."""
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
        }:
            self._json_response(404, {"error": "unknown endpoint"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 5_000_000:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(content_length))
            resolution = float(request.get("resolution", 0.05))
            if request_path == "/api/split-room":
                response = {"rooms": split_room_feature(
                    request["room"],
                    request["line"],
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
