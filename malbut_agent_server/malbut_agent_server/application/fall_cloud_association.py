"""Conservative experimental geometric association, never person recognition.

Thresholds below are implementation trial values, NOT validated accuracy claims.
Missing/ambiguous evidence is retained as an unidentified discovery by the caller.
"""

from dataclasses import dataclass
from typing import Optional


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
