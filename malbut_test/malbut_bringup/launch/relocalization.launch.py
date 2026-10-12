"""relocalization: start only this module, without waiting for external ROS peers."""

from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.launch_support import (
    description,
)

ARGUMENTS = ['scan_topic', 'use_sim_time']


def _setup(context):
    return [Node(
        package='malbut_relocalization', executable='relocalization',
        name='relocalization', output='screen', parameters=[{
            'scan_topic': LaunchConfiguration('scan_topic').perform(context),
            'use_sim_time': LaunchConfiguration('use_sim_time').perform(context) == 'true'}])]


def generate_launch_description():
    return description('relocalization', _setup, ARGUMENTS)
