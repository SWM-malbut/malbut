"""Tests for preserving localization between SLAM and Nav2."""

import math

from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import OccupancyGrid
import pytest

from malbut_gazebo.localization_handoff import (
    FORMAT,
    _boot_id,
    _compose,
    _load_state,
    _map_digest,
    _same_odom_session,
    _write_state,
)


def _map(values=(0, 100, -1, 0)):
    message = OccupancyGrid()
    message.header.frame_id = "map"
    message.info.width = 2
    message.info.height = 2
    message.info.resolution = 0.05
    message.info.origin.position.x = -1.0
    message.info.origin.position.y = -2.0
    message.info.origin.orientation.w = 1.0
    message.data = list(values)
    return message


def _transform(parent, child, translation, yaw, stamp=10):
    message = TransformStamped()
    message.header.frame_id = parent
    message.child_frame_id = child
    message.header.stamp.sec = stamp
    message.transform.translation.x = translation[0]
    message.transform.translation.y = translation[1]
    message.transform.translation.z = translation[2]
    message.transform.rotation.z = math.sin(yaw / 2.0)
    message.transform.rotation.w = math.cos(yaw / 2.0)
    return message


def test_map_digest_ignores_time_but_detects_different_cells():
    """A handoff must match map contents, not only a file name."""
    first = _map()
    second = _map()
    second.header.stamp.sec = 99
    assert _map_digest(first) == _map_digest(second)
    second.data[0] = 100
    assert _map_digest(first) != _map_digest(second)


def test_saved_map_to_odom_composes_with_current_robot_pose():
    """Current odometry must be expressed in the preserved map frame."""
    saved = {
        "translation": {"x": 1.0, "y": 2.0, "z": 0.0},
        "rotation": {
            "x": 0.0,
            "y": 0.0,
            "z": math.sin(math.pi / 4.0),
            "w": math.cos(math.pi / 4.0),
        },
    }
    odom_to_base = _transform(
        "odom", "base_footprint", (2.0, 0.0, 0.0), 0.0
    )
    composed = _compose(saved, odom_to_base)
    assert composed["translation"] == pytest.approx((1.0, 4.0, 0.0))
    assert composed["rotation"] == pytest.approx((
        0.0,
        0.0,
        math.sin(math.pi / 4.0),
        math.cos(math.pi / 4.0),
    ))


def test_state_file_records_map_identity_and_transform(tmp_path):
    """The recorder must persist enough information for safe restoration."""
    path = tmp_path / "localization.yaml"
    map_to_odom = _transform("map", "odom", (0.2, -0.3, 0.0), 0.1)
    boot_id = "81ad49b7-4125-4db0-965d-53d7a4ae8c71"
    _write_state(path, _map(), map_to_odom, boot_id=boot_id)
    value = _load_state(path)
    assert value["format"] == FORMAT
    assert value["boot_id"] == boot_id
    assert value["map_digest"] == _map_digest(_map())
    assert value["map_to_odom"]["parent_frame"] == "map"
    assert value["map_to_odom"]["child_frame"] == "odom"
    assert value["map_to_odom"]["stamp_nanoseconds"] == 10_000_000_000


def test_odom_session_requires_same_boot_and_non_reset_clock():
    """A power cycle or reset odometry clock invalidates the handoff."""
    saved = 120_000_000_000
    boot_id = "81ad49b7-4125-4db0-965d-53d7a4ae8c71"
    other_boot = "f9dd6021-79f5-42d5-afbb-b3d92d6a9f10"
    assert _same_odom_session(saved, 119_000_000_000, boot_id, boot_id)
    assert not _same_odom_session(saved, 3_000_000_000, boot_id, boot_id)
    assert not _same_odom_session(
        saved, 121_000_000_000, boot_id, other_boot
    )


def test_boot_id_is_normalized_and_invalid_value_is_rejected(tmp_path):
    """Boot identity parsing must fail closed on malformed state."""
    path = tmp_path / "boot_id"
    path.write_text(
        "81AD49B7-4125-4DB0-965D-53D7A4AE8C71\n", encoding="utf-8"
    )
    assert _boot_id(path) == "81ad49b7-4125-4db0-965d-53d7a4ae8c71"
    path.write_text("not-a-uuid\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _boot_id(path)
