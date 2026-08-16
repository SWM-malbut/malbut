"""Pure map exploration and revision-persistence helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import stat
from typing import Callable

import cv2
import numpy as np
import yaml

from malbut_gazebo.user_map_builder import build_user_map, load_slam_map


MAP_STORE_FORMAT = "malbut-map-store/v1"
ACTIVE_MANIFEST = "active.json"
FREE_THRESHOLD = 0.196
OCCUPIED_THRESHOLD = 0.65
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

_UNSAFE_WRITE_BITS = 0o022
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_REVISION_FILES = frozenset({
    "map.pgm",
    "map.yaml",
    "user-map.geojson",
    "preview.png",
})
_MAX_ACTIVE_MANIFEST_BYTES = 64 * 1024


@dataclass(frozen=True)
class _PublishedTreeIdentity:
    """Immutable file identities for one previously published revision."""

    active_file: tuple[int, ...]
    revision_directory: tuple[int, ...]
    revision_files: tuple[tuple[str, tuple[int, ...]], ...]


def _map_store_error() -> OSError:
    """Return one path-free failure for an unprotected map store."""
    return OSError("map store is not protected")


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    """Return all state relevant to an immutable regular-file snapshot."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_directory(
    value: os.stat_result,
    *,
    exact_private_mode: bool,
) -> None:
    mode = stat.S_IMODE(value.st_mode)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or mode & _UNSAFE_WRITE_BITS
        or (exact_private_mode and mode != PRIVATE_DIRECTORY_MODE)
    ):
        raise _map_store_error()


def _validate_regular_file(
    value: os.stat_result,
    *,
    exact_private_mode: bool,
) -> None:
    mode = stat.S_IMODE(value.st_mode)
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_nlink != 1
        or mode & _UNSAFE_WRITE_BITS
        or (exact_private_mode and mode != PRIVATE_FILE_MODE)
    ):
        raise _map_store_error()


def _attest_directory_entry(
    descriptor: int,
    parent_descriptor: int | None,
    name: str | None,
    *,
    exact_private_mode: bool,
) -> os.stat_result:
    """Bind an open directory to its no-follow directory entry."""
    opened = os.fstat(descriptor)
    _validate_directory(opened, exact_private_mode=exact_private_mode)
    if parent_descriptor is None or name is None:
        return opened
    entry = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
        raise _map_store_error()
    _validate_directory(entry, exact_private_mode=exact_private_mode)
    return opened


def _open_store_directory(store: Path) -> tuple[Path, int, bool]:
    """Open or provision an absolute store without following symlinks."""
    expanded = store.expanduser()
    absolute = Path(os.path.abspath(os.fspath(expanded)))
    if absolute == Path(os.path.sep):
        raise _map_store_error()
    current = os.open(os.path.sep, _DIRECTORY_OPEN_FLAGS)
    created_store = False
    try:
        parts = absolute.parts[1:]
        for index, component in enumerate(parts):
            created = False
            try:
                following = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=current,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(
                        component,
                        PRIVATE_DIRECTORY_MODE,
                        dir_fd=current,
                    )
                    created = True
                except FileExistsError:
                    pass
                following = os.open(
                    component,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=current,
                )
            opened = os.fstat(following)
            if created:
                _validate_directory(opened, exact_private_mode=True)
            if index == len(parts) - 1:
                _validate_directory(
                    opened,
                    exact_private_mode=created,
                )
                created_store = created
            os.close(current)
            current = following
        return absolute, current, created_store
    except BaseException:
        os.close(current)
        raise


def _reopen_store_directory(store: Path) -> int:
    """Reopen a configured store one component at a time, without symlinks."""
    current = os.open(os.path.sep, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in store.parts[1:]:
            following = os.open(
                component,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=current,
            )
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _attest_store_path(store: Path, descriptor: int) -> None:
    """Fail if the configured absolute path no longer names the open store."""
    reopened = _reopen_store_directory(store)
    try:
        expected = os.fstat(descriptor)
        actual = os.fstat(reopened)
        if (
            (actual.st_dev, actual.st_ino)
            != (expected.st_dev, expected.st_ino)
        ):
            raise _map_store_error()
        _validate_directory(actual, exact_private_mode=False)
    finally:
        os.close(reopened)


def _open_or_create_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    require_existing_private_mode: bool,
    allow_create: bool = True,
) -> tuple[int, bool]:
    """Open one child directory, creating it as 0700 when absent."""
    created = False
    try:
        descriptor = os.open(
            name,
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        if not allow_create:
            raise _map_store_error() from None
        try:
            os.mkdir(
                name,
                PRIVATE_DIRECTORY_MODE,
                dir_fd=parent_descriptor,
            )
            created = True
        except FileExistsError:
            pass
        descriptor = os.open(
            name,
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=parent_descriptor,
        )
    try:
        _attest_directory_entry(
            descriptor,
            parent_descriptor,
            name,
            exact_private_mode=(
                created or require_existing_private_mode
            ),
        )
        return descriptor, created
    except BaseException:
        os.close(descriptor)
        raise


def _open_regular_file_at(
    parent_descriptor: int,
    name: str,
    *,
    exact_private_mode: bool,
) -> tuple[int, tuple[int, ...]]:
    descriptor = os.open(name, _FILE_OPEN_FLAGS, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        _validate_regular_file(
            opened,
            exact_private_mode=exact_private_mode,
        )
        entry = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
            raise _map_store_error()
        _validate_regular_file(
            entry,
            exact_private_mode=exact_private_mode,
        )
        return descriptor, _stat_identity(opened)
    except BaseException:
        os.close(descriptor)
        raise


def _attest_regular_file_at(
    parent_descriptor: int,
    name: str,
    *,
    expected_identity: tuple[int, ...] | None = None,
    exact_private_mode: bool = True,
) -> tuple[int, ...]:
    descriptor, identity = _open_regular_file_at(
        parent_descriptor,
        name,
        exact_private_mode=exact_private_mode,
    )
    os.close(descriptor)
    if expected_identity is not None and identity != expected_identity:
        raise _map_store_error()
    return identity


def _write_private_file_at(
    parent_descriptor: int,
    name: str,
    value: bytes,
) -> tuple[int, ...]:
    """Create, flush, and bind one never-public regular file as 0600."""
    if type(value) is not bytes:
        raise TypeError("private map output must be bytes")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(
        name,
        flags,
        PRIVATE_FILE_MODE,
        dir_fd=parent_descriptor,
    )
    try:
        before = os.fstat(descriptor)
        _validate_regular_file(before, exact_private_mode=True)
        remaining = memoryview(value)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("could not write map output")
            remaining = remaining[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        _validate_regular_file(after, exact_private_mode=True)
        entry = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (entry.st_dev, entry.st_ino) != (after.st_dev, after.st_ino):
            raise _map_store_error()
        _validate_regular_file(entry, exact_private_mode=True)
        return _stat_identity(after)
    finally:
        os.close(descriptor)


def _entry_exists_at(parent_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _read_bounded_file(
    descriptor: int,
    maximum_bytes: int,
) -> bytes:
    chunks = []
    total = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum_bytes:
            raise _map_store_error()
    return b"".join(chunks)


def _attest_existing_active_tree(
    store_descriptor: int,
    versions_descriptor: int,
) -> _PublishedTreeIdentity | None:
    """Reject drift in the currently published lifecycle-owned snapshot."""
    if not _entry_exists_at(store_descriptor, ACTIVE_MANIFEST):
        return None
    active_descriptor, active_identity = _open_regular_file_at(
        store_descriptor,
        ACTIVE_MANIFEST,
        exact_private_mode=True,
    )
    try:
        manifest_bytes = _read_bounded_file(
            active_descriptor,
            _MAX_ACTIVE_MANIFEST_BYTES,
        )
        if _stat_identity(os.fstat(active_descriptor)) != active_identity:
            raise _map_store_error()
    finally:
        os.close(active_descriptor)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _map_store_error() from None
    if (
        type(manifest) is not dict
        or manifest.get("format") != MAP_STORE_FORMAT
    ):
        raise _map_store_error()
    revision = manifest.get("revision")
    if (
        type(revision) is not str
        or not revision
        or len(revision) > 128
        or not revision.isascii()
        or revision in {".", ".."}
        or "/" in revision
        or "\\" in revision
    ):
        raise _map_store_error()
    required_paths = {
        "map_yaml": "map.yaml",
        "map_image": "map.pgm",
        "user_map": "user-map.geojson",
        "preview": "preview.png",
    }
    for field_name, filename in required_paths.items():
        expected = str(Path("versions") / revision / filename)
        if manifest.get(field_name) != expected:
            raise _map_store_error()
    posegraph_values = manifest.get("posegraph_files")
    if type(posegraph_values) is not list:
        raise _map_store_error()
    posegraph_names = []
    for value in posegraph_values:
        if type(value) is not str:
            raise _map_store_error()
        prefix = str(Path("versions") / revision) + os.path.sep
        if not value.startswith(prefix):
            raise _map_store_error()
        name = value[len(prefix):]
        if (
            not name.startswith("posegraph")
            or not name
            or os.path.sep in name
            or "\\" in name
        ):
            raise _map_store_error()
        posegraph_names.append(name)
    if posegraph_names != sorted(set(posegraph_names)):
        raise _map_store_error()
    revision_descriptor = os.open(
        revision,
        _DIRECTORY_OPEN_FLAGS,
        dir_fd=versions_descriptor,
    )
    try:
        revision_before = _attest_directory_entry(
            revision_descriptor,
            versions_descriptor,
            revision,
            exact_private_mode=True,
        )
        expected_entries = _REVISION_FILES | frozenset(posegraph_names)
        if frozenset(os.listdir(revision_descriptor)) != expected_entries:
            raise _map_store_error()
        revision_files = []
        for name in sorted(expected_entries):
            identity = _attest_regular_file_at(
                revision_descriptor,
                name,
                exact_private_mode=True,
            )
            revision_files.append((name, identity))
        revision_after = _attest_directory_entry(
            revision_descriptor,
            versions_descriptor,
            revision,
            exact_private_mode=True,
        )
        if _stat_identity(revision_after) != _stat_identity(revision_before):
            raise _map_store_error()
    finally:
        os.close(revision_descriptor)
    return _PublishedTreeIdentity(
        active_file=active_identity,
        revision_directory=_stat_identity(revision_after),
        revision_files=tuple(revision_files),
    )


def _normalize_new_posegraph_file_at(
    staging_descriptor: int,
    name: str,
) -> tuple[int, ...]:
    """Privatize a writer-created file before its tree is published."""
    descriptor = os.open(
        name,
        _FILE_OPEN_FLAGS,
        dir_fd=staging_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
        ):
            raise _map_store_error()
        entry = os.stat(
            name,
            dir_fd=staging_descriptor,
            follow_symlinks=False,
        )
        if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
            raise _map_store_error()
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or entry.st_nlink != 1
        ):
            raise _map_store_error()
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        _validate_regular_file(after, exact_private_mode=True)
        rebound = os.stat(
            name,
            dir_fd=staging_descriptor,
            follow_symlinks=False,
        )
        if (rebound.st_dev, rebound.st_ino) != (after.st_dev, after.st_ino):
            raise _map_store_error()
        _validate_regular_file(rebound, exact_private_mode=True)
        return _stat_identity(after)
    finally:
        os.close(descriptor)


def _attest_staging_outputs(
    staging_descriptor: int,
    core_identities: dict[str, tuple[int, ...]],
) -> tuple[list[str], dict[str, tuple[int, ...]]]:
    entries = os.listdir(staging_descriptor)
    if len(entries) != len(set(entries)):
        raise _map_store_error()
    posegraph_names = []
    posegraph_identities = {}
    for name in sorted(entries):
        if name in _REVISION_FILES:
            _attest_regular_file_at(
                staging_descriptor,
                name,
                expected_identity=core_identities[name],
                exact_private_mode=True,
            )
        elif name.startswith("posegraph"):
            posegraph_identities[name] = _normalize_new_posegraph_file_at(
                staging_descriptor,
                name,
            )
            posegraph_names.append(name)
        else:
            raise _map_store_error()
    if frozenset(entries) != _REVISION_FILES | frozenset(posegraph_names):
        raise _map_store_error()
    return posegraph_names, posegraph_identities


def _create_private_directory_at(
    parent_descriptor: int,
    prefix: str,
) -> tuple[str, int]:
    for _attempt in range(128):
        name = prefix + secrets.token_hex(16)
        try:
            os.mkdir(
                name,
                PRIVATE_DIRECTORY_MODE,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            continue
        descriptor = os.open(
            name,
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=parent_descriptor,
        )
        try:
            _attest_directory_entry(
                descriptor,
                parent_descriptor,
                name,
                exact_private_mode=True,
            )
        except BaseException:
            os.close(descriptor)
            raise
        return name, descriptor
    raise OSError("could not allocate private map staging directory")


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


def _active_identity_at(store_descriptor: int) -> tuple[int, ...] | None:
    if not _entry_exists_at(store_descriptor, ACTIVE_MANIFEST):
        return None
    return _attest_regular_file_at(
        store_descriptor,
        ACTIVE_MANIFEST,
        exact_private_mode=True,
    )


def _write_json_atomic_at(
    store_descriptor: int,
    value: dict,
    *,
    expected_active_identity: tuple[int, ...] | None,
) -> None:
    """Commit active.json as a flushed 0600 inode inside the anchored store."""
    try:
        encoded = (
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n"
        ).encode("utf-8")
    except (OverflowError, TypeError, UnicodeEncodeError, ValueError):
        raise OSError("could not encode active map manifest") from None
    temporary_name = None
    temporary_identity = None
    try:
        for _attempt in range(128):
            candidate = (
                f".{ACTIVE_MANIFEST}.{secrets.token_hex(16)}.tmp"
            )
            try:
                temporary_identity = _write_private_file_at(
                    store_descriptor,
                    candidate,
                    encoded,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_name is None or temporary_identity is None:
            raise OSError("could not allocate active map manifest")
        current_active_identity = _active_identity_at(store_descriptor)
        if current_active_identity != expected_active_identity:
            raise _map_store_error()
        _attest_regular_file_at(
            store_descriptor,
            temporary_name,
            expected_identity=temporary_identity,
            exact_private_mode=True,
        )
        os.replace(
            temporary_name,
            ACTIVE_MANIFEST,
            src_dir_fd=store_descriptor,
            dst_dir_fd=store_descriptor,
        )
        temporary_name = None
        published_identity = _attest_regular_file_at(
            store_descriptor,
            ACTIVE_MANIFEST,
            exact_private_mode=True,
        )
        # Renaming an inode can update ctime, but every other bound attribute
        # (including inode, mode, link count, size, and mtime) must survive.
        if published_identity[:-1] != temporary_identity[:-1]:
            raise _map_store_error()
        os.fsync(store_descriptor)
    finally:
        if temporary_name is not None:
            try:
                current = _attest_regular_file_at(
                    store_descriptor,
                    temporary_name,
                    exact_private_mode=True,
                )
                if current == temporary_identity:
                    os.unlink(temporary_name, dir_fd=store_descriptor)
            except OSError:
                pass


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
    store, store_descriptor, _store_created = _open_store_directory(store)
    versions_descriptor = -1
    staging_descriptor = -1
    staging_name = None
    staging_path = None
    warnings = []
    try:
        active_exists = _entry_exists_at(store_descriptor, ACTIVE_MANIFEST)
        versions_descriptor, versions_created = _open_or_create_directory_at(
            store_descriptor,
            "versions",
            require_existing_private_mode=True,
            allow_create=not active_exists,
        )
        if versions_created:
            os.fsync(store_descriptor)
        expected_active_tree = _attest_existing_active_tree(
            store_descriptor,
            versions_descriptor,
        )
        _attest_store_path(store, store_descriptor)
        _attest_directory_entry(
            versions_descriptor,
            store_descriptor,
            "versions",
            exact_private_mode=True,
        )
        staging_name, staging_descriptor = _create_private_directory_at(
            versions_descriptor,
            ".staging-",
        )
        versions = store / "versions"
        staging_path = versions / staging_name
        image_bytes = _pgm_bytes(grid)
        core_identities = {
            "map.pgm": _write_private_file_at(
                staging_descriptor,
                "map.pgm",
                image_bytes,
            )
        }
        image_path = staging_path / "map.pgm"
        yaml_path = staging_path / "map.yaml"
        yaml_value = {
            "image": image_path.name,
            "mode": "trinary",
            "resolution": grid.resolution,
            "origin": [grid.origin_x, grid.origin_y, grid.origin_yaw],
            "negate": 0,
            "occupied_thresh": OCCUPIED_THRESHOLD,
            "free_thresh": FREE_THRESHOLD,
        }
        yaml_bytes = yaml.safe_dump(
            yaml_value,
            sort_keys=False,
        ).encode("utf-8")
        core_identities["map.yaml"] = _write_private_file_at(
            staging_descriptor,
            "map.yaml",
            yaml_bytes,
        )
        _attest_store_path(store, store_descriptor)
        _attest_directory_entry(
            staging_descriptor,
            versions_descriptor,
            staging_name,
            exact_private_mode=True,
        )
        slam_map = load_slam_map(yaml_path)
        user_map, preview = build_user_map(slam_map)
        user_map_bytes = (
            json.dumps(
                user_map,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        core_identities["user-map.geojson"] = _write_private_file_at(
            staging_descriptor,
            "user-map.geojson",
            user_map_bytes,
        )
        preview_success, preview_encoded = cv2.imencode(".png", preview)
        if not preview_success:
            raise OSError("could not write User Map preview")
        core_identities["preview.png"] = _write_private_file_at(
            staging_descriptor,
            "preview.png",
            preview_encoded.tobytes(),
        )
        posegraph_base = staging_path / "posegraph"
        posegraph_saved = False
        if posegraph_writer is not None:
            _attest_store_path(store, store_descriptor)
            _attest_directory_entry(
                staging_descriptor,
                versions_descriptor,
                staging_name,
                exact_private_mode=True,
            )
            posegraph_saved = bool(posegraph_writer(posegraph_base))
            if not posegraph_saved:
                warnings.append("SLAM pose graph를 저장하지 못했습니다.")
        _attest_store_path(store, store_descriptor)
        _attest_directory_entry(
            staging_descriptor,
            versions_descriptor,
            staging_name,
            exact_private_mode=True,
        )
        posegraph_names, posegraph_identities = _attest_staging_outputs(
            staging_descriptor,
            core_identities,
        )
        os.fsync(staging_descriptor)
        created_at = datetime.now(timezone.utc)
        digest = hashlib.sha256(
            image_bytes + slam_map.map_revision.encode("ascii")
        ).hexdigest()[:10]
        base_name = created_at.strftime("%Y%m%dT%H%M%SZ") + f"-{digest}"
        revision_name = base_name
        suffix = 2
        while _entry_exists_at(versions_descriptor, revision_name):
            revision_name = f"{base_name}-{suffix}"
            suffix += 1
        _attest_directory_entry(
            staging_descriptor,
            versions_descriptor,
            staging_name,
            exact_private_mode=True,
        )
        os.rename(
            staging_name,
            revision_name,
            src_dir_fd=versions_descriptor,
            dst_dir_fd=versions_descriptor,
        )
        staging_name = None
        staging_path = None
        _attest_directory_entry(
            staging_descriptor,
            versions_descriptor,
            revision_name,
            exact_private_mode=True,
        )
        for name, identity in core_identities.items():
            _attest_regular_file_at(
                staging_descriptor,
                name,
                expected_identity=identity,
                exact_private_mode=True,
            )
        for name in posegraph_names:
            _attest_regular_file_at(
                staging_descriptor,
                name,
                expected_identity=posegraph_identities[name],
                exact_private_mode=True,
            )
        os.fsync(staging_descriptor)
        os.fsync(versions_descriptor)
        relative = Path("versions") / revision_name
        posegraph_files = [
            str(relative / name) for name in posegraph_names
        ]
        manifest = {
            "format": MAP_STORE_FORMAT,
            "revision": revision_name,
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
        _attest_store_path(store, store_descriptor)
        _attest_directory_entry(
            versions_descriptor,
            store_descriptor,
            "versions",
            exact_private_mode=True,
        )
        current_active_tree = _attest_existing_active_tree(
            store_descriptor,
            versions_descriptor,
        )
        if current_active_tree != expected_active_tree:
            raise _map_store_error()
        _write_json_atomic_at(
            store_descriptor,
            manifest,
            expected_active_identity=(
                None
                if expected_active_tree is None
                else expected_active_tree.active_file
            ),
        )
        _attest_store_path(store, store_descriptor)
        _attest_directory_entry(
            staging_descriptor,
            versions_descriptor,
            revision_name,
            exact_private_mode=True,
        )
        return manifest
    except Exception:
        raise
    finally:
        cleanup_path = None
        if staging_name is not None and staging_descriptor >= 0:
            try:
                _attest_directory_entry(
                    staging_descriptor,
                    versions_descriptor,
                    staging_name,
                    exact_private_mode=True,
                )
                cleanup_path = staging_path
            except OSError:
                cleanup_path = None
        if staging_descriptor >= 0:
            os.close(staging_descriptor)
        if cleanup_path is not None:
            shutil.rmtree(cleanup_path, ignore_errors=True)
        if versions_descriptor >= 0:
            os.close(versions_descriptor)
        os.close(store_descriptor)
