"""patrol: start only this module, without waiting for external ROS peers."""

from launch.substitutions import LaunchConfiguration

from malbut_bringup.launch_support import (
    description, include as _include, package_file as _package_file,
)

ARGUMENTS = [
    'static_map_topic', 'patrol_costmap_topic', 'robot_frame', 'rgb_topic',
    'camera_info_topic', 'depth_topic', 'patrol_depth_aligned_to_rgb',
    'room_map_file', 'use_sim_time',
    'patrol_inspection_calibration_file', 'patrol_coverage_source',
    'patrol_require_room_entry', 'patrol_max_photos', 'patrol_analysis_wait_s',
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
            'depth_image_topic': value('depth_topic'),
            'depth_aligned_to_rgb': value('patrol_depth_aligned_to_rgb'),
            'room_map_file': value('room_map_file'),
            'inspection_calibration_file': value('patrol_inspection_calibration_file'),
            'coverage_source': value('patrol_coverage_source'),
            'require_room_entry': value('patrol_require_room_entry'),
            'max_photos': value('patrol_max_photos'),
            'analysis_wait_s': value('patrol_analysis_wait_s'),
            'use_sim_time': value('use_sim_time'),
            'start_enabled': 'false',
        })]


def generate_launch_description():
    return description('patrol', _setup, ARGUMENTS)
