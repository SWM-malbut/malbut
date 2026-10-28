"""Ground photo objects in immutable, registered depth and map-region evidence."""

import copy
import hashlib
import io
import json
import math
import re

import numpy as np


def _sidecar(path, suffix, digest, limit):
    evidence = path.with_name(path.stem + suffix)
    if (not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
            or evidence.is_symlink() or not 0 < evidence.stat().st_size <= limit):
        raise ValueError('invalid spatial evidence')
    contents = evidence.read_bytes()
    if hashlib.sha256(contents).hexdigest() != digest:
        raise ValueError('spatial evidence changed')
    return contents


def read_spatial(path, metadata):
    """Revalidate all sidecars on ingestion, inference and retrieval; v1 stays readable."""
    if metadata['version'] == 1:
        return [], None
    snapshot = json.loads(_sidecar(
        path, '.regions.json', metadata.get('regions_sha256'), 1024 * 1024))
    if snapshot.get('version') != 1 or not isinstance(snapshot.get('regions'), list):
        raise ValueError('invalid room snapshot')
    regions = snapshot['regions']
    if len(regions) > 256:
        raise ValueError('too many regions')
    for region in regions:
        if (set(region) != {'id', 'name', 'polygons'} or not region['polygons']
                or any(not isinstance(region[k], str) or not 1 <= len(region[k]) <= 120
                       for k in ('id', 'name'))):
            raise ValueError('invalid region')
        for polygon in region['polygons']:
            if not polygon:
                raise ValueError('empty polygon')
            for ring in polygon:
                points = np.asarray(ring, dtype=float)
                if (points.ndim != 2 or points.shape[0] < 3 or points.shape[1] != 2
                        or not np.isfinite(points).all()):
                    raise ValueError('invalid region ring')
    captures = metadata.get('capture_regions')
    expected = [{'id': r['id'], 'name': r['name']} for r in regions
                if r['name'] in metadata['capture_rooms']]
    if captures != expected:
        raise ValueError('capture regions mismatch')
    spatial = metadata.get('spatial')
    if not isinstance(spatial, dict) or not isinstance(spatial.get('status'), str):
        raise ValueError('invalid spatial status')
    if spatial['status'] != 'available':
        return regions, None
    shape = spatial.get('depth_shape')
    if (not isinstance(shape, list) or len(shape) != 2
            or any(type(v) is not int or v <= 0 for v in shape)
            or math.prod(shape) > 2000000):
        raise ValueError('invalid depth shape')
    k = spatial['intrinsics']
    if (set(k) != {'fx', 'fy', 'cx', 'cy'}
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in k.values())
            or min(k['fx'], k['fy']) <= 0):
        raise ValueError('invalid camera calibration')
    transform = spatial['map_from_camera']
    translation, quaternion = transform['translation'], transform['rotation_xyzw']
    if (len(translation) != 3 or len(quaternion) != 4
            or not all(type(v) in (float, int) and math.isfinite(v)
                       for v in translation + quaternion)
            or abs(sum(v * v for v in quaternion) - 1) > 0.001):
        raise ValueError('invalid camera transform')
    for stamp in (metadata['image_stamp'], spatial['depth_stamp']):
        if (set(stamp) != {'sec', 'nanosec'} or type(stamp['sec']) is not int
                or type(stamp['nanosec']) is not int or not 0 <= stamp['nanosec'] < 10**9):
            raise ValueError('invalid sensor stamp')
    rgb, depth = metadata['image_stamp'], spatial['depth_stamp']
    if abs(rgb['sec'] - depth['sec'] + (rgb['nanosec'] - depth['nanosec']) / 1e9) > 0.05:
        raise ValueError('unsynchronized depth')
    stream = io.BytesIO(_sidecar(path, '.depth.npy', spatial['depth_sha256'], 8001024))
    if np.lib.format.read_magic(stream) != (1, 0):
        raise ValueError('invalid depth format')
    stored_shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
    if list(stored_shape) != shape or fortran or dtype != np.dtype('float32'):
        raise ValueError('invalid depth array')
    if len(stream.getbuffer()) - stream.tell() != math.prod(shape) * 4:
        raise ValueError('invalid depth size')
    return regions, np.frombuffer(stream.read(), dtype=np.float32).reshape(shape)


def _inside(point, ring):
    x, y = point
    inside = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if (a[1] > y) != (b[1] > y):
            if x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
                inside = not inside
    return inside


def _distance(point, ring):
    p = np.asarray(point)
    a = np.asarray(ring)
    delta = np.roll(a, -1, axis=0) - a
    lengths = np.sum(delta * delta, axis=1)
    t = np.clip(np.sum((p - a) * delta, axis=1) / np.maximum(lengths, 1e-20), 0, 1)
    return float(np.min(np.linalg.norm(p - a - t[:, None] * delta, axis=1)))


def region_at(point, regions, margin):
    """Require a unique region, with clearance from every outer/hole boundary."""
    hits = []
    near_boundary = False
    for region in regions:
        for polygon in region['polygons']:
            near_boundary |= any(_distance(point, ring) <= margin for ring in polygon)
            if _inside(point, polygon[0]) and not any(_inside(point, h) for h in polygon[1:]):
                hits.append(region)
                break
    if near_boundary:
        return None, 'region_boundary'
    if len(hits) != 1:
        return None, 'overlapping_regions' if hits else 'outside_named_regions'
    return {k: hits[0][k] for k in ('id', 'name')}, None


def _object_position(item, metadata, regions, depth):
    unknown = {'status': 'unknown', 'region': None, 'map_position': None,
               'method': 'registered_rgbd', 'reason': None}
    spatial = metadata.get('spatial', {'status': 'legacy_without_depth'})
    bbox = item.get('image_bbox')
    if depth is None or spatial['status'] != 'available':
        return dict(unknown, reason=spatial['status'])
    if bbox is None or item['confidence'] != 'high':
        return dict(unknown, reason='object_not_grounded')
    height, width = depth.shape
    left, top, right, bottom = np.asarray(bbox) / 1000 * [width, height, width, height]
    # Avoid bounding-box background; use the central 40% of visible object pixels.
    x0, x1 = max(0, int(left + (right-left)*.3)), min(width, int(right-(right-left)*.3)+1)
    y0, y1 = max(0, int(top + (bottom-top)*.3)), min(height, int(bottom-(bottom-top)*.3)+1)
    roi = depth[y0:y1, x0:x1]
    valid = np.isfinite(roi) & (roi >= .15) & (roi <= 10)
    if np.count_nonzero(valid) < 16 or np.mean(valid) < .7:
        return dict(unknown, reason='insufficient_depth')
    z = float(np.median(roi[valid]))
    spread = float(np.quantile(np.abs(roi[valid] - z), .8))
    if spread > max(.08, z * .04):
        return dict(unknown, reason='mixed_depth')
    yy, xx = np.nonzero(valid & (np.abs(roi - z) <= max(.025, spread * 2)))
    k = spatial['intrinsics']
    camera = np.array([(float(np.median(xx)) + x0 - k['cx']) * z / k['fx'],
                       (float(np.median(yy)) + y0 - k['cy']) * z / k['fy'], z])
    transform = spatial['map_from_camera']
    qx, qy, qz, qw = transform['rotation_xyzw']
    q = np.array([qx, qy, qz])
    world = camera + 2 * np.cross(q, np.cross(q, camera) + qw * camera)
    world += transform['translation']
    # Include depth noise, calibration/localization allowance and visible ROI extent.
    margin = .12 + .02 * z + spread + z * max(x1-x0, y1-y0) / min(k['fx'], k['fy']) / 2
    region, reason = region_at(world[:2], regions, margin)
    return {'status': 'located' if region else 'unknown', 'region': region,
            'map_position': dict(zip(('x', 'y', 'z'), map(lambda v: round(float(v), 3), world))),
            'method': 'registered_rgbd', 'reason': reason,
            'depth_m': round(z, 3), 'boundary_margin_m': round(margin, 3)}


def localize_objects(analysis, metadata, regions, depth):
    """Only trusted geometry assigns object regions; never copy the capture room."""
    result = copy.deepcopy(analysis)
    for item in result['objects']:
        item['spatial'] = _object_position(item, metadata, regions, depth)
    return result
