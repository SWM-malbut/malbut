"""Select the camera-observed person without rejecting visible detections."""

from dataclasses import dataclass

from .geometry import Point2D, distance


@dataclass(frozen=True)
class TargetCandidate:
    """One confidence-filtered person observation in the global frame."""

    source_index: int
    position: Point2D
    confidence: float
    observed_track_id: str


class CameraObservationGate:
    """Require temporal or LiDAR support before accepting a large jump."""

    def __init__(
        self,
        confirmation_hits: int,
        pending_consistency_m: float,
    ) -> None:
        if confirmation_hits < 2:
            raise ValueError('camera jump confirmation needs at least 2 hits')
        if pending_consistency_m <= 0.0:
            raise ValueError('pending consistency distance must be positive')
        self._confirmation_hits = confirmation_hits
        self._pending_consistency_m = pending_consistency_m
        self.reset()

    def reset(self) -> None:
        """Forget a partially observed discontinuous camera candidate."""
        self._pending_position: Point2D | None = None
        self._pending_hits = 0

    def accept(
        self,
        observed_position: Point2D,
        predicted_position: Point2D | None,
        continuity_radius_m: float,
        lidar_supported: bool,
    ) -> bool:
        """Accept continuity immediately; otherwise require repeated support."""
        if continuity_radius_m <= 0.0:
            raise ValueError('camera continuity radius must be positive')
        if (
            predicted_position is None
            or distance(observed_position, predicted_position)
            <= continuity_radius_m
            or lidar_supported
        ):
            self.reset()
            return True
        if (
            self._pending_position is not None
            and distance(observed_position, self._pending_position)
            <= self._pending_consistency_m
        ):
            self._pending_hits += 1
        else:
            self._pending_hits = 1
        self._pending_position = observed_position
        if self._pending_hits < self._confirmation_hits:
            return False
        self.reset()
        return True


def select_target_candidate(
    candidates: list[TargetCandidate],
    predicted_position: Point2D | None,
    preferred_track_id: str = '',
) -> TargetCandidate | None:
    """Prefer Re-ID, then choose the visible camera person by continuity."""
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
    return nearest
