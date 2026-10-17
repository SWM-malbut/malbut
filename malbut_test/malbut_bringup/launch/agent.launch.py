"""agent: the Agent with its fall rules, on whenever fall or speech is on."""

# The Agent holds the fall rules (the former fall coordinator) and the speech
# dialogue. Bringup includes this after the fall and speech modules, so "auto"
# reads whether each of them actually started: the fall rules run only with the
# fall module, and the dialogue only with the speech module (STT and TTS). With
# speech off the Agent offers no confirmation server, so the Manager reports it
# unavailable and the question fails at once, as before.

from pathlib import Path
import shlex

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.launch_support import declarations, file_path, module_actions

ARGUMENTS = [
    'speech_python_executable', 'speech_agent_provider', 'speech_agent_user_id',
    'speech_agent_conversation_db', 'speech_manager_commands', 'speech_prerecorded_audio',
    'fall_manager_runtime_id', 'fall_bridge_runtime_id', 'fall_vlm_runtime_id', 'fall_flow',
]
PARTS = {'agent_speech': 'speech', 'agent_fall_rules': 'fall'}


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    modules = getattr(context.locals, 'malbut_modules', None) or {}
    enabled = {}
    for option, module in PARTS.items():
        choice = value(option)
        enabled[option] = (modules.get(module) == '' if choice == 'auto'
                           else choice == 'true')
    speech, fall_rules = enabled['agent_speech'], enabled['agent_fall_rules']
    if not speech and not fall_rules:
        raise RuntimeError('Agent has nothing to run: neither fall nor speech started')
    arguments = []
    prefix = None
    if speech:
        for name in ('speech_agent_user_id', 'speech_agent_conversation_db'):
            if not value(name).strip():
                raise RuntimeError(f'{name} must be explicitly configured')
        # Check the file, but keep the venv's own path: resolving its symlink
        # would run system Python without the speech packages.
        python = str(Path(value('speech_python_executable')).expanduser())
        file_path(python, 'speech Python')
        prefix = shlex.quote(python)
        arguments += [
            '--provider', value('speech_agent_provider'),
            '--user-id', value('speech_agent_user_id'),
            '--conversation-db', value('speech_agent_conversation_db'),
            '--prerecorded-audio' if value('speech_prerecorded_audio') == 'true'
            else '--no-prerecorded-audio',
        ]
        if value('speech_manager_commands') == 'true':
            arguments.append('--enable-manager-commands')
    else:
        # The fall rules need only ROS, as the coordinator did on system Python.
        arguments.append('--no-speech')
    if fall_rules:
        ids = [value(f'fall_{peer}_runtime_id') for peer in ('manager', 'bridge', 'vlm')]
        if not all(item.strip() for item in ids):
            raise RuntimeError('Fall rules need the fall runtime IDs from Bringup')
        arguments += ['--fall-rules', '--fall-runtime-id', ids[0],
                      '--fall-bridge-runtime-id', ids[1], '--fall-vlm-runtime-id', ids[2]]
        if value('fall_flow') == 'true':
            # The one fall check: the same Bringup switch as the fall monitor's.
            arguments.append('--fall-flow')
    # The fall rules live in this process: if it dies, fall checks would stop
    # until Bringup restarts, so it comes back like STT (Agent owner, 2026-10-17).
    return [Node(
        package='malbut_agent_server', executable='agent_communication',
        prefix=prefix, output='screen', arguments=arguments,
        respawn=True, respawn_delay=5.0,
    )]


def generate_launch_description():
    return LaunchDescription([
        *declarations(ARGUMENTS),
        *[DeclareLaunchArgument(name, default_value='auto', choices=['auto', 'true', 'false'])
          for name in PARTS],
        module_actions('agent', _setup),
    ])
