"""Offline verification from retained *observed* poses, never gap-filled poses.

No labels, images, clinical decision, VLM call, or production registration.
Missing time and actual adjacent observation span have different fields.
"""
from collections import deque
import copy
from dataclasses import asdict, dataclass, field
import math


@dataclass(frozen=True)
class PoseRetentionConfig:
    reobserved_low: bool = True
    transition_followup: bool = True
    window_sec: float = 2.0
    maximum_frame_gap_sec: float = .5
    maximum_missing_sec: float = .65
    minimum_low_observations: int = 3
    minimum_evidence_window_sec: float = .6
    maximum_followup_sec: float = .65
    minimum_followup_observations: int = 2
    minimum_followup_span_sec: float = .15
    minimum_followup_body_points: int = 6
    minimum_followup_torso_deg: float = 35.
    minimum_followup_box_aspect: float = 1.1
    minimum_followup_body_aspect: float = 1.1
    maximum_followup_height_ratio: float = 1.1

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name in {'reobserved_low', 'transition_followup'}:
                if type(value) is not bool:
                    raise ValueError('invalid ' + name)
            elif type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('invalid ' + name)
        for name, floor in [('minimum_low_observations', 3),
                            ('minimum_followup_observations', 2),
                            ('minimum_followup_body_points', 4)]:
            if type(getattr(self, name)) is not int or not floor <= getattr(self, name) <= 60:
                raise ValueError('invalid ' + name)
        if not (self.maximum_frame_gap_sec <= self.maximum_missing_sec <= self.window_sec
                and self.minimum_evidence_window_sec <= self.window_sec
                and self.minimum_followup_span_sec <= self.maximum_followup_sec <= self.window_sec
                and self.minimum_followup_torso_deg < 90):
            raise ValueError('inconsistent retention thresholds')


@dataclass
class _State:
    low: deque = field(default_factory=lambda: deque(maxlen=60))
    gaps: deque = field(default_factory=lambda: deque(maxlen=60))
    seed: dict = None
    followup: deque = field(default_factory=lambda: deque(maxlen=60))
    latest: dict = None
    previous_missing: bool = False
    emitted: set = field(default_factory=set)

    def clear(self):
        self.low.clear()
        self.gaps.clear()
        self.seed = self.latest = None
        self.followup.clear()
        self.previous_missing = False


def _low(feature):
    return bool(feature and feature['usable'] and (feature['near_floor'] or
                feature['floor_height_m'] is None and
                (feature['horizontal'] or feature['compact_body'])))


class PoseRetentionExperiment:
    """Clip-scoped add-on; tracking identity is never treated as proof."""

    def __init__(self, config=None):
        self.config = config or PoseRetentionConfig()
        self.states = {}
        self.last_stamp = self.last_frame = self.size = self.case = None
        self.next_candidate = 1

    def _clear(self):
        for state in self.states.values():
            state.clear()

    def _supports_seed(self, feature, seed):
        cfg = self.config
        return bool(feature['body_points'] >= cfg.minimum_followup_body_points
                    and feature['torso_angle_deg'] >= cfg.minimum_followup_torso_deg
                    and feature['box_aspect'] >= cfg.minimum_followup_box_aspect
                    and feature['body_spread_aspect'] >= cfg.minimum_followup_body_aspect
                    and feature['box_height'] <= seed['features']['box_height']
                    * cfg.maximum_followup_height_ratio)

    def _candidate(self, reason, state, current, tid, base):
        gap = reason == 'low_pose_reobserved_with_retained_evidence'
        samples = list(state.low) if gap else [state.seed, *state.followup]
        uncertainty = list(current['features']['uncertainties']) + [
            'experimental_verification_trigger_not_confirmed_fall',
            'track_continuity_not_verified_identity']
        if gap:
            uncertainty += ['posture_during_gap_unknown', 'continuous_low_posture_unproven']
        else:
            uncertainty += ['followup_support_not_a_new_low_pose_label']
        if base['robotMotion'] != 'stationary':
            uncertainty.append('camera_motion_uncompensated')
        if current['features']['floor_height_m'] is None:
            uncertainty.append('lying_surface_unknown')
        if any(s['features']['box_confidence'] < .45 for s in samples):
            uncertainty.append('weak_pose_detection')
        adjacent = sum(b['time'] - a['time'] for a, b in zip(samples, samples[1:])
                       if b['connected'])
        result = dict(
            candidateId=f'pose-retention-{self.next_candidate}-{tid}', revision=1,
            candidateKind='found_down' if gap else 'fall_suspected', source='yolo_pose',
            targetTrackId=tid, requiresVerification=True, observationAvailability='observed',
            observationId=current['observation_id'], emittedAtSec=current['time'],
            evidenceStartSec=samples[0]['time'], evidenceEndSec=current['time'], reasons=[reason],
            uncertainties=list(dict.fromkeys(uncertainty)),
            evidence=dict(pose=copy.deepcopy(current['features']), robotMotion=base['robotMotion'],
                          temporal=dict(basis=reason, observedSamples=copy.deepcopy(samples),
                                        evidenceWindowSpanSec=current['time']-samples[0]['time'],
                                        observedAdjacentSpanSec=adjacent,
                                        missingSamples=copy.deepcopy([
                                            g for g in state.gaps
                                            if g['time'] > samples[0]['time']]),
                                        currentPoseObserved=True,
                                        currentLowPostureConfirmed=_low(current['features']))))
        self.next_candidate += 1
        return result

    def update(self, row, *, image_size):
        stamp, frame = row['timestamp_s'], row['frame_index']
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or type(frame) is not int or frame < 0 or len(image_size) != 2
                or any(type(v) is not int or v <= 0 for v in image_size)):
            raise ValueError('invalid frame metadata')
        cfg = self.config
        base = row.get('baseline_analysis', row['fall_analysis'])
        cid = row['case_id']
        if self.case is not None and cid != self.case:
            self.states.clear()
            self.last_stamp = self.last_frame = None
        self.case = cid
        if self.size is not None and image_size != self.size:
            self._clear()
        self.size = image_size
        if self.last_stamp is not None and (stamp <= self.last_stamp or frame <= self.last_frame):
            self._clear()
            return [], [dict(status='invalid_clock')]
        if self.last_stamp is not None and stamp-self.last_stamp > cfg.maximum_frame_gap_sec+1e-9:
            self._clear()
        self.last_stamp, self.last_frame = stamp, frame
        if base['status'] != 'ok':
            self._clear()
            return [], [dict(status='failed_frame')]
        if base['unassignedCount'] or any(o['track_id'] is None for o in row['observations']):
            self._clear()
            return [], [dict(status='unassigned_observation')]
        observed = {}
        for obs in row['observations']:
            if obs['track_id'] in observed:
                raise ValueError('multiple observations assigned to one track')
            observed[obs['track_id']] = obs
        active = {d['targetTrackId'] for d in base['tracks']}
        self.states = {k: s for k, s in self.states.items() if k in active}
        output, details = [], []
        for diag in base['tracks']:
            tid, feature = diag['targetTrackId'], diag['features']
            state = self.states.setdefault(tid, _State())
            if state.latest and stamp-state.latest['time'] > cfg.maximum_missing_sec+1e-9:
                state.clear()
            while state.low and stamp-state.low[0]['time'] > cfg.window_sec+1e-9:
                state.low.popleft()
            while state.gaps and stamp-state.gaps[0]['time'] > cfg.window_sec+1e-9:
                state.gaps.popleft()
            if state.seed and stamp-state.seed['time'] > cfg.maximum_followup_sec+1e-9:
                state.seed = None
                state.followup.clear()
            if tid not in observed:
                if diag['trackingState'] == 'missing' and feature is None and state.latest:
                    state.gaps.append(dict(frame=frame, time=stamp))
                    state.previous_missing = True
                    status = 'retaining_history_not_observing_pose'
                else:
                    state.clear()
                    status = 'reset_ambiguous_or_missing_history'
                details.append(dict(track_id=tid, status=status))
                continue
            if (diag['trackingState'] not in {'tracked', 'tentative'} or not feature
                    or not feature['usable'] or feature['floor_height_m'] is not None
                    and not feature['near_floor']):
                state.clear()
                details.append(dict(track_id=tid, status='reset_unusable_or_contradictory_pose'))
                continue
            anchor = tuple(feature['anchor_names'])
            if state.latest and anchor != tuple(state.latest['features']['anchor_names']):
                state.clear()
            obs = observed[tid]
            current = dict(frame=frame, time=stamp, features=copy.deepcopy(feature),
                           observation_id=base['observationId'],
                           observation_index=obs['observation_index'],
                           connected=bool(state.latest and not state.previous_missing))
            state.latest = current
            state.previous_missing = False
            reasons = []
            if cfg.reobserved_low and _low(feature):
                state.low.append(current)
                has_gap = bool(state.low and any(
                    state.low[0]['time'] < g['time'] < stamp for g in state.gaps))
                if (len(state.low) >= cfg.minimum_low_observations and has_gap
                        and stamp-state.low[0]['time']+1e-9 >= cfg.minimum_evidence_window_sec):
                    reasons.append('low_pose_reobserved_with_retained_evidence')
            else:
                state.low.clear()
            if cfg.transition_followup:
                # A seed must come from the UNCHANGED upstream transition rule.
                if _low(feature) and diag.get('transitionSamples', 0) >= 1:
                    if state.seed is None:
                        state.seed = copy.deepcopy(current)
                        state.followup.clear()
                elif state.seed and self._supports_seed(feature, state.seed):
                    state.followup.append(current)
                    followup_span = stamp-state.followup[0]['time']
                    if (len(state.followup) >= cfg.minimum_followup_observations
                            and followup_span+1e-9 >= cfg.minimum_followup_span_sec):
                        reasons.append('rapid_change_then_observed_support')
                elif state.seed:
                    state.seed = None
                    state.followup.clear()
            for reason in reasons:
                if reason not in state.emitted:
                    output.append(self._candidate(reason, state, current, tid, base))
                    state.emitted.add(reason)
            details.append(dict(track_id=tid,
                                status='verification_candidate' if reasons else 'collecting',
                                low_observations=len(state.low),
                                low_window_span_sec=stamp-state.low[0]['time'] if state.low else 0,
                                low_adjacent_span_sec=sum(
                                    b['time']-a['time']
                                    for a, b in zip(state.low, list(state.low)[1:])
                                    if b['connected']),
                                seed_frame=state.seed['frame'] if state.seed else None,
                                followup_observations=len(state.followup)))
        return output, details
