"""Tests for preserving localization between SLAM and Nav2."""

import math
from pathlib import Path

from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import OccupancyGrid
import pytest

from malbut_gazebo.localization_handoff import (
    FORMAT,
    _boot_id,
    _compose,
    _load_state,
    select_record,
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
    record = select_record(value, _map_digest(_map()))
    assert value["format"] == FORMAT
    assert value["boot_id"] == boot_id
    assert record is not None
    assert record["map_to_odom"]["parent_frame"] == "map"
    assert record["map_to_odom"]["child_frame"] == "odom"
    assert record["map_to_odom"]["stamp_nanoseconds"] == 10_000_000_000


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


def test_remapping_does_not_evict_the_saved_map_record(tmp_path):
    """
    Cancelling a re-map must still restore the saved map's pose.

    One shared record meant the SLAM session overwrote the saved map's
    transform, so returning to the saved map refused the handoff and AMCL
    never received an initial pose.
    """
    path = tmp_path / "localization.yaml"
    boot_id = "81ad49b7-4125-4db0-965d-53d7a4ae8c71"
    saved_map = _map()
    slam_map = _map(values=(0, 0, 0, 100))

    _write_state(
        path, saved_map, _transform("map", "odom", (0.2, -0.3, 0.0), 0.1),
        boot_id=boot_id, pinned=True,
    )
    _write_state(
        path, slam_map, _transform("map", "odom", (0.0, 0.0, 0.0), 0.0),
        boot_id=boot_id,
    )
    value = _load_state(path)

    saved_record = select_record(value, _map_digest(saved_map))
    slam_record = select_record(value, _map_digest(slam_map))
    assert saved_record is not None
    assert saved_record["map_to_odom"]["translation"]["x"] == 0.2
    assert slam_record is not None
    assert slam_record["map_to_odom"]["translation"]["x"] == 0.0


def test_a_new_odom_session_drops_every_earlier_record(tmp_path):
    """Records only compose while the odometry source stays alive."""
    path = tmp_path / "localization.yaml"
    saved_map = _map()
    _write_state(
        path, saved_map, _transform("map", "odom", (0.2, -0.3, 0.0), 0.1),
        boot_id="81ad49b7-4125-4db0-965d-53d7a4ae8c71", pinned=True,
    )
    _write_state(
        path, _map(values=(0, 0, 0, 100)),
        _transform("map", "odom", (0.0, 0.0, 0.0), 0.0),
        boot_id="0f2f2f2f-4125-4db0-965d-53d7a4ae8c71",
    )
    value = _load_state(path)

    assert select_record(value, _map_digest(saved_map)) is None
    assert len(value["records"]) == 1


def test_unknown_map_has_no_record(tmp_path):
    path = tmp_path / "localization.yaml"
    _write_state(
        path, _map(), _transform("map", "odom", (0.2, -0.3, 0.0), 0.1),
        boot_id="81ad49b7-4125-4db0-965d-53d7a4ae8c71",
    )

    assert select_record(_load_state(path), "0" * 64) is None


def test_navigation_launch_records_the_saved_map_transform():
    """
    Saved-map navigation must record map->odom, not only SLAM.

    The recorder lived only in the SLAM launches, so no transform for the
    saved map ever existed. Cancelling a re-map then had nothing to
    restore and AMCL waited for an initial pose forever.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "launch"
        / "navigation.launch.py"
    ).read_text(encoding="utf-8")

    assert source.count("'record_localization_state'") == 1
    assert "condition=IfCondition(use_static_map)," in source
    assert source.count("localization_recorder,") == 1


def test_a_growing_slam_map_never_evicts_the_pinned_saved_map(tmp_path):
    """
    A SLAM map changes digest on every update while it grows.

    Rotating records by recency alone let one mapping session push the
    saved map out within a minute, so cancelling the re-map again had
    nothing to restore.
    """
    path = tmp_path / "localization.yaml"
    boot_id = "81ad49b7-4125-4db0-965d-53d7a4ae8c71"
    saved_map = _map()
    _write_state(
        path, saved_map, _transform("map", "odom", (0.2, -0.3, 0.0), 0.1),
        boot_id=boot_id, pinned=True,
    )

    for step in range(30):
        _write_state(
            path,
            _map(values=(0, step % 100, -1, 0)),
            _transform("map", "odom", (0.0, 0.0, 0.0), 0.0),
            boot_id=boot_id,
        )

    value = _load_state(path)
    assert select_record(value, _map_digest(saved_map)) is not None
    assert len(value["records"]) == 2


def test_navigation_launch_pins_its_recorded_transform():
    """Only saved-map navigation may keep its record across a re-map."""
    source = (
        Path(__file__).resolve().parents[1]
        / "launch"
        / "navigation.launch.py"
    ).read_text(encoding="utf-8")

    assert "'pinned': True," in source
