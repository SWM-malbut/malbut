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
    assert 60.0 <= route_length <= 61.0
    assert max(translation_speeds) <= 0.7 + 2e-3
    assert 108.0 <= times[-1] <= 109.0
    assert actor.findtext('script/loop') == 'true'
    assert actor.findtext('script/auto_start') == 'true'


def test_humanoid_is_a_camera_target_without_ground_truth_plugins():
    actor = _actor()
    assert actor.find('link') is None
    assert actor.find('plugin') is None


def test_default_route_covers_the_full_small_house():
    defaults = _launch_argument_defaults()
    assert defaults['world_name'] == 'small_house'
    offset_x = float(defaults['actor_x'])
    offset_y = float(defaults['actor_y'])

    poses = [
        _pose_values(waypoint)
        for waypoint in _actor().findall('script/trajectory/waypoint')
    ]
    points = [(offset_x + pose[0], offset_y + pose[1]) for pose in poses]
    assert min(x for x, _ in points) < -7.5
    assert max(x for x, _ in points) > 8.0
    assert min(y for _, y in points) < -4.4
    assert max(y for _, y in points) > 4.1
    assert any(x < -7.0 and y > 2.0 for x, y in points)
    assert any(abs(x) < 0.6 and y < -4.2 for x, y in points)
    assert any(x > 8.0 and y > 2.0 for x, y in points)


def test_humanoid_asset_has_source_and_no_machine_specific_paths():
    assert (ACTOR_ROOT / 'model.config').is_file()
    source = (ACTOR_ROOT / 'SOURCE.md').read_text(encoding='utf-8')
    assert '49af0df3a319d1cb8ca2cebf02dbd00f' in source
    model = (ACTOR_ROOT / 'model.sdf').read_text(encoding='utf-8')
    assert '/home/' not in model
    assert '/Users/' not in model
    assert 'http://' not in model
    assert 'https://' not in model
