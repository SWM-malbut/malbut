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


def fuse_camera_bearing_with_lidar_range(
    robot_position: Point2D,
    camera_position: Point2D,
    lidar_position: Point2D,
    maximum_lateral_error_m: float,
    maximum_range_error_m: float,
) -> Point2D | None:
    """Fuse camera bearing with one geometrically consistent LiDAR range."""
    if maximum_lateral_error_m <= 0.0 or maximum_range_error_m <= 0.0:
        raise ValueError('camera-LiDAR fusion gates must be positive')
    camera_dx = camera_position.x - robot_position.x
    camera_dy = camera_position.y - robot_position.y
    lidar_dx = lidar_position.x - robot_position.x
    lidar_dy = lidar_position.y - robot_position.y
    camera_range = (camera_dx * camera_dx + camera_dy * camera_dy) ** 0.5
    lidar_range = (lidar_dx * lidar_dx + lidar_dy * lidar_dy) ** 0.5
    if camera_range <= 1e-6 or lidar_range <= 1e-6:
        return None
    direction_x = camera_dx / camera_range
    direction_y = camera_dy / camera_range
    forward_projection = lidar_dx * direction_x + lidar_dy * direction_y
    lateral_error = abs(lidar_dx * direction_y - lidar_dy * direction_x)
    if (
        forward_projection <= 0.0
        or lateral_error > maximum_lateral_error_m
        or abs(lidar_range - camera_range) > maximum_range_error_m
    ):
        return None
    return Point2D(
        robot_position.x + direction_x * lidar_range,
        robot_position.y + direction_y * lidar_range,
    )


def select_target_candidate(
    candidates: list[TargetCandidate],
    predicted_position: Point2D | None,
) -> TargetCandidate | None:
    """Choose the visible camera person by map-frame continuity."""
    if not candidates:
        return None
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
