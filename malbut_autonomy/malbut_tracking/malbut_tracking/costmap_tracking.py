"""Represent navigation grids and track sensor obstacle measurements."""

from dataclasses import dataclass
import math
from typing import Sequence

import numpy as np

from .geometry import Point2D, distance


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
    """One compact dynamic obstacle measurement extracted from a sensor."""

    position: Point2D
    point_count: int
    extent_m: float


@dataclass(frozen=True)
class TrackedObstacle:
    """Public snapshot of one constant-velocity obstacle track."""

    track_id: int
    position: Point2D
    velocity: Point2D
    point_count: int
    extent_m: float
    hits: int
    misses: int
    confirmed: bool
    stamp_seconds: float


@dataclass(frozen=True)
class LabeledObstacle:
    """A stable semantic person label attached to an obstacle track."""

    label: str
    track: TrackedObstacle
    detector_track_id: str

    @property
    def cluster(self) -> ObstacleCluster:
        """Expose the latest obstacle measurement for follow policy code."""
        return ObstacleCluster(
            self.track.position,
            self.track.point_count,
            self.track.extent_m,
        )

    @property
    def stamp_seconds(self) -> float:
        """Return the sensor time represented by this labeled track."""
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
        self.point_count = measurement.point_count
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
        self.point_count = measurement.point_count
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
            point_count=self.point_count,
            extent_m=self.extent_m,
            hits=self.hits,
            misses=self.misses,
            confirmed=self.confirmed,
            stamp_seconds=self.stamp_seconds,
        )


class ObstacleTargetTracker:
    """Track sensor obstacle clusters and attach one persistent person label."""

    def __init__(
        self,
        process_variance: float = 1.0,
        measurement_variance: float = 0.04,
        mahalanobis_gate: float = 9.21,
        confirmation_hits: int = 3,
        maximum_missed_updates: int = 4,
        maximum_coast_time_s: float = 3.0,
        camera_label_gate_m: float = 0.40,
        camera_rebind_margin_m: float = 0.15,
    ) -> None:
        """Configure estimation, association, and track aging."""
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
        if camera_rebind_margin_m < 0.0:
            raise ValueError('camera rebind margin must be non-negative')
        self._process_variance = process_variance
        self._measurement_variance = measurement_variance
        self._mahalanobis_gate = mahalanobis_gate
        self._confirmation_hits = confirmation_hits
        self._maximum_missed_updates = maximum_missed_updates
        self._maximum_coast_time_s = maximum_coast_time_s
        self._camera_label_gate_m = camera_label_gate_m
        self._camera_rebind_margin_m = camera_rebind_margin_m
        self._tracks: dict[int, _KalmanTrack] = {}
        self._observed_track_ids: set[int] = set()
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
        """Return whether this sensor update measured the selected target."""
        return self._last_target_observed

    def reset(self) -> None:
        """Clear every obstacle track and semantic selection."""
        self._tracks.clear()
        self._observed_track_ids.clear()
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
        measurements: Sequence[ObstacleCluster],
        stamp_seconds: float,
    ) -> LabeledObstacle | None:
        """Globally associate sensor measurements and update all tracks."""
        tracks = [self._tracks[key] for key in sorted(self._tracks)]
        for track in tracks:
            track.predict(stamp_seconds, self._process_variance)
        associations = self._associate(tracks, measurements)
        matched_tracks = set()
        matched_measurements = set()
        self._observed_track_ids.clear()
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
            self._observed_track_ids.add(track.track_id)
            if track.track_id == self._selected_track_id:
                self._last_target_observed = True
                self._last_selected_snapshot = track.snapshot()
        for track_index, track in enumerate(tracks):
            if track_index not in matched_tracks:
                track.miss()
        for measurement_index, measurement in enumerate(measurements):
            if measurement_index not in matched_measurements:
                track_id = self._create_track(
                    measurement,
                    stamp_seconds,
                )
                self._observed_track_ids.add(track_id)
        self._delete_expired_tracks(stamp_seconds)
        target = self.target
        return target if target is not None and self._last_target_observed else None

    def bind(
        self,
        label: str,
        detected_position: Point2D,
        detector_track_id: str = '',
    ) -> LabeledObstacle | None:
        """Attach the camera person to a current, spatially consistent track."""
        if not label:
            raise ValueError('target label is required')
        self._selected_label = label
        self._selected_detector_id = detector_track_id
        selected = self._selected_track()
        candidates = [
            track
            for track in self._tracks.values()
            if track.track_id in self._observed_track_ids
            if distance(track.snapshot().position, detected_position)
            <= self._camera_label_gate_m
        ]
        if not candidates:
            # A visible camera person is authoritative. Do not let a stale or
            # spatially inconsistent obstacle retain the semantic label and
            # drive LiDAR-only continuation.
            self._selected_track_id = None
            self._last_selected_snapshot = None
            self._last_target_observed = False
            return None
        nearest = min(
            candidates,
            key=lambda track: (
                distance(track.snapshot().position, detected_position),
                not track.confirmed,
                track.track_id,
            ),
        )
        chosen = nearest
        if (
            selected is not None
            and selected.track_id in self._observed_track_ids
            and selected in candidates
        ):
            selected_residual = distance(
                selected.snapshot().position,
                detected_position,
            )
            nearest_residual = distance(
                nearest.snapshot().position,
                detected_position,
            )
            # Association hysteresis prevents two nearby obstacle clusters
            # from exchanging the person label due to one noisy grid frame.
            # A clearly better camera match still corrects a wrong label.
            if (
                nearest.track_id == selected.track_id
                or nearest_residual + self._camera_rebind_margin_m
                >= selected_residual
            ):
                chosen = selected
        self._selected_track_id = chosen.track_id
        self._last_selected_snapshot = chosen.snapshot()
        self._last_target_observed = True
        return self.target

    def bind_observed_track(
        self,
        label: str,
        track_id: int,
        detector_track_id: str = '',
    ) -> LabeledObstacle | None:
        """Bind a confirmed track measured in the current sensor update."""
        if not label:
            raise ValueError('target label is required')
        track = self._tracks.get(track_id)
        if (
            track is None
            or track_id not in self._observed_track_ids
            or not track.confirmed
        ):
            return None
        self._selected_label = label
        self._selected_detector_id = detector_track_id
        self._selected_track_id = track_id
        self._last_selected_snapshot = track.snapshot()
        self._last_target_observed = True
        return self.target

    def predict_target(
        self,
        stamp_seconds: float,
        maximum_horizon_s: float,
    ) -> Point2D | None:
        """Predict the selected obstacle track for bounded display/search."""
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
                    track.point_count, measurement.point_count
                ) / max(1, min(track.point_count, measurement.point_count))
                shape_penalty = min(2.0, 0.25 * abs(math.log(shape_ratio)))
                row.append(statistical_cost + shape_penalty)
            costs.append(row)
        return _gated_global_assignment(costs, self._mahalanobis_gate)

    def _create_track(
        self,
        measurement: ObstacleCluster,
        stamp_seconds: float,
    ) -> int:
        track = _KalmanTrack(
            self._next_track_id,
            measurement,
            stamp_seconds,
            self._measurement_variance,
            self._confirmation_hits,
        )
        self._tracks[track.track_id] = track
        self._next_track_id += 1
        return track.track_id

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
            self._observed_track_ids.discard(track_id)
            if track_id == self._selected_track_id:
                self._last_selected_snapshot = track.snapshot()
                self._selected_track_id = None

    def _selected_track(self) -> _KalmanTrack | None:
        if self._selected_track_id is None:
            return None
        return self._tracks.get(self._selected_track_id)


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
