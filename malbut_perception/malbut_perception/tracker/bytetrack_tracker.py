"""Small dependency-free ByteTrack-style two-stage IoU tracker."""

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from malbut_perception.detector.base import BoundingBox, ImageDetection

from .base import PersonTracker, TrackedDetection


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

    def predicted_box(self) -> BoundingBox:
        return self.bbox.translated(self.velocity_x, self.velocity_y)

    def observe(self, detection: ImageDetection) -> None:
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


def _greedy_iou_matches(
    tracks: Sequence[_Track],
    track_indices: Sequence[int],
    detections: Sequence[ImageDetection],
    detection_indices: Sequence[int],
    threshold: float,
) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    candidates = []
    for track_index in track_indices:
        predicted = tracks[track_index].predicted_box()
        for detection_index in detection_indices:
            overlap = predicted.iou(detections[detection_index].bbox)
            if overlap >= threshold:
                candidates.append((overlap, track_index, detection_index))
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


class ByteTrackTracker(PersonTracker):
    """Preserve IDs through low-confidence frames using two-stage matching."""

    def __init__(
        self,
        high_threshold: float = 0.50,
        low_threshold: float = 0.10,
        match_iou_threshold: float = 0.30,
        max_missed_frames: int = 15,
        min_confirmed_hits: int = 2,
    ) -> None:
        """Configure score gates, association, and track lifetime."""
        if not 0.0 <= low_threshold <= high_threshold <= 1.0:
            raise ValueError(
                'tracker thresholds must satisfy 0 <= low <= high <= 1'
            )
        if not 0.0 < match_iou_threshold <= 1.0:
            raise ValueError('match_iou_threshold must be in (0, 1]')
        if max_missed_frames < 0 or min_confirmed_hits < 1:
            raise ValueError('tracker frame counts are invalid')
        self._high_threshold = high_threshold
        self._low_threshold = low_threshold
        self._match_iou_threshold = match_iou_threshold
        self._max_missed_frames = max_missed_frames
        self._min_confirmed_hits = min_confirmed_hits
        self._tracks: List[_Track] = []
        self._next_track_id = 1

    def update(
        self,
        detections: List[ImageDetection],
    ) -> List[TrackedDetection]:
        """Run high- then low-score association for one image frame."""
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
        high_matches, unmatched_tracks, unmatched_high = _greedy_iou_matches(
            self._tracks,
            track_indices,
            detections,
            high,
            self._match_iou_threshold,
        )
        low_matches, unmatched_tracks, _ = _greedy_iou_matches(
            self._tracks,
            unmatched_tracks,
            detections,
            low,
            self._match_iou_threshold,
        )
        matches = high_matches + low_matches
        observed: Dict[int, int] = {}
        for track_index, detection_index in matches:
            self._tracks[track_index].observe(detections[detection_index])
            observed[track_index] = detection_index

        for track_index in unmatched_tracks:
            track = self._tracks[track_index]
            track.bbox = track.predicted_box()
            track.missed += 1

        for detection_index in unmatched_high:
            detection = detections[detection_index]
            self._tracks.append(
                _Track(
                    track_id=self._next_track_id,
                    bbox=detection.bbox,
                    score=detection.score,
                )
            )
            observed[len(self._tracks) - 1] = detection_index
            self._next_track_id += 1

        results = []
        for track_index, detection_index in observed.items():
            track = self._tracks[track_index]
            if track.hits >= self._min_confirmed_hits:
                results.append(
                    TrackedDetection(
                        track_id=track.track_id,
                        detection=detections[detection_index],
                        age=track.age,
                        hits=track.hits,
                    )
                )
        self._tracks = [
            track
            for track in self._tracks
            if track.missed <= self._max_missed_frames
        ]
        return sorted(results, key=lambda item: item.track_id)
