"""Resolve names from the applied User Map of Manager's active saved map.

The web editor supplies room names and representative points. Read the map's
``*.user-map.geojson`` on each request and recheck its occupancy revision and
content digest before dispatch. Never build a User Map, infer a room centroid,
or accept model-generated coordinates. Manager/Nav2 still own execution checks.
"""

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

import cv2
import numpy as np
import yaml


MAX_USER_MAP_BYTES = 8 * 1024 * 1024
MAX_MAP_BYTES = 64 * 1024
MAX_MAP_IMAGE_BYTES = 64 * 1024 * 1024
MAX_LOCATIONS = 128
MAX_LOCATION_CHARS = 128
MAX_ABS_COORDINATE_M = 10_000.0


class NavigationTargetError(ValueError):
    """A stable failure code without leaking filesystem details to a model."""

    def __init__(self, code: str):
        """Expose a stable code without leaking configuration contents."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NavigationTarget:
    """An immutable pose binding; ``arguments`` returns a detached ROS payload."""

    location: str
    map_path: str
    frame_id: str
    x: float
    y: float
    yaw: float
    digest: str

    @property
    def arguments(self) -> dict:
        """Return a detached pose for the Manager's navigation capability."""
        return {
            'pose': {
                'header': {'frame_id': self.frame_id},
                'pose': {
                    'position': {'x': self.x, 'y': self.y, 'z': 0.0},
                    'orientation': {
                        'x': 0.0, 'y': 0.0,
                        'z': math.sin(self.yaw / 2.0),
                        'w': math.cos(self.yaw / 2.0),
                    },
                },
            },
            'behavior_tree': '',
        }


class _StrictLoader(yaml.SafeLoader):
    """Reject duplicate keys, aliases and deeply nested YAML before loading."""

    def __init__(self, stream):
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent, index):
        self._nodes += 1
        if (self.check_event(yaml.AliasEvent) or self._depth >= 8
                or self._nodes > 4096):
            raise NavigationTargetError('catalog_invalid')
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise NavigationTargetError('catalog_invalid')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _document(content: bytes) -> dict:
    try:
        value = yaml.load(content.decode('utf-8'), Loader=_StrictLoader)
    except (UnicodeError, yaml.YAMLError, RecursionError, ValueError) as error:
        raise NavigationTargetError('catalog_invalid') from error
    if not isinstance(value, dict):
        raise NavigationTargetError('catalog_invalid')
    return value


def _read(path: Path, limit: int, code: str) -> bytes:
    try:
        if not path.is_file():
            raise NavigationTargetError(code)
        with path.open('rb') as stream:
            content = stream.read(limit + 1)
    except (OSError, ValueError) as error:
        raise NavigationTargetError(code) from error
    if not content or len(content) > limit:
        raise NavigationTargetError(code)
    return content


def _label(value, code):
    if (not isinstance(value, str) or not value.strip()
            or len(value) > MAX_LOCATION_CHARS
            or any(unicodedata.category(char).startswith('C') for char in value)):
        raise NavigationTargetError(code)
    return unicodedata.normalize('NFC', value.strip())


def explicitly_names_location(utterance, location, locations):
    """Bind a proposed name to a source span, without interpreting action intent."""
    text = unicodedata.normalize('NFC', utterance.strip())
    key = _label(location, 'target_invalid')
    names = tuple(_label(name, 'catalog_invalid') for name in locations)
    if key not in names:
        return False
    start = text.find(key)
    while start >= 0:
        end = start + len(key)
        left = text[start - 1] if start else ''
        tail = text[end:]
        right_boundary = (
            not tail or not (tail[0].isalnum() or tail[0] == '_')
            or tail.startswith(('으로', '로', '에', '까지'))
        )
        covered = False
        for name in names:
            if len(name) <= len(key):
                continue
            position = text.find(name, max(0, end - len(name)), start + len(name))
            if position >= 0 and position <= start and position + len(name) >= end:
                covered = True
                break
        if not (left.isalnum() or left == '_') and right_boundary and not covered:
            return True
        start = text.find(key, start + 1)
    return False


def matches_navigation_location(utterance, location, locations):
    """Accept an exact name or one unambiguous, closely matching source name."""
    if explicitly_names_location(utterance, location, locations):
        return True
    text = unicodedata.normalize('NFC', utterance.strip())
    key = _label(location, 'target_invalid')
    names = tuple(_label(name, 'catalog_invalid') for name in locations)
    if key not in names:
        return False
    sources = set()
    for word in re.findall(r'\w+', text):
        cuts = [position for particle in ('으로', '로', '에', '까지')
                if (position := word.find(particle)) > 0]
        if cuts:
            sources.add(word[:min(cuts)])
        elif word == text:
            sources.add(word)
    for source in sources:
        if (source in names or len(source) > MAX_LOCATION_CHARS
                or not explicitly_names_location(text, source, names + (source,))):
            continue
        # Room numbers and other non-Hangul identifiers are never corrected.
        identifier = ''.join(char for char in source if not '\uac00' <= char <= '\ud7a3')
        scores = {}
        for name in names:
            if identifier != ''.join(char for char in name
                                     if not '\uac00' <= char <= '\ud7a3'):
                scores[name] = 0.0
            else:
                scores[name] = SequenceMatcher(
                    None, unicodedata.normalize('NFD', source),
                    unicodedata.normalize('NFD', name), autojunk=False,
                ).ratio()
        runner_up = max((score for name, score in scores.items() if name != key), default=0.0)
        if scores[key] >= 0.8 and scores[key] - runner_up >= 0.15:
            return True
    return False


def _absolute_map(value, code):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise NavigationTargetError(code)
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() not in {'.yaml', '.yml'}:
        raise NavigationTargetError(code)
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise NavigationTargetError(code) from error


def _coordinate(value, bound):
    if type(value) not in (int, float):
        raise NavigationTargetError('catalog_invalid')
    try:
        result = float(value)
    except OverflowError as error:
        raise NavigationTargetError('catalog_invalid') from error
    if not math.isfinite(result) or abs(result) > bound:
        raise NavigationTargetError('catalog_invalid')
    return result


class NavigationTargets:
    """Reload applied rooms from the currently selected map on every resolution."""

    def resolve(self, location: str, active_map: str | None) -> NavigationTarget:
        """Bind one exact room name to its saved representative point."""
        key = _label(location, 'target_invalid')
        map_path, poses, binding = self._catalog(active_map)
        if key not in poses:
            raise NavigationTargetError('target_not_found')
        digest = hashlib.sha256()
        for part in (b'malbut-speech-user-map-v1', str(map_path).encode(), *binding, key.encode()):
            digest.update(len(part).to_bytes(8, 'big'))
            digest.update(part)
        x, y = poses[key]
        return NavigationTarget(key, str(map_path), 'map', x, y, 0.0, digest.hexdigest())

    def names(self, active_map: str | None) -> tuple[str, ...]:
        """Expose only names from the applied, revision-matched User Map."""
        _, poses, _ = self._catalog(active_map)
        return tuple(poses)

    def _catalog(self, active_map):
        if not active_map:
            raise NavigationTargetError('map_unavailable')
        map_path = _absolute_map(active_map, 'map_unavailable')
        raw = _read(map_path.with_suffix('.user-map.geojson'), MAX_USER_MAP_BYTES,
                    'catalog_unavailable')
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise NavigationTargetError('catalog_invalid') from error
        if (not isinstance(document, dict) or document.get('type') != 'FeatureCollection'
                or document.get('format') != 'malbut-user-map-v1'
                or document.get('frame_id') != 'map'
                or not isinstance(document.get('features'), list)):
            raise NavigationTargetError('catalog_invalid')
        map_raw, image_digest, revision = self._map_binding(map_path)
        if document.get('map_revision') != revision:
            raise NavigationTargetError('map_mismatch')
        poses = {}
        for feature in document['features']:
            if not isinstance(feature, dict) or not isinstance(feature.get('properties'), dict):
                raise NavigationTargetError('catalog_invalid')
            properties = feature['properties']
            if properties.get('role') != 'room':
                continue
            name = _label(properties.get('name'), 'catalog_invalid')
            if name in poses:
                raise NavigationTargetError('target_ambiguous')
            point = properties.get('representative_point')
            if not isinstance(point, list) or len(point) != 2:
                raise NavigationTargetError('catalog_invalid')
            poses[name] = tuple(_coordinate(value, MAX_ABS_COORDINATE_M) for value in point)
            if len(poses) > MAX_LOCATIONS:
                raise NavigationTargetError('catalog_invalid')
        return map_path, poses, (raw, map_raw, image_digest)

    @staticmethod
    def _map_binding(map_path):
        map_raw = _read(map_path, MAX_MAP_BYTES, 'catalog_unavailable')
        metadata = _document(map_raw)
        image = metadata.get('image')
        if (not isinstance(image, str) or not image or len(image) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in image)):
            raise NavigationTargetError('catalog_invalid')
        try:
            image_path = (map_path.parent / Path(image).expanduser()).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise NavigationTargetError('catalog_unavailable') from error
        image_raw = _read(image_path, MAX_MAP_IMAGE_BYTES, 'catalog_unavailable')
        occupancy = cv2.imdecode(np.frombuffer(image_raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if occupancy is None:
            raise NavigationTargetError('catalog_invalid')
        resolution = _coordinate(metadata.get('resolution'), MAX_ABS_COORDINATE_M)
        origin = metadata.get('origin')
        if resolution <= 0 or not isinstance(origin, list) or len(origin) != 3:
            raise NavigationTargetError('catalog_invalid')
        origin = [_coordinate(value, MAX_ABS_COORDINATE_M) for value in origin]
        occupied = _coordinate(metadata.get('occupied_thresh', 0.65), 1.0)
        free = _coordinate(metadata.get('free_thresh', 0.196), 1.0)
        negate = metadata.get('negate', 0)
        mode = str(metadata.get('mode', 'trinary')).strip().lower()
        if (not 0 <= free < occupied <= 1 or mode != 'trinary'
                or type(negate) not in (bool, int) or negate not in (0, 1)):
            raise NavigationTargetError('catalog_invalid')
        # Match bringup.user_map's occupancy revision without depending on bringup,
        # which already depends on Agent. Integration tests use its actual saver.
        revision_data = {
            'shape': list(occupancy.shape), 'resolution': resolution, 'origin': origin,
            'negate': bool(negate), 'occupied_thresh': occupied,
            'free_thresh': free, 'mode': mode,
        }
        digest = hashlib.sha256(json.dumps(
            revision_data, sort_keys=True, separators=(',', ':'),
        ).encode())
        digest.update(occupancy.tobytes())
        return map_raw, hashlib.sha256(image_raw).digest(), 'rev-' + digest.hexdigest()[:12]
