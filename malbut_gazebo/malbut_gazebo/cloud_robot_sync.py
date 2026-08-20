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
from std_msgs.msg import String
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
from malbut_gazebo.pose_checkpoint import VALIDATION_TOPIC
from malbut_gazebo.runtime_control import write_runtime_request


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
        runtime_request_file = self._optional_parameter(
            "runtime_request_file"
        )
        self.runtime_request_file = (
            Path(runtime_request_file).expanduser().resolve()
            if runtime_request_file else None
        )
        self.token = self._read_token(self.token_file)
        self.lock = Lock()
        self.grid: MapGrid | None = None
        self.map_counter = 0
        self.pose: dict | None = None
        initial_validation = (
            str(self.get_parameter("boot_validation_state").value).strip()
            if self.has_parameter("boot_validation_state")
            else "revalidation_required"
        )
        self.validation_state = (
            initial_validation
            if initial_validation in {"revalidation_required", "verifying"}
            else "ok"
        )
        self.localization = {
            "state": self.validation_state, "tfAgeS": None
        }
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
        self.create_subscription(
            String,
            VALIDATION_TOPIC,
            self._receive_validation,
            qos,
        )
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

    def _optional_parameter(self, name: str) -> str:
        parameter = self.get_parameter(name)
        value = parameter.value
        return "" if value is None else str(value).strip()

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

    def _receive_validation(self, message: String) -> None:
        value = message.data.strip()
        if value not in {"revalidation_required", "verifying", "ok"}:
            return
        with self.lock:
            self.validation_state = value

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
                state = (
                    self.validation_state
                    if self.validation_state != "ok" else "lost"
                )
                self.localization = {"state": state, "tfAgeS": None}
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
        if self.validation_state != "ok":
            state = self.validation_state
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
        target = status.get("target")
        if isinstance(target, dict):
            target = dict(target)
            path = status.get("path")
            if isinstance(path, dict):
                target["path"] = path
            frontier_count = status.get("frontier_count")
            if isinstance(frontier_count, int) and frontier_count >= 0:
                target["frontier_count"] = frontier_count
        elif navigation:
            target = navigation
        else:
            target = None
        state_payload = {
            "state": str(status.get("state", default_state)),
            "message": str(message),
            "pose": status.get("pose", pose),
            "localization": self._normal_localization(status, localization),
            "nav2": nav2,
            "target": target,
            "driveMode": self._normal_drive_mode(status, navigation),
            "mapRevision": self._normal_map_counter(status, counter),
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

    @staticmethod
    def _normal_map_counter(status: dict, fallback: int) -> int:
        """Return the numeric cloud revision, never the stable map hash."""
        for key in ("seq", "map_revision"):
            value = status.get(key)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 9_007_199_254_740_991
            ):
                return value
        return max(0, int(fallback))

    @staticmethod
    def _normal_drive_mode(status: dict, navigation: dict) -> dict:
        """Return one bounded common mode state, including legacy navigation."""
        value = status.get("drive_mode")
        if isinstance(value, dict):
            mode = value.get("mode")
            state = value.get("state")
            session_id = value.get("session_id")
            message = value.get("message")
            detail = value.get("detail")
            if (
                mode in {"destination", "patrol", "roaming", "person_following"}
                and state in {
                    "starting", "active", "pausing", "paused", "stopping", "failed"
                }
                and isinstance(session_id, str)
                and 8 <= len(session_id) <= 128
            ):
                normalized = {
                    "mode": mode,
                    "state": state,
                    "sessionId": session_id,
                    "message": str(message)[:512] if message else None,
                }
                if isinstance(detail, dict):
                    normalized["detail"] = detail
                return normalized
        navigation_state = navigation.get("state")
        navigation_session = navigation.get("session_id")
        if (
            navigation_state in {"driving", "canceling"}
            and isinstance(navigation_session, str)
            and navigation_session
        ):
            return {
                "mode": "destination",
                "state": (
                    "stopping" if navigation_state == "canceling" else "active"
                ),
                "sessionId": navigation_session,
                "message": navigation.get("message"),
            }
        normalized = {
            "mode": "idle", "state": "idle",
            "sessionId": None, "message": None,
        }
        if (
            isinstance(value, dict)
            and isinstance(value.get("detail"), dict)
        ):
            normalized["detail"] = value["detail"]
        return normalized

    def _semantic_zone_bytes(self, active: dict | None) -> bytes:
        if not active:
            return b""
        try:
            user_map_path = (self.map_store / active["user_map"]).resolve()
            zone_path = user_map_path.with_name(
                f"{active['map_id']}-zones.geojson"
            )
            return zone_path.read_bytes()
        except (KeyError, OSError):
            return b""

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
        zone_bytes = self._semantic_zone_bytes(active)
        finalized = active if (
            status.get("state") == "ready"
            or status.get("_runtime_mode") == "navigation"
        ) else None
        preview_bytes = render_map_png(grid)
        if finalized:
            try:
                preview_bytes = (
                    self.map_store / finalized["preview"]
                ).resolve().read_bytes()
            except (KeyError, OSError):
                preview_bytes = render_map_png(grid)
        metadata = (
            f"{grid.width}:{grid.height}:{grid.resolution}:"
            f"{grid.origin_x}:{grid.origin_y}"
        ).encode("ascii")
        fingerprint = hashlib.sha256(
            grid.cells.tobytes() + metadata + user_map_bytes + zone_bytes
            + preview_bytes
        ).hexdigest()
        now = self.get_clock().now().nanoseconds / 1e9
        if fingerprint == self.last_uploaded_map:
            return
        if now - self.last_map_upload_monotonic < 5.0:
            return
        user_map = None
        semantic_zones = None
        if finalized:
            try:
                user_map = json.loads(user_map_bytes.decode("utf-8"))
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                user_map = None
            try:
                semantic_zones = json.loads(zone_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                semantic_zones = None
        payload = {
            "finalized": finalized is not None,
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
                preview_bytes
            ).decode("ascii"),
            "userMap": user_map,
            "semanticZones": semantic_zones,
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
                    "drive_mode_start", "drive_mode_pause",
                    "drive_mode_resume", "drive_mode_stop",
                    "room_split", "room_merge", "rooms_save",
                    "zones_apply", "demo_person_show",
                    "demo_person_hide",
                }
                or not isinstance(payload, dict)
            ):
                continue
            ok = False
            result: object
            runtime_request = ""
            try:
                result = self._local_command(operation, payload)
                if isinstance(result, dict):
                    requested = result.pop("_runtime_request", "")
                    if isinstance(requested, str):
                        runtime_request = requested
                ok = True
            except Exception as error:
                result = {"error": str(error)[:512]}
            self._cloud_json(
                "api/device/v1/robot/commands/"
                + quote(command_id, safe="") + "/complete",
                "POST",
                {"ok": ok, "result": result},
            )
            if ok and runtime_request:
                try:
                    if not write_runtime_request(
                        self.runtime_request_file,
                        runtime_request,
                        delay_seconds=1.0,
                    ):
                        raise OSError("runtime supervisor is unavailable")
                except OSError as error:
                    self._warn(str(error))

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
        if operation == "start":
            status = self._local_status()
            if status.get("_runtime_mode") == "navigation":
                if self.runtime_request_file is None:
                    raise RuntimeError("runtime supervisor is unavailable")
                return {
                    "accepted": True,
                    "message": "지도 생성 모드로 전환합니다.",
                    "_runtime_request": "mapping",
                }
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
        elif operation in {
            "drive_mode_start", "drive_mode_pause",
            "drive_mode_resume", "drive_mode_stop",
        }:
            action = operation.removeprefix("drive_mode_").replace("_", "-")
            path = f"api/drive-mode/{action}"
            body = {"mode": payload.get("mode")}
            if operation != "drive_mode_start":
                body["session_id"] = payload.get("sessionId")
        elif operation in {"demo_person_show", "demo_person_hide"}:
            action = operation.removeprefix("demo_person_")
            path = f"api/demo/person/{action}"
            body = {}
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
            elif operation == "navigation_cancel":
                path = "api/navigation/cancel"
                body = {"session_id": payload.get("sessionId")}
            elif operation == "room_split":
                path = "api/split-room"
                body = payload
            elif operation == "room_merge":
                path = "api/merge-rooms"
                body = payload
            elif operation == "rooms_save":
                path = "api/rooms"
                body = payload
            else:
                path = "api/apply-zones"
                body = payload
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
                    (value.get("message") or value.get("error"))
                    if isinstance(value, dict) else None
                )
                code = (
                    value.get("error_code")
                    if isinstance(value, dict) else None
                )
            except (json.JSONDecodeError, OSError):
                message = None
                code = None
            raise RuntimeError(
                (
                    f"{code}: {message}"
                    if isinstance(code, str) and code and message
                    else message
                )
                or f"local robot command failed ({error.code})"
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
