"""Conservative cross-view duplicate suppression; offline experiment only.

Starts AFTER the frozen .85-IoU fusion, not a replacement of production NMS.
No labels, track IDs, previous frames or Cloud regions enter this function.
"""
from dataclasses import asdict, dataclass
import itertools
import math

from replay_reviewed_pose_cloud import require
from homecam_detector.pose import COCO_KEYPOINT_NAMES, PersonPose, PoseKeypoint, box_iou

TORSO = frozenset(('left_shoulder', 'right_shoulder', 'left_hip', 'right_hip'))
BODY = frozenset(COCO_KEYPOINT_NAMES[5:])


@dataclass(frozen=True)
class DuplicateConfig:
    minimum_iou: float = .45
    keypoint_confidence: float = .5
    minimum_body_points: int = 4
    minimum_torso_points: int = 3
    maximum_distance: float = .10
    maximum_mean_distance: float = .06

    def __post_init__(self):
        for name, value in asdict(self).items():
            require(type(value) in (int, float) and math.isfinite(value) and value > 0,
                    'invalid duplicate config: ' + name)
        require(0 < self.minimum_iou <= 1 and 0 < self.keypoint_confidence <= 1,
                'invalid probability')
        require(type(self.minimum_body_points) is int and 4 <= self.minimum_body_points <= 12
                and type(self.minimum_torso_points) is int and 3 <= self.minimum_torso_points <= 4,
                'insufficient anatomy requirement')
        require(self.maximum_mean_distance <= self.maximum_distance <= 1, 'invalid distance bounds')


def pose_from_dict(value):
    require(value.get('present') is True, 'expected actual observed pose')
    pose = PersonPose(value['boxConfidence'],
        tuple(value['box'][k] for k in ('left', 'top', 'right', 'bottom')),
        tuple(PoseKeypoint(p['name'], p['x'], p['y'], p['confidence']) for p in value['keypoints']),
        value['visibleKeypoints'])
    validate_pose(pose)
    return pose


def validate_pose(pose):
    values = [pose.box_confidence, *pose.box]
    values += [v for p in pose.keypoints for v in (p.x, p.y, p.confidence)]
    require(all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in values),
            'invalid pose values')
    require(len(pose.box) == 4 and pose.box[0] < pose.box[2] and pose.box[1] < pose.box[3],
            'invalid pose box')
    names = [p.name for p in pose.keypoints]
    require(len(names) == len(set(names)) and set(names) <= set(COCO_KEYPOINT_NAMES),
            'duplicate or unknown keypoint names')
    require(type(pose.visible_keypoints) is int and 0 <= pose.visible_keypoints <= len(names),
            'invalid visible point count')


def pair_evidence(a, b, cfg, image_size=(640, 400)):
    """Distances use pixels / smaller observed box diagonal, not square pixels."""
    pa, pb = a['pose'], b['pose']
    overlap = box_iou(pa.box, pb.box)
    output = dict(compatible=False, reason=None, iou=overlap, distances={}, mean_distance=None)
    if a['source'] == b['source']:
        return dict(output, reason='same_view')
    if overlap < cfg.minimum_iou:
        return dict(output, reason='box_overlap')
    points_a = {p.name: p for p in pa.keypoints if p.confidence >= cfg.keypoint_confidence}
    points_b = {p.name: p for p in pb.keypoints if p.confidence >= cfg.keypoint_confidence}
    common = points_a.keys() & points_b.keys()
    if len(common & BODY) < cfg.minimum_body_points or len(common & TORSO) < cfg.minimum_torso_points:
        return dict(output, reason='insufficient_shared_anatomy')
    w, h = image_size
    scale = min(math.hypot((p.box[2]-p.box[0])*w, (p.box[3]-p.box[1])*h) for p in (pa, pb))
    distances = {name: math.hypot((points_a[name].x-points_b[name].x)*w,
                                 (points_a[name].y-points_b[name].y)*h)/scale
                 for name in sorted(common)}
    mean = sum(distances.values()) / len(distances)
    output.update(distances=distances, mean_distance=mean)
    if max(distances.values()) > cfg.maximum_distance or mean > cfg.maximum_mean_distance:
        return dict(output, reason='keypoint_disagreement')
    return dict(output, compatible=True, reason='box_and_named_keypoints_agree')


def suppress_duplicates(observations, cfg=None, image_size=(640, 400)):
    """Suppress only complete, one-per-view components; no transitive ID merge.

    A-B and B-C do not imply A-C. A one-to-many or incomplete component is kept
    entirely. The representative is an actual input, never an averaged body.
    """
    cfg = cfg or DuplicateConfig()
    require(len(image_size) == 2 and all(type(v) is int and v > 0 for v in image_size),
            'invalid image size')
    for obs in observations:
        require(isinstance(obs['source'], str) and bool(obs['source']), 'missing view provenance')
        validate_pose(obs['pose'])
    edges = {i: set() for i in range(len(observations))}
    pairs = []
    for i, j in itertools.combinations(range(len(observations)), 2):
        evidence = pair_evidence(observations[i], observations[j], cfg, image_size)
        pairs.append(dict(a=i, b=j, **evidence))
        if evidence['compatible']:
            edges[i].add(j); edges[j].add(i)
    seen, suppressed, groups = set(), {}, []
    for start in range(len(observations)):
        if start in seen:
            continue
        pending, component = [start], set()
        while pending:
            i = pending.pop()
            if i in component:
                continue
            component.add(i); pending.extend(edges[i]-component)
        seen.update(component)
        if len(component) < 2:
            continue
        indices = sorted(component)
        sources = [observations[i]['source'] for i in indices]
        complete = all(component-{i} <= edges[i] for i in component)
        if len(set(sources)) != len(sources) or not complete:
            groups.append(dict(indices=indices, suppressed=[], kept=None, reason='ambiguous_component'))
            continue
        winner = min(indices, key=lambda i: (-observations[i]['pose'].box_confidence,
                                             observations[i]['source'] != 'full', i))
        removed = [i for i in indices if i != winner]
        suppressed.update({i: winner for i in removed})
        groups.append(dict(indices=indices, suppressed=removed, kept=winner,
                           reason='cross_view_duplicate'))
    kept = [i for i in range(len(observations)) if i not in suppressed]
    return tuple(observations[i]['pose'] for i in kept), dict(
        kept_indices=kept, groups=groups, pair_evidence=pairs,
        input_sources=[o['source'] for o in observations])


def retained_observations(row, arm):
    """Recover measured poses and their views without reviving suppressed boxes."""
    poses = [pose_from_dict(p) for p in row['poses']]
    if arm == 'full':
        return [dict(source='full', pose=p) for p in poses]
    by_index = {}
    for prediction in row['view_predictions']:
        i = prediction['fused_index']
        if i is None:
            continue
        require(type(i) is int and 0 <= i < len(poses) and i not in by_index, 'bad fused index')
        require(prediction['pose'] == row['poses'][i], 'view provenance differs from actual pose')
        by_index[i] = dict(source=prediction['source'], pose=poses[i])
    require(set(by_index) == set(range(len(poses))), 'missing view provenance')
    return [by_index[i] for i in range(len(poses))]
