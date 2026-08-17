"""Validated semantic-room routes for the autonomous-driving demo."""

from dataclasses import dataclass
import math
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Waypoint:
    """One map-frame room-patrol destination."""

    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class RoomRoute:
    """A demo room's rectangular selector and ordered patrol route."""

    room_id: str
    name: str
    minimum_x: float
    minimum_y: float
    maximum_x: float
    maximum_y: float
    waypoints: tuple[Waypoint, ...]

    def contains(self, x: float, y: float) -> bool:
        """Return whether a selected web goal belongs to this room."""
        return (
            self.minimum_x <= x <= self.maximum_x
            and self.minimum_y <= y <= self.maximum_y
        )

    def ordered_from(self, x: float, y: float) -> tuple[Waypoint, ...]:
        """Start the fixed circuit at the waypoint nearest the web goal."""
        start = min(
            range(len(self.waypoints)),
            key=lambda index: math.hypot(
                self.waypoints[index].x - x,
                self.waypoints[index].y - y,
            ),
        )
        return self.waypoints[start:] + self.waypoints[:start]


def load_room_routes(path: Path) -> tuple[str, tuple[RoomRoute, ...]]:
    """Load and validate the scenario room-route configuration."""
    value = yaml.safe_load(path.expanduser().read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('frame_id') != 'map':
        raise ValueError('room route frame_id must be map')
    source_rooms = value.get('rooms')
    if not isinstance(source_rooms, list) or not source_rooms:
        raise ValueError('room route configuration requires rooms')
    rooms = []
    identifiers = set()
    for source in source_rooms:
        if not isinstance(source, dict):
            raise ValueError('each room route must be a mapping')
        room_id = str(source.get('id', '')).strip()
        if not room_id or room_id in identifiers:
            raise ValueError('room IDs must be non-empty and unique')
        identifiers.add(room_id)
        bounds = source.get('bounds')
        if not isinstance(bounds, dict):
            raise ValueError(f'{room_id} requires bounds')
        try:
            minimum_x = float(bounds['minimum_x'])
            minimum_y = float(bounds['minimum_y'])
            maximum_x = float(bounds['maximum_x'])
            maximum_y = float(bounds['maximum_y'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f'{room_id} bounds must be numeric') from error
        if not all(math.isfinite(item) for item in (
            minimum_x, minimum_y, maximum_x, maximum_y
        )):
            raise ValueError(f'{room_id} bounds must be finite')
        if minimum_x >= maximum_x or minimum_y >= maximum_y:
            raise ValueError(f'{room_id} bounds are invalid')
        source_waypoints = source.get('waypoints')
        if not isinstance(source_waypoints, list) or not source_waypoints:
            raise ValueError(f'{room_id} requires patrol waypoints')
        waypoints = []
        for source_waypoint in source_waypoints:
            try:
                waypoint = Waypoint(
                    float(source_waypoint['x']),
                    float(source_waypoint['y']),
                    float(source_waypoint.get('yaw', 0.0)),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f'{room_id} waypoint values must be numeric'
                ) from error
            if not all(math.isfinite(item) for item in (
                waypoint.x, waypoint.y, waypoint.yaw
            )):
                raise ValueError(f'{room_id} waypoints must be finite')
            if not (
                minimum_x <= waypoint.x <= maximum_x
                and minimum_y <= waypoint.y <= maximum_y
            ):
                raise ValueError(f'{room_id} waypoint is outside its bounds')
            waypoints.append(waypoint)
        rooms.append(RoomRoute(
            room_id=room_id,
            name=str(source.get('name', room_id)),
            minimum_x=minimum_x,
            minimum_y=minimum_y,
            maximum_x=maximum_x,
            maximum_y=maximum_y,
            waypoints=tuple(waypoints),
        ))
    return 'map', tuple(rooms)


def room_for_goal(
    rooms: tuple[RoomRoute, ...],
    x: float,
    y: float,
) -> RoomRoute | None:
    """Resolve a web-selected coordinate to its configured room."""
    return next((room for room in rooms if room.contains(x, y)), None)
