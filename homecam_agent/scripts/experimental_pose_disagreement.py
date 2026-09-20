"""Request review of previously low posture followed by observed geometry conflict.

Current conflicting geometry is NOT counted as low posture or a witnessed fall.
"""
from collections import deque
import copy
from dataclasses import asdict, dataclass, field
import math

from experimental_pose_stability import PoseStabilityConfig, posture


@dataclass(frozen=True)
class PoseDisagreementConfig:
    window_sec: float = 2.0
    maximum_gap_sec: float = .5
    minimum_low_samples: int = 3
    minimum_low_span_sec: float = .4
    minimum_disagreement_samples: int = 2
    maximum_after_low_sec: float = .5

    def __post_init__(self):
        for name, v in asdict(self).items():
            if (type(v) not in (int, float) or not math.isfinite(v) or v <= 0):
                raise ValueError('invalid ' + name)
        for name in ('minimum_low_samples', 'minimum_disagreement_samples'):
            if type(getattr(self, name)) is not int or not 2 <= getattr(self, name) <= 60:
                raise ValueError('invalid sample count')
        if max(self.maximum_gap_sec, self.minimum_low_span_sec,
               self.maximum_after_low_sec) > self.window_sec:
            raise ValueError('duration exceeds window')


@dataclass
class _State:
    low: deque = field(default_factory=lambda: deque(maxlen=60))
    uncertain: deque = field(default_factory=lambda: deque(maxlen=60))
    emitted: bool = False

    def clear(self):
        self.low.clear()
        self.uncertain.clear()


class PoseDisagreementExperiment:
    def __init__(self, config=None):
        self.config = config or PoseDisagreementConfig()
        self.geometry = PoseStabilityConfig()
        self.states = {}
        self.last_stamp = None
        self.last_frame = None
        self.size = None
        self.next_candidate = 1

    def update(self, row, *, image_size):
        stamp, frame = row['timestamp_s'], row['frame_index']
        if (not math.isfinite(stamp) or type(frame) is not int or frame < 0
                or len(image_size) != 2 or any(type(x) is not int or x <= 0 for x in image_size)):
            raise ValueError('invalid frame metadata')
        base = row.get('baseline_analysis', row['fall_analysis'])
        if self.size is not None and image_size != self.size:
            for state in self.states.values():
                state.clear()
        self.size = image_size
        if (base['status'] != 'ok' or self.last_stamp is not None
                and (stamp <= self.last_stamp or frame <= self.last_frame)):
            for state in self.states.values():
                state.clear()
            return [], []
        self.last_stamp, self.last_frame = stamp, frame
        active = {d['targetTrackId'] for d in base['tracks']}
        self.states = {t: s for t, s in self.states.items() if t in active}
        if base['unassignedCount'] or any(o['track_id'] is None for o in row['observations']):
            for state in self.states.values():
                state.clear()
            return [], [dict(status='unassigned_observation')]
        observed = {}
        for o in row['observations']:
            if o['track_id'] in observed:
                raise ValueError('duplicate observation')
            observed[o['track_id']] = o
        cfg = self.config
        output, details = [], []
        for diag in base['tracks']:
            tid = diag['targetTrackId']
            state = self.states.setdefault(tid, _State())
            feature = diag['features']
            kind = posture(feature, self.geometry)
            if (kind in {'unusable', 'other'} or tid not in observed
                    or diag['trackingState'] not in {'tracked', 'tentative'}):
                state.clear()
                details.append(dict(track_id=tid, status='reset_invalid_or_other_posture'))
                continue
            anchor = tuple(feature['anchor_names'])
            latest = state.uncertain[-1] if state.uncertain else state.low[-1] if state.low else None
            if latest and (stamp - latest['time'] > cfg.maximum_gap_sec
                           or anchor != latest['anchor']):
                state.clear()
            while state.low and stamp - state.low[0]['time'] > cfg.window_sec:
                state.low.popleft()
            if kind == 'low':
                if state.uncertain:
                    state.clear()
                state.low.append(dict(frame=frame, time=stamp, anchor=anchor,
                                      observation_id=base['observationId'],
                                      observation_index=observed[tid]['observation_index'],
                                      features=copy.deepcopy(feature)))
            elif not state.low or stamp - state.low[-1]['time'] > cfg.maximum_after_low_sec + 1e-9:
                state.clear()
            else:
                state.uncertain.append(dict(frame=frame, time=stamp, anchor=anchor,
                                            observation_id=base['observationId'],
                                            observation_index=observed[tid]['observation_index'],
                                            features=copy.deepcopy(feature)))
            span = state.low[-1]['time'] - state.low[0]['time'] if state.low else 0.
            emit = (not state.emitted and kind == 'disagreement'
                    and len(state.low) >= cfg.minimum_low_samples
                    and span + 1e-9 >= cfg.minimum_low_span_sec
                    and len(state.uncertain) >= cfg.minimum_disagreement_samples)
            if emit:
                state.emitted = True
                uncertainty = list(feature['uncertainties']) + [
                    'current_low_posture_unconfirmed', 'observed_geometry_disagreement',
                    'experimental_verification_trigger_not_confirmed_fall']
                if feature['floor_height_m'] is None:
                    uncertainty.append('lying_surface_unknown')
                if base['robotMotion'] != 'stationary':
                    uncertainty.append('camera_motion_uncompensated')
                if observed[tid]['pose']['boxConfidence'] < .45:
                    uncertainty.append('weak_pose_detection')
                output.append(dict(
                    candidateId=f'pose-disagreement-{self.next_candidate}-{tid}', revision=1,
                    source='yolo_pose', candidateKind='found_down', targetTrackId=tid,
                    observationId=base['observationId'], observationAvailability='observed',
                    evidenceStartSec=state.low[0]['time'], evidenceEndSec=stamp, emittedAtSec=stamp,
                    requiresVerification=True, reasons=['low_pose_then_observed_geometry_disagreement'],
                    uncertainties=list(dict.fromkeys(uncertainty)),
                    evidence=dict(pose=copy.deepcopy(feature), robotMotion=base['robotMotion'],
                                  temporal=dict(basis='low_then_observed_disagreement',
                                      lowSamples=copy.deepcopy(list(state.low)),
                                      disagreementSamples=copy.deepcopy(list(state.uncertain)),
                                      observedLowSpanSec=span,
                                      lastLowTimestampSec=state.low[-1]['time'],
                                      currentPoseObserved=True, currentLowPostureConfirmed=False))))
                self.next_candidate += 1
            details.append(dict(track_id=tid, status='verification_candidate' if emit else kind,
                                low_samples=len(state.low), disagreement_samples=len(state.uncertain),
                                observed_low_span_sec=span))
        return output, details
