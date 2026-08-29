"""
Prepare an isolated Small House map store for named-navigation tests.

This helper never edits the checked-in User Map.  It creates a private copy
with three server-owned test target cells named ``거실``, ``주방``, and
``침실``.  The cells are acceptance fixtures, not a claim about production
room boundaries; production room naming remains a user-owned map-editing
operation.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile

from ament_index_python.packages import get_package_share_directory

from malbut_gazebo.map_lifecycle import MAP_STORE_FORMAT
from malbut_gazebo.room_editor import normalize_room_feature
from malbut_gazebo.user_map_builder import load_slam_map
from malbut_gazebo.zone_filter_mask import (
    build_filter_mask,
    validate_zone_collection,
)


FIXTURE_FORMAT = "malbut-named-navigation-fixture/v1"
FIXTURE_DEVICE_ID = "malbut-sim-01"
FIXTURE_ROOM_ID = "room-1"
FIXTURE_ROOM_NAME = "거실"
FIXTURE_ROOM_CATEGORY = "living_room"
_TARGET_CELL_HALF_EXTENT_M = 0.25


@dataclass(frozen=True)
class _FixtureRoomSpec:
    """One deterministic semantic target used only by the private fixture."""

    room_id: str
    name: str
    category: str
    color: str
    point: tuple[float, float]
    target_route_room_id: str
    anchor_route_room_id: str
    world_anchor_models: tuple[str, ...]


# SWM25-135 retains the legacy ``거실`` name/API but replaces the former
# whole-house synthetic pole with a center-south Sofa/CoffeeTable waypoint.
# Earlier SWM25-133/134 evidence stays commit-bound to its old target digest;
# this three-room fixture intentionally produces a new semantic binding.  The
# other targets use the right-side KitchenCabinet/CookingBench route and the
# safe left-room stand-off for the Bed/NightStand region.  Preparation
# revalidates each entire target cell against the occupancy map and Zone mask.
_FIXTURE_ROOM_SPECS = (
    _FixtureRoomSpec(
        room_id=FIXTURE_ROOM_ID,
        name=FIXTURE_ROOM_NAME,
        category=FIXTURE_ROOM_CATEGORY,
        color="#dce8ff",
        point=(1.75, 0.75),
        target_route_room_id="center_south",
        anchor_route_room_id="center_south",
        world_anchor_models=("SofaC_01_001", "CoffeeTable_01_001"),
    ),
    _FixtureRoomSpec(
        room_id="room-kitchen",
        name="주방",
        category="kitchen",
        color="#f9e1c7",
        point=(7.0, -3.25),
        target_route_room_id="right_room",
        anchor_route_room_id="right_room",
        world_anchor_models=(
            "CookingBench_01_001",
            "KitchenCabinet_01_001",
        ),
    ),
    _FixtureRoomSpec(
        room_id="room-bedroom",
        name="침실",
        category="bedroom",
        color="#d9f0e3",
        point=(-5.5, -0.25),
        target_route_room_id="left_room",
        anchor_route_room_id="left_room",
        world_anchor_models=("Bed_01_001", "NightStand_01_001"),
    ),
)


class NamedNavigationFixtureError(ValueError):
    """Report a deterministic fixture preparation failure."""


def _read_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NamedNavigationFixtureError(
            f"fixture source is not valid JSON: {path.name}"
        ) from error
    if not isinstance(value, dict):
        raise NamedNavigationFixtureError(
            f"fixture source must contain an object: {path.name}"
        )
    return value


def _write_private_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _target_cell(spec: _FixtureRoomSpec) -> dict:
    """Build one small, deterministic free-space cell around its target."""
    x, y = spec.point
    half = _TARGET_CELL_HALF_EXTENT_M
    ring = [
        [x - half, y - half],
        [x + half, y - half],
        [x + half, y + half],
        [x - half, y + half],
        [x - half, y - half],
    ]
    return {
        "type": "Feature",
        "id": spec.room_id,
        "properties": {
            "role": "room",
            "room_id": spec.room_id,
            "name": spec.name,
            "category": spec.category,
            "color": spec.color,
            "generated": True,
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [ring],
        },
    }


def _named_user_map(source: dict, *, resolution: float) -> dict:
    value = deepcopy(source)
    features = value.get("features")
    if not isinstance(features, list):
        raise NamedNavigationFixtureError("User Map features must be an array")
    rooms = [
        feature
        for feature in features
        if isinstance(feature, dict)
        and isinstance(feature.get("properties"), dict)
        and feature["properties"].get("role") == "room"
    ]
    if len(rooms) != 1:
        raise NamedNavigationFixtureError(
            "Small House fixture requires exactly one Room"
        )
    room = rooms[0]
    properties = room["properties"]
    room_id = room.get("id") or properties.get("room_id")
    if room_id != FIXTURE_ROOM_ID:
        raise NamedNavigationFixtureError(
            f"Small House fixture Room must be {FIXTURE_ROOM_ID}"
        )
    room_index = features.index(room)
    normalized = [
        normalize_room_feature(
            _target_cell(spec),
            resolution=resolution,
        )
        for spec in _FIXTURE_ROOM_SPECS
    ]
    features[room_index:room_index + 1] = normalized
    value["room_segmentation"] = {
        "method": "swm25_135_named_target_cells",
        "room_count": len(normalized),
        "edited": True,
    }
    value["fixture"] = {
        "format": FIXTURE_FORMAT,
        "device_id": FIXTURE_DEVICE_ID,
        "purpose": "SWM25-130/135 explicit Gazebo test only",
    }
    return value


def _validate_target_cells(slam_map, source_zones: dict) -> None:
    """Fail closed unless every server-owned target cell is safe."""
    try:
        zones = validate_zone_collection(
            source_zones,
            slam_map.map_id,
            slam_map.map_revision,
            slam_map.legacy_map_ids,
        )
        filter_mask = build_filter_mask(slam_map, zones)
    except ValueError as error:
        raise NamedNavigationFixtureError(
            "Small House fixture Zone mask is invalid"
        ) from error

    height, width = slam_map.image.shape[:2]
    for spec in _FIXTURE_ROOM_SPECS:
        x, y = spec.point
        half = _TARGET_CELL_HALF_EXTENT_M
        corner_pixels = (
            slam_map.transform.pixel([x - half, y - half]),
            slam_map.transform.pixel([x + half, y + half]),
        )
        minimum_x = min(pixel[0] for pixel in corner_pixels)
        maximum_x = max(pixel[0] for pixel in corner_pixels)
        minimum_y = min(pixel[1] for pixel in corner_pixels)
        maximum_y = max(pixel[1] for pixel in corner_pixels)
        if not (
            0 <= minimum_x <= maximum_x < width
            and 0 <= minimum_y <= maximum_y < height
        ):
            raise NamedNavigationFixtureError(
                "Small House fixture target cell is outside the map: "
                f"{spec.name}"
            )
        pixels = (
            (pixel_x, pixel_y)
            for pixel_y in range(minimum_y, maximum_y + 1)
            for pixel_x in range(minimum_x, maximum_x + 1)
        )
        if any(
            int(slam_map.image[pixel_y, pixel_x]) < 250
            or int(filter_mask[pixel_y, pixel_x]) != 0
            for pixel_x, pixel_y in pixels
        ):
            raise NamedNavigationFixtureError(
                "Small House fixture target cell is not navigation-safe: "
                f"{spec.name}"
            )


def prepare_small_house_named_navigation_fixture(
    destination: Path,
    *,
    map_yaml: Path,
    user_map: Path,
    zones: Path,
) -> dict:
    """Create one non-overwriting private map store and return its manifest."""
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise NamedNavigationFixtureError(
            "destination must not already exist"
        )
    # Test inputs are authority, not convenient aliases.  Check the supplied
    # path before resolving it: resolving first would erase a leaf symlink and
    # allow the fixture to be constructed from mutable indirection.
    source_paths = tuple(
        path.expanduser() for path in (map_yaml, user_map, zones)
    )
    for path in source_paths:
        if path.is_symlink() or not path.is_file():
            raise NamedNavigationFixtureError(
                f"fixture source must be a regular file: {path.name}"
            )
    map_yaml, user_map, zones = (path.resolve() for path in source_paths)

    slam_map = load_slam_map(map_yaml)
    source_user_map = _read_json_object(user_map)
    source_zones = _read_json_object(zones)
    for label, value in (
        ("User Map", source_user_map),
        ("Zone collection", source_zones),
    ):
        if value.get("map_id") != slam_map.map_id:
            raise NamedNavigationFixtureError(
                f"{label} map_id does not match the SLAM map"
            )
        if value.get("map_revision") != slam_map.map_revision:
            raise NamedNavigationFixtureError(
                f"{label} revision does not match the SLAM map"
            )
    _validate_target_cells(slam_map, source_zones)

    image_value = str(slam_map.image_path)
    image_path = Path(image_value).resolve()
    if image_path.is_symlink() or not image_path.is_file():
        raise NamedNavigationFixtureError(
            "SLAM map image must be a regular file"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.", dir=destination.parent
    ))
    staging.chmod(0o700)
    try:
        version = staging / "versions" / "swm25-130-small-house"
        version.mkdir(parents=True)
        version.chmod(0o700)
        copied_yaml = version / "small_house.yaml"
        copied_image = version / "small_house.pgm"
        copied_user_map = version / "user-map.geojson"
        copied_zones = version / f"{slam_map.map_id}-zones.geojson"
        shutil.copyfile(map_yaml, copied_yaml)
        shutil.copyfile(image_path, copied_image)

        named_map = _named_user_map(
            source_user_map,
            resolution=float(slam_map.transform.resolution),
        )
        _write_private_json(copied_user_map, named_map)
        _write_private_json(copied_zones, source_zones)

        copied_slam = load_slam_map(copied_yaml)
        if (
            copied_slam.map_id != slam_map.map_id
            or copied_slam.map_revision != slam_map.map_revision
        ):
            raise NamedNavigationFixtureError(
                "copied map identity changed during fixture preparation"
            )
        relative = version.relative_to(staging)
        manifest = {
            "format": MAP_STORE_FORMAT,
            "revision": "swm25-130-small-house",
            "map_id": slam_map.map_id,
            "map_revision": slam_map.map_revision,
            "map_yaml": str(relative / copied_yaml.name),
            "map_image": str(relative / copied_image.name),
            "user_map": str(relative / copied_user_map.name),
            "device_id": FIXTURE_DEVICE_ID,
            "fixture": FIXTURE_FORMAT,
        }
        _write_private_json(staging / "active.json", manifest)

        # The semantic revision is immutable after construction.  The map
        # store root and staging directory remain writable so operational
        # lifecycle files can still be maintained separately.
        copied_active = staging / "active.json"
        for path in (
            copied_yaml,
            copied_image,
            copied_user_map,
            copied_zones,
            copied_active,
        ):
            path.chmod(0o400)
        version.chmod(0o500)
        os.replace(staging, destination)
        return {
            **manifest,
            "store": str(destination),
            "user_map_path": str(
                destination / manifest["user_map"]
            ),
            "zone_path": str(destination / relative / copied_zones.name),
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _package_sources() -> tuple[Path, Path, Path]:
    """
    Resolve only the helper-selected assets from the installed packages.

    A ``--symlink-install`` overlay intentionally exposes package resources as
    symlinks.  Resolve those trusted, program-selected paths here; the public
    fixture function still rejects symlinks supplied by a caller.
    """
    gazebo = Path(get_package_share_directory("malbut_gazebo"))
    scenarios = Path(get_package_share_directory("malbut_scenarios"))
    selected = (
        gazebo / "maps" / "small_house.yaml",
        scenarios / "maps" / "small_house_user_map.geojson",
        scenarios / "maps" / "map-a0843f4df527-zones.geojson",
    )
    try:
        resolved = tuple(path.resolve(strict=True) for path in selected)
    except (OSError, RuntimeError) as error:
        raise NamedNavigationFixtureError(
            "installed fixture source is unavailable"
        ) from error
    if any(not path.is_file() for path in resolved):
        raise NamedNavigationFixtureError(
            "installed fixture source must be a regular file"
        )
    return resolved


def main(arguments: list[str] | None = None) -> int:
    """Prepare a fixture from installed assets without launching ROS."""
    parser = argparse.ArgumentParser(
        description="Prepare the private SWM25-130 Small House map store."
    )
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="New, non-existing directory to create.",
    )
    parsed = parser.parse_args(arguments)
    map_yaml, user_map, zones = _package_sources()
    result = prepare_small_house_named_navigation_fixture(
        parsed.destination,
        map_yaml=map_yaml,
        user_map=user_map,
        zones=zones,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
