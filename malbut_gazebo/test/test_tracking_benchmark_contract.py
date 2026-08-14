"""Contracts for the two one-lap person-tracking benchmarks."""

import importlib.util
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from launch.actions import IncludeLaunchDescription
from launch_ros.actions import Node


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _load_launch(filename):
    path = PACKAGE_ROOT / 'launch' / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trajectory(actor_filename):
    root = ET.parse(
        PACKAGE_ROOT / 'models' / 'humanoid_actor' / actor_filename
    ).getroot()
    trajectory = root.find('.//trajectory')
    assert trajectory is not None
    waypoints = trajectory.findall('waypoint')
    times = [float(item.findtext('time')) for item in waypoints]
    poses = [
        [float(value) for value in item.findtext('pose').split()]
        for item in waypoints
    ]
    return root, times, poses


def _read_pgm(path):
    with path.open('rb') as stream:
        assert stream.readline().strip() == b'P5'
        line = stream.readline()
        while line.startswith(b'#'):
            line = stream.readline()
        width, height = (int(value) for value in line.split())
        assert int(stream.readline()) == 255
        data = stream.read()
    assert len(data) == width * height
    return width, height, data


def test_benchmarks_cover_both_environments_for_exactly_one_lap():
    """Each launch selects its own world, map, actor path, and lap time."""
    launch_modules = [
        _load_launch('tracking_test_small_house.launch.py'),
        _load_launch('tracking_test_robocup_home.launch.py'),
    ]
    small_house, robocup = [module.PROFILE for module in launch_modules]

    assert small_house.world_name == 'small_house'
    assert small_house.map_filename == 'small_house.yaml'
    assert small_house.actor_filename == 'model.sdf'
    assert robocup.world_name == 'robocup_home'
    assert robocup.map_filename == 'robocup_home.yaml'
    assert robocup.actor_filename == 'robocup_home.sdf'

    for profile in (small_house, robocup):
        root, times, poses = _trajectory(profile.actor_filename)
        assert root.findtext('.//script/loop') == 'true'
        assert times == sorted(times)
        assert math.isclose(times[-1], profile.lap_duration_s, abs_tol=1e-3)
        assert poses[0][:2] == poses[-1][:2]

    for module, profile in zip(launch_modules, (small_house, robocup)):
        description = module.generate_launch_description()
        benchmark = next(
            entity
            for entity in description.entities
            if isinstance(entity, Node)
        )
        assert benchmark.node_package == 'malbut_tracking'
        assert benchmark.node_executable == 'tracking_benchmark'
        demo = next(
            entity
            for entity in description.entities
            if isinstance(entity, IncludeLaunchDescription)
        )
        arguments = dict(demo.launch_arguments)
        assert arguments['world_name'] == profile.world_name
        assert Path(arguments['map']).name == profile.map_filename
        assert Path(arguments['actor_file']).name == profile.actor_filename


def test_robocup_route_stays_in_mapped_free_space():
    """The baseline route must not use straight lines through map walls."""
    _, _, poses = _trajectory('robocup_home.sdf')
    width, height, pixels = _read_pgm(
        PACKAGE_ROOT / 'maps' / 'robocup_home.pgm'
    )
    resolution = 0.05
    origin_x = -5.04
    origin_y = -4.07
    clearance_pixels = 6  # 0.30 m around each sampled actor position

    def pixel(x, y):
        column = round((x - origin_x) / resolution)
        row = height - 1 - round((y - origin_y) / resolution)
        return column, row

    for start, end in zip(poses, poses[1:]):
        distance = math.dist(start[:2], end[:2])
        sample_count = max(1, math.ceil(distance / (resolution / 2.0)))
        for index in range(sample_count + 1):
            ratio = index / sample_count
            x = start[0] + (end[0] - start[0]) * ratio
            y = start[1] + (end[1] - start[1]) * ratio
            column, row = pixel(x, y)
            assert clearance_pixels <= column < width - clearance_pixels
            assert clearance_pixels <= row < height - clearance_pixels
            for offset_y in range(-clearance_pixels, clearance_pixels + 1):
                for offset_x in range(
                    -clearance_pixels,
                    clearance_pixels + 1,
                ):
                    if math.hypot(offset_x, offset_y) > clearance_pixels:
                        continue
                    cell = (row + offset_y) * width + column + offset_x
                    assert pixels[cell] >= 250


def test_benchmark_reports_time_metrics_without_quality_threshold():
    """The runtime result is descriptive and contains no pass threshold."""
    source = (
        PACKAGE_ROOT.parent
        / 'malbut_tracking'
        / 'malbut_tracking'
        / 'tracking_benchmark_node.py'
    ).read_text(encoding='utf-8')
    metrics = (
        PACKAGE_ROOT.parent
        / 'malbut_tracking'
        / 'malbut_tracking'
        / 'tracking_metrics.py'
    ).read_text(encoding='utf-8')

    assert 'tracking_duration_s' in metrics
    assert 'longest_continuous_tracking_s' in metrics
    assert 'reacquisition_count' in metrics
    assert "self._finish('lap_complete'" in source
    assert 'quality_threshold' not in source + metrics
    assert '/world/' not in source + metrics
    assert 'model_pose' not in source + metrics
