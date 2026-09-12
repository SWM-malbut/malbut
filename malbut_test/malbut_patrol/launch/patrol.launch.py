"""Start the camera patrol Action server using an already running Nav2 map."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    """Expose environment inputs without preconfigured patrol coordinates."""
    defaults = {
        'use_sim_time': 'false',
        'room_map_file': '',
        'map_topic': '/map',
        'costmap_topic': '/global_costmap/costmap',
        'camera_image_topic': '/camera/color/image_raw',
        'camera_info_topic': '/camera/color/camera_info',
        'camera_optical_frame': '',
        'base_frame': 'base_footprint',
        'nav2_action_name': 'navigate_to_pose',
        'spin_action_name': 'spin',
    }
    parameters = {name: LaunchConfiguration(name) for name in defaults}
    parameters['use_sim_time'] = ParameterValue(
        LaunchConfiguration('use_sim_time'), value_type=bool)
    parameters['room_map_file'] = ParameterValue(
        LaunchConfiguration('room_map_file'), value_type=str)
    parameters['camera_optical_frame'] = ParameterValue(
        LaunchConfiguration('camera_optical_frame'), value_type=str)
    return LaunchDescription([
        *[DeclareLaunchArgument(name, default_value=value)
          for name, value in defaults.items()],
        Node(package='malbut_patrol', executable='patrol_manager',
             name='patrol_manager', output='screen', parameters=[parameters]),
    ])
