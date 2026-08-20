#!/usr/bin/env python3
"""Convert semantic Zone GeoJSON into a Nav2 costmap filter mask."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np
import yaml

from malbut_gazebo.user_map_builder import (
    SlamMap,
    _free_mask,
    load_slam_map,
)


ZONE_FORMAT = "malbut-semantic-zones-v1"
DEFAULT_AVOID_COST = 70
DEFAULT_CLEARANCE_COST = 90
DEFAULT_HARD_CLEARANCE = 0.24
DEFAULT_PREFERRED_CLEARANCE = 0.60
DEFAULT_RESTRICTED_BUFFER = 0.20
RESTRICTED_COST = 100
SUPPORTED_BEHAVIORS = {"allow", "avoid", "restricted"}


def load_zones(
    path: Path,
    expected_map_id: str,
    expected_map_revision: str = "",
    accepted_map_ids: tuple[str, ...] = (),
) -> list[dict]:
    """Load and validate semantic Zones for one User Map."""
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"cannot read Zone GeoJSON {path}: {error}"
        ) from error
    return validate_zone_collection(
        value,
        expected_map_id,
        expected_map_revision,
        accepted_map_ids,
    )


def validate_zone_collection(
    value: dict,
    expected_map_id: str,
    expected_map_revision: str = "",
    accepted_map_ids: tuple[str, ...] = (),
) -> list[dict]:
    """Validate one in-memory semantic Zone FeatureCollection."""
    if not isinstance(value, dict):
        raise ValueError("Zone GeoJSON must contain an object")
    if value.get("type") != "FeatureCollection":
        raise ValueError("Zone GeoJSON must be a FeatureCollection")
    if value.get("format") != ZONE_FORMAT:
        raise ValueError(f"Zone GeoJSON format must be {ZONE_FORMAT}")
    if value.get("map_id") not in {expected_map_id, *accepted_map_ids}:
        raise ValueError("Zone map_id does not match the SLAM map")
    revision = value.get("map_revision")
    if (
        revision is not None
        and expected_map_revision
        and revision != expected_map_revision
    ):
        raise ValueError("Zone map_revision does not match the SLAM map")
    if value.get("frame_id", "map") != "map":
        raise ValueError("Zone frame_id must be map")
    features = value.get("features")
    if not isinstance(features, list):
        raise ValueError("Zone GeoJSON features must be an array")
    for feature in features:
        _validate_zone(feature)
    return features


def _validate_zone(zone: dict) -> None:
    if not isinstance(zone, dict) or zone.get("type") != "Feature":
        raise ValueError("every Zone must be a GeoJSON Feature")
    properties = zone.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("every Zone must contain properties")
    if properties.get("role") != "semantic_zone":
        raise ValueError("Zone file contains a non-semantic-zone Feature")
    behavior = properties.get("behavior")
    if behavior not in SUPPORTED_BEHAVIORS:
        raise ValueError(f"unsupported Zone behavior: {behavior}")
    geometry = zone.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        raise ValueError("Zone geometry must be a Polygon")
    rings = geometry.get("coordinates")
    if not isinstance(rings, list) or not rings:
        raise ValueError("Zone Polygon must contain an outer ring")
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 4:
            raise ValueError("Zone rings must contain at least four points")
        if ring[0] != ring[-1]:
            raise ValueError("Zone rings must be closed")
        for point in ring:
            if (
                not isinstance(point, list)
                or len(point) < 2
                or not all(
                    isinstance(value, (int, float))
                    and math.isfinite(value)
                    for value in point[:2]
                )
            ):
                raise ValueError(
                    "Zone coordinates must be finite [x, y] values"
                )


def _ring_pixels(ring: list, slam_map: SlamMap) -> np.ndarray:
    pixels = np.array([
        slam_map.transform.pixel(point[:2]) for point in ring
    ], dtype=np.int32)
    width = slam_map.image.shape[1]
    height = slam_map.image.shape[0]
    if any(
        x < 0 or x >= width or y < 0 or y >= height
        for x, y in pixels
    ):
        raise ValueError("Zone extends outside the SLAM map bounds")
    if abs(cv2.contourArea(pixels)) < 1.0:
        raise ValueError("Zone Polygon has no usable area")
    return pixels


def _zone_mask(zone: dict, slam_map: SlamMap) -> np.ndarray:
    mask = np.zeros(slam_map.image.shape, dtype=np.uint8)
    rings = zone["geometry"]["coordinates"]
    cv2.fillPoly(mask, [_ring_pixels(rings[0], slam_map)], 255)
    for hole in rings[1:]:
        cv2.fillPoly(mask, [_ring_pixels(hole, slam_map)], 0)
    return mask


def _validate_preferred_goals(
    zones: list[dict],
    slam_map: SlamMap,
    result: np.ndarray,
) -> None:
    """Reject representative Zone goals that cannot be driven to safely."""
    height, width = result.shape
    for zone in zones:
        goal = zone["properties"].get("preferred_goal")
        if goal is None:
            continue
        if (
            not isinstance(goal, list)
            or len(goal) != 2
            or not all(
                isinstance(value, (int, float)) and math.isfinite(value)
                for value in goal
            )
        ):
            raise ValueError("Zone preferred_goal must be a finite [x, y]")
        x, y = slam_map.transform.pixel(goal)
        if not 0 <= x < width or not 0 <= y < height:
            raise ValueError("Zone preferred_goal is outside the SLAM map")
        if _zone_mask(zone, slam_map)[y, x] == 0:
            raise ValueError("Zone preferred_goal must stay inside its Zone")
        if result[y, x] >= RESTRICTED_COST:
            raise ValueError("Zone preferred_goal is not safely traversable")


def _clearance_mask(
    slam_map: SlamMap,
    hard_clearance: float,
    preferred_clearance: float,
    clearance_cost: int,
) -> np.ndarray:
    """Build hard and preferred wall-clearance costs for robot centers."""
    if hard_clearance < 0.0:
        raise ValueError("hard clearance cannot be negative")
    if preferred_clearance <= hard_clearance:
        raise ValueError("preferred clearance must exceed hard clearance")
    if not 1 <= clearance_cost < RESTRICTED_COST:
        raise ValueError("clearance cost must be between 1 and 99")

    explored_free = (_free_mask(slam_map) > 0).astype(np.uint8)
    distance = cv2.distanceTransform(
        explored_free, cv2.DIST_L2, 5
    ) * slam_map.transform.resolution

    result = np.zeros(slam_map.image.shape, dtype=np.uint8)
    result[(explored_free == 0) | (distance <= hard_clearance)] = (
        RESTRICTED_COST
    )
    preferred = (
        (distance > hard_clearance)
        & (distance < preferred_clearance)
    )
    if np.any(preferred):
        ratio = (
            (preferred_clearance - distance[preferred])
            / (preferred_clearance - hard_clearance)
        )
        result[preferred] = np.maximum(
            1,
            np.rint(ratio * clearance_cost).astype(np.uint8),
        )
    return result


def build_filter_mask(
    slam_map: SlamMap,
    zones: list[dict],
    avoid_cost: int = DEFAULT_AVOID_COST,
    hard_clearance: float = DEFAULT_HARD_CLEARANCE,
    preferred_clearance: float = DEFAULT_PREFERRED_CLEARANCE,
    clearance_cost: int = DEFAULT_CLEARANCE_COST,
    restricted_buffer: float = DEFAULT_RESTRICTED_BUFFER,
) -> np.ndarray:
    """Rasterize wall clearance and Zone behavior by safety priority."""
    if abs(slam_map.transform.origin_yaw) > 1e-9:
        raise ValueError("Nav2 costmap filter masks require zero origin yaw")
    if not 1 <= avoid_cost < RESTRICTED_COST:
        raise ValueError("avoid cost must be between 1 and 99")
    if restricted_buffer < 0.0:
        raise ValueError("restricted buffer cannot be negative")
    result = _clearance_mask(
        slam_map,
        hard_clearance,
        preferred_clearance,
        clearance_cost,
    )
    costs = {
        "allow": 0,
        "avoid": avoid_cost,
        "restricted": RESTRICTED_COST,
    }
    for zone in zones:
        _validate_zone(zone)
        cost = costs[zone["properties"]["behavior"]]
        if cost == 0:
            continue
        current = _zone_mask(zone, slam_map)
        if (
            zone["properties"]["behavior"] == "restricted"
            and restricted_buffer > 0.0
        ):
            radius = max(
                1,
                int(math.ceil(
                    restricted_buffer / slam_map.transform.resolution
                )),
            )
            size = radius * 2 + 1
            current = cv2.dilate(
                current,
                np.ones((size, size), dtype=np.uint8),
            )
        result[current > 0] = np.maximum(result[current > 0], cost)
    _validate_preferred_goals(zones, slam_map, result)
    return result


def write_filter_mask(
    output_yaml: Path,
    mask: np.ndarray,
    slam_map: SlamMap,
) -> tuple[Path, Path]:
    """Write a raw-mode Nav2 mask image and its map metadata."""
    output_yaml = output_yaml.expanduser()
    if output_yaml.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("output mask path must end in .yaml or .yml")
    output_yaml.parent.mkdir(parents=True, exist_ok=True)
    image_path = output_yaml.with_suffix(".pgm")
    if not cv2.imwrite(str(image_path), mask):
        raise OSError(f"could not write filter mask image {image_path}")
    metadata = {
        "image": image_path.name,
        "mode": "raw",
        "resolution": slam_map.transform.resolution,
        "origin": [
            slam_map.transform.origin_x,
            slam_map.transform.origin_y,
            0.0,
        ],
        "negate": 0,
        "occupied_thresh": 1.0,
        "free_thresh": 0.0,
    }
    output_yaml.write_text(
        yaml.safe_dump(metadata, sort_keys=False),
        encoding="utf-8",
    )
    return output_yaml, image_path


def ensure_filter_mask(
    map_yaml: Path,
    output_yaml: Path,
    map_id: str | None = None,
) -> Path:
    """
    Guarantee one saved revision has a Nav2 filter mask.

    The launch starts the keepout filter servers only when this file
    already exists, and it decides once at start-up. Without a baseline
    mask those servers never run, so the first Zone an owner draws is
    saved and drawn on the map but never reaches the costmap, and the
    robot drives straight through a restricted area.
    """
    output_yaml = Path(output_yaml).expanduser()
    if output_yaml.is_file():
        return output_yaml
    slam_map = load_slam_map(Path(map_yaml).expanduser(), map_id)
    written, _image = write_filter_mask(
        output_yaml, build_filter_mask(slam_map, []), slam_map
    )
    return written


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a Nav2 filter mask from Malbut semantic Zones."
    )
    parser.add_argument("map_yaml", type=Path)
    parser.add_argument("zones_geojson", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--map-id", default="")
    parser.add_argument(
        "--avoid-cost",
        type=int,
        default=DEFAULT_AVOID_COST,
    )
    parser.add_argument(
        "--hard-clearance",
        type=float,
        default=DEFAULT_HARD_CLEARANCE,
        help="minimum robot-center clearance in meters",
    )
    parser.add_argument(
        "--preferred-clearance",
        type=float,
        default=DEFAULT_PREFERRED_CLEARANCE,
        help="wall distance beyond which no preference cost is added",
    )
    parser.add_argument(
        "--clearance-cost",
        type=int,
        default=DEFAULT_CLEARANCE_COST,
        help="maximum traversable cost near the hard-clearance boundary",
    )
    parser.add_argument(
        "--restricted-buffer",
        type=float,
        default=DEFAULT_RESTRICTED_BUFFER,
        help="hard buffer added around restricted Zones in meters",
    )
    return parser.parse_args()


def main() -> int:
    """Run the Zone-to-Nav2-filter conversion command."""
    arguments = _parse_arguments()
    try:
        slam_map = load_slam_map(arguments.map_yaml, arguments.map_id)
        zones = load_zones(
            arguments.zones_geojson,
            slam_map.map_id,
            slam_map.map_revision,
            slam_map.legacy_map_ids,
        )
        mask = build_filter_mask(
            slam_map,
            zones,
            arguments.avoid_cost,
            arguments.hard_clearance,
            arguments.preferred_clearance,
            arguments.clearance_cost,
            arguments.restricted_buffer,
        )
        yaml_path, image_path = write_filter_mask(
            arguments.output, mask, slam_map
        )
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1
    print(
        f"Wrote {yaml_path} and {image_path} "
        f"from {arguments.zones_geojson}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
