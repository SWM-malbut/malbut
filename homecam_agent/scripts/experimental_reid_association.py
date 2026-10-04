"""Offline ablation only: same-frame appearance, NOT track identity restoration.

No training, GT, remote service, gallery, token creation or production fallback.
The wider IoU control isolates threshold relaxation from adding appearance.
Thresholds are fixed trial settings, not calibrated identity probabilities.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import sys
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO/'malbut_agent_server'), str(REPO/'malbut_reid')]

from malbut_agent_server.application.fall_cloud_association import (
    SceneAssociation, associate_finding, box_iou,
)

ARMS = ('baseline', 'wider_iou', 'wider_iou_reid')


@dataclass(frozen=True)
class ReIDConfig:
    minimum_iou: float = .20
    iou_margin: float = .15
    minimum_cosine: float = .80
    cosine_margin: float = .10

    def __post_init__(self):
        for value in asdict(self).values():
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError('invalid experiment threshold')


def valid_box(box):
    return (box is not None and len(box) == 4
            and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box)
            and box[0] < box[2] and box[1] < box[3])


def cosine(a, b):
    """Unavailable/invalid features never become an arbitrary similarity."""
    import numpy as np
    if a is None or b is None:
        return None
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if (a.ndim != 1 or b.shape != a.shape or a.size == 0
            or not np.isfinite(a).all() or not np.isfinite(b).all()):
        return None
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if not math.isfinite(na * nb) or min(na, nb) <= 1e-12:
        return None
    return float(np.clip(np.dot(a / na, b / nb), -1, 1))


class AssociationExperiment:
    def __init__(self, arm, feature=None, config=ReIDConfig()):
        if arm not in ARMS or (arm == 'wider_iou_reid' and feature is None):
            raise ValueError('invalid experiment arm/feature source')
        self.arm, self.feature, self.config = arm, feature, config
        self.records = []

    def region(self, region, snapshot):
        index = region.frame_index
        info = dict(frame_index=index, cloud_box=region.box, candidates=[], selected=None)
        if type(index) is not int or not 0 <= index < len(snapshot):
            return dict(info, reason='invalid_sample_index')
        if not valid_box(region.box):
            return dict(info, reason='invalid_box')
        for key, token, pose in snapshot[index]:
            if pose.box is None:
                continue
            if not valid_box(pose.box):
                return dict(info, reason='invalid_box')
            info['candidates'].append(dict(subject_key=key, token=token, box=pose.box,
                usable=pose.association_usable, iou=box_iou(region.box, pose.box), cosine=None))
        ranked = sorted(info['candidates'], key=lambda c: c['iou'], reverse=True)
        gate = .60 if self.arm == 'baseline' else self.config.minimum_iou
        if self.arm == 'wider_iou_reid' and ranked:
            # Also measure outside-gate negatives for posthoc diagnostics. These
            # scores never make an outside-gate box eligible for assignment.
            query = self.feature(index, region.box)
            for item in ranked:
                item['cosine'] = cosine(query, self.feature(index, item['box']))
        if not ranked or ranked[0]['iou'] < gate:
            return dict(info, reason='no_matching_track')
        if self.arm == 'wider_iou_reid':
            # All geometrically competing boxes participate, even unusable ones.
            # Never remove a weak target then select the stronger helper by default.
            eligible = [c for c in ranked if c['iou'] >= gate]
            if any(c['cosine'] is None for c in eligible):
                return dict(info, reason='appearance_unavailable')
            ranked = sorted(eligible, key=lambda c: c['cosine'], reverse=True)
            if ranked[0]['cosine'] < self.config.minimum_cosine:
                return dict(info, reason='appearance_mismatch')
            if len(ranked) > 1 and ranked[0]['cosine'] - ranked[1]['cosine'] < self.config.cosine_margin:
                return dict(info, reason='ambiguous_appearance')
        else:
            margin = .15 if self.arm == 'baseline' else self.config.iou_margin
            if len(ranked) > 1 and ranked[0]['iou'] - ranked[1]['iou'] < margin:
                return dict(info, reason='ambiguous_tracks')
        info['selected'] = ranked[0]
        if ranked[0]['token'] is None or not ranked[0]['usable']:
            return dict(info, reason='track_unusable')
        return dict(info, reason='matched')

    def __call__(self, finding, snapshot):
        # Diagnose every returned region; accepting a subset would hide failures.
        regions = [self.region(r, snapshot) for r in finding.regions]
        if len(regions) < 2:
            result = SceneAssociation('insufficient_locations')
        elif any(r['reason'] != 'matched' for r in regions):
            result = SceneAssociation(next(r['reason'] for r in regions if r['reason'] != 'matched'))
        else:
            identities = {(r['selected']['subject_key'], r['selected']['token']) for r in regions}
            result = (SceneAssociation('matched', *next(iter(identities))) if len(identities) == 1
                      else SceneAssociation('track_changed'))
        if self.arm == 'baseline':
            # Valid production inputs must reproduce the actual frozen algorithm.
            reference = associate_finding(finding, snapshot)
            if result != reference:
                raise ValueError('baseline diagnostic disagrees with production')
            result = reference
        self.records.append(dict(association=asdict(result), regions=regions))
        return result

    def aligned_timed(self, finding, samples):
        """Adapt the aligned replay's exact samples to the monitor's timed hook.

        FrozenFeatures is indexed by the original Cloud RGB frame. Do not
        flatten neighboring Pose samples and accidentally extract appearance
        from a different frame, or discard one neighbor's identity evidence.
        Temporal-neighbor ablations need their own timestamp-bound RGB inputs.
        """
        if any(len(group) > 1 for group in samples):
            raise ValueError('offline appearance replay requires exact-frame Pose samples')
        snapshot = tuple(group[0][1] if group else () for group in samples)
        return self(finding, snapshot)


@contextmanager
def offline_associator(experiment):
    """Single-process replay injection; restored even on exception, no disk edit."""
    with patch('replay_reviewed_pose_cloud.associate_finding', experiment), patch(
            'malbut_agent_server.application.cloud_fall_monitor.associate_timed_finding',
            experiment.aligned_timed):
        yield
