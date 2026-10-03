"""Tests for fall pose node configuration validation."""

from dataclasses import replace

from homecam_detector.config import DetectorConfig, validate_config


VALID = DetectorConfig(fall_runtime_id="runtime-1", pose_model_path="/models/pose.onnx")


def test_minimal_fall_config_is_valid() -> None:
    assert validate_config(VALID) == []


def test_runtime_id_and_pose_model_are_required() -> None:
    assert validate_config(DetectorConfig()) == [
        "fall_runtime_id is required",
        "pose_model_path is required",
    ]
    assert validate_config(replace(VALID, fall_runtime_id="  ")) == ["fall_runtime_id is required"]


def test_model_execution_settings_use_tuned_defaults_and_validate_overrides():
    default = VALID
    assert (default.pose_execution_provider, default.pose_intra_op_num_threads,
            default.pose_allow_spinning, default.pose_opencv_num_threads) == ('auto', 2, False, 1)
    assert validate_config(replace(
        default, pose_execution_provider='cuda',
        pose_intra_op_num_threads=2, pose_allow_spinning=False, pose_opencv_num_threads=1)) == []
    assert validate_config(replace(default, pose_execution_provider='auto')) == []
    for changes in ({'pose_execution_provider': 'invalid'}, {'pose_intra_op_num_threads': True},
                    {'pose_intra_op_num_threads': -1}, {'pose_opencv_num_threads': -1},
                    {'pose_allow_spinning': 'false'}, {'pose_intra_op_num_threads': 257}):
        assert validate_config(replace(default, **changes))


def test_navigation_status_topic_must_be_absolute_or_disabled() -> None:
    assert validate_config(
        replace(VALID, navigation_status_topic="navigate_to_pose/_action/status")
    ) == [
        "navigation_status_topic must be empty or an absolute ROS topic"
    ]
    assert validate_config(replace(VALID, navigation_status_topic="")) == []


def test_rejects_relative_image_topic() -> None:
    assert validate_config(replace(VALID, image_topic="relative")) == [
        "image_topic must be an absolute ROS topic"
    ]


def test_rejects_nan_and_infinite_motion_parameters() -> None:
    float_fields = [
        "pose_confidence_threshold",
        "pose_keypoint_threshold",
        "pose_inference_fps",
        "pose_candidate_confidence_threshold",
        "pose_track_max_gap_sec",
        "fall_temporal_window_sec",
        "fall_found_down_hold_sec",
        "fall_max_frame_gap_sec",
        "stationary_after_sec",
        "odom_timeout_sec",
        "linear_motion_threshold",
        "angular_motion_threshold",
        "depth_scale_m",
        "depth_max_stamp_delta_sec",
        "camera_height_m",
        "camera_pitch_rad",
    ]
    defaults = VALID
    for field in float_fields:
        assert validate_config(replace(defaults, **{field: float("nan")}))
        assert validate_config(replace(defaults, **{field: float("inf")}))


def test_pose_rate_is_bounded() -> None:
    assert validate_config(replace(VALID, pose_inference_fps=0.0)) == [
        "pose_inference_fps must be in (0, 30]"
    ]
    assert validate_config(replace(VALID, pose_inference_fps=30.1)) == [
        "pose_inference_fps must be in (0, 30]"
    ]


def test_pose_tracking_limits_and_threshold_order() -> None:
    for field, values in {
        "pose_candidate_confidence_threshold": (0, -0.1, 0.46),
        "pose_track_max_gap_sec": (0, -1),
        "pose_track_min_observations": (1, 0, True, 2.5),
        "pose_track_max_people": (0, 129, True, 2.5),
    }.items():
        for value in values:
            assert validate_config(replace(VALID, **{field: value}))


def test_fall_candidate_window_parameters_are_validated():
    for name in ("fall_temporal_window_sec", "fall_found_down_hold_sec", "fall_max_frame_gap_sec"):
        assert validate_config(replace(VALID, **{name: 0}))
    assert validate_config(replace(VALID, fall_found_down_hold_sec=3))
    assert validate_config(replace(VALID, fall_max_frame_gap_sec=3))


def test_depth_topics_must_be_paired_and_explicitly_aligned() -> None:
    assert validate_config(replace(VALID, depth_image_topic="relative"))
    assert validate_config(
        replace(VALID, depth_image_topic="/camera/depth/aligned")
    ) == [
        "depth_image_topic and depth_camera_info_topic must be set together"
    ]
    assert validate_config(replace(VALID, depth_aligned_to_rgb=True)) == [
        "depth_aligned_to_rgb requires configured depth topics"
    ]
    assert "explicitly aligned" in validate_config(
        replace(
            VALID,
            depth_image_topic="/camera/depth/aligned",
            depth_camera_info_topic="/camera/depth/camera_info",
        )
    )[0]
    assert validate_config(
        replace(
            VALID,
            depth_image_topic="/camera/depth/aligned",
            depth_camera_info_topic="/camera/depth/camera_info",
            depth_aligned_to_rgb=True,
        )
    ) == []
