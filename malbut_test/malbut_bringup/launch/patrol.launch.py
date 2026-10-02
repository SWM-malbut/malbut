"""patrol: start only this module, without waiting for external ROS peers."""

from launch.substitutions import LaunchConfiguration

from malbut_bringup.launch_support import (
    description, include as _include, package_file as _package_file,
)

ARGUMENTS = [
    'static_map_topic', 'patrol_costmap_topic', 'robot_frame', 'rgb_topic',
    'camera_info_topic', 'room_map_file',
]


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    return [_include(_package_file(
        'malbut_patrol', 'launch/patrol.launch.py'), {
            'map_topic': value('static_map_topic'),
            'costmap_topic': value('patrol_costmap_topic'),
            'base_frame': value('robot_frame'),
            'camera_image_topic': value('rgb_topic'),
            'camera_info_topic': value('camera_info_topic'),
            'room_map_file': value('room_map_file'),
        })]


def generate_launch_description():
    return description('patrol', _setup, ARGUMENTS)
