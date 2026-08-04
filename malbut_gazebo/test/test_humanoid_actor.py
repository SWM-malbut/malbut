"""Contract tests for the RGB-D perception humanoid actor."""

import math
from pathlib import Path
from xml.etree import ElementTree


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ACTOR_ROOT = PACKAGE_ROOT / 'models' / 'humanoid_actor'


def _actor():
    root = ElementTree.parse(ACTOR_ROOT / 'model.sdf').getroot()
    actor = root.find('actor')
    assert actor is not None
    return actor


def _pose_values(waypoint):
    return [float(value) for value in waypoint.findtext('pose').split()]


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
    for start_time, end_time, start, end in zip(
        times, times[1:], poses, poses[1:]
    ):
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        translation_speeds.append(distance / (end_time - start_time))
    assert max(translation_speeds) <= 0.3 + 1e-9
    assert actor.findtext('script/loop') == 'true'
    assert actor.findtext('script/auto_start') == 'true'


def test_humanoid_is_a_camera_target_without_ground_truth_plugins():
    actor = _actor()
    assert actor.find('link') is None
    assert actor.find('plugin') is None


def test_humanoid_asset_has_source_and_no_machine_specific_paths():
    assert (ACTOR_ROOT / 'model.config').is_file()
    source = (ACTOR_ROOT / 'SOURCE.md').read_text(encoding='utf-8')
    assert '49af0df3a319d1cb8ca2cebf02dbd00f' in source
    model = (ACTOR_ROOT / 'model.sdf').read_text(encoding='utf-8')
    assert '/home/' not in model
    assert '/Users/' not in model
    assert 'http://' not in model
    assert 'https://' not in model
