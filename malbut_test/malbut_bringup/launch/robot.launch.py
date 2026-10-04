"""robot: start only this module, without waiting for external ROS peers."""

from malbut_bringup.nav2_stack import nav2_actions, navigation_profiles

from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.launch_support import (
    description, file_path as _file, include as _include, package_file as _package_file,
)

ARGUMENTS = [
    'start_hardware', 'hardware_launch_file', 'nav2_params_file', 'slam_params_file', 'map',
    'scan_topic', 'odom_topic', 'restore_pose',
]


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    hardware_path = None
    if value('start_hardware') == 'true':
        hardware_path = (_file(value('hardware_launch_file'), 'hardware launch')
                         if value('hardware_launch_file') else _package_file(
                             'slam', 'launch/include/robot.launch.py'))
    params = (_file(value('nav2_params_file'), 'Nav2 parameters')
              if value('nav2_params_file') else _package_file(
                  'malbut_bringup', 'config/nav2_params.yaml'))
    slam_params = (_file(value('slam_params_file'), 'SLAM parameters')
                   if value('slam_params_file') else _package_file(
                       'malbut_bringup', 'config/slam_toolbox.yaml'))
    # No map uses only odometry and live obstacles; SLAM starts on request.
    initial_map = _file(value('map'), 'saved map YAML') if value('map') else ''

    hardware_actions = []
    if hardware_path:
        hardware_actions.append(_include(hardware_path, {
            # This vendor version uses '/' (not '') for unprefixed TF/topics.
            'sim': 'false', 'robot_name': '/', 'master_name': '/',
            # Aurora's upstream launch accepts this inherited argument. Person
            # tracking uses the RGB/depth images; nothing needs a point cloud.
            'point_cloud_enable': 'false',
            # The joystick feeds manual_drive instead of the driver, so its
            # commands can no longer mix with autonomous /cmd_vel output.
            'use_joy': 'false',
        }, environment={
            'need_compile': 'False',
            # Scope DDS changes to the vendor group that launches Aurora.
            # Receivers and other Bringup stages retain their own settings.
            'RMW_IMPLEMENTATION': 'rmw_fastrtps_cpp',
            'FASTRTPS_DEFAULT_PROFILES_FILE': _package_file(
                'malbut_bringup', 'config/fastdds_camera.xml'),
        }))

    actions = [*hardware_actions, *nav2_actions(
        params, scan_topic=value('scan_topic'), odom_topic=value('odom_topic'),
        mapless=not bool(initial_map))]
    actions.append(Node(
        package='malbut_system_manager', executable='system_manager',
        name='system_manager', output='screen', parameters=[{
            'use_sim_time': False,
            # Manager owns admission/safety; optional modules do not gate it.
            'ready_topic': '',
            'localization_control': True, 'initial_map': initial_map,
            'slam_params_file': slam_params, 'scan_topic': value('scan_topic'),
            'navigation_profiles': navigation_profiles(params),
            'relocalize_action': '/relocalize' if value('restore_pose') == 'true' else '',
        }],
    ))
    return actions


def generate_launch_description():
    return description('robot', _setup, ARGUMENTS)
