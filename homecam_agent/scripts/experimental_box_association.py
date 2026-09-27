"""Offline ablation: box association without the fall-anatomy quality gate.

Not a ROS schema implementation and not imported by production. Strong-score,
current observation, tracked state, global ambiguity and continuity guards stay
unchanged. A usable box never supplies a CLEAR/normal posture observation.
"""
from dataclasses import dataclass
import math

from replay_reviewed_pose_cloud import require
from malbut_agent_server.domain.fall_monitoring import (
    SubjectCheckState, SubjectFrame, SubjectPose,
)


@dataclass(frozen=True)
class BoxAssociationConfig:
    strong_threshold: float = .45

    def __post_init__(self):
        require(type(self.strong_threshold) in (int, float)
                and math.isfinite(self.strong_threshold)
                and 0 < self.strong_threshold <= 1, 'invalid box score threshold')


def box_subject_frame(row, config=BoxAssociationConfig()):
    """Accept measured tracker records only; no GT, Cloud result or future data.

    Return a separate location-only SubjectFrame plus eligibility diagnostics.
    The original candidate payload, pose features and confidence are untouched.
    """
    payload = row['candidate_payload']
    require(payload['status'] == 'ok' and payload['subjectCheckVersion'] == 1,
            'unsupported source subject contract')
    require(payload['captureTimeSec'] == row['captured_at'], 'capture mismatch')
    tracks = {t['track_id']: t for t in row['tracks']}
    require(len(tracks) == len(row['tracks']), 'duplicate tracker ID')
    require(payload['unassignedCount'] == len(row['unassigned']), 'unassigned count mismatch')
    diagnostics = payload['tracks']
    require({t['targetTrackId'] for t in diagnostics} == set(tracks)
            and len(diagnostics) == len(tracks), 'tracker/subject ID mismatch')
    subjects, decisions = [], []
    for diagnostic in diagnostics:
        key = diagnostic['targetTrackId']
        track = tracks[key]
        require(track['state'] == diagnostic['trackingState'], 'tracker state mismatch')
        pose = track['pose']
        box = tuple(diagnostic['box']) if diagnostic['box'] is not None else None
        reasons = []
        score = None
        if pose is None:
            require(box is None, 'box invented for missing observation')
            reasons.append('not_observed')
        else:
            actual_box = tuple(pose['box'][k] for k in ('left', 'top', 'right', 'bottom'))
            require(box == actual_box, 'box differs from actual observation')
            score = pose['boxConfidence']
            require(type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1,
                    'invalid box score')
            require(track['confidence'] == ('strong' if score >= config.strong_threshold else 'weak'),
                    'confidence disagrees with unchanged threshold')
            if score < config.strong_threshold:
                reasons.append('weak_box')
        if track['state'] != 'tracked':
            reasons.append('track_not_confirmed')
        if row['unassigned']:
            reasons.append('unassigned_detections')
        usable = not reasons
        # Anatomy is deliberately not consulted for location eligibility.
        # UNKNOWN prevents these location-only observations clearing an incident.
        subjects.append(SubjectPose('pose:0:'+key, box, SubjectCheckState.UNKNOWN, usable))
        decisions.append(dict(subject_key='pose:0:'+key, box=box, usable=usable,
                              reasons=reasons, box_confidence=score, state=track['state'],
                              baseline_usable=diagnostic['associationUsable'],
                              feature_usable=(diagnostic['features'] or {}).get('usable'),
                              clearance='unknown'))
    return SubjectFrame(row['captured_at'], tuple(subjects), payload['subjectCheckMaxGapSec']), decisions
