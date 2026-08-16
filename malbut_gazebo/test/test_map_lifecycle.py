"""Tests for first-run exploration and atomic map revisions."""

from functools import partial
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import stat
from threading import Thread

import numpy as np
import pytest
import yaml
from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
)
from launch.utilities import perform_substitutions
from launch_ros.actions import Node

from malbut_gazebo.map_lifecycle import (
    FREE_THRESHOLD,
    MapGrid,
    find_frontiers,
    load_active_revision,
    map_statistics,
    persist_map_revision,
    render_map_png,
)
from malbut_gazebo.gazebo_monitor_room_active_map import (
    ActiveMapEvidenceResolver,
    ActiveMapResolverConfig,
    ActiveMapValidationError,
)
from malbut_gazebo.map_onboarding_server import MapOnboardingRequestHandler


def _grid() -> MapGrid:
    cells = np.full((40, 50), -1, dtype=np.int16)
    cells[5:35, 5:45] = 0
    cells[5, 5:45] = 100
    cells[34, 5:45] = 100
    cells[5:35, 5] = 100
    cells[5:35, 44] = 100
    cells.setflags(write=False)
    return MapGrid(50, 40, 0.1, -2.5, -2.0, 0.0, cells)


def test_frontiers_report_safe_unknown_boundaries():
    """Exploration targets must be free, useful, and obstacle-clear."""
    cells = np.full((40, 50), -1, dtype=np.int16)
    cells[10:30, 10:30] = 0
    cells[10:30, 10] = 100
    cells.setflags(write=False)
    grid = MapGrid(50, 40, 0.1, 0.0, 0.0, 0.0, cells)
    candidates = find_frontiers(
        grid, (1.5, 2.0), minimum_clearance_m=0.2
    )

    assert candidates
    candidate = candidates[0]
    column = int(candidate.x / grid.resolution)
    row = int(candidate.y / grid.resolution)
    assert grid.cells[row, column] == 0
    assert candidate.clearance_m >= 0.2
    assert candidate.distance_m >= 0.45
    assert find_frontiers(
        grid,
        (1.5, 2.0),
        minimum_clearance_m=0.2,
        blacklisted=((candidate.x, candidate.y),),
    ) == []


def test_map_render_and_progress_exclude_costmap_inflation():
    """The product preview must derive only from raw SLAM occupancy."""
    grid = _grid()
    statistics = map_statistics(grid)
    png = render_map_png(grid)

    assert png.startswith(b"\x89PNG")
    assert statistics["free_area_m2"] == pytest.approx(10.64)
    assert statistics["known_area_m2"] == pytest.approx(12.0)


def test_revision_save_is_atomic_and_preserves_previous_map(tmp_path):
    """A failed replacement must not change the active map manifest."""
    posegraph_calls = []

    def write_posegraph(base: Path) -> bool:
        posegraph_calls.append(base)
        base.with_suffix(".posegraph").write_bytes(b"graph")
        return True

    first = persist_map_revision(
        _grid(),
        tmp_path,
        initial_pose={"x": 1.25, "y": -0.5, "yaw": 0.75},
        posegraph_writer=write_posegraph,
    )
    active = load_active_revision(tmp_path)
    assert active == first
    assert first["posegraph_saved"] is True
    assert first["initial_pose"] == {
        "x": 1.25, "y": -0.5, "yaw": 0.75
    }
    assert posegraph_calls
    map_yaml = tmp_path / first["map_yaml"]
    values = yaml.safe_load(map_yaml.read_text(encoding="utf-8"))
    assert values["free_thresh"] == FREE_THRESHOLD
    assert (tmp_path / first["user_map"]).is_file()

    too_small = np.full((4, 4), -1, dtype=np.int16)
    too_small[1:3, 1:3] = 0
    too_small.setflags(write=False)
    with pytest.raises(ValueError, match="충분하지"):
        persist_map_revision(
            MapGrid(4, 4, 0.05, 0.0, 0.0, 0.0, too_small), tmp_path
        )
    assert load_active_revision(tmp_path) == first
    with pytest.raises(ValueError, match="위치"):
        persist_map_revision(
            _grid(),
            tmp_path,
            initial_pose={"x": float("nan"), "y": 0.0, "yaw": 0.0},
        )
    assert load_active_revision(tmp_path) == first


def _permission_mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)


def test_revision_store_is_private_and_resolver_ready_under_group_umask(
    tmp_path,
):
    """A normal 0002 shell umask must not publish 0775/0664 map data."""
    store = tmp_path / "fresh-active-map-store"

    def write_posegraph(base: Path) -> bool:
        base.with_suffix(".posegraph").write_bytes(b"graph")
        base.with_suffix(".data").write_bytes(b"data")
        return True

    previous_umask = os.umask(0o002)
    try:
        manifest = persist_map_revision(
            _grid(),
            store,
            posegraph_writer=write_posegraph,
        )
    finally:
        os.umask(previous_umask)

    revision = store / "versions" / manifest["revision"]
    assert _permission_mode(store) == 0o700
    assert _permission_mode(store / "versions") == 0o700
    assert _permission_mode(revision) == 0o700
    assert _permission_mode(store / "active.json") == 0o600
    assert not any(
        path.name.startswith(".staging-")
        for path in (store / "versions").iterdir()
    )
    for path in revision.iterdir():
        assert path.is_file()
        assert _permission_mode(path) == 0o600
        assert path.stat().st_nlink == 1

    evidence = ActiveMapEvidenceResolver(
        ActiveMapResolverConfig(str(store))
    ).resolve()
    assert evidence.map_id == manifest["map_id"]
    assert evidence.map_revision == manifest["map_revision"]


def test_published_permission_drift_fails_without_silent_repair(tmp_path):
    """Saving and trusted reads both reject every unsafe published layer."""
    store = tmp_path / "protected-active-map-store"
    manifest = persist_map_revision(_grid(), store)
    revision = store / "versions" / manifest["revision"]
    resolver = ActiveMapEvidenceResolver(ActiveMapResolverConfig(str(store)))
    resolver.resolve()
    original_active = (store / "active.json").read_bytes()
    original_entries = set((store / "versions").iterdir())
    targets = (
        (store, 0o770),
        (store / "versions", 0o770),
        (revision, 0o770),
        (store / "active.json", 0o660),
        (revision / "map.yaml", 0o660),
        (revision / "map.pgm", 0o660),
        (revision / "user-map.geojson", 0o660),
    )
    for target, unsafe_mode in targets:
        safe_mode = _permission_mode(target)
        target.chmod(unsafe_mode)
        try:
            with pytest.raises(OSError, match="map store is not protected"):
                persist_map_revision(_grid(), store)
            with pytest.raises(ActiveMapValidationError):
                resolver.resolve()
            assert _permission_mode(target) == unsafe_mode
            assert (store / "active.json").read_bytes() == original_active
            assert set((store / "versions").iterdir()) == original_entries
        finally:
            target.chmod(safe_mode)
        resolver.resolve()


def test_posegraph_hardlink_is_rejected_before_permission_normalization(
    tmp_path,
):
    """Provisioning must not chmod or publish a writer-supplied hardlink."""
    store = tmp_path / "hardlink-map-store"
    outside = tmp_path / "outside-posegraph"
    outside.write_bytes(b"outside")
    outside.chmod(0o644)

    def write_hardlink(base: Path) -> bool:
        os.link(outside, base.with_suffix(".posegraph"))
        return True

    with pytest.raises(OSError, match="map store is not protected"):
        persist_map_revision(
            _grid(),
            store,
            posegraph_writer=write_hardlink,
        )

    assert _permission_mode(outside) == 0o644
    assert outside.stat().st_nlink == 1
    assert not (store / "active.json").exists()


def test_published_map_mutation_during_staging_aborts_active_commit(tmp_path):
    """A changed old source cannot be hidden by publishing a new revision."""
    store = tmp_path / "mutated-active-map-store"
    first = persist_map_revision(_grid(), store)
    active_path = store / "active.json"
    active_bytes = active_path.read_bytes()
    old_map_image = store / first["map_image"]

    def mutate_published_map(_base: Path) -> bool:
        old_map_image.write_bytes(b"mutated while replacement was staged")
        return False

    with pytest.raises(OSError, match="map store is not protected"):
        persist_map_revision(
            _grid(),
            store,
            posegraph_writer=mutate_published_map,
        )

    assert active_path.read_bytes() == active_bytes
    assert _permission_mode(old_map_image) == 0o600
    with pytest.raises(ActiveMapValidationError):
        ActiveMapEvidenceResolver(
            ActiveMapResolverConfig(str(store))
        ).resolve()


class _FakeBridge:
    def __init__(self) -> None:
        self.calls = []

    def snapshot(self) -> dict:
        return {
            "state": "idle", "message": "ready", "map_revision": 0,
            "map": None, "pose": None, "target": None, "path": None,
            "frontier_count": 0, "active_revision": None,
        }

    def png_snapshot(self):
        return b"\x89PNG\r\n", 1

    def start(self, request):
        self.calls.append(("start", request))
        return self.snapshot()

    def finish(self, request):
        self.calls.append(("finish", request))
        return self.snapshot()

    def cancel(self, request):
        self.calls.append(("cancel", request))
        return self.snapshot()


class _QuietHandler(MapOnboardingRequestHandler):
    def log_message(self, _format, *_arguments):
        pass


def _request(address, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*address, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), payload


def test_mapping_commands_are_same_origin_and_csrf_protected(tmp_path):
    """Map motion commands must cross the existing security boundary."""
    bridge = _FakeBridge()
    _QuietHandler.bridge = bridge
    _QuietHandler.allowed_hosts = {"127.0.0.1", "localhost"}
    handler = partial(_QuietHandler, directory=tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = server.server_address
        status, headers, payload = _request(
            address, "GET", "/api/editor-config"
        )
        assert status == 200
        config = json.loads(payload)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        host = f"{address[0]}:{address[1]}"
        secure = {
            "Content-Type": "application/json",
            "Cookie": cookie,
            "X-CSRF-Token": config["csrf_token"],
            "Origin": f"http://{host}",
        }
        status, _, _ = _request(
            address, "POST", "/api/mapping/start", "{}", secure
        )
        assert status == 200
        assert bridge.calls == [("start", {})]

        insecure = dict(secure)
        insecure["Origin"] = "http://evil.example"
        status, _, _ = _request(
            address, "POST", "/api/mapping/cancel", "{}", insecure
        )
        assert status == 403
        status, _, payload = _request(
            address,
            "POST",
            "/api/mapping/cancel",
            "{}",
            {**secure, "Content-Type": "text/plain"},
        )
        assert status == 415
        assert b"application/json" in payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _launch_description(filename: str, module_name: str):
    path = Path(__file__).resolve().parents[1] / "launch" / filename
    specification = importlib.util.spec_from_file_location(module_name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.generate_launch_description()


def _launch_defaults(description) -> dict:
    context = LaunchContext()
    return {
        entity.name: perform_substitutions(context, entity.default_value)
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }


def _included_filename(include: IncludeLaunchDescription) -> str:
    source = include.launch_description_source
    location = perform_substitutions(
        LaunchContext(), source._LaunchDescriptionSource__location
    )
    return Path(location).name


def test_mapping_launches_separate_hardware_and_simulation_layers():
    """Real-robot onboarding must not implicitly start Gazebo."""
    hardware = _launch_description(
        "map_onboarding.launch.py", "malbut_map_onboarding_launch"
    )
    hardware_includes = [
        entity
        for entity in hardware.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    assert {_included_filename(item) for item in hardware_includes} == {
        "online_async_launch.py", "navigation.launch.py"
    }
    hardware_nodes = {
        (node.node_package, node.node_executable)
        for node in hardware.entities
        if isinstance(node, Node)
    }
    assert hardware_nodes == {
        ("malbut_gazebo", "record_localization_state"),
        ("malbut_gazebo", "map_onboarding_server"),
        ("rviz2", "rviz2"),
    }
    assert _launch_defaults(hardware)["use_sim_time"] == "false"

    simulation = _launch_description(
        "first_run_mapping.launch.py", "malbut_first_run_mapping_launch"
    )
    simulation_includes = [
        entity
        for entity in simulation.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    assert {_included_filename(item) for item in simulation_includes} == {
        "worlds.launch.py", "map_onboarding.launch.py"
    }
    assert _launch_defaults(simulation)["use_sim_time"] == "true"

    managed = _launch_description(
        "managed_home.launch.py", "malbut_managed_home_launch"
    )
    assert _launch_defaults(managed)["simulation"] == "true"
    assert _launch_defaults(managed)["trusted_initial_pose"] == "false"
    assert (
        _launch_defaults(managed)["trusted_localization_handoff"]
        == "false"
    )
    assert sum(
        isinstance(entity, OpaqueFunction) for entity in managed.entities
    ) == 1


def test_managed_launch_accepts_only_finite_saved_initial_pose():
    """A corrupt manifest must never inject NaN coordinates into AMCL."""
    path = (
        Path(__file__).resolve().parents[1]
        / "launch"
        / "managed_home.launch.py"
    )
    specification = importlib.util.spec_from_file_location(
        "malbut_managed_home_pose", path
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    assert module._saved_initial_pose({
        "initial_pose": {"x": 1.0, "y": -2.0, "yaw": 0.25}
    }) == {"x": "1.0", "y": "-2.0", "yaw": "0.25"}
    assert module._saved_initial_pose({
        "initial_pose": {"x": float("nan"), "y": 0.0, "yaw": 0.0}
    }) is None


def test_managed_launch_uses_one_pose_for_simulation_spawn_and_amcl():
    """A saved map pose must initialize both physics and localization."""
    path = (
        Path(__file__).resolve().parents[1]
        / "launch"
        / "managed_home.launch.py"
    )
    specification = importlib.util.spec_from_file_location(
        "malbut_managed_home_simulation_pose", path
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    context = LaunchContext()
    context.launch_configurations.update({
        "world_name": "small_house",
        "x": "",
        "y": "",
        "yaw": "",
    })
    saved = {"x": "1.25", "y": "-0.5", "yaw": "0.75"}

    pose = module._simulation_initial_pose(
        context, Path(__file__).resolve().parents[1], saved
    )

    assert pose == saved
    assert module._localization_arguments(pose) == {
        "restore_localization": "false",
        "set_initial_pose": "true",
        "initial_pose_x": "1.25",
        "initial_pose_y": "-0.5",
        "initial_pose_yaw": "0.75",
    }


def test_managed_hardware_restart_does_not_trust_map_last_pose():
    """Hardware remains unlocalized when safe state restoration is rejected."""
    path = (
        Path(__file__).resolve().parents[1]
        / "launch"
        / "managed_home.launch.py"
    )
    specification = importlib.util.spec_from_file_location(
        "malbut_managed_home_hardware_pose", path
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)

    assert module._localization_arguments(None) == {
        "restore_localization": "false",
        "set_initial_pose": "false",
    }
    assert module._localization_arguments(None, True) == {
        "restore_localization": "true",
        "set_initial_pose": "false",
    }


def test_managed_trusted_pose_requires_finite_explicit_coordinates():
    """A supervisor opt-in must never silently fall back to zero."""
    path = (
        Path(__file__).resolve().parents[1]
        / "launch"
        / "managed_home.launch.py"
    )
    specification = importlib.util.spec_from_file_location(
        "malbut_managed_home_trusted_pose", path
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    context = LaunchContext()
    context.launch_configurations.update({
        "x": "-1.25", "y": "2.5", "yaw": "0.75"
    })

    assert module._explicit_initial_pose(context) == {
        "x": "-1.25", "y": "2.5", "yaw": "0.75"
    }
    context.launch_configurations["x"] = ""
    with pytest.raises(RuntimeError, match="finite x"):
        module._explicit_initial_pose(context)
