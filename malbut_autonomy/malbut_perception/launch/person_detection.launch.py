"""Launch Malbut RGB-D person detection on simulation or robot topics."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    """Create the configurable person detection node launch description."""
    package_share = Path(get_package_share_directory('malbut_perception'))
    default_config = package_share / 'config' / 'person_detection.yaml'
    default_model = (
        Path.home() / '.cache' / 'malbut_perception' / 'yolo26n.onnx'
    )
    default_reid_model = (
        Path.home()
        / '.cache'
        / 'malbut_perception'
        / 'osnet_ain_x1_0_msmt17.onnx'
    )

    arguments = [
        DeclareLaunchArgument('config', default_value=str(default_config)),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument(
            'rgb_topic', default_value='/camera/color/image_raw'
        ),
        DeclareLaunchArgument(
            'depth_topic', default_value='/camera/depth/image_raw'
        ),
        DeclareLaunchArgument(
            'camera_info_topic',
            default_value='/camera/color/camera_info',
        ),
        DeclareLaunchArgument(
            'processing_trace_topic',
            default_value='/perception/sensor_processing_trace',
        ),
        DeclareLaunchArgument('detector_backend', default_value='auto'),
        DeclareLaunchArgument('model_path', default_value=str(default_model)),
        DeclareLaunchArgument('inference_backend', default_value='auto'),
        DeclareLaunchArgument('dnn_target', default_value='auto'),
        DeclareLaunchArgument('opencv_num_threads', default_value='4'),
        DeclareLaunchArgument('reid_backend', default_value='auto'),
        DeclareLaunchArgument(
            'reid_model_path', default_value=str(default_reid_model)
        ),
        DeclareLaunchArgument('output_frame', default_value=''),
        DeclareLaunchArgument('projection_frame', default_value=''),
        DeclareLaunchArgument('publish_debug_image', default_value='true'),
        DeclareLaunchArgument(
            'debug_image_transport', default_value='compressed'
        ),
        DeclareLaunchArgument('debug_jpeg_quality', default_value='80'),
    ]
    node = Node(
        package='malbut_perception',
        executable='person_localizer',
        name='person_localizer',
        output='screen',
        parameters=[
            LaunchConfiguration('config'),
            {
                'use_sim_time': LaunchConfiguration('use_sim_time'),
                'rgb_topic': LaunchConfiguration('rgb_topic'),
                'depth_topic': LaunchConfiguration('depth_topic'),
                'camera_info_topic': LaunchConfiguration('camera_info_topic'),
                'processing_trace_topic': LaunchConfiguration(
                    'processing_trace_topic'
                ),
                'detector_backend': LaunchConfiguration('detector_backend'),
                'model_path': LaunchConfiguration('model_path'),
                'inference_backend': LaunchConfiguration(
                    'inference_backend'
                ),
                'dnn_target': LaunchConfiguration('dnn_target'),
                'opencv_num_threads': LaunchConfiguration(
                    'opencv_num_threads'
                ),
                'reid_backend': LaunchConfiguration('reid_backend'),
                'reid_model_path': LaunchConfiguration('reid_model_path'),
                'output_frame': LaunchConfiguration('output_frame'),
                'projection_frame': LaunchConfiguration('projection_frame'),
                'publish_debug_image': LaunchConfiguration(
                    'publish_debug_image'
                ),
                'debug_image_transport': LaunchConfiguration(
                    'debug_image_transport'
                ),
                'debug_jpeg_quality': LaunchConfiguration(
                    'debug_jpeg_quality'
                ),
            },
        ],
    )
    return LaunchDescription(arguments + [node])
