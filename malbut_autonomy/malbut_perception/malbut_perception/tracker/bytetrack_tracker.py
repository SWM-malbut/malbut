"""ByteTrack-style association with Deep SORT appearance re-identification."""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

from malbut_perception.detector.base import BoundingBox, ImageDetection
from malbut_perception.reid.base import AppearanceFeature, normalized_feature

from .base import PersonTracker, TrackedDetection


def _cosine_distance(first: np.ndarray, second: np.ndarray) -> float:
    """Return bounded cosine distance for normalized descriptors."""
    similarity = float(np.dot(first, second))
    return 1.0 - max(-1.0, min(1.0, similarity))


@dataclass
class _Track:
    track_id: int
    bbox: BoundingBox
    score: float
    age: int = 1
    hits: int = 1
    missed: int = 0
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    features: List[np.ndarray] = field(default_factory=list)
    retired_frame: Optional[int] = None

    def predicted_box(self) -> BoundingBox:
        return self.bbox.translated(self.velocity_x, self.velocity_y)

    def appearance_distance(self, feature: AppearanceFeature) -> Optional[float]:
        """Compare a descriptor to the nearest item in this track's gallery."""
        if feature is None or not self.features:
            return None
        return min(_cosine_distance(stored, feature) for stored in self.features)

    def _append_feature(
        self,
        feature: AppearanceFeature,
        feature_budget: int,
    ) -> None:
        if feature is None:
            return
        self.features.append(feature)
        if len(self.features) > feature_budget:
            self.features = self.features[-feature_budget:]

    def observe(
        self,
        detection: ImageDetection,
        feature: AppearanceFeature,
        feature_budget: int,
    ) -> None:
        old_x, old_y = self.bbox.center
        new_x, new_y = detection.bbox.center
        measured_x = new_x - old_x
        measured_y = new_y - old_y
        self.velocity_x = 0.65 * self.velocity_x + 0.35 * measured_x
        self.velocity_y = 0.65 * self.velocity_y + 0.35 * measured_y
        self.bbox = detection.bbox
        self.score = detection.score
        self.hits += 1
        self.missed = 0
        self.retired_frame = None
        self._append_feature(feature, feature_budget)

    def restore(
        self,
        detection: ImageDetection,
        feature: AppearanceFeature,
        feature_budget: int,
        inactive_frames: int,
    ) -> None:
        """Restore a retired identity without carrying stale image velocity."""
        self.age += max(1, inactive_frames)
        self.bbox = detection.bbox
        self.score = detection.score
        self.hits += 1
        self.missed = 0
        self.velocity_x = 0.0
        self.velocity_y = 0.0
        self.retired_frame = None
        self._append_feature(feature, feature_budget)


def _association_quality(
    track: _Track,
    detection: ImageDetection,
    feature: AppearanceFeature,
    iou_threshold: float,
    appearance_threshold: float,
    appearance_weight: float,
) -> Optional[float]:
    predicted = track.predicted_box()
    overlap = predicted.iou(detection.bbox)
    distance = track.appearance_distance(feature)
    appearance_matches = (
        distance is not None and distance <= appearance_threshold
    )
    if overlap < iou_threshold and not appearance_matches:
        return None
    if distance is None:
        return overlap
    similarity = max(0.0, 1.0 - distance)
    return appearance_weight * similarity + (1.0 - appearance_weight) * overlap


def _greedy_matches(
    tracks: Sequence[_Track],
    track_indices: Sequence[int],
    detections: Sequence[ImageDetection],
    detection_indices: Sequence[int],
    features: Sequence[AppearanceFeature],
    iou_threshold: float,
    appearance_threshold: float,
    appearance_weight: float,
) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    candidates = []
    for track_index in track_indices:
        for detection_index in detection_indices:
            quality = _association_quality(
                tracks[track_index],
                detections[detection_index],
                features[detection_index],
                iou_threshold,
                appearance_threshold,
                appearance_weight,
            )
            if quality is not None:
                candidates.append((quality, track_index, detection_index))
    candidates.sort(reverse=True)
    used_tracks = set()
    used_detections = set()
    matches = []
    for _, track_index, detection_index in candidates:
        if track_index in used_tracks or detection_index in used_detections:
            continue
        used_tracks.add(track_index)
        used_detections.add(detection_index)
        matches.append((track_index, detection_index))
    return (
        matches,
        [index for index in track_indices if index not in used_tracks],
        [index for index in detection_indices if index not in used_detections],
    )


def _reidentification_matches(
    retired: Sequence[_Track],
    detections: Sequence[ImageDetection],
    detection_indices: Sequence[int],
    features: Sequence[AppearanceFeature],
    threshold: float,
) -> Tuple[List[Tuple[int, int]], List[int]]:
    """Match unmatched detections to inactive appearance galleries."""
    candidates = []
    for retired_index, track in enumerate(retired):
        for detection_index in detection_indices:
            distance = track.appearance_distance(features[detection_index])
            if distance is not None and distance <= threshold:
                candidates.append((distance, retired_index, detection_index))
    candidates.sort()
    used_tracks = set()
    used_detections = set()
    matches = []
    for _, retired_index, detection_index in candidates:
        if retired_index in used_tracks or detection_index in used_detections:
            continue
        used_tracks.add(retired_index)
        used_detections.add(detection_index)
        matches.append((retired_index, detection_index))
    unmatched = [
        index for index in detection_indices if index not in used_detections
    ]
    return matches, unmatched


class ByteTrackTracker(PersonTracker):
    """Associate boxes short-term and restore long-term IDs by appearance."""

    def __init__(
        self,
        high_threshold: float = 0.50,
        low_threshold: float = 0.10,
        match_iou_threshold: float = 0.30,
        max_missed_frames: int = 15,
        min_confirmed_hits: int = 2,
        appearance_threshold: float = 0.30,
        appearance_weight: float = 0.65,
        reid_threshold: float = 0.25,
        reid_max_inactive_frames: int = 0,
        feature_budget: int = 30,
    ) -> None:
        """Configure association and the bounded appearance gallery."""
        if not 0.0 <= low_threshold <= high_threshold <= 1.0:
            raise ValueError(
                'tracker thresholds must satisfy 0 <= low <= high <= 1'
            )
        if not 0.0 < match_iou_threshold <= 1.0:
            raise ValueError('match_iou_threshold must be in (0, 1]')
        if max_missed_frames < 0 or min_confirmed_hits < 1:
            raise ValueError('tracker frame counts are invalid')
        for name, value in (
            ('appearance_threshold', appearance_threshold),
            ('reid_threshold', reid_threshold),
        ):
            if not 0.0 <= value <= 2.0:
                raise ValueError(f'{name} must be in [0, 2]')
        if not 0.0 <= appearance_weight <= 1.0:
            raise ValueError('appearance_weight must be in [0, 1]')
        if reid_max_inactive_frames < 0 or feature_budget < 1:
            raise ValueError('Re-ID gallery limits are invalid')
        self._high_threshold = high_threshold
        self._low_threshold = low_threshold
        self._match_iou_threshold = match_iou_threshold
        self._max_missed_frames = max_missed_frames
        self._min_confirmed_hits = min_confirmed_hits
        self._appearance_threshold = appearance_threshold
        self._appearance_weight = appearance_weight
        self._reid_threshold = reid_threshold
        self._reid_max_inactive_frames = reid_max_inactive_frames
        self._feature_budget = feature_budget
        self._tracks: List[_Track] = []
        self._retired: List[_Track] = []
        self._next_track_id = 1
        self._frame_index = 0

    @staticmethod
    def _prepare_features(
        detections: Sequence[ImageDetection],
        features: Optional[Sequence[AppearanceFeature]],
    ) -> List[AppearanceFeature]:
        if features is None:
            return [None] * len(detections)
        if len(features) != len(detections):
            raise ValueError(
                'appearance feature count must match detection count'
            )
        prepared: List[AppearanceFeature] = []
        for feature in features:
            prepared.append(
                None if feature is None else normalized_feature(feature)
            )
        return prepared

    def _prune_retired(self) -> None:
        if self._reid_max_inactive_frames == 0:
            # Zero means process-lifetime identity memory. A restarted node
            # still starts from a clean gallery, but a temporarily absent
            # person never loses their ID merely because time elapsed.
            return
        self._retired = [
            track
            for track in self._retired
            if track.retired_frame is not None
            and self._frame_index - track.retired_frame
            <= self._reid_max_inactive_frames
        ]

    def needs_appearance_features(
        self,
        detections: Sequence[ImageDetection],
    ) -> bool:
        """Return whether geometry alone cannot safely associate a person."""
        high = [
            index
            for index, detection in enumerate(detections)
            if detection.score >= self._high_threshold
        ]
        if not high:
            return False
        if not self._tracks:
            return True

        features: List[AppearanceFeature] = [None] * len(detections)
        _, _, unmatched_high = _greedy_matches(
            self._tracks,
            list(range(len(self._tracks))),
            detections,
            high,
            features,
            self._match_iou_threshold,
            self._appearance_threshold,
            self._appearance_weight,
        )
        return bool(unmatched_high)

    def update(
        self,
        detections: List[ImageDetection],
        appearance_features: Optional[Sequence[AppearanceFeature]] = None,
    ) -> List[TrackedDetection]:
        """Associate one frame and restore recently retired visual IDs."""
        self._frame_index += 1
        self._prune_retired()
        features = self._prepare_features(detections, appearance_features)
        for track in self._tracks:
            track.age += 1

        high = [
            index
            for index, detection in enumerate(detections)
            if detection.score >= self._high_threshold
        ]
        low = [
            index
            for index, detection in enumerate(detections)
            if self._low_threshold <= detection.score < self._high_threshold
        ]
        track_indices = list(range(len(self._tracks)))
        high_matches, unmatched_tracks, unmatched_high = _greedy_matches(
            self._tracks,
            track_indices,
            detections,
            high,
            features,
            self._match_iou_threshold,
            self._appearance_threshold,
            self._appearance_weight,
        )
        low_matches, unmatched_tracks, _ = _greedy_matches(
            self._tracks,
            unmatched_tracks,
            detections,
            low,
            features,
            self._match_iou_threshold,
            self._appearance_threshold,
            self._appearance_weight,
        )

        observed: List[Tuple[_Track, int]] = []
        for track_index, detection_index in high_matches + low_matches:
            track = self._tracks[track_index]
            track.observe(
                detections[detection_index],
                features[detection_index],
                self._feature_budget,
            )
            observed.append((track, detection_index))

        for track_index in unmatched_tracks:
            track = self._tracks[track_index]
            track.bbox = track.predicted_box()
            track.missed += 1

        reid_matches, unmatched_high = _reidentification_matches(
            self._retired,
            detections,
            unmatched_high,
            features,
            self._reid_threshold,
        )
        matched_retired = set()
        for retired_index, detection_index in reid_matches:
            track = self._retired[retired_index]
            inactive_frames = self._frame_index - int(track.retired_frame)
            track.restore(
                detections[detection_index],
                features[detection_index],
                self._feature_budget,
                inactive_frames,
            )
            self._tracks.append(track)
            observed.append((track, detection_index))
            matched_retired.add(retired_index)
        self._retired = [
            track
            for index, track in enumerate(self._retired)
            if index not in matched_retired
        ]

        for detection_index in unmatched_high:
            detection = detections[detection_index]
            track = _Track(
                track_id=self._next_track_id,
                bbox=detection.bbox,
                score=detection.score,
            )
            track._append_feature(
                features[detection_index], self._feature_budget
            )
            self._tracks.append(track)
            observed.append((track, detection_index))
            self._next_track_id += 1

        results = []
        for track, detection_index in observed:
            if track.hits >= self._min_confirmed_hits:
                results.append(
                    TrackedDetection(
                        track_id=track.track_id,
                        detection=detections[detection_index],
                        age=track.age,
                        hits=track.hits,
                    )
                )

        active = []
        for track in self._tracks:
            if track.missed <= self._max_missed_frames:
                active.append(track)
                continue
            if (
                track.hits >= self._min_confirmed_hits
                and track.features
            ):
                track.retired_frame = self._frame_index
                self._retired.append(track)
        self._tracks = active
        return sorted(results, key=lambda item: item.track_id)
