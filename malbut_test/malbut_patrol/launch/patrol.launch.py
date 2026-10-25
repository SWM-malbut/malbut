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
        'camera_image_rectified': 'false',
        'depth_image_topic': '/camera/depth/image_raw',
        'depth_aligned_to_rgb': 'false',
        'depth_registered_frame': '',
        'depth_scale_m': '0.0',
        'scan_topic': '/scan',
        'reobserve_enabled': 'true',
        'reobserve_rounds': '2',
        'reobserve_targets': '2',
        'reobserve_analysis_timeout_s': '120.0',
        'reobserve_max_travel_m': '3.0',
        'return_tolerance_m': '0.25',
        'robot_length_m': '0.277',
        'robot_width_m': '0.212',
        'footprint_padding_m': '0.01',
        'goal_position_tolerance_m': '0.12',
        'base_frame': 'base_footprint',
        'nav2_action_name': 'navigate_to_pose',
        'spin_action_name': 'spin',
        'record_observations': ('true' if os.environ.get('MALBUT_PATROL_OBSERVATIONS', '1') == '1'
                                else 'false'),
        'observation_directory': os.environ.get(
            'MALBUT_PATROL_OBSERVATION_DIR', '~/.local/share/malbut/patrol-observations'),
        'inspection_calibration_file': '',
        'inspection_analysis_timeout_s': '120.0',
    }
    parameters = {name: LaunchConfiguration(name) for name in defaults}
    for name, kind in [('reobserve_enabled', bool), ('reobserve_rounds', int),
                       ('reobserve_targets', int), ('reobserve_analysis_timeout_s', float),
                       ('reobserve_max_travel_m', float), ('return_tolerance_m', float),
                       ('robot_length_m', float), ('robot_width_m', float),
                       ('footprint_padding_m', float), ('goal_position_tolerance_m', float)]:
        parameters[name] = ParameterValue(LaunchConfiguration(name), value_type=kind)
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
    parameters['camera_image_rectified'] = ParameterValue(
        LaunchConfiguration('camera_image_rectified'), value_type=bool)
    parameters['depth_registered_frame'] = ParameterValue(
        LaunchConfiguration('depth_registered_frame'), value_type=str)
    parameters['depth_scale_m'] = ParameterValue(
        LaunchConfiguration('depth_scale_m'), value_type=float)
    parameters['observation_directory'] = ParameterValue(
        LaunchConfiguration('observation_directory'), value_type=str)
    parameters['inspection_calibration_file'] = ParameterValue(
        LaunchConfiguration('inspection_calibration_file'), value_type=str)
    parameters['inspection_analysis_timeout_s'] = ParameterValue(
        LaunchConfiguration('inspection_analysis_timeout_s'), value_type=float)
    return LaunchDescription([
        *[DeclareLaunchArgument(name, default_value=value)
          for name, value in defaults.items()],
        Node(package='malbut_patrol', executable='patrol_manager',
             name='patrol_manager', output='screen', parameters=[parameters]),
    ])
