"""Start the camera patrol Action server using an already running Nav2 map."""

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    """Expose environment inputs without preconfigured patrol coordinates."""
    defaults = {
        'use_sim_time': 'false',
        'start_enabled': 'true',
        'room_map_file': '',
        'map_topic': '/map',
        'costmap_topic': '/global_costmap/costmap',
        'camera_image_topic': '/camera/color/image_raw',
        'camera_info_topic': '/camera/color/camera_info',
        'camera_optical_frame': '',
        'depth_image_topic': '/camera/depth/image_raw',
        'depth_aligned_to_rgb': 'false',
        'base_frame': 'base_footprint',
        'nav2_action_name': 'navigate_to_pose',
        'spin_action_name': 'spin',
        'record_observations': ('true' if os.environ.get('MALBUT_PATROL_OBSERVATIONS', '1') == '1'
                                else 'false'),
        'observation_directory': os.environ.get(
            'MALBUT_PATROL_OBSERVATION_DIR', '~/.local/share/malbut/patrol-observations'),
        'inspection_calibration_file': '',
        'coverage_source': 'auto',
        'require_room_entry': 'false',
        'max_photos': '96',
        'analysis_wait_s': '120.0',
    }
    parameters = {name: LaunchConfiguration(name) for name in defaults}
    parameters['use_sim_time'] = ParameterValue(
        LaunchConfiguration('use_sim_time'), value_type=bool)
    parameters['start_enabled'] = ParameterValue(
        LaunchConfiguration('start_enabled'), value_type=bool)
    parameters['room_map_file'] = ParameterValue(
        LaunchConfiguration('room_map_file'), value_type=str)
    parameters['camera_optical_frame'] = ParameterValue(
        LaunchConfiguration('camera_optical_frame'), value_type=str)
    parameters['record_observations'] = ParameterValue(
        LaunchConfiguration('record_observations'), value_type=bool)
    parameters['depth_aligned_to_rgb'] = ParameterValue(
        LaunchConfiguration('depth_aligned_to_rgb'), value_type=bool)
    parameters['observation_directory'] = ParameterValue(
        LaunchConfiguration('observation_directory'), value_type=str)
    for name in ('inspection_calibration_file', 'coverage_source'):
        parameters[name] = ParameterValue(LaunchConfiguration(name), value_type=str)
    parameters['require_room_entry'] = ParameterValue(
        LaunchConfiguration('require_room_entry'), value_type=bool)
    parameters['max_photos'] = ParameterValue(LaunchConfiguration('max_photos'), value_type=int)
    parameters['analysis_wait_s'] = ParameterValue(
        LaunchConfiguration('analysis_wait_s'), value_type=float)
    return LaunchDescription([
        *[DeclareLaunchArgument(name, default_value=value)
          for name, value in defaults.items()],
        Node(package='malbut_patrol', executable='patrol_manager',
             name='patrol_manager', output='screen', parameters=[parameters]),
    ])
