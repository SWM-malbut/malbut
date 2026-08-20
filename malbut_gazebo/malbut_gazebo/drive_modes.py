"""Shared helpers for browser-controlled autonomous drive modes."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

import yaml


AUTONOMOUS_MODES = {"patrol", "person_following", "roaming"}
TRIGGER_DRIVE_MODES = {"patrol", "roaming"}


def build_room_patrol_route(user_map: dict, map_id: str) -> dict:
    """Build a bounded one-run patrol from each Room's safe label point."""
    if not isinstance(user_map, dict) or user_map.get("map_id") != map_id:
        raise ValueError("User Map identity does not match the active map")
    features = user_map.get("features")
    if not isinstance(features, list):
        raise ValueError("User Map features must be an array")
    waypoints = []
    seen = set()
    seen_names = set()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, dict) or properties.get("role") != "room":
            continue
        room_id = str(feature.get("id") or properties.get("room_id") or "").strip()
        point = properties.get("representative_point")
        if (
            not room_id or room_id in seen or not isinstance(point, list)
            or len(point) != 2 or not all(_finite_number(value) for value in point)
        ):
            continue
        seen.add(room_id)
        name = str(properties.get("name") or room_id).strip()[:80] or room_id
        base_name = name
        suffix = 2
        while name in seen_names:
            name = f"{base_name} {suffix}"[:80]
            suffix += 1
        seen_names.add(name)
        waypoints.append({
            "name": name,
            "pose": {"x": float(point[0]), "y": float(point[1]), "yaw": 0.0},
            "dwell_seconds": 2.0,
        })
        if len(waypoints) >= 64:
            break
    if not waypoints:
        raise ValueError("순찰할 방의 대표 위치가 없습니다.")
    for index, waypoint in enumerate(waypoints):
        next_waypoint = waypoints[(index + 1) % len(waypoints)]
        dx = next_waypoint["pose"]["x"] - waypoint["pose"]["x"]
        dy = next_waypoint["pose"]["y"] - waypoint["pose"]["y"]
        if math.hypot(dx, dy) > 1e-6:
            waypoint["pose"]["yaw"] = round(math.atan2(dy, dx), 9)
    return {
        "schema_version": 1,
        "route": {
            "name": f"{map_id}_room_patrol",
            "map_id": map_id,
            "frame_id": "map",
            "cycles_per_run": 1,
            "defaults": {
                "dwell_seconds": 2.0,
                "max_retries": 1,
                "retry_backoff_seconds": 2.0,
                "on_failure": "skip",
            },
            "waypoints": waypoints,
        },
        "schedule": {"mode": "manual"},
    }


def write_room_patrol_route(
    user_map_path: Path,
    route_path: Path,
    map_id: str,
) -> Path:
    """Atomically refresh the patrol route paired with the current User Map."""
    try:
        user_map = json.loads(user_map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("사용자 지도를 읽을 수 없습니다.") from error
    route = build_room_patrol_route(user_map, map_id)
    route_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{route_path.name}.", suffix=".tmp", dir=route_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(route, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, route_path)
    finally:
        temporary.unlink(missing_ok=True)
    return route_path


def common_mode_state(mode: str, status: dict) -> str:
    """Normalize patrol and roaming implementation states for the web API."""
    raw = str(status.get("state", "idle"))
    if raw == "idle" or (mode == "patrol" and raw == "completed"):
        return "idle"
    if raw == "pausing":
        return "pausing"
    if raw == "paused":
        return "paused"
    if raw == "stopping":
        return "stopping"
    if mode == "patrol" and raw == "aborted":
        return "failed"
    return "active"


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
