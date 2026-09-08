"""Launch only upstream detection, without its optional tracking/3D nodes."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    """Keep model/runtime selection separate from ROS and JetPack installs."""
    share = Path(get_package_share_directory('malbut_yolo'))
    runtime_python = Path.home() / '.cache/malbut_yolo/runtime/bin/python'
    arguments = [
        DeclareLaunchArgument('config', default_value=str(share / 'config/yolo.yaml')),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('rgb_topic', default_value='/camera/color/image_raw'),
        DeclareLaunchArgument('detections_topic', default_value='/yolo/detections'),
        DeclareLaunchArgument('namespace', default_value='yolo'),
        DeclareLaunchArgument('model_path', default_value=str(
            Path.home() / '.cache/malbut_perception/yolo26n.pt'
        )),
        DeclareLaunchArgument('device', default_value='cuda:0'),
        DeclareLaunchArgument('python_executable', default_value=str(runtime_python)),
        DeclareLaunchArgument('opencv_num_threads', default_value='4'),
        DeclareLaunchArgument(
            'processing_trace_topic',
            default_value='/perception/yolo_processing_trace',
        ),
    ]
    node = Node(
        package='malbut_yolo', executable='yolo_node', name='yolo_node',
        namespace=LaunchConfiguration('namespace'), output='screen',
        prefix=['"', LaunchConfiguration('python_executable'), '"'],
        parameters=[LaunchConfiguration('config'), {
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'model': LaunchConfiguration('model_path'),
            'device': ParameterValue(LaunchConfiguration('device'), value_type=str),
            'opencv_num_threads': LaunchConfiguration('opencv_num_threads'),
            'processing_trace_topic': LaunchConfiguration('processing_trace_topic'),
        }],
        remappings=[('image_raw', LaunchConfiguration('rgb_topic')),
                    ('detections', LaunchConfiguration('detections_topic'))],
    )
    return LaunchDescription(arguments + [node])
