"""Tests for verified pose persistence and map-revision isolation."""

from array import array
import json
from pathlib import Path

import numpy as np
import pytest

from malbut_gazebo.pose_checkpoint import (
    MIN_SAFE_CHECKPOINT_CLEARANCE_M,
    POSE_CHECKPOINT_FILE,
    PoseSafetyGrid,
    acceptable_amcl_covariance,
    load_pose_checkpoint,
    persist_pose_checkpoint,
)


def _active() -> dict:
    return {"map_id": "home-map", "map_revision": "revision-7"}


def test_checkpoint_is_separate_from_map_origin_and_round_trips(
    tmp_path: Path,
):
    """A runtime pose must not mutate the immutable map manifest."""
    active = _active()
    saved = persist_pose_checkpoint(
        tmp_path,
        active,
        {"x": 1.25, "y": -0.5, "yaw": 0.75},
        boot_id="boot-a",
        observed_at="2026-08-19T01:02:03+00:00",
    )

    assert not (tmp_path / "active.json").exists()
    assert saved["validation"] == "verified"
    assert load_pose_checkpoint(tmp_path, active) == saved


def test_checkpoint_from_another_map_or_with_invalid_pose_is_rejected(
    tmp_path: Path,
):
    """Only a finite checkpoint for the exact active map may be restored."""
    persist_pose_checkpoint(
        tmp_path,
        _active(),
        {"x": 1.0, "y": 2.0, "yaw": 0.25},
        boot_id="boot-a",
    )

    assert load_pose_checkpoint(
        tmp_path,
        {"map_id": "home-map", "map_revision": "revision-8"},
    ) is None

    path = tmp_path / POSE_CHECKPOINT_FILE
    value = json.loads(path.read_text(encoding="utf-8"))
    value["pose"]["x"] = "NaN"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert load_pose_checkpoint(tmp_path, _active()) is None


def test_checkpoint_writer_rejects_non_finite_pose(tmp_path: Path):
    """Non-finite coordinates must never enter a future simulator spawn."""
    with pytest.raises(ValueError, match="finite pose"):
        persist_pose_checkpoint(
            tmp_path,
            _active(),
            {"x": float("nan"), "y": 0.0, "yaw": 0.0},
            boot_id="boot-a",
        )


def test_pose_safety_grid_rejects_obstacle_adjacent_boot_pose(
    tmp_path: Path,
):
    """A valid checkpoint coordinate still needs robot-sized map clearance."""
    image = np.full((20, 20), 254, dtype=np.uint8)
    image[[0, -1], :] = 0
    image[:, [0, -1]] = 0
    image[10, 10] = 205
    pgm = tmp_path / "map.pgm"
    pgm.write_bytes(
        b"P5\n20 20\n255\n" + image.tobytes()
    )
    yaml_path = tmp_path / "map.yaml"
    yaml_path.write_text(
        "\n".join((
            "image: map.pgm",
            "mode: trinary",
            "resolution: 0.1",
            "origin: [0.0, 0.0, 0.0]",
            "negate: 0",
            "occupied_thresh: 0.65",
            "free_thresh: 0.196",
        )),
        encoding="utf-8",
    )

    safety = PoseSafetyGrid.load(yaml_path)

    assert safety.accepts({"x": 0.55, "y": 0.55, "yaw": 0.0})
    assert not safety.accepts({"x": 0.15, "y": 0.95, "yaw": 0.0})
    assert not safety.accepts({"x": 1.05, "y": 0.95, "yaw": 0.0})
    assert not safety.accepts({"x": -1.0, "y": 0.0, "yaw": 0.0})
    assert MIN_SAFE_CHECKPOINT_CLEARANCE_M == 0.30


def test_boot_revalidation_requires_bounded_amcl_uncertainty():
    """A fresh TF alone must not promote an uncertain AMCL candidate."""
    covariance = [0.0] * 36
    covariance[0] = 0.25
    covariance[7] = 0.25
    covariance[35] = 0.275
    assert acceptable_amcl_covariance(covariance)
    assert acceptable_amcl_covariance(array("d", covariance))
    assert acceptable_amcl_covariance(np.asarray(covariance))

    covariance[0] = 0.251
    assert not acceptable_amcl_covariance(covariance)
    covariance[0] = float("nan")
    assert not acceptable_amcl_covariance(covariance)
