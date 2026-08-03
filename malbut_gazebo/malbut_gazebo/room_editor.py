"""Geometry operations for editing automatically generated Room features."""

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
        masks.append(mask)
    return masks, transform


def _cut_room(
    room_mask: np.ndarray,
    transform: LocalTransform,
    line: list[list[float]],
    minimum_room_area: float,
) -> np.ndarray:
    if len(line) != 2 or any(len(point) != 2 for point in line):
        raise ValueError("split line must contain exactly two [x, y] points")
    first = transform.pixel(line[0])
    second = transform.pixel(line[1])
    height, width = room_mask.shape
    for x, y in (first, second):
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError("split line points must be inside the Room")
        if room_mask[y, x] == 0:
            raise ValueError("split line points must be inside the Room")
    dx = second[0] - first[0]
    dy = second[1] - first[1]
    length = math.hypot(dx, dy)
    if length < 2.0:
        raise ValueError("split line is too short")
    extent = math.hypot(room_mask.shape[0], room_mask.shape[1]) * 2.0
    unit_x = dx / length
    unit_y = dy / length
    start = (
        round(first[0] - unit_x * extent),
        round(first[1] - unit_y * extent),
    )
    end = (
        round(first[0] + unit_x * extent),
        round(first[1] + unit_y * extent),
    )
    cut_mask = room_mask.copy()
    thickness = max(2, round(0.08 / transform.resolution))
    cv2.line(cut_mask, start, end, 0, thickness)
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
    """Split one Room Feature with an infinite divider through two points."""
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
        geometry = _mask_geometry(mask, transform, simplify_meters)
        ys, xs = np.where(labels == label)
        centroid = transform.world(
            round(float(xs.mean())), round(float(ys.mean()))
        )
        parts.append({
            "geometry": geometry,
            "area": _geometry_area(geometry),
            "centroid": centroid,
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
