"""Configuration types and validation for the detector node."""

from dataclasses import dataclass
import math
import re
from typing import List
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DetectorConfig:
    """Runtime configuration independent of ROS parameter plumbing."""

    image_topic: str = "/depth_cam/depth_cam"
    odom_topic: str = "/odom"
    model_path: str = ""
    device_id: str = ""
    backend_url: str = ""
    confidence_threshold: float = 0.45
    consecutive_frames: int = 3
    event_cooldown_sec: float = 30.0
    event_confirmation_window_frames: int = 5
    event_confirmation_required_frames: int = 3
    event_pre_roll_sec: float = 5.0
    event_merge_gap_sec: float = 10.0
    max_event_clip_sec: float = 120.0
    event_clips_enabled: bool = False
    max_frame_gap_sec: float = 1.0
    stationary_after_sec: float = 1.0
    odom_timeout_sec: float = 2.0
    linear_motion_threshold: float = 0.03
    angular_motion_threshold: float = 0.05
    motion_area_ratio: float = 0.02
    monitoring_enabled: bool = False


def is_allowed_backend_url(value: str) -> bool:
    """Allow HTTPS, or plaintext only to an exact loopback hostname."""
    if not value or any(character.isspace() or character == "\\" for character in value):
        return False
    try:
        parsed = urlsplit(value)
        # Accessing port performs urllib's range and numeric validation.
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if not parsed.netloc or parsed.hostname is None:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if port is not None and port == 0:
        return False
    normalized_authority = (
        f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    )
    if port is not None:
        normalized_authority += f":{port}"
    if parsed.netloc.lower() != normalized_authority.lower():
        return False
    if parsed.scheme.lower() == "https":
        return True
    return parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}


def is_valid_device_id(value: str) -> bool:
    """Match the device identity syntax shared with the backend broker."""
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is not None


def validate_config(config: DetectorConfig) -> List[str]:
    """Return every actionable configuration error."""
    errors: List[str] = []
    if not config.image_topic.startswith("/"):
        errors.append("image_topic must be an absolute ROS topic")
    if config.odom_topic and not config.odom_topic.startswith("/"):
        errors.append("odom_topic must be empty or an absolute ROS topic")
    if (
        not math.isfinite(config.confidence_threshold)
        or not 0.0 < config.confidence_threshold <= 1.0
    ):
        errors.append("confidence_threshold must be in (0, 1]")
    if config.consecutive_frames < 1:
        errors.append("consecutive_frames must be at least 1")
    if (
        not math.isfinite(config.event_cooldown_sec)
        or config.event_cooldown_sec < 0.0
    ):
        errors.append("event_cooldown_sec must be non-negative")
    if config.event_confirmation_window_frames < 1:
        errors.append("event_confirmation_window_frames must be at least 1")
    if not (
        1
        <= config.event_confirmation_required_frames
        <= config.event_confirmation_window_frames
    ):
        errors.append(
            "event_confirmation_required_frames must be within the window"
        )
    for name, value in (
        ("event_pre_roll_sec", config.event_pre_roll_sec),
        ("event_merge_gap_sec", config.event_merge_gap_sec),
        ("max_event_clip_sec", config.max_event_clip_sec),
    ):
        if not math.isfinite(value) or value < 0.0:
            errors.append(f"{name} must be finite and non-negative")
    if config.max_event_clip_sec <= config.event_pre_roll_sec:
        errors.append("max_event_clip_sec must be greater than event_pre_roll_sec")
    if (
        not math.isfinite(config.max_frame_gap_sec)
        or config.max_frame_gap_sec <= 0.0
    ):
        errors.append("max_frame_gap_sec must be positive")
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
    if (
        not math.isfinite(config.motion_area_ratio)
        or not 0.0 < config.motion_area_ratio <= 1.0
    ):
        errors.append("motion_area_ratio must be in (0, 1]")
    if config.backend_url:
        if not is_allowed_backend_url(config.backend_url):
            errors.append(
                "backend_url must use HTTPS; plaintext HTTP is accepted only "
                "for the exact localhost, 127.0.0.1, or [::1] hostname"
            )
        if not is_valid_device_id(config.device_id):
            errors.append(
                "device_id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127} "
                "when backend_url is configured"
            )
    return errors
