"""ROS 2 node for privacy-aware home-camera event detection."""

import math
import sys
import time
from typing import Dict, Optional

from cv_bridge import CvBridge, CvBridgeError
from nav_msgs.msg import Odometry
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import Bool
from std_msgs.msg import String

from .clip_poster import EventClipPoster
from .config import DetectorConfig, validate_config
from .credentials import load_device_token
from .event_dedupe import EventDedupe
from .event_poster import EventPoster
from .event_segmenter import EventSegmenter
from .inference_health import InferenceHealth
from .motion_detector import FrameMotionDetector
from .motion_gate import MotionGate
from .yolo import YoloOnnxDetector


class HomecamDetectorNode(Node):
    """Detect events without publishing any robot motion command."""

    def __init__(self) -> None:
        super().__init__("homecam_detector")
        self._declare_parameters()
        self._config = self._read_config()
        errors = validate_config(self._config)
        if errors:
            raise ValueError("invalid detector configuration: " + "; ".join(errors))

        self._bridge = CvBridge()
        # Never process an image before the media agent publishes its
        # transient-local effective camera/monitoring privacy state.
        self._monitoring_state_received = False
        self._motion_gate = MotionGate(
            stationary_after_sec=self._config.stationary_after_sec,
            odom_timeout_sec=self._config.odom_timeout_sec,
            linear_threshold=self._config.linear_motion_threshold,
            angular_threshold=self._config.angular_motion_threshold,
        )
        self._motion_detector = FrameMotionDetector(
            area_ratio=self._config.motion_area_ratio
        )
        self._dedupe = EventDedupe(
            device_id=self._config.device_id,
            consecutive_frames=self._config.consecutive_frames,
            cooldown_sec=self._config.event_cooldown_sec,
            max_frame_gap_sec=self._config.max_frame_gap_sec,
        )
        self._segmenter = EventSegmenter(
            device_id=self._config.device_id or "local-homecam",
            confirmation_window_frames=(
                self._config.event_confirmation_window_frames
            ),
            confirmation_required_frames=(
                self._config.event_confirmation_required_frames
            ),
            pre_roll_sec=self._config.event_pre_roll_sec,
            merge_gap_sec=self._config.event_merge_gap_sec,
            max_segment_sec=self._config.max_event_clip_sec,
            notification_cooldown_sec=self._config.event_cooldown_sec,
            max_frame_gap_sec=self._config.max_frame_gap_sec,
        )
        self._storage_session_id = ""
        self._model: Optional[YoloOnnxDetector] = None
        if self._config.model_path:
            try:
                self._model = YoloOnnxDetector(
                    self._config.model_path,
                    confidence_threshold=self._config.confidence_threshold,
                )
                self.get_logger().info(
                    f"Loaded YOLO ONNX model: {self._config.model_path}"
                )
            except (FileNotFoundError, RuntimeError, ValueError) as error:
                self.get_logger().error(
                    f"YOLO unavailable; continuing with generic motion only: {error}"
                )
        else:
            self.get_logger().warning(
                "model_path is empty; person, dog, and cat detection is disabled"
            )
        self._inference_health = InferenceHealth(
            model_available=self._model is not None,
            active=self._config.monitoring_enabled,
            now=time.monotonic(),
        )

        token = load_device_token()
        self._poster: Optional[EventPoster] = None
        self._clip_poster: Optional[EventClipPoster] = None
        if self._config.backend_url and token:
            if self._config.event_clips_enabled:
                self._clip_poster = EventClipPoster(
                    self._config.backend_url,
                    token,
                    enabled=self._config.monitoring_enabled,
                    on_error=self.get_logger().error,
                )
            else:
                self._poster = EventPoster(
                    self._config.backend_url,
                    token,
                    enabled=self._config.monitoring_enabled,
                    on_error=self.get_logger().error,
                )
        else:
            self.get_logger().warning(
                "Remote event delivery disabled. Set backend_url and "
                "HOMECAM_DEVICE_TOKEN_FILE; confirmed events will only be logged."
            )

        self._image_subscription = self.create_subscription(
            Image,
            self._config.image_topic,
            self._on_image,
            rclpy.qos.qos_profile_sensor_data,
        )
        self._odom_subscription = None
        if self._config.odom_topic:
            self._odom_subscription = self.create_subscription(
                Odometry,
                self._config.odom_topic,
                self._on_odom,
                rclpy.qos.qos_profile_sensor_data,
            )
        monitoring_qos = rclpy.qos.QoSProfile(
            depth=1,
            reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
            durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._monitoring_subscription = self.create_subscription(
            Bool,
            "/homecam/monitoring_enabled",
            self._on_monitoring_state,
            monitoring_qos,
        )
        self._storage_session_subscription = self.create_subscription(
            String,
            "/homecam/storage_session_id",
            self._on_storage_session,
            monitoring_qos,
        )
        self._health_publisher = self.create_publisher(
            Bool, "/homecam/detector_healthy", monitoring_qos
        )
        self._health_timer = self.create_timer(1.0, self._publish_health)
        self._publish_health()
        self.add_on_set_parameters_callback(self._on_parameter_update)
        self.get_logger().info(
            f"Detector listening on {self._config.image_topic}; "
            f"monitoring={'on' if self._config.monitoring_enabled else 'off'}. "
            "Odometry is read-only and /cmd_vel is never published."
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter("image_topic", "/depth_cam/depth_cam")
        self.declare_parameter("odom_topic", "/odom")
        self.declare_parameter("model_path", "")
        self.declare_parameter("device_id", "")
        self.declare_parameter("backend_url", "")
        self.declare_parameter("confidence_threshold", 0.45)
        self.declare_parameter("consecutive_frames", 3)
        self.declare_parameter("event_cooldown_sec", 30.0)
        self.declare_parameter("event_confirmation_window_frames", 5)
        self.declare_parameter("event_confirmation_required_frames", 3)
        self.declare_parameter("event_pre_roll_sec", 5.0)
        self.declare_parameter("event_merge_gap_sec", 10.0)
        self.declare_parameter("max_event_clip_sec", 120.0)
        self.declare_parameter("event_clips_enabled", False)
        self.declare_parameter("max_frame_gap_sec", 1.0)
        self.declare_parameter("stationary_after_sec", 1.0)
        self.declare_parameter("odom_timeout_sec", 2.0)
        self.declare_parameter("linear_motion_threshold", 0.03)
        self.declare_parameter("angular_motion_threshold", 0.05)
        self.declare_parameter("motion_area_ratio", 0.02)
        self.declare_parameter("monitoring_enabled", False)

    def _read_config(self) -> DetectorConfig:
        return DetectorConfig(
            image_topic=self.get_parameter("image_topic").value,
            odom_topic=self.get_parameter("odom_topic").value,
            model_path=self.get_parameter("model_path").value,
            device_id=self.get_parameter("device_id").value,
            backend_url=self.get_parameter("backend_url").value,
            confidence_threshold=float(
                self.get_parameter("confidence_threshold").value
            ),
            consecutive_frames=int(self.get_parameter("consecutive_frames").value),
            event_cooldown_sec=float(
                self.get_parameter("event_cooldown_sec").value
            ),
            event_confirmation_window_frames=int(
                self.get_parameter("event_confirmation_window_frames").value
            ),
            event_confirmation_required_frames=int(
                self.get_parameter("event_confirmation_required_frames").value
            ),
            event_pre_roll_sec=float(
                self.get_parameter("event_pre_roll_sec").value
            ),
            event_merge_gap_sec=float(
                self.get_parameter("event_merge_gap_sec").value
            ),
            max_event_clip_sec=float(
                self.get_parameter("max_event_clip_sec").value
            ),
            event_clips_enabled=bool(
                self.get_parameter("event_clips_enabled").value
            ),
            max_frame_gap_sec=float(
                self.get_parameter("max_frame_gap_sec").value
            ),
            stationary_after_sec=float(
                self.get_parameter("stationary_after_sec").value
            ),
            odom_timeout_sec=float(self.get_parameter("odom_timeout_sec").value),
            linear_motion_threshold=float(
                self.get_parameter("linear_motion_threshold").value
            ),
            angular_motion_threshold=float(
                self.get_parameter("angular_motion_threshold").value
            ),
            motion_area_ratio=float(self.get_parameter("motion_area_ratio").value),
            monitoring_enabled=bool(
                self.get_parameter("monitoring_enabled").value
            ),
        )

    def _on_parameter_update(self, parameters) -> SetParametersResult:
        for parameter in parameters:
            if parameter.name != "monitoring_enabled":
                return SetParametersResult(
                    successful=False,
                    reason=(
                        f"{parameter.name} requires a node restart; only "
                        "monitoring_enabled is dynamic"
                    ),
                )
        for parameter in parameters:
            enabled = bool(parameter.value)
            object.__setattr__(self._config, "monitoring_enabled", enabled)
            self._inference_health.set_active(enabled, time.monotonic())
            if self._poster is not None:
                self._poster.set_enabled(enabled)
            if self._clip_poster is not None:
                self._clip_poster.set_enabled(enabled)
            self._motion_detector.reset()
            self._dedupe.reset()
            self._segmenter.discard()
            self.get_logger().info(
                f"Monitoring changed to {'on' if enabled else 'off'}"
            )
        return SetParametersResult(successful=True)

    def _on_odom(self, message: Odometry) -> None:
        linear = message.twist.twist.linear
        angular = message.twist.twist.angular
        linear_speed = math.sqrt(linear.x**2 + linear.y**2 + linear.z**2)
        angular_speed = math.sqrt(angular.x**2 + angular.y**2 + angular.z**2)
        self._motion_gate.update(linear_speed, angular_speed, time.monotonic())

    def _on_monitoring_state(self, message: Bool) -> None:
        enabled = bool(message.data)
        self._monitoring_state_received = True
        if enabled == self._config.monitoring_enabled:
            return
        object.__setattr__(self._config, "monitoring_enabled", enabled)
        self._inference_health.set_active(enabled, time.monotonic())
        if self._poster is not None:
            self._poster.set_enabled(enabled)
        if self._clip_poster is not None:
            self._clip_poster.set_enabled(enabled)
        self._motion_detector.reset()
        self._dedupe.reset()
        self._segmenter.discard()
        self.get_logger().info(
            f"Monitoring state received from media agent: "
            f"{'on' if enabled else 'off'}"
        )

    def _on_storage_session(self, message: String) -> None:
        session_id = message.data.strip()
        if session_id and not _is_uuid(session_id):
            self.get_logger().error("Ignoring malformed storage session ID")
            return
        if not session_id and self._storage_session_id:
            self._segmenter.discard()
        self._storage_session_id = session_id

    def _publish_health(self) -> None:
        message = Bool()
        # While monitoring, health requires recent successful inference and
        # becomes false immediately after three consecutive inference errors.
        message.data = self._inference_health.healthy(time.monotonic())
        self._health_publisher.publish(message)

    def _on_image(self, message: Image) -> None:
        if (
            not self._monitoring_state_received
            or not self._config.monitoring_enabled
        ):
            return
        try:
            frame = self._bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
        except CvBridgeError as error:
            self.get_logger().error(f"Cannot convert camera frame: {error}")
            return

        candidates: Dict[str, float] = {}
        now_monotonic = time.monotonic()
        has_motion = self._motion_detector.detect(frame)
        if has_motion and self._motion_gate.generic_motion_allowed(now_monotonic):
            candidates["motion"] = 1.0

        if self._model is not None:
            try:
                candidates.update(self._model.detect(frame))
                self._inference_health.record_success(now_monotonic)
            except (RuntimeError, ValueError) as error:
                self._inference_health.record_failure()
                self.get_logger().error(f"YOLO inference failed: {error}")

        now_wall = time.time()
        if self._config.event_clips_enabled:
            if not self._storage_session_id:
                self._segmenter.discard()
                return
            for boundary in self._segmenter.observe(
                candidates,
                occurred_at=now_wall,
                observed_at=now_monotonic,
                session_id=self._storage_session_id,
            ):
                self.get_logger().info(
                    f"Event clip {boundary.phase}: {boundary.primary_type} "
                    f"(group={boundary.event_group_id}, "
                    f"segment={boundary.segment_index})"
                )
                if self._clip_poster is not None:
                    self._clip_poster.enqueue(boundary)
        else:
            for event in self._dedupe.observe(
                candidates,
                occurred_at=now_wall,
                observed_at=now_monotonic,
            ):
                self.get_logger().info(
                    f"Confirmed {event.event_type} event "
                    f"(confidence={event.confidence:.3f}, "
                    f"id={event.idempotency_key[:8]})"
                )
                if self._poster is not None:
                    self._poster.enqueue(event)

    def destroy_node(self):
        """Stop delivery before ROS tears down the logger and subscriptions."""
        if self._poster is not None:
            self._poster.close()
            self._poster = None
        if self._clip_poster is not None:
            self._clip_poster.close()
            self._clip_poster = None
        return super().destroy_node()


def _is_uuid(value: str) -> bool:
    import uuid

    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value.lower()
    except ValueError:
        return False


def main(args=None) -> int:
    """Run the detector node."""
    rclpy.init(args=args)
    node = None
    exit_code = 0
    try:
        node = HomecamDetectorNode()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        exit_code = 0
    except ValueError as error:
        print(f"homecam_detector startup failed: {error}", file=sys.stderr)
        exit_code = 2
    finally:
        if node is not None:
            try:
                node.destroy_node()
            except KeyboardInterrupt:
                pass
        if rclpy.ok():
            try:
                rclpy.shutdown()
            except KeyboardInterrupt:
                pass
    return exit_code
