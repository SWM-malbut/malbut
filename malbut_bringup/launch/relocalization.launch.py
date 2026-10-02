"""relocalization: start only this module, without waiting for external ROS peers."""

from launch_ros.actions import Node

from malbut_bringup.launch_support import (
    description,
)

ARGUMENTS = []


def _setup(context):
    return [Node(
        package='malbut_relocalization', executable='relocalization',
        name='relocalization', output='screen', parameters=[{'use_sim_time': False}])]


def generate_launch_description():
    return description('relocalization', _setup, ARGUMENTS)
