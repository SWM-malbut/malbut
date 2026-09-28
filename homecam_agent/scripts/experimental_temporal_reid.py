"""Offline appearance ablation on the existing short-lived Pose tracker.

Only association costs change. Observations, confidence, confirmation, expiry,
one-to-one ambiguity gates, candidate generation and incident tokens do not.
The reference is the last *accepted real observation*, not a predicted crop,
future sample, VLM box or human annotation. No persistent identity gallery.
"""
from dataclasses import dataclass
import math

import numpy as np

from replay_reviewed_pose_cloud import require
from experimental_reid_association import cosine
from homecam_detector.pose import box_iou
from homecam_detector.pose_tracker import PersonPoseTracker


@dataclass(frozen=True)
class TemporalReIDConfig:
    minimum_cosine: float = .80
    appearance_weight: float = 1.0

    def __post_init__(self):
        require(type(self.minimum_cosine) in (int, float)
                and math.isfinite(self.minimum_cosine) and 0 < self.minimum_cosine <= 1,
                'invalid appearance threshold')
        require(type(self.appearance_weight) in (int, float)
                and math.isfinite(self.appearance_weight) and 0 < self.appearance_weight <= 2,
                'invalid appearance weight')


class TemporalReIDTracker(PersonPoseTracker):
    """Inherited tracker with explicit experimental cost and audited observations.

Missing crop: fall back for the WHOLE frame to baseline geometry. A missing
competitor feature must not remove that competitor or mix cost scales. This is
not evidence that geometry is safe; the baseline ambiguity guards still apply.
"""
    def __init__(self, feature_provider=None, *, use_reid=True,
                 appearance_config=TemporalReIDConfig(), **kwargs):
        super().__init__(**kwargs)
        require(type(use_reid) is bool and (not use_reid or callable(feature_provider)),
                'appearance provider required')
        self.feature_provider = feature_provider
        self.use_reid = use_reid
        self.appearance_config = appearance_config
        self._references = {}
        self._current = {}
        self._use_this_frame = False
        self.frame_diagnostic = None

    def reset(self):
        expired = super().reset()
        self._references.clear()
        self._current.clear()
        self._use_this_frame = False
        self.frame_diagnostic = None
        return expired

    def update(self, poses, now):
        require(math.isfinite(now) and (self._last_update is None or now > self._last_update),
                'pose tracking time must be finite and increasing')
        poses = tuple(poses)
        # Validate the same observation contract before consulting an encoder.
        # This temporary validator neither returns IDs nor provides tracking state.
        validator = PersonPoseTracker(strong_threshold=self.strong_threshold,
            candidate_threshold=self.candidate_threshold, max_gap_sec=self.max_gap_sec,
            min_observations=self.min_observations, max_people=self.max_people)
        validator.update(poses, now)
        candidates = []
        for p in sorted((p for p in poses if p.box_confidence >= self.candidate_threshold),
                        key=lambda p: (-p.box_confidence, p.box)):
            if not any(box_iou(p.box, q.box) >= .85 for q in candidates):
                candidates.append(p)
        current = {}
        for p in candidates:
            feature = self.feature_provider(p, now) if self.use_reid else None
            if feature is not None:
                require(cosine(feature, feature) is not None, 'invalid appearance vector')
                feature = np.array(feature, dtype=np.float32, copy=True)
                feature.setflags(write=False)
            current[id(p)] = dict(feature=feature, observed_at=now, box=p.box)
        missing_pairs = 0
        for track in self._tracks.values():
            if now - track.last_seen > self.max_gap_sec:
                continue
            for p in candidates:
                if PersonPoseTracker._cost(track.pose, p) is None:
                    continue
                ref = self._references.get(id(track.pose))
                if (ref is None or ref['feature'] is None or current[id(p)]['feature'] is None):
                    missing_pairs += 1
        previous_transient = self._current, self._use_this_frame, self.frame_diagnostic
        self._current = current
        self._use_this_frame = self.use_reid and missing_pairs == 0
        self.frame_diagnostic = dict(observed_at=now, reid_enabled=self._use_this_frame,
            missing_feature_pairs=missing_pairs if self.use_reid else 0,
            fallback_reason='missing_competing_feature' if self.use_reid and missing_pairs else None,
            pair_costs=[], accepted=[], births=[])
        try:
            result = super().update(poses, now)
        except Exception:
            self._current, self._use_this_frame, self.frame_diagnostic = previous_transient
            raise
        # Keep one feature per retained track. Missing tracks keep their actual
        # old observation, never its box labelled with the current timestamp.
        previous = self._references
        self._references = {id(t.pose): (current[id(t.pose)] if t.last_seen == now
                                       else previous[id(t.pose)]) for t in self._tracks.values()}
        self._current = {}
        return result

    def _cost(self, previous, current):
        geometry = PersonPoseTracker._cost(previous, current)
        ref = self._references[id(previous)]
        sample = self._current[id(current)]
        require(ref['observed_at'] < sample['observed_at'], 'non-past appearance reference')
        score = cosine(ref['feature'], sample['feature']) if self.use_reid else None
        if geometry is None:
            final, reason = None, 'geometry_gate'
        elif not self._use_this_frame:
            final, reason = geometry, 'baseline_geometry'
        elif score < self.appearance_config.minimum_cosine:
            final, reason = None, 'appearance_gate'
        else:
            final = geometry + self.appearance_config.appearance_weight * (1-score)
            reason = 'geometry_and_appearance'
        self.frame_diagnostic['pair_costs'].append(dict(previous_at=ref['observed_at'],
            observed_at=sample['observed_at'], previous_box=previous.box, current_box=current.box,
            geometric_cost=geometry, cosine=score, final_cost=final, reason=reason))
        return final

    def _observe(self, track, pose, now):
        prior = self._references.get(id(track.pose)) if track.observation_count else None
        if prior is not None:
            self.frame_diagnostic['accepted'].append(dict(track_id=track.track_id,
                previous_at=track.last_seen, observed_at=now, previous_box=track.pose.box,
                current_box=pose.box, gap_s=now-track.last_seen,
                cosine=cosine(prior['feature'], self._current[id(pose)]['feature']) if self.use_reid else None,
                used_reid=self._use_this_frame))
        else:
            self.frame_diagnostic['births'].append(dict(track_id=track.track_id, box=pose.box))
        super()._observe(track, pose, now)
