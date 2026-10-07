"""Keep cloud control and voice alive while the robot execution group is stopped."""

import os
from pathlib import Path
import sys
import uuid

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, LogInfo, OpaqueFunction
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.speech_audio import shared_xfm_source


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    # Namespace ownership is unique to this launch. Only these exact speech
    # nodes may coexist with a child robot bringup; unrelated duplicates fail.
    namespace = '/malbut/resident_voice_' + uuid.uuid4().hex
    environment = dict(context.environment)
    environment.update(HOMECAM_BACKEND_URL=value('backend_url'),
                       HOMECAM_DEVICE_TOKEN_FILE=value('token_file'),
                       MALBUT_RESIDENT_VOICE_NAMESPACE=namespace)
    actions = []
    voice = value('resident_voice') == 'true'
    if voice and value('input_device') == '0':
        try:
            source = shared_xfm_source(environment)
            environment.update(MALBUT_SHARED_MICROPHONE=source, PULSE_SOURCE=source)
        except RuntimeError as error:
            # An unavailable microphone must not take cloud control down.
            voice = False
            actions.append(LogInfo(msg=f'Resident voice unavailable: {error}'))
    actions.append(Node(
        package='malbut_bringup', executable='robot_cloud_sync',
        name='robot_cloud_sync', output='screen', additional_env=environment,
        parameters=[{
            'use_sim_time': False, 'manage_bringup': True, 'map_topic': '/map',
            'backend_url': value('backend_url'), 'token_file': value('token_file'),
            'map_directory': value('map_directory'),
            # Even a failed voice launch owns the profile. The robot child must
            # not silently launch a second speech runtime on subsequent starts.
            'resident_voice_namespace': namespace if value('resident_voice') == 'true' else '',
        }],
    ))
    if voice:
        arguments = [f'{name}:={value(name)}' for name in (
            'python_executable', 'stt_model_path', 'stt_library_path',
            'input_device', 'output_device', 'cpp_threads', 'input_has_aec',
            'agent_provider', 'agent_user_id', 'agent_conversation_db', 'navigation_targets',
            'preflight_timeout_s', 'peer_timeout_s') if value(name)]
        actions.append(ExecuteProcess(
            cmd=[sys.executable, '-m', 'malbut_bringup.resident_voice',
                 'control_server:=none', 'manager_commands:=true',
                 'device_operations:=true',
                 f'node_namespace:={namespace}', *arguments],
            name='resident_speech', output='screen', additional_env=environment,
            # The child LaunchService needs its own 5s INT + 5s TERM window
            # to reap speech workers before this outer launch terminates it.
            sigterm_timeout='20',
        ))
    return actions


def generate_launch_description():
    """Reuse robot speech defaults, with a separate child LaunchService."""
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'malbut_speech'
    runtime = Path(os.environ.get('MALBUT_SPEECH_RUNTIME', cache / 'runtime'))
    build = Path(os.environ.get('MALBUT_STT_BUILD_DIR', cache / 'whisper-cpp-build'))
    defaults = {
        'backend_url': EnvironmentVariable('HOMECAM_BACKEND_URL', default_value=''),
        'token_file': EnvironmentVariable('HOMECAM_DEVICE_TOKEN_FILE', default_value=''),
        'map_directory': EnvironmentVariable(
            'HOMECAM_MAP_DIRECTORY', default_value='~/.ros/malbut/maps'),
        'resident_voice': 'true', 'python_executable': str(runtime / 'bin/python'),
        'stt_model_path': os.environ.get(
            'MALBUT_STT_MODEL_PATH', str(cache / 'models/ggml-small.bin')),
        'stt_library_path': os.environ.get(
            'MALBUT_STT_LIBRARY_PATH', str(build / 'bin/libmalbut_whisper.so')),
        'input_device': '0', 'output_device': '-1', 'cpp_threads': '6',
        'input_has_aec': 'false', 'agent_provider': 'openai',
        'agent_user_id': 'speech-development-user',
        'agent_conversation_db': '~/.local/state/malbut/speech-dialogue.sqlite3',
        'navigation_targets': '', 'preflight_timeout_s': '120.0', 'peer_timeout_s': '30.0',
    }
    return LaunchDescription([
        *[DeclareLaunchArgument(name, default_value=default)
          for name, default in defaults.items()],
        OpaqueFunction(function=_setup),
    ])
