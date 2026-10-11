"""Fixtures saved through the same User Map API as web rooms_save commands."""

from copy import deepcopy
import json
from pathlib import Path
import sys

import yaml


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'malbut_bringup'))
from malbut_bringup.user_map import load_slam_map, save_rooms  # noqa: E402


def room(name, x, y, identity='room-1'):
    """Use a representative point supplied by a normalized web room Feature."""
    return {
        'type': 'Feature', 'id': identity,
        'properties': {'role': 'room', 'room_id': identity, 'name': name,
                       'representative_point': [x, y]},
        'geometry': {'type': 'Polygon', 'coordinates': [[
            [x - 0.5, y - 0.5], [x + 0.5, y - 0.5],
            [x + 0.5, y + 0.5], [x - 0.5, y + 0.5], [x - 0.5, y - 0.5],
        ]]},
    }


def write_user_map(directory, *, map_name='home', rooms=None):
    """Apply web-shaped rooms to a real saved map and return its stored document."""
    directory = Path(directory)
    image = directory / (map_name + '.pgm')
    image.write_bytes(b'P5\n100 100\n255\n' + b'\xff' * 10_000)
    selected = directory / (map_name + '.yaml')
    selected.write_text(yaml.safe_dump({
        'image': image.name, 'resolution': 0.1, 'origin': [-5.0, -5.0, 0.0],
        'negate': 0, 'occupied_thresh': 0.65, 'free_thresh': 0.196,
    }), encoding='utf-8')
    slam_map = load_slam_map(selected, 'test-' + map_name)
    document = save_rooms(selected, slam_map.map_id, {
        'map_id': slam_map.map_id, 'map_revision': slam_map.map_revision,
        'rooms': deepcopy(rooms if rooms is not None else [room('거실', 1.25, -2.5)]),
    })
    return selected.with_suffix('.user-map.geojson'), selected, image, document


def rewrite(path, document):
    """Replace an already applied document without a cached Agent resolver."""
    path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')


def rooms(document):
    """Return only room Features, ignoring the walkable and wall geometry."""
    return [feature for feature in document['features']
            if feature['properties']['role'] == 'room']
