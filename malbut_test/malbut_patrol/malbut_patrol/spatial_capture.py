"""Persist registered RGB-D geometry without doing inference on the motion thread."""

import io
import json
import math

import numpy as np


def room_snapshot(rooms):
    """Freeze map-frame polygons and the same display names used by coverage."""
    result, names = [], set()
    features = rooms.get('features', []) if isinstance(rooms, dict) else (rooms or [])
    for index, feature in enumerate(features):
        props, geometry = feature.get('properties', {}), feature.get('geometry') or {}
        if props.get('role', 'room') != 'room' or geometry.get('type') not in (
                'Polygon', 'MultiPolygon'):
            continue
        identity = str(props.get('room_id') or feature.get('id') or f'room-{index + 1}')
        name = str(props.get('name') or props.get('room_id')
                   or feature.get('id') or f'room-{index + 1}')
        if name in names:
            suffix = props.get('room_id') or feature.get('id') or index + 1
            name = f'{name} ({suffix})'
        names.add(name)
        polygons = geometry['coordinates']
        if geometry['type'] == 'Polygon':
            polygons = [polygons]
        clean = []
        for polygon in polygons:
            rings = []
            for ring in polygon:
                points = np.asarray(ring, dtype=float)
                if (points.ndim != 2 or points.shape[0] < 3 or points.shape[1] < 2
                        or not np.isfinite(points).all()):
                    raise ValueError('invalid room polygon')
                rings.append(points[:, :2].tolist())
            if rings:
                clean.append(rings)
        if clean:
            result.append({'id': identity[:120], 'name': name[:120], 'polygons': clean})
    data = json.dumps({'version': 1, 'regions': result}, ensure_ascii=False).encode()
    if len(data) > 1024 * 1024 or len(result) > 256:
        raise ValueError('room snapshot too large')
    return data


def stamp_seconds(stamp):
    """Compare RGB and depth timestamps in their shared ROS clock."""
    return stamp.sec + stamp.nanosec / 1e9


def encode_spatial(image, bundle):
    """Fail closed unless depth is registered to the RGB optical camera."""
    if not bundle or not bundle.get('aligned'):
        return {'status': 'depth_not_registered'}, None
    depth, info, transform = bundle.get('depth'), bundle['info'], bundle['transform']
    if depth is None:
        return {'status': 'depth_missing'}, None
    if abs(stamp_seconds(depth.header.stamp) - stamp_seconds(image.header.stamp)) > 0.05:
        return {'status': 'depth_unsynchronized'}, None
    if ((depth.width, depth.height) != (image.width, image.height)
            or (info.width, info.height) != (image.width, image.height)):
        return {'status': 'calibration_size_mismatch'}, None
    # Unrectified/distorted imagery needs calibrated registration before use.
    k = list(info.k)
    if (not all(math.isfinite(v) for v in k) or k[0] <= 0 or k[4] <= 0
            or any(not math.isfinite(v) or abs(v) > 1e-9 for v in info.d)
            or abs(k[1]) > 1e-9):
        return {'status': 'calibration_unsupported'}, None
    if (info.header.frame_id != image.header.frame_id
            or depth.header.frame_id != image.header.frame_id):
        return {'status': 'registration_frame_mismatch'}, None
    dtype = {'32FC1': 'f4', '16UC1': 'u2'}.get(depth.encoding)
    if dtype is None:
        return {'status': 'depth_encoding_unsupported'}, None
    dtype = np.dtype(('>' if depth.is_bigendian else '<') + dtype)
    width, height, step = depth.width, depth.height, depth.step
    if (not 0 < width * height <= 2000000 or step < width * dtype.itemsize
            or step % dtype.itemsize or len(depth.data) != height * step):
        return {'status': 'depth_invalid'}, None
    values = np.frombuffer(bytes(depth.data), dtype=dtype).reshape(height, step // dtype.itemsize)
    values = values[:, :width].astype(np.float32)
    if depth.encoding == '16UC1':
        values *= 0.001
    output = io.BytesIO()
    np.save(output, values, allow_pickle=False)
    p, q = transform.translation, transform.rotation
    spatial = {
        'status': 'available', 'depth_shape': [height, width],
        'depth_stamp': {'sec': depth.header.stamp.sec, 'nanosec': depth.header.stamp.nanosec},
        'intrinsics': {'fx': k[0], 'fy': k[4], 'cx': k[2], 'cy': k[5]},
        'map_from_camera': {'translation': [p.x, p.y, p.z],
                            'rotation_xyzw': [q.x, q.y, q.z, q.w]},
        'optical_frame': bundle['optical_frame'],
    }
    return spatial, output.getvalue()
