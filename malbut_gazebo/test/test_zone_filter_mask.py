"""Tests for converting semantic Zones into Nav2 filter masks."""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from malbut_gazebo.user_map_editor import apply_zone_configuration
from malbut_gazebo.user_map_builder import load_slam_map
from malbut_gazebo.zone_filter_mask import (
    build_filter_mask,
    load_zones,
    write_filter_mask,
)


def _write_map(
    tmp_path: Path,
    yaw: float = 0.0,
    image: np.ndarray | None = None,
) -> Path:
    if image is None:
        image = np.full((100, 100), 254, dtype=np.uint8)
    image_path = tmp_path / "map.pgm"
    assert cv2.imwrite(str(image_path), image)
    yaml_path = tmp_path / "map.yaml"
    yaml_path.write_text(yaml.safe_dump({
        "image": image_path.name,
        "mode": "trinary",
        "resolution": 0.1,
        "origin": [0.0, 0.0, yaw],
        "negate": 0,
        "occupied_thresh": 0.65,
        "free_thresh": 0.196,
    }), encoding="utf-8")
    return yaml_path


def _zone(
    zone_id: str,
    behavior: str,
    minimum: float,
    maximum: float,
    needs_review: bool = False,
) -> dict:
    return {
        "type": "Feature",
        "id": zone_id,
        "properties": {
            "role": "semantic_zone",
            "zone_id": zone_id,
            "name": zone_id,
            "behavior": behavior,
            "needs_review": needs_review,
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [minimum, minimum],
                [maximum, minimum],
                [maximum, maximum],
                [minimum, maximum],
                [minimum, minimum],
            ]],
        },
    }


def _write_zones(tmp_path: Path, map_id: str, features: list) -> Path:
    path = tmp_path / "zones.geojson"
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "format": "malbut-semantic-zones-v1",
        "map_id": map_id,
        "frame_id": "map",
        "features": features,
    }), encoding="utf-8")
    return path


def _mask_value(mask, slam_map, point):
    x, y = slam_map.transform.pixel(point)
    return int(mask[y, x])


def test_filter_mask_applies_allow_avoid_and_restricted_priority(tmp_path):
    """Restricted must win overlaps and allow must not erase constraints."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")
    zones = [
        _zone("avoid", "avoid", 1.0, 6.0),
        _zone("restricted", "restricted", 3.0, 5.0),
        _zone("allow-overlap", "allow", 4.0, 7.0),
    ]

    mask = build_filter_mask(slam_map, zones)

    assert _mask_value(mask, slam_map, [0.5, 0.5]) == 0
    assert _mask_value(mask, slam_map, [2.0, 2.0]) == 70
    assert _mask_value(mask, slam_map, [4.0, 4.0]) == 100
    assert set(np.unique(mask)) == {0, 70, 100}


def test_clearance_cost_prefers_open_space_without_closing_a_corridor(
    tmp_path,
):
    """Wall proximity is costly, but a physically usable gap stays open."""
    image = np.full((100, 100), 254, dtype=np.uint8)
    image[:, 40] = 0
    image[:, 50] = 0
    slam_map = load_slam_map(
        _write_map(tmp_path, image=image), "home"
    )

    mask = build_filter_mask(
        slam_map,
        [],
        hard_clearance=0.24,
        preferred_clearance=0.60,
        clearance_cost=90,
    )

    assert mask[50, 40] == 100
    assert mask[50, 42] == 100
    assert 0 < mask[50, 45] < 100
    assert mask[50, 60] == 0
    assert np.any(mask[50, 41:50] < 100)


def test_restricted_zone_buffer_is_hard_without_costmap_reinflation(tmp_path):
    """Restricted Zones own one explicit buffer without closing wall gaps."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")
    zone = _zone("restricted", "restricted", 3.0, 5.0)

    mask = build_filter_mask(
        slam_map,
        [zone],
        restricted_buffer=0.2,
    )

    assert _mask_value(mask, slam_map, [2.9, 4.0]) == 100
    assert _mask_value(mask, slam_map, [2.6, 4.0]) < 100


def test_restricted_zone_buffer_cannot_be_negative(tmp_path):
    """A negative safety buffer must be rejected instead of shrinking a Zone."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")

    with pytest.raises(ValueError, match="restricted buffer"):
        build_filter_mask(slam_map, [], restricted_buffer=-0.1)


@pytest.mark.parametrize(
    "hard,preferred,cost,error",
    [
        (-0.1, 0.6, 90, "hard clearance"),
        (0.6, 0.6, 90, "preferred clearance"),
        (0.7, 0.6, 90, "preferred clearance"),
        (0.2, 0.6, 0, "clearance cost"),
        (0.2, 0.6, 100, "clearance cost"),
    ],
)
def test_clearance_parameters_reject_unsafe_values(
    tmp_path, hard, preferred, cost, error
):
    """Invalid hard/soft safety bands must not silently build a mask."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")

    with pytest.raises(ValueError, match=error):
        build_filter_mask(
            slam_map,
            [],
            hard_clearance=hard,
            preferred_clearance=preferred,
            clearance_cost=cost,
        )


def test_filter_mask_priority_does_not_depend_on_feature_order(tmp_path):
    """Feature order must not change the safety result."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")
    zones = [
        _zone("restricted", "restricted", 2.0, 5.0),
        _zone("avoid", "avoid", 1.0, 6.0),
    ]

    first = build_filter_mask(slam_map, zones)
    second = build_filter_mask(slam_map, list(reversed(zones)))

    assert np.array_equal(first, second)


def test_filter_mask_writes_raw_aligned_map_metadata(tmp_path):
    """The mask pair must preserve the source map grid exactly."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")
    mask = build_filter_mask(
        slam_map, [_zone("restricted", "restricted", 2.0, 4.0)]
    )

    yaml_path, image_path = write_filter_mask(
        tmp_path / "filters" / "zones.yaml", mask, slam_map
    )
    metadata = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    written = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)

    assert metadata == {
        "image": "zones.pgm",
        "mode": "raw",
        "resolution": 0.1,
        "origin": [0.0, 0.0, 0.0],
        "negate": 0,
        "occupied_thresh": 1.0,
        "free_thresh": 0.0,
    }
    assert np.array_equal(written, mask)


def test_editor_apply_persists_zones_and_builds_the_nav2_mask(tmp_path):
    """One editor action must replace the export-and-convert workflow."""
    map_path = _write_map(tmp_path)
    value = {
        "type": "FeatureCollection",
        "format": "malbut-semantic-zones-v1",
        "map_id": "home",
        "frame_id": "map",
        "features": [_zone("restricted", "restricted", 2.0, 4.0)],
    }

    zone_path, yaml_path, image_path = apply_zone_configuration(
        value,
        "home",
        map_path,
        tmp_path / "runtime" / "zone-filter.yaml",
        tmp_path / "runtime" / "home-zones.geojson",
    )

    assert json.loads(zone_path.read_text(encoding="utf-8")) == value
    metadata = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    written = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    assert metadata["mode"] == "raw"
    assert set(np.unique(written)) == {0, 100}


def test_zone_file_must_match_map_identity(tmp_path):
    """A Zone file from another home must never affect navigation."""
    path = _write_zones(tmp_path, "other-home", [])

    with pytest.raises(ValueError, match="map_id"):
        load_zones(path, "current-home")


@pytest.mark.parametrize("behavior", ["unknown", "", None])
def test_filter_mask_rejects_unknown_behavior(tmp_path, behavior):
    """Unknown behavior must not silently become an allowed Zone."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")

    with pytest.raises(ValueError, match="unsupported Zone behavior"):
        build_filter_mask(slam_map, [_zone("zone", behavior, 1.0, 2.0)])


def test_filter_mask_ignores_legacy_room_review_metadata(tmp_path):
    """Room editing metadata must not affect an independent Nav2 Zone."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")

    mask = build_filter_mask(
        slam_map,
        [_zone("zone", "restricted", 1.0, 2.0, True)],
    )

    assert _mask_value(mask, slam_map, [1.5, 1.5]) == 100


def test_filter_mask_rejects_zone_outside_map_bounds(tmp_path):
    """Clipping a Zone silently would change the user's safety boundary."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")

    with pytest.raises(ValueError, match="outside the SLAM map"):
        build_filter_mask(
            slam_map, [_zone("zone", "restricted", -1.0, 2.0)]
        )


def test_filter_mask_rejects_rotated_map_origin(tmp_path):
    """Nav2 costmap filters do not support a rotated mask origin."""
    slam_map = load_slam_map(_write_map(tmp_path, yaw=0.2), "home")

    with pytest.raises(ValueError, match="zero origin yaw"):
        build_filter_mask(
            slam_map, [_zone("zone", "restricted", 1.0, 2.0)]
        )


@pytest.mark.parametrize("avoid_cost", [0, 100, -1, 101])
def test_filter_mask_rejects_non_soft_avoid_cost(tmp_path, avoid_cost):
    """Avoid cost must stay traversable and below restricted cost."""
    slam_map = load_slam_map(_write_map(tmp_path), "home")

    with pytest.raises(ValueError, match="between 1 and 99"):
        build_filter_mask(slam_map, [], avoid_cost)
