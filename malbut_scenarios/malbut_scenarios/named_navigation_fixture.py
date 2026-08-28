"""
Prepare an isolated Small House map store for named-navigation tests.

This helper never edits the checked-in User Map.  It creates a private copy in
which the single synthetic Small House room has the explicit test name
``거실``.  Production room naming remains a user-owned map-editing operation.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile

from ament_index_python.packages import get_package_share_directory

from malbut_gazebo.map_lifecycle import MAP_STORE_FORMAT
from malbut_gazebo.room_editor import normalize_room_feature
from malbut_gazebo.user_map_builder import load_slam_map


FIXTURE_FORMAT = "malbut-named-navigation-fixture/v1"
FIXTURE_DEVICE_ID = "malbut-sim-01"
FIXTURE_ROOM_ID = "room-1"
FIXTURE_ROOM_NAME = "거실"
FIXTURE_ROOM_CATEGORY = "living_room"


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
    properties["name"] = FIXTURE_ROOM_NAME
    properties["category"] = FIXTURE_ROOM_CATEGORY
    normalized = normalize_room_feature(room, resolution=resolution)
    features[features.index(room)] = normalized
    value["fixture"] = {
        "format": FIXTURE_FORMAT,
        "device_id": FIXTURE_DEVICE_ID,
        "purpose": "SWM25-130 explicit Gazebo test only",
    }
    return value


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
