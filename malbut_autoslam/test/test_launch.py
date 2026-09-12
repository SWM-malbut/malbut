"""Check launch shutdown timing without starting ROS processes."""

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters
import pytest

from malbut_autoslam.runtime import DEFAULT_READY_TIMEOUT_S, PROCESS_SHUTDOWN_STAGES


@pytest.mark.parametrize('override', [None, '0.5', '90.0'])
def test_parent_launch_grace_includes_configured_cancel_and_process_cleanup(override):
    """The parent cannot force-kill AutoSLAM during its normal owned-child cleanup."""
    source = Path(__file__).parents[1] / 'launch/autoslam.launch.py'
    spec = importlib.util.spec_from_file_location('autoslam_launch', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    launch = module.generate_launch_description()
    context = LaunchContext()
    if override is not None:
        context.launch_configurations['ready_timeout_s'] = override
    for action in launch.entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(context)
    server = next(action for action in launch.entities
                  if isinstance(action, Node) and action.node_package == 'malbut_autoslam')
    parameters = evaluate_parameters(context, server._Node__parameters)[0]
    ready_timeout = parameters['ready_timeout_s']
    assert ready_timeout == (DEFAULT_READY_TIMEOUT_S if override is None else float(override))
    # Humble ExecuteLocal stores these substitutions until actual process
    # execution. Evaluate the same data without spawning the Action server.
    grace = float(perform_substitutions(context, server._ExecuteLocal__sigterm_timeout))
    assert grace > ready_timeout + sum(timeout for _, timeout in PROCESS_SHUTDOWN_STAGES)
