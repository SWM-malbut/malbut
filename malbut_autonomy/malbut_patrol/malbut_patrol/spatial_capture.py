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


def camera_calibration(info, rectified):
    """Use raw K/D or rectified P explicitly; nonzero D is not a stream-state flag."""
    if rectified:
        projection = np.asarray(info.p, dtype=float).reshape(3, 4)
        rotation = np.asarray(info.r, dtype=float).reshape(3, 3)
        # A rotated stereo rectification needs a depth/TF conversion too.
        if (not np.allclose(rotation, np.eye(3), atol=1e-9, rtol=0)
                or not np.allclose(projection[:, 3], 0, atol=1e-9, rtol=0)):
            raise ValueError('unsupported rectified depth geometry')
        matrix = projection[:, :3]
        model, coefficients = 'none', []
    else:
        matrix = np.asarray(info.k, dtype=float).reshape(3, 3)
        model = getattr(info, 'distortion_model', '')
        coefficients = list(info.d)
        lengths = {'': (0, 5), 'plumb_bob': (0, 5), 'rational_polynomial': (8,)}
        if (model not in lengths or len(coefficients) not in lengths[model]
                or not all(math.isfinite(v) for v in coefficients)
                or (not model and any(coefficients))):
            raise ValueError('unsupported distortion model')
        if not any(coefficients):
            model, coefficients = 'none', []
    if (not np.isfinite(matrix).all() or min(matrix[0, 0], matrix[1, 1]) <= 0
            or not np.allclose(matrix[2], [0, 0, 1], atol=1e-9, rtol=0)
            or abs(matrix[0, 1]) > 1e-9 or abs(matrix[1, 0]) > 1e-9):
        raise ValueError('invalid camera matrix')
    return ({'fx': float(matrix[0, 0]), 'fy': float(matrix[1, 1]),
             'cx': float(matrix[0, 2]), 'cy': float(matrix[1, 2])},
            {'image_space': 'rectified' if rectified else 'raw',
             'distortion_model': model, 'distortion_coefficients': coefficients})


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
    try:
        intrinsics, calibration = camera_calibration(
            info, bundle.get('image_rectified', False))
    except (AttributeError, TypeError, ValueError):
        return {'status': 'calibration_unsupported'}, None
    registered_frame = bundle.get('depth_registered_frame') or depth.header.frame_id
    if (info.header.frame_id != image.header.frame_id
            or registered_frame != image.header.frame_id):
        return {'status': 'registration_frame_mismatch'}, None
    dtype = {'32FC1': 'f4', '16UC1': 'u2', 'mono16': 'u2'}.get(depth.encoding)
    if dtype is None:
        return {'status': 'depth_encoding_unsupported'}, None
    # mono16 does not declare a depth unit. Only use a verified deployment scale.
    scale = ({'32FC1': 1.0, '16UC1': .001}.get(depth.encoding)
             if depth.encoding != 'mono16' else bundle.get('depth_scale_m', 0.0))
    if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
        return {'status': 'depth_unit_unknown'}, None
    dtype = np.dtype(('>' if depth.is_bigendian else '<') + dtype)
    width, height, step = depth.width, depth.height, depth.step
    if (not 0 < width * height <= 2000000 or step < width * dtype.itemsize
            or step % dtype.itemsize or len(depth.data) != height * step):
        return {'status': 'depth_invalid'}, None
    values = np.frombuffer(bytes(depth.data), dtype=dtype).reshape(height, step // dtype.itemsize)
    values = values[:, :width].astype(np.float32)
    values *= scale
    output = io.BytesIO()
    np.save(output, values, allow_pickle=False)
    p, q = transform.translation, transform.rotation
    spatial = {
        'status': 'available', 'depth_shape': [height, width],
        'depth_stamp': {'sec': depth.header.stamp.sec, 'nanosec': depth.header.stamp.nanosec},
        'intrinsics': intrinsics, 'calibration': calibration,
        'depth_encoding': depth.encoding, 'depth_scale_m': scale,
        'depth_source_frame': depth.header.frame_id,
        'depth_registered_frame': registered_frame,
        'map_from_camera': {'translation': [p.x, p.y, p.z],
                            'rotation_xyzw': [q.x, q.y, q.z, q.w]},
        'optical_frame': bundle['optical_frame'],
    }
    return spatial, output.getvalue()


def encode_lidar(image, bundle):
    """Store optional scan evidence only with known timing and negligible sensor motion."""
    if bundle is None:
        return None, None
    scan = bundle.get('scan')
    start, end = bundle.get('transform'), bundle.get('end_transform')
    if scan is None or start is None or end is None:
        return {'status': 'lidar_transform_missing'}, None
    count = len(scan.ranges)
    parameters = (scan.angle_min, scan.angle_increment, scan.range_min,
                  scan.range_max, scan.time_increment)
    if (not 1 <= count <= 4096 or not all(math.isfinite(v) for v in parameters)
            or not 0 < scan.angle_increment <= math.pi
            or not 0 <= scan.range_min < scan.range_max <= 1000
            or not 0 <= scan.time_increment*(count-1) <= .25):
        return {'status': 'lidar_invalid'}, None
    a, b = scan.header.stamp, image.header.stamp
    offset = a.sec-b.sec + (a.nanosec-b.nanosec)/1e9
    times = offset + np.arange(count)*scan.time_increment
    if np.min(np.abs(times)) > .05:
        return {'status': 'lidar_unsynchronized'}, None
    p, q = start.translation, start.rotation
    ep, eq = end.translation, end.rotation
    translation, quaternion = [p.x, p.y, p.z], [q.x, q.y, q.z, q.w]
    other_position, other_quaternion = [ep.x, ep.y, ep.z], [eq.x, eq.y, eq.z, eq.w]
    if (not all(math.isfinite(v) for v in translation+quaternion+other_position+other_quaternion)
            or abs(np.linalg.norm(quaternion)-1) > .001
            or abs(np.linalg.norm(other_quaternion)-1) > .001):
        return {'status': 'lidar_transform_invalid'}, None
    motion = np.linalg.norm(np.asarray(translation)-other_position)
    rotation = 2*math.acos(min(1., abs(float(np.dot(quaternion, other_quaternion)))))
    if motion > .02 or rotation > .02:
        return {'status': 'lidar_sensor_moving'}, None
    stream = io.BytesIO()
    np.save(stream, np.asarray(scan.ranges, dtype=np.float32), allow_pickle=False)
    return {'status': 'available', 'count': count,
            'stamp': {'sec': scan.header.stamp.sec, 'nanosec': scan.header.stamp.nanosec},
            'frame': scan.header.frame_id, 'angle_min': float(scan.angle_min),
            'angle_increment': float(scan.angle_increment),
            'time_increment': float(scan.time_increment),
            'range_min': float(scan.range_min), 'range_max': float(scan.range_max),
            'map_from_lidar': {'translation': translation,
                               'rotation_xyzw': quaternion}}, stream.getvalue()
