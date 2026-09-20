"""Short-lived, conservative image-space identities for person poses.

No face recognition, robot control, fall classification, or hidden pose
prediction. Ambiguous associations stay unassigned instead of merging people.
"""

from collections import deque
from dataclasses import dataclass, field
import math
from typing import Deque, Dict, Optional, Sequence, Tuple
from uuid import uuid4

from .pose import PersonPose, box_iou


@dataclass(frozen=True)
class PoseObservation:
    """One actual observation at the local monotonic receive time."""

    observed_at: float
    pose: PersonPose


@dataclass(frozen=True)
class TrackedPose:
    """An ID snapshot; missing/ambiguous tracks have no current pose."""

    track_id: str
    state: str
    pose: Optional[PersonPose]
    confidence_level: str
    observation_count: int
    consecutive_observations: int
    last_seen_age_sec: float
    history: Tuple[PoseObservation, ...]


@dataclass(frozen=True)
class UnassignedPose:
    """Keep uncertain/overflow evidence visible without inventing an ID."""

    pose: PersonPose
    reason: str


@dataclass(frozen=True)
class PoseTrackingResult:
    tracks: Tuple[TrackedPose, ...]
    unassigned: Tuple[UnassignedPose, ...]
    expired_track_ids: Tuple[str, ...]


@dataclass
class _Track:
    track_id: str
    pose: PersonPose
    last_seen: float
    observation_count: int = 0
    consecutive: int = 0
    strong_seen: bool = False
    history: Deque[PoseObservation] = field(
        default_factory=lambda: deque(maxlen=30)
    )


class PersonPoseTracker:
    """Associate boxes/keypoints one-to-one with an explicit ambiguity gate.

    IDs are local to this instance and never reused after OFF, expiry, or
    reset. Weak candidates can acquire tentative IDs without first passing
    the strong threshold. Repetition establishes continuity, not truth.
    """

    def __init__(
        self, *, strong_threshold: float = 0.45,
        candidate_threshold: float = 0.10, max_gap_sec: float = 1.0,
        min_observations: int = 3, max_people: int = 32,
    ) -> None:
        if not (math.isfinite(strong_threshold)
                and math.isfinite(candidate_threshold)
                and 0 < candidate_threshold <= strong_threshold <= 1):
            raise ValueError("pose thresholds must satisfy 0 < candidate <= strong <= 1")
        if not math.isfinite(max_gap_sec) or max_gap_sec <= 0:
            raise ValueError("max_gap_sec must be positive")
        if type(min_observations) is not int or min_observations < 2:
            raise ValueError("min_observations must be an integer >= 2")
        if type(max_people) is not int or not 1 <= max_people <= 128:
            raise ValueError("max_people must be an integer in [1, 128]")
        self.strong_threshold = strong_threshold
        self.candidate_threshold = candidate_threshold
        self.max_gap_sec = max_gap_sec
        self.min_observations = min_observations
        self.max_people = max_people
        self._prefix = uuid4().hex[:12]
        self._next_id = 1
        self._tracks: Dict[str, _Track] = {}
        self._last_update: Optional[float] = None

    def reset(self) -> Tuple[str, ...]:
        """Forget histories without recycling identities."""
        expired = tuple(self._tracks)
        self._tracks.clear()
        self._last_update = None
        return expired

    def update(self, poses: Sequence[PersonPose], now: float) -> PoseTrackingResult:
        """Advance once per sampled frame, even if no people were observed."""
        if not math.isfinite(now) or (
            self._last_update is not None and now <= self._last_update
        ):
            raise ValueError("pose tracking time must be finite and increasing")
        # Validate before changing state. Empty detections are not errors.
        for pose in poses:
            if (not math.isfinite(pose.box_confidence)
                    or not 0 <= pose.box_confidence <= 1
                    or len(pose.box) != 4
                    or not all(math.isfinite(v) and 0 <= v <= 1 for v in pose.box)
                    or pose.box[0] >= pose.box[2] or pose.box[1] >= pose.box[3]):
                raise ValueError("invalid pose tracking candidate")
            if any(not all(math.isfinite(v) and 0 <= v <= 1
                           for v in (point.x, point.y, point.confidence))
                   for point in pose.keypoints):
                raise ValueError("invalid pose tracking keypoint")
        self._last_update = now
        expired = tuple(key for key, track in self._tracks.items()
                        if now - track.last_seen > self.max_gap_sec)
        for key in expired:
            del self._tracks[key]
        candidates = sorted(
            (p for p in poses if p.box_confidence >= self.candidate_threshold),
            key=lambda p: (-p.box_confidence, p.box),
        )
        # Also protect direct callers against near-identical duplicate boxes.
        distinct = []
        for pose in candidates:
            if not any(box_iou(pose.box, p.box) >= 0.85 for p in distinct):
                distinct.append(pose)
        candidates = distinct
        old_tracks = list(self._tracks.values())
        costs = {(i, j): self._cost(track.pose, pose)
                 for i, track in enumerate(old_tracks)
                 for j, pose in enumerate(candidates)}
        eligible = {pair: cost for pair, cost in costs.items() if cost is not None}
        rows = {}
        columns = {}
        for (i, j), cost in eligible.items():
            rows.setdefault(i, []).append((cost, j))
            columns.setdefault(j, []).append((cost, i))
        for options in (*rows.values(), *columns.values()):
            options.sort()
        matches = []
        # An association must be uniquely best from BOTH directions. Near
        # ties and many-to-one merges do not inherit another person's history.
        for i, options in rows.items():
            cost, j = options[0]
            competing = columns[j]
            if competing[0][1] != i:
                continue
            if ((len(options) > 1 and options[1][0] - cost < 0.10)
                    or (len(competing) > 1 and competing[1][0] - cost < 0.10)):
                continue
            matches.append((i, j))
        matched_tracks = {i for i, _ in matches}
        matched_poses = {j for _, j in matches}
        observed = set()
        ambiguous = set()
        for i, j in matches:
            track = old_tracks[i]
            self._observe(track, candidates[j], now)
            observed.add(track.track_id)
        unassigned = []
        for j, pose in enumerate(candidates):
            if j in matched_poses:
                continue
            possible = [i for i in range(len(old_tracks)) if (i, j) in eligible]
            if possible:
                unassigned.append(UnassignedPose(pose, "association_ambiguous"))
                ambiguous.update(old_tracks[i].track_id for i in possible
                                 if i not in matched_tracks)
            elif len(self._tracks) >= self.max_people:
                unassigned.append(UnassignedPose(pose, "track_capacity"))
            else:
                key = f"{self._prefix}-{self._next_id}"
                self._next_id += 1
                track = _Track(key, pose, now)
                self._observe(track, pose, now)
                self._tracks[key] = track
                observed.add(key)
        snapshots = []
        for track in self._tracks.values():
            current = track.track_id in observed
            if not current:
                track.consecutive = 0
                state = "ambiguous" if track.track_id in ambiguous else "missing"
            else:
                state = ("tracked" if track.strong_seen
                         or track.consecutive >= self.min_observations
                         else "tentative")
            snapshots.append(TrackedPose(
                track_id=track.track_id, state=state,
                pose=track.pose if current else None,
                confidence_level=("strong" if track.pose.box_confidence >= self.strong_threshold
                                  else "weak"),
                observation_count=track.observation_count,
                consecutive_observations=track.consecutive,
                last_seen_age_sec=now - track.last_seen,
                history=tuple(track.history),
            ))
        return PoseTrackingResult(tuple(snapshots), tuple(unassigned), expired)

    def _observe(self, track: _Track, pose: PersonPose, now: float) -> None:
        track.pose = pose
        track.last_seen = now
        track.observation_count += 1
        track.consecutive += 1
        track.strong_seen |= pose.box_confidence >= self.strong_threshold
        track.history.append(PoseObservation(now, pose))

    @staticmethod
    def _cost(previous: PersonPose, current: PersonPose) -> Optional[float]:
        overlap = box_iou(previous.box, current.box)
        left, top, right, bottom = previous.box
        width, height = right - left, bottom - top
        other = current.box
        ratio = ((other[2] - other[0]) * (other[3] - other[1])) / (width * height)
        if not 0.2 <= ratio <= 5.0:
            return None
        scale = max(math.hypot(width, height), 0.01)
        displacement = math.hypot(
            (other[0] + other[2] - left - right) / 2,
            (other[1] + other[3] - top - bottom) / 2,
        ) / scale
        if overlap < 0.2 and displacement > 0.35:
            return None
        old_points = {p.name: p for p in previous.keypoints if p.confidence >= 0.5}
        distances = [math.hypot(p.x - old_points[p.name].x,
                                p.y - old_points[p.name].y) / scale
                     for p in current.keypoints
                     if p.confidence >= 0.5 and p.name in old_points]
        keypoint_cost = min(1.0, sum(distances) / len(distances)) if distances else 0.0
        return 1.0 - overlap + 0.25 * min(displacement, 1.0) + 0.25 * keypoint_cost
