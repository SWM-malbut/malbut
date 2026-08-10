"""Tests for user-directed Room geometry editing."""

import pytest

from malbut_gazebo.room_editor import (
    merge_room_features,
    normalize_room_feature,
    room_representative_point,
    split_room_feature,
)


def _rectangular_room(room_id="room-1", minimum_x=0.0):
    return {
        "type": "Feature",
        "id": room_id,
        "properties": {
            "role": "room",
            "room_id": room_id,
            "name": room_id,
            "category": "unassigned",
            "color": "#dce8ff",
            "area_m2": 60.0,
            "centroid": [minimum_x + 5.0, 3.0],
            "generated": True,
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [minimum_x, 0.0],
                [minimum_x + 10.0, 0.0],
                [minimum_x + 10.0, 6.0],
                [minimum_x, 6.0],
                [minimum_x, 0.0],
            ]],
        },
    }


def _room_with_two_passages():
    room = _rectangular_room()
    room["geometry"]["coordinates"].append([
        [2.0, 2.5], [8.0, 2.5], [8.0, 3.5],
        [2.0, 3.5], [2.0, 2.5],
    ])
    room["properties"]["area_m2"] = 54.0
    return room


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


def _point_in_ring(point, ring):
    inside = False
    previous = ring[-1]
    for current in ring:
        crosses = (
            (current[1] > point[1]) != (previous[1] > point[1])
            and point[0] < (
                (previous[0] - current[0])
                * (point[1] - current[1])
                / (previous[1] - current[1])
                + current[0]
            )
        )
        if crosses:
            inside = not inside
        previous = current
    return inside


def test_divider_line_splits_one_room_and_reports_geometry_area():
    """A valid divider must report each exported polygon's actual area."""
    parts = split_room_feature(
        _rectangular_room(),
        [[5.0, 0.0], [5.0, 6.0]],
    )

    assert [part["id"] for part in parts] == ["room-1-a", "room-1-b"]
    assert all(part["properties"]["role"] == "room" for part in parts)
    assert all(part["properties"]["edited"] for part in parts)
    assert all(part["properties"]["split_from"] == "room-1" for part in parts)
    assert (
        parts[0]["properties"]["color"]
        != parts[1]["properties"]["color"]
    )
    total_area = sum(part["properties"]["area_m2"] for part in parts)
    assert total_area == pytest.approx(60.0, abs=0.5)
    assert all(
        part["properties"]["area_m2"] == pytest.approx(
            _geometry_area(part["geometry"]), abs=0.01
        )
        for part in parts
    )
    assert all(
        part["properties"]["area_m2"] >= 29.0
        for part in parts
    )
    assert all(part["properties"]["clearance_m"] > 0 for part in parts)


def test_concave_room_gets_an_interior_representative_point():
    """Navigation metadata must not reuse a centroid outside a concave Room."""
    room = _rectangular_room()
    room["geometry"] = {
        "type": "Polygon",
        "coordinates": [[
            [0.0, 0.0], [8.0, 0.0], [8.0, 2.0], [2.0, 2.0],
            [2.0, 6.0], [8.0, 6.0], [8.0, 8.0], [0.0, 8.0],
            [0.0, 0.0],
        ]],
    }
    room["properties"]["centroid"] = [4.0, 4.0]

    point, clearance = room_representative_point(room["geometry"])
    normalized = normalize_room_feature(room)

    assert not _point_in_ring([4.0, 4.0], room["geometry"]["coordinates"][0])
    assert _point_in_ring(point, room["geometry"]["coordinates"][0])
    assert normalized["properties"]["representative_point"] == point
    assert normalized["properties"]["clearance_m"] == clearance
    assert clearance > 0.5


def test_divider_points_must_be_near_the_selected_room_walls():
    """The API must reject a divider far from its Room walls."""
    with pytest.raises(ValueError, match="near a Room wall"):
        split_room_feature(
            _rectangular_room(),
            [[-1.0, -1.0], [-0.5, -0.5]],
        )


def test_control_points_bend_one_wall_to_wall_divider():
    """Connected control points may form an editable right-angle path."""
    parts = split_room_feature(
        _rectangular_room(),
        [[3.0, 0.0], [3.0, 3.0], [7.0, 3.0], [7.0, 6.0]],
    )

    assert len(parts) == 2
    assert sum(
        part["properties"]["area_m2"] for part in parts
    ) == pytest.approx(60.0, abs=0.7)


def test_independent_dividers_do_not_connect_to_each_other():
    """Two point pairs close two passages without a cross-connection."""
    parts = split_room_feature(
        _room_with_two_passages(),
        [
            [[0.0, 3.0], [2.0, 3.0]],
            [[8.0, 3.0], [10.0, 3.0]],
        ],
    )

    assert len(parts) == 2
    assert sum(
        _geometry_area(part["geometry"]) for part in parts
    ) == pytest.approx(
        _geometry_area(_room_with_two_passages()["geometry"]),
        abs=0.01,
    )


def test_divider_requires_at_least_two_points():
    """A single wall point cannot define a Room divider."""
    with pytest.raises(ValueError, match="at least two finite"):
        split_room_feature(_rectangular_room(), [[5.0, 3.0]])


def test_near_wall_points_snap_to_the_room_boundary():
    """A user does not need pixel-perfect wall clicks."""
    parts = split_room_feature(
        _rectangular_room(),
        [[5.0, 0.18], [5.0, 5.82]],
    )

    assert len(parts) == 2


def test_divider_rejects_a_tiny_accidental_fragment():
    """A cut near an edge must not create an unusably small Room."""
    with pytest.raises(ValueError, match="exactly two meaningful areas"):
        split_room_feature(
            _rectangular_room(),
            [[0.2, 0.0], [0.2, 6.0]],
            minimum_room_area=2.0,
        )


def test_split_rooms_can_be_merged_back_without_losing_area():
    """Two adjacent edited Rooms must merge into one complete Room."""
    original = _rectangular_room()
    original["properties"]["name"] = "거실"
    original["properties"]["category"] = "living_room"
    parts = split_room_feature(
        original,
        [[5.0, 0.0], [5.0, 6.0]],
    )

    merged = merge_room_features(parts)

    assert merged["properties"]["role"] == "room"
    assert merged["properties"]["edited"] is True
    assert merged["properties"]["generated"] is False
    assert merged["properties"]["merged_from"] == [
        "room-1-a",
        "room-1-b",
    ]
    assert [part["properties"]["name"] for part in parts] == [
        "거실 A",
        "거실 B",
    ]
    assert merged["properties"]["name"] == "거실"
    assert merged["properties"]["merged_from_names"] == [
        "거실 A",
        "거실 B",
    ]
    assert merged["properties"]["category"] == "living_room"
    assert merged["properties"]["area_m2"] == pytest.approx(60.0)
    assert merged["geometry"]["type"] == "Polygon"
    assert merged["geometry"] == original["geometry"]


def test_repeated_split_merge_cycles_do_not_erode_room_geometry():
    """Editor round trips must never grow white gaps inside a Room."""
    room = _rectangular_room()
    original_geometry = room["geometry"]

    for _ in range(5):
        parts = split_room_feature(
            room,
            [[5.0, 0.0], [5.0, 6.0]],
        )
        room = merge_room_features(parts)
        assert room["geometry"] == original_geometry
        assert room["properties"]["area_m2"] == pytest.approx(60.0)


def test_merge_of_different_room_types_becomes_unassigned():
    """A merge must not silently keep one of two conflicting meanings."""
    parts = split_room_feature(
        _rectangular_room(),
        [[5.0, 0.0], [5.0, 6.0]],
    )
    parts[0]["properties"]["category"] = "living_room"
    parts[1]["properties"]["category"] = "kitchen"

    merged = merge_room_features(parts)

    assert merged["properties"]["category"] == "unassigned"


def test_merge_rejects_rooms_that_do_not_touch():
    """A user must not create one Room from disconnected floor areas."""
    with pytest.raises(ValueError, match="adjacent Rooms"):
        merge_room_features([
            _rectangular_room("room-1", 0.0),
            _rectangular_room("room-2", 20.0),
        ])


def test_merge_accepts_the_one_pixel_gap_from_independent_vectorization():
    """Rounding must not prevent merging adjacent auto-generated Rooms."""
    merged = merge_room_features([
        _rectangular_room("room-1", 0.0),
        _rectangular_room("room-2", 10.10),
    ], resolution=0.05)

    assert merged["properties"]["merged_from"] == [
        "room-1", "room-2",
    ]
    assert merged["geometry"]["type"] == "Polygon"
    assert merged["properties"]["area_m2"] == pytest.approx(
        _geometry_area(merged["geometry"]), abs=0.01
    )


def test_nested_splits_have_unique_names_and_restore_their_lineage():
    """Splitting a child Room must not duplicate a sibling's label."""
    original = _rectangular_room()
    original["properties"]["name"] = "거실"
    outer_parts = split_room_feature(
        original,
        [[5.0, 0.0], [5.0, 6.0]],
    )
    inner_parts = split_room_feature(
        outer_parts[0],
        [[2.5, 0.0], [2.5, 6.0]],
    )

    names = {
        outer_parts[1]["properties"]["name"],
        *(part["properties"]["name"] for part in inner_parts),
    }
    assert names == {"거실 B", "거실 A-A", "거실 A-B"}

    restored_a = merge_room_features(inner_parts)
    assert restored_a["properties"]["name"] == "거실 A"
    assert restored_a["properties"]["split_path"] == "A"

    restored_original = merge_room_features([
        restored_a, outer_parts[1],
    ])
    assert restored_original["properties"]["name"] == "거실"
    assert restored_original["geometry"] == original["geometry"]


def test_deeply_split_rooms_keep_area_equal_to_their_geometry():
    """Repeated vectorization must not lose floor between child Rooms."""
    original = _rectangular_room()
    leaves = [original]

    for _ in range(5):
        room = leaves.pop(0)
        points = room["geometry"]["coordinates"][0]
        minimum_x = min(point[0] for point in points)
        maximum_x = max(point[0] for point in points)
        divider_x = (minimum_x + maximum_x) / 2.0
        parts = split_room_feature(
            room,
            [[divider_x, 0.0], [divider_x, 6.0]],
            minimum_room_area=0.1,
        )
        for part in parts:
            assert part["properties"]["area_m2"] == pytest.approx(
                _geometry_area(part["geometry"]), abs=0.01
            )
        leaves = [parts[0], *leaves, parts[1]]
        assert sum(
            _geometry_area(part["geometry"]) for part in leaves
        ) == pytest.approx(
            _geometry_area(original["geometry"]), abs=0.01
        )
