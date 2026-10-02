"""autoslam: start only this module, without waiting for external ROS peers."""

from launch.substitutions import LaunchConfiguration

from malbut_bringup.launch_support import (
    description, include as _include, package_file as _package_file,
)

ARGUMENTS = ['map_directory', 'static_map_topic', 'robot_frame']


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    return [_include(_package_file(
        'malbut_autoslam', 'launch/autoslam.launch.py'), {
            'map_directory': value('map_directory'),
            'map_topic': value('static_map_topic'),
            'base_frame': value('robot_frame'),
        })]


def generate_launch_description():
    return description('autoslam', _setup, ARGUMENTS)
