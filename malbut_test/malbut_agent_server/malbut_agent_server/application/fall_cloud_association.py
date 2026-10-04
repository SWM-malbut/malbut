"""Conservative experimental geometric association, never person recognition.

Thresholds below are implementation trial values, NOT validated accuracy claims.
Missing/ambiguous evidence is retained as an unidentified discovery by the caller.
"""

from dataclasses import dataclass
from typing import Optional

from malbut_agent_server.domain.fall_monitoring import CloudAssociationEvidence, CloudPersonRegion


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

    Count ONLY supplied measured samples. A helper's box can make evidence
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
    return _associate_regions(finding.regions, snapshot)


def _associate_regions(regions, snapshot):
    if len(regions) < 2:
        return SceneAssociation('insufficient_locations')
    matched = set()
    for region in regions:
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


def supplement_samples(frozen, current):
    """Only fill observations not available at dispatch; never replace evidence."""
    if len(frozen) != len(current):
        raise ValueError('association snapshots must cover the same RGB frames')
    return tuple(old or new for old, new in zip(frozen, current))


def timed_association_evidence(finding, samples):
    # Availability is counted per Cloud image, not per neighboring Pose image.
    return association_evidence(finding, tuple(
        tuple(entry for _, observed in group for entry in observed) for group in samples))


def associate_timed_finding(finding, samples):
    """Every measured neighbor must independently identify the SAME token.

    Existing IoU and ambiguity guards apply to each neighbor, including weak
    competing people. We never invent/interpolate a box at the RGB timestamp.
    """
    if len(finding.regions) < 2:
        return SceneAssociation('insufficient_locations')
    expanded, regions = [], []
    for region in finding.regions:
        if region.frame_index >= len(samples):
            return SceneAssociation('invalid_sample_index')
        group = samples[region.frame_index]
        if not group:
            return SceneAssociation('no_matching_track')
        for _, observed in group:
            regions.append(CloudPersonRegion(len(expanded), region.box))
            expanded.append(observed)
    # Four Cloud regions may require eight measured neighbors. Keep that
    # internal expansion separate from the Cloud reply's four-region limit.
    return _associate_regions(regions, expanded)
