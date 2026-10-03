"""Configuration types and validation for the fall pose node."""

from dataclasses import dataclass
import math
from typing import List


EXECUTION_PARAMETER_NAMES = (
    'pose_execution_provider', 'pose_intra_op_num_threads', 'pose_allow_spinning',
    'pose_opencv_num_threads',
)


@dataclass(frozen=True)
class DetectorConfig:
    """Runtime configuration independent of ROS parameter plumbing."""

    image_topic: str = "/depth_cam/depth_cam"
    depth_image_topic: str = ""
    depth_camera_info_topic: str = ""
    depth_aligned_to_rgb: bool = False
    depth_scale_m: float = 0.0
    depth_max_stamp_delta_sec: float = 0.15
    camera_height_m: float = 0.091864
    camera_pitch_rad: float = 0.0
    odom_topic: str = "/odom"
    navigation_status_topic: str = "/navigate_to_pose/_action/status"
    pose_model_path: str = ""
    pose_keep_aspect: bool = False
    pose_execution_provider: str = 'auto'
    pose_intra_op_num_threads: int = 2
    pose_allow_spinning: bool = False
    # OpenCV's setting is process-wide.
    pose_opencv_num_threads: int = 1  # Legacy parameter name; 0 preserves library defaults.
    fall_runtime_id: str = ""
    pose_confidence_threshold: float = 0.45
    pose_keypoint_threshold: float = 0.5
    pose_inference_fps: float = 5.0
    pose_candidate_confidence_threshold: float = 0.10
    pose_track_max_gap_sec: float = 1.0
    pose_track_min_observations: int = 3
    pose_track_max_people: int = 32
    fall_temporal_window_sec: float = 2.0
    fall_found_down_hold_sec: float = 0.6
    fall_max_frame_gap_sec: float = 0.5
    stationary_after_sec: float = 2.0
    odom_timeout_sec: float = 2.0
    linear_motion_threshold: float = 0.03
    angular_motion_threshold: float = 0.05


def validate_config(config: DetectorConfig) -> List[str]:
    """Return every actionable configuration error."""
    errors: List[str] = []
    if config.pose_execution_provider not in ('auto', 'cpu', 'cuda'):
        errors.append('pose_execution_provider must be auto, cpu or cuda')
    threads = config.pose_intra_op_num_threads
    if type(threads) is not int or not 0 <= threads <= 256:
        errors.append('pose_intra_op_num_threads must be an integer in [0, 256]')
    if type(config.pose_allow_spinning) is not bool:
        errors.append('pose_allow_spinning must be bool')
    if (type(config.pose_opencv_num_threads) is not int
            or not 0 <= config.pose_opencv_num_threads <= 256):
        errors.append('pose_opencv_num_threads must be an integer in [0, 256]')
    if not config.fall_runtime_id.strip():
        errors.append("fall_runtime_id is required")
    if not config.pose_model_path:
        errors.append("pose_model_path is required")
    if not config.image_topic.startswith("/"):
        errors.append("image_topic must be an absolute ROS topic")
    for name, value in (
        ("depth_image_topic", config.depth_image_topic),
        ("depth_camera_info_topic", config.depth_camera_info_topic),
    ):
        if value and not value.startswith("/"):
            errors.append(f"{name} must be empty or an absolute ROS topic")
    if bool(config.depth_image_topic) != bool(config.depth_camera_info_topic):
        errors.append(
            "depth_image_topic and depth_camera_info_topic must be set together"
        )
    if config.depth_aligned_to_rgb and not config.depth_image_topic:
        errors.append("depth_aligned_to_rgb requires configured depth topics")
    if (
        config.depth_image_topic
        and config.depth_camera_info_topic
        and not config.depth_aligned_to_rgb
    ):
        errors.append(
            "configured depth topics must be explicitly aligned to RGB"
        )
    if (
        not math.isfinite(config.depth_scale_m)
        or config.depth_scale_m < 0.0
    ):
        errors.append("depth_scale_m must be zero (auto) or positive")
    if (
        not math.isfinite(config.depth_max_stamp_delta_sec)
        or config.depth_max_stamp_delta_sec <= 0.0
    ):
        errors.append("depth_max_stamp_delta_sec must be positive")
    if (
        not math.isfinite(config.camera_height_m)
        or config.camera_height_m <= 0.0
    ):
        errors.append("camera_height_m must be positive")
    if not math.isfinite(config.camera_pitch_rad):
        errors.append("camera_pitch_rad must be finite")
    if config.odom_topic and not config.odom_topic.startswith("/"):
        errors.append("odom_topic must be empty or an absolute ROS topic")
    if (
        config.navigation_status_topic
        and not config.navigation_status_topic.startswith("/")
    ):
        errors.append(
            "navigation_status_topic must be empty or an absolute ROS topic"
        )
    for name, value in (
        ("pose_confidence_threshold", config.pose_confidence_threshold),
        ("pose_keypoint_threshold", config.pose_keypoint_threshold),
    ):
        if not math.isfinite(value) or not 0.0 < value <= 1.0:
            errors.append(f"{name} must be in (0, 1]")
    if (
        not math.isfinite(config.pose_inference_fps)
        or not 0.0 < config.pose_inference_fps <= 30.0
    ):
        errors.append("pose_inference_fps must be in (0, 30]")
    if (not math.isfinite(config.pose_candidate_confidence_threshold)
            or not 0 < config.pose_candidate_confidence_threshold
            <= config.pose_confidence_threshold):
        errors.append("pose candidate threshold must be in (0, pose_confidence_threshold]")
    if (not math.isfinite(config.pose_track_max_gap_sec)
            or config.pose_track_max_gap_sec <= 0):
        errors.append("pose_track_max_gap_sec must be positive")
    if (type(config.pose_track_min_observations) is not int
            or config.pose_track_min_observations < 2):
        errors.append("pose_track_min_observations must be an integer >= 2")
    if (type(config.pose_track_max_people) is not int
            or not 1 <= config.pose_track_max_people <= 128):
        errors.append("pose_track_max_people must be an integer in [1, 128]")
    for name in ("fall_temporal_window_sec", "fall_found_down_hold_sec", "fall_max_frame_gap_sec"):
        value = getattr(config, name)
        if not math.isfinite(value) or value <= 0:
            errors.append(f"{name} must be finite and positive")
    if config.fall_found_down_hold_sec > config.fall_temporal_window_sec:
        errors.append("fall_found_down_hold_sec must not exceed fall_temporal_window_sec")
    if config.fall_max_frame_gap_sec > config.fall_temporal_window_sec:
        errors.append("fall_max_frame_gap_sec must not exceed fall_temporal_window_sec")
    if (
        not math.isfinite(config.stationary_after_sec)
        or config.stationary_after_sec < 0.0
    ):
        errors.append("stationary_after_sec must be non-negative")
    if (
        not math.isfinite(config.odom_timeout_sec)
        or config.odom_timeout_sec <= 0.0
    ):
        errors.append("odom_timeout_sec must be positive")
    if (
        not math.isfinite(config.linear_motion_threshold)
        or config.linear_motion_threshold < 0.0
    ):
        errors.append("linear_motion_threshold must be non-negative")
    if (
        not math.isfinite(config.angular_motion_threshold)
        or config.angular_motion_threshold < 0.0
    ):
        errors.append("angular_motion_threshold must be non-negative")
    return errors
