"""ROS 2 node that turns camera frames into fall pose candidates."""

import json
import math
import sys
import threading
import time
from typing import Dict, Optional

from action_msgs.msg import GoalStatus, GoalStatusArray
from cv_bridge import CvBridge, CvBridgeError
from nav_msgs.msg import Odometry
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import CameraInfo, Image
from std_msgs.msg import String

from .aurora_depth import AuroraDepthEvidenceExtractor, CameraIntrinsics
from .config import DetectorConfig, EXECUTION_PARAMETER_NAMES, validate_config
from .fall_candidate import FallCandidateConfig, FallCandidateDetector
from .fall_pose_control import FallPoseControl
from .motion_gate import MotionGate
from .pose import PersonPoseEstimator, PersonPoseGate
from .pose_tracker import PersonPoseTracker, PoseTrackingResult


class HomecamDetectorNode(Node):
    """Publish fall pose candidates without any robot motion command."""

    def __init__(self) -> None:
        super().__init__("homecam_detector")
        self._declare_parameters()
        self._config = self._read_config()
        errors = validate_config(self._config)
        if errors:
            raise ValueError("invalid detector configuration: " + "; ".join(errors))
        self._fall_control = FallPoseControl(self._config.fall_runtime_id)
        self._fall_active = False

        self._bridge = CvBridge()
        self._depth_lock = threading.Lock()
        self._depth_log_lock = threading.Lock()
        self._depth_error_log_times: Dict[str, float] = {}
        self._latest_depth_image = None
        self._latest_depth_stamp_s: Optional[float] = None
        self._depth_intrinsics: Optional[CameraIntrinsics] = None
        self._depth_extractor = AuroraDepthEvidenceExtractor(
            camera_height_m=self._config.camera_height_m,
            camera_pitch_rad=self._config.camera_pitch_rad,
            maximum_stamp_delta_s=self._config.depth_max_stamp_delta_sec,
            keypoint_threshold=self._config.pose_keypoint_threshold,
        )
        self._motion_gate = MotionGate(
            stationary_after_sec=self._config.stationary_after_sec,
            odom_timeout_sec=self._config.odom_timeout_sec,
            linear_threshold=self._config.linear_motion_threshold,
            angular_threshold=self._config.angular_motion_threshold,
        )
        if self._config.pose_opencv_num_threads:
            import cv2
            cv2.setNumThreads(self._config.pose_opencv_num_threads)
        self._pose_estimator: Optional[PersonPoseEstimator] = None
        try:
            self._pose_estimator = PersonPoseEstimator(
                self._config.pose_model_path,
                confidence_threshold=(
                    self._config.pose_confidence_threshold
                ),
                keypoint_threshold=self._config.pose_keypoint_threshold,
                keep_aspect=self._config.pose_keep_aspect,
                execution_provider=self._config.pose_execution_provider,
                intra_op_num_threads=self._config.pose_intra_op_num_threads,
                allow_spinning=self._config.pose_allow_spinning,
            )
            self.get_logger().info(
                "Loaded independent YOLO pose ONNX model: "
                f"{self._config.pose_model_path}; "
                f"provider={self._pose_estimator.execution_provider} "
                f"(requested={self._config.pose_execution_provider}), "
                f"threads={self._config.pose_intra_op_num_threads}, "
                f"spinning={self._config.pose_allow_spinning}, "
                f"opencv_threads={self._config.pose_opencv_num_threads}"
            )
            if (self._config.pose_execution_provider == 'auto'
                    and self._pose_estimator.execution_provider == 'cpu'):
                self.get_logger().warning(
                    'YOLO pose auto selected CPU: this Python has no CUDAExecutionProvider. '
                    'GPU acceleration is NOT active. Prepare a compatible GPU runtime and '
                    'select it as the detector Python; set pose_execution_provider=cuda '
                    '(Bringup: fall_pose_execution_provider:=cuda) to require GPU execution.')
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            raise RuntimeError(f"Required pose model could not be loaded: {error}") from error
        self._pose_gate = PersonPoseGate(self._config.pose_inference_fps)
        self._pose_tracker = PersonPoseTracker(
            strong_threshold=self._config.pose_confidence_threshold,
            candidate_threshold=self._config.pose_candidate_confidence_threshold,
            max_gap_sec=self._config.pose_track_max_gap_sec,
            min_observations=self._config.pose_track_min_observations,
            max_people=self._config.pose_track_max_people,
        )
        self._pose_frame_context = None
        self._fall_detector = FallCandidateDetector(FallCandidateConfig(
            keypoint_threshold=self._config.pose_keypoint_threshold,
            temporal_window_sec=self._config.fall_temporal_window_sec,
            found_down_hold_sec=self._config.fall_found_down_hold_sec,
            max_frame_gap_sec=self._config.fall_max_frame_gap_sec,
        ))
        self._last_pose_source_stamp = None
        self._pose_failure_count = 0
        self._pose_present = False
        self._image_subscription = self.create_subscription(
            Image,
            self._config.image_topic,
            self._on_image,
            rclpy.qos.qos_profile_sensor_data,
        )
        self._depth_subscription = None
        self._depth_camera_info_subscription = None
        if self._config.depth_image_topic:
            self._depth_subscription = self.create_subscription(
                Image,
                self._config.depth_image_topic,
                self._on_depth_image,
                rclpy.qos.qos_profile_sensor_data,
            )
            self._depth_camera_info_subscription = self.create_subscription(
                CameraInfo,
                self._config.depth_camera_info_topic,
                self._on_depth_camera_info,
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
        self._navigation_subscription = None
        if self._config.navigation_status_topic:
            self._navigation_subscription = self.create_subscription(
                GoalStatusArray,
                self._config.navigation_status_topic,
                self._on_navigation_status,
                10,
            )
        from malbut_interfaces.msg import FallRuntimeStatus

        self._fall_status_subscription = self.create_subscription(
            FallRuntimeStatus, "/malbut/falls/status",
            self._on_fall_status, 1,
        )
        self._pose_publisher = self.create_publisher(
            String,
            "/homecam/person_pose",
            rclpy.qos.qos_profile_sensor_data,
        )
        self._poses_publisher = self.create_publisher(
            String, "/homecam/person_poses", rclpy.qos.qos_profile_sensor_data
        )
        # Local experimental contract, not the proposed typed F05 interface.
        self._fall_candidates_publisher = self.create_publisher(
            String, "/homecam/fall_candidates", 10
        )
        # Expire a stale VLM status even when no camera frame arrives.
        self._fall_control_timer = self.create_timer(1.0, self._refresh_fall_control)
        self._refresh_fall_control()
        self.add_on_set_parameters_callback(self._on_parameter_update)
        self.get_logger().info(
            f"Fall pose node listening on {self._config.image_topic}. "
            "Odometry is read-only and /cmd_vel is never published."
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter("fall_runtime_id", "")
        self.declare_parameter("pose_keep_aspect", False)
        defaults = DetectorConfig()
        for name in EXECUTION_PARAMETER_NAMES:
            self.declare_parameter(name, getattr(defaults, name))
        self.declare_parameter("image_topic", "/depth_cam/depth_cam")
        self.declare_parameter("depth_image_topic", "")
        self.declare_parameter("depth_camera_info_topic", "")
        self.declare_parameter("depth_aligned_to_rgb", False)
        self.declare_parameter("depth_scale_m", 0.0)
        self.declare_parameter("depth_max_stamp_delta_sec", 0.15)
        self.declare_parameter("camera_height_m", 0.091864)
        self.declare_parameter("camera_pitch_rad", 0.0)
        self.declare_parameter("odom_topic", "/odom")
        self.declare_parameter(
            "navigation_status_topic", "/navigate_to_pose/_action/status"
        )
        self.declare_parameter("pose_model_path", "")
        self.declare_parameter("pose_confidence_threshold", 0.45)
        self.declare_parameter("pose_keypoint_threshold", 0.5)
        self.declare_parameter("pose_inference_fps", 5.0)
        self.declare_parameter("pose_candidate_confidence_threshold", 0.10)
        self.declare_parameter("pose_track_max_gap_sec", 1.0)
        self.declare_parameter("pose_track_min_observations", 3)
        self.declare_parameter("pose_track_max_people", 32)
        self.declare_parameter("fall_temporal_window_sec", 2.0)
        self.declare_parameter("fall_found_down_hold_sec", 0.6)
        self.declare_parameter("fall_max_frame_gap_sec", 0.5)
        self.declare_parameter("stationary_after_sec", 2.0)
        self.declare_parameter("odom_timeout_sec", 2.0)
        self.declare_parameter("linear_motion_threshold", 0.03)
        self.declare_parameter("angular_motion_threshold", 0.05)

    def _read_config(self) -> DetectorConfig:
        return DetectorConfig(
            fall_runtime_id=self.get_parameter("fall_runtime_id").value,
            pose_keep_aspect=bool(self.get_parameter("pose_keep_aspect").value),
            pose_execution_provider=self.get_parameter('pose_execution_provider').value,
            pose_intra_op_num_threads=self.get_parameter('pose_intra_op_num_threads').value,
            pose_allow_spinning=self.get_parameter('pose_allow_spinning').value,
            pose_opencv_num_threads=self.get_parameter('pose_opencv_num_threads').value,
            image_topic=self.get_parameter("image_topic").value,
            depth_image_topic=self.get_parameter("depth_image_topic").value,
            depth_camera_info_topic=self.get_parameter(
                "depth_camera_info_topic"
            ).value,
            depth_aligned_to_rgb=bool(
                self.get_parameter("depth_aligned_to_rgb").value
            ),
            depth_scale_m=float(self.get_parameter("depth_scale_m").value),
            depth_max_stamp_delta_sec=float(
                self.get_parameter("depth_max_stamp_delta_sec").value
            ),
            camera_height_m=float(
                self.get_parameter("camera_height_m").value
            ),
            camera_pitch_rad=float(
                self.get_parameter("camera_pitch_rad").value
            ),
            odom_topic=self.get_parameter("odom_topic").value,
            navigation_status_topic=self.get_parameter(
                "navigation_status_topic"
            ).value,
            pose_model_path=self.get_parameter("pose_model_path").value,
            pose_confidence_threshold=float(
                self.get_parameter("pose_confidence_threshold").value
            ),
            pose_keypoint_threshold=float(
                self.get_parameter("pose_keypoint_threshold").value
            ),
            pose_inference_fps=float(
                self.get_parameter("pose_inference_fps").value
            ),
            pose_candidate_confidence_threshold=float(
                self.get_parameter("pose_candidate_confidence_threshold").value
            ),
            pose_track_max_gap_sec=float(
                self.get_parameter("pose_track_max_gap_sec").value
            ),
            pose_track_min_observations=self.get_parameter(
                "pose_track_min_observations"
            ).value,
            pose_track_max_people=self.get_parameter("pose_track_max_people").value,
            fall_temporal_window_sec=float(self.get_parameter("fall_temporal_window_sec").value),
            fall_found_down_hold_sec=float(self.get_parameter("fall_found_down_hold_sec").value),
            fall_max_frame_gap_sec=float(self.get_parameter("fall_max_frame_gap_sec").value),
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
        )

    def _on_parameter_update(self, parameters) -> SetParametersResult:
        return SetParametersResult(
            successful=False, reason="Fall pose permissions come from VLM status")

    def _on_odom(self, message: Odometry) -> None:
        linear = message.twist.twist.linear
        angular = message.twist.twist.angular
        linear_speed = math.sqrt(linear.x**2 + linear.y**2 + linear.z**2)
        angular_speed = math.sqrt(angular.x**2 + angular.y**2 + angular.z**2)
        self._motion_gate.update(linear_speed, angular_speed, time.monotonic())

    def _on_navigation_status(self, message: GoalStatusArray) -> None:
        active_statuses = {
            GoalStatus.STATUS_ACCEPTED,
            GoalStatus.STATUS_EXECUTING,
            GoalStatus.STATUS_CANCELING,
        }
        active = any(
            status.status in active_statuses for status in message.status_list
        )
        # A finished run needs a new stable period before poses count as stationary.
        self._motion_gate.set_navigation_active(active)

    def _on_fall_status(self, message) -> None:
        if self._fall_control.receive(message):
            self._refresh_fall_control()

    def _refresh_fall_control(self) -> bool:
        active = self._fall_control.active()
        if active != self._fall_active:
            self._fall_active = active
            self._reset_pose_state()
        return active

    def _reset_pose_state(self) -> None:
        self._pose_gate.reset()
        self._pose_failure_count = 0
        if self._pose_present:
            self._publish_pose_absent()
        expired = self._pose_tracker.reset()
        self._fall_detector.reset()
        self._pose_frame_context = None
        self._last_pose_source_stamp = None
        self._publish_tracked_poses(
            PoseTrackingResult((), (), expired), None,
            status="waiting_frame" if self._fall_active else "disabled",
        )

    @staticmethod
    def _stamp_seconds(message) -> float:
        return (
            float(message.header.stamp.sec)
            + float(message.header.stamp.nanosec) / 1_000_000_000.0
        )

    def _on_depth_image(self, message: Image) -> None:
        if not self._refresh_fall_control():
            return
        try:
            depth = self._bridge.imgmsg_to_cv2(
                message,
                desired_encoding="passthrough",
            )
        except CvBridgeError as error:
            self._log_depth_error(
                "conversion",
                f"Cannot convert depth frame: {error}",
            )
            return
        if getattr(depth, "ndim", 0) != 2:
            self._log_depth_error(
                "shape",
                "Ignoring non-scalar Aurora depth frame",
            )
            return
        with self._depth_lock:
            self._latest_depth_image = depth.copy()
            self._latest_depth_stamp_s = self._stamp_seconds(message)

    def _on_depth_camera_info(self, message: CameraInfo) -> None:
        try:
            intrinsics = CameraIntrinsics(
                width=int(message.width),
                height=int(message.height),
                fx=float(message.k[0]),
                fy=float(message.k[4]),
                cx=float(message.k[2]),
                cy=float(message.k[5]),
            )
        except (IndexError, TypeError, ValueError) as error:
            self._log_depth_error(
                "camera_info",
                f"Ignoring invalid Aurora CameraInfo: {type(error).__name__}",
            )
            return
        with self._depth_lock:
            self._depth_intrinsics = intrinsics

    def _depth_evidence(self, pose, image_message: Image):
        if not self._config.depth_image_topic:
            return {"usable": False, "reason": "depth_unavailable"}
        with self._depth_lock:
            depth = self._latest_depth_image
            depth_stamp_s = self._latest_depth_stamp_s
            intrinsics = self._depth_intrinsics
        if depth is None or depth_stamp_s is None or intrinsics is None:
            return {"usable": False, "reason": "depth_unavailable"}
        try:
            observation = self._depth_extractor.extract(
                depth_image=depth,
                intrinsics=intrinsics,
                pose=pose,
                rgb_stamp_s=self._stamp_seconds(image_message),
                depth_stamp_s=depth_stamp_s,
                aligned_to_rgb=self._config.depth_aligned_to_rgb,
                depth_scale_m=(
                    self._config.depth_scale_m
                    if self._config.depth_scale_m > 0
                    else None
                ),
            )
        except ValueError as error:
            self._log_depth_error(
                "evidence",
                f"Aurora depth evidence rejected: {type(error).__name__}",
            )
            return {"usable": False, "reason": "depth_invalid"}
        result = observation.as_dict()
        if not observation.aligned_to_rgb:
            result["reason"] = "depth_unaligned"
        elif observation.stale:
            result["reason"] = "sensor_stale"
        elif not observation.usable:
            result["reason"] = "depth_insufficient"
        return result

    def _log_depth_error(self, key: str, message: str) -> None:
        """Rate-limit repeated camera calibration/format failures."""
        now = time.monotonic()
        with self._depth_log_lock:
            previous = self._depth_error_log_times.get(key, 0.0)
            if now - previous < 10.0:
                return
            self._depth_error_log_times[key] = now
        self.get_logger().error(message)

    def _publish_pose_absent(self) -> None:
        message = String()
        message.data = '{"present":false}'
        self._pose_publisher.publish(message)
        self._pose_present = False

    def _observe_person_pose(
        self, frame, image_message: Image, now: float
    ) -> None:
        if not self._pose_gate.should_infer(now):
            return
        stamp = self._stamp_seconds(image_message)
        context = (image_message.header.frame_id, frame.shape[:2])
        expired = ()
        if (context != self._pose_frame_context
                or (self._last_pose_source_stamp is not None
                    and stamp < self._last_pose_source_stamp)):
            expired = self._pose_tracker.reset()
            self._fall_detector.reset()
            self._last_pose_source_stamp = None
        self._pose_frame_context = context
        if stamp == self._last_pose_source_stamp:
            if self._pose_present:
                self._publish_pose_absent()
            self._publish_tracked_poses(
                self._pose_tracker.update((), now), image_message,
                status="duplicate_frame",
            )
            return
        self._last_pose_source_stamp = stamp
        if self._pose_estimator is None:
            self._publish_tracked_poses(
                self._pose_tracker.update((), now), image_message,
                status="model_unavailable", expired=expired,
            )
            return
        try:
            poses = self._pose_estimator.estimate_all(
                frame, confidence_threshold=self._config.pose_candidate_confidence_threshold
            )
            result = self._pose_tracker.update(poses, now)
            self._pose_failure_count = 0
        except (RuntimeError, ValueError) as error:
            self._pose_failure_count += 1
            self.get_logger().error(f"YOLO pose inference failed: {error}")
            if self._pose_present:
                self._publish_pose_absent()
            self._publish_tracked_poses(
                self._pose_tracker.update((), now), image_message,
                status="inference_error", expired=expired,
            )
            return
        self._publish_tracked_poses(result, image_message, expired=expired)
        # Compatibility output only: same high threshold and strongest person.
        # New multi-person consumers MUST use /homecam/person_poses instead.
        pose = next((p for p in poses
                     if p.box_confidence >= self._config.pose_confidence_threshold), None)
        if pose is None:
            if self._pose_present:
                self._publish_pose_absent()
            return
        payload = pose.as_dict()
        payload["depthEvidence"] = self._depth_evidence(pose, image_message)
        payload["captureStamp"] = {
            "sec": int(image_message.header.stamp.sec),
            "nanosec": int(image_message.header.stamp.nanosec),
        }
        output = String()
        output.data = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        self._pose_publisher.publish(output)
        self._pose_present = True

    def _publish_tracked_poses(
        self, result: PoseTrackingResult, image_message, *, status="ok", expired=()
    ) -> None:
        persons = []
        for track in result.tracks:
            persons.append({
                "trackId": track.track_id,
                "state": track.state,
                "observed": track.pose is not None,
                "confidenceLevel": track.confidence_level if track.pose else None,
                "observationCount": track.observation_count,
                "consecutiveObservations": track.consecutive_observations,
                "lastSeenAgeSec": track.last_seen_age_sec,
                "pose": track.pose.as_dict() if track.pose else None,
                "depthEvidence": (self._depth_evidence(track.pose, image_message)
                                  if track.pose and image_message is not None
                                  else {"usable": False, "reason": "not_observed"}),
            })
        payload = {
            "schemaVersion": 1, "status": status,
            "captureStamp": ({"sec": int(image_message.header.stamp.sec),
                              "nanosec": int(image_message.header.stamp.nanosec)}
                             if image_message is not None else None),
            "frameId": image_message.header.frame_id if image_message is not None else None,
            "persons": persons,
            "unassigned": [{"pose": item.pose.as_dict(), "reason": item.reason}
                           for item in result.unassigned],
            "expiredTrackIds": list(expired) + list(result.expired_track_ids),
        }
        message = String()
        message.data = json.dumps(payload, ensure_ascii=True, allow_nan=False)
        self._poses_publisher.publish(message)
        self._publish_fall_candidates(result, image_message, persons, status, expired)

    def _publish_fall_candidates(self, result, image_message, persons, status, expired):
        if status != "ok" or image_message is None:
            payload = self._fall_detector.unavailable(status)
        else:
            try:
                height, width = self._pose_frame_context[1]
                payload = self._fall_detector.update(
                    result, capture_time=self._stamp_seconds(image_message),
                    image_size=(width, height),
                    robot_motion=self._motion_gate.pose_motion_state(time.monotonic()),
                    depth_by_track={p["trackId"]: p["depthEvidence"] for p in persons},
                )
            except (ValueError, RuntimeError) as error:
                self.get_logger().error(f"Fall candidate analysis failed: {type(error).__name__}")
                payload = self._fall_detector.unavailable("analysis_error")
        payload["expiredTrackIds"] = list(expired) + list(result.expired_track_ids)
        payload["frameId"] = image_message.header.frame_id if image_message is not None else None
        message = String()
        message.data = json.dumps(payload, ensure_ascii=True, allow_nan=False)
        self._fall_candidates_publisher.publish(message)

    def _on_image(self, message: Image) -> None:
        if not self._refresh_fall_control():
            return
        # Check the rate limit before conversion: frames between pose samples
        # have no other consumer in this node.
        pose_sample_time = time.monotonic()
        if not self._pose_gate.is_due(pose_sample_time):
            return
        try:
            frame = self._bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
        except CvBridgeError as error:
            # A stream of corrupt images must not bypass the conversion
            # budget just because no inference was reached.
            self._pose_gate.should_infer(pose_sample_time)
            self.get_logger().error(f"Cannot convert camera frame: {error}")
            self._publish_fall_candidates(
                PoseTrackingResult((), (), ()), message, [], "invalid_image", ()
            )
            return

        self._observe_person_pose(
            frame,
            image_message=message,
            now=pose_sample_time,
        )


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
