"""fall: start only this module, without waiting for external ROS peers."""

import os
from pathlib import Path
import shlex
from uuid import uuid4

from launch.actions import LogInfo
from malbut_bringup.fall_setup import prepare_fall_monitor

from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.launch_support import (
    description, file_path as _file,
)

ARGUMENTS = [
    'fall_monitor', 'fall_config', 'fall_pose_model_path', 'fall_pose_python_executable',
    'fall_pose_execution_provider', 'fall_pose_intra_op_num_threads',
    'fall_pose_opencv_num_threads', 'fall_pose_allow_spinning', 'rgb_topic', 'odom_topic',
    'fall_manager_runtime_id', 'fall_bridge_runtime_id', 'fall_vlm_runtime_id',
]


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    fall_inputs = prepare_fall_monitor(value('fall_monitor'), value('fall_config'))
    fall_monitor = None
    fall_pose = None
    fall_coordinator = None
    # The legacy "manager" wire slot now belongs to the fall coordinator.
    fall_ids = {key: value('fall_' + key + '_runtime_id') or str(uuid4())
                for key in ('manager', 'bridge', 'vlm')}
    if fall_inputs is not None:
        fall_config, fall_image_topic = fall_inputs
        fall_coordinator = Node(
            package='malbut_fall_coordinator', executable='fall_coordinator',
            name='fall_coordinator', output='screen',
            parameters=[{
                'use_sim_time': False, 'runtime_id': fall_ids['manager'],
                'bridge_runtime_id': fall_ids['bridge'], 'vlm_runtime_id': fall_ids['vlm'],
            }],
        )
        pose_model = _file(value('fall_pose_model_path'), 'Fall pose ONNX model')
        pose_python = str(Path(value('fall_pose_python_executable')).expanduser())
        _file(pose_python, 'Fall pose Python')
        if not os.access(pose_python, os.X_OK):
            raise RuntimeError('Fall pose Python is not executable')
        pose_provider = value('fall_pose_execution_provider')
        pose_threads = int(value('fall_pose_intra_op_num_threads'))
        cv_threads = int(value('fall_pose_opencv_num_threads'))
        pose_spinning = value('fall_pose_allow_spinning')
        if (pose_provider not in ('auto', 'cpu', 'cuda') or not 0 <= pose_threads <= 256
                or not 0 <= cv_threads <= 256 or pose_spinning not in ('true', 'false')):
            raise RuntimeError('Invalid fall pose execution options')
        fall_pose = Node(
            package='homecam_detector', executable='homecam_detector_node',
            name='malbut_fall_pose', output='screen', prefix=[shlex.quote(pose_python)],
            parameters=[{
                'use_sim_time': False, 'fall_only': True,
                'fall_runtime_id': fall_ids['vlm'],
                'image_topic': value('rgb_topic'), 'odom_topic': value('odom_topic'),
                'pose_model_path': pose_model, 'pose_keep_aspect': True,
                'pose_execution_provider': pose_provider,
                'pose_intra_op_num_threads': pose_threads,
                'pose_allow_spinning': pose_spinning == 'true',
                'pose_opencv_num_threads': cv_threads,
                'pose_inference_fps': 5.0,
                'pose_candidate_confidence_threshold': 0.10,
            }],
        )
        fall_monitor = Node(
            package='malbut_agent_server', executable='malbut-fall-monitor',
            output='screen', arguments=['--config', fall_config, '--execute'],
            parameters=[{'use_sim_time': False, 'manager_runtime_id': fall_ids['manager'],
                         'runtime_id': fall_ids['vlm']}],
            remappings=[(fall_image_topic, value('rgb_topic'))],
        )

    if fall_inputs is None:
        return [LogInfo(msg='Fall module disabled: no configured fall runtime.')]
    return [fall_coordinator, fall_monitor, fall_pose]


def generate_launch_description():
    return description('fall', _setup, ARGUMENTS)
