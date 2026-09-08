"""Start shared YOLO, shared identity tracking, and RGB-D localization."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument, GroupAction, IncludeLaunchDescription,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    """Wire public topics once, independently of any FollowPerson goal."""
    tracking_share = Path(get_package_share_directory('malbut_tracking'))
    yolo_share = Path(get_package_share_directory('malbut_yolo'))
    reid_share = Path(get_package_share_directory('malbut_reid'))
    cache = Path.home() / '.cache/malbut_perception'
    defaults = {
        'config': str(tracking_share / 'config/person_detection.yaml'),
        'use_sim_time': 'false',
        'rgb_topic': '/camera/color/image_raw',
        'depth_topic': '/camera/depth/image_raw',
        'camera_info_topic': '/camera/color/camera_info',
        'yolo_detections_topic': '/yolo/detections',
        'detections_2d_topic': '/perception/person/detections_2d',
        'detections_3d_topic': '/perception/person/detections_3d',
        'processing_trace_topic': '/perception/sensor_processing_trace',
        'yolo_processing_trace_topic': '/perception/yolo_processing_trace',
        'model_path': str(cache / 'yolo26n.pt'),
        'device': 'cuda:0',
        'python_executable': str(
            Path.home() / '.cache/malbut_yolo/runtime/bin/python'
        ),
        'reid_backend': 'auto',
        'reid_model_path': str(cache / 'osnet_ain_x1_0_msmt17.onnx'),
        'inference_backend': 'auto',
        'dnn_target': 'auto',
        'opencv_num_threads': '4',
        'projection_frame': '',
        'output_frame': '',
        'publish_debug_image': 'true',
        'debug_image_transport': 'compressed',
        'debug_jpeg_quality': '80',
    }
    arguments = [DeclareLaunchArgument(key, default_value=value)
                 for key, value in defaults.items()]
    yolo_arguments = {key: LaunchConfiguration(key) for key in (
        'use_sim_time', 'rgb_topic', 'model_path', 'device',
        'python_executable', 'opencv_num_threads',
    )}
    yolo_arguments.update({
        'config': str(yolo_share / 'config/yolo.yaml'),
        'namespace': 'yolo',
        'detections_topic': LaunchConfiguration('yolo_detections_topic'),
        'processing_trace_topic': LaunchConfiguration('yolo_processing_trace_topic'),
    })
    reid_arguments = {key: LaunchConfiguration(key) for key in (
        'use_sim_time', 'rgb_topic', 'yolo_detections_topic',
        'detections_2d_topic', 'reid_backend', 'reid_model_path',
        'inference_backend', 'dnn_target', 'opencv_num_threads',
    )}
    reid_arguments['config'] = str(
        reid_share / 'config/person_reidentification.yaml'
    )
    shared_nodes = [
        GroupAction([IncludeLaunchDescription(
            PythonLaunchDescriptionSource(str(share / 'launch' / filename)),
            launch_arguments=options.items(),
        )], scoped=True)
        for share, filename, options in (
            (yolo_share, 'yolo.launch.py', yolo_arguments),
            (reid_share, 'person_reidentification.launch.py', reid_arguments),
        )
    ]
    types = {'use_sim_time': bool, 'opencv_num_threads': int,
             'publish_debug_image': bool, 'debug_jpeg_quality': int}
    localization_parameters = {
        key: ParameterValue(LaunchConfiguration(key), value_type=types.get(key, str))
        for key in (
            'use_sim_time', 'rgb_topic', 'depth_topic', 'camera_info_topic',
            'detections_2d_topic', 'detections_3d_topic', 'yolo_processing_trace_topic',
            'processing_trace_topic', 'opencv_num_threads', 'projection_frame',
            'output_frame', 'publish_debug_image', 'debug_image_transport',
            'debug_jpeg_quality',
        )
    }
    localizer = Node(
        package='malbut_tracking', executable='person_localizer',
        name='person_localizer', output='screen',
        parameters=[LaunchConfiguration('config'), localization_parameters],
    )
    return LaunchDescription(arguments + shared_nodes + [localizer])
