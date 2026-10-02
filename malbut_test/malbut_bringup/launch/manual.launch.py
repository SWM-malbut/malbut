"""manual: start only this module, without waiting for external ROS peers."""

from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.launch_support import (
    description,
)

ARGUMENTS = ['start_hardware']


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    actions = [Node(
        package='malbut_system_manager', executable='manual_control',
        name='manual_control', output='screen',
        parameters=[{'use_sim_time': False, 'teleop_topic': '/cmd_vel_teleop'}])]
    if value('start_hardware') == 'true':
        # Same vendor node and speeds as its hardware launch; only the output moves.
        actions.append(Node(
            package='peripherals', executable='joystick_control',
            name='joystick_control', output='screen', parameters=[{
                'use_sim_time': False, 'max_linear': 0.15, 'max_angular': 0.45,
                'disable_servo_control': True,
            }],
            remappings=[('controller/cmd_vel', '/cmd_vel_teleop')]))
    return actions


def generate_launch_description():
    return description('manual', _setup, ARGUMENTS)
