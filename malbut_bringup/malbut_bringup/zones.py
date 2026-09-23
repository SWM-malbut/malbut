"""
Turn saved-map Zones into a Nav2 keepout filter mask.

Ported from malbut_gazebo's zone_filter_mask (SWM25-81) to the robot's saved
maps. The Zone GeoJSON format and costs are the same; the simulation User Map,
wall-clearance costs and preferred goals are not used. Wall clearance stays
with the robot's inflation layer.
"""

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import yaml


ZONE_FORMAT = 'malbut-semantic-zones-v1'
# Raw mask values: Nav2 scales 0..100 to costmap cost, 100 is lethal.
COSTS = {'allow': 0, 'avoid': 70, 'restricted': 100}
MAX_ZONES = 64
MAX_POINTS = 64
MIN_AREA_M2 = 0.01


class ZoneError(ValueError):
    """Zones cannot be read, validated or applied to this map."""


def zones_path(map_yaml):
    """Keep a map's Zones beside it: home.yaml -> home.zones.geojson."""
    return Path(map_yaml).with_suffix('.zones.geojson')


def map_identity(map_yaml):
    """Hash map metadata and image so a replaced map drops its old Zones."""
    path = Path(map_yaml).expanduser().resolve()
    content = path.read_bytes()
    metadata = yaml.safe_load(content)
    image = Path(metadata['image']).expanduser()
    if not image.is_absolute():
        image = path.parent / image
    digest = hashlib.sha256(content)
    with image.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_zone(feature):
    """Check one Zone Feature and return its behavior and outer ring."""
    if not isinstance(feature, dict) or feature.get('type') != 'Feature':
        raise ZoneError('every Zone must be a GeoJSON Feature')
    properties = feature.get('properties')
    if not isinstance(properties, dict) or properties.get('role') != 'semantic_zone':
        raise ZoneError('every Zone needs semantic_zone properties')
    behavior = properties.get('behavior')
    if behavior not in COSTS:
        raise ZoneError(f'unsupported Zone behavior: {behavior}')
    geometry = feature.get('geometry')
    if not isinstance(geometry, dict) or geometry.get('type') != 'Polygon':
        raise ZoneError('Zone geometry must be a Polygon')
    rings = geometry.get('coordinates')
    if not isinstance(rings, list) or not rings:
        raise ZoneError('Zone Polygon must contain an outer ring')
    for ring in rings:
        if (not isinstance(ring, list) or not 4 <= len(ring) <= MAX_POINTS + 1
                or ring[0] != ring[-1]):
            raise ZoneError('Zone rings must be closed with 3 to 64 corners')
        for point in ring:
            if (not isinstance(point, list) or len(point) < 2
                    or not all(type(value) in (int, float) and math.isfinite(value)
                               for value in point[:2])):
                raise ZoneError('Zone coordinates must be finite [x, y] values')
    if _area(rings[0]) < MIN_AREA_M2:
        raise ZoneError(f'Zone area must be at least {MIN_AREA_M2} m²')
    return behavior, rings


def _area(ring):
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0, *_), (x1, y1, *_)
                   in zip(ring, ring[1:]))) / 2.0


def read_zones(map_yaml):
    """Return this map's Zone Features; a missing file means no Zones."""
    path = zones_path(map_yaml)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ZoneError(f'cannot read {path.name}: {error}') from error
    if (not isinstance(value, dict) or value.get('type') != 'FeatureCollection'
            or value.get('format') != ZONE_FORMAT or value.get('frame_id', 'map') != 'map'):
        raise ZoneError(f'{path.name} is not a {ZONE_FORMAT} FeatureCollection')
    if value.get('map_id') != map_identity(map_yaml):
        raise ZoneError(f'{path.name} was drawn for a different version of this map')
    features = value.get('features')
    if not isinstance(features, list) or len(features) > MAX_ZONES:
        raise ZoneError(f'{path.name} must list at most {MAX_ZONES} Zones')
    for feature in features:
        validate_zone(feature)
    return features


def write_zones(map_yaml, features):
    """Atomically replace this map's Zones after validating every Feature."""
    if not isinstance(features, list) or len(features) > MAX_ZONES:
        raise ZoneError(f'at most {MAX_ZONES} Zones are allowed')
    for feature in features:
        validate_zone(feature)
    document = {
        'type': 'FeatureCollection', 'format': ZONE_FORMAT, 'frame_id': 'map',
        'map_id': map_identity(map_yaml),
        'saved_at': datetime.now(timezone.utc).isoformat(), 'features': features,
    }
    _atomic_write(zones_path(map_yaml),
                  json.dumps(document, ensure_ascii=False, indent=1).encode('utf-8'))


def zone_feature(behavior, points, name=''):
    """Build a Zone Feature from an open list of [x, y] corners."""
    ring = [[float(x), float(y)] for x, y in points]
    feature = {
        'type': 'Feature',
        'properties': {'role': 'semantic_zone', 'behavior': behavior, 'name': str(name)},
        'geometry': {'type': 'Polygon', 'coordinates': [ring + [ring[0]]]},
    }
    validate_zone(feature)
    return feature


def build_mask(map_yaml, features, restricted_buffer_m=0.20):
    """Rasterize Zones on the saved map's grid; the highest cost wins."""
    import cv2
    import numpy as np

    map_yaml = Path(map_yaml)
    metadata = yaml.safe_load(map_yaml.read_text(encoding='utf-8'))
    resolution = float(metadata['resolution'])
    origin = [float(value) for value in metadata['origin']]
    if abs(origin[2]) > 1e-9:
        raise ZoneError('Nav2 costmap filter masks require a zero map origin yaw')
    image = Path(metadata['image'])
    if not image.is_absolute():
        image = map_yaml.parent / image
    grid = cv2.imread(str(image), cv2.IMREAD_UNCHANGED)
    if grid is None:
        raise ZoneError(f'cannot read map image {image}')
    height, width = grid.shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    buffer_cells = max(0, int(math.ceil(restricted_buffer_m / resolution)))

    def pixels(ring):
        # Image row 0 is the top (largest y) of the map.
        return np.array([[round((x - origin[0]) / resolution - 0.5),
                          round(height - (y - origin[1]) / resolution - 0.5)]
                         for x, y, *_ in ring], dtype=np.int32)

    for feature in features:
        behavior, rings = validate_zone(feature)
        if COSTS[behavior] == 0:
            continue
        zone = np.zeros_like(mask)
        cv2.fillPoly(zone, [pixels(rings[0])], 255)
        for hole in rings[1:]:
            cv2.fillPoly(zone, [pixels(hole)], 0)
        if behavior == 'restricted' and buffer_cells:
            # Nav2 checks the restricted cost at the robot center; the buffer keeps
            # the footprint out as well.
            size = 2 * buffer_cells + 1
            zone = cv2.dilate(zone, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size)))
        mask[zone > 0] = np.maximum(mask[zone > 0], COSTS[behavior])
    return mask, {'resolution': resolution, 'origin': [origin[0], origin[1], 0.0]}


def write_mask(output_yaml, mask, geometry):
    """Write a raw-mode Nav2 mask whose image values are the Zone costs."""
    import cv2

    output_yaml = Path(output_yaml)
    image = output_yaml.with_suffix('.pgm')
    ok, encoded = cv2.imencode('.pgm', mask)
    if not ok:
        raise OSError(f'cannot encode filter mask {image}')
    _atomic_write(image, encoded.tobytes())
    metadata = {'image': image.name, 'mode': 'raw', **geometry, 'negate': 0,
                'occupied_thresh': 1.0, 'free_thresh': 0.0}
    _atomic_write(output_yaml, yaml.safe_dump(metadata, sort_keys=False).encode('utf-8'))
    return output_yaml


def empty_mask(output_yaml):
    """Write a one-cell free mask: loaded when no saved map is selected."""
    import numpy as np

    return write_mask(output_yaml, np.zeros((1, 1), dtype=np.uint8),
                      {'resolution': 0.05, 'origin': [0.0, 0.0, 0.0]})


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='wb', dir=path.parent, prefix=f'.{path.name}-', delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
