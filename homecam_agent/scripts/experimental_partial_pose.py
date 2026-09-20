"""Offline, bounded verification triggers from observed partial/body-change evidence.

No GT, RGB inference, track aliasing, fall confirmation or production registration.
Missing joints and gaps are not imputed. Features in candidates remain unmodified.
"""
from collections import deque
import copy
from dataclasses import dataclass
import math
import statistics


BODY = {f'{s}_{j}' for s in ('left', 'right') for j in
        ('shoulder', 'elbow', 'wrist', 'hip', 'knee', 'ankle')}
HEAD = {'nose', 'left_eye', 'right_eye', 'left_ear', 'right_ear'}


@dataclass(frozen=True)
class PartialConfig:
    partial_upper: bool = True
    observed_descent: bool = True
    minimum_detection: float = .45
    minimum_joint: float = .65
    window_sec: float = 2.0
    maximum_gap_sec: float = .5
    partial_samples: int = 4
    partial_span_sec: float = .6
    descent_samples: int = 3
    descent_span_sec: float = .3

    def __post_init__(self):
        if type(self.partial_upper) is not bool or type(self.observed_descent) is not bool:
            raise ValueError('invalid switches')
        if not 0 < self.minimum_detection <= 1 or not 0 < self.minimum_joint <= 1:
            raise ValueError('invalid confidence')
        for key in ('window_sec', 'maximum_gap_sec', 'partial_span_sec', 'descent_span_sec'):
            v = getattr(self, key)
            if type(v) not in (float, int) or not math.isfinite(v) or v <= 0:
                raise ValueError('invalid time')
        if max(self.maximum_gap_sec, self.partial_span_sec,
               self.descent_span_sec) > self.window_sec:
            raise ValueError('inconsistent windows')
        for key in ('partial_samples', 'descent_samples'):
            minimum = 3 if key == 'partial_samples' else 2
            if type(getattr(self, key)) is not int or not minimum <= getattr(self, key) <= 60:
                raise ValueError('invalid sample count')


def geometry(observation, cfg):
    p = observation['pose']
    b = [p['box'][k] for k in ('left', 'top', 'right', 'bottom')]
    c = p['boxConfidence']
    if (type(c) not in (float, int) or not math.isfinite(c)
            or not cfg.minimum_detection <= c <= 1
            or not all(math.isfinite(v) and 0 <= v <= 1 for v in b)
            or b[0] >= b[2] or b[1] >= b[3]):
        return None
    points = {}
    seen = set()
    for point in p['keypoints']:
        name = point['name']
        if name in seen:
            return None
        seen.add(name)
        if (name in BODY | HEAD and
                all(math.isfinite(point[k]) and 0 <= point[k] <= 1
                    for k in ('x', 'y', 'confidence'))
                and point['confidence'] >= cfg.minimum_joint
                and b[0] <= point['x'] <= b[2] and b[1] <= point['y'] <= b[3]):
            points[name] = (point['x'], point['y'])
    return dict(box=b, points=points)


def partial_upper(sample, size):
    p, b = sample['points'], sample['box']
    if (len(BODY.intersection(p)) < 4 or not {'left_shoulder', 'right_shoulder'} <= p.keys()
            or {'left_hip', 'right_hip'} <= p.keys()):
        return False
    arms = any(all(f'{s}_{j}' in p for j in ('shoulder', 'elbow', 'wrist'))
               for s in ('left', 'right'))
    heads = [p[n][1] for n in HEAD.intersection(p)]
    aspect = (b[2]-b[0])*size[0]/((b[3]-b[1])*size[1])
    shoulder_y = (p['left_shoulder'][1]+p['right_shoulder'][1])/2
    return bool(arms and heads and aspect >= 1.5 and
                abs(statistics.median(heads)-shoulder_y) <= .35*(b[3]-b[1]))


def descent_evidence(before, after, size):
    common = BODY.intersection(before['points'], after['points'])
    anchors = {n for n in common if n.endswith(('_shoulder', '_hip'))}
    if len(common) < 4 or len(anchors) < 2:
        return None
    a, b = before['box'], after['box']
    ah, bh = a[3]-a[1], b[3]-b[1]
    ar = (a[2]-a[0])*size[0]/(ah*size[1])
    br = (b[2]-b[0])*size[0]/(bh*size[1])
    dt = after['time']-before['time']
    if dt <= 0 or not (ar <= 1 and br >= 1.1 and bh/ah <= .65 and br/ar >= 1.4):
        return None
    dy = statistics.median(after['points'][n][1]-before['points'][n][1] for n in common)
    if dy/ah < .25 or dy/(ah*dt) < .35:
        return None
    return dict(referenceFrame=before['frame'], referenceTimeSec=before['time'],
                commonJoints=sorted(common), heightRatio=bh/ah, aspectRatioChange=br/ar,
                descentBodyHeights=dy/ah, descentSpeedBodyHeightsPerSec=dy/(ah*dt))


class PartialPoseExperiment:
    def __init__(self, config=None):
        self.cfg = config or PartialConfig()
        self.states = {}
        self.last = self.size = None
        self.case = None
        self.counter = 0

    def update(self, row, *, image_size):
        stamp, frame = row['timestamp_s'], row['frame_index']
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or type(frame) is not int or frame < 0
                or len(image_size) != 2 or any(type(v) is not int or v <= 0 for v in image_size)):
            raise ValueError('invalid frame')
        base = row.get('baseline_analysis', row['fall_analysis'])
        if self.case is not None and row['case_id'] != self.case:
            self.states.clear()
            self.last = self.size = None
        self.case = row['case_id']
        invalid = self.last and (stamp <= self.last[0] or frame <= self.last[1])
        if (invalid or self.last and stamp-self.last[0] > self.cfg.maximum_gap_sec+1e-9
                or self.size and self.size != image_size or base['status'] != 'ok'):
            self.states.clear()
        self.last, self.size = (stamp, frame), image_size
        if invalid or base['status'] != 'ok':
            return [], []
        states = {d['targetTrackId']: d for d in base['tracks']}
        self.states = {tid: s for tid, s in self.states.items() if tid in states}
        seen, candidates, details = set(), [], []
        for obs in row['observations']:
            tid = obs['track_id']
            if tid is None:
                continue
            if tid in seen:
                raise ValueError('duplicate assignment')
            seen.add(tid)
            diag = states.get(tid)
            if not diag or diag['trackingState'] not in ('tracked', 'tentative'):
                self.states.pop(tid, None)
                continue
            feature = diag['features']
            if (feature and feature.get('floor_height_m') is not None
                    and not feature.get('near_floor')):
                self.states.pop(tid, None)
                continue
            box = obs['pose']['box']
            # Ambiguity is local to overlapping observations, not the whole image.
            unassigned_overlap = False
            for other in row['observations']:
                if other['track_id'] is not None:
                    continue
                b = other['pose']['box']
                intersection = max(0, min(box['right'], b['right']) -
                                   max(box['left'], b['left'])) * max(
                    0, min(box['bottom'], b['bottom'])-max(box['top'], b['top']))
                unassigned_overlap |= intersection > 0
            if unassigned_overlap:
                self.states.pop(tid, None)
                continue
            sample = geometry(obs, self.cfg)
            if sample is None:
                self.states.pop(tid, None)
                continue
            sample.update(frame=frame, time=stamp, observationIndex=obs['observation_index'])
            state = self.states.setdefault(tid, dict(
                history=deque(maxlen=60), partial=deque(maxlen=60),
                descent=deque(maxlen=60), emitted=set()))
            if state['history'] and stamp-state['history'][-1]['time'] > self.cfg.maximum_gap_sec:
                for key in ('history', 'partial', 'descent'):
                    state[key].clear()
            for key in ('history', 'partial', 'descent'):
                while state[key] and stamp-state[key][0]['time'] > self.cfg.window_sec:
                    state[key].popleft()
            partial = self.cfg.partial_upper and partial_upper(sample, image_size)
            proof = next((p for old in state['history']
                          if (p := descent_evidence(old, sample, image_size))), None)
            for key, enabled in [('partial', partial),
                                 ('descent', self.cfg.observed_descent and proof is not None)]:
                if enabled:
                    state[key].append(dict(
                        frame=frame, time=stamp, observationIndex=obs['observation_index'],
                        **({'descent': proof} if key == 'descent' else {})))
                else:
                    state[key].clear()
            state['history'].append(sample)
            for key, reason in [('partial', 'partial_upper_body_needs_review'),
                                ('descent', 'observed_descent_without_stable_final_angle')]:
                items = list(state[key])
                needed = self.cfg.partial_samples if key == 'partial' else self.cfg.descent_samples
                span = self.cfg.partial_span_sec if key == 'partial' else self.cfg.descent_span_sec
                if (key in state['emitted'] or len(items) < needed
                        or stamp-items[0]['time']+1e-9 < span):
                    continue
                self.counter += 1
                state['emitted'].add(key)
                start = items[0]['time'] if key == 'partial' else min(
                    s['descent']['referenceTimeSec'] for s in items)
                candidates.append(dict(
                    candidateId=f'partial-review-{self.counter}-{tid}',
                    revision=1,
                    candidateKind='found_down' if key == 'partial' else 'fall_suspected',
                    source='yolo_pose', targetTrackId=tid, observationAvailability='observed',
                    observationId=base['observationId'], evidenceStartSec=start,
                    evidenceEndSec=stamp, emittedAtSec=stamp, requiresVerification=True,
                    reasons=[reason], uncertainties=[
                        'experimental_verification_not_confirmed_fall',
                        'partial_or_unstable_pose', 'lying_surface_unknown',
                        'camera_motion_uncompensated', 'track_continuity_not_verified_identity'],
                    evidence=dict(
                        pose=copy.deepcopy(diag['features']), robotMotion=base['robotMotion'],
                        temporal=dict(basis=reason, observedSamples=copy.deepcopy(items),
                                      currentPoseObserved=True))))
            details.append(dict(track_id=tid, partial_samples=len(state['partial']),
                                descent_samples=len(state['descent'])))
        # Missing/ambiguous samples never count towards a sustained observation.
        for tid in self.states.keys()-seen:
            self.states[tid]['partial'].clear()
            self.states[tid]['descent'].clear()
        return candidates, details
