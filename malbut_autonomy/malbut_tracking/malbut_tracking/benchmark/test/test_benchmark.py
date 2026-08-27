"""Contracts for the reusable person-following benchmark scenarios."""

import math
from pathlib import Path
import shutil
from xml.etree import ElementTree

import pytest
import yaml

from malbut_tracking.benchmark.scenario import instrument_world


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(__file__).resolve().parents[3]
REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
GAZEBO_ROOT = REPOSITORY_ROOT / 'malbut_gazebo'
CATALOG = BENCHMARK_ROOT / 'config' / 'scenarios.yaml'


def _actor_points(path, offset=(0.0, 0.0)):
    points = []
    root = ElementTree.parse(path).getroot()
    for waypoint in root.findall('actor/script/trajectory/waypoint'):
        values = [float(value) for value in waypoint.findtext('pose').split()]
        point = (offset[0] + values[0], offset[1] + values[1])
        if not points or point != points[-1]:
            points.append(point)
    return points


def _point_segment_distance(point, start, end):
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0.0:
        return math.dist(point, start)
    ratio = max(
        0.0,
        min(
            1.0,
            ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
            / length_squared,
        ),
    )
    closest = (start[0] + ratio * dx, start[1] + ratio * dy)
    return math.dist(point, closest)


def _read_pgm(path):
    raw = path.read_bytes()
    position = 0
    tokens = []
    while len(tokens) < 4:
        while chr(raw[position]).isspace():
            position += 1
        if raw[position:position + 1] == b'#':
            position = raw.index(b'\n', position) + 1
            continue
        end = position
        while not chr(raw[end]).isspace():
            end += 1
        tokens.append(raw[position:end])
        position = end
    while chr(raw[position]).isspace():
        position += 1
    assert tokens[0] == b'P5'
    width, height = int(tokens[1]), int(tokens[2])
    return width, height, raw[position:position + width * height]


def _map_clearance(map_yaml, route):
    metadata = yaml.safe_load(map_yaml.read_text(encoding='utf-8'))
    image = map_yaml.parent / metadata['image']
    width, height, pixels = _read_pgm(image)
    resolution = float(metadata['resolution'])
    origin_x, origin_y = [float(value) for value in metadata['origin'][:2]]
    minimum = math.inf
    for start, end in zip(route, route[1:]):
        samples = max(1, math.ceil(math.dist(start, end) / resolution))
        for index in range(samples + 1):
            ratio = index / samples
            x = start[0] + ratio * (end[0] - start[0])
            y = start[1] + ratio * (end[1] - start[1])
            column = int((x - origin_x) / resolution)
            map_row = int((y - origin_y) / resolution)
            row = height - 1 - map_row
            assert 0 <= column < width and 0 <= row < height
            for radius in range(1, 13):
                occupied = False
                for delta_row in range(-radius, radius + 1):
                    for delta_column in (-radius, radius):
                        scan_row = row + delta_row
                        scan_column = column + delta_column
                        if not (
                            0 <= scan_row < height
                            and 0 <= scan_column < width
                        ):
                            occupied = True
                            break
                        if pixels[scan_row * width + scan_column] < 250:
                            occupied = True
                            break
                    if occupied:
                        break
                if not occupied:
                    for delta_column in range(-radius + 1, radius):
                        for delta_row in (-radius, radius):
                            scan_row = row + delta_row
                            scan_column = column + delta_column
                            if pixels[scan_row * width + scan_column] < 250:
                                occupied = True
                                break
                        if occupied:
                            break
                if occupied:
                    minimum = min(minimum, (radius - 1) * resolution)
                    break
    return minimum


def test_catalog_defines_two_worlds_and_four_scenarios():
    catalog = yaml.safe_load(CATALOG.read_text(encoding='utf-8'))
    assert catalog['schema_version'] == 1
    scenarios = catalog['scenarios']
    assert set(scenarios) == {
        'test_arena_perimeter',
        'test_arena_complex',
        'small_house_front_door',
        'small_house_living_room',
    }
    assert {value['world_name'] for value in scenarios.values()} == {
        'test_arena',
        'small_house',
    }


@pytest.mark.parametrize(
    'name', ['test_arena_perimeter.sdf', 'test_arena_complex.sdf']
)
def test_test_arena_routes_clear_walls_and_physical_targets(name):
    route = _actor_points(BENCHMARK_ROOT / 'actors' / name)
    assert route[0] == route[-1]
    obstacles = (
        ((1.5, 0.0), 0.55),
        ((-1.2, 0.8), math.hypot(0.25, 0.25)),
        ((-1.2, -0.8), 0.15),
    )
    for point in route:
        assert -2.75 <= point[0] <= 2.75
        assert -1.75 <= point[1] <= 1.75
    for center, radius in obstacles:
        clearance = min(
            _point_segment_distance(center, start, end) - radius
            for start, end in zip(route, route[1:])
        )
        assert clearance >= 0.25 - 1e-9, (name, center, clearance)


def test_living_room_loop_has_map_and_geometry_clearance():
    route = _actor_points(
        BENCHMARK_ROOT / 'actors' / 'small_house_living_room.sdf'
    )
    assert route == [
        (-1.5, -0.2),
        (3.2, -0.2),
        (3.2, -4.0),
        (-1.5, -4.0),
        (-1.5, -0.2),
    ]
    assert _map_clearance(
        GAZEBO_ROOT / 'maps' / 'small_house.yaml', route
    ) >= 0.40


def test_front_door_scenario_reuses_the_verified_project_route():
    catalog = yaml.safe_load(CATALOG.read_text(encoding='utf-8'))
    actor = catalog['scenarios']['small_house_front_door']['actor_file']
    assert actor == {
        'package': 'malbut_gazebo',
        'path': 'models/humanoid_actor/scenarios/front_door_entry.sdf',
    }
    route = _actor_points(
        GAZEBO_ROOT / actor['path'], offset=(6.0, -6.2)
    )
    assert route[0] == route[-1] == (6.0, -6.2)
    assert len(route) >= 30


def test_benchmark_instruments_a_temporary_world_only():
    source = GAZEBO_ROOT / 'worlds' / 'test_arena.sdf'
    world = ElementTree.parse(source).getroot().find('world')
    assert world.find(
        "plugin[@name='malbut::gazebo::BenchmarkGroundTruthSystem']"
    ) is None
    prepared = instrument_world(
        source,
        robot_name='malbut',
        actor_name='benchmark_person',
        topic='/benchmark/ground_truth',
        publish_rate_hz=20.0,
    )
    try:
        prepared_world = ElementTree.parse(prepared).getroot().find('world')
        plugin = prepared_world.find(
            "plugin[@name='malbut::gazebo::BenchmarkGroundTruthSystem']"
        )
        assert plugin is not None
        assert plugin.findtext('robot_name') == 'malbut'
        assert plugin.findtext('actor_name') == 'benchmark_person'
        assert plugin.findtext('publish_rate') == '20'
    finally:
        shutil.rmtree(prepared.parent)


def test_benchmark_source_is_owned_by_tracking_package():
    source = (BENCHMARK_ROOT / 'evaluator.py').read_text(encoding='utf-8')
    launch = (
        BENCHMARK_ROOT / 'launch' / 'person_tracking_benchmark.launch.py'
    ).read_text(encoding='utf-8')
    assert "'scenario_name': scenario.name" in launch
    assert "executable='person_tracking_benchmark'" in launch
    assert "package='malbut_tracking'" in launch
    assert "'world': 'test_arena'" not in source
    assert "'trajectory': 'four_vertex_test_arena_rectangle'" not in source
    assert '/benchmark/ground_truth' not in (
        PACKAGE_ROOT / 'malbut_tracking' / 'person_follower_node.py'
    ).read_text(encoding='utf-8')
