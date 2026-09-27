"""Conservative experimental geometric association, never person recognition.

Thresholds below are implementation trial values, NOT validated accuracy claims.
Missing/ambiguous evidence is retained as an unidentified discovery by the caller.
"""

from dataclasses import dataclass
from typing import Optional

from malbut_agent_server.domain.fall_monitoring import CloudAssociationEvidence


@dataclass(frozen=True)
class SceneAssociation:
    reason: str
    subject_key: Optional[str] = None
    token: Optional[str] = None


def box_iou(a, b):
    area = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1]))
    union = ((a[2] - a[0]) * (a[3] - a[1])
             + (b[2] - b[0]) * (b[3] - b[1]) - area)
    return area / union if union else 0.0


def association_evidence(finding, snapshot):
    """Distinguish no usable counterpart observations from unverified identity.

    Count ONLY frozen dispatch samples. A helper's box can make evidence
    available; this never means Pose found the person in the Cloud finding.
    Missing Cloud locations use the whole request window for availability only.
    """
    if finding.regions:
        if any(r.frame_index >= len(snapshot) for r in finding.regions):
            return CloudAssociationEvidence('invalid_sample_reference', 0, 0, 0)
        selected = [snapshot[r.frame_index] for r in finding.regions]
        scope = 'finding_frames'
    else:
        selected, scope = snapshot, 'request_window'
    return CloudAssociationEvidence(
        scope, len(selected),
        sum(any(pose.box is not None for _, _, pose in sample) for sample in selected),
        sum(any(token is not None and pose.association_usable and pose.box is not None
                for _, token, pose in sample) for sample in selected))


def associate_finding(finding, snapshot):
    if len(finding.regions) < 2:
        return SceneAssociation('insufficient_locations')
    matched = set()
    for region in finding.regions:
        if region.frame_index >= len(snapshot):
            return SceneAssociation('invalid_sample_index')
        # Weak tracks can still be competing boxes: do not ignore them and
        # automatically attach a floor person to a stronger upright helper.
        ranked = sorted(((box_iou(region.box, pose.box), key, token)
                         for key, token, pose in snapshot[region.frame_index]
                         if pose.box is not None), reverse=True)
        if not ranked or ranked[0][0] < 0.60:
            return SceneAssociation('no_matching_track')
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.15:
            return SceneAssociation('ambiguous_tracks')
        _, key, token = ranked[0]
        if token is None:
            return SceneAssociation('track_unusable')
        matched.add((key, token))
    if len(matched) != 1:
        return SceneAssociation('track_changed')
    key, token = next(iter(matched))
    return SceneAssociation('matched', key, token)
