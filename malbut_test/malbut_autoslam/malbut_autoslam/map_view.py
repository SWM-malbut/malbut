"""
Find a dominant wall direction and persist display-only map alignment.

OpenCV's progressive probabilistic Hough transform supplies wall segments.
Angles are folded modulo 90 degrees, as in Manhattan-frame pose normalization
(Huebner et al., arXiv:2107.07778). Only a well-supported direction is accepted;
the occupancy image, ROS map metadata and robot poses are never rewritten.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import cv2
import numpy as np
import yaml


METHOD = 'hough-manhattan-v1'
QUARTER_TURN = math.pi / 2


def _fold(angle):
    return (angle + math.pi / 4) % QUARTER_TURN - math.pi / 4


def estimate_view(occupied, resolution):
    """Return an image-space rotation; weak or competing wall evidence keeps zero."""
    height, width = occupied.shape
    scale = min(1.0, 1536 / max(height, width))
    image = np.asarray(occupied, dtype=np.uint8) * 255
    if scale < 1.0:
        # Area pooling keeps thin walls visible in a bounded analysis image.
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        image = np.where(image > 32, 255, 0).astype(np.uint8)
    pixel_size = resolution / scale
    minimum = max(12, round(0.8 / pixel_size))
    edges = cv2.Canny(image, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, math.pi / 720, max(12, minimum // 2),
                            minLineLength=minimum, maxLineGap=max(2, round(0.15 / pixel_size)))
    empty = {'rotation_rad': 0.0, 'confidence': 0.0, 'method': METHOD}
    if lines is None or len(lines) < 3:
        return empty
    segments = lines.reshape(-1, 4).astype(float)
    dx, dy = segments[:, 2] - segments[:, 0], segments[:, 3] - segments[:, 1]
    weights = np.hypot(dx, dy)
    angles = _fold(np.arctan2(dy, dx))
    # Circular histogram: horizontal/vertical walls reinforce one direction,
    # including the wrap between -45 and +45 degrees.
    bins = np.floor((angles + math.pi / 4) / math.radians(1)).astype(int) % 90
    histogram = np.bincount(bins, weights=weights, minlength=90)
    votes = sum(np.roll(histogram, shift) for shift in range(-3, 4))
    peak = int(np.argmax(votes))
    center = math.radians(peak - 45 + 0.5)
    residual = _fold(angles - center)
    selected = np.abs(residual) <= math.radians(4)
    confidence = float(weights[selected].sum() / weights.sum())
    if (confidence < 0.65 or selected.sum() < 3
            or weights[selected].sum() * pixel_size < 3.0):
        return {**empty, 'confidence': round(confidence, 4)}
    # Weighted median rejects shorter furniture edges and stray diagonal beams.
    values, lengths = residual[selected], weights[selected]
    order = np.argsort(values)
    midpoint = np.searchsorted(np.cumsum(lengths[order]), lengths.sum() / 2)
    direction = float(_fold(center + values[order[midpoint]]))
    if abs(direction) < math.radians(0.5):
        direction = 0.0
    return {'rotation_rad': -direction, 'confidence': round(confidence, 4), 'method': METHOD}


def _source(map_yaml):
    path = Path(map_yaml).expanduser().resolve()
    content = path.read_bytes()
    try:
        metadata = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ValueError('invalid saved map YAML') from error
    if not isinstance(metadata, dict) or not isinstance(metadata.get('image'), str):
        raise ValueError('saved map has no image')
    image = Path(metadata['image']).expanduser()
    if not image.is_absolute():
        image = path.parent / image
    pixels = image.read_bytes()
    signature = hashlib.sha256(content + pixels).hexdigest()
    return path, metadata, pixels, signature


def write_map_view(map_yaml):
    """Atomically store one alignment beside a saved map, without changing that map."""
    path, metadata, content, signature = _source(map_yaml)
    pixels = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if pixels is None:
        raise ValueError('saved map image cannot be read')
    resolution = float(metadata['resolution'])
    origin = metadata['origin']
    if (not math.isfinite(resolution) or resolution <= 0
            or not isinstance(origin, list) or len(origin) != 3
            or not all(type(item) in (float, int) and math.isfinite(item) for item in origin)):
        raise ValueError('invalid saved map geometry')
    negate = metadata.get('negate', 0)
    threshold = float(metadata.get('occupied_thresh', 0.65))
    if negate not in (0, 1) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError('invalid saved map occupancy settings')
    occupancy = pixels.astype(np.float32) / 255
    if not negate:
        occupancy = 1 - occupancy
    occupied = occupancy >= threshold
    if metadata.get('mode', 'trinary') == 'trinary':
        occupied &= pixels != 205  # Unknown cells are not walls.
    try:
        view = estimate_view(occupied, resolution)
    except cv2.error as error:
        raise ValueError(f'wall direction detection failed: {error}') from error
    view.update(map_signature=signature, source={
        'width': int(pixels.shape[1]), 'height': int(pixels.shape[0]),
        'resolution': resolution, 'origin': [float(item) for item in origin]})
    destination = path.with_suffix('.view.json')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', dir=path.parent, prefix='.map-view-', delete=False) as stream:
            temporary = stream.name
            json.dump(view, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    return view


def read_map_view(map_yaml):
    """Ignore absent, damaged or stale display settings; never prevent map loading."""
    try:
        path = Path(map_yaml).expanduser().resolve()
        view = json.loads(path.with_suffix('.view.json').read_text())
        if (not isinstance(view, dict) or view.get('method') != METHOD
                or type(view.get('rotation_rad')) not in (int, float)
                or not math.isfinite(view['rotation_rad'])
                or abs(view['rotation_rad']) > math.pi / 4):
            return None
        if _source(path)[3] != view.get('map_signature'):
            return None
        return view
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        return None
