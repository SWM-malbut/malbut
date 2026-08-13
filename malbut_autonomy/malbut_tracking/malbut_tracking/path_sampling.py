"""Select a bounded tracking waypoint from a Nav2 global path."""

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Sequence

from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path

from .geometry import Point2D


@dataclass(frozen=True)
class PathWaypoint:
    """An interpolated path position with the local path-tangent heading."""

    position: Point2D
    yaw: float
    travelled_m: float


def sample_path_waypoint(
    poses: Sequence[PoseStamped],
    lookahead_m: float,
    final_yaw: float,
) -> PathWaypoint | None:
    """Return the point `lookahead_m` along a planned path."""
    if lookahead_m <= 0.0:
        raise ValueError('path lookahead must be positive')
    if not poses:
        return None
    points = [
        Point2D(float(pose.pose.position.x), float(pose.pose.position.y))
        for pose in poses
    ]
    if len(points) == 1:
        return PathWaypoint(points[0], final_yaw, 0.0)

    travelled = 0.0
    for index, (start, end) in enumerate(zip(points, points[1:])):
        segment = math.hypot(end.x - start.x, end.y - start.y)
        if segment <= 1e-9:
            continue
        if travelled + segment >= lookahead_m:
            remaining = lookahead_m - travelled
            ratio = max(0.0, min(1.0, remaining / segment))
            point = Point2D(
                start.x + ratio * (end.x - start.x),
                start.y + ratio * (end.y - start.y),
            )
            tangent_end = end
            if ratio >= 1.0 - 1e-6:
                for candidate in points[index + 2:]:
                    if math.hypot(
                        candidate.x - point.x,
                        candidate.y - point.y,
                    ) >= 0.15:
                        tangent_end = candidate
                        break
            if math.hypot(
                tangent_end.x - point.x,
                tangent_end.y - point.y,
            ) <= 1e-9:
                yaw = final_yaw
            else:
                yaw = math.atan2(
                    tangent_end.y - point.y,
                    tangent_end.x - point.x,
                )
            return PathWaypoint(point, yaw, lookahead_m)
        travelled += segment
    return PathWaypoint(points[-1], final_yaw, travelled)


def truncate_path(
    path: Path,
    lookahead_m: float,
) -> tuple[Path, PathWaypoint] | None:
    """
    Return a bounded copy of the planner path without changing its heading.

    Every planner-produced position is preserved. If the lookahead falls
    inside a segment, only the same interpolation already used for bounded
    tracking is appended. Camera control is deliberately handled downstream
    and cannot alter either the global path geometry or its orientation.
    """
    if not path.poses:
        return None
    orientation = path.poses[-1].pose.orientation
    final_yaw = math.atan2(
        2.0 * (
            orientation.w * orientation.z
            + orientation.x * orientation.y
        ),
        1.0 - 2.0 * (
            orientation.y * orientation.y
            + orientation.z * orientation.z
        ),
    )
    waypoint = sample_path_waypoint(path.poses, lookahead_m, final_yaw)
    if waypoint is None:
        return None
    output = Path()
    output.header = deepcopy(path.header)
    output.poses.append(deepcopy(path.poses[0]))
    travelled = 0.0
    for start, end in zip(path.poses, path.poses[1:]):
        start_point = Point2D(
            float(start.pose.position.x),
            float(start.pose.position.y),
        )
        end_point = Point2D(
            float(end.pose.position.x),
            float(end.pose.position.y),
        )
        segment = math.hypot(
            end_point.x - start_point.x,
            end_point.y - start_point.y,
        )
        if segment <= 1e-9:
            continue
        if travelled + segment < lookahead_m - 1e-9:
            output.poses.append(deepcopy(end))
            travelled += segment
            continue
        endpoint = deepcopy(end)
        remaining = max(0.0, lookahead_m - travelled)
        ratio = max(0.0, min(1.0, remaining / segment))
        endpoint.pose.position.x = (
            start_point.x + ratio * (end_point.x - start_point.x)
        )
        endpoint.pose.position.y = (
            start_point.y + ratio * (end_point.y - start_point.y)
        )
        output.poses.append(endpoint)
        break
    return output, PathWaypoint(
        waypoint.position,
        waypoint.yaw,
        waypoint.travelled_m,
    )
