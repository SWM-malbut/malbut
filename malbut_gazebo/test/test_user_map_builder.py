"""Tests for conversion from a saved SLAM map to a vector User Map."""

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from malbut_gazebo.user_map_builder import (
    build_user_map,
    clean_free_space,
    load_slam_map,
)


GAZEBO_ROOT = Path(__file__).resolve().parents[1]


def _write_slam_map(tmp_path, name, free_polygon, obstacles=()):
    image = np.full((160, 220), 205, dtype=np.uint8)
    cv2.fillPoly(image, [np.array(free_polygon, dtype=np.int32)], 254)
    for obstacle in obstacles:
        cv2.fillPoly(image, [np.array(obstacle, dtype=np.int32)], 0)
    image_path = tmp_path / f"{name}.pgm"
    yaml_path = tmp_path / f"{name}.yaml"
    assert cv2.imwrite(str(image_path), image)
    yaml_path.write_text(yaml.safe_dump({
        "image": image_path.name,
        "mode": "trinary",
        "resolution": 0.05,
        "origin": [-2.0, -1.0, 0.0],
        "negate": 0,
        "occupied_thresh": 0.65,
        "free_thresh": 0.196,
    }), encoding="utf-8")
    return yaml_path


def _geometry_bounds(geometry):
    polygons = (
        [geometry["coordinates"]]
        if geometry["type"] == "Polygon"
        else geometry["coordinates"]
    )
    points = [
        point
        for polygon in polygons
        for ring in polygon
        for point in ring
    ]
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _ring_area(ring):
    return abs(sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(ring, ring[1:])
    )) / 2.0


def _geometry_area(geometry):
    polygons = (
        [geometry["coordinates"]]
        if geometry["type"] == "Polygon"
        else geometry["coordinates"]
    )
    return sum(
        _ring_area(polygon[0])
        - sum(_ring_area(hole) for hole in polygon[1:])
        for polygon in polygons
    )


def test_builder_uses_each_saved_slam_map_shape(tmp_path):
    """Different occupancy maps must produce different User Map geometry."""
    compact_path = _write_slam_map(
        tmp_path,
        "compact",
        [(20, 20), (100, 20), (100, 100), (20, 100)],
    )
    wide_path = _write_slam_map(
        tmp_path,
        "wide",
        [(15, 30), (180, 30), (180, 85), (15, 85)],
    )
    compact, _ = build_user_map(load_slam_map(compact_path))
    wide, _ = build_user_map(load_slam_map(wide_path))
    compact_bounds = _geometry_bounds(compact["features"][0]["geometry"])
    wide_bounds = _geometry_bounds(wide["features"][0]["geometry"])

    assert 3.8 < compact_bounds[2] - compact_bounds[0] < 4.2
    assert 3.8 < compact_bounds[3] - compact_bounds[1] < 4.2
    assert 8.0 < wide_bounds[2] - wide_bounds[0] < 8.5
    assert 2.5 < wide_bounds[3] - wide_bounds[1] < 3.0


def test_user_map_keeps_slam_coordinates_and_stable_identity(tmp_path):
    """The output must stay tied to the navigation map coordinate frame."""
    map_path = _write_slam_map(
        tmp_path,
        "home",
        [(20, 20), (190, 20), (190, 130), (20, 130)],
        obstacles=[[(80, 60), (120, 60), (120, 66), (80, 66)]],
    )
    first_source = load_slam_map(map_path)
    second_source = load_slam_map(map_path)
    user_map, preview = build_user_map(first_source)

    roles = {
        feature["properties"]["role"]
        for feature in user_map["features"]
    }
    assert first_source.map_id == second_source.map_id
    assert user_map["frame_id"] == "map"
    assert user_map["source"]["type"] == "slam_occupancy_grid"
    assert roles == {"walkable_area", "wall_outline", "room"}
    assert user_map["room_segmentation"]["method"] == "single_initial_room"
    assert user_map["room_segmentation"]["room_count"] == 1
    assert preview.ndim == 3
    assert preview.shape == (160, 220, 3)
    room = next(
        feature for feature in user_map["features"]
        if feature["properties"]["role"] == "room"
    )
    center = first_source.transform.pixel(room["properties"]["centroid"])
    # The product preview contains only map geometry. Room names are rendered
    # by the interactive web overlay, never baked in as debug numbers.
    assert preview[center[1], center[0]].tolist() == [255, 232, 220]


def test_small_occupancy_speckles_do_not_become_user_map_rooms(tmp_path):
    """A tiny SLAM noise spot must not survive as a visible wall hole."""
    map_path = _write_slam_map(
        tmp_path,
        "noisy",
        [(20, 20), (190, 20), (190, 130), (20, 130)],
        obstacles=[[(100, 80), (101, 80), (101, 81), (100, 81)]],
    )
    user_map, _ = build_user_map(load_slam_map(map_path))
    floor = user_map["features"][0]["geometry"]

    assert floor["type"] == "Polygon"
    assert len(floor["coordinates"]) == 1


def test_partitioned_home_still_starts_as_one_unassigned_room(tmp_path):
    """Walls must not make the generator infer Room meaning for the user."""
    map_path = _write_slam_map(
        tmp_path,
        "two_rooms",
        [(20, 20), (190, 20), (190, 130), (20, 130)],
        obstacles=[
            [(103, 20), (107, 20), (107, 70), (103, 70)],
            [(103, 84), (107, 84), (107, 130), (103, 130)],
        ],
    )
    user_map, _ = build_user_map(load_slam_map(map_path))
    rooms = [
        feature
        for feature in user_map["features"]
        if feature["properties"]["role"] == "room"
    ]

    floor = next(
        feature for feature in user_map["features"]
        if feature["properties"]["role"] == "walkable_area"
    )
    assert user_map["room_segmentation"] == {
        "method": "single_initial_room",
        "room_count": 1,
    }
    assert len(rooms) == 1
    assert rooms[0]["id"] == "room-1"
    assert rooms[0]["properties"]["name"] == "공간 1"
    assert rooms[0]["properties"]["category"] == "unassigned"
    assert rooms[0]["geometry"] == floor["geometry"]


def test_disconnected_scan_patch_is_not_part_of_the_editable_home(tmp_path):
    """An unreachable free patch must not turn the initial Room multipart."""
    image = np.full((160, 220), 205, dtype=np.uint8)
    cv2.fillPoly(
        image,
        [np.array([(20, 20), (190, 20), (190, 130), (20, 130)])],
        254,
    )
    cv2.fillPoly(
        image,
        [np.array([(5, 140), (25, 140), (25, 155), (5, 155)])],
        254,
    )
    image_path = tmp_path / "disconnected.pgm"
    map_path = tmp_path / "disconnected.yaml"
    assert cv2.imwrite(str(image_path), image)
    map_path.write_text(yaml.safe_dump({
        "image": image_path.name,
        "mode": "trinary",
        "resolution": 0.05,
        "origin": [-2.0, -1.0, 0.0],
        "negate": 0,
        "occupied_thresh": 0.65,
        "free_thresh": 0.196,
    }), encoding="utf-8")

    user_map, _ = build_user_map(load_slam_map(map_path))
    floor = next(
        feature for feature in user_map["features"]
        if feature["properties"]["role"] == "walkable_area"
    )
    room = next(
        feature for feature in user_map["features"]
        if feature["properties"]["role"] == "room"
    )

    assert floor["geometry"]["type"] == "Polygon"
    assert room["geometry"] == floor["geometry"]
    assert _geometry_bounds(room["geometry"])[0] > -1.1


def test_smoothing_never_erases_a_confirmed_thin_wall(tmp_path):
    """A residential partition must survive floor-plan denoising."""
    map_path = _write_slam_map(
        tmp_path,
        "thin_wall",
        [(20, 20), (190, 20), (190, 130), (20, 130)],
        obstacles=[
            [(104, 20), (106, 20), (106, 72), (104, 72)],
            [(104, 83), (106, 83), (106, 130), (104, 130)],
        ],
    )

    slam_map = load_slam_map(map_path)
    free = clean_free_space(slam_map)
    user_map, _ = build_user_map(slam_map)
    rooms = [
        feature for feature in user_map["features"]
        if feature["properties"]["role"] == "room"
    ]

    assert free[50, 105] == 0
    assert free[77, 105] == 255
    assert len(rooms) == 1


def test_default_conversion_preserves_the_saved_free_space_boundary(tmp_path):
    """Default User Maps must follow SLAM geometry without forced smoothing."""
    map_path = _write_slam_map(
        tmp_path,
        "unsmoothed",
        [(20, 20), (190, 20), (190, 130), (20, 130)],
        obstacles=[[(80, 60), (90, 60), (90, 70), (80, 70)]],
    )
    slam_map = load_slam_map(map_path)
    expected = np.where(slam_map.image == 254, 255, 0).astype(np.uint8)

    assert np.array_equal(clean_free_space(slam_map), expected)


def test_ros2_map_saver_gray_cells_remain_unexplored(tmp_path):
    """Gray 205 cells must not become floor under ROS 2 save defaults."""
    map_path = _write_slam_map(
        tmp_path,
        "ros2_saved",
        [(20, 60), (190, 60), (190, 130), (20, 130)],
    )
    metadata = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    metadata["free_thresh"] = 0.25
    map_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    slam_map = load_slam_map(map_path)
    free = clean_free_space(slam_map)

    assert slam_map.image[30, 100] == 205
    assert free[30, 100] == 0
    assert free[90, 100] == 255


def test_room_area_matches_the_exported_vector_geometry(tmp_path):
    """Displayed area must describe the polygon a user actually edits."""
    map_path = _write_slam_map(
        tmp_path,
        "area",
        [(20, 20), (190, 20), (190, 130), (20, 130)],
    )

    user_map, _ = build_user_map(load_slam_map(map_path))
    room = next(
        feature for feature in user_map["features"]
        if feature["properties"]["role"] == "room"
    )

    assert room["properties"]["area_m2"] == pytest.approx(
        _geometry_area(room["geometry"]), abs=0.01
    )


def test_map_identity_ignores_file_names_and_yaml_formatting(tmp_path):
    """Moving an unchanged map must not orphan Room and Zone storage."""
    first = _write_slam_map(
        tmp_path,
        "first",
        [(20, 20), (190, 20), (190, 130), (20, 130)],
    )
    first_image = cv2.imread(str(tmp_path / "first.pgm"), cv2.IMREAD_GRAYSCALE)
    second_image_path = tmp_path / "renamed-image.pgm"
    assert cv2.imwrite(str(second_image_path), first_image)
    second = tmp_path / "renamed-map.yaml"
    second.write_text(
        "image: renamed-image.pgm\n"
        "origin: [-2.0, -1.0, 0.0]\n"
        "resolution: 0.050\n"
        "mode: trinary\n"
        "free_thresh: 0.196\n"
        "occupied_thresh: 0.650\n"
        "negate: 0\n",
        encoding="utf-8",
    )

    assert load_slam_map(first).map_id == load_slam_map(second).map_id


def test_map_identity_survives_threshold_tuning_with_new_revision(tmp_path):
    """Occupancy tuning must invalidate previews without orphaning Rooms."""
    map_path = _write_slam_map(
        tmp_path,
        "thresholds",
        [(20, 20), (190, 20), (190, 130), (20, 130)],
    )
    first = load_slam_map(map_path)
    metadata = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    metadata["free_thresh"] = 0.25
    map_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    second = load_slam_map(map_path)

    assert first.map_id == second.map_id
    assert first.map_revision != second.map_revision
    assert first.legacy_map_ids != second.legacy_map_ids


def test_packaged_map_keeps_legacy_identity_and_unknown_threshold():
    """The shipped map must stay bound to saved Zones while preserving unknown."""
    slam_map = load_slam_map(
        GAZEBO_ROOT / "maps" / "robocup_home.yaml"
    )

    assert slam_map.map_id == "map-12e2d8760d08"
    assert slam_map.free_threshold == pytest.approx(0.196)
    assert np.any(slam_map.image == 205)
    assert not np.any(clean_free_space(slam_map)[slam_map.image == 205])


def test_loader_uses_ros_defaults_and_rejects_unsupported_modes(tmp_path):
    """Optional ROS metadata has defaults; unsupported semantics fail loud."""
    map_path = _write_slam_map(
        tmp_path,
        "defaults",
        [(20, 20), (190, 20), (190, 130), (20, 130)],
    )
    metadata = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    for key in ("negate", "occupied_thresh", "free_thresh", "mode"):
        metadata.pop(key)
    map_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")

    slam_map = load_slam_map(map_path)
    assert slam_map.negate is False
    assert slam_map.occupied_threshold == 0.65
    assert slam_map.free_threshold == 0.196
    assert slam_map.mode == "trinary"

    for mode in ("raw", "scale", "nonsense_value"):
        metadata["mode"] = mode
        map_path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
        with pytest.raises(ValueError, match="only ROS map mode"):
            load_slam_map(map_path)


def test_loader_rejects_an_image_without_ros_map_metadata(tmp_path):
    """A PGM alone lacks the resolution and world-coordinate contract."""
    fake_map = tmp_path / "map.pgm"
    fake_map.write_bytes(b"P5\n1 1\n255\n\x00")

    try:
        load_slam_map(fake_map)
    except ValueError:
        pass
    else:
        raise AssertionError("an occupancy image was accepted without YAML")


def test_editor_loads_maps_dynamically_and_has_no_fixed_house_geometry():
    """The browser editor must import a map instead of embedding one."""
    editor_root = GAZEBO_ROOT / "web" / "semantic_zone_editor"
    html = (editor_root / "index.html").read_text(encoding="utf-8")
    script = (editor_root / "app.js").read_text(encoding="utf-8")
    scenario_script = (editor_root / "scenario_controls.js").read_text(
        encoding="utf-8"
    )

    assert 'id="mapFile"' in html
    assert 'id="zoneFile"' in html
    assert 'id="zoneRoom"' not in html
    assert 'id="zoneArea"' in html
    assert 'id="zoneCategory"' not in html
    assert 'id="zoneBehavior"' in html
    assert 'id="roomLayer"' in html
    assert 'id="roomList"' in html
    assert 'id="splitRoom"' in html
    assert 'id="mergeRoom"' in html
    assert 'id="roomName"' in html
    assert 'id="roomCategory"' in html
    assert 'id="resetRooms"' in html
    assert 'id="exportMap"' in html
    assert 'id="applyZones"' in html
    assert "walkable_area" in script
    assert 'role === "room"' in script
    assert "function renderRooms()" in script
    assert 'postJson("/api/split-room"' in script
    assert 'postJson("/api/merge-rooms"' in script
    assert 'postJson("/api/apply-zones"' in script
    assert 'postJson("/api/rooms"' in script
    assert 'id="navigateMode"' in html
    assert 'id="startPatrol"' in html
    assert 'id="startPersonTracking"' in html
    assert 'id="stopScenario"' in html
    assert 'id="navigationPanel"' in html
    assert 'id="robotLayer"' in html
    assert 'new EventSource("/api/robot/stream")' in script
    assert 'new EventSource("/api/robot/stream")' in scenario_script
    assert 'mode === "transitioning"' in scenario_script
    assert 'mode === "person_tracking"' in scenario_script
    assert 'navigateMode.click()' in scenario_script
    assert 'postJson("/api/navigation/preview"' in script
    assert 'postJson("/api/navigation/start"' in script
    assert 'postJson("/api/navigation/cancel"' in script
    assert 'state.mode === "navigate"' in script
    assert "liveNavigationPath" in script
    assert 'class: "navigation-trail"' in script
    assert '"live_global_costmap"' in script
    assert 'fetch("/api/editor-config"' in script
    assert "function pointNearRoomWall(" in script
    assert "function orthogonalCorner(" in script
    assert 'state.dragging = {type: "split", lineIndex, index}' in script
    assert 'class: "split-bend-point"' in script
    assert "lines: state.splitLines" in script
    assert "function scheduleSplitValidation()" in script
    assert "splitValidationMessage" in script
    assert "const value = await response.json();" in script
    assert "function refreshGeneratedInitialRoom(" in script
    assert "!state.splitLines.length" in script
    assert 'event.key === "Backspace"' in script
    assert "function defaultZoneRing()" in script
    assert "function createDefaultZone()" in script
    assert 'type: "zone-move"' in script
    assert 'type: "zone-corner"' in script
    assert 'type: "zone-edge"' in script
    assert "state.draft.push" not in script
    assert 'state.mode = "drawing"' not in script
    assert "function persistRooms()" in script
    assert '"X-CSRF-Token": state.csrfToken' in script
    assert "malbut-rooms:v2:" in script
    assert "function roomDisplayName(room)" in script
    assert "semantic_zone" in script
    assert "function validateZoneRing(ring, boundary)" in script
    assert "function normalizeZones(zones = state.zones)" in script
    assert 'value.format !== "malbut-semantic-zones-v1"' in script
    assert 'if (!zones.length)' in script
    assert "function readStoredArray(" in script
    assert "delete zone.properties.needs_review" in script
    assert "acceptedMapIds.has(value.map_id)" in script
    assert "small_house" not in script.lower()
    assert "aws" not in script.lower()
