"""Tests for Room-derived patrol routes and common mode state mapping."""

import json

import pytest

from malbut_gazebo.drive_modes import (
    AUTONOMOUS_MODES,
    TRIGGER_DRIVE_MODES,
    build_room_patrol_route,
    common_mode_state,
    write_room_patrol_route,
)
from malbut_patrol.route_loader import load_route


def _user_map():
    return {
        "type": "FeatureCollection",
        "map_id": "home-map",
        "features": [
            {
                "type": "Feature",
                "id": "living-room",
                "properties": {
                    "role": "room",
                    "name": "거실",
                    "representative_point": [1.0, 2.0],
                },
                "geometry": {"type": "Polygon", "coordinates": []},
            },
            {
                "type": "Feature",
                "id": "bed-room",
                "properties": {
                    "role": "room",
                    "name": "침실",
                    "representative_point": [4.0, 2.0],
                },
                "geometry": {"type": "Polygon", "coordinates": []},
            },
        ],
    }


def test_common_modes_separate_service_and_action_transports():
    assert AUTONOMOUS_MODES == {
        "patrol", "person_following", "roaming",
    }
    assert TRIGGER_DRIVE_MODES == {"patrol", "roaming"}


def test_room_patrol_uses_safe_room_points_and_faces_the_next_room():
    route = build_room_patrol_route(_user_map(), "home-map")

    assert route["schedule"] == {"mode": "manual"}
    assert route["route"]["map_id"] == "home-map"
    assert [item["name"] for item in route["route"]["waypoints"]] == [
        "거실", "침실",
    ]
    assert route["route"]["waypoints"][0]["pose"] == {
        "x": 1.0, "y": 2.0, "yaw": 0.0,
    }


def test_room_patrol_is_atomic_and_loadable_by_patrol_manager(tmp_path):
    user_map = tmp_path / "user-map.geojson"
    route_path = tmp_path / "room-patrol.yaml"
    user_map.write_text(json.dumps(_user_map()), encoding="utf-8")

    write_room_patrol_route(user_map, route_path, "home-map")
    route = load_route(route_path)

    assert route.map_id == "home-map"
    assert len(route.waypoints) == 2
    assert not list(tmp_path.glob(".room-patrol.yaml.*.tmp"))


def test_room_patrol_rejects_the_wrong_map_or_missing_room_points():
    with pytest.raises(ValueError, match="identity"):
        build_room_patrol_route(_user_map(), "other-map")
    value = _user_map()
    for feature in value["features"]:
        feature["properties"].pop("representative_point")
        feature["properties"]["centroid"] = [100.0, 100.0]
    with pytest.raises(ValueError, match="대표 위치"):
        build_room_patrol_route(value, "home-map")


@pytest.mark.parametrize(
    ("mode", "state", "expected"),
    [
        ("patrol", "navigating", "active"),
        ("patrol", "pausing", "pausing"),
        ("patrol", "aborted", "failed"),
        ("patrol", "completed", "idle"),
        ("roaming", "selecting", "active"),
        ("roaming", "paused", "paused"),
        ("roaming", "idle", "idle"),
    ],
)
def test_manager_states_have_one_web_contract(mode, state, expected):
    assert common_mode_state(mode, {"state": state}) == expected
