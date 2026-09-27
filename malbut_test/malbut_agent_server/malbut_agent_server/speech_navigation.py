"""Resolve explicit named poses on the saved map selected by Manager.

The server-owned YAML file has exactly ``map``, ``frame_id`` and ``locations``.
``map`` is an absolute Nav2 map YAML path, ``frame_id`` is ``map``, and each
location has exactly numeric ``x``, ``y`` and ``yaw`` (radians). No simulation
catalog, room centroid, model-generated coordinate or default pose is used.

Call ``NavigationTargets(path).resolve(location, active_map)`` on proposal and
again before submission. Compare the returned ``digest`` to reject intervening
catalog or map changes. ``active_map`` must come from Manager's LOCALIZATION
state, never from the language model. Resolution proves the configuration
binding, not localization quality or path reachability; Manager/Nav2 own those.
"""

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import unicodedata

import yaml


MAX_CONFIG_BYTES = 64 * 1024
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


def _digest_image(path: Path) -> bytes:
    digest = hashlib.sha256()
    count = 0
    try:
        if not path.is_file():
            raise NavigationTargetError('catalog_unavailable')
        with path.open('rb') as stream:
            while True:
                chunk = stream.read(min(1024 * 1024, MAX_MAP_IMAGE_BYTES + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_MAP_IMAGE_BYTES:
                    raise NavigationTargetError('catalog_invalid')
                digest.update(chunk)
    except OSError as error:
        raise NavigationTargetError('catalog_unavailable') from error
    if not count:
        raise NavigationTargetError('catalog_invalid')
    return digest.digest()


class NavigationTargets:
    """Reload a bounded server-configured catalog for every resolution."""

    def __init__(self, path: str | Path):
        """Retain an explicit local catalog path without loading ROS."""
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise NavigationTargetError('catalog_unavailable')
        self._path = Path(path).expanduser()

    def resolve(self, location: str, active_map: str | None) -> NavigationTarget:
        """Bind one exact name to the selected map, or raise a stable error."""
        key = _label(location, 'target_invalid')
        if not active_map:
            raise NavigationTargetError('map_unavailable')
        active_path = _absolute_map(active_map, 'map_unavailable')
        raw = _read(self._path, MAX_CONFIG_BYTES, 'catalog_unavailable')
        catalog = _document(raw)
        if set(catalog) != {'map', 'frame_id', 'locations'} or catalog['frame_id'] != 'map':
            raise NavigationTargetError('catalog_invalid')
        map_path = _absolute_map(catalog['map'], 'catalog_invalid')
        if map_path != active_path:
            raise NavigationTargetError('map_mismatch')
        locations = catalog['locations']
        if not isinstance(locations, dict) or not 1 <= len(locations) <= MAX_LOCATIONS:
            raise NavigationTargetError('catalog_invalid')
        poses = {}
        for name, pose in locations.items():
            name = _label(name, 'catalog_invalid')
            if name in poses:
                raise NavigationTargetError('target_ambiguous')
            if not isinstance(pose, dict) or set(pose) != {'x', 'y', 'yaw'}:
                raise NavigationTargetError('catalog_invalid')
            poses[name] = (
                _coordinate(pose['x'], MAX_ABS_COORDINATE_M),
                _coordinate(pose['y'], MAX_ABS_COORDINATE_M),
                _coordinate(pose['yaw'], math.pi),
            )
        if key not in poses:
            raise NavigationTargetError('target_not_found')

        map_raw = _read(map_path, MAX_MAP_BYTES, 'catalog_unavailable')
        map_data = _document(map_raw)
        image = map_data.get('image')
        if (not isinstance(image, str) or not image or len(image) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in image)):
            raise NavigationTargetError('catalog_invalid')
        try:
            image_path = (map_path.parent / image).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise NavigationTargetError('catalog_unavailable') from error
        image_digest = _digest_image(image_path)
        digest = hashlib.sha256()
        for part in (b'malbut-speech-navigation-v1', raw, str(map_path).encode(),
                     map_raw, str(image_path).encode(), image_digest, key.encode()):
            digest.update(len(part).to_bytes(8, 'big'))
            digest.update(part)
        x, y, yaw = poses[key]
        return NavigationTarget(key, str(map_path), 'map', x, y, yaw, digest.hexdigest())
