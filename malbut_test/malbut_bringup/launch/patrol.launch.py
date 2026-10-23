"""patrol: start only this module, without waiting for external ROS peers."""

from launch.substitutions import LaunchConfiguration

from malbut_bringup.launch_support import (
    description, include as _include, package_file as _package_file,
)

ARGUMENTS = [
    'static_map_topic', 'patrol_costmap_topic', 'robot_frame', 'rgb_topic',
    'camera_info_topic', 'depth_topic', 'patrol_depth_aligned_to_rgb',
    'patrol_camera_image_rectified', 'patrol_depth_registered_frame', 'patrol_depth_scale_m',
    'room_map_file', 'use_sim_time', 'scan_topic',
    'patrol_inspection_calibration_file', 'patrol_inspection_analysis_timeout_s',
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
            'scan_topic': value('scan_topic'),
            'depth_aligned_to_rgb': value('patrol_depth_aligned_to_rgb'),
            'camera_image_rectified': value('patrol_camera_image_rectified'),
            'depth_registered_frame': value('patrol_depth_registered_frame'),
            'depth_scale_m': value('patrol_depth_scale_m'),
            'room_map_file': value('room_map_file'),
            'inspection_calibration_file': value('patrol_inspection_calibration_file'),
            'inspection_analysis_timeout_s': value('patrol_inspection_analysis_timeout_s'),
            'use_sim_time': value('use_sim_time'),
            'start_enabled': 'false',
        })]


def generate_launch_description():
    return description('patrol', _setup, ARGUMENTS)
