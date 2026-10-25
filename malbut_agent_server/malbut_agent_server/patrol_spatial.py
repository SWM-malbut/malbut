"""Ground photo objects in immutable, registered depth and map-region evidence."""

import copy
import hashlib
import io
from itertools import combinations
import json
import math
import re

import cv2
import numpy as np


class RegionSnapshot(list):
    """Capture rooms and object envelopes have separate, immutable evidence roles."""

    def __init__(self, regions, object_regions=None, policy=None):
        super().__init__(regions)
        self.object_regions = regions if object_regions is None else object_regions
        self.object_region_policy = policy


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
    if snapshot.get('version') not in (1, 2) or not isinstance(snapshot.get('regions'), list):
        raise ValueError('invalid room snapshot')
    regions = snapshot['regions']
    if len(regions) > 256:
        raise ValueError('too many regions')
    objects = snapshot.get('object_regions', regions)
    policy = snapshot.get('object_region_policy')
    if snapshot['version'] == 2:
        if (set(snapshot) != {'version', 'regions', 'object_regions', 'object_region_policy'}
                or not isinstance(objects, list) or len(objects) != len(regions)
                or not isinstance(policy, dict) or set(policy) != {
                    'method', 'extension_m', 'resolution_m'}
                or policy['method'] not in ('bounded_nonfree_room_envelope', 'floor_polygons_only')
                or policy['extension_m'] != .45
                or type(policy['resolution_m']) not in (int, float)
                or not 0 < policy['resolution_m'] <= 100
                or [(r.get('id'), r.get('name')) for r in regions]
                != [(r.get('id'), r.get('name')) for r in objects]):
            raise ValueError('invalid object room snapshot')
    elif objects != regions or policy is not None:
        raise ValueError('unexpected object room snapshot')
    for region in regions + objects:
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
    regions = RegionSnapshot(regions, objects, policy)
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
    _distortion(spatial)
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


def _distortion(spatial):
    """Legacy captures are pinhole; new evidence declares raw or rectified pixels."""
    calibration = spatial.get('calibration')
    if calibration is None:
        return []
    if (not isinstance(calibration, dict) or set(calibration) != {
            'image_space', 'distortion_model', 'distortion_coefficients'}
            or calibration['image_space'] not in ('raw', 'rectified')):
        raise ValueError('invalid pixel calibration')
    model = calibration['distortion_model']
    coefficients = calibration['distortion_coefficients']
    lengths = {'none': 0, 'plumb_bob': 5, 'rational_polynomial': 8}
    if (not isinstance(model, str) or model not in lengths or not isinstance(coefficients, list)
            or len(coefficients) != lengths[model]
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in coefficients)
            or (calibration['image_space'] == 'rectified' and model != 'none')):
        raise ValueError('invalid distortion calibration')
    return coefficients


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


def _rays(pixels, spatial):
    pixels = np.asarray(pixels, dtype=np.float64).reshape(-1, 2)
    k = spatial['intrinsics']
    coefficients = _distortion(spatial)
    if coefficients:
        matrix = np.array([[k['fx'], 0, k['cx']], [0, k['fy'], k['cy']], [0, 0, 1]])
        return cv2.undistortPoints(
            pixels.reshape(-1, 1, 2), matrix,
            np.asarray(coefficients, dtype=np.float64)).reshape(-1, 2)
    return (pixels - [k['cx'], k['cy']]) / [k['fx'], k['fy']]


def _map_points(camera, spatial):
    transform = spatial['map_from_camera']
    qx, qy, qz, qw = transform['rotation_xyzw']
    q = np.array([qx, qy, qz])
    return (camera + 2 * np.cross(q, np.cross(q, camera) + qw * camera)
            + transform['translation'])


def _planar_support(roi, valid, x0, y0, spatial):
    """Accept a sloped surface only when almost all depths agree with one 3D plane."""
    yy, xx = np.nonzero(valid)
    stride = max(1, math.ceil(len(xx) / 4096))
    xx, yy = xx[::stride], yy[::stride]
    rays = _rays(np.column_stack((xx + x0, yy + y0)), spatial)
    matrix = np.column_stack((rays, np.ones(len(rays))))
    if not np.isfinite(matrix).all():
        return None
    values = roi[yy, xx]
    # On a plane, 1/Z is affine in the undistorted optical ray (x/z, y/z).
    plane, _, rank, _ = np.linalg.lstsq(matrix, 1 / values, rcond=None)
    inverse = matrix @ plane
    if rank != 3 or not np.isfinite(inverse).all() or np.any(inverse <= 0):
        return None
    error = float(np.quantile(np.abs(values - 1 / inverse), .95))
    if error > max(.025, float(np.median(values)) * .015):
        return None
    height, width = roi.shape
    # Keep the actual measurement local; never replace missing center depth by the fit.
    cx, cy = width // 2, height // 2
    bounds = max(0, cx-3), min(width, cx+4), max(0, cy-3), min(height, cy+4)
    sx, sy = np.meshgrid(np.linspace(x0, x0+width-1, 3),
                         np.linspace(y0, y0+height-1, 3))
    support_rays = _rays(np.column_stack((sx.ravel(), sy.ravel())), spatial)
    support_inverse = np.column_stack((support_rays, np.ones(9))) @ plane
    if not np.isfinite(support_inverse).all() or np.any(support_inverse <= 0):
        return None
    support = np.column_stack((support_rays, np.ones(9))) / support_inverse[:, None]
    return bounds, error, _map_points(support, spatial), plane


def _support_clear(points, regions, margin):
    """Check the measured footprint itself, including holes, instead of its enclosing disc."""
    points = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    hull = cv2.convexHull(points).reshape(-1, 2)
    for region in regions:
        for polygon in region['polygons']:
            for ring in polygon:
                boundary = np.asarray(ring, dtype=np.float32)
                if len(hull) >= 3 and any(
                        cv2.pointPolygonTest(hull, tuple(map(float, p)), False) >= 0
                        for p in boundary):
                    return False
                # Sample a continuous segment conservatively: the extra half-step
                # bounds the distance of any unsampled point to the boundary.
                for a, b in zip(hull, np.roll(hull, -1, axis=0)):
                    count = max(1, math.ceil(float(np.linalg.norm(b-a)) / .02))
                    allowance = margin + float(np.linalg.norm(b-a)) / count / 2
                    if any(_distance(p, ring) <= allowance
                           for p in np.linspace(a, b, count+1)):
                        return False
    return True


def _surface_link(a, b, depth):
    """Two object anchors must have measured, continuous surface between them.

    Smooth nonplanar surfaces are allowed. A wall behind the object cannot join
    the object's consensus through a depth jump.
    Missing pixels are never replaced by the expected/interpolated values.
    """
    count = max(5, int(math.ceil(math.dist(a, b)))+1)
    pixels = np.rint(np.linspace(a, b, count)).astype(int)
    values = depth[pixels[:, 1], pixels[:, 0]]
    valid = np.isfinite(values) & (values >= .15) & (values <= 10)
    if np.mean(valid) < .8 or not valid[0] or not valid[-1]:
        return False
    indices = np.flatnonzero(valid)
    # Follow measured local continuity, including folds and curved surfaces.
    # A global plane fit wrongly rejects a continuous blanket or chair back.
    # Do not bridge a missing strip or interpolate across an occlusion.
    if np.max(np.diff(indices), initial=0) > 2:
        return False
    measured = values[indices]
    jumps = np.abs(np.diff(measured))
    tolerance = .025 + .01*np.minimum(measured[:-1], measured[1:])
    return bool(np.all(jumps <= tolerance))


def _connected_group(links):
    """Require one measured surface component, not a straight line for every pair."""
    reached = {0}
    while True:
        others = {i for j in reached for i in np.flatnonzero(links[j])}
        if others <= reached:
            return len(reached) == len(links)
        reached.update(others)


def _anchor_position(item, spatial, regions, depth, unknown, *, person=True):
    """Require independent visible surface patches, never bounding-box background."""
    prefix = 'person' if person else 'object'
    anchors = item.get('image_anchors', [])
    if len(anchors) < 2:
        return dict(unknown, reason=prefix+'_anchors_missing')
    height, width = depth.shape
    left, top, right, bottom = item['image_bbox']
    if any(not (left < x < right and top < y < bottom) for x, y in anchors):
        return dict(unknown, reason=prefix+'_anchor_outside_bbox')
    # Small patches stay inside narrow limbs. Each needs real, coherent depth support.
    radius = 2
    samples, used, pixels = [], [], []
    for x, y in anchors:
        px, py = int(x/1000*width), int(y/1000*height)
        if any(math.hypot(px-a, py-b) < 2*radius+1 for a, b in used):
            continue
        used.append((px, py))
        x0, x1 = max(0, px-radius), min(width, px+radius+1)
        y0, y1 = max(0, py-radius), min(height, py+radius+1)
        roi = depth[y0:y1, x0:x1]
        valid = np.isfinite(roi) & (roi >= .15) & (roi <= 10)
        if np.count_nonzero(valid) < 16 or np.mean(valid) < .8:
            continue
        z = float(np.median(roi[valid]))
        spread = float(np.quantile(np.abs(roi[valid]-z), .95))
        if spread > max(.06, z*.03):
            continue
        # The selected pixel itself must agree; do not fill a hole or a depth edge.
        center = float(depth[py, px])
        if not math.isfinite(center) or abs(center-z) > max(.04, z*.02):
            continue
        ray = _rays([[px, py]], spatial)[0]
        if not np.isfinite(ray).all():
            continue
        world = _map_points(np.r_[ray*z, z], spatial)
        focal = min(spatial['intrinsics']['fx'], spatial['intrinsics']['fy'])
        margin = .12 + .02*z + spread + z*radius/focal
        region, reason = region_at(world[:2], regions, margin)
        samples.append((world, z, margin, region, reason))
        pixels.append((px, py))
    if len(samples) < 2:
        return dict(unknown, reason=prefix+'_depth_insufficient')
    points = np.asarray([s[0] for s in samples])
    distances = np.linalg.norm(points[:, None, :2]-points[None, :, :2], axis=2)
    connected = np.ones(distances.shape, dtype=bool)
    if not person:
        for a, b in combinations(range(len(samples)), 2):
            connected[a, b] = connected[b, a] = _surface_link(pixels[a], pixels[b], depth)
    groups = []
    for size in range(len(samples), 1, -1):
        groups = [ids for ids in combinations(range(len(samples)), size)
                  if np.max(distances[np.ix_(ids, ids)]) <= (.8 if person else 3.)
                  and _connected_group(connected[np.ix_(ids, ids)])]
        if groups:
            break
    # A unique pair may reject a farther background point, never a nearer occluder.
    # Ambiguous groups cannot be averaged into an invented position.
    if len(groups) != 1:
        return dict(unknown, reason=prefix+'_depth_disagreement')
    selected = groups[0]
    farthest = max(samples[i][1] for i in selected)
    separation = .15 if person else max(.04, farthest*.02)
    if any(sample[1] <= farthest+separation
           for i, sample in enumerate(samples) if i not in selected):
        return dict(unknown, reason=prefix+'_depth_disagreement')
    samples = [samples[i] for i in selected]
    points = points[list(selected)]
    region = samples[0][3]
    if region is None or any(s[3] != region for s in samples):
        return dict(unknown, reason=prefix+'_region_disagreement')
    world = np.median(points, axis=0)
    margin = max(s[2] for s in samples)
    if (region_at(world[:2], regions, margin)[0] != region
            or not _support_clear(points[:, :2], regions, margin)):
        return dict(unknown, reason='region_boundary')
    if person and _person_crosses_region(item, spatial, depth, regions, region, world):
        return dict(unknown, reason='visible_body_crosses_region')
    return {'status': 'located', 'region': region,
            'map_position': dict(zip(('x', 'y', 'z'), np.round(world, 3).tolist())),
            'method': 'registered_rgbd', 'reason': None,
            'depth_m': round(float(np.median([s[1] for s in samples])), 3),
            'boundary_margin_m': round(margin, 3),
            'depth_support': 'visible_body_patches' if person else 'visible_surface_patches',
            'anchor_count': len(samples)}


def _person_crosses_region(item, spatial, depth, regions, region, center):
    """Veto a room claim when another visible nearby body surface crosses its boundary."""
    height, width = depth.shape
    left, top, right, bottom = (np.asarray(item['image_bbox']) / 1000
                                * [width, height, width, height])
    yy, xx = np.nonzero(np.isfinite(depth) & (depth >= .15) & (depth <= 10))
    inside = (xx > left) & (xx < right) & (yy > top) & (yy < bottom)
    xx, yy = xx[inside], yy[inside]
    if not len(xx):
        return False
    z = depth[yy, xx]
    rays = _rays(np.column_stack((xx, yy)), spatial)
    points = _map_points(np.column_stack((rays*z[:, None], z)), spatial)
    # Background and floor are not evidence of a person's extent. Nearby uncertain
    # foreground may veto an assignment but can never establish a different room.
    nearby = ((np.linalg.norm(points[:, :2]-center[:2], axis=1) <= 1.2)
              & (points[:, 2] > .05))
    if not np.any(nearby):
        return False
    # Keep every valid foreground pixel when building the footprint: sparse
    # sampling could miss a thin limb in the adjacent region.
    hull = cv2.convexHull(points[nearby, :2].astype(np.float32)).reshape(-1, 2)
    margin = .12 + .02*float(np.max(z[nearby]))
    return (any(region_at(p, regions, margin)[0] != region for p in hull)
            or not _support_clear(hull, regions, margin))


def _object_position(item, metadata, regions, depth):
    unknown = {'status': 'unknown', 'region': None, 'map_position': None,
               'method': 'registered_rgbd', 'reason': None}
    spatial = metadata.get('spatial', {'status': 'legacy_without_depth'})
    bbox = item.get('image_bbox')
    if depth is None or spatial['status'] != 'available':
        return dict(unknown, reason=spatial['status'])
    if bbox is None or item['confidence'] != 'high':
        return dict(unknown, reason='object_not_grounded')
    if item.get('kind') == 'person':
        return _anchor_position(item, spatial, regions, depth, unknown)
    if 'image_anchors' in item:
        return _anchor_position(item, spatial, regions, depth, unknown, person=False)
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
    support, plane_coefficients, fit_error = None, None, 0.
    if spread > max(.08, z * .04):
        plane = _planar_support(roi, valid, x0, y0, spatial)
        if plane is None:
            return dict(unknown, reason='mixed_depth')
        (a, b, c, d), fit_error, support, plane_coefficients = plane
        roi, valid = roi[c:d, a:b], valid[c:d, a:b]
        x0, x1, y0, y1 = x0+a, x0+b, y0+c, y0+d
        if np.count_nonzero(valid) < 16 or np.mean(valid) < .7:
            return dict(unknown, reason='insufficient_depth')
        z = float(np.median(roi[valid]))
        yy, xx = np.nonzero(valid)
        patch_rays = _rays(np.column_stack((xx+x0, yy+y0)), spatial)
        inverse = np.column_stack((patch_rays, np.ones(len(xx)))) @ plane_coefficients
        if not np.isfinite(inverse).all() or np.any(inverse <= 0):
            return dict(unknown, reason='mixed_depth')
        spread = float(np.quantile(np.abs(roi[valid] - 1 / inverse), .95))
        if spread > max(.025, z * .015):
            return dict(unknown, reason='mixed_depth')
    yy, xx = np.nonzero(valid & (np.abs(roi - z) <= max(.025, spread * 2)))
    pixel = [float(np.median(xx)) + x0, float(np.median(yy)) + y0]
    ray = _rays([pixel], spatial)[0]
    if not np.isfinite(ray).all():
        return dict(unknown, reason='calibration_unsupported')
    if plane_coefficients is not None:
        predicted_inverse = np.dot(np.r_[ray, 1.], plane_coefficients)
        if predicted_inverse <= 0 or abs(z - 1 / predicted_inverse) > max(.025, z * .015):
            return dict(unknown, reason='mixed_depth')
    camera = np.array([ray[0] * z, ray[1] * z, z])
    world = _map_points(camera, spatial)
    # Check uncertainty and the measured floor-plane extent separately: an object's
    # vertical image height is not uncertainty in its horizontal map position.
    margin = .12 + .02 * z + max(spread, fit_error)
    if support is None:
        rays = _rays(np.column_stack((xx+x0, yy+y0)), spatial)
        measured = roi[yy, xx]
        support = _map_points(np.column_stack((rays*measured[:, None], measured)), spatial)
    region, reason = region_at(world[:2], regions, margin)
    if region:
        # A small anchor must not hide a broad surface spanning several map regions.
        if not _support_clear(support[:, :2], regions, margin):
            region, reason = None, 'surface_crosses_regions'
    result = {
        'status': 'located' if region else 'unknown', 'region': region,
        'map_position': dict(zip(('x', 'y', 'z'), map(lambda v: round(float(v), 3), world))),
        'method': 'registered_rgbd', 'reason': reason,
        'depth_m': round(z, 3), 'boundary_margin_m': round(margin, 3)}
    if plane_coefficients is not None:
        result.update(depth_support='planar_center_patch', surface_fit_error_m=round(fit_error, 3))
    return result


def localize_objects(analysis, metadata, regions, depth, lidar=None):
    """Only trusted geometry assigns object regions; never copy the capture room."""
    result = copy.deepcopy(analysis)
    object_regions = getattr(regions, 'object_regions', regions)
    for item in result['objects']:
        item['spatial'] = _object_position(item, metadata, object_regions, depth)
        policy = getattr(regions, 'object_region_policy', None)
        if policy is not None:
            item['spatial']['region_basis'] = copy.deepcopy(policy)
        if item['spatial']['status'] == 'unknown' and regions:
            hint = _reobserve_hint(item, metadata, depth)
            if hint is not None:
                item['spatial']['reobserve_hint'] = hint
            else:
                hint = _bearing_hint(item, metadata)
                if hint is not None:
                    item['spatial']['reobserve_hint'] = hint
        if item.get('kind') == 'person' and lidar is not None:
            from malbut_agent_server.patrol_lidar import compare_person_lidar
            other_boxes = [o['image_bbox'] for o in result['objects']
                           if o is not item and o.get('kind') == 'person' and o.get('image_bbox')]
            item['spatial']['lidar_comparison'] = compare_person_lidar(
                item, metadata, depth, lidar, other_boxes)
    return result


def _bearing_hint(item, metadata):
    """Keep a sighting direction even without usable depth; the range is an assumption."""
    bbox = item.get('image_bbox')
    fov = metadata.get('camera_hfov_rad')
    if (item.get('confidence') != 'high' or not bbox
            or type(fov) not in (float, int) or not math.isfinite(fov) or not 0 < fov < math.pi):
        return None
    pose = metadata['camera_pose']
    horizontal = ((bbox[0]+bbox[2])/2000-.5)*2*math.tan(fov/2)
    bearing = pose['yaw']-math.atan(horizontal)
    return {'xy': [round(pose['x']+1.5*math.cos(bearing), 3),
                   round(pose['y']+1.5*math.sin(bearing), 3)],
            'purpose': 'aim_only_not_object_position', 'source': 'camera_bearing',
            'assumed_range_m': 1.5}


def _reobserve_hint(item, metadata, depth):
    """Estimate an aiming point only; never turn uncertain geometry into a room claim."""
    spatial = metadata.get('spatial', {})
    if (depth is None or spatial.get('status') != 'available'
            or item.get('confidence') != 'high' or not item.get('image_bbox')):
        return None
    if item.get('kind') == 'person':
        from malbut_agent_server.patrol_lidar import _body_mask
        mask = _body_mask(item, depth)
    else:
        height, width = depth.shape
        left, top, right, bottom = np.asarray(item['image_bbox']) / 1000
        x0, x1 = int((left*.7+right*.3)*width), int((left*.3+right*.7)*width)
        y0, y1 = int((top*.7+bottom*.3)*height), int((top*.3+bottom*.7)*height)
        mask = np.zeros(depth.shape, dtype=bool)
        mask[y0:y1, x0:x1] = True
    mask &= np.isfinite(depth) & (depth >= .15) & (depth <= 5)
    yy, xx = np.nonzero(mask)
    if len(xx) < 16:
        return None
    z = depth[yy, xx]
    if np.quantile(z, .9)-np.quantile(z, .1) > .8:
        return None
    rays = _rays(np.column_stack((xx, yy)), spatial)
    points = _map_points(np.column_stack((rays*z[:, None], z)), spatial)
    center = np.median(points[:, :2], axis=0)
    if (not np.isfinite(center).all()
            or np.quantile(np.linalg.norm(points[:, :2]-center, axis=1), .9) > .8):
        return None
    return {'xy': np.round(center, 3).tolist(), 'purpose': 'aim_only_not_object_position'}
