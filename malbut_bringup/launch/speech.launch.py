"""Preflight the robot speech runtime before starting Agent, TTS, then STT."""

from math import isfinite
from pathlib import Path
import shlex
import sys

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument, EmitEvent, ExecuteProcess, OpaqueFunction,
    RegisterEventHandler, TimerAction,
)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    for name in ('stt_model_path', 'stt_library_path', 'python_executable'):
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
    preflight_only = value('preflight_only') == 'true'
    input_has_aec = value('input_has_aec') == 'true'
    control_server = value('control_server')
    command = [python, '-m', 'malbut_bringup.speech_preflight']
    control = ExecuteProcess(
        cmd=[*command, '--wait-for-control', control_server,
             '--timeout-s', str(timeouts['peer_timeout_s'])],
        name='speech_control_readiness', output='screen',
    ) if control_server != 'none' else None
    preflight = ExecuteProcess(
        cmd=[*command, '--stt-model-path', model, '--stt-library-path', library,
             '--input-device', str(input_device), '--output-device', str(output_device),
             '--cpp-threads', str(threads), '--agent-provider', agent_provider],
        name='speech_preflight', output='screen',
    )
    peers = ExecuteProcess(
        cmd=[*command, '--wait-for-peers', '--timeout-s', str(timeouts['peer_timeout_s'])],
        name='speech_peer_readiness', output='screen',
    )
    stage = 'control' if control else 'preflight'
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
        nonlocal stage, stt
        if launch_context.is_shutdown or stage != 'preflight':
            return []
        if event.returncode != 0:
            return fail('Speech preflight failed')
        if preflight_only:
            stage = 'stopped'
            return [EmitEvent(event=Shutdown(
                reason='Speech preflight passed; preflight_only is complete'))]
        config = Path(get_package_share_directory('malbut_stt')) / 'config/jetson.yaml'
        if not config.is_file():
            return fail(f'Speech STT configuration is missing: {config}')
        prefix = shlex.quote(python)
        agent = Node(
            package='malbut_agent_server', executable='agent_communication',
            prefix=prefix, output='screen', arguments=['--provider', agent_provider],
        )
        tts = Node(
            package='malbut_tts', executable='tts_node', prefix=prefix, output='screen',
            parameters=[{'backend': 'openai', 'output_device': output_device}],
        )
        stt = Node(
            package='malbut_stt', executable='stt', prefix=prefix, output='screen',
            parameters=[str(config), {
                'stt_model_path': model, 'stt_library_path': library,
                'device_index': input_device, 'cpp_threads': threads,
                'input_has_aec': input_has_aec,
            }],
        )
        runtime_nodes.extend([agent, tts, stt])
        stage = 'peers'
        return [agent, tts, peers, watchdog('peers', timeouts['peer_timeout_s'])]

    def control_exited(event, launch_context):
        nonlocal stage
        if launch_context.is_shutdown or stage != 'control':
            return []
        if event.returncode != 0:
            return fail('Speech robot control readiness failed')
        stage = 'preflight'
        return [preflight, watchdog('preflight', timeouts['preflight_timeout_s'])]

    def peers_exited(event, launch_context):
        nonlocal stage
        if launch_context.is_shutdown or stage != 'peers':
            return []
        if event.returncode != 0:
            return fail('Speech peer readiness failed')
        stage = 'running'
        return [stt]

    def child_exited(event, launch_context):
        if launch_context.is_shutdown or stage == 'stopped':
            return []
        if event.action in runtime_nodes:
            return fail(f'Speech runtime child exited: {event.process_name}')
        return []

    registrations = [
        RegisterEventHandler(OnProcessExit(target_action=preflight, on_exit=preflight_exited)),
        RegisterEventHandler(OnProcessExit(target_action=peers, on_exit=peers_exited)),
        RegisterEventHandler(OnProcessExit(on_exit=child_exited)),
    ]
    if control:
        return [*registrations, RegisterEventHandler(OnProcessExit(
            target_action=control, on_exit=control_exited)),
            control, watchdog('control', timeouts['peer_timeout_s'])]
    return [*registrations, preflight, watchdog('preflight', timeouts['preflight_timeout_s'])]


def generate_launch_description():
    """Expose robot speech settings without starting navigation or missions."""
    defaults = {
        'stt_model_path': '', 'stt_library_path': '',
        'input_device': '-1', 'output_device': '-1', 'cpp_threads': '6',
        'input_has_aec': 'false', 'agent_provider': 'openai',
        'python_executable': sys.executable, 'preflight_only': 'false',
        'preflight_timeout_s': '120.0', 'peer_timeout_s': '30.0',
        'control_server': 'none',
    }
    choices = {
        'input_has_aec': ['true', 'false'], 'preflight_only': ['true', 'false'],
        'agent_provider': ['openai', 'mock'],
        'control_server': ['none', 'manager', 'autoslam'],
    }
    return LaunchDescription([
        *[DeclareLaunchArgument(name, default_value=default, choices=choices.get(name))
          for name, default in defaults.items()],
        OpaqueFunction(function=_setup),
    ])
