"""tracking: start only this module, without waiting for external ROS peers."""

from malbut_bringup.perception_setup import validate_perception_files

from launch.substitutions import LaunchConfiguration

from malbut_bringup.launch_support import (
    description, file_path as _file, include as _include, package_file as _package_file,
)

ARGUMENTS = [
    'rgb_topic', 'depth_topic', 'camera_info_topic', 'model_path', 'python_executable',
    'reid_python_executable', 'device', 'reid_model_path', 'inference_backend', 'dnn_target',
    'publish_debug_image', 'scan_topic', 'global_frame', 'robot_frame', 'static_map_topic',
    'global_costmap_topic', 'following_config', 'lidar_config',
]


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    perception_files = validate_perception_files(
        value('python_executable'), value('reid_python_executable'),
        value('model_path'), value('reid_model_path'))
    actions = []
    options = {name: value(name) for name in (
        'rgb_topic', 'depth_topic', 'camera_info_topic', 'model_path',
        'python_executable', 'reid_python_executable', 'device', 'reid_model_path',
        'inference_backend', 'dnn_target', 'publish_debug_image',
    )}
    options.update(perception_files)
    options['reid_backend'] = 'osnet'
    actions.append(_include(_package_file(
        'malbut_tracking', 'launch/person_detection.launch.py'), options))
    follower = {name: value(name) for name in (
        'scan_topic', 'global_frame', 'robot_frame', 'static_map_topic',
        'global_costmap_topic',
    )}
    if value('following_config'):
        follower['config'] = _file(value('following_config'), 'follower config')
    # An empty parent LaunchConfiguration otherwise shadows the child's
    # default, which ROS interprets as the current directory, not a YAML.
    follower['lidar_config'] = (
        _file(value('lidar_config'), 'LiDAR config') if value('lidar_config')
        else _package_file('malbut_tracking', 'config/lidar_foreground.yaml'))
    actions.append(_include(_package_file(
        'malbut_tracking', 'launch/person_following.launch.py'), follower))

    return actions


def generate_launch_description():
    return description('tracking', _setup, ARGUMENTS)
