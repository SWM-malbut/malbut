"""Experimental per-track fall candidates, not medical or incident decisions.

Uses observed poses only. No model/provider calls, robot commands, alarms,
wall-clock deadlines, or inference that missing observations mean recovery.
"""

from collections import deque
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import Deque, Dict, Mapping, Optional, Tuple
from uuid import uuid4

from .pose import PersonPose
from .pose_tracker import PoseTrackingResult


ALGORITHM_VERSION = "pose-temporal-candidates-v2"
BODY_NAMES = frozenset(f"{side}_{joint}" for side in ("left", "right")
                       for joint in ("shoulder", "elbow", "wrist", "hip", "knee", "ankle"))


@dataclass(frozen=True)
class FallCandidateConfig:
    """Pilot heuristics; these numbers are not validated operating thresholds."""

    keypoint_threshold: float = 0.5
    minimum_body_points: int = 4
    temporal_window_sec: float = 2.0
    max_frame_gap_sec: float = 0.5
    found_down_hold_sec: float = 0.6
    minimum_samples: int = 3
    horizontal_torso_deg: float = 60.0
    upright_torso_deg: float = 35.0
    minimum_box_aspect: float = 1.1
    compact_body_aspect: float = 2.0
    compact_box_aspect: float = 2.0
    compact_minimum_body_points: int = 6
    compact_max_vertical_fraction: float = 0.7
    minimum_tilt_change_deg: float = 30.0
    minimum_descent_body_lengths: float = 0.25
    minimum_descent_speed: float = 0.35
    minimum_height_loss: float = 0.25
    moving_shape_ratio: float = 1.4
    maximum_floor_height_m: float = 0.30
    rearm_upright_sec: float = 2.0

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0 < self.keypoint_threshold <= 1:
            raise ValueError("keypoint_threshold must be in (0, 1]")
        if type(self.minimum_body_points) is not int or not 2 <= self.minimum_body_points <= 12:
            raise ValueError("minimum_body_points must be an integer in [2, 12]")
        if type(self.minimum_samples) is not int or not 2 <= self.minimum_samples <= 60:
            raise ValueError("minimum_samples must be an integer in [2, 60]")
        if (type(self.compact_minimum_body_points) is not int
                or not 6 <= self.compact_minimum_body_points <= 12):
            raise ValueError("compact_minimum_body_points must be an integer in [6, 12]")
        if (self.compact_body_aspect <= 1 or self.compact_box_aspect <= 1
                or self.compact_max_vertical_fraction > 1):
            raise ValueError("invalid compact body thresholds")
        if not 0 < self.upright_torso_deg < self.horizontal_torso_deg < 90:
            raise ValueError("torso angles must satisfy 0 < upright < horizontal < 90")
        if self.minimum_tilt_change_deg >= 90 or self.minimum_height_loss >= 1:
            raise ValueError("invalid tilt/height change threshold")
        if self.max_frame_gap_sec > self.temporal_window_sec:
            raise ValueError("max_frame_gap_sec must not exceed temporal_window_sec")
        if self.found_down_hold_sec > self.temporal_window_sec:
            raise ValueError("found_down_hold_sec must not exceed temporal_window_sec")
        if self.moving_shape_ratio <= 1:
            raise ValueError("moving_shape_ratio must be greater than one")

    @property
    def sha256(self) -> str:
        content = json.dumps(asdict(self), sort_keys=True, allow_nan=False)
        return hashlib.sha256(content.encode()).hexdigest()


@dataclass(frozen=True)
class PoseFeatures:
    """Image geometry uses pixels scaled by source height, NOT normalized x/y angles."""

    usable: bool
    box_confidence: float
    body_points: int
    mean_body_confidence: Optional[float]
    box_aspect: float
    box_height: float
    torso_angle_deg: Optional[float]
    body_axis_angle_deg: Optional[float]
    torso_center_y: Optional[float]
    anchor_names: Tuple[str, ...]
    horizontal: bool
    compact_body: bool
    body_spread_aspect: Optional[float]
    body_vertical_fraction: Optional[float]
    floor_height_m: Optional[float]
    near_floor: bool
    uncertainties: Tuple[str, ...]


def _angle(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    return math.degrees(math.atan2(abs(dx), abs(dy)))


def _floor_height(depth: Mapping) -> Optional[float]:
    """Only use explicitly aligned, fresh, measured torso depth."""
    height = depth.get("torsoFloorDistanceM")
    ratio = depth.get("validTorsoRatio")
    points = depth.get("sampledTorsoPoints")
    if (depth.get("usable") is not True or depth.get("alignedToRgb") is not True
            or depth.get("stale") is not False
            or type(height) not in (float, int) or not math.isfinite(height) or height < 0
            or type(ratio) not in (float, int) or not 0.5 <= ratio <= 1
            or type(points) is not int or points < 2):
        return None
    return float(height)


def extract_pose_features(
    pose: PersonPose, image_size: Tuple[int, int], depth: Mapping,
    config: FallCandidateConfig,
) -> PoseFeatures:
    """Describe usable anatomy; an empty/poor skeleton is unknown, not normal."""
    width, height = image_size
    if width <= 0 or height <= 0:
        raise ValueError("source image dimensions must be positive")
    if (not math.isfinite(pose.box_confidence) or not 0 <= pose.box_confidence <= 1
            or len(pose.box) != 4
            or not all(math.isfinite(v) and 0 <= v <= 1 for v in pose.box)
            or pose.box[2] <= pose.box[0] or pose.box[3] <= pose.box[1]):
        raise ValueError("invalid pose geometry")
    points = {}
    for point in pose.keypoints:
        if (point.name not in BODY_NAMES or point.confidence < config.keypoint_threshold):
            continue
        if not all(math.isfinite(v) and 0 <= v <= 1
                   for v in (point.x, point.y, point.confidence)):
            continue
        # Invalid repeated joint names must not inflate observation quality.
        if point.name not in points or point.confidence > points[point.name].confidence:
            points[point.name] = point
    aspect_scale = width / height

    def center(joint):
        selected = [p for name, p in points.items() if name.endswith("_" + joint)]
        if not selected:
            return None
        return (sum(p.x for p in selected) / len(selected) * aspect_scale,
                sum(p.y for p in selected) / len(selected))

    shoulder, hip, ankle = center("shoulder"), center("hip"), center("ankle")
    box_height = pose.box[3] - pose.box[1]
    box_aspect = (pose.box[2] - pose.box[0]) * aspect_scale / box_height
    torso_length = math.dist(shoulder, hip) if shoulder and hip else 0.0
    usable = (len(points) >= config.minimum_body_points
              and torso_length >= max(0.015, 0.1 * box_height))
    torso_angle = _angle(shoulder, hip) if usable else None
    body_axis = _angle(shoulder, ankle) if usable and ankle else None
    horizontal = bool(usable and torso_angle >= config.horizontal_torso_deg
                      and box_aspect >= config.minimum_box_aspect
                      and (body_axis is None or body_axis >= config.horizontal_torso_deg))
    # A front-facing, low camera can foreshorten the torso. Use the measured
    # shoulder/hip/leg layout as an alternative, never just a wide box or ID.
    body_width = ((max(p.x for p in points.values()) - min(p.x for p in points.values()))
                  * aspect_scale) if points else 0.0
    body_height = (max(p.y for p in points.values()) - min(p.y for p in points.values())
                   if points else 0.0)
    body_aspect = body_width / body_height if body_height > 0 else None
    vertical_fraction = body_height / box_height if points else None
    anchors_present = all(f"{side}_{joint}" in points for side in ("left", "right")
                          for joint in ("shoulder", "hip"))
    legs_present = any(name.endswith(("_knee", "_ankle")) for name in points)
    compact = bool(usable and len(points) >= config.compact_minimum_body_points
                   and anchors_present and legs_present
                   and box_aspect >= config.compact_box_aspect
                   and body_aspect is not None and body_aspect >= config.compact_body_aspect
                   and vertical_fraction <= config.compact_max_vertical_fraction)
    floor = _floor_height(depth)
    uncertainty = []
    if not usable:
        uncertainty.append("insufficient_body_keypoints")
    if ankle is None:
        uncertainty.append("feet_not_visible")
    if floor is None:
        uncertainty.append("floor_distance_unavailable")
    # This is a quality warning, NOT another person-confidence gate.
    return PoseFeatures(
        usable, pose.box_confidence, len(points),
        sum(p.confidence for p in points.values()) / len(points) if points else None,
        box_aspect, box_height, torso_angle, body_axis,
        (shoulder[1] + hip[1]) / 2 if usable else None,
        tuple(sorted(name for name in points if name.endswith(("_shoulder", "_hip")))),
        horizontal, compact, body_aspect, vertical_fraction,
        floor, bool(usable and floor is not None and floor <= config.maximum_floor_height_m),
        tuple(uncertainty),
    )


@dataclass(frozen=True)
class _Sample:
    stamp: float
    features: PoseFeatures
    robot_motion: str


@dataclass
class _TrackState:
    samples: Deque[_Sample] = field(default_factory=lambda: deque(maxlen=60))
    down: Deque[_Sample] = field(default_factory=lambda: deque(maxlen=60))
    transition_hits: int = 0
    upright_since: Optional[float] = None
    candidate: Optional[dict] = None
    clear_since: Optional[float] = None
    clear_samples: int = 0

    def interrupt(self):
        self.samples.clear()
        self.down.clear()
        self.transition_hits = 0
        self.upright_since = None
        self.clear_since = None
        self.clear_samples = 0


class FallCandidateDetector:
    """Produce explainable, deduplicated requests for later verification.

    Source-camera timestamps drive posture changes. Track IDs only partition
    observations. They are never features or proof of a human/fall.
    """

    def __init__(self, config: Optional[FallCandidateConfig] = None):
        self.config = config or FallCandidateConfig()
        self._states: Dict[str, _TrackState] = {}
        self._prefix = uuid4().hex
        self._next_candidate = 1
        self._next_observation = 1
        self._last_stamp = None
        self._image_size = None
        self._config_sha = self.config.sha256

    def reset(self):
        """Discard working histories, not a clinical statement of recovery."""
        self._states.clear()
        self._last_stamp = None
        self._image_size = None

    def unavailable(self, status: str) -> dict:
        """Break temporal windows, retaining already raised candidate identity."""
        for state in self._states.values():
            state.interrupt()
        return self._output(status, [], [])

    def _output(self, status, candidates, tracks):
        return dict(schemaVersion=1, algorithmVersion=ALGORITHM_VERSION,
                    configSha256=self._config_sha, status=status,
                    subjectCheckVersion=1, subjectCheckMaxGapSec=self.config.max_frame_gap_sec,
                    timeBase="ros_image_stamp", candidates=candidates, tracks=tracks)

    def update(
        self, result: PoseTrackingResult, *, capture_time: float,
        image_size: Tuple[int, int], robot_motion: str,
        depth_by_track: Optional[Mapping[str, Mapping]] = None,
    ) -> dict:
        """Process one successful Pose sample. No motion/strong-score precondition."""
        if not math.isfinite(capture_time):
            raise ValueError("capture_time must be finite")
        if robot_motion not in {"stationary", "moving", "unknown"}:
            raise ValueError("unsupported robot_motion")
        if (len(image_size) != 2
                or any(type(v) is not int or v <= 0 for v in image_size)):
            raise ValueError("image_size must contain positive integer width/height")
        if self._last_stamp is not None and capture_time <= self._last_stamp:
            return self.unavailable("invalid_capture_time")
        if self._image_size is not None and image_size != self._image_size:
            self.reset()
        depth_by_track = depth_by_track or {}
        # Validate/extract all observations before modifying temporal state.
        features = {track.track_id: extract_pose_features(
            track.pose, image_size, depth_by_track.get(track.track_id, {}), self.config
        ) for track in result.tracks if track.pose is not None}
        self._last_stamp = capture_time
        self._image_size = image_size
        active_ids = {track.track_id for track in result.tracks}
        self._states = {key: state for key, state in self._states.items() if key in active_ids}
        observation_id = f"{self._prefix}-frame-{self._next_observation}"
        self._next_observation += 1
        emitted, diagnostics = [], []
        for track in result.tracks:
            state = self._states.setdefault(track.track_id, _TrackState())
            feature = features.get(track.track_id)
            diagnostic = dict(targetTrackId=track.track_id, trackingState=track.state,
                              features=asdict(feature) if feature else None,
                              box=list(track.pose.box) if track.pose else None,
                              associationUsable=bool(
                                  feature and feature.usable and track.state == "tracked"
                                  and track.confidence_level == "strong"
                                  and not result.unassigned),
                              subjectCheck=dict(state="unknown", reason="insufficient_observation"),
                              activeCandidateId=(state.candidate["candidateId"]
                                                 if state.candidate else None))
            if feature is None or not feature.usable:
                state.interrupt()
                diagnostic["status"] = ("insufficient_pose" if feature else track.state)
                diagnostics.append(diagnostic)
                continue
            if (state.samples
                    and capture_time - state.samples[-1].stamp > self.config.max_frame_gap_sec):
                state.interrupt()
            while (state.samples
                   and capture_time - state.samples[0].stamp > self.config.temporal_window_sec):
                state.samples.popleft()
            sample = _Sample(capture_time, feature, robot_motion)
            low_posture = (feature.near_floor
                           or ((feature.horizontal or feature.compact_body)
                               and feature.floor_height_m is None))
            upright = (feature.torso_angle_deg <= self.config.upright_torso_deg
                       and not low_posture)
            if upright:
                if state.upright_since is None:
                    state.upright_since = capture_time
                if capture_time - state.upright_since >= self.config.rearm_upright_sec:
                    # Permit a later new candidate; never emit "resolved"/"normal".
                    state.candidate = None
            else:
                state.upright_since = None
            transition = self._transition(state, sample)
            state.transition_hits = state.transition_hits + 1 if transition else 0
            state.samples.append(sample)
            if low_posture:
                state.down.append(sample)
            else:
                state.down.clear()
            while (state.down
                   and capture_time - state.down[0].stamp > self.config.temporal_window_sec):
                state.down.popleft()
            sustained = (len(state.down) >= self.config.minimum_samples
                         and capture_time - state.down[0].stamp + 1e-9
                         >= self.config.found_down_hold_sec)
            kind = None
            reasons = []
            evidence_start = capture_time
            if transition and state.transition_hits >= 2:
                kind = "fall_suspected"
                evidence_start = transition["startSec"]
                reasons = ["rapid_posture_change", transition["basis"]]
            elif sustained:
                kind = "found_down"
                evidence_start = state.down[0].stamp
                reasons = ["sustained_low_posture"]
            if kind:
                reasons += (["horizontal_torso_and_body"] if feature.horizontal else [])
                reasons += (["compact_shoulder_hip_leg_layout"] if feature.compact_body else [])
                reasons += (["measured_torso_near_floor"] if feature.near_floor else [])
                candidate = self._candidate(
                    state, track.track_id, kind, observation_id, evidence_start,
                    sample, reasons, transition, track.confidence_level,
                )
                if candidate is not None:
                    emitted.append(candidate)
                diagnostic["status"] = "verification_candidate"
            else:
                diagnostic["status"] = (
                    "collecting" if low_posture or transition else "no_candidate"
                )
            diagnostic["activeCandidateId"] = (state.candidate["candidateId"]
                                               if state.candidate else None)
            diagnostic["lowPostureSamples"] = len(state.down)
            diagnostic["transitionSamples"] = state.transition_hits
            # Additional observation output only. Do not change candidate
            # thresholds, re-arm behaviour or turn this into a recovery claim.
            stable = (diagnostic["associationUsable"] and robot_motion == "stationary"
                      and upright and not transition and not kind
                      and len(feature.anchor_names) == 4
                      and feature.body_axis_angle_deg is not None
                      and feature.body_axis_angle_deg <= self.config.upright_torso_deg
                      and not feature.horizontal and not feature.compact_body)
            if stable:
                if state.clear_since is None:
                    state.clear_since = capture_time
                state.clear_samples += 1
            else:
                state.clear_since, state.clear_samples = None, 0
            clear = (stable and state.clear_samples >= self.config.minimum_samples
                     and capture_time - state.clear_since >= self.config.rearm_upright_sec
                     and state.candidate is None)
            suspect = bool(low_posture or transition or kind or state.candidate is not None)
            diagnostic["subjectCheck"] = dict(
                state="clear" if clear else "suspected" if suspect else "unknown",
                reason="stable_upright" if clear else "posture_suspected" if suspect
                else "insufficient_observation",
            )
            diagnostics.append(diagnostic)
        output = self._output("ok", emitted, diagnostics)
        output.update(observationId=observation_id, captureTimeSec=capture_time,
                      robotMotion=robot_motion, unassignedCount=len(result.unassigned),
                      expiredTrackIds=list(result.expired_track_ids))
        return output

    def _transition(self, state: _TrackState, current: _Sample):
        """Require real posture change, never just screen translation or an ID."""
        cfg, feature = self.config, current.features
        matches = []
        for old in state.samples:
            previous = old.features
            elapsed = current.stamp - old.stamp
            if (elapsed <= 0 or previous.torso_angle_deg > cfg.upright_torso_deg
                    or previous.horizontal or previous.compact_body or previous.near_floor
                    or previous.anchor_names != feature.anchor_names):
                continue
            tilt = feature.torso_angle_deg - previous.torso_angle_deg
            drop = (feature.torso_center_y - previous.torso_center_y) / previous.box_height
            speed = drop / elapsed
            height_loss = 1 - feature.box_height / previous.box_height
            shape_ratio = feature.box_aspect / previous.box_aspect
            rotated = tilt >= cfg.minimum_tilt_change_deg
            # Every sample in the interval must have stationary odometry before
            # treating image downward displacement as a descent cue.
            stationary = current.robot_motion == "stationary" and all(
                s.robot_motion == "stationary" for s in state.samples if s.stamp >= old.stamp
            )
            descent = (stationary and drop >= cfg.minimum_descent_body_lengths
                       and speed >= cfg.minimum_descent_speed)
            collapsed = height_loss >= cfg.minimum_height_loss and feature.horizontal
            shape_change = (feature.horizontal and shape_ratio >= cfg.moving_shape_ratio)
            if rotated and (descent or collapsed or shape_change):
                matches.append(dict(
                    startSec=old.stamp, endSec=current.stamp, elapsedSec=elapsed,
                    tiltChangeDeg=tilt, heightLossRatio=height_loss,
                    boxAspectRatioChange=shape_ratio,
                    descentBodyLengths=drop if stationary else None,
                    descentBodyLengthsPerSec=speed if stationary else None,
                    basis="descent_and_tilt" if descent else "shape_and_tilt",
                    motionCompensated=False, stationaryDuringWindow=stationary,
                ))
        return max(matches, key=lambda item: item["tiltChangeDeg"]) if matches else None

    def _candidate(self, state, track_id, kind, observation_id, start, sample,
                   reasons, transition, confidence_level):
        previous = state.candidate
        if previous and (previous["candidateKind"] == kind
                         or previous["candidateKind"] == "fall_suspected"):
            return None
        if previous:
            candidate_id = previous["candidateId"]
            revision = previous["revision"] + 1
            start = min(start, previous["evidenceStartSec"])
        else:
            candidate_id = f"{self._prefix}-candidate-{self._next_candidate}"
            self._next_candidate += 1
            revision = 1
        uncertainty = list(sample.features.uncertainties)
        if confidence_level == "weak":
            uncertainty.append("weak_pose_detection")
        if sample.robot_motion != "stationary":
            uncertainty.append("camera_motion_uncompensated")
        if ((sample.features.horizontal or sample.features.compact_body)
                and sample.features.floor_height_m is None):
            uncertainty.append("lying_surface_unknown")
        payload = dict(
            candidateId=candidate_id, revision=revision, source="yolo_pose",
            candidateKind=kind, targetTrackId=track_id, observationId=observation_id,
            evidenceStartSec=start, evidenceEndSec=sample.stamp,
            requiresVerification=True, reasons=reasons, uncertainties=uncertainty,
            evidence=dict(pose=asdict(sample.features), temporal=transition,
                          robotMotion=sample.robot_motion),
        )
        state.candidate = payload
        return payload
