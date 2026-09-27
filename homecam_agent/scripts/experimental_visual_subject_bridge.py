"""Offline hypothesis only: conservative SAM segment <-> usable Pose token.

No incident state, alarm, clearance, provider or robot integration. Geometric
continuity is NOT proof of human identity, especially through full occlusion.
"""
from dataclasses import asdict, dataclass
import math


def valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(type(x) in (int, float) and math.isfinite(x) for x in box)
            and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)


def area(box):
    return (box[2]-box[0])*(box[3]-box[1])


def iou(a, b):
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(
        0, min(a[3], b[3])-max(a[1], b[1]))
    return intersection / (area(a)+area(b)-intersection)


def sample_schedule(frames, fps, seed=None):
    """Neutral 4 Hz grid with exact cached seed; never exceed 5 Hz spacing.

    Remove grid neighbors too near the seed. No annotations select samples.
    """
    if type(frames) is not int or frames <= 0 or not math.isfinite(fps) or fps < 5:
        raise ValueError('invalid media metadata')
    if seed is not None and (type(seed) is not int or not 0 <= seed < frames):
        raise ValueError('invalid seed index')
    minimum = math.ceil(fps/5 - 1e-10)
    grid = sorted({round(k*fps/4) for k in range(math.ceil(frames*4/fps))
                   if round(k*fps/4) < frames})
    if seed is not None:
        grid = sorted([i for i in grid if abs(i-seed) >= minimum] + [seed])
    if any(b-a < minimum for a, b in zip(grid, grid[1:])):
        raise ValueError('unsupported frame rate for sampling policy')
    return grid


@dataclass(frozen=True)
class Policy:
    min_iou: float = .60
    margin: float = .15
    confirmations: int = 3
    min_span_s: float = .5
    max_gap_s: float = .5
    visual_min_iou: float = .20
    visual_max_area_ratio: float = 3.0


class VisualSubjectBridge:
    """One visual object per instance. Multi-object assignment is not supported.

    All observed candidate boxes, including unusable/unassigned ones, compete.
    Missing/ambiguous Pose revokes the live alias immediately. A replacement
    token/ID must pass confirmation again; earlier frames are never backfilled.
    """
    def __init__(self, namespace, policy=Policy()):
        self.namespace, self.policy = namespace, policy
        self.segment = 0
        self.last_time = self.last_box = self.pending = self.active = None

    def step(self, now, visual_box, candidates):
        if not math.isfinite(now) or (self.last_time is not None and now <= self.last_time):
            raise ValueError('non-increasing or invalid observation time')
        if visual_box is not None and not valid_box(visual_box):
            raise ValueError('invalid visual box')
        ids = set()
        for c in candidates:
            if not valid_box(c['box']) or not isinstance(c['id'], str) or c['id'] in ids:
                raise ValueError('invalid/duplicate candidate')
            ids.add(c['id'])
            if type(c['usable']) is not bool or (c['usable'] and not c.get('token')):
                raise ValueError('usable candidate requires continuity token')
        events = []

        def revoke(reason):
            if self.active is not None:
                events.append(dict(event='revoked', reason=reason, **self.active))
            self.active = self.pending = None

        gap = self.last_time is not None and now-self.last_time > self.policy.max_gap_s+1e-9
        broken = self.last_box is None or gap
        if visual_box is not None and self.last_box is not None:
            ratio = max(area(visual_box), area(self.last_box))/min(area(visual_box), area(self.last_box))
            broken |= iou(visual_box, self.last_box) < self.policy.visual_min_iou or ratio > self.policy.visual_max_area_ratio
        self.last_time, self.last_box = now, visual_box
        if visual_box is None:
            revoke('visual_missing')
            return dict(common_id=None, active=None, reason='visual_missing', events=events, best_iou=None)
        if broken:
            revoke('visual_continuity_break')
            self.segment += 1
        common_id = f'{self.namespace}:v{self.segment}'
        ranked = sorted([(iou(visual_box, c['box']), c) for c in candidates],
                        key=lambda item: item[0], reverse=True)
        best = ranked[0][0] if ranked else None
        if not ranked or best < self.policy.min_iou:
            reason = 'no_overlap'
        elif len(ranked)>1 and best-ranked[1][0] < self.policy.margin:
            reason = 'ambiguous'
        elif not ranked[0][1]['usable']:
            reason = 'pose_unusable'
        else:
            reason = None
        if reason:
            revoke(reason)
            return dict(common_id=common_id, active=None, reason=reason, events=events, best_iou=best)
        c = ranked[0][1]
        key = (c['id'], c['token'])
        if self.active is not None and key != (self.active['pose_id'], self.active['pose_token']):
            revoke('pose_identity_or_token_changed')
        if self.pending is None or self.pending['key'] != key:
            self.pending = dict(key=key, start=now, count=0)
        self.pending['count'] += 1
        if (self.active is None and self.pending['count'] >= self.policy.confirmations
                and now-self.pending['start'] >= self.policy.min_span_s-1e-9):
            self.active = dict(common_id=common_id, pose_id=c['id'], pose_token=c['token'])
            events.append(dict(event='linked', **self.active))
        return dict(common_id=common_id, active=dict(self.active) if self.active else None,
                    reason='linked' if self.active else 'pending', events=events, best_iou=best)

    def parameters(self):
        return asdict(self.policy)
