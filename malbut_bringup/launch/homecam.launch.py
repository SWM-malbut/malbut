"""homecam: start only this module, without waiting for external ROS peers."""

from launch.substitutions import LaunchConfiguration

from malbut_bringup.launch_support import (
    description, include as _include, package_file as _package_file,
)

ARGUMENTS = [
    'rgb_topic', 'camera_info_topic', 'odom_topic', 'fall_manager_runtime_id',
    'fall_bridge_runtime_id', 'fall_vlm_runtime_id',
]


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    backend_url = context.environment.get('HOMECAM_BACKEND_URL', '').strip()
    if not backend_url:
        raise RuntimeError('Homecam requires HOMECAM_BACKEND_URL')
    return [_include(_package_file(
        'homecam_media_agent', 'launch/homecam_robot.launch.py'), {
            'backend_url': backend_url,
            'device_id': context.environment.get('HOMECAM_DEVICE_ID', 'jetson-homecam'),
            'image_topic': value('rgb_topic'),
            'camera_info_topic': value('camera_info_topic'), 'odom_topic': value('odom_topic'),
            **({'audio_source': 'pulse'}
               if context.environment.get('MALBUT_SHARED_MICROPHONE') else {}),
            **{'fall_' + key + '_runtime_id': value('fall_' + key + '_runtime_id')
               for key in ('manager', 'bridge', 'vlm')},
        })]


def generate_launch_description():
    return description('homecam', _setup, ARGUMENTS)
