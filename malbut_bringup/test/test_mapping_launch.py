"""Check mapping prerequisite composition without starting any ROS processes."""

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription
from launch_ros.actions import Node
import pytest


@pytest.fixture
def mapping(tmp_path, monkeypatch):
    """Load the launch file with temporary stand-ins for vendor assets."""
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        'mapping_launch', root / 'launch/mapping_backend.launch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    for relative in ('slam/launch/include/robot.launch.py', 'slam/config/slam.yaml',
                     'navigation/launch/include/bringup.launch.py'):
        path = tmp_path / 'ros2_ws/src' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')
    module.get_package_share_directory = lambda _: str(root)
    return module


def _context(module, **values):
    result = LaunchContext()
    result.launch_configurations.update(values)
    for action in module.generate_launch_description().entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(result)
    return result


def test_mapping_starts_independent_components(mapping):
    """Do not use the factory SLAM launch that also starts duplicate drivers."""
    context = _context(mapping)
    actions = mapping._setup(context)
    nodes = [item for item in actions if isinstance(item, Node)]
    assert [node.node_executable for node in nodes] == [
        'scan_normalizer', 'sync_slam_toolbox_node']
    includes = [child for action in actions if isinstance(action, GroupAction)
                for child in action.get_sub_entities()
                if isinstance(child, IncludeLaunchDescription)]
    assert len(includes) == 2
    assert dict(includes[0].launch_arguments)['robot_name'] == '/'
    assert dict(includes[1].launch_arguments)['rtabmap'] == 'true'
    assert dict(includes[1].launch_arguments)['use_teb'] == 'false'


def test_reuse_does_not_load_vendor_files_or_start_processes(mapping):
    """All four prerequisites can be supplied by an existing runtime."""
    mapping.get_package_share_directory = lambda _: pytest.fail('unexpected lookup')
    context = _context(mapping, **{name: 'false' for name in (
        'start_hardware', 'start_slam', 'start_navigation', 'start_scan_adapter')})
    actions = mapping._setup(context)
    assert not any(isinstance(action, (Node, GroupAction)) for action in actions)


def test_missing_slam_config_fails_before_launch(mapping):
    """Never silently substitute a simulation or unrelated SLAM configuration."""
    with pytest.raises(RuntimeError, match='not found'):
        mapping._setup(_context(mapping, slam_params_file='/not/a/real/slam.yaml'))
