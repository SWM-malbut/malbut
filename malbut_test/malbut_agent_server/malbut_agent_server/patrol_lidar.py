"""Compare synchronized LiDAR returns with visible, connected body surfaces."""

import io
import math
from collections import Counter

import cv2
import numpy as np

from malbut_agent_server.patrol_spatial import _distortion, _map_points, _rays, _sidecar


def _seconds(stamp):
    if (set(stamp) != {'sec', 'nanosec'} or type(stamp['sec']) is not int
            or type(stamp['nanosec']) is not int or not 0 <= stamp['nanosec'] < 10**9):
        raise ValueError('invalid lidar stamp')
    return stamp['sec'] + stamp['nanosec'] / 1e9


def read_lidar(path, metadata):
    """Optional immutable evidence: missing LiDAR never prevents RGB-D observations."""
    header = metadata.get('lidar')
    if header is None:
        return None
    if not isinstance(header, dict) or not isinstance(header.get('status'), str):
        raise ValueError('invalid lidar evidence')
    if header['status'] != 'available':
        return header
    count = header.get('count')
    if type(count) is not int or not 1 <= count <= 4096:
        raise ValueError('invalid lidar size')
    _seconds(header['stamp'])
    for name in ('angle_min', 'angle_increment', 'range_min', 'range_max', 'time_increment'):
        if type(header.get(name)) not in (int, float) or not math.isfinite(header[name]):
            raise ValueError('invalid lidar geometry')
    if (not 0 < header['angle_increment'] <= math.pi
            or not 0 <= header['range_min'] < header['range_max'] <= 1000
            or not 0 <= header['time_increment'] * (count-1) <= .25):
        raise ValueError('invalid lidar bounds')
    tf = header['map_from_lidar']
    if (set(tf) != {'translation', 'rotation_xyzw'} or len(tf['translation']) != 3
            or len(tf['rotation_xyzw']) != 4
            or not all(type(v) in (int, float) and math.isfinite(v)
                       for v in tf['translation'] + tf['rotation_xyzw'])
            or abs(sum(v*v for v in tf['rotation_xyzw'])-1) > .001):
        raise ValueError('invalid lidar transform')
    stream = io.BytesIO(_sidecar(path, '.lidar.npy', header.get('sha256'), 20000))
    if np.lib.format.read_magic(stream) != (1, 0):
        raise ValueError('invalid lidar array')
    shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
    if (shape != (count,) or fortran or dtype != np.dtype('float32')
            or len(stream.getbuffer()) - stream.tell() != count*4):
        raise ValueError('invalid lidar array')
    return dict(header, ranges=np.frombuffer(stream.read(), dtype=np.float32))


def _body_mask(item, depth):
    """Grow only continuous depth surfaces from observed body anchors inside the box."""
    height, width = depth.shape
    left, top, right, bottom = (np.asarray(item['image_bbox']) / 1000
                                * [width, height, width, height])
    yy, xx = np.indices(depth.shape)
    valid = (np.isfinite(depth) & (depth >= .15) & (depth <= 10)
             & (xx > left) & (xx < right) & (yy > top) & (yy < bottom))
    image = np.where(valid, depth, 1000).astype(np.float32)
    mask = np.ones((height+2, width+2), dtype=np.uint8)
    mask[1:-1, 1:-1] = (~valid).astype(np.uint8)
    votes, used = Counter(), []
    for index, (x, y) in enumerate(item.get('image_anchors', [])):
        px, py = int(x/1000*width), int(y/1000*height)
        if not (0 <= px < width and 0 <= py < height and valid[py, px]):
            continue
        if any(math.hypot(px-a, py-b) < 5 for a, b in used):
            continue
        used.append((px, py))
        tolerance = .025 + .01*float(depth[py, px])
        cv2.floodFill(image, mask, (px, py), 0, tolerance, tolerance,
                      4 | cv2.FLOODFILL_MASK_ONLY | ((index+2) << 8))
        votes[int(mask[py+1, px+1])] += 1
    supported = [label for label, count in votes.items() if label >= 2 and count >= 2]
    return np.isin(mask[1:-1, 1:-1], supported)


def compare_person_lidar(item, metadata, depth, lidar, other_boxes=()):
    """Never fuse unmatched distances or use LiDAR to override an unknown room."""
    result = {'status': 'unmatched', 'method': 'projected_body_surface'}
    spatial = metadata.get('spatial', {})
    if item.get('confidence') != 'high' or len(item.get('image_anchors', [])) < 2:
        return dict(result, reason='body_anchors_unreliable')
    if lidar.get('status') != 'available' or depth is None or not item.get('image_bbox'):
        return dict(result, status='unavailable', reason=lidar.get('status', 'depth_missing'))
    if spatial.get('status') != 'available':
        return dict(result, status='unavailable', reason='depth_missing')
    ranges = lidar['ranges']
    _seconds(lidar['stamp'])
    _seconds(metadata['image_stamp'])
    a, b = lidar['stamp'], metadata['image_stamp']
    offset = a['sec']-b['sec'] + (a['nanosec']-b['nanosec'])/1e9
    times = offset + np.arange(len(ranges))*lidar['time_increment']
    valid = (np.isfinite(ranges) & (ranges >= lidar['range_min'])
             & (ranges <= lidar['range_max']) & (np.abs(times) <= .05))
    ids = np.flatnonzero(valid)
    if not len(ids):
        return dict(result, reason='no_synchronized_returns')
    angles = lidar['angle_min'] + ids*lidar['angle_increment']
    laser = np.column_stack((ranges[ids]*np.cos(angles), ranges[ids]*np.sin(angles),
                             np.zeros(len(ids))))
    world = _map_points(laser, {'map_from_camera': lidar['map_from_lidar']})
    tf = spatial['map_from_camera']
    inverse = [-v for v in tf['rotation_xyzw'][:3]] + tf['rotation_xyzw'][3:]
    camera = _map_points(world-np.asarray(tf['translation']), {
        'map_from_camera': {'translation': [0, 0, 0], 'rotation_xyzw': inverse}})
    k = spatial['intrinsics']
    matrix = np.array([[k['fx'], 0, k['cx']], [0, k['fy'], k['cy']], [0, 0, 1.]])
    coefficients = _distortion(spatial)
    pixels = cv2.projectPoints(
        camera, np.zeros(3), np.zeros(3), matrix,
        np.asarray(coefficients) if coefficients else None)[0].reshape(-1, 2)
    mask = _body_mask(item, depth)
    height, width = depth.shape
    errors, offsets = [], []
    candidates = occluded = disagree = edges = ambiguous = 0
    origin = np.asarray(lidar['map_from_lidar']['translation'])
    for j, (u, v) in enumerate(pixels):
        if camera[j, 2] <= 0 or not np.isfinite([u, v]).all():
            continue
        px, py = int(round(u)), int(round(v))
        if not (2 <= px < width-2 and 2 <= py < height-2 and mask[py, px]):
            continue
        candidates += 1
        if any(left < u/width*1000 < right and top < v/height*1000 < bottom
               for left, top, right, bottom in other_boxes):
            ambiguous += 1
            continue
        roi = depth[py-2:py+3, px-2:px+3]
        good = mask[py-2:py+3, px-2:px+3] & np.isfinite(roi)
        if np.count_nonzero(good) < 20 or not np.isfinite(depth[py, px]):
            edges += 1
            continue
        z = float(np.median(roi[good]))
        spread = float(np.quantile(np.abs(roi[good]-z), .95))
        tolerance = .04 + .01*z
        if spread > tolerance or abs(float(depth[py, px])-z) > tolerance:
            edges += 1
            continue
        if z < camera[j, 2]-tolerance:
            occluded += 1
            continue
        if z > camera[j, 2]+tolerance:
            disagree += 1
            continue
        # Use the very same subpixel ray, not the centre of the person or another leg.
        ray = _rays([[u, v]], spatial)[0]
        point = _map_points(np.r_[ray*z, z], spatial)
        errors.append(abs(float(np.linalg.norm(point-origin))-float(ranges[ids[j]])))
        offsets.append(abs(float(times[ids[j]])))
    result.update(candidate_count=candidates, matched_count=len(errors),
                  occluded_count=occluded, disagreement_count=disagree,
                  edge_count=edges, ambiguous_count=ambiguous)
    if len(errors) >= 2:
        result.update(status='matched', median_abs_error_m=round(float(np.median(errors)), 4),
                      p95_abs_error_m=round(float(np.quantile(errors, .95)), 4),
                      max_time_offset_s=round(max(offsets), 4))
    else:
        result['reason'] = 'insufficient_same_surface_support'
    return result
