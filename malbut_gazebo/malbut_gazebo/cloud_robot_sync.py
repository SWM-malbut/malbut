"""Synchronize one robot map lifecycle with the authenticated cloud service."""

from __future__ import annotations

import base64
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from http.cookiejar import CookieJar
import json
import math
from pathlib import Path
import re
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
    urlopen,
)

from nav_msgs.msg import OccupancyGrid
import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
import tf2_ros

from malbut_gazebo.map_lifecycle import (
    MapGrid,
    load_active_revision,
    map_grid_from_message,
    render_map_png,
)


TOKEN_PATTERN = re.compile(
    r"^hc1\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\.[0-9a-f]{64}$",
    re.IGNORECASE,
)


class CloudRobotSync(Node):
    """Publish map/pose snapshots and execute authenticated cloud commands."""

    def __init__(self) -> None:
        super().__init__(
            "cloud_robot_sync",
            automatically_declare_parameters_from_overrides=True,
        )
        self.backend_url = self._parameter("backend_url").rstrip("/") + "/"
        self.device_id = self._parameter("device_id")
        self.token_file = Path(self._parameter("token_file")).expanduser()
        self.map_store = Path(
            self._parameter("map_store")
        ).expanduser().resolve()
        self.local_url = self._parameter("local_url").rstrip("/") + "/"
        self.token = self._read_token(self.token_file)
        self.lock = Lock()
        self.grid: MapGrid | None = None
        self.map_counter = 0
        self.pose: dict | None = None
        self.localization = {"state": "uninitialized", "tfAgeS": None}
        self.last_uploaded_map = ""
        self.last_map_upload_monotonic = 0.0
        self.last_warning = ""
        self.last_warning_at = 0.0
        self.worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cloud-robot-sync"
        )
        self.pending: Future | None = None
        self.local_opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.tf_buffer = tf2_ros.Buffer(node=self)
        self.tf_listener = tf2_ros.TransformListener(
            self.tf_buffer, self, spin_thread=False
        )
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(OccupancyGrid, "/map", self._receive_map, qos)
        self.create_timer(0.2, self._refresh_pose)
        self.create_timer(1.0, self._schedule_sync)
        self.get_logger().info(
            f"Cloud robot sync enabled for device {self.device_id}"
        )

    def _parameter(self, name: str) -> str:
        value = str(self.get_parameter(name).value).strip()
        if not value:
            raise ValueError(f"{name} is required")
        return value

    @staticmethod
    def _read_token(path: Path) -> str:
        if path.is_symlink() or not path.is_file():
            raise ValueError("device token file must be a regular file")
        token = path.read_text(encoding="utf-8").strip()
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("device token file has an invalid format")
        return token

    def _receive_map(self, message: OccupancyGrid) -> None:
        try:
            grid = map_grid_from_message(message)
        except ValueError as error:
            self._warn(str(error))
            return
        with self.lock:
            self.grid = grid
            self.map_counter += 1

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
                self.localization = {"state": "lost", "tfAgeS": None}
            return
        rotation = transform.transform.rotation
        yaw = math.atan2(
            2.0 * (rotation.w * rotation.z + rotation.x * rotation.y),
            1.0 - 2.0 * (rotation.y * rotation.y + rotation.z * rotation.z),
        )
        stamp = transform.header.stamp
        stamp_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
        age = (self.get_clock().now().nanoseconds - stamp_ns) / 1e9
        state = "ok" if -2.0 <= age <= 1.0 else "stale"
        with self.lock:
            self.pose = None if state != "ok" else {
                "x": round(float(transform.transform.translation.x), 4),
                "y": round(float(transform.transform.translation.y), 4),
                "yaw": round(yaw, 5),
            }
            self.localization = {"state": state, "tfAgeS": round(age, 3)}

    def _schedule_sync(self) -> None:
        if self.pending is not None and not self.pending.done():
            return
        if self.pending is not None:
            try:
                self.pending.result()
            except Exception as error:  # pragma: no cover - executor boundary
                self._warn(f"cloud sync failed: {error}")
        with self.lock:
            grid = self.grid
            counter = self.map_counter
            pose = None if self.pose is None else dict(self.pose)
            localization = dict(self.localization)
        self.pending = self.worker.submit(
            self._sync_once, grid, counter, pose, localization
        )

    def _sync_once(
        self,
        grid: MapGrid | None,
        counter: int,
        pose: dict | None,
        localization: dict,
    ) -> None:
        status = self._local_status()
        default_state = (
            "ready" if load_active_revision(self.map_store) else "idle"
        )
        navigation = status.get("navigation")
        navigation = navigation if isinstance(navigation, dict) else {}
        nav2 = status.get("nav2", {})
        nav2 = dict(nav2) if isinstance(nav2, dict) else {}
        nav2["runtime_mode"] = str(
            status.get("_runtime_mode", "unavailable")
        )
        message = (
            status.get("message")
            or navigation.get("message")
            or "말벗 지도를 동기화하고 있습니다."
        )
        state_payload = {
            "state": str(status.get("state", default_state)),
            "message": str(message),
            "pose": status.get("pose", pose),
            "localization": self._normal_localization(status, localization),
            "nav2": nav2,
            "target": status.get("target", navigation or None),
            "mapRevision": int(status.get("map_revision", counter)),
            "observedAt": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        }
        self._cloud_json("api/device/v1/robot/state", "POST", state_payload)
        if grid is not None:
            self._upload_map_if_needed(grid, status)
        self._run_commands()

    @staticmethod
    def _normal_localization(status: dict, fallback: dict) -> dict:
        value = status.get("localization", fallback)
        if not isinstance(value, dict):
            value = fallback
        tf_age = value.get("tf_age_s", value.get("tfAgeS"))
        return {
            "state": str(value.get("state", "uninitialized")),
            "tfAgeS": (
                float(tf_age)
                if isinstance(tf_age, (int, float)) else None
            ),
        }

    def _upload_map_if_needed(self, grid: MapGrid, status: dict) -> None:
        active = load_active_revision(self.map_store)
        user_map_bytes = b""
        if active:
            try:
                user_map_bytes = (
                    self.map_store / active["user_map"]
                ).resolve().read_bytes()
            except (KeyError, OSError):
                user_map_bytes = b""
        metadata = (
            f"{grid.width}:{grid.height}:{grid.resolution}:"
            f"{grid.origin_x}:{grid.origin_y}"
        ).encode("ascii")
        fingerprint = hashlib.sha256(
            grid.cells.tobytes() + metadata + user_map_bytes
        ).hexdigest()
        now = self.get_clock().now().nanoseconds / 1e9
        if fingerprint == self.last_uploaded_map:
            return
        if now - self.last_map_upload_monotonic < 5.0:
            return
        finalized = active if status.get("state") == "ready" else None
        user_map = None
        if finalized:
            try:
                user_map = json.loads(user_map_bytes.decode("utf-8"))
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                user_map = None
        payload = {
            "revision": str(
                finalized.get("revision")
                if finalized else f"live-{fingerprint[:24]}"
            ),
            "mapId": str(
                finalized.get("map_id")
                if finalized else f"live-{fingerprint[:16]}"
            ),
            "mapRevision": str(
                finalized.get("map_revision")
                if finalized else f"live-{fingerprint[:20]}"
            ),
            "sourceCreatedAt": (
                finalized.get("created_at") if finalized else None
            ),
            "geometry": {
                "width": grid.width,
                "height": grid.height,
                "resolution": grid.resolution,
                "originX": grid.origin_x,
                "originY": grid.origin_y,
                "originYaw": grid.origin_yaw,
            },
            "previewBase64": base64.b64encode(
                render_map_png(grid)
            ).decode("ascii"),
            "userMap": user_map,
        }
        self._cloud_json("api/device/v1/robot/map", "PUT", payload)
        self.last_uploaded_map = fingerprint
        self.last_map_upload_monotonic = now

    def _run_commands(self) -> None:
        response = self._cloud_json(
            "api/device/v1/robot/commands", "GET", None
        )
        commands = (
            response.get("commands", [])
            if isinstance(response, dict) else []
        )
        if not isinstance(commands, list):
            return
        for command in commands:
            if not isinstance(command, dict):
                continue
            command_id = command.get("id")
            operation = command.get("operation")
            payload = command.get("payload", {})
            if (
                not isinstance(command_id, str)
                or operation not in {
                    "start", "finish", "cancel",
                    "navigation_preview", "navigation_start",
                    "navigation_cancel",
                }
                or not isinstance(payload, dict)
            ):
                continue
            ok = False
            result: object
            try:
                result = self._local_command(operation, payload)
                ok = True
            except Exception as error:
                result = {"error": str(error)[:512]}
            self._cloud_json(
                "api/device/v1/robot/commands/"
                + quote(command_id, safe="") + "/complete",
                "POST",
                {"ok": ok, "result": result},
            )

    def _local_status(self) -> dict:
        for path, runtime_mode in (
            ("api/mapping/status", "mapping"),
            ("api/robot/status", "navigation"),
        ):
            try:
                with urlopen(
                    urljoin(self.local_url, path), timeout=2.0
                ) as response:
                    value = json.loads(response.read())
                    if isinstance(value, dict):
                        value["_runtime_mode"] = runtime_mode
                        return value
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
                continue
        return {}

    def _local_command(self, operation: str, payload: dict) -> dict:
        config_request = Request(
            urljoin(self.local_url, "api/editor-config"),
            headers={"Accept": "application/json"},
        )
        with self.local_opener.open(config_request, timeout=3.0) as response:
            config = json.loads(response.read())
        csrf = config.get("csrf_token") if isinstance(config, dict) else None
        if not isinstance(csrf, str) or not csrf:
            raise RuntimeError("local mapping CSRF session unavailable")
        if operation in {"start", "finish", "cancel"}:
            path = f"api/mapping/{operation}"
            body = {"replace": True} if operation == "start" else {}
        else:
            active = load_active_revision(self.map_store)
            if not active:
                raise RuntimeError("saved map is unavailable")
            if operation == "navigation_preview":
                path = "api/navigation/preview"
                body = {
                    "map_id": active.get("map_id"),
                    "map_revision": active.get("map_revision", ""),
                    "x": payload.get("x"),
                    "y": payload.get("y"),
                }
            elif operation == "navigation_start":
                path = "api/navigation/start"
                body = {"preview_token": payload.get("previewToken")}
            else:
                path = "api/navigation/cancel"
                body = {"session_id": payload.get("sessionId")}
        request = Request(
            urljoin(self.local_url, path),
            method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": self.local_url.rstrip("/"),
                "X-CSRF-Token": csrf,
            },
        )
        try:
            with self.local_opener.open(request, timeout=8.0) as response:
                value = json.loads(response.read())
                return value if isinstance(value, dict) else {"accepted": True}
        except HTTPError as error:
            try:
                value = json.loads(error.read())
                message = (
                    value.get("error") if isinstance(value, dict) else None
                )
            except (json.JSONDecodeError, OSError):
                message = None
            raise RuntimeError(
                message or f"local mapping command failed ({error.code})"
            ) from error

    def _cloud_json(
        self, path: str, method: str, payload: dict | None
    ) -> dict:
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "malbut-cloud-robot-sync/1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            urljoin(self.backend_url, path),
            method=method,
            data=data,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=8.0) as response:
                value = json.loads(response.read())
                return value if isinstance(value, dict) else {}
        except HTTPError as error:
            raise RuntimeError(f"cloud returned HTTP {error.code}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"cloud request failed: {type(error).__name__}"
            ) from error

    def _warn(self, message: str) -> None:
        now = self.get_clock().now().nanoseconds / 1e9
        if message != self.last_warning or now - self.last_warning_at >= 30.0:
            self.get_logger().warning(message)
            self.last_warning = message
            self.last_warning_at = now

    def destroy_node(self) -> bool:
        self.worker.shutdown(wait=False, cancel_futures=True)
        self.token = ""
        return super().destroy_node()


def main() -> int:
    rclpy.init()
    node = CloudRobotSync()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
