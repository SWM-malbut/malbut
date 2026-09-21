"""A small walled room: a saved map, its /map message and LiDAR scans in it."""

import math
from types import SimpleNamespace

from nav_msgs.msg import OccupancyGrid
import pytest
from sensor_msgs.msg import LaserScan

RESOLUTION = 0.05
ORIGIN = (-2.0, -2.0)
SIZE = 80
# Wall faces of a 3 m x 2 m room; an L-shaped inner wall breaks the symmetry.
ROOM = (-1.5, 1.5, -1.0, 1.0)
INNER = ((0.4, 1.0, -1.0, -0.2),)


def _occupied(column, row):
    x = ORIGIN[0] + (column + 0.5) * RESOLUTION
    y = ORIGIN[1] + (row + 0.5) * RESOLUTION
    left, right, bottom, top = ROOM
    wall = ((left - 0.1 <= x <= left or right <= x <= right + 0.1)
            and bottom - 0.1 <= y <= top + 0.1) or (
            (bottom - 0.1 <= y <= bottom or top <= y <= top + 0.1)
            and left - 0.1 <= x <= right + 0.1)
    inner = any(x0 <= x <= x0 + 0.1 and y0 <= y <= y1 for x0, _, y0, y1 in INNER)
    return wall or inner


def _ray(x, y, angle):
    """Distance from (x, y) to the first wall face along angle."""
    best = math.inf
    dx, dy = math.cos(angle), math.sin(angle)
    left, right, bottom, top = ROOM
    for face, vertical in ((left, True), (right, True), (bottom, False), (top, False)):
        delta = dx if vertical else dy
        if abs(delta) < 1e-9:
            continue
        distance = (face - (x if vertical else y)) / delta
        if distance > 1e-6:
            best = min(best, distance)
    for x0, _, y0, y1 in INNER:
        for face in (x0, x0 + 0.1):
            if abs(dx) < 1e-9:
                continue
            distance = (face - x) / dx
            if distance > 1e-6 and y0 <= y + distance * dy <= y1:
                best = min(best, distance)
    return best


@pytest.fixture
def room(tmp_path):
    """Write the room as a saved map and build matching ROS messages."""
    rows = []
    for row in range(SIZE - 1, -1, -1):  # PGM row 0 is the top of the map.
        rows.append(bytes(0 if _occupied(column, row) else 254 for column in range(SIZE)))
    image = tmp_path / 'room.pgm'
    image.write_bytes(f'P5\n{SIZE} {SIZE}\n255\n'.encode() + b''.join(rows))
    map_file = tmp_path / 'room.yaml'
    map_file.write_text(
        f'image: room.pgm\nresolution: {RESOLUTION}\norigin: [{ORIGIN[0]}, {ORIGIN[1]}, 0.0]\n'
        'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\nmode: trinary\n')

    def grid():
        message = OccupancyGrid()
        message.header.frame_id = 'map'
        message.info.width = message.info.height = SIZE
        message.info.resolution = RESOLUTION
        message.info.origin.position.x, message.info.origin.position.y = ORIGIN
        message.info.origin.orientation.w = 1.0
        message.data = [100 if _occupied(column, row) else 0
                        for row in range(SIZE) for column in range(SIZE)]
        return message

    def scan(x, y, yaw, beams=360):
        message = LaserScan()
        message.header.frame_id = 'laser'
        message.angle_min = -math.pi
        message.angle_increment = 2 * math.pi / beams
        message.range_min, message.range_max = 0.05, 12.0
        message.ranges = [_ray(x, y, yaw + message.angle_min + index * message.angle_increment)
                          for index in range(beams)]
        return message

    return SimpleNamespace(map_file=map_file, grid=grid, scan=scan)
