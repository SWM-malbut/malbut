"""Contracts for the isolated SWM25-130 Small House fixture."""

import json
from pathlib import Path

import pytest

import malbut_scenarios.named_navigation_fixture as fixture_module

from malbut_gazebo.map_lifecycle import load_active_revision
from malbut_gazebo.named_navigation_facade import (
    ActiveMapCatalogSource,
    NamedNavigationFacadeError,
)
from malbut_gazebo.room_editor import normalize_room_feature
from malbut_gazebo.user_map_builder import load_slam_map
from malbut_scenarios.named_navigation_fixture import (
    FIXTURE_DEVICE_ID,
    NamedNavigationFixtureError,
    prepare_small_house_named_navigation_fixture,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GAZEBO_ROOT = REPOSITORY_ROOT / "malbut_gazebo"
SCENARIO_ROOT = REPOSITORY_ROOT / "malbut_scenarios"
MAP_YAML = GAZEBO_ROOT / "maps" / "small_house.yaml"
USER_MAP = SCENARIO_ROOT / "maps" / "small_house_user_map.geojson"
ZONES = SCENARIO_ROOT / "maps" / "map-a0843f4df527-zones.geojson"


def _prepare(destination: Path) -> dict:
    return prepare_small_house_named_navigation_fixture(
        destination,
        map_yaml=MAP_YAML,
        user_map=USER_MAP,
        zones=ZONES,
    )


def test_fixture_copies_exact_map_and_names_only_the_private_room(tmp_path):
    """Keep canonical assets immutable while naming the private test Room."""
    original_bytes = USER_MAP.read_bytes()
    destination = tmp_path / "private-store"

    result = _prepare(destination)

    assert USER_MAP.read_bytes() == original_bytes
    active = load_active_revision(destination)
    assert active is not None
    assert active["device_id"] == FIXTURE_DEVICE_ID
    copied_slam = load_slam_map(destination / active["map_yaml"])
    source_slam = load_slam_map(MAP_YAML)
    assert copied_slam.map_id == source_slam.map_id == active["map_id"]
    assert copied_slam.map_revision == source_slam.map_revision
    assert copied_slam.map_revision == active["map_revision"]

    copied_user_map_path = Path(result["user_map_path"])
    copied = json.loads(copied_user_map_path.read_text(encoding="utf-8"))
    rooms = [
        feature for feature in copied["features"]
        if feature.get("properties", {}).get("role") == "room"
    ]
    assert len(rooms) == 1
    room = rooms[0]
    assert room["properties"]["name"] == "거실"
    assert room["properties"]["category"] == "living_room"
    normalized = normalize_room_feature(
        room, resolution=copied_slam.transform.resolution
    )
    for field in ("area_m2", "representative_point", "clearance_m"):
        assert room["properties"][field] == normalized["properties"][field]

    catalog = ActiveMapCatalogSource(
        destination,
        FIXTURE_DEVICE_ID,
    ).load()
    target = catalog.resolve("거실")
    assert target.room_id == "room-1"
    assert (target.x, target.y) == (5.35, -1.8)
    assert not {"device_id", "room_id", "x", "y"} & set(
        target.to_public_dict()
    )

    semantic_version = (destination / active["user_map"]).parent
    assert destination.stat().st_mode & 0o777 == 0o700
    assert semantic_version.stat().st_mode & 0o777 == 0o500
    assert copied_user_map_path.stat().st_mode & 0o777 == 0o400
    assert (destination / "active.json").stat().st_mode & 0o777 == 0o400
    for relative_path in (
        active["map_yaml"],
        active["map_image"],
        result["zone_path"].removeprefix(f"{destination}/"),
    ):
        assert (destination / relative_path).stat().st_mode & 0o777 == 0o400


def test_fixture_never_overwrites_an_existing_destination(tmp_path):
    """Reject a destination that may already contain operator-owned data."""
    destination = tmp_path / "keep"
    destination.mkdir()
    marker = destination / "owner-data"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(
        NamedNavigationFixtureError,
        match="must not already exist",
    ):
        _prepare(destination)

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_internal_package_sources_support_symlink_install(
    tmp_path, monkeypatch
):
    """Resolve trusted package assets before the public symlink guard."""
    gazebo_share = tmp_path / "gazebo-share"
    scenario_share = tmp_path / "scenario-share"
    real = tmp_path / "real"
    for directory in (gazebo_share / "maps", scenario_share / "maps", real):
        directory.mkdir(parents=True, exist_ok=True)
    targets = (
        real / "small_house.yaml",
        real / "small_house_user_map.geojson",
        real / "map-a0843f4df527-zones.geojson",
    )
    for target in targets:
        target.write_text("trusted", encoding="utf-8")
    (gazebo_share / "maps" / targets[0].name).symlink_to(targets[0])
    for target in targets[1:]:
        (scenario_share / "maps" / target.name).symlink_to(target)

    def package_share(package_name):
        return str(
            gazebo_share
            if package_name == "malbut_gazebo"
            else scenario_share
        )

    monkeypatch.setattr(
        fixture_module,
        "get_package_share_directory",
        package_share,
    )

    assert fixture_module._package_sources() == targets


def test_active_catalog_source_rejects_symlinked_authority_paths(tmp_path):
    """Do not let a mutable symlink redefine the active semantic authority."""
    destination = tmp_path / "private-store"
    result = _prepare(destination)
    store_alias = tmp_path / "store-alias"
    store_alias.symlink_to(destination, target_is_directory=True)

    with pytest.raises(NamedNavigationFacadeError, match="symlink"):
        ActiveMapCatalogSource(store_alias, FIXTURE_DEVICE_ID)

    user_map = Path(result["user_map_path"])
    user_map.parent.chmod(0o700)
    backup = user_map.with_name("user-map-backup.geojson")
    user_map.rename(backup)
    user_map.symlink_to(backup.name)
    source = ActiveMapCatalogSource(destination, FIXTURE_DEVICE_ID)
    with pytest.raises(NamedNavigationFacadeError, match="symlink"):
        source.load()


@pytest.mark.parametrize("source_name", ["map_yaml", "user_map", "zones"])
def test_fixture_rejects_a_symlinked_source_before_resolving(
    tmp_path, source_name
):
    """A supplied symlink must never be accepted as fixture authority."""
    source = {
        "map_yaml": MAP_YAML,
        "user_map": USER_MAP,
        "zones": ZONES,
    }[source_name]
    alias = tmp_path / source.name
    alias.symlink_to(source)
    inputs = {
        "map_yaml": MAP_YAML,
        "user_map": USER_MAP,
        "zones": ZONES,
    }
    inputs[source_name] = alias

    with pytest.raises(NamedNavigationFixtureError, match="regular file"):
        prepare_small_house_named_navigation_fixture(
            tmp_path / "rejected",
            **inputs,
        )

    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("source_name", ["user_map", "zones"])
def test_fixture_fails_closed_on_map_identity_mismatch(tmp_path, source_name):
    """Reject either semantic source when its pinned revision is stale."""
    source = USER_MAP if source_name == "user_map" else ZONES
    altered = tmp_path / source.name
    value = json.loads(source.read_text(encoding="utf-8"))
    value["map_revision"] = "rev-stale"
    altered.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(
        NamedNavigationFixtureError,
        match="revision does not match",
    ):
        prepare_small_house_named_navigation_fixture(
            tmp_path / "rejected",
            map_yaml=MAP_YAML,
            user_map=altered if source_name == "user_map" else USER_MAP,
            zones=altered if source_name == "zones" else ZONES,
        )

    assert not (tmp_path / "rejected").exists()
