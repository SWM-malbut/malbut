"""
Plan camera coverage from a saved occupancy grid, without fixed waypoints.

Coverage is a two-dimensional floor-plan proxy: a free cell is observed when
an actual camera frame arrives from a pose with unobstructed planar sight of
that cell, inside the camera's horizontal field of view and inspection range.
It is not a claim that every three-dimensional surface was visible in pixels.
Unknown/occupied cells block sight; unreachable viewpoints are never generated.
Static preprocessing and candidate visibility are reused for the whole patrol.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass
import math
from typing import Callable, Optional

import cv2
import numpy as np


@dataclass(frozen=True)
class CoverageProfile:
    """Specify inspection range, desired visible fraction, and sampling pitch."""

    range_m: float
    target_ratio: float
    candidate_spacing_m: float

    def __post_init__(self):
        """Reject invalid settings before allocating visibility caches."""
        if not math.isfinite(self.range_m) or self.range_m <= 0.0:
            raise ValueError('range_m must be positive and finite')
        if not math.isfinite(self.target_ratio) or not 0.0 < self.target_ratio <= 1.0:
            raise ValueError('target_ratio must be in (0, 1]')
        if (not math.isfinite(self.candidate_spacing_m)
                or self.candidate_spacing_m <= 0.0):
            raise ValueError('candidate_spacing_m must be positive and finite')


@dataclass(frozen=True)
class Viewpoint:
    """Describe a map-frame navigation pose and its stable candidate index."""

    x: float
    y: float
    yaw: float
    index: int


class CoverageGrid:
    """Keep an immutable ROS occupancy grid with its full planar origin pose."""

    def __init__(self, cells, resolution, origin_x, origin_y, origin_yaw=0.0):
        """Copy occupancy values; rows follow ROS grid order, not image order."""
        values = np.asarray(cells)
        if values.ndim != 2:
            raise ValueError('cells must be a two-dimensional array')
        if not math.isfinite(resolution) or resolution <= 0.0:
            raise ValueError('resolution must be positive and finite')
        if not all(math.isfinite(v) for v in (origin_x, origin_y, origin_yaw)):
            raise ValueError('grid origin must be finite')
        self.cells = values.astype(np.int16, copy=True)
        self.cells.flags.writeable = False
        self.resolution = float(resolution)
        self.origin_x = float(origin_x)
        self.origin_y = float(origin_y)
        self.origin_yaw = float(origin_yaw)
        self.height, self.width = self.cells.shape
        self.free = (self.cells >= 0) & (self.cells < 50)
        self.free.flags.writeable = False
        self._cos = math.cos(origin_yaw)
        self._sin = math.sin(origin_yaw)

    def world_to_grid(self, x, y):
        """Return continuous column/row coordinates measured from grid edges."""
        dx, dy = np.asarray(x) - self.origin_x, np.asarray(y) - self.origin_y
        return (
            (self._cos * dx + self._sin * dy) / self.resolution,
            (-self._sin * dx + self._cos * dy) / self.resolution,
        )

    def world_to_cell(self, x, y):
        """Return row and column for a map-frame position, possibly out of bounds."""
        col, row = self.world_to_grid(x, y)
        return int(math.floor(float(row))), int(math.floor(float(col)))

    def cell_to_world(self, row, col):
        """Return the map-frame center of one cell or arrays of cells."""
        local_x = (np.asarray(col) + 0.5) * self.resolution
        local_y = (np.asarray(row) + 0.5) * self.resolution
        return (
            self.origin_x + self._cos * local_x - self._sin * local_y,
            self.origin_y + self._sin * local_x + self._cos * local_y,
        )

    def contains(self, row, col):
        """Return whether a cell is inside the map."""
        return 0 <= row < self.height and 0 <= col < self.width


def _flood_distances(mask, start):
    """Compute four-connected distances, disallowing diagonal corner cutting."""
    distances = np.full(mask.shape, -1, dtype=np.int32)
    row, col = start
    if not (0 <= row < mask.shape[0] and 0 <= col < mask.shape[1]
            and mask[row, col]):
        return distances
    distances[row, col] = 0
    pending = deque([(row, col)])
    height, width = mask.shape
    while pending:
        row, col = pending.popleft()
        distance = distances[row, col] + 1
        for nr, nc in ((row - 1, col), (row + 1, col),
                       (row, col - 1), (row, col + 1)):
            if (0 <= nr < height and 0 <= nc < width
                    and mask[nr, nc] and distances[nr, nc] < 0):
                distances[nr, nc] = distance
                pending.append((nr, nc))
    return distances


class CoveragePlanner:
    """Choose reachable, useful camera viewpoints and track actual observations."""

    def __init__(
        self, grid, profile, robot_xy, robot_clearance_m=0.25,
        rooms=None, camera_fov_rad=1.05,
    ):
        """Preprocess static clearance, reachable candidates, rooms, and sight."""
        if not math.isfinite(robot_clearance_m) or robot_clearance_m < 0.0:
            raise ValueError('robot_clearance_m must be nonnegative and finite')
        self._validate_fov(camera_fov_rad)
        self.grid = grid
        self.profile = profile
        self.camera_fov_rad = camera_fov_rad
        self._clearance_m = robot_clearance_m
        self._attempted = set()
        self._observed = np.zeros(grid.cells.size, dtype=bool)
        self._target = np.zeros(grid.cells.size, dtype=bool)
        self._target_count = 0
        self._observed_count = 0
        self._visit_names = set()
        self._visibility_cache = OrderedDict()
        self._candidates = []
        self._candidate_visible = []
        self._candidate_angles = []
        self._candidate_rooms = []
        self._room_cells = {}
        self._room_masks = self._load_rooms(rooms)
        self._inaccessible_names = []
        self.reachable = np.zeros(grid.cells.shape, dtype=bool)
        self._clearance = np.zeros(grid.cells.shape, dtype=np.float32)
        if not grid.cells.size or not np.any(grid.free):
            self._inaccessible_names = sorted(self._room_masks)
            return

        # Padding treats the unknown space beyond map boundaries as an obstacle.
        padded = np.pad(grid.free.astype(np.uint8), 1)
        self._clearance = (
            cv2.distanceTransform(padded, cv2.DIST_L2, 5)[1:-1, 1:-1]
            * grid.resolution
        )
        # Distance-transform values refer to obstacle cell centers; subtract the
        # cell half-diagonal to avoid letting the footprint intersect cell edges.
        safe = grid.free & (
            self._clearance >= robot_clearance_m + grid.resolution / math.sqrt(2)
        )
        start = self._nearest_cell(grid.free, robot_xy, robot_clearance_m)
        if start is None:
            self._inaccessible_names = sorted(self._room_masks)
            return
        free_component = _flood_distances(grid.free, start) >= 0
        start = self._nearest_cell(safe & free_component, robot_xy, robot_clearance_m)
        if start is None:
            self._inaccessible_names = sorted(self._room_masks)
            return
        self.reachable = _flood_distances(safe, start) >= 0
        self._make_candidates()
        for row, col in self._candidates:
            visible = self._visible_from(row, col)
            visible = visible[free_component.ravel()[visible]]
            self._candidate_visible.append(visible)
            vr, vc = np.divmod(visible, grid.width)
            self._candidate_angles.append(np.arctan2(vr - row, vc - col))
            self._target[visible] = True
            self._candidate_rooms.append(tuple(
                name for name, mask in self._room_masks.items() if mask[row, col]
            ))
        # The denominator includes areas observable from reachable viewpoints,
        # not enclosed rooms or corners that no legal camera position can see.
        for name, mask in self._room_masks.items():
            if np.any(mask & self.reachable):
                self._room_cells[name] = np.flatnonzero(mask.ravel() & self._target)
            else:
                self._inaccessible_names.append(name)
        self._target_count = int(np.count_nonzero(self._target))

    @staticmethod
    def _validate_fov(fov):
        """Require a physical horizontal field of view in radians."""
        if not math.isfinite(fov) or not 0.0 < fov <= 2.0 * math.pi:
            raise ValueError('camera field of view must be in (0, 2*pi]')

    def _nearest_cell(self, mask, xy, maximum_distance_m=None):
        """Find the nearest usable cell, with an optional distance bound."""
        row, col = self.grid.world_to_cell(*xy)
        if self.grid.contains(row, col) and mask[row, col]:
            return row, col
        rows, cols = np.nonzero(mask)
        if not rows.size:
            return None
        xs, ys = self.grid.cell_to_world(rows, cols)
        squared = (xs - xy[0]) ** 2 + (ys - xy[1]) ** 2
        best = int(np.argmin(squared))
        if (maximum_distance_m is not None
                and squared[best] > (maximum_distance_m + self.grid.resolution) ** 2):
            return None
        return int(rows[best]), int(cols[best])

    def _load_rooms(self, rooms):
        """Rasterize optional map-frame GeoJSON room polygons, including holes."""
        result = {}
        if not rooms or not self.grid.cells.size:
            return result
        features = rooms.get('features', []) if isinstance(rooms, dict) else rooms
        for index, feature in enumerate(features):
            props = feature.get('properties', {})
            if props.get('role', 'room') != 'room':
                continue
            geometry = feature.get('geometry') or {}
            kind = geometry.get('type')
            coordinates = geometry.get('coordinates', [])
            polygons = [coordinates] if kind == 'Polygon' else coordinates
            if kind not in ('Polygon', 'MultiPolygon'):
                continue
            mask = np.zeros(self.grid.cells.shape, dtype=np.uint8)
            for polygon in polygons:
                part = np.zeros_like(mask)
                for ring_index, ring in enumerate(polygon):
                    points = np.asarray(ring, dtype=float)
                    if (points.ndim != 2 or points.shape[0] < 3
                            or points.shape[1] < 2 or not np.isfinite(points).all()):
                        continue
                    cols, rows = self.grid.world_to_grid(points[:, 0], points[:, 1])
                    vertices = np.rint(
                        (np.column_stack((cols, rows)) - 0.5) * 256
                    ).astype(np.int32)
                    cv2.fillPoly(part, [vertices], 1 if ring_index == 0 else 0, shift=8)
                mask |= part
            name = str(props.get('name') or props.get('room_id')
                       or feature.get('id') or f'room-{index + 1}')
            if name in result:
                identity = props.get('room_id') or feature.get('id') or index + 1
                name = f'{name} ({identity})'
            result[name] = mask.astype(bool)
        return result

    def _make_candidates(self):
        """Sample safe cells in metric tiles and include every reachable room."""
        stride = max(1, int(round(
            self.profile.candidate_spacing_m / self.grid.resolution
        )))
        selected = set()
        for row in range(0, self.grid.height, stride):
            for col in range(0, self.grid.width, stride):
                tile = self.reachable[row:row + stride, col:col + stride]
                if not np.any(tile):
                    continue
                rr, cc = np.nonzero(tile)
                # Prefer clearance within a tile rather than a wall-adjacent
                # lattice point; this remains entirely map-derived.
                clearance = self._clearance[rr + row, cc + col]
                choice = int(np.argmax(clearance))
                selected.add((int(rr[choice] + row), int(cc[choice] + col)))
        for mask in self._room_masks.values():
            room = mask & self.reachable
            if not np.any(room) or any(room[row, col] for row, col in selected):
                continue
            rr, cc = np.nonzero(room)
            best = int(np.argmax(self._clearance[rr, cc]))
            selected.add((int(rr[best]), int(cc[best])))
        self._candidates = sorted(selected)

    def _visible_from(self, row, col):
        """Cast local supercover rays; walls and unknown cells terminate sight."""
        key = row, col
        if key in self._visibility_cache:
            self._visibility_cache.move_to_end(key)
            return self._visibility_cache[key]
        grid = self.grid
        if not grid.contains(row, col) or not grid.free[row, col]:
            return np.empty(0, dtype=np.int32)
        radius = self.profile.range_m / grid.resolution
        # Half-cell radial and arc spacing prevent holes at the range boundary.
        ray_count = max(16, int(math.ceil(4.0 * math.pi * radius)))
        angles = np.arange(ray_count) * (2.0 * math.pi / ray_count)
        distances = np.arange(0.0, radius + 0.25, 0.5)
        cols = np.floor(col + 0.5 + np.cos(angles[:, None]) * distances).astype(np.int32)
        rows = np.floor(row + 0.5 + np.sin(angles[:, None]) * distances).astype(np.int32)
        inside = (rows >= 0) & (rows < grid.height) & (cols >= 0) & (cols < grid.width)
        clipped_rows = np.clip(rows, 0, grid.height - 1)
        clipped_cols = np.clip(cols, 0, grid.width - 1)
        blocked = ~inside | ~grid.free[clipped_rows, clipped_cols]
        # When crossing a grid corner, both adjacent side cells must be clear.
        prev_rows = np.concatenate((clipped_rows[:, :1], clipped_rows[:, :-1]), axis=1)
        prev_cols = np.concatenate((clipped_cols[:, :1], clipped_cols[:, :-1]), axis=1)
        diagonal = (prev_rows != clipped_rows) & (prev_cols != clipped_cols)
        blocked |= diagonal & (
            ~grid.free[prev_rows, clipped_cols] | ~grid.free[clipped_rows, prev_cols]
        )
        clear = ~np.maximum.accumulate(blocked, axis=1)
        result = np.unique((rows[clear] * grid.width + cols[clear]).astype(np.int32))
        vr, vc = np.divmod(result, grid.width)
        result = result[(vr - row) ** 2 + (vc - col) ** 2 <= radius ** 2]
        self._visibility_cache[key] = result
        # Moving-camera observations get a bounded cache; candidate arrays have
        # their own persistent references and are not recomputed after eviction.
        if len(self._visibility_cache) > 64:
            self._visibility_cache.popitem(last=False)
        return result

    def mark_observed(self, x, y, yaw, fov_rad=None):
        """Credit coverage only when the caller received an actual camera frame."""
        fov = self.camera_fov_rad if fov_rad is None else fov_rad
        self._validate_fov(fov)
        if not all(math.isfinite(value) for value in (x, y, yaw)):
            return 0.0
        row, col = self.grid.world_to_cell(x, y)
        visible = self._visible_from(row, col)
        if not visible.size:
            return 0.0
        rows, cols = np.divmod(visible, self.grid.width)
        xs, ys = self.grid.cell_to_world(rows, cols)
        angles = np.arctan2(ys - y, xs - x) - yaw
        angles = np.arctan2(np.sin(angles), np.cos(angles))
        distance = (xs - x) ** 2 + (ys - y) ** 2
        visible = visible[((np.abs(angles) <= fov / 2.0 + 1e-12) | (distance <= 1e-24))
                          & (distance <= self.profile.range_m ** 2 + 1e-12)
                          & self._target[visible]]
        newly_seen = visible[~self._observed[visible]]
        self._observed[newly_seen] = True
        self._observed_count += newly_seen.size
        for name in self._room_cells:
            mask = self._room_masks[name]
            if mask[row, col] and np.any(mask.ravel()[visible]):
                self._visit_names.add(name)
        return float(newly_seen.size * self.grid.resolution ** 2)

    def _best_yaw(self, index, unseen):
        """Aim the first frame toward the largest remaining visible sector."""
        angles = self._candidate_angles[index][unseen]
        if not angles.size:
            return self.grid.origin_yaw
        bins = 36
        counts, _ = np.histogram(angles, bins=bins, range=(-math.pi, math.pi))
        width = max(1, min(bins, int(round(
            self.camera_fov_rad * bins / (2.0 * math.pi)
        ))))
        scores = np.convolve(np.tile(counts, 3), np.ones(width), mode='same')[bins:2 * bins]
        direction = (int(np.argmax(scores)) + 0.5) * 2.0 * math.pi / bins - math.pi
        return math.atan2(
            math.sin(direction + self.grid.origin_yaw),
            math.cos(direction + self.grid.origin_yaw),
        )

    def select(
        self, robot_xy, allowed: Optional[Callable[[float, float], bool]] = None,
    ):
        """Choose the next useful pose by room need, unseen area, and travel cost."""
        if self.complete or not self._candidates:
            return None
        start = self._nearest_cell(self.reachable, robot_xy, self._clearance_m)
        if start is None:
            return None
        travel = _flood_distances(self.reachable, start)
        unvisited = set(self.unvisited_room_names)
        uncovered = set(self.uncovered_room_names)
        best_key = None
        best = None
        for index, (row, col) in enumerate(self._candidates):
            if index in self._attempted or travel[row, col] < 0:
                continue
            x, y = self.grid.cell_to_world(row, col)
            x, y = float(x), float(y)
            if allowed is not None and not allowed(x, y):
                continue
            visible = self._candidate_visible[index]
            unseen = ~self._observed[visible]
            gain = int(np.count_nonzero(unseen))
            rooms = set(self._candidate_rooms[index])
            visit_needed = bool(rooms & unvisited)
            if not gain and not visit_needed:
                continue
            room_priority = 2 if visit_needed else int(bool(rooms & uncovered))
            distance_m = float(travel[row, col]) * self.grid.resolution
            score = gain * self.grid.resolution ** 2 / (1.0 + distance_m)
            key = room_priority, score, -distance_m, -index
            if best_key is None or key > best_key:
                best_key = key
                best = Viewpoint(x, y, self._best_yaw(index, unseen), index)
        return best

    def mark_attempted(self, index):
        """Retire a reached or failed viewpoint so blocked goals cannot loop."""
        if not 0 <= index < len(self._candidates):
            raise IndexError('unknown viewpoint index')
        self._attempted.add(index)

    @property
    def candidate_count(self):
        """Return the finite number of reachable map-derived viewpoints."""
        return len(self._candidates)

    @property
    def target_area_m2(self):
        """Return observable free-space area used as the coverage denominator."""
        return float(self._target_count * self.grid.resolution ** 2)

    @property
    def observed_area_m2(self):
        """Return the area credited from actual camera-frame observations."""
        return float(self._observed_count * self.grid.resolution ** 2)

    @property
    def remaining_area_m2(self):
        """Return observable target area that has not yet been seen."""
        return max(0.0, self.target_area_m2 - self.observed_area_m2)

    @property
    def coverage_ratio(self):
        """Return observed target fraction; an empty target has zero coverage."""
        return self._observed_count / self._target_count if self._target_count else 0.0

    @property
    def unvisited_room_names(self):
        """Return reachable labels not yet physically entered with a camera frame."""
        return tuple(sorted(set(self._room_cells) - self._visit_names))

    @property
    def uncovered_room_names(self):
        """Return reachable rooms lacking either a visit or desired coverage."""
        return tuple(sorted(
            name for name, cells in self._room_cells.items()
            if (name not in self._visit_names or not cells.size
                or np.count_nonzero(self._observed[cells]) / cells.size
                < self.profile.target_ratio)
        ))

    @property
    def inaccessible_room_names(self):
        """Return labeled rooms with no safe connected navigation position."""
        return tuple(sorted(self._inaccessible_names))

    @property
    def complete(self):
        """Require the selected coverage target and each reachable room's visit."""
        return (self.candidate_count > 0
                and self.coverage_ratio >= self.profile.target_ratio
                and not self.uncovered_room_names)
