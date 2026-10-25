"""Bounded room envelopes for observations, never for robot navigation."""

import copy

import cv2
import numpy as np


def object_room_snapshot(regions, grid):
    """
    Fill adjacent furniture cells without relabelling free floor or other rooms.

    The floor polygons remain authoritative at shared room boundaries. Only
    occupied/unknown cells outside them can join the nearest room, within 45 cm.
    This is an explicit semantic approximation, not evidence of traversability.
    """
    extension = .45
    policy = {'method': 'bounded_nonfree_room_envelope', 'extension_m': extension,
              'resolution_m': grid.resolution}
    if (not regions or grid.resolution > .1
            or grid.cells.size * len(regions) > 16000000):
        return copy.deepcopy(regions), dict(policy, method='floor_polygons_only')
    masks, holes = [], np.zeros(grid.cells.shape, np.uint8)
    for region in regions:
        mask = np.zeros_like(holes)
        for polygon in region['polygons']:
            rings = []
            for ring in polygon:
                points = np.asarray(ring)
                cols, rows = grid.world_to_grid(points[:, 0], points[:, 1])
                rings.append(np.rint(np.column_stack((cols, rows))-.5).astype(np.int32))
            part = np.zeros_like(mask)
            cv2.fillPoly(part, rings[:1], 1)
            for ring in rings[1:]:
                cv2.fillPoly(part, [ring], 0)
                cv2.fillPoly(holes, [ring], 1)
            mask |= part
        masks.append(mask)
    occupied = np.sum(masks, axis=0) > 0
    best = np.full(grid.cells.shape, np.inf, np.float32)
    second = best.copy()
    owner = np.full(grid.cells.shape, -1, np.int16)
    for index, mask in enumerate(masks):
        if not np.any(mask):
            continue
        distances = cv2.distanceTransform(1-mask, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
        distances *= grid.resolution
        closer = distances < best
        second = np.where(closer, best, np.minimum(second, distances))
        best = np.minimum(best, distances)
        owner[closer] = index
    # One-cell ambiguity band avoids inventing a boundary in tied furniture cells.
    separation = np.full_like(best, np.inf)
    np.subtract(second, best, out=separation, where=np.isfinite(best))
    eligible = (~occupied & ~grid.free & (holes == 0) & (best <= extension)
                & (separation > grid.resolution))
    result = []
    for index, region in enumerate(regions):
        mask = masks[index] | (eligible & (owner == index)).astype(np.uint8)
        contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        polygons = []
        if hierarchy is not None:
            for i, contour in enumerate(contours):
                if hierarchy[0, i, 3] != -1 or len(contour) < 3:
                    continue
                rings, current = [], i
                while current != -1:
                    points = contours[current].reshape(-1, 2)
                    if len(points) >= 3:
                        x, y = grid.cell_to_world(points[:, 1], points[:, 0])
                        ring = np.round(np.column_stack((x, y)), 6).tolist()
                        rings.append(ring + [ring[0]])
                    current = (hierarchy[0, i, 2] if current == i
                               else hierarchy[0, current, 0])
                polygons.append(rings)
        # Rooms outside the grid still retain their original, unextended geometry.
        result.append(dict(region, polygons=polygons or copy.deepcopy(region['polygons'])))
    return result, policy
