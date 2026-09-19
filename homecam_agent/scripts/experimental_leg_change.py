"""Offline-only leg-layout experiment. Not imported by the ROS detector.

Translation/scale-normalized 2-D geometry is NOT measured floor height, nor
camera-motion compensation. Sitting and sinking can both require verification.
"""
from collections import deque
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math


@dataclass(frozen=True)
class LegChangeConfig:
    keypoint_threshold: float = .5
    window_sec: float = 2.0
    max_gap_sec: float = .5
    minimum_samples: int = 3
    maximum_torso_angle_deg: float = 45.0
    minimum_start_clearance: float = .45
    maximum_end_clearance: float = .25
    minimum_clearance_change: float = .25
    maximum_hip_above_knee: float = .05
    minimum_segment_length: float = .015

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'invalid {name}')
        if not 0 < self.keypoint_threshold <= 1:
            raise ValueError('invalid keypoint threshold')
        if type(self.minimum_samples) is not int or not 2 <= self.minimum_samples <= 60:
            raise ValueError('invalid sample count')
        if self.max_gap_sec > self.window_sec:
            raise ValueError('gap exceeds window')
        if not 0 < self.maximum_torso_angle_deg < 90:
            raise ValueError('invalid torso angle')
        if not 0 < self.maximum_end_clearance < self.minimum_start_clearance <= 1:
            raise ValueError('invalid clearance bounds')
        if self.minimum_clearance_change > 1 or self.maximum_hip_above_knee > 1:
            raise ValueError('invalid change bounds')

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def leg_layout(pose, image_size, feature, config):
    """Use the SAME named joints on both legs; never substitute one for another."""
    if (not feature['usable'] or feature['torso_angle_deg'] is None
            or feature['torso_angle_deg'] > config.maximum_torso_angle_deg):
        return None
    points = {}
    for p in pose.keypoints:
        if not all(math.isfinite(x) and 0 <= x <= 1 for x in (p.x, p.y, p.confidence)):
            continue
        if p.confidence >= config.keypoint_threshold:
            if p.name not in points or p.confidence > points[p.name].confidence:
                points[p.name] = p
    if not any(f'{side}_shoulder' in points for side in ('left', 'right')):
        return None
    scale = image_size[0] / image_size[1]
    legs = []
    for side in ('left', 'right'):
        names = [f'{side}_{j}' for j in ('hip', 'knee', 'ankle')]
        if any(n not in points for n in names):
            return None
        hip, knee, ankle = [(points[n].x * scale, points[n].y) for n in names]
        thigh, shin = math.dist(hip, knee), math.dist(knee, ankle)
        if min(thigh, shin) < config.minimum_segment_length:
            return None
        length = thigh + shin
        legs.append(dict(hip_above_ankle=(ankle[1] - hip[1]) / length,
                         hip_above_knee=(knee[1] - hip[1]) / length))
    return legs


@dataclass
class _State:
    samples: deque = field(default_factory=lambda: deque(maxlen=60))
    streak: int = 0
    streak_start: float = None
    emitted: bool = False

    def interrupt(self):
        self.samples.clear()
        self.streak = 0
        self.streak_start = None


class LegChangeExperiment:
    """One additional verification request per observed track, offline only.

    No confirmed-fall output, no recovery decision, no VLM invocation. A broken
    observation window breaks evidence immediately, as in the baseline.
    """
    def __init__(self, config=None):
        self.config = config or LegChangeConfig()
        self.states = {}
        self.last_stamp = None
        self.image_size = None

    def update(self, tracks, baseline, *, capture_time, image_size):
        if (not math.isfinite(capture_time) or len(image_size) != 2
                or any(type(v) is not int or v <= 0 for v in image_size)):
            raise ValueError('invalid frame metadata')
        if self.image_size is not None and image_size != self.image_size:
            self.states.clear()
        self.image_size = image_size
        if (baseline['status'] != 'ok' or
                self.last_stamp is not None and capture_time <= self.last_stamp):
            for state in self.states.values():
                state.interrupt()
            return [], []
        self.last_stamp = capture_time
        cfg = self.config
        active = {t.track_id for t in tracks.tracks}
        self.states = {k: v for k, v in self.states.items() if k in active}
        diagnostics = {d['targetTrackId']: d for d in baseline['tracks']}
        emitted, details = [], []
        for track in tracks.tracks:
            state = self.states.setdefault(track.track_id, _State())
            diag = diagnostics[track.track_id]
            layout = (leg_layout(track.pose, image_size, diag['features'], cfg)
                      if track.pose is not None and diag['features'] else None)
            if layout is None:
                state.interrupt()
                details.append(dict(track_id=track.track_id, layout=None,
                                    status='insufficient_leg_layout'))
                continue
            if state.samples and capture_time - state.samples[-1][0] > cfg.max_gap_sec:
                state.interrupt()
            while state.samples and capture_time - state.samples[0][0] > cfg.window_sec:
                state.samples.popleft()
            matches = []
            for stamp, previous in state.samples:
                if all(old['hip_above_ankle'] >= cfg.minimum_start_clearance
                       and new['hip_above_ankle'] <= cfg.maximum_end_clearance
                       and old['hip_above_ankle'] - new['hip_above_ankle']
                       >= cfg.minimum_clearance_change
                       and new['hip_above_knee'] <= cfg.maximum_hip_above_knee
                       for old, new in zip(previous, layout)):
                    matches.append((stamp, previous))
            if matches:
                if state.streak == 0:
                    state.streak_start = matches[0][0]
                state.streak += 1
            else:
                state.streak = 0
                state.streak_start = None
            state.samples.append((capture_time, layout))
            status = 'collecting' if matches else 'no_candidate'
            if state.streak >= cfg.minimum_samples and not state.emitted:
                state.emitted = True
                status = 'verification_candidate'
                emitted.append(dict(
                    candidateId=f'leg-change-{track.track_id}', revision=1,
                    source='yolo_pose', candidateKind='fall_suspected',
                    targetTrackId=track.track_id,
                    observationId=baseline['observationId'],
                    evidenceStartSec=state.streak_start, evidenceEndSec=capture_time,
                    requiresVerification=True,
                    reasons=['bilateral_hip_ankle_clearance_change'],
                    uncertainties=['sitting_or_sinking_not_distinguished',
                                   'floor_distance_unavailable',
                                   'camera_motion_uncompensated',
                                   'experimental_rule_not_validated'],
                    evidence=dict(pose=diag['features'], robotMotion=baseline['robotMotion'],
                                  temporal=dict(startSec=state.streak_start,
                                                endSec=capture_time,
                                                basis='bilateral_relative_leg_layout',
                                                currentLegs=layout,
                                                matchedReferenceSec=matches[0][0],
                                                referenceLegs=matches[0][1],
                                                consecutiveSamples=state.streak)),
                ))
            details.append(dict(track_id=track.track_id, layout=layout,
                                status=status, consecutive_samples=state.streak))
        return emitted, details


def merge_requests(baseline, extra, requested):
    """First request per track; later evidence is an update, not another call.

    Clip-scoped comparison only. Does not implement product incident lifetime.
    Baseline candidates take precedence when both emit on the same frame.
    """
    output, updates = [], []
    for candidate in [*baseline, *extra]:
        tid = candidate['targetTrackId']
        if tid in requested:
            updates.append(dict(requestCandidateId=requested[tid], evidence=candidate))
        else:
            requested[tid] = candidate['candidateId']
            output.append(candidate)
    return output, updates
