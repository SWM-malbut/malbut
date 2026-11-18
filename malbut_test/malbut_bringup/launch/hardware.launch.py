"""Start ROSOrin hardware with the Malbut velocity-command watchdog."""

from pathlib import Path

from launch import LaunchDescription
from launch.actions import OpaqueFunction
from launch_ros.actions import Node
from nav2_common.launch import ReplaceString

from malbut_bringup.launch_support import file_path, include


# Match the vendor's need_compile=False paths used by robot.launch.py.
VENDOR_ROOT = Path('/home/ubuntu/ros2_ws/src')


def _vendor_file(relative):
    return file_path(VENDOR_ROOT / relative, f'ROSOrin {relative}')


def _setup(_context):
    ekf_params = ReplaceString(
        source_file=_vendor_file('driver/controller/config/ekf.yaml'),
        replacements={'namespace/': ''})
    return [
        include(_vendor_file(
            'simulations/rosorin_description/launch/robot_description.launch.py'), {
                'frame_prefix': '', 'use_gui': 'false', 'use_rviz': 'false',
                'use_namespace': 'false', 'namespace': '', 'use_sim_time': 'false',
            }),
        include(_vendor_file(
            'driver/ros_robot_controller/launch/ros_robot_controller.launch.py'), {
                'imu_frame': 'imu_link',
            }),
        include(_vendor_file('peripherals/launch/imu_filter.launch.py'), {}),
        include(_vendor_file('driver/servo_controller/launch/servo_controller.launch.py'), {
            'base_frame': 'base_footprint',
        }),
        include(_vendor_file('peripherals/launch/depth_camera.launch.py'), {
            'depth_camera_name': 'depth_cam', 'tf_prefix': '', 'point_cloud_enable': 'false',
        }),
        include(_vendor_file('peripherals/launch/lidar.launch.py'), {
            'lidar_frame': 'lidar_frame', 'scan_topic': '/scan_raw', 'scan_raw': '/scan_raw',
        }),
        Node(
            package='robot_localization', executable='ekf_node', name='ekf_filter_node',
            output='screen', parameters=[ekf_params, {'use_sim_time': False}],
            remappings=[
                ('/tf', 'tf'), ('/tf_static', 'tf_static'),
                ('odometry/filtered', 'odom'), ('cmd_vel', 'controller/cmd_vel'),
            ],
        ),
        Node(
            package='malbut_bringup', executable='velocity_controller', name='odom_publisher',
            output='screen', parameters=[
                _vendor_file('driver/controller/config/calibrate_params.yaml'), {
                    'base_frame_id': 'base_footprint', 'odom_frame_id': 'odom',
                    'pub_odom_topic': True,
                },
            ],
        ),
    ]


def generate_launch_description():
    """Compose the hardware used by the root-namespace robot launch."""
    return LaunchDescription([OpaqueFunction(function=_setup)])
