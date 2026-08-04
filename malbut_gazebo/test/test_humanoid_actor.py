"""Contract tests for the RGB-D perception humanoid actor."""

import ast
import math
from pathlib import Path
from xml.etree import ElementTree


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ACTOR_ROOT = PACKAGE_ROOT / 'models' / 'humanoid_actor'
DEMO_LAUNCH = PACKAGE_ROOT / 'launch' / 'humanoid_demo.launch.py'


def _actor():
    root = ElementTree.parse(ACTOR_ROOT / 'model.sdf').getroot()
    actor = root.find('actor')
    assert actor is not None
    return actor


def _pose_values(waypoint):
    return [float(value) for value in waypoint.findtext('pose').split()]


def _launch_argument_defaults():
    tree = ast.parse(DEMO_LAUNCH.read_text(encoding='utf-8'))
    defaults = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        if node.func.id != 'DeclareLaunchArgument' or not node.args:
            continue
        if not isinstance(node.args[0], ast.Constant):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == 'default_value'
                and isinstance(keyword.value, ast.Constant)
            ):
                defaults[node.args[0].value] = keyword.value.value
    return defaults


def _read_pgm(path):
    with path.open('rb') as stream:
        assert stream.readline().strip() == b'P5'
        width, height = map(int, stream.readline().split())
        assert stream.readline().strip() == b'255'
        pixels = stream.read()
    assert len(pixels) == width * height
    return width, height, pixels


def test_humanoid_uses_a_local_animated_collada_skin():
    actor = _actor()
    skin_uri = actor.findtext('skin/filename')
    animation_uri = actor.findtext('animation/filename')
    assert skin_uri == 'model://humanoid_actor/meshes/walk.dae'
    assert animation_uri == skin_uri
    assert (ACTOR_ROOT / 'meshes' / 'walk.dae').is_file()
    assert actor.findtext('animation/interpolate_x') == 'true'


def test_humanoid_route_is_continuous_and_indoor_speed():
    actor = _actor()
    waypoints = actor.findall('script/trajectory/waypoint')
    times = [float(waypoint.findtext('time')) for waypoint in waypoints]
    poses = [_pose_values(waypoint) for waypoint in waypoints]
    assert times == sorted(set(times))
    assert poses[0] == poses[-1]
    assert all(math.isclose(pose[2], 1.0) for pose in poses)

    translation_speeds = []
    route_length = 0.0
    for start_time, end_time, start, end in zip(
        times, times[1:], poses, poses[1:]
    ):
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        route_length += distance
        translation_speeds.append(distance / (end_time - start_time))
    assert 26.0 <= route_length <= 26.5
    assert max(translation_speeds) <= 0.35 + 1e-3
    assert 93.0 <= times[-1] <= 95.0
    assert actor.findtext('script/loop') == 'true'
    assert actor.findtext('script/auto_start') == 'true'


def test_humanoid_is_a_camera_target_without_ground_truth_plugins():
    actor = _actor()
    assert actor.find('link') is None
    assert actor.find('plugin') is None


def test_default_route_stays_in_mapped_small_house_free_space():
    defaults = _launch_argument_defaults()
    assert defaults['world_name'] == 'small_house'
    offset_x = float(defaults['actor_x'])
    offset_y = float(defaults['actor_y'])

    width, height, pixels = _read_pgm(PACKAGE_ROOT / 'maps' / 'map_01.pgm')
    resolution = 0.05
    origin_x, origin_y = -5.04, -4.07
    clearance_pixels = round(0.35 / resolution)
    poses = [
        _pose_values(waypoint)
        for waypoint in _actor().findall('script/trajectory/waypoint')
    ]

    for start, end in zip(poses, poses[1:]):
        for step in range(25):
            fraction = step / 24
            world_x = offset_x + start[0] + (end[0] - start[0]) * fraction
            world_y = offset_y + start[1] + (end[1] - start[1]) * fraction
            center_x = round((world_x - origin_x) / resolution)
            center_y = height - 1 - round((world_y - origin_y) / resolution)
            for y in range(center_y - clearance_pixels, center_y + clearance_pixels + 1):
                for x in range(center_x - clearance_pixels, center_x + clearance_pixels + 1):
                    if math.hypot(x - center_x, y - center_y) > clearance_pixels:
                        continue
                    assert 0 <= x < width and 0 <= y < height
                    assert pixels[y * width + x] >= 250


def test_humanoid_asset_has_source_and_no_machine_specific_paths():
    assert (ACTOR_ROOT / 'model.config').is_file()
    source = (ACTOR_ROOT / 'SOURCE.md').read_text(encoding='utf-8')
    assert '49af0df3a319d1cb8ca2cebf02dbd00f' in source
    model = (ACTOR_ROOT / 'model.sdf').read_text(encoding='utf-8')
    assert '/home/' not in model
    assert '/Users/' not in model
    assert 'http://' not in model
    assert 'https://' not in model
