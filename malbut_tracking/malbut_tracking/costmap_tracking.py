"""Track dynamic global-costmap obstacles with stable object identities."""

from dataclasses import dataclass
import math
from typing import Sequence

import numpy as np

from .geometry import Point2D, distance


# nav2_costmap_2d/cost_values.hpp. Unknown space is not an obstacle
# measurement and must never enter dynamic-object clustering.
NAV2_NO_INFORMATION = 255


@dataclass(frozen=True)
class CostmapGrid:
    """ROS-independent view of one Nav2 costmap or occupancy grid."""

    frame_id: str
    stamp_seconds: float
    resolution: float
    width: int
    height: int
    origin: Point2D
    origin_yaw: float
    costs: Sequence[int]

    def validate(self) -> None:
        """Reject malformed grids before indexing their cost data."""
        if not self.frame_id:
            raise ValueError('costmap frame_id is required')
        if self.resolution <= 0.0:
            raise ValueError('costmap resolution must be positive')
        if self.width <= 0 or self.height <= 0:
            raise ValueError('costmap dimensions must be positive')
        if len(self.costs) != self.width * self.height:
            raise ValueError('costmap data length does not match dimensions')

    def world_to_cell(self, point: Point2D) -> tuple[int, int] | None:
        """Convert a world point into the costmap's possibly rotated grid."""
        offset_x = point.x - self.origin.x
        offset_y = point.y - self.origin.y
        cosine = math.cos(self.origin_yaw)
        sine = math.sin(self.origin_yaw)
        local_x = cosine * offset_x + sine * offset_y
        local_y = -sine * offset_x + cosine * offset_y
        cell_x = math.floor(local_x / self.resolution)
        cell_y = math.floor(local_y / self.resolution)
        if not 0 <= cell_x < self.width or not 0 <= cell_y < self.height:
            return None
        return cell_x, cell_y

    def cell_center(self, cell_x: int, cell_y: int) -> Point2D:
        """Return the world position at the center of one costmap cell."""
        local_x = (cell_x + 0.5) * self.resolution
        local_y = (cell_y + 0.5) * self.resolution
        cosine = math.cos(self.origin_yaw)
        sine = math.sin(self.origin_yaw)
        return Point2D(
            self.origin.x + cosine * local_x - sine * local_y,
            self.origin.y + sine * local_x + cosine * local_y,
        )

    def cost(self, cell_x: int, cell_y: int) -> int:
        """Return the numeric cost stored at one valid cell."""
        return int(self.costs[cell_y * self.width + cell_x])


@dataclass(frozen=True)
class ObstacleCluster:
    """One compact dynamic obstacle measurement extracted from a grid."""

    position: Point2D
    cell_count: int
    extent_m: float


@dataclass(frozen=True)
class TrackedObstacle:
    """Public snapshot of one constant-velocity costmap track."""

    track_id: int
    position: Point2D
    velocity: Point2D
    cell_count: int
    extent_m: float
    hits: int
    misses: int
    confirmed: bool
    stamp_seconds: float


@dataclass(frozen=True)
class LabeledObstacle:
    """A stable semantic person label attached to a costmap track."""

    label: str
    track: TrackedObstacle
    detector_track_id: str

    @property
    def cluster(self) -> ObstacleCluster:
        """Expose the latest obstacle measurement for follow policy code."""
        return ObstacleCluster(
            self.track.position,
            self.track.cell_count,
            self.track.extent_m,
        )

    @property
    def stamp_seconds(self) -> float:
        """Return the costmap time represented by this labeled track."""
        return self.track.stamp_seconds


class _KalmanTrack:
    """Internal planar constant-velocity linear Kalman filter."""

    def __init__(
        self,
        track_id: int,
        measurement: ObstacleCluster,
        stamp_seconds: float,
        measurement_variance: float,
        confirmation_hits: int,
    ) -> None:
        self.track_id = track_id
        self.state = np.array(
            [measurement.position.x, measurement.position.y, 0.0, 0.0],
            dtype=float,
        )
        self.covariance = np.diag(
            [measurement_variance, measurement_variance, 1.0, 1.0]
        )
        self.stamp_seconds = stamp_seconds
        self.last_measurement_seconds = stamp_seconds
        self.cell_count = measurement.cell_count
        self.extent_m = measurement.extent_m
        self.hits = 1
        self.misses = 0
        self.confirmed = confirmation_hits <= 1

    def predict(self, stamp_seconds: float, process_variance: float) -> None:
        """Predict state and covariance at one monotonically newer time."""
        elapsed = max(0.0, stamp_seconds - self.stamp_seconds)
        if elapsed <= 0.0:
            return
        transition = np.array(
            [
                [1.0, 0.0, elapsed, 0.0],
                [0.0, 1.0, 0.0, elapsed],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )
        elapsed_2 = elapsed * elapsed
        elapsed_3 = elapsed_2 * elapsed
        elapsed_4 = elapsed_2 * elapsed_2
        noise = process_variance * np.array(
            [
                [elapsed_4 / 4.0, 0.0, elapsed_3 / 2.0, 0.0],
                [0.0, elapsed_4 / 4.0, 0.0, elapsed_3 / 2.0],
                [elapsed_3 / 2.0, 0.0, elapsed_2, 0.0],
                [0.0, elapsed_3 / 2.0, 0.0, elapsed_2],
            ]
        )
        self.state = transition @ self.state
        self.covariance = (
            transition @ self.covariance @ transition.T + noise
        )
        self.stamp_seconds = stamp_seconds

    def squared_mahalanobis(
        self,
        measurement: ObstacleCluster,
        measurement_variance: float,
    ) -> float:
        """Return statistically normalized innovation distance in 2-D."""
        observation = np.array(
            [measurement.position.x, measurement.position.y], dtype=float
        )
        innovation = observation - self.state[:2]
        innovation_covariance = (
            self.covariance[:2, :2]
            + np.eye(2, dtype=float) * measurement_variance
        )
        try:
            solved = np.linalg.solve(innovation_covariance, innovation)
        except np.linalg.LinAlgError:
            return math.inf
        return float(innovation.T @ solved)

    def correct(
        self,
        measurement: ObstacleCluster,
        measurement_variance: float,
        confirmation_hits: int,
    ) -> None:
        """Correct the predicted state with one globally assigned cluster."""
        observation_matrix = np.array(
            [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        )
        measurement_covariance = (
            np.eye(2, dtype=float) * measurement_variance
        )
        measurement_vector = np.array(
            [measurement.position.x, measurement.position.y], dtype=float
        )
        innovation = measurement_vector - observation_matrix @ self.state
        innovation_covariance = (
            observation_matrix
            @ self.covariance
            @ observation_matrix.T
            + measurement_covariance
        )
        gain = (
            self.covariance
            @ observation_matrix.T
            @ np.linalg.inv(innovation_covariance)
        )
        self.state = self.state + gain @ innovation
        identity = np.eye(4, dtype=float)
        residual = identity - gain @ observation_matrix
        self.covariance = (
            residual @ self.covariance @ residual.T
            + gain @ measurement_covariance @ gain.T
        )
        self.last_measurement_seconds = self.stamp_seconds
        self.cell_count = measurement.cell_count
        self.extent_m = measurement.extent_m
        self.hits += 1
        self.misses = 0
        self.confirmed = self.hits >= confirmation_hits

    def miss(self) -> None:
        """Record one grid update with no admissible measurement."""
        self.misses += 1

    def snapshot(self) -> TrackedObstacle:
        """Return an immutable view for the ROS-facing policy."""
        return TrackedObstacle(
            track_id=self.track_id,
            position=Point2D(float(self.state[0]), float(self.state[1])),
            velocity=Point2D(float(self.state[2]), float(self.state[3])),
            cell_count=self.cell_count,
            extent_m=self.extent_m,
            hits=self.hits,
            misses=self.misses,
            confirmed=self.confirmed,
            stamp_seconds=self.stamp_seconds,
        )


def find_obstacle_clusters(
    grid: CostmapGrid,
    obstacle_cost_threshold: int,
    cluster_radius_m: float,
    minimum_cluster_cells: int,
    maximum_cluster_cells: int,
    maximum_cluster_extent_m: float,
    static_map: CostmapGrid | None = None,
    static_occupied_threshold: int = 65,
    static_exclusion_radius_m: float = 0.10,
) -> list[ObstacleCluster]:
    """Extract compact non-static obstacles from the complete costmap."""
    grid.validate()
    if static_map is not None:
        static_map.validate()
    _validate_cluster_parameters(
        obstacle_cost_threshold,
        cluster_radius_m,
        minimum_cluster_cells,
        maximum_cluster_cells,
        maximum_cluster_extent_m,
        static_occupied_threshold,
        static_exclusion_radius_m,
    )
    dynamic_cells = set()
    for index, raw_cost in enumerate(grid.costs):
        cost = int(raw_cost)
        if cost == NAV2_NO_INFORMATION or cost < obstacle_cost_threshold:
            continue
        cell_x = index % grid.width
        cell_y = index // grid.width
        if not _is_static_obstacle(
            static_map,
            grid.cell_center(cell_x, cell_y),
            static_occupied_threshold,
            static_exclusion_radius_m,
        ):
            dynamic_cells.add((cell_x, cell_y))
    neighbor_radius = max(1, math.ceil(cluster_radius_m / grid.resolution))
    clusters = []
    while dynamic_cells:
        seed = dynamic_cells.pop()
        component = [seed]
        pending = [seed]
        while pending:
            cell_x, cell_y = pending.pop()
            for offset_y in range(-neighbor_radius, neighbor_radius + 1):
                for offset_x in range(-neighbor_radius, neighbor_radius + 1):
                    if offset_x == 0 and offset_y == 0:
                        continue
                    if math.hypot(offset_x, offset_y) > neighbor_radius:
                        continue
                    neighbor = cell_x + offset_x, cell_y + offset_y
                    if neighbor in dynamic_cells:
                        dynamic_cells.remove(neighbor)
                        component.append(neighbor)
                        pending.append(neighbor)
        cluster = _make_cluster(
            grid,
            component,
            minimum_cluster_cells,
            maximum_cluster_cells,
            maximum_cluster_extent_m,
        )
        if cluster is not None:
            clusters.append(cluster)
    clusters.sort(key=lambda cluster: (cluster.position.x, cluster.position.y))
    return clusters


class CostmapTargetTracker:
    """Track all dynamic obstacles and attach one persistent person label."""

    def __init__(
        self,
        cluster_radius_m: float,
        obstacle_cost_threshold: int,
        minimum_cluster_cells: int,
        maximum_cluster_cells: int,
        maximum_cluster_extent_m: float,
        static_occupied_threshold: int = 65,
        static_exclusion_radius_m: float = 0.10,
        process_variance: float = 1.0,
        measurement_variance: float = 0.04,
        mahalanobis_gate: float = 9.21,
        confirmation_hits: int = 3,
        maximum_missed_updates: int = 4,
        maximum_coast_time_s: float = 3.0,
        camera_label_gate_m: float = 0.75,
    ) -> None:
        """Configure extraction, estimation, association, and track aging."""
        _validate_cluster_parameters(
            obstacle_cost_threshold,
            cluster_radius_m,
            minimum_cluster_cells,
            maximum_cluster_cells,
            maximum_cluster_extent_m,
            static_occupied_threshold,
            static_exclusion_radius_m,
        )
        if process_variance <= 0.0:
            raise ValueError('process variance must be positive')
        if measurement_variance <= 0.0:
            raise ValueError('measurement variance must be positive')
        if mahalanobis_gate <= 0.0:
            raise ValueError('Mahalanobis gate must be positive')
        if confirmation_hits <= 0:
            raise ValueError('confirmation hits must be positive')
        if maximum_missed_updates <= 0:
            raise ValueError('maximum missed updates must be positive')
        if maximum_coast_time_s <= 0.0:
            raise ValueError('maximum coast time must be positive')
        if camera_label_gate_m <= 0.0:
            raise ValueError('camera label gate must be positive')
        self._cluster_radius_m = cluster_radius_m
        self._threshold = obstacle_cost_threshold
        self._minimum_cells = minimum_cluster_cells
        self._maximum_cells = maximum_cluster_cells
        self._maximum_extent = maximum_cluster_extent_m
        self._static_threshold = static_occupied_threshold
        self._static_exclusion_radius_m = static_exclusion_radius_m
        self._process_variance = process_variance
        self._measurement_variance = measurement_variance
        self._mahalanobis_gate = mahalanobis_gate
        self._confirmation_hits = confirmation_hits
        self._maximum_missed_updates = maximum_missed_updates
        self._maximum_coast_time_s = maximum_coast_time_s
        self._camera_label_gate_m = camera_label_gate_m
        self._tracks: dict[int, _KalmanTrack] = {}
        self._next_track_id = 1
        self._selected_track_id: int | None = None
        self._selected_label = ''
        self._selected_detector_id = ''
        self._last_selected_snapshot: TrackedObstacle | None = None
        self._last_target_observed = False

    @property
    def tracks(self) -> list[TrackedObstacle]:
        """Return all current tracks in stable numeric order."""
        return [
            self._tracks[track_id].snapshot()
            for track_id in sorted(self._tracks)
        ]

    @property
    def target(self) -> LabeledObstacle | None:
        """Return the selected person track, including coasting state."""
        track = self._selected_track()
        if track is None or not self._selected_label:
            return None
        return LabeledObstacle(
            self._selected_label,
            track.snapshot(),
            self._selected_detector_id,
        )

    @property
    def target_observed(self) -> bool:
        """Return whether this grid update measured the selected target."""
        return self._last_target_observed

    def reset(self) -> None:
        """Clear every costmap track and semantic selection."""
        self._tracks.clear()
        self._next_track_id = 1
        self.clear_selection()

    def clear_selection(self) -> None:
        """Forget only the selected semantic person, preserving world tracks."""
        self._selected_track_id = None
        self._selected_label = ''
        self._selected_detector_id = ''
        self._last_selected_snapshot = None
        self._last_target_observed = False

    def update(
        self,
        grid: CostmapGrid,
        static_map: CostmapGrid | None,
    ) -> LabeledObstacle | None:
        """Extract, globally associate, and update all obstacle tracks."""
        measurements = find_obstacle_clusters(
            grid,
            self._threshold,
            self._cluster_radius_m,
            self._minimum_cells,
            self._maximum_cells,
            self._maximum_extent,
            static_map,
            self._static_threshold,
            self._static_exclusion_radius_m,
        )
        tracks = [self._tracks[key] for key in sorted(self._tracks)]
        for track in tracks:
            track.predict(grid.stamp_seconds, self._process_variance)
        associations = self._associate(tracks, measurements)
        matched_tracks = set()
        matched_measurements = set()
        self._last_target_observed = False
        for track_index, measurement_index in associations:
            track = tracks[track_index]
            track.correct(
                measurements[measurement_index],
                self._measurement_variance,
                self._confirmation_hits,
            )
            matched_tracks.add(track_index)
            matched_measurements.add(measurement_index)
            if track.track_id == self._selected_track_id:
                self._last_target_observed = True
                self._last_selected_snapshot = track.snapshot()
        for track_index, track in enumerate(tracks):
            if track_index not in matched_tracks:
                track.miss()
        for measurement_index, measurement in enumerate(measurements):
            if measurement_index not in matched_measurements:
                self._create_track(measurement, grid.stamp_seconds)
        self._delete_expired_tracks(grid.stamp_seconds)
        target = self.target
        return target if target is not None and self._last_target_observed else None

    def bind(
        self,
        label: str,
        detected_position: Point2D,
        detector_track_id: str = '',
    ) -> LabeledObstacle | None:
        """Attach or reconfirm one camera person on an admissible grid track."""
        if not label:
            raise ValueError('target label is required')
        selected = self._selected_track()
        if selected is not None:
            if (
                distance(selected.snapshot().position, detected_position)
                <= self._camera_label_gate_m
            ):
                self._selected_detector_id = detector_track_id
                self._last_selected_snapshot = selected.snapshot()
                return self.target

        candidates = [
            track
            for track in self._tracks.values()
            if distance(track.snapshot().position, detected_position)
            <= self._camera_label_gate_m
        ]
        if not candidates:
            return None
        chosen = min(
            candidates,
            key=lambda track: (
                distance(track.snapshot().position, detected_position),
                not track.confirmed,
                track.track_id,
            ),
        )
        self._selected_track_id = chosen.track_id
        self._selected_label = label
        self._selected_detector_id = detector_track_id
        self._last_selected_snapshot = chosen.snapshot()
        return self.target

    def predict_target(
        self,
        stamp_seconds: float,
        maximum_horizon_s: float,
    ) -> Point2D | None:
        """Predict the selected costmap track for bounded display/search use."""
        target = self.target
        snapshot = target.track if target is not None else self._last_selected_snapshot
        if snapshot is None:
            return None
        return _predict_snapshot(snapshot, stamp_seconds, maximum_horizon_s)

    def _associate(
        self,
        tracks: list[_KalmanTrack],
        measurements: list[ObstacleCluster],
    ) -> list[tuple[int, int]]:
        if not tracks or not measurements:
            return []
        costs = []
        for track in tracks:
            row = []
            for measurement in measurements:
                statistical_cost = track.squared_mahalanobis(
                    measurement, self._measurement_variance
                )
                shape_ratio = max(
                    track.cell_count, measurement.cell_count
                ) / max(1, min(track.cell_count, measurement.cell_count))
                shape_penalty = min(2.0, 0.25 * abs(math.log(shape_ratio)))
                row.append(statistical_cost + shape_penalty)
            costs.append(row)
        return _gated_global_assignment(costs, self._mahalanobis_gate)

    def _create_track(
        self,
        measurement: ObstacleCluster,
        stamp_seconds: float,
    ) -> None:
        track = _KalmanTrack(
            self._next_track_id,
            measurement,
            stamp_seconds,
            self._measurement_variance,
            self._confirmation_hits,
        )
        self._tracks[track.track_id] = track
        self._next_track_id += 1

    def _delete_expired_tracks(self, stamp_seconds: float) -> None:
        expired = [
            track_id
            for track_id, track in self._tracks.items()
            if track.misses > self._maximum_missed_updates
            or stamp_seconds - track.last_measurement_seconds
            > self._maximum_coast_time_s
        ]
        for track_id in expired:
            track = self._tracks.pop(track_id)
            if track_id == self._selected_track_id:
                self._last_selected_snapshot = track.snapshot()
                self._selected_track_id = None

    def _selected_track(self) -> _KalmanTrack | None:
        if self._selected_track_id is None:
            return None
        return self._tracks.get(self._selected_track_id)


def _make_cluster(
    grid: CostmapGrid,
    component: list[tuple[int, int]],
    minimum_cluster_cells: int,
    maximum_cluster_cells: int,
    maximum_cluster_extent_m: float,
) -> ObstacleCluster | None:
    cell_count = len(component)
    if not minimum_cluster_cells <= cell_count <= maximum_cluster_cells:
        return None
    xs = [cell[0] for cell in component]
    ys = [cell[1] for cell in component]
    extent = grid.resolution * math.hypot(
        max(xs) - min(xs) + 1,
        max(ys) - min(ys) + 1,
    )
    if extent > maximum_cluster_extent_m:
        return None
    centers = [grid.cell_center(*cell) for cell in component]
    return ObstacleCluster(
        Point2D(
            sum(point.x for point in centers) / cell_count,
            sum(point.y for point in centers) / cell_count,
        ),
        cell_count,
        extent,
    )


def _is_static_obstacle(
    static_map: CostmapGrid | None,
    point: Point2D,
    occupied_threshold: int,
    exclusion_radius_m: float,
) -> bool:
    """Test whether a cell belongs to saved static geometry plus margin."""
    if static_map is None:
        return False
    center = static_map.world_to_cell(point)
    if center is None:
        return False
    radius_cells = math.ceil(exclusion_radius_m / static_map.resolution)
    for offset_y in range(-radius_cells, radius_cells + 1):
        for offset_x in range(-radius_cells, radius_cells + 1):
            if math.hypot(offset_x, offset_y) * static_map.resolution > (
                exclusion_radius_m + 1e-9
            ):
                continue
            cell_x = center[0] + offset_x
            cell_y = center[1] + offset_y
            if not 0 <= cell_x < static_map.width:
                continue
            if not 0 <= cell_y < static_map.height:
                continue
            if static_map.cost(cell_x, cell_y) >= occupied_threshold:
                return True
    return False


def _predict_snapshot(
    snapshot: TrackedObstacle,
    stamp_seconds: float,
    maximum_horizon_s: float,
) -> Point2D:
    elapsed = min(
        max(0.0, stamp_seconds - snapshot.stamp_seconds),
        max(0.0, maximum_horizon_s),
    )
    return Point2D(
        snapshot.position.x + snapshot.velocity.x * elapsed,
        snapshot.position.y + snapshot.velocity.y * elapsed,
    )


def _gated_global_assignment(
    costs: list[list[float]], gate: float
) -> list[tuple[int, int]]:
    """Solve gated global nearest-neighbor assignment with Hungarian."""
    row_count = len(costs)
    column_count = len(costs[0]) if costs else 0
    if row_count == 0 or column_count == 0:
        return []
    size = row_count + column_count
    blocked = gate * 1000000.0
    unmatched = gate
    square = [[0.0] * size for _ in range(size)]
    for row in range(row_count):
        for column in range(column_count):
            value = costs[row][column]
            square[row][column] = value if value <= gate else blocked
        for column in range(column_count, size):
            square[row][column] = unmatched
    for row in range(row_count, size):
        for column in range(column_count):
            square[row][column] = unmatched
        for column in range(column_count, size):
            square[row][column] = 0.0
    assignment = _hungarian(square)
    return [
        (row, column)
        for row, column in enumerate(assignment[:row_count])
        if 0 <= column < column_count and costs[row][column] <= gate
    ]


def _hungarian(costs: list[list[float]]) -> list[int]:
    """Return minimum-cost column per row for a square cost matrix."""
    size = len(costs)
    if size == 0 or any(len(row) != size for row in costs):
        raise ValueError('Hungarian input must be a non-empty square matrix')
    row_potential = [0.0] * (size + 1)
    column_potential = [0.0] * (size + 1)
    matching = [0] * (size + 1)
    previous = [0] * (size + 1)
    for row in range(1, size + 1):
        matching[0] = row
        column_0 = 0
        minimum = [math.inf] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[column_0] = True
            current_row = matching[column_0]
            delta = math.inf
            column_1 = 0
            for column in range(1, size + 1):
                if used[column]:
                    continue
                reduced = (
                    costs[current_row - 1][column - 1]
                    - row_potential[current_row]
                    - column_potential[column]
                )
                if reduced < minimum[column]:
                    minimum[column] = reduced
                    previous[column] = column_0
                if minimum[column] < delta:
                    delta = minimum[column]
                    column_1 = column
            for column in range(size + 1):
                if used[column]:
                    row_potential[matching[column]] += delta
                    column_potential[column] -= delta
                else:
                    minimum[column] -= delta
            column_0 = column_1
            if matching[column_0] == 0:
                break
        while True:
            column_1 = previous[column_0]
            matching[column_0] = matching[column_1]
            column_0 = column_1
            if column_0 == 0:
                break
    result = [-1] * size
    for column in range(1, size + 1):
        if matching[column] != 0:
            result[matching[column] - 1] = column - 1
    return result


def _validate_cluster_parameters(
    obstacle_cost_threshold: int,
    cluster_radius_m: float,
    minimum_cluster_cells: int,
    maximum_cluster_cells: int,
    maximum_cluster_extent_m: float,
    static_occupied_threshold: int,
    static_exclusion_radius_m: float,
) -> None:
    if not 0 <= obstacle_cost_threshold <= 255:
        raise ValueError('obstacle cost threshold must be in [0, 255]')
    if cluster_radius_m <= 0.0:
        raise ValueError('cluster radius must be positive')
    if minimum_cluster_cells <= 0:
        raise ValueError('minimum cluster cells must be positive')
    if maximum_cluster_cells < minimum_cluster_cells:
        raise ValueError('maximum cluster cells must cover the minimum')
    if maximum_cluster_extent_m <= 0.0:
        raise ValueError('maximum cluster extent must be positive')
    if not 0 <= static_occupied_threshold <= 100:
        raise ValueError('static occupied threshold must be in [0, 100]')
    if static_exclusion_radius_m < 0.0:
        raise ValueError('static exclusion radius must be non-negative')
