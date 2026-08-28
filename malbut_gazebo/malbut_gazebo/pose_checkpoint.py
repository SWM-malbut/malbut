"""Persist verified robot poses without treating them as map origins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

import cv2
from geometry_msgs.msg import PoseWithCovarianceStamped
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
import tf2_ros

from malbut_gazebo.map_lifecycle import load_active_revision
from malbut_gazebo.user_map_builder import load_slam_map


POSE_CHECKPOINT_FORMAT = "malbut-pose-checkpoint/v1"
POSE_CHECKPOINT_FILE = "last-localized-pose.json"
VALIDATION_TOPIC = "/malbut/localization/validation"
STABLE_SAMPLE_COUNT = 5
MAX_TF_AGE_S = 0.5
CHECKPOINT_INTERVAL_S = 2.0
CHECKPOINT_DISTANCE_M = 0.05
CHECKPOINT_YAW_RAD = 0.05
MAX_POSITION_VARIANCE_M2 = 0.25
MAX_YAW_VARIANCE_RAD2 = 0.275
MIN_SAFE_CHECKPOINT_CLEARANCE_M = 0.30
TRINARY_UNKNOWN_VALUE = 205


@dataclass(frozen=True)
class PoseSafetyGrid:
    """Provide saved-map obstacle clearance for boot pose validation."""

    resolution: float
    origin_x: float
    origin_y: float
    clearance: np.ndarray

    @classmethod
    def load(cls, map_yaml: Path) -> "PoseSafetyGrid":
        """Load one non-rotated ROS map as a bottom-up clearance grid."""
        slam_map = load_slam_map(map_yaml)
        transform = slam_map.transform
        if abs(transform.origin_yaw) > 1e-6:
            raise ValueError(
                "pose checkpoints do not support a rotated map origin"
            )
        normalized = slam_map.image.astype(np.float32) / 255.0
        occupancy = (
            normalized if slam_map.negate else 1.0 - normalized
        )
        free = occupancy <= slam_map.free_threshold
        if slam_map.mode == "trinary":
            free &= slam_map.image != TRINARY_UNKNOWN_VALUE
        bottom_up = np.flipud(free.astype(np.uint8))
        clearance = cv2.distanceTransform(
            bottom_up, cv2.DIST_L2, 5
        ) * transform.resolution
        clearance.setflags(write=False)
        return cls(
            transform.resolution,
            transform.origin_x,
            transform.origin_y,
            clearance,
        )

    def clearance_at(self, pose: object) -> float:
        """Return physical obstacle clearance or zero outside known free map."""
        normalized = _finite_pose(pose)
        if normalized is None:
            return 0.0
        column = math.floor(
            (normalized["x"] - self.origin_x) / self.resolution
        )
        row = math.floor(
            (normalized["y"] - self.origin_y) / self.resolution
        )
        height, width = self.clearance.shape
        if not (0 <= column < width and 0 <= row < height):
            return 0.0
        return float(self.clearance[row, column])

    def accepts(
        self,
        pose: object,
        minimum_clearance_m: float = MIN_SAFE_CHECKPOINT_CLEARANCE_M,
    ) -> bool:
        """Return whether a pose is safe enough for a future robot spawn."""
        return self.clearance_at(pose) >= minimum_clearance_m


def _finite_pose(value: object) -> dict[str, float] | None:
    """Return finite x/y/yaw values or reject the complete pose."""
    if not isinstance(value, dict):
        return None
    try:
        pose = {name: float(value[name]) for name in ("x", "y", "yaw")}
    except (KeyError, TypeError, ValueError):
        return None
    return pose if all(math.isfinite(item) for item in pose.values()) else None


def load_pose_checkpoint(store: Path, active: dict | None) -> dict | None:
    """Load one checkpoint only when it belongs to the active map revision."""
    if not isinstance(active, dict):
        return None
    path = store.expanduser() / POSE_CHECKPOINT_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if value.get("format") != POSE_CHECKPOINT_FORMAT:
        return None
    if value.get("map_id") != active.get("map_id"):
        return None
    if value.get("map_revision") != active.get("map_revision"):
        return None
    pose = _finite_pose(value.get("pose"))
    if pose is None:
        return None
    result = dict(value)
    result["pose"] = pose
    return result


def persist_pose_checkpoint(
    store: Path,
    active: dict,
    pose: dict,
    *,
    boot_id: str,
    observed_at: str | None = None,
) -> dict:
    """Atomically store a verified pose independently from ``active.json``."""
    normalized = _finite_pose(pose)
    map_id = active.get("map_id") if isinstance(active, dict) else None
    map_revision = (
        active.get("map_revision") if isinstance(active, dict) else None
    )
    if normalized is None or not isinstance(map_id, str) or not map_id:
        raise ValueError("a finite pose and active map identity are required")
    if not isinstance(map_revision, str) or not map_revision:
        raise ValueError("a finite pose and active map identity are required")
    value = {
        "format": POSE_CHECKPOINT_FORMAT,
        "map_id": map_id,
        "map_revision": map_revision,
        "pose": normalized,
        "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        "boot_id": str(boot_id),
        "validation": "verified",
    }
    store = store.expanduser().resolve()
    store.mkdir(parents=True, exist_ok=True)
    path = store / POSE_CHECKPOINT_FILE
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=store
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def _yaw_distance(first: float, second: float) -> float:
    """Return the shortest angular distance between two yaws."""
    return abs(math.atan2(math.sin(first - second), math.cos(first - second)))


def acceptable_amcl_covariance(covariance: object) -> bool:
    """Return whether AMCL reports a bounded planar pose uncertainty."""
    if isinstance(covariance, (str, bytes)):
        return False
    try:
        if len(covariance) != 36:  # type: ignore[arg-type]
            return False
        values = [float(covariance[index]) for index in (0, 7, 35)]
    except (IndexError, KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(value) and value >= 0.0 for value in values):
        return False
    return (
        values[0] <= MAX_POSITION_VARIANCE_M2
        and values[1] <= MAX_POSITION_VARIANCE_M2
        and values[2] <= MAX_YAW_VARIANCE_RAD2
    )


class PoseCheckpointNode(Node):
    """Validate boot localization and record only stable localized poses."""

    def __init__(self) -> None:
        """Connect AMCL, TF, validation state, and atomic persistence."""
        super().__init__(
            "pose_checkpoint",
            automatically_declare_parameters_from_overrides=True,
        )
        self.map_store = Path(
            str(self.get_parameter("map_store").value)
        ).expanduser().resolve()
        self.active = load_active_revision(self.map_store)
        if self.active is None:
            raise ValueError("pose checkpoint requires an active map revision")
        map_yaml = self.map_store / str(self.active.get("map_yaml", ""))
        self.pose_safety = PoseSafetyGrid.load(map_yaml)
        expected_map_id = str(self.get_parameter("map_id").value).strip()
        expected_revision = str(
            self.get_parameter("map_revision").value
        ).strip()
        if expected_map_id and expected_map_id != self.active.get("map_id"):
            raise ValueError(
                "pose checkpoint map id does not match active map"
            )
        if (
            expected_revision
            and expected_revision != self.active.get("map_revision")
        ):
            raise ValueError(
                "pose checkpoint map revision does not match active map"
            )
        trusted_value = self.get_parameter("initially_trusted").value
        self.initially_trusted = (
            trusted_value
            if isinstance(trusted_value, bool)
            else str(trusted_value).strip().lower() in {
                "1", "true", "yes", "on"
            }
        )
        self.validation_state = (
            "verifying" if self.initially_trusted
            else "revalidation_required"
        )
        self.proposal_received = self.initially_trusted
        self.checkpoint = load_pose_checkpoint(self.map_store, self.active)
        if (
            self.checkpoint is not None
            and not self.pose_safety.accepts(self.checkpoint.get("pose"))
        ):
            clearance = self.pose_safety.clearance_at(
                self.checkpoint.get("pose")
            )
            self.get_logger().warning(
                "ignoring an unsafe pose checkpoint "
                f"(clearance={clearance:.3f} m)"
            )
            self.checkpoint = None
        self.checkpoint_proposed = False
        self.ignore_initial_pose_count = 0
        self.proposal_stamp_ns = 0
        self.stable_samples = 0
        self.amcl_active = False
        self.latest_amcl_quality: tuple[int, int, bool] | None = None
        self.lifecycle_future = None
        self.last_pose: dict[str, float] | None = None
        self.latest_verified_pose: dict[str, float] | None = None
        self.last_write_monotonic = -CHECKPOINT_INTERVAL_S
        self.boot_id = self._boot_id()
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.validation_publisher = self.create_publisher(
            String, VALIDATION_TOPIC, qos
        )
        self.initial_pose_publisher = self.create_publisher(
            PoseWithCovarianceStamped, "/initialpose", 10
        )
        self.create_subscription(
            PoseWithCovarianceStamped,
            "/initialpose",
            self._receive_initial_pose,
            10,
        )
        self.create_subscription(
            PoseWithCovarianceStamped,
            "/amcl_pose",
            self._receive_amcl_pose,
            qos,
        )
        self.lifecycle_client = self.create_client(
            GetState, "/amcl/get_state"
        )
        self.tf_buffer = tf2_ros.Buffer(node=self)
        self.tf_listener = tf2_ros.TransformListener(
            self.tf_buffer, self, spin_thread=False
        )
        self.create_timer(0.2, self._sample)
        self.create_timer(1.0, self._refresh_lifecycle)
        self._publish_validation()

    @staticmethod
    def _boot_id() -> str:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
        except OSError:
            return "unknown"

    def _publish_validation(self) -> None:
        message = String()
        message.data = self.validation_state
        self.validation_publisher.publish(message)

    def _set_validation(self, value: str) -> None:
        if value == self.validation_state:
            return
        self.validation_state = value
        self._publish_validation()
        self.get_logger().info(f"boot localization: {value}")

    def _receive_initial_pose(
        self, _message: PoseWithCovarianceStamped
    ) -> None:
        """Treat an explicit pose proposal as the start, not end, of checks."""
        if self.ignore_initial_pose_count > 0:
            self.ignore_initial_pose_count -= 1
            return
        if self.validation_state == "ok":
            return
        self.proposal_received = True
        self.proposal_stamp_ns = self.get_clock().now().nanoseconds
        self.stable_samples = 0
        self.latest_amcl_quality = None
        self._set_validation("verifying")

    def _receive_amcl_pose(
        self, message: PoseWithCovarianceStamped
    ) -> None:
        """Keep the latest AMCL confidence sample for boot revalidation."""
        stamp = message.header.stamp
        stamp_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
        acceptable = acceptable_amcl_covariance(message.pose.covariance)
        self.latest_amcl_quality = (
            self.get_clock().now().nanoseconds,
            stamp_ns,
            acceptable,
        )
        if self.validation_state == "verifying":
            covariance = message.pose.covariance
            self.get_logger().info(
                "received AMCL revalidation confidence: "
                + ("bounded" if acceptable else "too uncertain")
                + (
                    f" (x={covariance[0]:.3f}, y={covariance[7]:.3f}, "
                    f"yaw={covariance[35]:.3f})"
                )
            )

    def _propose_checkpoint(self) -> None:
        """Offer the last pose to AMCL as an uncertain candidate, not truth."""
        if self.checkpoint_proposed or self.checkpoint is None:
            return
        pose = self.checkpoint["pose"]
        message = PoseWithCovarianceStamped()
        message.header.frame_id = "map"
        message.header.stamp = self.get_clock().now().to_msg()
        message.pose.pose.position.x = pose["x"]
        message.pose.pose.position.y = pose["y"]
        message.pose.pose.orientation.z = math.sin(pose["yaw"] / 2.0)
        message.pose.pose.orientation.w = math.cos(pose["yaw"] / 2.0)
        message.pose.covariance[0] = MAX_POSITION_VARIANCE_M2
        message.pose.covariance[7] = MAX_POSITION_VARIANCE_M2
        message.pose.covariance[35] = MAX_YAW_VARIANCE_RAD2
        self.checkpoint_proposed = True
        self.proposal_received = True
        self.proposal_stamp_ns = self.get_clock().now().nanoseconds
        self.ignore_initial_pose_count += 1
        self.stable_samples = 0
        self.latest_amcl_quality = None
        self._set_validation("verifying")
        self.initial_pose_publisher.publish(message)
        self.get_logger().info(
            "proposed the last checkpoint for AMCL revalidation"
        )

    def _refresh_lifecycle(self) -> None:
        if self.lifecycle_future is not None:
            if not self.lifecycle_future.done():
                return
            try:
                response = self.lifecycle_future.result()
                self.amcl_active = (
                    response.current_state.id == State.PRIMARY_STATE_ACTIVE
                )
            except Exception:
                self.amcl_active = False
            self.lifecycle_future = None
        if self.lifecycle_client.service_is_ready():
            self.lifecycle_future = self.lifecycle_client.call_async(
                GetState.Request()
            )

    def _sample(self) -> None:
        if (
            not self.initially_trusted
            and not self.proposal_received
            and self.amcl_active
        ):
            self._propose_checkpoint()
        if not self.proposal_received or not self.amcl_active:
            self.stable_samples = 0
            return
        if not self.initially_trusted:
            quality = self.latest_amcl_quality
            if quality is None or not quality[2]:
                self.stable_samples = 0
                return
            if quality[0] < self.proposal_stamp_ns:
                self.stable_samples = 0
                return
            quality_age = (
                self.get_clock().now().nanoseconds - quality[1]
            ) / 1e9
            if not -2.0 <= quality_age <= 1.0:
                self.stable_samples = 0
                return
        try:
            transform = self.tf_buffer.lookup_transform(
                "map", "base_footprint", rclpy.time.Time()
            )
        except (
            tf2_ros.ConnectivityException,
            tf2_ros.ExtrapolationException,
            tf2_ros.LookupException,
        ):
            self.stable_samples = 0
            return
        stamp = transform.header.stamp
        stamp_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
        age = (self.get_clock().now().nanoseconds - stamp_ns) / 1e9
        if not -2.0 <= age <= MAX_TF_AGE_S:
            self.stable_samples = 0
            return
        rotation = transform.transform.rotation
        yaw = math.atan2(
            2.0 * (rotation.w * rotation.z + rotation.x * rotation.y),
            1.0 - 2.0 * (rotation.y * rotation.y + rotation.z * rotation.z),
        )
        pose = {
            "x": float(transform.transform.translation.x),
            "y": float(transform.transform.translation.y),
            "yaw": yaw,
        }
        self.stable_samples += 1
        if self.stable_samples < STABLE_SAMPLE_COUNT:
            return
        if not self.pose_safety.accepts(pose):
            self.stable_samples = 0
            return
        self._set_validation("ok")
        self.latest_verified_pose = dict(pose)
        self._write_if_due(pose)

    def _write_if_due(self, pose: dict[str, float]) -> None:
        now = self.get_clock().now().nanoseconds / 1e9
        moved = self.last_pose is None or math.hypot(
            pose["x"] - self.last_pose["x"],
            pose["y"] - self.last_pose["y"],
        ) >= CHECKPOINT_DISTANCE_M
        turned = self.last_pose is None or _yaw_distance(
            pose["yaw"], self.last_pose["yaw"]
        ) >= CHECKPOINT_YAW_RAD
        if now - self.last_write_monotonic < CHECKPOINT_INTERVAL_S:
            return
        if not moved and not turned:
            return
        persist_pose_checkpoint(
            self.map_store,
            self.active,
            pose,
            boot_id=self.boot_id,
        )
        self.last_pose = dict(pose)
        self.last_write_monotonic = now

    def destroy_node(self) -> bool:
        """Flush the latest verified pose during an orderly shutdown."""
        if self.validation_state == "ok" and self.latest_verified_pose:
            try:
                persist_pose_checkpoint(
                    self.map_store,
                    self.active,
                    self.latest_verified_pose,
                    boot_id=self.boot_id,
                )
            except OSError as error:
                self.get_logger().error(
                    f"could not flush pose checkpoint: {error}"
                )
        return super().destroy_node()


def main() -> int:
    """Run the boot-localization validator and pose checkpoint recorder."""
    rclpy.init()
    node = PoseCheckpointNode()
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
