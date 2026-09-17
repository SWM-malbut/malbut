"""Tests for robust depth filtering and pinhole projection."""

import numpy as np
import pytest

from malbut_tracking.depth.projector import (
    CameraIntrinsics,
    project_pixel,
    projected_box_size,
)
from malbut_tracking.depth.roi_depth import estimate_roi_depth
from malbut_tracking.depth.observation import BoundingBox
from malbut_tracking.depth import roi_depth


def test_16bit_depth_is_converted_to_metres_and_outliers_are_ignored():
    depth = np.full((20, 20), 1500, dtype=np.uint16)
    depth[8:10, 8:10] = 0
    depth[10, 10] = 65000
    estimate = estimate_roi_depth(
        depth,
        '16UC1',
        BoundingBox(2.0, 2.0, 18.0, 18.0),
        roi_scale=0.5,
        minimum_depth_m=0.3,
        maximum_depth_m=3.0,
        minimum_samples=10,
    )
    assert estimate is not None
    assert np.isclose(estimate.distance_m, 1.5)
    assert estimate.dispersion_m == 0.0


def test_invalid_depth_roi_returns_none():
    depth = np.full((20, 20), np.nan, dtype=np.float32)
    estimate = estimate_roi_depth(
        depth,
        '32FC1',
        BoundingBox(2.0, 2.0, 18.0, 18.0),
        minimum_samples=1,
    )
    assert estimate is None


@pytest.mark.parametrize('encoding,value,scale', [
    ('16UC1', 1500, 1.0),
    ('32FC1', 1.5, 1.0),
    ('custom', 15.0, 0.1),
])
def test_depth_conversion_only_visits_the_selected_roi(
    monkeypatch, encoding, value, scale,
):
    dtype = np.uint16 if encoding == '16UC1' else np.float32
    depth = np.full((480, 640), value, dtype=dtype)
    converted_shapes = []
    convert = roi_depth.depth_image_to_metres

    def record_roi(image, encoding, fallback_scale):
        converted_shapes.append(image.shape)
        assert np.shares_memory(image, depth)
        return convert(image, encoding, fallback_scale)

    monkeypatch.setattr(roi_depth, 'depth_image_to_metres', record_roi)
    estimate = estimate_roi_depth(
        depth, encoding, BoundingBox(100, 120, 140, 200),
        roi_scale=0.5, fallback_scale=scale,
    )
    assert converted_shapes == [(40, 20)]
    assert estimate.distance_m == pytest.approx(1.5)
    assert estimate.dispersion_m == 0.0
    assert estimate.sample_count == 800


def test_depth_rejects_multichannel_images_before_cropping():
    with pytest.raises(ValueError, match='single channel'):
        estimate_roi_depth(
            np.ones((10, 10, 3)), '32FC1', BoundingBox(1, 1, 9, 9),
        )


def test_camera_projection_uses_optical_frame_convention():
    intrinsics = CameraIntrinsics(
        fx=500.0,
        fy=500.0,
        cx=320.0,
        cy=200.0,
        width=640,
        height=400,
    )
    assert project_pixel(intrinsics, 320.0, 200.0, 2.0) == (0.0, 0.0, 2.0)
    assert project_pixel(intrinsics, 420.0, 250.0, 2.0) == (0.4, 0.2, 2.0)
    assert projected_box_size(
        intrinsics,
        BoundingBox(270.0, 100.0, 370.0, 300.0),
        2.0,
    ) == (0.4, 0.8, 0.35)
