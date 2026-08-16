"""Geometry operations for editing User Map Room features."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
import hashlib
import json
import math

import cv2
import numpy as np


SPLIT_COLORS = (
    "#dce8ff",
    "#f9e1c7",
    "#d9f0e3",
    "#eadffd",
    "#f8dce3",
    "#d9edf2",
    "#f3eabf",
    "#dfe4eb",
)
WALL_SNAP_DISTANCE_METERS = 0.25


class LocalTransform:
    """Convert one Room's world coordinates to a temporary image grid."""

    def __init__(
        self,
        minimum_x: float,
        maximum_y: float,
        resolution: float,
    ) -> None:
        """Store local grid bounds and resolution."""
        self.minimum_x = minimum_x
        self.maximum_y = maximum_y
        self.resolution = resolution

    def pixel(self, point: list[float]) -> tuple[int, int]:
        """Convert a world point to a local raster pixel."""
        return (
            round((point[0] - self.minimum_x) / self.resolution),
            round((self.maximum_y - point[1]) / self.resolution),
        )

    def world(self, x: int, y: int) -> list[float]:
        """Convert a local raster pixel to a world point."""
        return [
            round(self.minimum_x + x * self.resolution, 4),
            round(self.maximum_y - y * self.resolution, 4),
        ]


def _geometry_polygons(geometry: dict) -> list[list[list[list[float]]]]:
    if geometry.get("type") == "Polygon":
        return [geometry["coordinates"]]
    if geometry.get("type") == "MultiPolygon":
        return geometry["coordinates"]
    raise ValueError("Room geometry must be Polygon or MultiPolygon")


def _geometry_points(geometry: dict) -> list[list[float]]:
    return [
        point
        for polygon in _geometry_polygons(geometry)
        for ring in polygon
        for point in ring
    ]


def _ring_area(ring: list[list[float]]) -> float:
    return abs(sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(ring, ring[1:])
    )) / 2.0


def _geometry_area(geometry: dict) -> float:
    return sum(
        _ring_area(polygon[0])
        - sum(_ring_area(hole) for hole in polygon[1:])
        for polygon in _geometry_polygons(geometry)
    )


def _rasterize_room(
    geometry: dict,
    resolution: float,
) -> tuple[np.ndarray, LocalTransform]:
    masks, transform = _rasterize_geometries([geometry], resolution)
    return masks[0], transform


def room_representative_point(
    geometry: dict,
    resolution: float = 0.05,
) -> tuple[list[float], float]:
    """Return an interior Room point with the greatest wall clearance."""
    if resolution <= 0.0:
        raise ValueError("resolution must be positive")
    mask, transform = _rasterize_room(geometry, resolution)
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    _, maximum_distance, _, maximum_point = cv2.minMaxLoc(distance)
    if maximum_distance <= 0.0:
        raise ValueError("Room geometry contains no usable interior")
    return (
        transform.world(*maximum_point),
        round(float(maximum_distance) * resolution, 3),
    )


def normalize_room_feature(
    room: dict,
    resolution: float = 0.05,
) -> dict:
    """Validate a Room and refresh its server-owned navigation metadata."""
    if not isinstance(room, dict) or room.get("type") != "Feature":
        raise ValueError("every Room must be a GeoJSON Feature")
    properties = room.get("properties")
    if not isinstance(properties, dict) or properties.get("role") != "room":
        raise ValueError("every Room must have the room role")
    room_id = room.get("id") or properties.get("room_id")
    if not isinstance(room_id, str) or not room_id.strip():
        raise ValueError("every Room must have a non-empty ID")
    geometry = room.get("geometry")
    if not isinstance(geometry, dict):
        raise ValueError("every Room must contain geometry")
    polygons = _geometry_polygons(geometry)
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise ValueError("Room Polygon must contain an outer ring")
        for ring in polygon:
            if (
                not isinstance(ring, list)
                or len(ring) < 4
                or ring[0] != ring[-1]
            ):
                raise ValueError("Room rings must be closed polygons")
            if any(
                not isinstance(point, list)
                or len(point) < 2
                or not all(
                    isinstance(value, (int, float))
                    and math.isfinite(value)
                    for value in point[:2]
                )
                for point in ring
            ):
                raise ValueError("Room coordinates must be finite [x, y]")
    normalized = deepcopy(room)
    normalized["id"] = room_id.strip()
    normalized["properties"]["room_id"] = room_id.strip()
    representative_point, clearance_m = room_representative_point(
        geometry, resolution
    )
    normalized["properties"].update({
        "area_m2": round(_geometry_area(geometry), 2),
        "representative_point": representative_point,
        "clearance_m": clearance_m,
    })
    return normalized


def _rasterize_geometries(
    geometries: list[dict],
    resolution: float,
) -> tuple[list[np.ndarray], LocalTransform]:
    points = [
        point
        for geometry in geometries
        for point in _geometry_points(geometry)
    ]
    if not points:
        raise ValueError("Room geometry contains no points")
    margin = resolution * 4.0
    minimum_x = math.floor(
        (min(point[0] for point in points) - margin) / resolution
    ) * resolution
    maximum_x = math.ceil(
        (max(point[0] for point in points) + margin) / resolution
    ) * resolution
    minimum_y = math.floor(
        (min(point[1] for point in points) - margin) / resolution
    ) * resolution
    maximum_y = math.ceil(
        (max(point[1] for point in points) + margin) / resolution
    ) * resolution
    width = math.ceil((maximum_x - minimum_x) / resolution) + 1
    height = math.ceil((maximum_y - minimum_y) / resolution) + 1
    if width * height > 50_000_000:
        raise ValueError("Room is too large to split safely")
    transform = LocalTransform(minimum_x, maximum_y, resolution)
    masks = []
    for geometry in geometries:
        mask = np.zeros((height, width), dtype=np.uint8)
        for polygon in _geometry_polygons(geometry):
            outer = np.array(
                [transform.pixel(point) for point in polygon[0]],
                dtype=np.int32,
            )
            cv2.fillPoly(mask, [outer], 255)
            for hole in polygon[1:]:
                hole_pixels = np.array(
                    [transform.pixel(point) for point in hole],
                    dtype=np.int32,
                )
                cv2.fillPoly(mask, [hole_pixels], 0)
                # OpenCV includes the contour pixels in fillPoly(). Restore
                # the shared wall boundary so a vector -> mask -> vector
                # round trip does not enlarge every hole by one grid cell.
                cv2.polylines(mask, [hole_pixels], True, 255, 1)
        masks.append(mask)
    return masks, transform


def _cut_room(
    room_mask: np.ndarray,
    transform: LocalTransform,
    line: list,
    minimum_room_area: float,
) -> np.ndarray:
    def valid_point(point: object) -> bool:
        return (
            isinstance(point, (list, tuple))
            and len(point) == 2
            and all(isinstance(value, (int, float)) for value in point)
            and all(math.isfinite(value) for value in point)
        )

    if not isinstance(line, list) or not line:
        raise ValueError("at least one split divider is required")
    dividers = [line] if valid_point(line[0]) else line
    if any(
        not isinstance(divider, list)
        or len(divider) < 2
        or any(not valid_point(point) for point in divider)
        for divider in dividers
    ):
        raise ValueError(
            "each split divider must contain at least two finite points"
        )
    height, width = room_mask.shape
    eroded = cv2.erode(room_mask, np.ones((3, 3), dtype=np.uint8))
    boundary = (room_mask > 0) & (eroded == 0)
    tolerance = max(
        2,
        math.ceil(WALL_SNAP_DISTANCE_METERS / transform.resolution),
    )
    cut_mask = room_mask.copy()
    thickness = max(2, round(0.08 / transform.resolution))
    for source_divider in dividers:
        points = [transform.pixel(point) for point in source_divider]
        if any(
            not (0 <= x < width and 0 <= y < height)
            for x, y in points
        ):
            raise ValueError("split divider points must be near a Room wall")
        divider_points = []
        for index, (x, y) in enumerate(points):
            minimum_x = max(0, x - tolerance)
            maximum_x = min(width, x + tolerance + 1)
            minimum_y = max(0, y - tolerance)
            maximum_y = min(height, y + tolerance + 1)
            candidates = np.argwhere(
                boundary[minimum_y:maximum_y, minimum_x:maximum_x]
            )
            endpoint = index in (0, len(points) - 1)
            if endpoint or room_mask[y, x] == 0:
                if not len(candidates):
                    message = (
                        "split divider endpoints must be near a Room wall"
                        if endpoint
                        else "split divider control points must stay in the Room"
                    )
                    raise ValueError(message)
                distances = (
                    (candidates[:, 1] + minimum_x - x) ** 2
                    + (candidates[:, 0] + minimum_y - y) ** 2
                )
                nearest_y, nearest_x = candidates[int(np.argmin(distances))]
                divider_points.append((
                    int(nearest_x + minimum_x),
                    int(nearest_y + minimum_y),
                ))
            else:
                divider_points.append((x, y))
        segments = [
            (second[0] - first[0], second[1] - first[1])
            for first, second in zip(divider_points, divider_points[1:])
        ]
        lengths = [math.hypot(dx, dy) for dx, dy in segments]
        if any(length < 2.0 for length in lengths):
            raise ValueError("split divider segments are too short")
        extension = thickness + 1
        first_dx, first_dy = segments[0]
        last_dx, last_dy = segments[-1]
        start = (
            round(divider_points[0][0] - first_dx / lengths[0] * extension),
            round(divider_points[0][1] - first_dy / lengths[0] * extension),
        )
        end = (
            round(divider_points[-1][0] + last_dx / lengths[-1] * extension),
            round(divider_points[-1][1] + last_dy / lengths[-1] * extension),
        )
        divider = np.array(
            [start, *divider_points, end], dtype=np.int32
        )
        cv2.polylines(cut_mask, [divider], False, 0, thickness)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        cut_mask, connectivity=8
    )
    minimum_pixels = minimum_room_area / transform.resolution ** 2
    components = [
        component
        for component in range(1, count)
        if stats[component, cv2.CC_STAT_AREA] >= minimum_pixels
    ]
    if len(components) != 2:
        raise ValueError(
            "the divider must cut the selected Room into exactly two "
            "meaningful areas"
        )
    seeds = np.zeros_like(labels, dtype=np.int32)
    for new_label, component in enumerate(components, start=1):
        seeds[labels == component] = new_label
    return _propagate_labels(room_mask, seeds)


def _propagate_labels(free: np.ndarray, seeds: np.ndarray) -> np.ndarray:
    labels = seeds.copy()
    queue = deque(
        (int(y), int(x)) for y, x in np.argwhere(labels > 0)
    )
    neighbors = (
        (-1, 0), (1, 0), (0, -1), (0, 1),
        (-1, -1), (-1, 1), (1, -1), (1, 1),
    )
    height, width = free.shape
    while queue:
        y, x = queue.popleft()
        label = labels[y, x]
        for dy, dx in neighbors:
            next_y = y + dy
            next_x = x + dx
            if not (
                0 <= next_y < height
                and 0 <= next_x < width
                and free[next_y, next_x] > 0
                and labels[next_y, next_x] == 0
            ):
                continue
            labels[next_y, next_x] = label
            queue.append((next_y, next_x))
    return labels


def _ring_from_contour(
    contour: np.ndarray,
    transform: LocalTransform,
    simplify_meters: float,
) -> list[list[float]]:
    if simplify_meters > 0.0:
        epsilon = simplify_meters / transform.resolution
        simplified = cv2.approxPolyDP(contour, epsilon, True)
    else:
        simplified = contour
    ring = [
        transform.world(int(point[0][0]), int(point[0][1]))
        for point in simplified
    ]
    if len(ring) < 3:
        return []
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _mask_geometry(
    mask: np.ndarray,
    transform: LocalTransform,
    simplify_meters: float,
) -> dict:
    contours, hierarchy = cv2.findContours(
        mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
    )
    if hierarchy is None:
        raise ValueError("split result contains no polygon")
    hierarchy = hierarchy[0]
    polygons = []
    for index, contour in enumerate(contours):
        if hierarchy[index][3] != -1:
            continue
        outer = _ring_from_contour(
            contour, transform, simplify_meters
        )
        if not outer:
            continue
        rings = [outer]
        child = hierarchy[index][2]
        while child != -1:
            hole = _ring_from_contour(
                contours[child], transform, simplify_meters
            )
            if hole:
                rings.append(hole)
            child = hierarchy[child][0]
        polygons.append(rings)
    if not polygons:
        raise ValueError("split result contains no usable polygon")
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _split_feature(
    source: dict,
    geometry: dict,
    suffix: str,
    color: str,
    area: float,
    centroid: list[float],
    representative_point: list[float],
    clearance_m: float,
    split_operation_id: str,
    parent_geometry: dict | None,
) -> dict:
    source_id = str(source.get("id") or source["properties"]["room_id"])
    room_id = f"{source_id}-{suffix}"
    properties = dict(source["properties"])
    base_name = properties.get(
        "base_name",
        properties.get("name", source_id),
    )
    parent_path = str(properties.get("split_path", "")).strip()
    child_path = "-".join(filter(None, (
        parent_path, suffix.upper()
    )))
    properties.pop("merged_from", None)
    properties.pop("merged_from_names", None)
    properties.pop("split_parent_geometry", None)
    properties.pop("split_parent_properties", None)
    properties.update({
        "room_id": room_id,
        "name": f"{base_name} {child_path}",
        "base_name": base_name,
        "split_path": child_path,
        "color": color,
        "area_m2": round(area, 2),
        "centroid": centroid,
        "representative_point": representative_point,
        "clearance_m": clearance_m,
        "generated": False,
        "edited": True,
        "split_from": source_id,
        "split_operation_id": split_operation_id,
        "split_parent_id": source_id,
        "split_parent_name": source["properties"].get(
            "name", source_id
        ),
        "split_parent_path": parent_path,
        "split_parent_color": source["properties"].get(
            "color", SPLIT_COLORS[0]
        ),
    })
    if parent_geometry is not None:
        properties["split_parent_geometry"] = deepcopy(parent_geometry)
        properties["split_parent_properties"] = deepcopy(
            source["properties"]
        )
    return {
        "type": "Feature",
        "id": room_id,
        "properties": properties,
        "geometry": geometry,
    }


def split_room_feature(
    room: dict,
    line: list[list[float]],
    resolution: float = 0.05,
    minimum_room_area: float = 1.0,
    simplify_meters: float = 0.0,
) -> list[dict]:
    """Split one Room with independent editable wall-to-wall lines."""
    if room.get("properties", {}).get("role") != "room":
        raise ValueError("selected feature is not a Room")
    if resolution <= 0.0:
        raise ValueError("resolution must be positive")
    if minimum_room_area <= 0.0:
        raise ValueError("minimum_room_area must be positive")
    room_mask, transform = _rasterize_room(room["geometry"], resolution)
    labels = _cut_room(
        room_mask, transform, line, minimum_room_area
    )
    parts = []
    for label in (1, 2):
        mask = np.where(labels == label, 255, 0).astype(np.uint8)
        if label == 1:
            # Adjacent raster regions meet between pixel centers. Extending
            # one side to the neighboring center gives both vector polygons
            # the same shared boundary instead of leaving a visible cell-wide
            # seam. Restricting it to the source Room preserves walls/holes.
            mask = np.where(
                (cv2.dilate(
                    mask, np.ones((3, 3), dtype=np.uint8)
                ) > 0)
                & (room_mask > 0),
                255,
                0,
            ).astype(np.uint8)
        geometry = _mask_geometry(mask, transform, simplify_meters)
        ys, xs = np.where(labels == label)
        centroid = transform.world(
            round(float(xs.mean())), round(float(ys.mean()))
        )
        representative_point, clearance_m = room_representative_point(
            geometry, resolution
        )
        parts.append({
            "geometry": geometry,
            "area": _geometry_area(geometry),
            "centroid": centroid,
            "representative_point": representative_point,
            "clearance_m": clearance_m,
        })
    parts.sort(key=lambda part: (part["centroid"][0], part["centroid"][1]))
    source_color = room["properties"].get("color", SPLIT_COLORS[0])
    color_index = sum(ord(character) for character in str(room.get("id")))
    next_color = SPLIT_COLORS[(color_index + 1) % len(SPLIT_COLORS)]
    if next_color == source_color:
        next_color = SPLIT_COLORS[(color_index + 2) % len(SPLIT_COLORS)]
    operation_value = json.dumps({
        "id": room.get("id"),
        "geometry": room["geometry"],
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    operation_id = hashlib.sha1(operation_value).hexdigest()[:12]
    return [
        _split_feature(
            room,
            parts[0]["geometry"],
            "a",
            source_color,
            parts[0]["area"],
            parts[0]["centroid"],
            parts[0]["representative_point"],
            parts[0]["clearance_m"],
            operation_id,
            room["geometry"],
        ),
        _split_feature(
            room,
            parts[1]["geometry"],
            "b",
            next_color,
            parts[1]["area"],
            parts[1]["centroid"],
            parts[1]["representative_point"],
            parts[1]["clearance_m"],
            operation_id,
            None,
        ),
    ]


def merge_room_features(
    rooms: list[dict],
    resolution: float = 0.05,
    simplify_meters: float = 0.0,
) -> dict:
    """Merge exactly two adjacent Room Features into one Room Feature."""
    if len(rooms) != 2:
        raise ValueError("exactly two Rooms are required for a merge")
    if any(
        room.get("properties", {}).get("role") != "room"
        for room in rooms
    ):
        raise ValueError("all selected features must be Rooms")
    if resolution <= 0.0:
        raise ValueError("resolution must be positive")
    source_ids = [
        str(room.get("id") or room["properties"]["room_id"])
        for room in rooms
    ]
    if source_ids[0] == source_ids[1]:
        raise ValueError("two different Rooms are required for a merge")
    masks, transform = _rasterize_geometries(
        [room["geometry"] for room in rooms], resolution
    )
    combined = cv2.bitwise_or(masks[0], masks[1])
    adjacency_probe = cv2.dilate(
        combined, np.ones((3, 3), dtype=np.uint8)
    )
    component_count, _ = cv2.connectedComponents(
        adjacency_probe, connectivity=8
    )
    if component_count != 2:
        raise ValueError("only adjacent Rooms can be merged")
    operation_ids = {
        room["properties"].get("split_operation_id")
        for room in rooms
    }
    parent_geometry = next((
        room["properties"].get("split_parent_geometry")
        for room in rooms
        if room["properties"].get("split_parent_geometry")
    ), None)
    parent_properties = next((
        room["properties"].get("split_parent_properties")
        for room in rooms
        if room["properties"].get("split_parent_properties")
    ), None)
    restores_split = len(operation_ids) == 1 and None not in operation_ids
    if restores_split and parent_geometry is not None:
        geometry = deepcopy(parent_geometry)
    else:
        sealed = cv2.morphologyEx(
            combined,
            cv2.MORPH_CLOSE,
            np.ones((3, 3), dtype=np.uint8),
        )
        geometry = _mask_geometry(
            sealed, transform, simplify_meters
        )
    ys, xs = np.where(combined > 0)
    centroid = transform.world(
        round(float(xs.mean())), round(float(ys.mean()))
    )
    area = _geometry_area(geometry)
    representative_point, clearance_m = room_representative_point(
        geometry, resolution
    )
    if restores_split:
        room_id = str(rooms[0]["properties"]["split_parent_id"])
    else:
        digest_source = "|".join(sorted(source_ids)).encode("utf-8")
        room_id = "room-merged-" + hashlib.sha1(
            digest_source
        ).hexdigest()[:10]
    properties = (
        deepcopy(parent_properties)
        if restores_split and parent_properties is not None
        else dict(rooms[0]["properties"])
    )
    base_names = [
        room["properties"].get(
            "base_name",
            room["properties"].get("name", source_id),
        )
        for room, source_id in zip(rooms, source_ids)
    ]
    source_names = [
        room["properties"].get("name", source_id)
        for room, source_id in zip(rooms, source_ids)
    ]
    categories = {
        room["properties"].get("category", "unassigned")
        for room in rooms
    }
    if restores_split and parent_properties is not None:
        merged_name = properties.get("name", base_names[0])
        merged_base_name = properties.get("base_name", merged_name)
        parent_color = properties.get("color")
    else:
        properties.pop("split_from", None)
        properties.pop("split_operation_id", None)
        properties.pop("split_parent_id", None)
        properties.pop("split_parent_geometry", None)
        properties.pop("split_parent_properties", None)
        parent_name = properties.pop("split_parent_name", None)
        parent_path = properties.pop("split_parent_path", "")
        properties.pop("split_path", None)
        parent_color = properties.pop("split_parent_color", None)
        merged_name = (
            parent_name
            if restores_split and parent_name
            else base_names[0]
        )
        merged_base_name = base_names[0]
        if restores_split and parent_path:
            properties["split_path"] = parent_path
    properties.update({
        "room_id": room_id,
        "name": merged_name,
        "base_name": merged_base_name,
        "area_m2": round(area, 2),
        "centroid": centroid,
        "representative_point": representative_point,
        "clearance_m": clearance_m,
        "category": categories.pop()
        if len(categories) == 1 else "unassigned",
        "generated": False,
        "edited": True,
        "merged_from": source_ids,
        "merged_from_names": source_names,
    })
    if restores_split and parent_color:
        properties["color"] = parent_color
    return {
        "type": "Feature",
        "id": room_id,
        "properties": properties,
        "geometry": geometry,
    }
