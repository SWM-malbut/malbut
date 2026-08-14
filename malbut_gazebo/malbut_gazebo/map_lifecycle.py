"""Pure map exploration and revision-persistence helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable

import cv2
import numpy as np
import yaml

from malbut_gazebo.user_map_builder import build_user_map, load_slam_map


MAP_STORE_FORMAT = "malbut-map-store/v1"
ACTIVE_MANIFEST = "active.json"
FREE_THRESHOLD = 0.196
OCCUPIED_THRESHOLD = 0.65


@dataclass(frozen=True)
class MapGrid:
    """Immutable occupancy-grid snapshot in ROS map coordinates."""

    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    cells: np.ndarray
    stamp_ns: int = 0

    def world(self, row: int, column: int) -> tuple[float, float]:
        """Return the center of one grid cell in the map frame."""
        local_x = (column + 0.5) * self.resolution
        local_y = (row + 0.5) * self.resolution
        cosine = math.cos(self.origin_yaw)
        sine = math.sin(self.origin_yaw)
        return (
            self.origin_x + cosine * local_x - sine * local_y,
            self.origin_y + sine * local_x + cosine * local_y,
        )


@dataclass(frozen=True)
class Frontier:
    """One navigable boundary between known and unknown map cells."""

    x: float
    y: float
    yaw: float
    cell_count: int
    clearance_m: float
    distance_m: float


def map_grid_from_message(message: object) -> MapGrid:
    """Copy a ROS OccupancyGrid-like message into an immutable snapshot."""
    width = int(message.info.width)
    height = int(message.info.height)
    cells = np.asarray(message.data, dtype=np.int16)
    if width <= 0 or height <= 0 or cells.size != width * height:
        raise ValueError("invalid occupancy-grid dimensions")
    orientation = message.info.origin.orientation
    yaw = math.atan2(
        2.0 * (
            orientation.w * orientation.z
            + orientation.x * orientation.y
        ),
        1.0 - 2.0 * (
            orientation.y * orientation.y
            + orientation.z * orientation.z
        ),
    )
    stamp = getattr(message.header, "stamp", None)
    stamp_ns = 0
    if stamp is not None:
        stamp_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
    cells = cells.reshape((height, width)).copy()
    cells.setflags(write=False)
    return MapGrid(
        width=width,
        height=height,
        resolution=float(message.info.resolution),
        origin_x=float(message.info.origin.position.x),
        origin_y=float(message.info.origin.position.y),
        origin_yaw=yaw,
        cells=cells,
        stamp_ns=stamp_ns,
    )


def map_statistics(grid: MapGrid) -> dict:
    """Return progress indicators without claiming a false floor-plan total."""
    known = grid.cells >= 0
    free = (grid.cells >= 0) & (grid.cells <= 19)
    occupied = grid.cells >= 65
    cell_area = grid.resolution * grid.resolution
    return {
        "known_area_m2": round(float(np.count_nonzero(known)) * cell_area, 2),
        "free_area_m2": round(float(np.count_nonzero(free)) * cell_area, 2),
        "occupied_area_m2": round(
            float(np.count_nonzero(occupied)) * cell_area, 2
        ),
        "known_cells": int(np.count_nonzero(known)),
    }


def find_frontiers(
    grid: MapGrid,
    robot_xy: tuple[float, float] | None,
    *,
    minimum_cells: int = 8,
    minimum_clearance_m: float = 0.30,
    minimum_goal_distance_m: float = 0.45,
    blacklisted: tuple[tuple[float, float], ...] = (),
) -> list[Frontier]:
    """Find safe, connected frontier clusters sorted by utility."""
    free = ((grid.cells >= 0) & (grid.cells <= 19)).astype(np.uint8)
    unknown = (grid.cells < 0).astype(np.uint8)
    neighborhood = np.ones((3, 3), dtype=np.uint8)
    frontier_mask = free & cv2.dilate(unknown, neighborhood, iterations=1)
    count, labels, statistics, _centroids = cv2.connectedComponentsWithStats(
        frontier_mask, connectivity=8
    )
    clearance = (
        cv2.distanceTransform(free, cv2.DIST_L2, 5)
        * grid.resolution
    )
    candidates = []
    for label in range(1, count):
        cell_count = int(statistics[label, cv2.CC_STAT_AREA])
        if cell_count < minimum_cells:
            continue
        cluster = (labels == label).astype(np.uint8)
        approach_cells = max(
            1, int(math.ceil(0.60 / grid.resolution))
        )
        approach = cv2.dilate(
            cluster,
            cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE,
                (approach_cells * 2 + 1, approach_cells * 2 + 1),
            ),
            iterations=1,
        )
        safe_approach = (
            (approach > 0)
            & (free > 0)
            & (clearance >= minimum_clearance_m)
        )
        rows, columns = np.where(safe_approach)
        if rows.size == 0:
            continue
        if robot_xy is None:
            distances = np.zeros(rows.shape, dtype=np.float64)
        else:
            points = [grid.world(int(row), int(column)) for row, column in zip(
                rows, columns
            )]
            distances = np.asarray([
                math.hypot(x - robot_xy[0], y - robot_xy[1])
                for x, y in points
            ])
        eligible = distances >= minimum_goal_distance_m
        if not np.any(eligible):
            continue
        eligible_indices = np.flatnonzero(eligible)
        cluster_rows, cluster_columns = np.where(cluster)
        center_row = float(np.mean(cluster_rows))
        center_column = float(np.mean(cluster_columns))
        frontier_distance_cells = np.hypot(
            rows[eligible] - center_row,
            columns[eligible] - center_column,
        )
        # Stay inside known space while remaining close enough to observe it.
        utility = (
            clearance[rows[eligible], columns[eligible]]
            - 0.05 * distances[eligible]
            - 0.02 * frontier_distance_cells
        )
        selected = eligible_indices[int(np.argmax(utility))]
        row = int(rows[selected])
        column = int(columns[selected])
        x, y = grid.world(row, column)
        if any(math.hypot(x - bx, y - by) < 0.75 for bx, by in blacklisted):
            continue
        unknown_rows, unknown_columns = np.where(
            unknown
            & cv2.dilate(
                (labels == label).astype(np.uint8),
                neighborhood,
                iterations=1,
            )
        )
        if unknown_rows.size:
            unknown_x, unknown_y = grid.world(
                int(round(float(np.mean(unknown_rows)))),
                int(round(float(np.mean(unknown_columns)))),
            )
            yaw = math.atan2(unknown_y - y, unknown_x - x)
        elif robot_xy is not None:
            yaw = math.atan2(y - robot_xy[1], x - robot_xy[0])
        else:
            yaw = 0.0
        distance = float(distances[selected]) if robot_xy is not None else 0.0
        candidates.append(Frontier(
            x=x,
            y=y,
            yaw=yaw,
            cell_count=cell_count,
            clearance_m=float(clearance[row, column]),
            distance_m=distance,
        ))
    return sorted(
        candidates,
        key=lambda item: (-min(item.cell_count, 200), item.distance_m),
    )


def render_map_png(grid: MapGrid) -> bytes:
    """Render a user-facing occupancy map without costmap inflation shadows."""
    image = np.empty((grid.height, grid.width, 3), dtype=np.uint8)
    image[:] = (247, 242, 247)
    image[(grid.cells >= 0) & (grid.cells <= 19)] = (255, 255, 255)
    image[grid.cells >= 65] = (39, 31, 25)
    intermediate = (grid.cells > 19) & (grid.cells < 65)
    image[intermediate] = (205, 205, 205)
    image = np.flipud(image)
    success, encoded = cv2.imencode(".png", image)
    if not success:
        raise OSError("could not encode map preview")
    return encoded.tobytes()


def _pgm_bytes(grid: MapGrid) -> bytes:
    pixels = np.full((grid.height, grid.width), 205, dtype=np.uint8)
    pixels[(grid.cells >= 0) & (grid.cells <= 19)] = 254
    pixels[grid.cells >= 65] = 0
    pixels = np.flipud(pixels)
    header = (
        "P5\n# CREATOR: Malbut map lifecycle\n"
        f"{grid.width} {grid.height}\n255\n"
    ).encode("ascii")
    return header + pixels.tobytes()


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_active_path(store: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = (store / value).resolve()
    try:
        candidate.relative_to(store.resolve())
    except ValueError:
        return None
    return candidate


def load_active_revision(store: Path) -> dict | None:
    """Return the active, internally consistent map revision if one exists."""
    manifest_path = store.expanduser() / ACTIVE_MANIFEST
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if value.get("format") != MAP_STORE_FORMAT:
        return None
    for field in ("map_yaml", "map_image", "user_map"):
        path = _safe_active_path(store, value.get(field))
        if path is None or not path.is_file():
            return None
    return value


def persist_map_revision(
    grid: MapGrid,
    store: Path,
    *,
    initial_pose: dict | None = None,
    posegraph_writer: Callable[[Path], bool] | None = None,
) -> dict:
    """Stage a complete revision, then atomically make it active."""
    if map_statistics(grid)["free_area_m2"] < 1.0:
        raise ValueError("저장할 수 있는 주행 가능 공간이 충분하지 않습니다.")
    normalized_pose = None
    if initial_pose is not None:
        try:
            normalized_pose = {
                name: float(initial_pose[name])
                for name in ("x", "y", "yaw")
            }
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("저장할 로봇 위치가 올바르지 않습니다.") from error
        if not all(math.isfinite(value) for value in normalized_pose.values()):
            raise ValueError("저장할 로봇 위치가 유한한 값이 아닙니다.")
    store = store.expanduser().resolve()
    versions = store / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=versions))
    warnings = []
    try:
        image_path = staging / "map.pgm"
        yaml_path = staging / "map.yaml"
        image_path.write_bytes(_pgm_bytes(grid))
        yaml_value = {
            "image": image_path.name,
            "mode": "trinary",
            "resolution": grid.resolution,
            "origin": [grid.origin_x, grid.origin_y, grid.origin_yaw],
            "negate": 0,
            "occupied_thresh": OCCUPIED_THRESHOLD,
            "free_thresh": FREE_THRESHOLD,
        }
        yaml_path.write_text(
            yaml.safe_dump(yaml_value, sort_keys=False), encoding="utf-8"
        )
        slam_map = load_slam_map(yaml_path)
        user_map, preview = build_user_map(slam_map)
        user_map_path = staging / "user-map.geojson"
        user_map_path.write_text(
            json.dumps(user_map, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        preview_path = staging / "preview.png"
        if not cv2.imwrite(str(preview_path), preview):
            raise OSError("could not write User Map preview")
        posegraph_base = staging / "posegraph"
        posegraph_saved = False
        if posegraph_writer is not None:
            posegraph_saved = bool(posegraph_writer(posegraph_base))
            if not posegraph_saved:
                warnings.append("SLAM pose graph를 저장하지 못했습니다.")
        created_at = datetime.now(timezone.utc)
        digest = hashlib.sha256(
            image_path.read_bytes() + slam_map.map_revision.encode("ascii")
        ).hexdigest()[:10]
        base_name = created_at.strftime("%Y%m%dT%H%M%SZ") + f"-{digest}"
        revision_dir = versions / base_name
        suffix = 2
        while revision_dir.exists():
            revision_dir = versions / f"{base_name}-{suffix}"
            suffix += 1
        os.replace(staging, revision_dir)
        relative = revision_dir.relative_to(store)
        posegraph_files = sorted(
            str(path.relative_to(store))
            for path in revision_dir.glob("posegraph*")
            if path.is_file()
        )
        manifest = {
            "format": MAP_STORE_FORMAT,
            "revision": revision_dir.name,
            "created_at": created_at.isoformat(),
            "map_id": slam_map.map_id,
            "map_revision": slam_map.map_revision,
            "map_yaml": str(relative / "map.yaml"),
            "map_image": str(relative / "map.pgm"),
            "user_map": str(relative / "user-map.geojson"),
            "preview": str(relative / "preview.png"),
            "posegraph_files": posegraph_files,
            "posegraph_saved": posegraph_saved,
            "warnings": warnings,
        }
        if normalized_pose is not None:
            manifest["initial_pose"] = normalized_pose
        _write_json_atomic(store / ACTIVE_MANIFEST, manifest)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
