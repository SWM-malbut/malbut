#!/usr/bin/env python3
"""Create a clean vector User Map from a saved ROS SLAM map."""

from __future__ import annotations

import argparse
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
TRINARY_UNKNOWN_VALUE = 205
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
    map_revision: str
    legacy_map_ids: tuple[str, ...]


def _stable_map_id(
    image: np.ndarray,
    resolution: float,
    origin: list[float],
) -> str:
    """Identify one spatial map independently of occupancy tuning."""
    digest = hashlib.sha256()
    metadata = {
        "shape": list(image.shape),
        "resolution": resolution,
        "origin": [float(value) for value in origin],
    }
    digest.update(json.dumps(
        metadata, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    digest.update(image.tobytes())
    return f"map-{digest.hexdigest()[:12]}"


def _map_revision(
    image: np.ndarray,
    resolution: float,
    origin: list[float],
    negate: bool,
    occupied_threshold: float,
    free_threshold: float,
    mode: str,
) -> str:
    """Identify the exact occupancy interpretation of a spatial map."""
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
    return f"rev-{digest.hexdigest()[:12]}"


def _legacy_map_id(
    image: np.ndarray,
    resolution: float,
    origin: list[float],
    negate: bool,
    occupied_threshold: float,
    free_threshold: float,
    mode: str,
) -> str:
    """Return the pre-revision identity for storage migration."""
    revision = _map_revision(
        image,
        resolution,
        origin,
        negate,
        occupied_threshold,
        free_threshold,
        mode,
    )
    return f"map-{revision.removeprefix('rev-')}"


def _map_id_values(value: object, field: str) -> tuple[str, ...]:
    """Validate optional Malbut map identity metadata."""
    if value is None:
        return ()
    values = value if isinstance(value, list) else [value]
    if not all(
        isinstance(item, str) and item.strip() and len(item.strip()) <= 128
        for item in values
    ):
        raise ValueError(f"{field} must contain non-empty map IDs")
    return tuple(dict.fromkeys(item.strip() for item in values))


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
    stable_id = _stable_map_id(image, resolution, origin)
    configured_ids = _map_id_values(
        metadata.get("malbut_map_id"), "malbut_map_id"
    )
    if len(configured_ids) > 1:
        raise ValueError("malbut_map_id must contain one map ID")
    legacy_id = _legacy_map_id(
        image, resolution, origin, negate,
        occupied_threshold, free_threshold, mode,
    )
    selected_id = map_id.strip() or (
        configured_ids[0] if configured_ids else stable_id
    )
    configured_legacy_ids = _map_id_values(
        metadata.get("malbut_legacy_map_ids"),
        "malbut_legacy_map_ids",
    )
    legacy_ids = tuple(
        identity
        for identity in dict.fromkeys((
            *configured_legacy_ids, legacy_id, stable_id,
        ))
        if identity != selected_id
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
        map_id=selected_id,
        map_revision=_map_revision(
            image, resolution, origin, negate,
            occupied_threshold, free_threshold, mode,
        ),
        legacy_map_ids=legacy_ids,
    )


def _occupancy(slam_map: SlamMap) -> np.ndarray:
    normalized = slam_map.image.astype(np.float32) / 255.0
    return normalized if slam_map.negate else 1.0 - normalized


def _free_mask(slam_map: SlamMap) -> np.ndarray:
    occupancy = _occupancy(slam_map)
    free = occupancy <= slam_map.free_threshold
    # ROS map_saver writes unknown trinary cells as gray 205. Its ROS 2
    # default free threshold (0.25) would otherwise classify that encoded
    # value as free space even though the robot never observed it.
    if slam_map.mode == "trinary":
        free &= slam_map.image != TRINARY_UNKNOWN_VALUE
    return np.where(free, 255, 0).astype(np.uint8)


def _occupied_mask(slam_map: SlamMap) -> np.ndarray:
    occupancy = _occupancy(slam_map)
    return np.where(
        occupancy >= slam_map.occupied_threshold, 255, 0
    ).astype(np.uint8)


def clean_free_space(
    slam_map: SlamMap,
    smoothing_meters: float = 0.0,
    minimum_area: float = 0.4,
) -> np.ndarray:
    """Remove scan noise while preserving meaningful rooms and passages."""
    if smoothing_meters < 0.0:
        raise ValueError("smoothing_meters cannot be negative")
    if minimum_area < 0.0:
        raise ValueError("minimum_area cannot be negative")
    free = _free_mask(slam_map)
    if smoothing_meters > 0.0:
        radius = max(
            1,
            round(smoothing_meters / slam_map.transform.resolution),
        )
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
        # Smoothing may close scan pinholes, but must never turn a confirmed
        # wall into traversable floor. Thin residential partitions are often
        # only a few occupancy-grid pixels wide.
        free[_occupied_mask(slam_map) > 0] = 0

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        free, connectivity=8
    )
    minimum_pixels = minimum_area / slam_map.transform.resolution ** 2
    usable_labels = [
        label for label in range(1, count)
        if stats[label, cv2.CC_STAT_AREA] >= minimum_pixels
    ]
    if not usable_labels:
        raise ValueError("SLAM map contains no usable explored free space")
    # A Room must be one connected, physically reachable floor region. Scan
    # rays can reveal isolated free patches behind walls; retaining those in
    # the initial Room creates a MultiPolygon that cannot be split reliably.
    primary_label = max(
        usable_labels,
        key=lambda label: stats[label, cv2.CC_STAT_AREA],
    )
    return np.where(labels == primary_label, 255, 0).astype(np.uint8)


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
    epsilon = simplify_meters / transform.resolution
    simplified = cv2.approxPolyDP(contour, epsilon, True)
    pixel_points = [
        (float(point[0][0]), float(point[0][1]))
        for point in simplified
    ]
    if simplify_meters > 0.0:
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
    simplify_meters: float = 0.0,
    minimum_floor_area: float = 0.4,
    minimum_hole_area: float = 0.02,
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


def initial_room_feature(
    free: np.ndarray,
    floor_geometry: dict,
    transform: MapTransform,
) -> dict:
    """Create one unassigned Room covering all explored walkable space."""
    distance = cv2.distanceTransform(free, cv2.DIST_L2, 5)
    _, _, _, center = cv2.minMaxLoc(distance)
    representative_point = transform.world(*center)
    clearance = float(distance[center[1], center[0]]) * transform.resolution
    return {
        "type": "Feature",
        "id": "room-1",
        "properties": {
            "role": "room",
            "room_id": "room-1",
            "name": "공간 1",
            "category": "unassigned",
            "color": ROOM_COLORS[0],
            "area_m2": round(_geometry_area(floor_geometry), 2),
            "centroid": representative_point,
            "representative_point": representative_point,
            "clearance_m": round(clearance, 3),
            "generated": True,
        },
        "geometry": floor_geometry,
    }


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
    smoothing_meters: float = 0.0,
    simplify_meters: float = 0.0,
) -> tuple[dict, np.ndarray]:
    """Build User Map GeoJSON and a styled preview from saved SLAM data."""
    free = clean_free_space(slam_map, smoothing_meters)
    floor_geometry, outlines = mask_to_geometry(
        free,
        slam_map.transform,
        simplify_meters=simplify_meters,
    )
    rooms = [initial_room_feature(
        free, floor_geometry, slam_map.transform
    )]
    user_map = {
        "type": "FeatureCollection",
        "format": FORMAT_VERSION,
        "map_id": slam_map.map_id,
        "map_revision": slam_map.map_revision,
        "legacy_map_ids": list(slam_map.legacy_map_ids),
        "frame_id": "map",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "type": "slam_occupancy_grid",
            "map_yaml": slam_map.yaml_path.name,
            "map_image": slam_map.image_path.name,
            "resolution": slam_map.transform.resolution,
            "mode": slam_map.mode,
            "occupied_thresh": slam_map.occupied_threshold,
            "free_thresh": slam_map.free_threshold,
            "width": int(slam_map.image.shape[1]),
            "height": int(slam_map.image.shape[0]),
        },
        "room_segmentation": {
            "method": "single_initial_room",
            "room_count": 1,
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
    parser.add_argument("--smoothing", type=float, default=0.0)
    parser.add_argument("--simplify", type=float, default=0.0)
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
