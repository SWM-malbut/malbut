"""Offline-only verification after short loss of observed low postures.

Missing frames supply NO posture, geometry, observed duration or transition.
This is a weaker 'please check' trigger, not evidence of a witnessed fall.
"""
from collections import deque
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math


@dataclass(frozen=True)
class PoseGapConfig:
    resume_after_missing: bool = True
    max_missing_sec: float = .5
    window_sec: float = 2.0
    minimum_low_samples: int = 3
    minimum_observed_span_sec: float = .4
    minimum_missing_samples: int = 2
    reobserved_span_sec: float = .6

    def __post_init__(self):
        if type(self.resume_after_missing) is not bool:
            raise ValueError('resume_after_missing must be boolean')
        for name, value in asdict(self).items():
            if name == 'resume_after_missing':
                continue
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'invalid {name}')
        for name in ('minimum_low_samples', 'minimum_missing_samples'):
            if type(getattr(self, name)) is not int or not 2 <= getattr(self, name) <= 60:
                raise ValueError(f'invalid {name}')
        if not self.max_missing_sec <= self.window_sec:
            raise ValueError('missing allowance exceeds window')
        if not self.minimum_observed_span_sec <= self.reobserved_span_sec <= self.window_sec:
            raise ValueError('invalid observed span thresholds')

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass
class _State:
    low: deque = field(default_factory=lambda: deque(maxlen=60))
    missing: deque = field(default_factory=lambda: deque(maxlen=60))
    consecutive_missing: int = 0
    emitted: bool = False

    def interrupt(self):
        self.low.clear()
        self.missing.clear()
        self.consecutive_missing = 0

    def observed_span(self):
        return sum(new['time'] - old['time'] for old, new in zip(self.low, list(self.low)[1:])
                   if new['connected'])


class PoseGapExperiment:
    def __init__(self, config=None):
        self.config = config or PoseGapConfig()
        self.states = {}
        self.last_time = None
        self.last_frame = None
        self.image_size = None

    def update(self, tracks, baseline, *, capture_time, image_size, frame_index, observations):
        if (not math.isfinite(capture_time) or type(frame_index) is not int or frame_index < 0
                or len(image_size) != 2 or any(type(v) is not int or v <= 0 for v in image_size)):
            raise ValueError('invalid source frame metadata')
        if self.image_size is not None and image_size != self.image_size:
            self.states.clear()
        self.image_size = image_size
        if (baseline['status'] != 'ok' or self.last_time is not None and
                (capture_time <= self.last_time or frame_index <= self.last_frame)):
            for state in self.states.values():
                state.interrupt()
            return [], []
        self.last_time, self.last_frame = capture_time, frame_index
        cfg = self.config
        active = {t.track_id for t in tracks.tracks}
        self.states = {k: v for k, v in self.states.items() if k in active}
        diagnostics = {d['targetTrackId']: d for d in baseline['tracks']}
        out, details = [], []
        for track in tracks.tracks:
            tid = track.track_id
            state = self.states.setdefault(tid, _State())
            feature = diagnostics[tid]['features']
            if state.low and capture_time - state.low[-1]['time'] > cfg.max_missing_sec + 1e-9:
                state.interrupt()
            while state.low and capture_time - state.low[0]['time'] > cfg.window_sec:
                state.low.popleft()
            while state.missing and capture_time - state.missing[0] > cfg.window_sec:
                state.missing.popleft()
            reason = None
            status = 'no_candidate'
            image_low = bool(feature and (feature['horizontal'] or feature['compact_body'])
                             and feature['floor_height_m'] is None)
            low_posture = bool(feature and feature['usable']
                               and (feature['near_floor'] or image_low))
            if track.pose is None:
                # Ambiguous/failed associations do not get the missing-track grace period.
                if track.state != 'missing' or tracks.unassigned or not state.low:
                    state.interrupt()
                else:
                    state.consecutive_missing += 1
                    state.missing.append(capture_time)
                    status = 'pending_missing'
                    if (len(state.low) >= cfg.minimum_low_samples
                            and state.observed_span() + 1e-9 >= cfg.minimum_observed_span_sec
                            and state.consecutive_missing >= cfg.minimum_missing_samples):
                        reason = 'low_pose_then_unobserved'
            elif low_posture:
                if state.consecutive_missing and not cfg.resume_after_missing:
                    state.interrupt()
                matching = [o for o in observations if o['track_id'] == tid]
                if len(matching) != 1:
                    raise ValueError('low posture needs one actual pose observation')
                state.low.append(dict(
                    time=capture_time, frame=frame_index,
                    observation_index=matching[0]['observation_index'],
                    observation_id=baseline['observationId'], features=feature,
                    connected=state.consecutive_missing == 0,
                    weak=track.pose.box_confidence < .45,
                ))
                state.consecutive_missing = 0
                status = 'collecting_observed_low_pose'
                if (cfg.resume_after_missing and state.missing
                        and len(state.low) >= cfg.minimum_low_samples
                        and state.observed_span() + 1e-9 >= cfg.reobserved_span_sec):
                    reason = 'low_pose_reobserved_after_gap'
            else:
                # An observed non-low or insufficient pose breaks the low-posture evidence.
                state.interrupt()
            if reason and not state.emitted:
                state.emitted = True
                latest = state.low[-1]
                uncertainty = list(latest['features']['uncertainties']) + [
                    'posture_during_gap_unknown', 'lying_surface_unknown',
                    'experimental_verification_trigger_not_confirmed_fall']
                if baseline['robotMotion'] != 'stationary':
                    uncertainty.append('camera_motion_uncompensated')
                if latest['weak']:
                    uncertainty.append('weak_pose_detection')
                missing = track.pose is None
                status = 'verification_requested_last_seen' if missing else 'verification_candidate'
                candidate = dict(
                    candidateId=f'pose-gap-{tid}', revision=1, candidateKind='found_down',
                    source='yolo_pose', targetTrackId=tid, requiresVerification=True,
                    observationId=latest['observation_id'],
                    observationAvailability='missing' if missing else 'observed',
                    emittedAtSec=capture_time, evidenceStartSec=state.low[0]['time'],
                    evidenceEndSec=latest['time'], reasons=[reason], uncertainties=uncertainty,
                    evidence=dict(pose=latest['features'], robotMotion=baseline['robotMotion'],
                                  temporal=dict(basis=reason,
                                                observedTimesSec=[s['time'] for s in state.low],
                                                observedFrames=[s['frame'] for s in state.low],
                                                observedSpanSec=state.observed_span(),
                                                missingTimesSec=list(state.missing),
                                                currentPoseAvailable=not missing)),
                )
                if missing:
                    candidate['evidenceReference'] = dict(
                        frameIndex=latest['frame'], timestampSec=latest['time'],
                        observationIndex=latest['observation_index'])
                out.append(candidate)
            details.append(dict(track_id=tid, status=status, observed_samples=len(state.low),
                                observed_span_sec=state.observed_span(),
                                missing_samples=state.consecutive_missing,
                                last_observed_frame=state.low[-1]['frame'] if state.low else None))
        return out, details
