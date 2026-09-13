"""Save the SLAM exit pose as an initial estimate tied to exact map contents."""

from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import tempfile

import yaml


def write_mapping_pose(map_yaml, x, y, yaw):
    """Atomically write a map-adjacent pose record compatible with pose memory."""
    if not all(math.isfinite(value) for value in (x, y, yaw)):
        raise ValueError('SLAM pose must be finite')
    path = Path(map_yaml).expanduser().resolve()
    content = path.read_bytes()
    try:
        metadata = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ValueError('Invalid saved map YAML') from error
    if not isinstance(metadata, dict) or not isinstance(metadata.get('image'), str):
        raise ValueError('Saved map YAML has no image path')
    image = Path(metadata['image']).expanduser()
    if not image.is_absolute():
        image = path.parent / image
    digest = hashlib.sha256(content)
    with image.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    # TF has no covariance. These are conventional AMCL initial-estimate
    # uncertainties (0.5 m, 15 degrees), not measured SLAM sensor accuracy.
    covariance = [0.0] * 36
    covariance[0] = covariance[7] = 0.25
    covariance[35] = (math.pi / 12.0) ** 2
    data = {
        'map_id': digest.hexdigest(), 'frame_id': 'map',
        'x': float(x), 'y': float(y), 'yaw': float(yaw),
        'covariance': covariance,
        'saved_at': datetime.now(timezone.utc).isoformat(),
    }
    destination = path.with_suffix('.pose.yaml')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', dir=path.parent, prefix='.mapping-pose-', delete=False) as stream:
            temporary = stream.name
            yaml.safe_dump(data, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    return destination
