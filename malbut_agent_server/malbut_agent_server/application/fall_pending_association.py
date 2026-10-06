"""Bounded scheduling hints for unassociated discoveries, NOT identity proof.

An old box may justify briefly waiting for late evidence, never a person merge,
answer reuse or risk clearance. Keep metadata only; no image retention here.
"""

from dataclasses import dataclass

from malbut_agent_server.application.fall_cloud_association import box_iou
from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, CloudDiscovery, IncidentState, SubjectCheckState, VideoAssessment,
)


@dataclass(frozen=True)
class PendingAssociation:
    discovery: CloudDiscovery
    epoch: int
    received_at: float
    candidate_id: str
    candidate_revision: int
    deadline: float


class AssociationWait:
    # Development bounds, not measured identity accuracy or a safety guarantee.
    max_wait_s = 20.0
    max_entries = 256

    def __init__(self):
        self.anchors = {}
        self.stationary_at = None
        self.pending = {}

    def clear(self):
        self.anchors.clear()
        self.stationary_at = None
        self.pending.clear()

    def observe(self, frame, incidents, evidence):
        if not frame.camera_stationary:
            self.anchors.clear()
            self.stationary_at = None
            return
        self.stationary_at = frame.observed_at
        for iid, incident in incidents.items():
            if incident.state is IncidentState.RESOLVED:
                self.anchors.pop(iid, None)
                continue
            latest = evidence.latest(incident.subject_key)
            if latest is None:
                continue  # Pose absence is not an incident lifecycle event.
            stamp, token, pose = latest
            if pose.state is SubjectCheckState.CLEAR:
                self.anchors.pop(iid, None)
            elif (token and token == incident.subject_association_token
                  and pose.box is not None):
                self.anchors[iid] = (incident.revision, stamp, pose.box)

    def candidate(self, finding, times, incidents, versions, *, now, policy):
        if (finding.kind is not CandidateKind.ALREADY_DOWN
                or finding.assessment is not VideoAssessment.SUSPECTED_FALL
                or len(finding.regions) < 2
                or any(r.frame_index >= len(times) for r in finding.regions)
                or len(self.pending) >= self.max_entries
                or self.stationary_at is None
                or now - self.stationary_at > policy.max_person_observation_age_s
                or any(i.subject_key is None and i.state is not IncidentState.RESOLVED
                       for i in incidents.values())):
            return None
        candidates = []
        for iid, (revision, stamp, box) in self.anchors.items():
            incident = incidents[iid]
            before = versions.get(iid)
            if (incident.state is IncidentState.RESOLVED or incident.subject_key is None
                    or incident.revision != revision or before is None
                    or before[1] != revision or before[2] is IncidentState.RESOLVED
                    or incident.video is None or incident.video.assessment not in {
                        VideoAssessment.OBSERVED_FALL, VideoAssessment.SUSPECTED_FALL}
                    or incident.question_id is None
                    or now - stamp > policy.scan_interval_s + policy.cloud_timeout_s):
                continue
            # Position is only a WAIT hint. Require every supplied region to
            # overlap; two overlapping candidate incidents are ambiguous.
            if all(box_iou(r.box, box) >= .60 for r in finding.regions):
                candidates.append(incident)
        return candidates[0] if len(candidates) == 1 else None

    def deadline(self, candidate, now, policy):
        # Repeated detections must not postpone the first discovery forever.
        prior = [p.deadline for p in self.pending.values()
                 if p.candidate_id == candidate.incident_id
                 and p.candidate_revision == candidate.revision]
        return min(prior) if prior else now + min(self.max_wait_s, policy.cloud_timeout_s)
