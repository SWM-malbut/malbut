"""Associate changing person detections by map-frame position continuity."""

from dataclasses import dataclass

from .geometry import Point2D, distance


@dataclass(frozen=True)
class TargetCandidate:
    """One confidence-filtered person observation in the global frame."""

    source_index: int
    position: Point2D
    confidence: float
    observed_track_id: str


def select_target_candidate(
    candidates: list[TargetCandidate],
    predicted_position: Point2D | None,
    maximum_distance_m: float,
    preferred_track_id: str = '',
) -> TargetCandidate | None:
    """Preserve a person by Re-ID first and spatial continuity second."""
    if maximum_distance_m <= 0.0:
        raise ValueError('maximum association distance must be positive')
    if not candidates:
        return None
    if preferred_track_id:
        same_identity = [
            candidate
            for candidate in candidates
            if candidate.observed_track_id == preferred_track_id
        ]
        if same_identity:
            if predicted_position is None:
                return max(
                    same_identity,
                    key=lambda candidate: candidate.confidence,
                )
            return min(
                same_identity,
                key=lambda candidate: (
                    distance(candidate.position, predicted_position),
                    -candidate.confidence,
                    candidate.source_index,
                ),
            )
    if predicted_position is None:
        return max(candidates, key=lambda candidate: candidate.confidence)

    nearest = min(
        candidates,
        key=lambda candidate: (
            distance(candidate.position, predicted_position),
            -candidate.confidence,
            candidate.source_index,
        ),
    )
    if distance(nearest.position, predicted_position) > maximum_distance_m:
        return None
    return nearest
