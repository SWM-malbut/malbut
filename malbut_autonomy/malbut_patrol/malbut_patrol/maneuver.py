"""Live turning clearance and reversible rotate-then-drive connectivity."""

import math

import cv2
import numpy as np


class ManeuverSpace:
    """
    Require the padded body's entire rotation sweep to fit on the live map.

    A connected path through these cells can be followed using forward/reverse
    translation and in-place turns; it does not assume lateral wheel commands.
    Nav2 remains responsible for actual paths and collision checks.
    """

    def __init__(self, grid, length=.277, width=.212, padding=.01):
        if (not all(math.isfinite(v) and v > 0 for v in (length, width))
                or not math.isfinite(padding) or padding < 0):
            raise ValueError('Invalid maneuver footprint dimensions')
        self.grid = grid
        self.radius = math.hypot(length/2+padding, width/2+padding)
        occupied = ((grid.cells < 0) | (grid.cells >= 100)).astype('uint8')
        self.occupied = occupied.astype(bool)
        # Include obstacle cell extent AND possible robot-centre offset within
        # the goal cell. This also protects translations between adjacent cells.
        size = math.ceil((self.radius+grid.resolution*math.sqrt(2))/grid.resolution)
        yy, xx = np.mgrid[-size:size+1, -size:size+1] * grid.resolution
        distance = np.hypot(np.maximum(abs(xx)-grid.resolution/2, 0),
                            np.maximum(abs(yy)-grid.resolution/2, 0))
        kernel = (distance <= self.radius+grid.resolution/math.sqrt(2)).astype('uint8')
        self.rotation_safe = cv2.dilate(
            occupied, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=1) == 0
        # Cost 99 already represents centre collision with the inscribed band.
        # Keep it blocked without inflating the soft gradient a second time.
        self.rotation_safe &= (grid.cells >= 0) & (grid.cells < 99)
        _, self.labels = cv2.connectedComponents(
            self.rotation_safe.astype('uint8'), connectivity=4)

    def _sweep_clear(self, first, second):
        # Work in grid coordinates, also for rotated map origins. The rounded
        # bounding rectangle encloses the swept disk between the two points.
        # This conservative connector avoids snapping an actual arrival pose
        # into a whole-cell mask, which can falsely reject a safe off-centre turn.
        a = np.asarray(self.grid.world_to_grid(*first[:2]), dtype=float)
        b = np.asarray(self.grid.world_to_grid(*second[:2]), dtype=float)
        low, high = np.minimum(a, b), np.maximum(a, b)
        radius = self.radius / self.grid.resolution
        if (np.any(low-radius <= 0)
                or np.any(high+radius >= [self.grid.width, self.grid.height])):
            return False
        left, bottom = np.floor(low-radius).astype(int)
        right, top = np.ceil(high+radius).astype(int)
        rows, cols = np.nonzero(self.occupied[bottom:top, left:right])
        cols, rows = cols+left, rows+bottom
        dx = np.maximum(np.maximum(low[0]-(cols+1), cols-high[0]), 0)
        dy = np.maximum(np.maximum(low[1]-(rows+1), rows-high[1]), 0)
        return bool(np.all(np.hypot(dx, dy) > radius))

    def _components(self, pose):
        row, col = self.grid.world_to_cell(*pose[:2])
        if (not self.grid.contains(row, col)
                or not 0 <= self.grid.cells[row, col] < 99):
            return set()
        component = int(self.labels[row, col])
        if component:
            return {component}
        if not self._sweep_clear(pose, pose):
            return set()
        components = set()
        for rr in range(max(0, row-1), min(self.grid.height, row+2)):
            for cc in range(max(0, col-1), min(self.grid.width, col+2)):
                label = int(self.labels[rr, cc])
                if label and self._sweep_clear(pose, self.grid.cell_to_world(rr, cc)):
                    components.add(label)
        return components

    def connected(self, first, second):
        """Require both endpoints in one collision-free manoeuvring component."""
        return bool(self._components(first) & self._components(second))

    def observation_mask(self, current, home, arrival_tolerance=0.):
        """Reject spots without a live approach AND a return connection."""
        if not math.isfinite(arrival_tolerance) or arrival_tolerance < 0:
            raise ValueError('Invalid arrival tolerance')
        components = self._components(current) & self._components(home)
        allowed = np.isin(self.labels, list(components))
        if arrival_tolerance == 0:
            return allowed
        # NavigateToPose may succeed anywhere in its XY goal tolerance. Require
        # that entire arrival disk to retain room for turning and leaving.
        radius = arrival_tolerance/self.grid.resolution + 1/math.sqrt(2)
        size = math.ceil(radius)
        yy, xx = np.mgrid[-size:size+1, -size:size+1]
        kernel = (np.hypot(xx, yy) <= radius).astype('uint8')
        return cv2.erode(allowed.astype('uint8'), kernel,
                         borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool)
