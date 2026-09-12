"""Check isolated ReID runtime routing without starting inference."""

import importlib.util
from pathlib import Path
import shlex

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters


PACKAGE_ROOT = Path(__file__).parents[1]


def _launch():
    source = PACKAGE_ROOT / 'launch/person_reidentification.launch.py'
    spec = importlib.util.spec_from_file_location('reid_launch_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.get_package_share_directory = lambda _: str(PACKAGE_ROOT)
    description = module.generate_launch_description()
    context = LaunchContext()
    for action in description.entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(context)
    node = next(action for action in description.entities if isinstance(action, Node))
    return context, node


def test_launch_defaults_to_isolated_runtime(monkeypatch):
    """The runtime path follows the same cache override as the installer."""
    monkeypatch.delenv('MALBUT_REID_RUNTIME', raising=False)
    monkeypatch.setenv('XDG_CACHE_HOME', '/tmp/reid-test-cache')
    context, _ = _launch()
    assert context.launch_configurations['python_executable'] == (
        '/tmp/reid-test-cache/malbut_reid/runtime/bin/python'
    )


def test_launch_routes_selected_interpreter_without_ros_parameter(monkeypatch):
    """A custom interpreter path is quoted and never sent to the ROS node."""
    monkeypatch.setenv('MALBUT_REID_RUNTIME', '/tmp/reid-test-runtime')
    context, node = _launch()
    assert context.launch_configurations['python_executable'] == (
        '/tmp/reid-test-runtime/bin/python'
    )
    context.launch_configurations['python_executable'] = '/tmp/reid runtime/bin/python'
    prefix = perform_substitutions(context, node.process_description.prefix)
    assert shlex.split(prefix) == ['/tmp/reid runtime/bin/python']
    parameters = evaluate_parameters(context, node._Node__parameters)
    overrides = next(value for value in parameters if isinstance(value, dict))
    assert 'python_executable' not in overrides
    assert overrides['use_sim_time'] is False
