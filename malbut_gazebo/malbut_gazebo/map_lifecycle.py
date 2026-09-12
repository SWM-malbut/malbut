"""Pure map exploration and revision-persistence helpers."""

from __future__ import annotations

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

from malbut_autoslam.frontier import (
    FREE_THRESHOLD,
    FRONTIER_CELL_CAP,
    FRONTIER_DISTANCE_PENALTY_CELLS_PER_M,
    OCCUPIED_THRESHOLD,
    Frontier,
    MapGrid,
    find_frontiers,
    map_grid_from_message,
    map_statistics,
)
from malbut_gazebo.user_map_builder import build_user_map, load_slam_map


MAP_STORE_FORMAT = "malbut-map-store/v1"
ACTIVE_MANIFEST = "active.json"
__all__ = [
    "MAP_STORE_FORMAT", "ACTIVE_MANIFEST", "FREE_THRESHOLD", "OCCUPIED_THRESHOLD",
    "FRONTIER_CELL_CAP", "FRONTIER_DISTANCE_PENALTY_CELLS_PER_M",
    "MapGrid", "Frontier", "map_grid_from_message", "map_statistics",
    "find_frontiers", "render_map_png", "load_active_revision",
    "persist_map_revision",
]


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
