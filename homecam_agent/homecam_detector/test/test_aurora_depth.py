"""Unit tests for Aurora930 depth evidence without ROS or hardware."""

import numpy as np
import pytest

from homecam_detector.aurora_depth import (
    AuroraDepthEvidenceExtractor,
    CameraIntrinsics,
)
from homecam_detector.pose import PersonPose, PoseKeypoint


def _pose(y: float) -> PersonPose:
    points = tuple(
        PoseKeypoint(name=name, x=0.5, y=y, confidence=0.95)
        for name in (
            'nose',
            'left_shoulder',
            'right_shoulder',
            'left_hip',
            'right_hip',
        )
    )
    return PersonPose(
        box_confidence=0.95,
        box=(0.4, 0.2, 0.6, 0.8),
        keypoints=points,
        visible_keypoints=len(points),
    )


def _intrinsics() -> CameraIntrinsics:
    return CameraIntrinsics(
        width=640,
        height=400,
        fx=500.0,
        fy=500.0,
        cx=319.5,
        cy=199.5,
    )


def test_extracts_near_floor_torso_from_aligned_millimeter_depth() -> None:
    image = np.full((400, 640), 1000, dtype=np.uint16)
    result = AuroraDepthEvidenceExtractor().extract(
        depth_image=image,
        intrinsics=_intrinsics(),
        pose=_pose(0.5),
        rgb_stamp_s=10.0,
        depth_stamp_s=10.05,
        aligned_to_rgb=True,
    )

    assert result.usable is True
    assert result.sampled_torso_points == 5
    assert result.person_distance_m == pytest.approx(1.0)
    # Normalized 0.5 maps to integer row 200 while cy is 199.5, so the
    # projected point is 1 mm below the optical centre at one metre.
    assert result.torso_floor_distance_m == pytest.approx(0.090864)
    assert result.floor_proximity_score > 0.7


def test_high_torso_is_not_a_floor_proximity_signal() -> None:
    image = np.full((400, 640), 2000, dtype=np.uint16)
    result = AuroraDepthEvidenceExtractor().extract(
        depth_image=image,
        intrinsics=_intrinsics(),
        pose=_pose(0.25),
        rgb_stamp_s=1.0,
        depth_stamp_s=1.0,
        aligned_to_rgb=True,
    )

    assert result.torso_floor_distance_m > 0.45
    assert result.floor_proximity_score == 0.0


@pytest.mark.parametrize(
    ('aligned', 'rgb_stamp', 'depth_stamp', 'expected_stale'),
    [
        (False, 1.0, 1.0, False),
        (True, 1.0, 1.3, True),
    ],
)
def test_unaligned_or_stale_depth_fails_closed(
    aligned: bool,
    rgb_stamp: float,
    depth_stamp: float,
    expected_stale: bool,
) -> None:
    image = np.full((400, 640), 1000, dtype=np.uint16)
    result = AuroraDepthEvidenceExtractor().extract(
        depth_image=image,
        intrinsics=_intrinsics(),
        pose=_pose(0.5),
        rgb_stamp_s=rgb_stamp,
        depth_stamp_s=depth_stamp,
        aligned_to_rgb=aligned,
    )

    assert result.usable is False
    assert result.stale is expected_stale
    assert result.torso_floor_distance_m is None


def test_invalid_depth_values_are_ignored() -> None:
    image = np.zeros((400, 640), dtype=np.uint16)
    result = AuroraDepthEvidenceExtractor().extract(
        depth_image=image,
        intrinsics=_intrinsics(),
        pose=_pose(0.5),
        rgb_stamp_s=1.0,
        depth_stamp_s=1.0,
        aligned_to_rgb=True,
    )

    assert result.usable is False
    assert result.valid_torso_ratio == 0.0


def test_projection_below_floor_rejects_bad_camera_calibration() -> None:
    image = np.full((400, 640), 1000, dtype=np.uint16)
    extractor = AuroraDepthEvidenceExtractor(camera_pitch_rad=0.0)

    with pytest.raises(ValueError, match='below floor'):
        extractor.extract(
            depth_image=image,
            intrinsics=_intrinsics(),
            pose=_pose(0.8),
            rgb_stamp_s=1.0,
            depth_stamp_s=1.0,
            aligned_to_rgb=True,
        )
