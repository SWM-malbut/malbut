"""Tests for detector configuration validation."""

from dataclasses import replace

from homecam_detector.config import (
    DetectorConfig,
    is_allowed_backend_url,
    is_valid_device_id,
    validate_config,
)


def test_default_config_is_valid() -> None:
    assert validate_config(DetectorConfig()) == []


def test_navigation_status_topic_must_be_absolute_or_disabled() -> None:
    assert validate_config(
        DetectorConfig(navigation_status_topic="navigate_to_pose/_action/status")
    ) == [
        "navigation_status_topic must be empty or an absolute ROS topic"
    ]
    assert validate_config(DetectorConfig(navigation_status_topic="")) == []


def test_rejects_unsafe_backend_and_bad_thresholds() -> None:
    config = DetectorConfig(
        image_topic="relative",
        backend_url="http://public.example.test",
        confidence_threshold=0.0,
        consecutive_frames=0,
        max_frame_gap_sec=0.0,
        motion_area_ratio=2.0,
    )
    errors = validate_config(config)
    assert len(errors) >= 6


def test_rejects_nan_and_infinite_motion_parameters() -> None:
    float_fields = [
        "confidence_threshold",
        "pose_confidence_threshold",
        "pose_keypoint_threshold",
        "pose_inference_fps",
        "event_cooldown_sec",
        "max_frame_gap_sec",
        "stationary_after_sec",
        "odom_timeout_sec",
        "linear_motion_threshold",
        "angular_motion_threshold",
        "motion_area_ratio",
    ]
    defaults = DetectorConfig()
    for field in float_fields:
        assert validate_config(replace(defaults, **{field: float("nan")}))
        assert validate_config(replace(defaults, **{field: float("inf")}))


def test_pose_rate_is_bounded() -> None:
    assert validate_config(DetectorConfig(pose_inference_fps=0.0)) == [
        "pose_inference_fps must be in (0, 30]"
    ]
    assert validate_config(DetectorConfig(pose_inference_fps=30.1)) == [
        "pose_inference_fps must be in (0, 30]"
    ]


def test_allows_local_development_http() -> None:
    config = DetectorConfig(
        backend_url="http://localhost:3000",
        device_id="local-device",
    )
    assert validate_config(config) == []


def test_plaintext_http_allows_only_exact_loopback_hosts() -> None:
    allowed = [
        "http://localhost",
        "http://LOCALHOST:3000/api",
        "http://127.0.0.1:8080",
        "http://[::1]:3000",
    ]
    rejected = [
        "http://localhost.evil",
        "http://localhost@evil.example",
        "http://127.0.0.1.evil",
        "http://[::1].evil",
        "http://[::1]evil",
        "http://localhost:",
        "http://localhost:70000",
        "http://localhost:0",
        "http://192.168.0.10",
        "http://example.com",
    ]
    assert all(is_allowed_backend_url(value) for value in allowed)
    assert not any(is_allowed_backend_url(value) for value in rejected)


def test_https_requires_host_and_rejects_userinfo() -> None:
    assert is_allowed_backend_url("https://homecam.example.test")
    assert is_allowed_backend_url("HTTPS://homecam.example.test:443/api")
    assert not is_allowed_backend_url("https://")
    assert not is_allowed_backend_url("https://user@homecam.example.test")
    assert not is_allowed_backend_url("ftp://homecam.example.test")


def test_backend_device_id_matches_broker_contract() -> None:
    assert is_valid_device_id("gazebo-homecam:sim")
    assert is_valid_device_id("A")
    assert not is_valid_device_id(".leading-dot")
    assert not is_valid_device_id("contains space")
    assert not is_valid_device_id("a" * 129)
    config = DetectorConfig(
        backend_url="https://homecam.example.test",
        device_id=".invalid",
    )
    assert validate_config(config)
