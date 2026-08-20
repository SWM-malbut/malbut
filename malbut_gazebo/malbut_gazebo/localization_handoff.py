#!/usr/bin/env python3
"""Preserve localization across a SLAM-to-Nav2 process handoff."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import time
import uuid

from geometry_msgs.msg import PoseWithCovarianceStamped, TransformStamped
from nav2_msgs.srv import SetInitialPose
from nav_msgs.msg import OccupancyGrid
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from tf2_msgs.msg import TFMessage
import yaml


FORMAT = "malbut-localization-handoff-v3"
# 지도마다 기록을 따로 둔다. 하나만 두면 재매핑 중 SLAM 지도의 기록이
# 저장된 지도의 기록을 덮어써, 매핑을 중지하고 돌아올 때 복원이
# 거부되고 AMCL 이 초기 위치를 영영 받지 못한다.
#
# SLAM 지도는 탐색하며 자라기 때문에 갱신마다 digest 가 달라진다. 그대로
# 두면 매핑 한 세션이 기록을 수십 개 만들어 저장된 지도 기록을 밀어낸다.
# 그래서 저장된 지도로 주행할 때의 기록만 고정하고, 고정되지 않은 기록은
# 항상 최신 하나만 남긴다.
MAX_PINNED_RECORDS = 4
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
MAP_QOS = QoSProfile(
    depth=1,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
    reliability=ReliabilityPolicy.RELIABLE,
)
TF_QOS = QoSProfile(depth=100, reliability=ReliabilityPolicy.BEST_EFFORT)
ODOM_STAMP_TOLERANCE_NS = 5_000_000_000


def _map_digest(message: OccupancyGrid) -> str:
    """Return a stable digest for an occupancy grid and its geometry."""
    metadata = message.info
    origin = metadata.origin
    fields = (
        message.header.frame_id,
        metadata.width,
        metadata.height,
        round(metadata.resolution, 9),
        round(origin.position.x, 9),
        round(origin.position.y, 9),
        round(origin.position.z, 9),
        round(origin.orientation.x, 9),
        round(origin.orientation.y, 9),
        round(origin.orientation.z, 9),
        round(origin.orientation.w, 9),
    )
    digest = hashlib.sha256(repr(fields).encode("utf-8"))
    digest.update(bytes((value + 256) % 256 for value in message.data))
    return digest.hexdigest()


def _stamp_nanoseconds(transform: TransformStamped) -> int:
    stamp = transform.header.stamp
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def _transform_value(transform: TransformStamped) -> dict:
    value = transform.transform
    return {
        "parent_frame": transform.header.frame_id,
        "child_frame": transform.child_frame_id,
        "stamp_nanoseconds": _stamp_nanoseconds(transform),
        "translation": {
            "x": float(value.translation.x),
            "y": float(value.translation.y),
            "z": float(value.translation.z),
        },
        "rotation": {
            "x": float(value.rotation.x),
            "y": float(value.rotation.y),
            "z": float(value.rotation.z),
            "w": float(value.rotation.w),
        },
    }


def _quaternion_multiply(first: tuple, second: tuple) -> tuple:
    """Compose two quaternions in parent-to-child order."""
    ax, ay, az, aw = first
    bx, by, bz, bw = second
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _rotate(vector: tuple, quaternion: tuple) -> tuple:
    """Rotate a 3D vector by a normalized quaternion."""
    x, y, z = vector
    qx, qy, qz, qw = quaternion
    uv = (
        qy * z - qz * y,
        qz * x - qx * z,
        qx * y - qy * x,
    )
    uuv = (
        qy * uv[2] - qz * uv[1],
        qz * uv[0] - qx * uv[2],
        qx * uv[1] - qy * uv[0],
    )
    return tuple(
        component + 2.0 * (qw * cross + second_cross)
        for component, cross, second_cross in zip(vector, uv, uuv)
    )


def _compose(saved: dict, odom_to_base: TransformStamped) -> dict:
    """Compose saved map-to-odom with current odom-to-base."""
    saved_translation = saved["translation"]
    saved_rotation = saved["rotation"]
    first_translation = (
        float(saved_translation["x"]),
        float(saved_translation["y"]),
        float(saved_translation["z"]),
    )
    first_rotation = (
        float(saved_rotation["x"]),
        float(saved_rotation["y"]),
        float(saved_rotation["z"]),
        float(saved_rotation["w"]),
    )
    current = odom_to_base.transform
    second_translation = (
        float(current.translation.x),
        float(current.translation.y),
        float(current.translation.z),
    )
    second_rotation = (
        float(current.rotation.x),
        float(current.rotation.y),
        float(current.rotation.z),
        float(current.rotation.w),
    )
    rotated = _rotate(second_translation, first_rotation)
    translation = tuple(
        first + second
        for first, second in zip(first_translation, rotated)
    )
    rotation = _quaternion_multiply(first_rotation, second_rotation)
    norm = math.sqrt(sum(component * component for component in rotation))
    if norm == 0.0:
        raise ValueError("composed orientation is invalid")
    return {
        "translation": translation,
        "rotation": tuple(component / norm for component in rotation),
    }


def _boot_id(path: Path = BOOT_ID_PATH) -> str:
    """Return the OS boot identity used as a power-cycle boundary."""
    value = path.read_text(encoding="utf-8").strip()
    return str(uuid.UUID(value))


def _same_odom_session(
    saved_stamp: int,
    current_stamp: int,
    saved_boot_id: str,
    current_boot_id: str,
) -> bool:
    """Require one OS boot and a non-reset odometry clock."""
    return (
        saved_boot_id == current_boot_id
        and current_stamp + ODOM_STAMP_TOLERANCE_NS >= saved_stamp
    )


def _write_state(
    path: Path,
    map_message: OccupancyGrid,
    map_to_odom: TransformStamped,
    boot_id: str | None = None,
    pinned: bool = False,
) -> None:
    """Atomically write this map's identity and frame transform."""
    session = boot_id or _boot_id()
    digest = _map_digest(map_message)
    kept = []
    try:
        previous = _load_state(path)
    except (OSError, ValueError, yaml.YAMLError):
        previous = None
    if previous is not None and previous["boot_id"] == session:
        # 같은 부팅 안에서만 다른 지도의 기록이 유효하다.
        kept = [
            record for record in previous["records"]
            if record["map_digest"] != digest and record["pinned"]
        ]
    records = kept[-MAX_PINNED_RECORDS:]
    records.append({
        "map_digest": digest,
        "pinned": bool(pinned),
        "map_to_odom": _transform_value(map_to_odom),
    })
    state = {
        "format": FORMAT,
        "boot_id": session,
        "records": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        yaml.safe_dump(state, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(path)


def _load_state(path: Path) -> dict:
    """Load and minimally validate one handoff state file."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise ValueError("unsupported localization handoff format")
    try:
        value["boot_id"] = str(uuid.UUID(value["boot_id"]))
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError("localization handoff has no valid boot id") from error
    records = value.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("localization handoff has no map records")
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("localization handoff record is malformed")
        if not isinstance(record.get("map_digest"), str):
            raise ValueError("localization handoff has no map digest")
        if not isinstance(record.get("map_to_odom"), dict):
            raise ValueError(
                "localization handoff has no map-to-odom transform"
            )
        record["pinned"] = bool(record.get("pinned", False))
    return value


def select_record(state: dict, map_digest: str) -> dict | None:
    """Return the transform recorded for one specific map, if any."""
    for record in reversed(state.get("records", [])):
        if record["map_digest"] == map_digest:
            return record
    return None


class LocalizationRecorder(Node):
    """Continuously persist the transform produced by online SLAM."""

    def __init__(self):
        """Subscribe to the live map and its map-to-odom transform."""
        super().__init__("localization_state_recorder")
        default_path = str(
            Path.home() / ".ros" / "malbut" / "localization_state.yaml"
        )
        self.state_path = Path(
            self.declare_parameter("state_path", default_path).value
        ).expanduser()
        self.map_frame = self.declare_parameter("map_frame", "map").value
        self.odom_frame = self.declare_parameter("odom_frame", "odom").value
        # 저장된 지도로 주행할 때의 기록만 고정한다. SLAM 지도는 갱신마다
        # digest 가 바뀌므로 고정하면 기록이 무한히 늘어난다.
        self.pinned = bool(self.declare_parameter("pinned", False).value)
        self.map_message = None
        self.last_write = 0.0
        self.create_subscription(
            OccupancyGrid, "map", self._receive_map, MAP_QOS
        )
        self.create_subscription(TFMessage, "/tf", self._receive_tf, TF_QOS)

    def _receive_map(self, message):
        self.map_message = message

    def _receive_tf(self, message):
        if self.map_message is None:
            return
        now = time.monotonic()
        if now - self.last_write < 0.5:
            return
        for transform in message.transforms:
            if (
                transform.header.frame_id == self.map_frame
                and transform.child_frame_id == self.odom_frame
            ):
                _write_state(
                    self.state_path,
                    self.map_message,
                    transform,
                    pinned=self.pinned,
                )
                self.last_write = now
                return


class LocalizationRestorer(Node):
    """Restore AMCL's pose only when the saved map and odom session match."""

    def __init__(self):
        """Load handoff state and wait for matching map and odometry data."""
        super().__init__("localization_state_restorer")
        default_path = str(
            Path.home() / ".ros" / "malbut" / "localization_state.yaml"
        )
        self.state_path = Path(
            self.declare_parameter("state_path", default_path).value
        ).expanduser()
        self.map_frame = self.declare_parameter("map_frame", "map").value
        self.odom_frame = self.declare_parameter("odom_frame", "odom").value
        self.base_frame = self.declare_parameter(
            "base_frame", "base_footprint"
        ).value
        self.finished = False
        self.success = False
        self.map_matches = False
        self.request_sent = False
        try:
            self.state = _load_state(self.state_path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            self.get_logger().error(
                f"cannot restore localization state: {error}"
            )
            self.finished = True
            self.state = None
            return
        self.record = None
        try:
            self.boot_id = _boot_id()
        except (OSError, ValueError) as error:
            self.get_logger().error(
                f"cannot identify the current odometry session: {error}"
            )
            self.finished = True
            return
        if self.state["boot_id"] != self.boot_id:
            self.get_logger().error(
                "OS restarted; refusing stale localization state"
            )
            self.finished = True
            return
        self.client = self.create_client(SetInitialPose, "set_initial_pose")
        self.create_subscription(
            OccupancyGrid, "map", self._receive_map, MAP_QOS
        )
        self.create_subscription(TFMessage, "/tf", self._receive_tf, TF_QOS)

    def _receive_map(self, message):
        # 재매핑 중에도 기록이 쌓이므로 지금 서빙 중인 지도의 기록을 고른다.
        record = select_record(self.state, _map_digest(message))
        if record is None:
            self.get_logger().error(
                "saved localization belongs to a different map"
            )
            self.finished = True
            return
        transform = record["map_to_odom"]
        if (
            transform.get("parent_frame") != self.map_frame
            or transform.get("child_frame") != self.odom_frame
        ):
            self.get_logger().error("localization state frame mismatch")
            self.finished = True
            return
        self.record = record
        self.map_matches = True

    def _receive_tf(self, message):
        if self.finished or self.request_sent or not self.map_matches:
            return
        for transform in message.transforms:
            if (
                transform.header.frame_id == self.odom_frame
                and transform.child_frame_id == self.base_frame
            ):
                self._restore(transform)
                return

    def _restore(self, odom_to_base):
        saved = self.record["map_to_odom"]
        if not _same_odom_session(
            int(saved["stamp_nanoseconds"]),
            _stamp_nanoseconds(odom_to_base),
            self.state["boot_id"],
            self.boot_id,
        ):
            self.get_logger().error(
                "odom clock restarted; refusing stale localization state"
            )
            self.finished = True
            return
        if not self.client.service_is_ready():
            return
        composed = _compose(saved, odom_to_base)
        request = SetInitialPose.Request()
        request.pose = PoseWithCovarianceStamped()
        request.pose.header.frame_id = self.map_frame
        request.pose.header.stamp = self.get_clock().now().to_msg()
        position = composed["translation"]
        orientation = composed["rotation"]
        request.pose.pose.pose.position.x = position[0]
        request.pose.pose.pose.position.y = position[1]
        request.pose.pose.pose.position.z = position[2]
        request.pose.pose.pose.orientation.x = orientation[0]
        request.pose.pose.pose.orientation.y = orientation[1]
        request.pose.pose.pose.orientation.z = orientation[2]
        request.pose.pose.pose.orientation.w = orientation[3]
        request.pose.pose.covariance[0] = 0.04
        request.pose.pose.covariance[7] = 0.04
        request.pose.pose.covariance[35] = 0.0076
        future = self.client.call_async(request)
        future.add_done_callback(self._restored)
        self.request_sent = True

    def _restored(self, future):
        try:
            future.result()
        except Exception as error:  # pragma: no cover - rclpy transport error
            self.get_logger().error(f"initial pose restore failed: {error}")
        else:
            self.get_logger().info("restored SLAM localization into AMCL")
            self.success = True
        self.finished = True


def record_main():
    """Run the SLAM localization-state recorder."""
    rclpy.init()
    node = LocalizationRecorder()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def restore_main():
    """Restore one saved pose and exit with a bounded timeout."""
    rclpy.init()
    node = LocalizationRestorer()
    deadline = time.monotonic() + 30.0
    while not node.finished and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.2)
    if not node.finished:
        node.get_logger().error("timed out restoring localization state")
    success = node.success
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()
    return 0 if success else 1
