"""Occupancy-grid processing used to derive safe roaming candidates."""

from array import array
from collections import deque
from dataclasses import dataclass
import math
from typing import Iterable

from malbut_roaming.geometry import Point2D


@dataclass(frozen=True)
class Candidate:
    """A collision-clear candidate destination in map coordinates."""

    cell_x: int
    cell_y: int
    x: float
    y: float
    clearance: float

    @property
    def key(self) -> tuple[int, int]:
        """Return a stable map-cell identifier."""
        return self.cell_x, self.cell_y

    @property
    def point(self) -> Point2D:
        """Return this candidate as a planar point."""
        return Point2D(self.x, self.y)


class GridMap:
    """Immutable occupancy grid with a conservative clearance field."""

    _UNREACHED = 65535
    _NEIGHBORS = (
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (1, 0),
        (-1, 1),
        (0, 1),
        (1, 1),
    )

    def __init__(
        self,
        width: int,
        height: int,
        resolution: float,
        origin_x: float,
        origin_y: float,
        origin_yaw: float,
        occupancy: Iterable[int],
        occupied_threshold: int = 65,
    ) -> None:
        """Create a grid and compute distance from occupied or unknown cells."""
        if width <= 0 or height <= 0:
            raise ValueError('grid dimensions must be positive')
        if resolution <= 0.0:
            raise ValueError('grid resolution must be positive')
        values = tuple(int(value) for value in occupancy)
        if len(values) != width * height:
            raise ValueError('occupancy data does not match grid dimensions')
        if not 0 <= occupied_threshold <= 100:
            raise ValueError('occupied_threshold must be in [0, 100]')

        self.width = width
        self.height = height
        self.resolution = resolution
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.origin_yaw = origin_yaw
        self.occupancy = values
        self.occupied_threshold = occupied_threshold
        self._clearance_cells = self._build_clearance_field()

    @classmethod
    def from_message(
        cls,
        message,
        occupied_threshold: int = 65,
    ) -> 'GridMap':
        """Build from a nav_msgs/OccupancyGrid-compatible object."""
        orientation = message.info.origin.orientation
        sin_yaw = 2.0 * (
            orientation.w * orientation.z
            + orientation.x * orientation.y
        )
        cos_yaw = 1.0 - 2.0 * (
            orientation.y * orientation.y
            + orientation.z * orientation.z
        )
        return cls(
            width=int(message.info.width),
            height=int(message.info.height),
            resolution=float(message.info.resolution),
            origin_x=float(message.info.origin.position.x),
            origin_y=float(message.info.origin.position.y),
            origin_yaw=math.atan2(sin_yaw, cos_yaw),
            occupancy=message.data,
            occupied_threshold=occupied_threshold,
        )

    def candidates(
        self,
        spacing_m: float,
        minimum_clearance_m: float,
        maximum_free_occupancy: int = 20,
    ) -> tuple[Candidate, ...]:
        """Sample safe destinations on a regular metric grid."""
        if spacing_m <= 0.0:
            raise ValueError('spacing_m must be positive')
        if minimum_clearance_m < 0.0:
            raise ValueError('minimum_clearance_m must be non-negative')
        if not 0 <= maximum_free_occupancy <= 100:
            raise ValueError('maximum_free_occupancy must be in [0, 100]')
        stride = max(1, math.ceil(spacing_m / self.resolution))
        offset = stride // 2
        minimum_cells = math.ceil(
            minimum_clearance_m / self.resolution
        )
        sampled = []
        for cell_y in range(offset, self.height, stride):
            row_start = cell_y * self.width
            for cell_x in range(offset, self.width, stride):
                index = row_start + cell_x
                value = self.occupancy[index]
                clearance_cells = self._clearance_cells[index]
                if not 0 <= value <= maximum_free_occupancy:
                    continue
                if clearance_cells < minimum_cells:
                    continue
                point = self.cell_to_world(cell_x, cell_y)
                sampled.append(Candidate(
                    cell_x=cell_x,
                    cell_y=cell_y,
                    x=point.x,
                    y=point.y,
                    clearance=clearance_cells * self.resolution,
                ))
        return tuple(sampled)

    def nearest_candidate(
        self,
        candidates: Iterable[Candidate],
        point: Point2D,
        maximum_distance_m: float,
    ) -> Candidate | None:
        """Find the nearest safe candidate to a desired map point."""
        if maximum_distance_m < 0.0 or not math.isfinite(maximum_distance_m):
            raise ValueError('maximum_distance_m must be finite and non-negative')
        nearest = None
        nearest_distance = maximum_distance_m
        for candidate in candidates:
            candidate_distance = math.hypot(
                candidate.x - point.x,
                candidate.y - point.y,
            )
            if candidate_distance <= nearest_distance:
                nearest = candidate
                nearest_distance = candidate_distance
        return nearest

    def cell_to_world(self, cell_x: int, cell_y: int) -> Point2D:
        """Convert the center of a map cell to map-frame coordinates."""
        if not 0 <= cell_x < self.width or not 0 <= cell_y < self.height:
            raise ValueError('cell coordinates are outside the occupancy grid')
        local_x = (cell_x + 0.5) * self.resolution
        local_y = (cell_y + 0.5) * self.resolution
        cosine = math.cos(self.origin_yaw)
        sine = math.sin(self.origin_yaw)
        return Point2D(
            self.origin_x + cosine * local_x - sine * local_y,
            self.origin_y + sine * local_x + cosine * local_y,
        )

    def occupancy_at(self, point: Point2D) -> int | None:
        """Return the occupancy value at a world point, if it is in bounds."""
        delta_x = point.x - self.origin_x
        delta_y = point.y - self.origin_y
        cosine = math.cos(self.origin_yaw)
        sine = math.sin(self.origin_yaw)
        local_x = cosine * delta_x + sine * delta_y
        local_y = -sine * delta_x + cosine * delta_y
        cell_x = math.floor(local_x / self.resolution)
        cell_y = math.floor(local_y / self.resolution)
        if not 0 <= cell_x < self.width or not 0 <= cell_y < self.height:
            return None
        return self.occupancy[cell_y * self.width + cell_x]

    def _build_clearance_field(self) -> array:
        distances = array(
            'H',
            [self._UNREACHED],
        ) * (self.width * self.height)
        frontier = deque()
        for index, value in enumerate(self.occupancy):
            cell_x = index % self.width
            cell_y = index // self.width
            is_boundary = (
                cell_x in {0, self.width - 1}
                or cell_y in {0, self.height - 1}
            )
            if is_boundary or value < 0 or value >= self.occupied_threshold:
                distances[index] = 0
                frontier.append(index)

        while frontier:
            index = frontier.popleft()
            cell_x = index % self.width
            cell_y = index // self.width
            next_distance = min(distances[index] + 1, self._UNREACHED - 1)
            for delta_x, delta_y in self._NEIGHBORS:
                neighbor_x = cell_x + delta_x
                neighbor_y = cell_y + delta_y
                if not 0 <= neighbor_x < self.width:
                    continue
                if not 0 <= neighbor_y < self.height:
                    continue
                neighbor = neighbor_y * self.width + neighbor_x
                if next_distance >= distances[neighbor]:
                    continue
                distances[neighbor] = next_distance
                frontier.append(neighbor)
        return distances
