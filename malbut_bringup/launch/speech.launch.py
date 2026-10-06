"""Start independent speech peers without waiting for robot control."""

from math import isfinite
from pathlib import Path
import shlex
import sys

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument, EmitEvent, ExecuteProcess, OpaqueFunction,
    LogInfo, RegisterEventHandler, TimerAction,
)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.launch_support import file_path, module_actions


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    for name in ('stt_model_path', 'stt_library_path', 'python_executable',
                 'agent_user_id', 'agent_conversation_db'):
        if not value(name).strip():
            raise RuntimeError(f'{name} must be explicitly configured')
    timeouts = {}
    for name in ('preflight_timeout_s', 'peer_timeout_s'):
        timeouts[name] = float(value(name))
        if not isfinite(timeouts[name]) or timeouts[name] <= 0:
            raise RuntimeError(f'{name} must be finite and positive')
    python = str(Path(value('python_executable')).expanduser())
    model = str(Path(value('stt_model_path')).expanduser())
    library = str(Path(value('stt_library_path')).expanduser())
    input_device = int(value('input_device'))
    output_device = int(value('output_device'))
    threads = int(value('cpp_threads'))
    # Scoped includes restore LaunchConfigurations before exit callbacks run.
    # Capture these now so the parent's YOLO/runtime settings cannot leak in.
    agent_provider = value('agent_provider')
    agent_user_id = value('agent_user_id')
    agent_conversation_db = value('agent_conversation_db')
    manager_commands = value('manager_commands') == 'true'
    navigation_targets = value('navigation_targets')
    preflight_only = value('preflight_only') == 'true'
    input_has_aec = value('input_has_aec') == 'true'
    command = [python, '-m', 'malbut_bringup.speech_preflight']
    supervised = [python, '-m', 'malbut_bringup.speech_process',
                  '--startup-timeout-s', str(timeouts['preflight_timeout_s'])]
    preflight = ExecuteProcess(
        cmd=[*supervised, '--', *command,
             '--stt-model-path', model, '--stt-library-path', library,
             '--input-device', str(input_device), '--output-device', str(output_device),
             '--cpp-threads', str(threads), '--agent-provider', agent_provider],
        name='speech_preflight', output='screen',
    )
    stage = 'preflight'
    runtime_nodes = []
    stt = None

    def fail(reason):
        nonlocal stage
        stage = 'stopped'
        # LaunchService shuts children down and returns nonzero on exceptions.
        # A plain Shutdown event would incorrectly report a failed check as 0.
        raise RuntimeError(reason)

    def watchdog(expected_stage, timeout):
        def expired(launch_context):
            if launch_context.is_shutdown or stage != expected_stage:
                return []
            return fail(f'Speech {expected_stage} timed out after {timeout} seconds')
        return TimerAction(period=timeout, actions=[OpaqueFunction(function=expired)])

    def preflight_exited(event, launch_context):
        nonlocal stage
        if launch_context.is_shutdown or stage != 'preflight':
            return []
        if event.returncode != 0:
            return fail('Speech preflight failed')
        if preflight_only:
            stage = 'stopped'
            return [EmitEvent(event=Shutdown(
                reason='Speech preflight passed; preflight_only is complete'))]
        return start_runtime(launch_context)

    def start_runtime(launch_context):
        nonlocal stage, stt
        if launch_context.is_shutdown:
            return []
        for path, label in ((python, 'speech Python'), (model, 'STT model'),
                            (library, 'STT CUDA library')):
            file_path(path, label)
        config = Path(get_package_share_directory('malbut_stt')) / 'config/jetson.yaml'
        if not config.is_file():
            return fail(f'Speech STT configuration is missing: {config}')
        prefix = shlex.quote(python)
        mission_arguments = []
        if manager_commands:
            mission_arguments.append('--enable-manager-commands')
            if navigation_targets.strip():
                mission_arguments.extend(['--navigation-targets', navigation_targets])
        agent = Node(
            package='malbut_agent_server', executable='agent_communication',
            prefix=prefix, output='screen', arguments=[
                '--provider', agent_provider, '--user-id', agent_user_id,
                '--conversation-db', agent_conversation_db,
                *mission_arguments,
            ],
        )
        tts = Node(
            package='malbut_tts', executable='tts_node', prefix=prefix, output='screen',
            parameters=[{'backend': 'openai', 'output_device': output_device}],
        )
        weather = Node(
            package='malbut_agent_server', executable='weather',
            prefix=prefix, output='screen',
        )
        # The OpenAI/KMA keys the owner sets on the web; it stays off without HOMECAM_* settings.
        key_sync = Node(
            package='malbut_agent_server', executable='key_sync',
            prefix=prefix, output='screen',
        )
        stt = Node(
            package='malbut_stt', executable='stt', output='screen',
            prefix=shlex.join([*supervised, '--wait-for-ready', '--', python]),
            parameters=[str(config), {
                'stt_model_path': model, 'stt_library_path': library,
                'device_index': input_device, 'cpp_threads': threads,
                'wake_chime_device_index': output_device,
                'input_has_aec': input_has_aec,
            }],
        )
        runtime_nodes.extend([agent, tts, stt, weather, key_sync])
        stage = 'running'
        return [agent, tts, weather, stt, key_sync]

    def child_exited(event, launch_context):
        if launch_context.is_shutdown or event.action not in runtime_nodes:
            return []
        # A module-local failure must not shut down another module.
        # Module-aware recovery is a separate follow-up.
        return [LogInfo(msg=f'Speech child stopped: {event.process_name} '
                            f'(code {event.returncode}); other modules remain running')]

    registrations = [
        RegisterEventHandler(OnProcessExit(target_action=preflight, on_exit=preflight_exited)),
        RegisterEventHandler(OnProcessExit(on_exit=child_exited)),
    ]
    if preflight_only:
        return [*registrations, preflight, watchdog('preflight', timeouts['preflight_timeout_s'])]
    return [*registrations, *start_runtime(context)]


def generate_launch_description():
    """Expose robot speech settings without starting navigation or missions."""
    defaults = {
        'stt_model_path': '', 'stt_library_path': '',
        'input_device': '0', 'output_device': '-1', 'cpp_threads': '6',
        'input_has_aec': 'false', 'agent_provider': 'openai',
        'agent_user_id': 'speech-development-user',
        'agent_conversation_db': '~/.local/state/malbut/speech-dialogue.sqlite3',
        'python_executable': sys.executable, 'preflight_only': 'false',
        'preflight_timeout_s': '120.0', 'peer_timeout_s': '30.0',
        # Accepted for old callers; external control never gates speech startup.
        'control_server': 'none',
        'manager_commands': 'true', 'navigation_targets': '',
    }
    choices = {
        'input_has_aec': ['true', 'false'], 'preflight_only': ['true', 'false'],
        'agent_provider': ['openai', 'mock'],
        'control_server': ['none', 'manager', 'autoslam'],
        'manager_commands': ['true', 'false'],
    }
    return LaunchDescription([
        *[DeclareLaunchArgument(name, default_value=default, choices=choices.get(name))
          for name, default in defaults.items()],
        module_actions('speech', _setup),
    ])
