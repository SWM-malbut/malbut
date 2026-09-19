"""Bounded, full-frame-seeded ROI inference helpers. Offline experiment only.

Seeds never come from GT or ROI detections. A seed is not a verified person.
New detections are projected, not assigned their seed's track identity.
"""
from dataclasses import asdict, dataclass
import math

from homecam_detector.pose import PersonPose, PoseKeypoint, box_iou


@dataclass(frozen=True)
class RoiPoseConfig:
    seed_confidence: float = .45
    minimum_seed_samples: int = 3
    maximum_sample_gap_sec: float = .3
    seed_ttl_sec: float = 1.5
    maximum_rois_per_frame: int = 2
    width_factor: float = 3.0
    width_from_height: float = 2.0
    height_factor: float = 2.0
    height_from_width: float = 1.5
    minimum_side_px: int = 96
    downward_shift: float = .25
    duplicate_iou: float = .85

    def __post_init__(self):
        for name, v in asdict(self).items():
            if isinstance(v, bool) or not math.isfinite(v) or v <= 0:
                raise ValueError(f'invalid {name}')
        for name in ('minimum_seed_samples', 'maximum_rois_per_frame', 'minimum_side_px'):
            if type(getattr(self, name)) is not int:
                raise ValueError(f'{name} must be integer')
        if not (0 < self.seed_confidence <= 1 and 0 < self.duplicate_iou <= 1
                and self.minimum_seed_samples >= 2 and self.maximum_rois_per_frame <= 32
                and self.maximum_sample_gap_sec <= self.seed_ttl_sec):
            raise ValueError('invalid ROI bounds')


def crop_rectangle(box, image_size, cfg):
    if (len(image_size) != 2 or any(type(n) is not int or n <= 0 for n in image_size)
            or len(box) != 4 or not all(math.isfinite(v) and 0 <= v <= 1 for v in box)
            or box[0] >= box[2] or box[1] >= box[3]):
        raise ValueError('invalid crop geometry')
    width, height = image_size
    l, t, r, b = box[0]*width, box[1]*height, box[2]*width, box[3]*height
    w, h = r-l, b-t
    rw = max(cfg.width_factor*w, cfg.width_from_height*h, cfg.minimum_side_px)
    rh = max(cfg.height_factor*h, cfg.height_from_width*w, cfg.minimum_side_px)
    cx, cy = (l+r)/2, (t+b)/2 + cfg.downward_shift*h
    return (max(0, math.floor(cx-rw/2)), max(0, math.floor(cy-rh/2)),
            min(width, math.ceil(cx+rw/2)), min(height, math.ceil(cy+rh/2)))


def project_pose(pose, rectangle, image_size):
    l, t, r, b = rectangle
    w, h = image_size
    if (not all(type(n) is int for n in (*rectangle, *image_size))
            or not 0 <= l < r <= w or not 0 <= t < b <= h):
        raise ValueError('crop must be an integer rectangle within the actual image')
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in pose.box):
        raise ValueError('invalid normalized crop pose')

    def xy(x, y):
        if not all(math.isfinite(v) and 0 <= v <= 1 for v in (x, y)):
            raise ValueError('invalid normalized crop point')
        return (l+x*(r-l))/w, (t+y*(b-t))/h

    return PersonPose(pose.box_confidence, (*xy(*pose.box[:2]), *xy(*pose.box[2:])),
                      tuple(PoseKeypoint(p.name, *xy(p.x, p.y), p.confidence)
                            for p in pose.keypoints), pose.visible_keypoints)


def fuse_poses(full_poses, crop_observations, cfg):
    """Full-frame detections win near-identical duplicates; preserve distinct people."""
    poses, provenance = list(full_poses), [dict(source='full_frame') for _ in full_poses]
    records = []
    for obs in sorted(crop_observations, key=lambda o: (-o['pose'].box_confidence, o['pose'].box)):
        p = obs['pose']
        duplicate = next((i for i, old in enumerate(poses)
                          if box_iou(p.box, old.box) >= cfg.duplicate_iou), None)
        record = {k: v for k, v in obs.items() if k != 'pose'}
        record.update(pose=p.as_dict(), duplicate_of=duplicate)
        if duplicate is None:
            record['fused_observation_index'] = len(poses)
            poses.append(p)
            provenance.append({k: v for k, v in obs.items() if k != 'pose'})
        else:
            record['fused_observation_index'] = None
        records.append(record)
    return tuple(poses), provenance, records


class RoiPlanner:
    def __init__(self, config=None):
        self.config = config or RoiPoseConfig()
        self.streaks = {}
        self.seeds = {}
        self.last_attempt = {}
        self.last_time = None
        self.last_frame = None
        self.size = None

    def update(self, full_row, image_size):
        """Consumes ONLY the original full-frame observations and diagnostics."""
        stamp, frame = full_row['timestamp_s'], full_row['frame_index']
        if (not math.isfinite(stamp) or type(frame) is not int or frame < 0
                or self.last_time is not None and
                (stamp <= self.last_time or frame <= self.last_frame)):
            raise ValueError('non-increasing source clock')
        if self.size is not None and (
                self.size != image_size or
                stamp-self.last_time > self.config.maximum_sample_gap_sec+1e-9):
            self.streaks.clear()
            self.seeds.clear()
            self.last_attempt.clear()
        self.last_time, self.last_frame, self.size = stamp, frame, image_size
        cfg = self.config
        diagnostics = {d['targetTrackId']: d for d in full_row['baseline_analysis']['tracks']}
        obs = {o['track_id']: o for o in full_row['observations'] if o['track_id'] is not None}
        active = set(diagnostics)
        self.streaks = {tid: count for tid, count in self.streaks.items() if tid in active}
        for tid, diag in diagnostics.items():
            o = obs.get(tid)
            good = (o is not None and diag['trackingState'] in {'tracked', 'tentative'}
                    and o['features']['usable'] and
                    o['pose']['boxConfidence'] >= cfg.seed_confidence)
            self.streaks[tid] = self.streaks.get(tid, 0)+1 if good else 0
            if self.streaks[tid] >= cfg.minimum_seed_samples:
                self.seeds[tid] = dict(
                    seed_track_id=tid, seed_frame_index=frame, seed_timestamp_s=stamp,
                    seed_observation_index=o['observation_index'],
                    seed_box=tuple(o['pose']['box'][k]
                                   for k in ('left', 'top', 'right', 'bottom')))
        self.seeds = {t: s for t, s in self.seeds.items()
                      if stamp-s['seed_timestamp_s'] <= cfg.seed_ttl_sec+1e-9}
        self.last_attempt = {t: v for t, v in self.last_attempt.items() if t in self.seeds}
        if full_row['baseline_analysis']['status'] != 'ok':
            self.seeds.clear()
            self.streaks.clear()
            return [], 'full_frame_analysis_failed'
        if (full_row['baseline_analysis']['unassignedCount'] or
                any(o['track_id'] is None for o in full_row['observations'])):
            return [], 'association_ambiguous'
        eligible = []
        for tid, seed in self.seeds.items():
            diag = diagnostics.get(tid)
            if diag and diag['trackingState'] == 'ambiguous':
                continue
            o = obs.get(tid)
            if o is not None and o['features']['usable']:
                continue
            item = dict(seed, roi=crop_rectangle(seed['seed_box'], image_size, cfg))
            eligible.append(item)
        eligible.sort(key=lambda s: (self.last_attempt.get(s['seed_track_id'], -math.inf),
                                     -s['seed_timestamp_s'], s['seed_track_id']))
        selected = eligible[:cfg.maximum_rois_per_frame]
        for item in selected:
            self.last_attempt[item['seed_track_id']] = stamp
        return selected, 'roi_scheduled' if selected else 'no_eligible_missing_seed'
