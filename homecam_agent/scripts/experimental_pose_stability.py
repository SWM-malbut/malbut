"""Offline-only tolerance for one OBSERVED geometry disagreement, not missing pose.

No imputation, posture inference from IDs, alarm, robot command or recovery claim.
Only genuine adjacent low observations contribute observed time.
"""
from collections import deque
import copy
from dataclasses import asdict, dataclass, field
import math


@dataclass(frozen=True)
class PoseStabilityConfig:
    window_sec: float = 2.0
    max_sample_gap_sec: float = .5
    minimum_low_samples: int = 3
    minimum_low_span_sec: float = .6
    minimum_low_fraction: float = .75
    maximum_disagreements: int = 1
    minimum_box_aspect: float = 1.1
    minimum_body_aspect: float = 2.0
    maximum_body_vertical_fraction: float = .7

    def __post_init__(self):
        for name, value in asdict(self).items():
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f'invalid {name}')
        if type(self.minimum_low_samples) is not int or not 3 <= self.minimum_low_samples <= 60:
            raise ValueError('invalid low sample count')
        if type(self.maximum_disagreements) is not int or self.maximum_disagreements != 1:
            raise ValueError('this experiment permits exactly one disagreement')
        if not .5 < self.minimum_low_fraction <= 1:
            raise ValueError('low fraction must be a majority')
        if not 0 < self.maximum_body_vertical_fraction <= 1:
            raise ValueError('invalid vertical fraction')
        if max(self.max_sample_gap_sec, self.minimum_low_span_sec) > self.window_sec:
            raise ValueError('duration exceeds window')
        if min(self.minimum_box_aspect, self.minimum_body_aspect) <= 1:
            raise ValueError('aspect must be wide')


def posture(feature, config):
    if not feature or feature['usable'] is not True:
        return 'unusable'
    if feature['floor_height_m'] is not None:
        return 'low' if feature['near_floor'] else 'other'
    if feature['horizontal'] or feature['compact_body']:
        return 'low'
    checks = (
        ('box_aspect', config.minimum_box_aspect, True),
        ('body_spread_aspect', config.minimum_body_aspect, True),
        ('body_vertical_fraction', config.maximum_body_vertical_fraction, False),
    )
    for name, limit, lower in checks:
        v = feature[name]
        if (type(v) not in (int, float) or not math.isfinite(v)
                or (v < limit if lower else not 0 <= v <= limit)):
            return 'other'
    return 'disagreement'


@dataclass
class _State:
    samples: deque = field(default_factory=lambda: deque(maxlen=60))
    emitted: bool = False

    def interrupt(self):
        self.samples.clear()


class PoseStabilityExperiment:
    def __init__(self, config=None):
        self.config = config or PoseStabilityConfig()
        self.states = {}
        self.last_stamp = None
        self.last_frame = None
        self.image_size = None
        self.next_candidate = 1

    def update(self, row, *, image_size):
        """Consume only cached observations, never labels/boxes/onset annotations."""
        stamp, frame = row['timestamp_s'], row['frame_index']
        if (not math.isfinite(stamp) or type(frame) is not int or frame < 0
                or len(image_size) != 2 or any(type(v) is not int or v <= 0 for v in image_size)):
            raise ValueError('invalid source frame metadata')
        if self.image_size is not None and self.image_size != image_size:
            for state in self.states.values():
                state.interrupt()
        self.image_size = image_size
        base = row.get('baseline_analysis', row['fall_analysis'])
        if (base['status'] != 'ok' or self.last_stamp is not None
                and (stamp <= self.last_stamp or frame <= self.last_frame)):
            for state in self.states.values():
                state.interrupt()
            return [], []
        self.last_stamp, self.last_frame = stamp, frame
        active = {d['targetTrackId'] for d in base['tracks']}
        self.states = {t: s for t, s in self.states.items() if t in active}
        if base['unassignedCount'] or any(o['track_id'] is None for o in row['observations']):
            for state in self.states.values():
                state.interrupt()
            return [], [dict(status='unassigned_observation')]
        observed = {}
        for o in row['observations']:
            if o['track_id'] in observed:
                raise ValueError('duplicate observation for a track')
            observed[o['track_id']] = o
        cfg = self.config
        out, details = [], []
        for diag in base['tracks']:
            tid = diag['targetTrackId']
            state = self.states.setdefault(tid, _State())
            feature = diag['features']
            kind = posture(feature, cfg)
            reason = None
            if diag['trackingState'] not in {'tracked', 'tentative'} or tid not in observed:
                reason = 'missing_or_ambiguous'
            elif kind in {'unusable', 'other'}:
                reason = kind
            if reason:
                state.interrupt()
                details.append(dict(track_id=tid, status='reset_' + reason))
                continue
            anchor = tuple(feature['anchor_names'])
            if state.samples and (stamp - state.samples[-1]['time'] > cfg.max_sample_gap_sec
                                  or anchor != state.samples[-1]['anchor']):
                state.interrupt()
            while state.samples and stamp - state.samples[0]['time'] > cfg.window_sec:
                state.samples.popleft()
            # Start a history only with real positive evidence, never with an outlier.
            if kind == 'disagreement' and (not state.samples or sum(
                    not s['low'] for s in state.samples) >= cfg.maximum_disagreements):
                state.interrupt()
                details.append(dict(track_id=tid, status='reset_disagreement_limit'))
                continue
            state.samples.append(dict(time=stamp, frame=frame, low=kind == 'low', anchor=anchor,
                                      observation_id=base['observationId'],
                                      observation_index=observed[tid]['observation_index']))
            samples = list(state.samples)
            low = [s for s in samples if s['low']]
            bad = [s for s in samples if not s['low']]
            # Sum only intervals bounded by two adjacent actual LOW samples.
            span = sum(b['time'] - a['time'] for a, b in zip(samples, samples[1:])
                       if a['low'] and b['low'])
            fraction = len(low) / len(samples)
            emit = (not state.emitted and kind == 'low' and bool(bad)
                    and len(low) >= cfg.minimum_low_samples
                    and fraction >= cfg.minimum_low_fraction
                    and span + 1e-9 >= cfg.minimum_low_span_sec)
            if emit:
                state.emitted = True
                uncertainties = list(feature['uncertainties']) + [
                    'observed_geometry_disagreement_not_low_evidence',
                    'experimental_verification_trigger_not_confirmed_fall']
                if base['robotMotion'] != 'stationary':
                    uncertainties.append('camera_motion_uncompensated')
                if feature['floor_height_m'] is None:
                    uncertainties.append('lying_surface_unknown')
                if observed[tid]['pose']['boxConfidence'] < .45:
                    uncertainties.append('weak_pose_detection')
                out.append(dict(
                    candidateId=f'pose-stability-{self.next_candidate}-{tid}', revision=1,
                    candidateKind='found_down', source='yolo_pose', targetTrackId=tid,
                    observationId=base['observationId'], observationAvailability='observed',
                    evidenceStartSec=low[0]['time'], evidenceEndSec=stamp,
                    emittedAtSec=stamp, requiresVerification=True,
                    reasons=['repeated_low_pose_around_geometry_disagreement'],
                    uncertainties=list(dict.fromkeys(uncertainties)),
                    evidence=dict(pose=copy.deepcopy(feature), robotMotion=base['robotMotion'],
                                  temporal=dict(basis='observed_pose_stability',
                                                actualSamples=copy.deepcopy(samples),
                                                lowFrames=[s['frame'] for s in low],
                                                disagreementFrames=[s['frame'] for s in bad],
                                                observedLowSpanSec=span,
                                                lowFraction=fraction))))
                self.next_candidate += 1
            details.append(dict(track_id=tid, status='verification_candidate' if emit else kind,
                                low_samples=len(low), disagreements=len(bad),
                                observed_low_span_sec=span, low_fraction=fraction))
        return out, details
