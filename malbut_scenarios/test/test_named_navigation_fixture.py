"""Contracts for the isolated SWM25-130/135 Small House fixture."""

import json
from itertools import combinations
from pathlib import Path
from xml.etree import ElementTree

import pytest

import malbut_scenarios.named_navigation_fixture as fixture_module

from malbut_gazebo.map_lifecycle import load_active_revision
from malbut_gazebo.named_navigation_facade import (
    ActiveMapCatalogSource,
    NamedNavigationFacadeError,
)
from malbut_gazebo.room_editor import normalize_room_feature
from malbut_gazebo.user_map_builder import load_slam_map
from malbut_gazebo.zone_filter_mask import build_filter_mask, load_zones
from malbut_scenarios.named_navigation_fixture import (
    FIXTURE_DEVICE_ID,
    NamedNavigationFixtureError,
    SWM25_137_ALTERNATE_ACTIVE_FILENAME,
    activate_swm25_137_alternate_revision,
    prepare_small_house_named_navigation_fixture,
)
from malbut_scenarios.scenario_config import load_room_routes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GAZEBO_ROOT = REPOSITORY_ROOT / "malbut_gazebo"
SCENARIO_ROOT = REPOSITORY_ROOT / "malbut_scenarios"
MAP_YAML = GAZEBO_ROOT / "maps" / "small_house.yaml"
USER_MAP = SCENARIO_ROOT / "maps" / "small_house_user_map.geojson"
ZONES = SCENARIO_ROOT / "maps" / "map-a0843f4df527-zones.geojson"
ROOM_ROUTES = SCENARIO_ROOT / "config" / "room_routes.yaml"
SMALL_HOUSE_WORLD = GAZEBO_ROOT / "worlds" / "small_house.sdf"


def _prepare(destination: Path) -> dict:
    return prepare_small_house_named_navigation_fixture(
        destination,
        map_yaml=MAP_YAML,
        user_map=USER_MAP,
        zones=ZONES,
    )


def test_fixture_copies_map_and_builds_three_private_named_targets(tmp_path):
    """Keep canonical assets immutable while adding three target cells."""
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
    assert len(rooms) == 3
    assert copied["room_segmentation"] == {
        "method": "swm25_135_named_target_cells",
        "room_count": 3,
        "edited": True,
    }
    rooms_by_name = {
        room["properties"]["name"]: room for room in rooms
    }
    assert {
        name: room["properties"]["category"]
        for name, room in rooms_by_name.items()
    } == {
        "거실": "living_room",
        "주방": "kitchen",
        "침실": "bedroom",
    }
    assert {
        room["properties"]["area_m2"] for room in rooms
    } == {0.25}
    for room in rooms:
        normalized = normalize_room_feature(
            room,
            resolution=copied_slam.transform.resolution,
        )
        for field in ("area_m2", "representative_point", "clearance_m"):
            assert (
                room["properties"][field]
                == normalized["properties"][field]
            )

    def bounds(room):
        ring = room["geometry"]["coordinates"][0]
        return (
            min(point[0] for point in ring),
            min(point[1] for point in ring),
            max(point[0] for point in ring),
            max(point[1] for point in ring),
        )

    for first, second in combinations(rooms, 2):
        first_min_x, first_min_y, first_max_x, first_max_y = bounds(first)
        second_min_x, second_min_y, second_max_x, second_max_y = bounds(
            second
        )
        assert (
            first_max_x <= second_min_x
            or second_max_x <= first_min_x
            or first_max_y <= second_min_y
            or second_max_y <= first_min_y
        )

    catalog = ActiveMapCatalogSource(
        destination,
        FIXTURE_DEVICE_ID,
    ).load()
    assert catalog.room_count == 3
    targets = {
        name: catalog.resolve(name) for name in ("거실", "주방", "침실")
    }
    assert {
        name: (target.room_id, target.x, target.y)
        for name, target in targets.items()
    } == {
        "거실": ("room-1", 1.75, 0.75),
        "주방": ("room-kitchen", 7.0, -3.25),
        "침실": ("room-bedroom", -5.5, -0.25),
    }
    assert len({target.binding_digest for target in targets.values()}) == 3
    assert len({(target.x, target.y) for target in targets.values()}) == 3
    assert len({target.room_name for target in targets.values()}) == 3
    for target in targets.values():
        assert target.map_id == source_slam.map_id
        assert target.map_revision == source_slam.map_revision
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


def test_fixture_targets_have_model_anchors_and_safe_target_cells(tmp_path):
    """Pin model regions, semantic targets, and vetted route points."""
    destination = tmp_path / "private-store"
    _prepare(destination)
    slam_map = load_slam_map(MAP_YAML)
    zones = load_zones(
        ZONES,
        slam_map.map_id,
        slam_map.map_revision,
        slam_map.legacy_map_ids,
    )
    filter_mask = build_filter_mask(slam_map, zones)
    _, routes = load_room_routes(ROOM_ROUTES)
    routes_by_id = {route.room_id: route for route in routes}
    includes = ElementTree.parse(SMALL_HOUSE_WORLD).getroot().findall(
        "world/include"
    )
    model_points = {
        include.findtext("name"): tuple(
            float(value)
            for value in include.findtext("pose").split()[:2]
        )
        for include in includes
    }
    catalog = ActiveMapCatalogSource(
        destination,
        FIXTURE_DEVICE_ID,
    ).load()

    for spec in fixture_module._FIXTURE_ROOM_SPECS:
        target = catalog.resolve(spec.name)
        assert (target.x, target.y) == spec.point
        half = fixture_module._TARGET_CELL_HALF_EXTENT_M
        corner_pixels = (
            slam_map.transform.pixel([
                spec.point[0] - half,
                spec.point[1] - half,
            ]),
            slam_map.transform.pixel([
                spec.point[0] + half,
                spec.point[1] + half,
            ]),
        )
        for pixel_y in range(
            min(pixel[1] for pixel in corner_pixels),
            max(pixel[1] for pixel in corner_pixels) + 1,
        ):
            for pixel_x in range(
                min(pixel[0] for pixel in corner_pixels),
                max(pixel[0] for pixel in corner_pixels) + 1,
            ):
                assert slam_map.image[pixel_y, pixel_x] >= 250
                assert filter_mask[pixel_y, pixel_x] == 0

        anchor_route = routes_by_id[spec.anchor_route_room_id]
        for model_name in spec.world_anchor_models:
            model_x, model_y = model_points[model_name]
            assert anchor_route.contains(model_x, model_y)

        target_route = routes_by_id[spec.target_route_room_id]
        assert any(
            (waypoint.x, waypoint.y) == spec.point
            for waypoint in target_route.waypoints
        )


def test_fixture_has_one_valid_atomic_map_revision_change(tmp_path):
    """Provide a real alternate binding without editing checked-in assets."""
    destination = tmp_path / "private-store"
    result = _prepare(destination)
    source = ActiveMapCatalogSource(destination, FIXTURE_DEVICE_ID)
    before_catalog = source.load()
    before_target = before_catalog.resolve("거실")
    alternate_authority = (
        destination / SWM25_137_ALTERNATE_ACTIVE_FILENAME
    )

    assert Path(result["alternate_active_path"]) == alternate_authority
    assert alternate_authority.stat().st_mode & 0o777 == 0o400

    activate_swm25_137_alternate_revision(destination)

    after_catalog = source.load()
    after_target = after_catalog.resolve("거실")
    assert after_catalog.map_id == before_catalog.map_id
    assert after_catalog.map_revision != before_catalog.map_revision
    assert after_target.binding_digest != before_target.binding_digest
    assert after_target.room_name == before_target.room_name == "거실"
    assert after_target.room_category == before_target.room_category
    assert (after_target.x, after_target.y) == (
        before_target.x,
        before_target.y,
    )
    assert (destination / "active.json").stat().st_mode & 0o777 == 0o400

    with pytest.raises(
        NamedNavigationFixtureError,
        match="alternate fixture binding is invalid",
    ):
        activate_swm25_137_alternate_revision(destination)


def test_fixture_fails_closed_when_a_target_is_outside_the_map(
    tmp_path,
    monkeypatch,
):
    """Preparation must revalidate server-owned targets against the map."""
    original = fixture_module._FIXTURE_ROOM_SPECS
    invalid = fixture_module._FixtureRoomSpec(
        room_id=original[1].room_id,
        name=original[1].name,
        category=original[1].category,
        color=original[1].color,
        point=(100.0, 100.0),
        target_route_room_id=original[1].target_route_room_id,
        anchor_route_room_id=original[1].anchor_route_room_id,
        world_anchor_models=original[1].world_anchor_models,
    )
    monkeypatch.setattr(
        fixture_module,
        "_FIXTURE_ROOM_SPECS",
        (original[0], invalid, original[2]),
    )

    with pytest.raises(
        NamedNavigationFixtureError,
        match="outside the map",
    ):
        _prepare(tmp_path / "rejected")

    assert not (tmp_path / "rejected").exists()


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
