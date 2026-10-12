"""autoslam: start only this module, without waiting for external ROS peers."""

from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackagePrefix

from malbut_bringup.launch_support import (
    description, file_path as _file, include as _include, package_file as _package_file,
)

ARGUMENTS = ['map_directory', 'static_map_topic', 'robot_frame', 'slam_params_file',
             'scan_topic', 'use_sim_time']


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    return [_include(_package_file(
        'malbut_autoslam', 'launch/autoslam.launch.py'), {
            'map_directory': value('map_directory'),
            'map_topic': value('static_map_topic'),
            'base_frame': value('robot_frame'),
            'start_mapping_service': '/malbut/localization/start_mapping',
            'stop_mapping_service': '/malbut/localization/stop_mapping',
            'ready_mapping_service': '/malbut/localization/mapping_ready',
            'slam_executable': PathJoinSubstitution([
                FindPackagePrefix('malbut_bringup'), 'lib', 'malbut_bringup',
                'sync_slam_toolbox']),
            'slam_params_file': (_file(value('slam_params_file'), 'SLAM parameters')
                                 if value('slam_params_file') else _package_file(
                                     'malbut_bringup', 'config/slam_toolbox.yaml')),
            'scan_topic': value('scan_topic'), 'use_sim_time': value('use_sim_time'),
        })]


def generate_launch_description():
    return description('autoslam', _setup, ARGUMENTS)
