"""Start reusable image-only person re-identification."""

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    """Expose connection and runtime settings without creating a mission."""
    package_share = Path(get_package_share_directory('malbut_reid'))
    cache_root = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    runtime_root = Path(
        os.environ.get('MALBUT_REID_RUNTIME') or cache_root / 'malbut_reid/runtime'
    )
    defaults = {
        'config': str(package_share / 'config' / 'person_reidentification.yaml'),
        'python_executable': str(runtime_root / 'bin/python'),
        'use_sim_time': 'false',
        'rgb_topic': '/camera/color/image_raw',
        'yolo_detections_topic': '/yolo/detections',
        'detections_2d_topic': '/perception/person/detections_2d',
        'reid_backend': 'auto',
        'reid_model_path': str(
            cache_root / 'malbut_perception'
            / 'osnet_ain_x1_0_msmt17.onnx'
        ),
        'inference_backend': 'auto',
        'dnn_target': 'auto',
        'opencv_num_threads': '4',
    }
    arguments = [
        DeclareLaunchArgument(name, default_value=value)
        for name, value in defaults.items()
    ]
    parameter_types = {'use_sim_time': bool, 'opencv_num_threads': int}
    node = Node(
        package='malbut_reid', executable='person_reidentifier',
        name='person_reidentifier', output='screen',
        prefix=['"', LaunchConfiguration('python_executable'), '"'],
        parameters=[
            LaunchConfiguration('config'),
            {
                name: ParameterValue(
                    LaunchConfiguration(name),
                    value_type=parameter_types.get(name, str),
                )
                for name in defaults if name not in {'config', 'python_executable'}
            },
        ],
    )
    return LaunchDescription(arguments + [node])
