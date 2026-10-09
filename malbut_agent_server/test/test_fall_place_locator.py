"""Cloud box to map point: aligned depth + camera pose; unknown when either is missing."""

import numpy as np
import pytest

import math

from malbut_agent_server.application.fall_place_locator import (
    _rotate, FallPlaceLocator, FrameGeometry, map_point, map_point3d, mount_corrected,
)

# REP-103 optical (z forward, x right, y down) to a body/map frame (x forward, y left, z up).
OPTICAL = (-0.5, 0.5, -0.5, 0.5)


def geometry(depth_m=2.0, *, translation=(1.0, 1.0, 0.1), rotation=OPTICAL, shape=(100, 160)):
    depth = np.full(shape, int(depth_m * 1000), dtype=np.uint16)
    return FrameGeometry(depth, 500.0, 500.0, 320.0, 200.0, 640, 400, translation, rotation)


def test_box_centre_and_median_depth_give_the_map_point():
    x, y = map_point(geometry(), (.45, .4, .55, .6))
    assert (x, y) == pytest.approx((3.0, 1.0))  # 2 m straight ahead of (1, 1)


def test_box_to_the_right_is_to_the_robots_right():
    x, y = map_point(geometry(), (.85, .4, .95, .6))
    # u = 576 px, 256 px right of centre at 2 m: 1.024 m to the right (negative y).
    assert (x, y) == pytest.approx((3.0, 1.0 - 1.024))


def test_robot_heading_rotates_the_point():
    # Robot facing +y on the map: yaw 90 degrees applied after the optical rotation.
    yaw = (0.0, 0.0, 2 ** -.5, 2 ** -.5)

    def mul(a, b):
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        return (aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz)
    x, y = map_point(geometry(rotation=mul(yaw, OPTICAL), translation=(0, 0, 0)),
                     (.45, .4, .55, .6))
    assert (x, y) == pytest.approx((0.0, 2.0), abs=1e-9)


def test_invalid_or_too_little_depth_is_unknown_not_guessed():
    assert map_point(geometry(0.0), (.45, .4, .55, .6)) is None      # no return
    assert map_point(geometry(9.0), (.45, .4, .55, .6)) is None      # beyond range
    sparse = geometry()
    sparse.depth_mm[:, :] = 0
    sparse.depth_mm[50, 80] = 2000
    assert map_point(sparse, (.45, .4, .55, .6)) is None             # one sample only


def test_median_ignores_a_few_wall_pixels_behind_the_object():
    g = geometry()
    g.depth_mm[45:55, 75:85] = 1500
    g.depth_mm[45:47, 75:85] = 4500
    x, _ = map_point(g, (.45, .4, .55, .6))
    assert x == pytest.approx(2.5)


@pytest.mark.parametrize('change', [
    dict(rotation=(0, 0, 0, 2)), dict(translation=(0, float('nan'), 0)),
])
def test_bad_geometry_is_rejected(change):
    with pytest.raises(ValueError):
        geometry(**change)
    with pytest.raises(ValueError):
        FrameGeometry(np.zeros((4, 4), dtype=np.float32), 500, 500, 320, 200, 640, 400,
                      (0, 0, 0), OPTICAL)


def test_locator_keeps_recent_frames_by_monitor_time_only():
    locator = FallPlaceLocator(retention_s=60, max_frames=3)
    for t in (100, 101, 102):
        locator.add(t, geometry())
    assert locator.locate(101, (.45, .4, .55, .6)) == pytest.approx((3.0, 1.0))
    assert locator.locate(101.5, (.45, .4, .55, .6)) is None  # no nearby guessing
    locator.add(103, geometry())
    assert locator.locate(100, (.45, .4, .55, .6)) is None    # bounded
    locator.add(170, geometry())
    assert locator.locate(103, (.45, .4, .55, .6)) is None    # older than 60 s
    locator.clear()
    assert locator.locate(170, (.45, .4, .55, .6)) is None


def test_the_point_keeps_its_height_above_the_floor():
    x, y, z = map_point3d(geometry(translation=(1.0, 1.0, 0.12)), (.45, .1, .55, .3))
    # 120 px above the image centre at 2 m: 0.48 m up from the 0.12 m camera.
    assert (x, y, z) == pytest.approx((3.0, 1.0, 0.60))


def test_the_measured_mount_puts_the_floor_back_at_zero():
    """2026-10-09: 1.6 degrees down, left side low, 0.4 cm lower than the model."""
    pitch, roll, lower = math.radians(1.6), math.radians(-1.6), -0.004
    t_real, q_real = mount_corrected((0.0, 0.0, 0.12), OPTICAL,
                                     pitch_rad=pitch, roll_rad=roll, height_m=lower)
    assert _rotate(q_real, (0, 0, 1))[2] == pytest.approx(-math.sin(pitch), abs=1e-4)
    assert _rotate(q_real, (1, 0, 0))[2] == pytest.approx(-math.sin(roll), abs=1e-4)
    # The real camera sees a floor point 3 m ahead and 0.5 m to the right.
    columns = [_rotate(q_real, axis) for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
    offset = np.array((3.0, -0.5, 0.0)) - np.array(t_real)
    camera = np.array([np.dot(column, offset) for column in columns])
    u, v = 320 + 500 * camera[0] / camera[2], 200 + 500 * camera[1] / camera[2]
    depth = np.full((400, 640), int(round(camera[2] * 1000)), dtype=np.uint16)
    box = ((u - 8) / 640, (v - 8) / 400, (u + 8) / 640, (v + 8) / 400)
    seen = FrameGeometry(depth, 500.0, 500.0, 320.0, 200.0, 640, 400, t_real, q_real)
    model = FrameGeometry(depth, 500.0, 500.0, 320.0, 200.0, 640, 400, (0.0, 0.0, 0.12), OPTICAL)
    assert map_point3d(seen, box) == pytest.approx((3.0, -0.5, 0.0), abs=0.005)
    assert map_point3d(model, box)[2] > 0.05, 'uncorrected, the floor floats several cm'
