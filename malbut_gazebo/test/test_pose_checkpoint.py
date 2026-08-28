"""Tests for verified pose persistence and map-revision isolation."""

from array import array
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_gazebo.pose_checkpoint import (
    MIN_SAFE_CHECKPOINT_CLEARANCE_M,
    POSE_CHECKPOINT_FILE,
    STABLE_SAMPLE_COUNT,
    PoseCheckpointNode,
    PoseSafetyGrid,
    acceptable_amcl_covariance,
    load_pose_checkpoint,
    persist_pose_checkpoint,
)


def _active() -> dict:
    return {"map_id": "home-map", "map_revision": "revision-7"}


class _ControlledPoseSafety:
    """Record sampled poses while allowing the test to change clearance."""

    def __init__(self, accepts: bool) -> None:
        self.accepts_pose = accepts
        self.checked_poses: list[dict[str, float]] = []

    def accepts(self, pose: dict[str, float]) -> bool:
        self.checked_poses.append(dict(pose))
        return self.accepts_pose


class _ControlledTransformBuffer:
    """Return one fresh, mutable map-to-base transform."""

    def __init__(self) -> None:
        self.x = 0.1
        self.y = 0.1

    def lookup_transform(self, *_arguments) -> SimpleNamespace:
        return SimpleNamespace(
            header=SimpleNamespace(
                stamp=SimpleNamespace(sec=10, nanosec=0),
            ),
            transform=SimpleNamespace(
                translation=SimpleNamespace(x=self.x, y=self.y),
                rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
            ),
        )


def _sampling_harness(*, accepts_pose: bool) -> tuple:
    """Build only the state consumed by ``PoseCheckpointNode._sample``."""
    safety = _ControlledPoseSafety(accepts_pose)
    transforms = _ControlledTransformBuffer()
    validation_events: list[str] = []
    checkpoint_poses: list[dict[str, float]] = []
    node = SimpleNamespace(
        initially_trusted=True,
        proposal_received=True,
        amcl_active=True,
        stable_samples=STABLE_SAMPLE_COUNT - 1,
        validation_state="verifying",
        latest_verified_pose=None,
        pose_safety=safety,
        tf_buffer=transforms,
        get_clock=lambda: SimpleNamespace(
            now=lambda: SimpleNamespace(nanoseconds=10_000_000_000),
        ),
    )

    def set_validation(value: str) -> None:
        if value != node.validation_state:
            node.validation_state = value
            validation_events.append(value)

    node._set_validation = set_validation
    node._write_if_due = lambda pose: checkpoint_poses.append(dict(pose))
    return node, safety, transforms, validation_events, checkpoint_poses


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


def test_unsafe_stable_pose_never_becomes_verified_or_persisted():
    """Clearance rejection must keep boot localization fail-closed."""
    node, safety, _transforms, validations, checkpoints = (
        _sampling_harness(accepts_pose=False)
    )

    PoseCheckpointNode._sample(node)

    assert node.validation_state == "verifying"
    assert node.stable_samples == 0
    assert node.latest_verified_pose is None
    assert validations == []
    assert checkpoints == []
    assert safety.checked_poses == [{"x": 0.1, "y": 0.1, "yaw": 0.0}]


def test_safe_pose_requires_a_fresh_stable_window_after_unsafe_pose():
    """An unsafe window resets the samples needed for a later safe pose."""
    node, safety, transforms, validations, checkpoints = (
        _sampling_harness(accepts_pose=False)
    )
    PoseCheckpointNode._sample(node)

    safety.accepts_pose = True
    transforms.x = 1.0
    transforms.y = 2.0
    for _index in range(STABLE_SAMPLE_COUNT - 1):
        PoseCheckpointNode._sample(node)

    assert node.validation_state == "verifying"
    assert node.latest_verified_pose is None
    assert checkpoints == []

    PoseCheckpointNode._sample(node)

    expected = {"x": 1.0, "y": 2.0, "yaw": 0.0}
    assert node.validation_state == "ok"
    assert node.latest_verified_pose == expected
    assert validations == ["ok"]
    assert checkpoints == [expected]
    assert safety.checked_poses == [
        {"x": 0.1, "y": 0.1, "yaw": 0.0},
        expected,
    ]
