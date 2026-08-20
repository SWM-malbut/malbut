"""Tests for first-run exploration and atomic map revisions."""

from functools import partial
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
from threading import Lock, Thread

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
    FRONTIER_CELL_CAP,
    FRONTIER_DISTANCE_PENALTY_CELLS_PER_M,
    MapGrid,
    find_frontiers,
    load_active_revision,
    map_statistics,
    persist_map_revision,
    render_map_png,
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


def test_managed_simulation_exposes_one_explicit_demo_actor_manager():
    """The cloud web may show or hide one actor without a scenario runtime."""
    source = (
        Path(__file__).resolve().parents[1]
        / "launch"
        / "managed_home.launch.py"
    ).read_text(encoding="utf-8")

    assert source.count('executable="demo_actor_manager"') == 1
    assert 'perform(context) == "small_house"' in source
    assert "actor_spawn_delay" not in source


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


class _Frontier:
    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y
        self.yaw = 0.0


def _stub_onboarding_server():
    from malbut_gazebo.map_onboarding_server import MapOnboardingBridge

    server = MapOnboardingBridge.__new__(MapOnboardingBridge)
    server.lock = Lock()
    server.blacklisted = []
    server.completed_target = None
    server.unproductive_visits = 0
    server.state = "exploring"
    server.message = ""
    return server


def test_exploration_keeps_a_frontier_that_still_reveals_new_space():
    """A visit that moves the frontier onward must not be discarded."""
    server = _stub_onboarding_server()
    server.completed_target = (7.98, -0.90)

    assert server._discard_unproductive_frontier(_Frontier(5.0, -0.90)) is False
    assert server.blacklisted == []
    assert server.unproductive_visits == 0


def test_exploration_discards_a_frontier_two_visits_failed_to_resolve():
    """
    Reaching the same frontier twice reveals nothing, so stop retrying.

    Only failed goals used to be blacklisted, so two reachable-but-blind
    approach points made exploration ping-pong between them forever.
    """
    server = _stub_onboarding_server()
    server.completed_target = (7.98, -0.90)

    # 첫 근접 재선정은 정상적인 점진 탐색일 수 있으므로 통과시킨다.
    assert server._discard_unproductive_frontier(_Frontier(7.93, -1.41)) is False
    assert server.blacklisted == []

    server.completed_target = (7.93, -1.41)
    assert server._discard_unproductive_frontier(_Frontier(7.98, -0.90)) is True
    assert server.blacklisted == [(7.93, -1.41)]
    assert server.completed_target is None
    assert server.unproductive_visits == 0
    assert server.state == "exploring"


def test_exploration_ignores_repeat_checks_without_a_completed_visit():
    server = _stub_onboarding_server()

    assert server._discard_unproductive_frontier(_Frontier(1.0, 1.0)) is False
    assert server.blacklisted == []


def test_frontier_order_prefers_nearby_work_over_a_house_crossing():
    """
    Exploration must not cross the house for a marginally larger cluster.

    Cluster size alone used to decide the order, so the robot drove from
    one end of the house to the other and straight back.
    """
    from malbut_gazebo.map_lifecycle import Frontier

    near = Frontier(
        x=1.0, y=0.0, yaw=0.0,
        cell_count=120, clearance_m=0.5, distance_m=1.0,
    )
    far = Frontier(
        x=9.0, y=0.0, yaw=0.0,
        cell_count=160, clearance_m=0.5, distance_m=9.0,
    )
    ordered = sorted(
        [far, near],
        key=lambda item: (
            -(
                min(item.cell_count, FRONTIER_CELL_CAP)
                - FRONTIER_DISTANCE_PENALTY_CELLS_PER_M * item.distance_m
            ),
            item.distance_m,
        ),
    )

    assert ordered[0] is near

    # 이동 비용을 덮을 만큼 큰 공간이면 멀어도 먼저 간다.
    huge = Frontier(
        x=9.0, y=0.0, yaw=0.0,
        cell_count=400, clearance_m=0.5, distance_m=9.0,
    )
    small_near = Frontier(
        x=1.0, y=0.0, yaw=0.0,
        cell_count=40, clearance_m=0.5, distance_m=1.0,
    )
    ordered = sorted(
        [small_near, huge],
        key=lambda item: (
            -(
                min(item.cell_count, FRONTIER_CELL_CAP)
                - FRONTIER_DISTANCE_PENALTY_CELLS_PER_M * item.distance_m
            ),
            item.distance_m,
        ),
    )

    assert ordered[0] is huge
