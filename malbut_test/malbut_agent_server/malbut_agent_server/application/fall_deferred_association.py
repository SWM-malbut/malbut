"""Local visual-track evidence for one Cloud discovery; not a person identifier.

The adapter must track the actual RGB object seeded by this discovery. This
boundary accepts measured boxes, not a caller-supplied Pose ID or 'verified'
flag. A lost visual segment cannot reacquire a different person under its seed.
No model, interpolation, Cloud call or automatic incident closure lives here.
"""

from dataclasses import dataclass
from typing import Optional

from malbut_agent_server.application.fall_cloud_association import box_iou
from malbut_agent_server.domain.fall_monitoring import CloudDiscovery, CloudPersonRegion, timestamp


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


@dataclass(frozen=True)
class DiscoveryLinkResult:
    reason: str
    incident_id: Optional[str] = None


class DiscoveryTrack:
    # Development gate, deliberately matching the offline pilot. These are not
    # a guarantee of identity through complete occlusion or crossing people.
    max_gap_s = .5
    min_iou = .60
    margin = .15
    confirmations = 3
    min_span_s = .5

    def __init__(self, discovery):
        first = discovery.finding.regions[0]
        self.seed_time = discovery.sample_times[first.frame_index]
        self.seed_box = first.box
        self.last_time = self.last_box = self.pending = None
        self.samples = 0
        self.broken = False
        self.anchors = {discovery.sample_times[r.frame_index]: r.box
                        for r in discovery.finding.regions}

    def step(self, observed_at, box, poses):
        timestamp(observed_at)
        if self.broken:
            return 'visual_track_broken', None
        if self.last_time is not None and observed_at <= self.last_time:
            raise ValueError('non-increasing visual observation')
        if box is not None:
            try:
                CloudPersonRegion(0, box)  # Same finite, normalized box contract.
            except ValueError:
                self.broken = True
                raise
        if self.last_time is None and observed_at != self.seed_time:
            raise ValueError('visual track must start at its Cloud seed')
        broken = box is None
        if self.last_time is not None:
            broken |= observed_at - self.last_time > self.max_gap_s + 1e-9
            if box is not None:
                ratio = max(_area(box), _area(self.last_box)) / min(_area(box), _area(self.last_box))
                broken |= box_iou(box, self.last_box) < .20 or ratio > 3
        if box is not None and observed_at in self.anchors:
            broken |= box_iou(box, self.anchors[observed_at]) < self.min_iou
        if broken:
            self.broken = True
            self.pending = None
            return 'visual_track_broken', None
        self.last_time, self.last_box = observed_at, box
        self.samples += 1
        # Weak/ambiguous boxes compete too. Dropping them could select a helper.
        ranked = sorted(((box_iou(box, p.box), key, token, p)
                         for key, token, p in poses if p.box is not None),
                        key=lambda item: item[0], reverse=True)
        reason = None
        if not ranked:
            reason = 'pose_evidence_missing'
        elif ranked[0][0] < self.min_iou:
            reason = 'no_matching_track'
        elif len(ranked) > 1 and ranked[0][0] - ranked[1][0] < self.margin:
            reason = 'ambiguous_tracks'
        elif not ranked[0][2] or not ranked[0][3].association_usable:
            reason = 'track_unusable'
        if reason:
            self.pending = None
            return reason, None
        key = ranked[0][1:3]
        if self.pending is None or self.pending[0] != key:
            self.pending = (key, observed_at, 1)
        else:
            self.pending = (key, self.pending[1], self.pending[2] + 1)
        if (self.pending[2] < self.confirmations
                or observed_at - self.pending[1] < self.min_span_s - 1e-9):
            return 'confirming_track', None
        return 'track_confirmed', key


@dataclass
class DeferredDiscovery:
    discovery: CloudDiscovery
    epoch: int
    received_at: float
    source_revision: int
    track: Optional[DiscoveryTrack] = None
    session_id: Optional[str] = None
