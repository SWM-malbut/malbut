"""Behavioral tests for semantic room-route selection."""

from pathlib import Path

from malbut_scenarios.scenario_config import (
    load_room_routes,
    room_for_goal,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_room_routes_are_valid_and_cover_demo_house_regions():
    frame, rooms = load_room_routes(
        PACKAGE_ROOT / 'config' / 'room_routes.yaml'
    )

    assert frame == 'map'
    assert {room.room_id for room in rooms} == {
        'left_room',
        'center_south',
        'center_north',
        'right_room',
    }
    assert room_for_goal(rooms, -6.0, -2.0).room_id == 'left_room'
    assert room_for_goal(rooms, 1.0, -2.0).room_id == 'center_south'
    assert room_for_goal(rooms, 1.0, 3.0).room_id == 'center_north'
    assert room_for_goal(rooms, 7.0, 0.0).room_id == 'right_room'


def test_room_patrol_starts_at_the_waypoint_nearest_the_web_goal():
    _, rooms = load_room_routes(
        PACKAGE_ROOT / 'config' / 'room_routes.yaml'
    )
    room = room_for_goal(rooms, 7.1, -3.2)

    ordered = room.ordered_from(7.1, -3.2)

    assert ordered[0].x == 7.0
    assert ordered[0].y == -3.25
    assert set(ordered) == set(room.waypoints)
