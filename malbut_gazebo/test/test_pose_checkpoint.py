"""Tests for verified pose persistence and map-revision isolation."""

from array import array
import json
from pathlib import Path

import numpy as np
import pytest

from malbut_gazebo.pose_checkpoint import (
    POSE_CHECKPOINT_FILE,
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
