#!/usr/bin/env python3
"""Create a clean vector User Map from a saved ROS SLAM map."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np
import yaml


FORMAT_VERSION = "malbut-user-map-v1"
ROOM_COLORS = (
    "#dce8ff",
    "#f9e1c7",
    "#d9f0e3",
    "#eadffd",
    "#f8dce3",
    "#d9edf2",
    "#f3eabf",
    "#dfe4eb",
)


@dataclass(frozen=True)
class MapTransform:
    """Convert occupancy-image pixels into the ROS map frame."""

    origin_x: float
    origin_y: float
    origin_yaw: float
    resolution: float
    height: int

    def world(self, x: float, y: float) -> list[float]:
        """Convert a top-left-origin image pixel to a map coordinate."""
        local_x = (x + 0.5) * self.resolution
        local_y = (self.height - y - 0.5) * self.resolution
        cosine = math.cos(self.origin_yaw)
        sine = math.sin(self.origin_yaw)
        return [
            round(
                self.origin_x + cosine * local_x - sine * local_y,
                4,
            ),
            round(
                self.origin_y + sine * local_x + cosine * local_y,
                4,
            ),
        ]

    def pixel(self, point: list[float]) -> tuple[int, int]:
        """Convert a map coordinate back to a top-left-origin pixel."""
        dx = point[0] - self.origin_x
        dy = point[1] - self.origin_y
        cosine = math.cos(self.origin_yaw)
        sine = math.sin(self.origin_yaw)
        local_x = cosine * dx + sine * dy
        local_y = -sine * dx + cosine * dy
        return (
            round(local_x / self.resolution - 0.5),
            round(self.height - local_y / self.resolution - 0.5),
        )


@dataclass(frozen=True)
class SlamMap:
    """A saved ROS occupancy map and its coordinate metadata."""

    yaml_path: Path
    image_path: Path
    image: np.ndarray
    transform: MapTransform
    occupied_threshold: float
    free_threshold: float
    negate: bool
    mode: str
    map_id: str


def _stable_map_id(
    image: np.ndarray,
    resolution: float,
    origin: list[float],
    negate: bool,
    occupied_threshold: float,
    free_threshold: float,
    mode: str,
) -> str:
    """Identify map content independently of filenames and YAML formatting."""
    digest = hashlib.sha256()
    metadata = {
        "shape": list(image.shape),
        "resolution": resolution,
        "origin": [float(value) for value in origin],
        "negate": negate,
        "occupied_thresh": occupied_threshold,
        "free_thresh": free_threshold,
        "mode": mode,
    }
    digest.update(json.dumps(
        metadata, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    digest.update(image.tobytes())
    return f"map-{digest.hexdigest()[:12]}"


def load_slam_map(yaml_path: Path, map_id: str = "") -> SlamMap:
    """Load a map_server YAML file and its referenced occupancy image."""
    yaml_path = yaml_path.expanduser().resolve()
    try:
        metadata = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError(
            f"cannot read ROS map YAML {yaml_path}: {error}"
        ) from error
    if not isinstance(metadata, dict):
        raise ValueError("ROS map YAML must contain a mapping")
    required = {"image", "resolution", "origin"}
    missing = sorted(required - metadata.keys())
    if missing:
        raise ValueError(f"ROS map YAML is missing: {', '.join(missing)}")
    image_path = Path(str(metadata["image"])).expanduser()
    if not image_path.is_absolute():
        image_path = yaml_path.parent / image_path
    image_path = image_path.resolve()
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"cannot read occupancy image {image_path}")
    resolution = float(metadata["resolution"])
    origin = metadata["origin"]
    if resolution <= 0.0:
        raise ValueError("map resolution must be positive")
    if not isinstance(origin, list) or len(origin) != 3:
        raise ValueError("map origin must be [x, y, yaw]")
    occupied_threshold = float(metadata.get("occupied_thresh", 0.65))
    free_threshold = float(metadata.get("free_thresh", 0.196))
    if not 0.0 <= free_threshold < occupied_threshold <= 1.0:
        raise ValueError("invalid free/occupied thresholds")
    negate_value = metadata.get("negate", 0)
    if isinstance(negate_value, bool):
        negate = negate_value
    elif isinstance(negate_value, int) and negate_value in (0, 1):
        negate = bool(negate_value)
    else:
        raise ValueError("map negate must be 0 or 1")
    mode = str(metadata.get("mode", "trinary")).strip().lower()
    if mode != "trinary":
        raise ValueError("only ROS map mode 'trinary' is supported")
    stable_id = _stable_map_id(
        image,
        resolution,
        origin,
        negate,
        occupied_threshold,
        free_threshold,
        mode,
    )
    return SlamMap(
        yaml_path=yaml_path,
        image_path=image_path,
        image=image,
        transform=MapTransform(
            float(origin[0]),
            float(origin[1]),
            float(origin[2]),
            resolution,
            image.shape[0],
        ),
        occupied_threshold=occupied_threshold,
        free_threshold=free_threshold,
        negate=negate,
        mode=mode,
        map_id=map_id.strip() or stable_id,
    )


def _occupancy(slam_map: SlamMap) -> np.ndarray:
    normalized = slam_map.image.astype(np.float32) / 255.0
    return normalized if slam_map.negate else 1.0 - normalized


def _free_mask(slam_map: SlamMap) -> np.ndarray:
    occupancy = _occupancy(slam_map)
    return np.where(
        occupancy <= slam_map.free_threshold, 255, 0
    ).astype(np.uint8)


def _occupied_mask(slam_map: SlamMap) -> np.ndarray:
    occupancy = _occupancy(slam_map)
    return np.where(
        occupancy >= slam_map.occupied_threshold, 255, 0
    ).astype(np.uint8)


def clean_free_space(
    slam_map: SlamMap,
    smoothing_meters: float = 0.12,
    minimum_area: float = 0.4,
) -> np.ndarray:
    """Remove scan noise while preserving meaningful rooms and passages."""
    if smoothing_meters < 0.0:
        raise ValueError("smoothing_meters cannot be negative")
    if minimum_area < 0.0:
        raise ValueError("minimum_area cannot be negative")
    free = _free_mask(slam_map)
    radius = max(1, round(smoothing_meters / slam_map.transform.resolution))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (radius * 2 + 1, radius * 2 + 1),
    )
    free = cv2.morphologyEx(free, cv2.MORPH_CLOSE, kernel)
    open_radius = max(1, round(radius / 2))
    open_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (open_radius * 2 + 1, open_radius * 2 + 1),
    )
    free = cv2.morphologyEx(free, cv2.MORPH_OPEN, open_kernel)
    # Smoothing may close scan pinholes, but must never turn a confirmed wall
    # into traversable floor. Thin residential partitions are often only a
    # few occupancy-grid pixels wide.
    free[_occupied_mask(slam_map) > 0] = 0

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        free, connectivity=8
    )
    cleaned = np.zeros_like(free)
    minimum_pixels = minimum_area / slam_map.transform.resolution ** 2
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= minimum_pixels:
            cleaned[labels == label] = 255
    if not np.any(cleaned):
        raise ValueError("SLAM map contains no usable explored free space")
    return cleaned


def _dominant_wall_angle(mask: np.ndarray) -> float:
    edges = cv2.Canny(mask, 50, 150)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180.0,
        threshold=30,
        minLineLength=20,
        maxLineGap=8,
    )
    if lines is None:
        return 0.0
    sine_sum = 0.0
    cosine_sum = 0.0
    for x1, y1, x2, y2 in lines[:, 0]:
        angle = math.atan2(y2 - y1, x2 - x1)
        length = math.hypot(x2 - x1, y2 - y1)
        sine_sum += length * math.sin(4.0 * angle)
        cosine_sum += length * math.cos(4.0 * angle)
    return math.atan2(sine_sum, cosine_sum) / 4.0


def _snap_ring(
    points: list[tuple[float, float]],
    dominant_angle: float,
    tolerance_degrees: float = 12.0,
) -> list[tuple[float, float]]:
    if len(points) < 4:
        return points
    cosine = math.cos(-dominant_angle)
    sine = math.sin(-dominant_angle)
    rotated = [
        (cosine * x - sine * y, sine * x + cosine * y)
        for x, y in points
    ]
    orientations = []
    offsets = []
    tolerance = math.radians(tolerance_degrees)
    for index, first in enumerate(rotated):
        second = rotated[(index + 1) % len(rotated)]
        angle = abs(math.atan2(
            second[1] - first[1], second[0] - first[0]
        ))
        horizontal_error = min(angle, abs(math.pi - angle))
        vertical_error = abs(math.pi / 2.0 - angle)
        if horizontal_error <= tolerance:
            orientations.append("horizontal")
            offsets.append((first[1] + second[1]) / 2.0)
        elif vertical_error <= tolerance:
            orientations.append("vertical")
            offsets.append((first[0] + second[0]) / 2.0)
        else:
            orientations.append("free")
            offsets.append(0.0)

    snapped = []
    for index, point in enumerate(rotated):
        previous = (index - 1) % len(rotated)
        previous_kind = orientations[previous]
        next_kind = orientations[index]
        x, y = point
        if previous_kind == "vertical":
            x = offsets[previous]
        if next_kind == "vertical":
            x = offsets[index]
        if previous_kind == "horizontal":
            y = offsets[previous]
        if next_kind == "horizontal":
            y = offsets[index]
        snapped.append((x, y))

    cosine = math.cos(dominant_angle)
    sine = math.sin(dominant_angle)
    return [
        (cosine * x - sine * y, sine * x + cosine * y)
        for x, y in snapped
    ]


def _ring_from_contour(
    contour: np.ndarray,
    transform: MapTransform,
    simplify_meters: float,
    dominant_angle: float,
) -> list[list[float]]:
    epsilon = max(0.5, simplify_meters / transform.resolution)
    simplified = cv2.approxPolyDP(contour, epsilon, True)
    pixel_points = [
        (float(point[0][0]), float(point[0][1]))
        for point in simplified
    ]
    pixel_points = _snap_ring(pixel_points, dominant_angle)
    ring = [transform.world(x, y) for x, y in pixel_points]
    if len(ring) < 3:
        return []
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def mask_to_geometry(
    mask: np.ndarray,
    transform: MapTransform,
    simplify_meters: float = 0.08,
    minimum_floor_area: float = 0.4,
    minimum_hole_area: float = 0.20,
) -> tuple[dict, list[list[list[float]]]]:
    """Convert cleaned free space to a simplified vector floor plan."""
    contours, hierarchy = cv2.findContours(
        mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
    )
    if hierarchy is None:
        raise ValueError("no floor contour was detected")
    hierarchy = hierarchy[0]
    dominant_angle = _dominant_wall_angle(mask)
    polygons = []
    outlines = []
    for index, contour in enumerate(contours):
        if hierarchy[index][3] != -1:
            continue
        outer_area = abs(cv2.contourArea(contour)) * transform.resolution ** 2
        if outer_area < minimum_floor_area:
            continue
        outer = _ring_from_contour(
            contour, transform, simplify_meters, dominant_angle
        )
        if not outer:
            continue
        rings = [outer]
        outlines.append(outer)
        child = hierarchy[index][2]
        while child != -1:
            hole_area = abs(cv2.contourArea(contours[child]))
            hole_area *= transform.resolution ** 2
            _, _, width, height = cv2.boundingRect(contours[child])
            longest_side = max(width, height) * transform.resolution
            if hole_area >= minimum_hole_area or longest_side >= 0.8:
                hole = _ring_from_contour(
                    contours[child],
                    transform,
                    simplify_meters,
                    dominant_angle,
                )
                if hole:
                    rings.append(hole)
                    outlines.append(hole)
            child = hierarchy[child][0]
        polygons.append(rings)
    if not polygons:
        raise ValueError("no usable floor polygon was detected")
    if len(polygons) == 1:
        geometry = {"type": "Polygon", "coordinates": polygons[0]}
    else:
        geometry = {"type": "MultiPolygon", "coordinates": polygons}
    return geometry, outlines


def _ring_area(ring: list[list[float]]) -> float:
    return abs(sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(ring, ring[1:])
    )) / 2.0


def _geometry_area(geometry: dict) -> float:
    polygons = (
        [geometry["coordinates"]]
        if geometry["type"] == "Polygon"
        else geometry["coordinates"]
    )
    return sum(
        _ring_area(polygon[0])
        - sum(_ring_area(hole) for hole in polygon[1:])
        for polygon in polygons
    )


def _ensure_seed_per_free_component(
    free: np.ndarray,
    seeds: np.ndarray,
    next_label: int,
) -> int:
    component_count, components = cv2.connectedComponents(
        free, connectivity=8
    )
    distance = cv2.distanceTransform(free, cv2.DIST_L2, 5)
    for component in range(1, component_count):
        component_mask = components == component
        if np.any(seeds[component_mask] > 0):
            continue
        candidates = np.where(component_mask, distance, -1.0)
        y, x = np.unravel_index(
            int(np.argmax(candidates)), candidates.shape
        )
        seeds[y, x] = next_label
        next_label += 1
    return next_label


def _propagate_room_seeds(
    free: np.ndarray,
    seeds: np.ndarray,
) -> np.ndarray:
    labels = seeds.copy()
    queue = deque(
        (int(y), int(x))
        for y, x in np.argwhere(labels > 0)
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


def _merge_small_rooms(
    labels: np.ndarray,
    minimum_pixels: int,
) -> np.ndarray:
    labels = labels.copy()
    while True:
        room_labels, counts = np.unique(
            labels[labels > 0], return_counts=True
        )
        small_rooms = [
            int(label)
            for label, count in zip(room_labels, counts)
            if count < minimum_pixels
        ]
        if not small_rooms:
            break
        merged = False
        for label in small_rooms:
            room = np.where(labels == label, 255, 0).astype(np.uint8)
            border = cv2.dilate(room, np.ones((3, 3), np.uint8)) > 0
            adjacent = labels[border & (labels != label) & (labels > 0)]
            if adjacent.size == 0:
                continue
            neighbors, contacts = np.unique(adjacent, return_counts=True)
            destination = int(neighbors[int(np.argmax(contacts))])
            labels[labels == label] = destination
            merged = True
        if not merged:
            break
    return labels


def segment_rooms(
    free: np.ndarray,
    transform: MapTransform,
    doorway_width: float = 0.80,
    minimum_room_area: float = 1.5,
) -> np.ndarray:
    """Partition free space using doorway-width erosion and propagation."""
    if doorway_width <= 0.0:
        raise ValueError("doorway_width must be positive")
    if minimum_room_area <= 0.0:
        raise ValueError("minimum_room_area must be positive")
    radius = max(1, round(
        doorway_width / (2.0 * transform.resolution)
    ))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (radius * 2 + 1, radius * 2 + 1),
    )
    room_cores = cv2.erode(free, kernel)
    component_count, components, stats, _ = (
        cv2.connectedComponentsWithStats(room_cores, connectivity=8)
    )
    seeds = np.zeros_like(components, dtype=np.int32)
    minimum_core_pixels = max(
        1,
        round(0.15 / transform.resolution ** 2),
    )
    next_label = 1
    for component in range(1, component_count):
        if stats[component, cv2.CC_STAT_AREA] < minimum_core_pixels:
            continue
        seeds[components == component] = next_label
        next_label += 1
    _ensure_seed_per_free_component(free, seeds, next_label)
    labels = _propagate_room_seeds(free, seeds)
    minimum_pixels = round(
        minimum_room_area / transform.resolution ** 2
    )
    labels = _merge_small_rooms(labels, minimum_pixels)

    ordered = []
    for label in np.unique(labels[labels > 0]):
        ys, xs = np.where(labels == label)
        ordered.append((float(xs.mean()), float(ys.mean()), int(label)))
    relabeled = np.zeros_like(labels)
    for new_label, (_, _, old_label) in enumerate(
        sorted(ordered, key=lambda value: (value[1], value[0])),
        start=1,
    ):
        relabeled[labels == old_label] = new_label
    return relabeled


def room_features(
    room_labels: np.ndarray,
    transform: MapTransform,
    simplify_meters: float,
) -> list[dict]:
    """Convert raster room labels into stable GeoJSON room features."""
    features = []
    for room_number in np.unique(room_labels[room_labels > 0]):
        room_mask = np.where(
            room_labels == room_number, 255, 0
        ).astype(np.uint8)
        geometry, _ = mask_to_geometry(
            room_mask,
            transform,
            simplify_meters=simplify_meters,
            minimum_floor_area=0.05,
            minimum_hole_area=0.20,
        )
        ys, xs = np.where(room_labels == room_number)
        centroid = transform.world(float(xs.mean()), float(ys.mean()))
        features.append({
            "type": "Feature",
            "id": f"room-{room_number}",
            "properties": {
                "role": "room",
                "room_id": f"room-{room_number}",
                "name": f"공간 {room_number}",
                "category": "unassigned",
                "color": ROOM_COLORS[
                    (int(room_number) - 1) % len(ROOM_COLORS)
                ],
                "area_m2": round(_geometry_area(geometry), 2),
                "centroid": centroid,
                "generated": True,
            },
            "geometry": geometry,
        })
    return features


def render_preview(
    floor_geometry: dict,
    outlines: list[list[list[float]]],
    slam_map: SlamMap,
    rooms: list[dict] = (),
) -> np.ndarray:
    """Render the final vector plan in the same extent as the SLAM image."""
    shape = slam_map.image.shape
    canvas = np.full((*shape, 3), (240, 243, 245), dtype=np.uint8)
    polygons = (
        [floor_geometry["coordinates"]]
        if floor_geometry["type"] == "Polygon"
        else floor_geometry["coordinates"]
    )
    for polygon in polygons:
        outer = np.array([
            slam_map.transform.pixel(point) for point in polygon[0]
        ], dtype=np.int32)
        cv2.fillPoly(canvas, [outer], (250, 252, 253))
        for hole in polygon[1:]:
            hole_pixels = np.array([
                slam_map.transform.pixel(point) for point in hole
            ], dtype=np.int32)
            cv2.fillPoly(canvas, [hole_pixels], (240, 243, 245))
    for room in rooms:
        color = room["properties"]["color"].lstrip("#")
        red, green, blue = (
            int(color[index:index + 2], 16) for index in (0, 2, 4)
        )
        geometry = room["geometry"]
        room_polygons = (
            [geometry["coordinates"]]
            if geometry["type"] == "Polygon"
            else geometry["coordinates"]
        )
        for polygon in room_polygons:
            outer = np.array([
                slam_map.transform.pixel(point) for point in polygon[0]
            ], dtype=np.int32)
            cv2.fillPoly(canvas, [outer], (blue, green, red))
            cv2.polylines(canvas, [outer], True, (150, 157, 164), 1)
            for hole in polygon[1:]:
                hole_pixels = np.array([
                    slam_map.transform.pixel(point) for point in hole
                ], dtype=np.int32)
                cv2.fillPoly(canvas, [hole_pixels], (240, 243, 245))
    for outline in outlines:
        pixels = np.array([
            slam_map.transform.pixel(point) for point in outline
        ], dtype=np.int32)
        cv2.polylines(canvas, [pixels], True, (48, 59, 70), 2)
    for room in rooms:
        center = slam_map.transform.pixel(
            room["properties"]["centroid"]
        )
        label = room["properties"]["room_id"].split("-")[-1]
        cv2.circle(canvas, center, 11, (255, 255, 255), -1)
        cv2.circle(canvas, center, 11, (74, 84, 94), 1)
        text_size, _ = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1
        )
        cv2.putText(
            canvas,
            label,
            (
                center[0] - text_size[0] // 2,
                center[1] + text_size[1] // 2,
            ),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (48, 59, 70),
            1,
            cv2.LINE_AA,
        )
    return canvas


def build_user_map(
    slam_map: SlamMap,
    smoothing_meters: float = 0.12,
    simplify_meters: float = 0.08,
    doorway_width: float = 0.80,
    minimum_room_area: float = 1.5,
) -> tuple[dict, np.ndarray]:
    """Build User Map GeoJSON and a styled preview from saved SLAM data."""
    free = clean_free_space(slam_map, smoothing_meters)
    floor_geometry, outlines = mask_to_geometry(
        free,
        slam_map.transform,
        simplify_meters=simplify_meters,
    )
    labels = segment_rooms(
        free,
        slam_map.transform,
        doorway_width=doorway_width,
        minimum_room_area=minimum_room_area,
    )
    rooms = room_features(labels, slam_map.transform, simplify_meters)
    user_map = {
        "type": "FeatureCollection",
        "format": FORMAT_VERSION,
        "map_id": slam_map.map_id,
        "frame_id": "map",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "type": "slam_occupancy_grid",
            "map_yaml": slam_map.yaml_path.name,
            "map_image": slam_map.image_path.name,
            "resolution": slam_map.transform.resolution,
            "mode": slam_map.mode,
            "width": int(slam_map.image.shape[1]),
            "height": int(slam_map.image.shape[0]),
        },
        "room_segmentation": {
            "method": "doorway_erosion_geodesic_propagation",
            "doorway_width": doorway_width,
            "minimum_room_area": minimum_room_area,
            "room_count": len(rooms),
        },
        "features": [
            {
                "type": "Feature",
                "id": "walkable-area",
                "properties": {
                    "role": "walkable_area",
                    "name": "우리 집",
                },
                "geometry": floor_geometry,
            },
            {
                "type": "Feature",
                "id": "wall-outline",
                "properties": {"role": "wall_outline"},
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": outlines,
                },
            },
        ] + rooms,
    }
    return user_map, render_preview(
        floor_geometry, outlines, slam_map, rooms
    )


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a clean vector User Map from a saved ROS SLAM map."
    )
    parser.add_argument("map_yaml", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--map-id", default="")
    parser.add_argument("--smoothing", type=float, default=0.12)
    parser.add_argument("--simplify", type=float, default=0.08)
    parser.add_argument("--doorway-width", type=float, default=0.80)
    parser.add_argument("--minimum-room-area", type=float, default=1.5)
    return parser.parse_args()


def main() -> int:
    """Run the saved-SLAM-map conversion command."""
    arguments = _parse_arguments()
    try:
        slam_map = load_slam_map(arguments.map_yaml, arguments.map_id)
        user_map, preview = build_user_map(
            slam_map,
            smoothing_meters=arguments.smoothing,
            simplify_meters=arguments.simplify,
            doorway_width=arguments.doorway_width,
            minimum_room_area=arguments.minimum_room_area,
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(user_map, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if arguments.preview is not None:
            arguments.preview.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(arguments.preview), preview):
                raise OSError(
                    f"could not write preview to {arguments.preview}"
                )
    except (KeyError, OSError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1
    print(f"Wrote {arguments.output} from {arguments.map_yaml}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
