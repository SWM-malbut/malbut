"""Persist an AMCL pose only for the exact saved map it belongs to."""

from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import tempfile

import yaml


def map_identity(map_file):
    """Hash map metadata and image so replacing a map invalidates old poses."""
    path = Path(map_file).expanduser().resolve()
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


def valid_pose(data):
    """Reject corrupt/non-finite pose data, without claiming localization quality."""
    if not isinstance(data, dict) or data.get('frame_id') != 'map':
        return False
    try:
        values = [data['x'], data['y'], data['yaw'], *data['covariance']]
        return (
            len(data['covariance']) == 36
            and all(isinstance(x, (float, int)) and not isinstance(x, bool)
                    and math.isfinite(x) for x in values)
            and all(data['covariance'][i] >= 0 for i in range(0, 36, 7))
        )
    except (KeyError, TypeError):
        return False


def read_pose(path, identity):
    """Ignore missing, corrupt, or different-map records."""
    try:
        data = yaml.safe_load(Path(path).expanduser().read_text())
        if valid_pose(data) and data.get('map_id') == identity:
            return data
    except (OSError, ValueError, yaml.YAMLError):
        pass
    return None


def write_pose(path, identity, pose):
    """Atomically replace a small pose record; never leave a truncated YAML."""
    if not valid_pose(pose):
        raise ValueError('Invalid AMCL pose')
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**pose, 'map_id': identity,
            'saved_at': datetime.now(timezone.utc).isoformat()}
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', dir=path.parent, prefix='.last_pose-', delete=False) as stream:
            temp = stream.name
            yaml.safe_dump(data, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)
