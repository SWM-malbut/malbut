"""Compose independent robot modules; no feature waits for another feature."""

from pathlib import Path
from uuid import uuid4

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, LogInfo, OpaqueFunction, SetEnvironmentVariable
from malbut_resource_monitor.launch_support import record_first
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from malbut_bringup.launch_support import defaults, declarations, include, package_file
from malbut_bringup.speech_audio import shared_xfm_source


def _setup(context):
    def value(name):
        return LaunchConfiguration(name).perform(context)

    context.extend_globals({'malbut_modular_bringup': True, 'malbut_modules': {}})
    options = {name: value(name) for name in defaults()}
    enabled = ['robot']
    for name, option in (('tracking', 'perception'), ('patrol', 'patrol'),
                         ('autoslam', 'autoslam'), ('manual', 'manual'),
                         ('relocalization', 'relocalization')):
        if value(option) == 'true':
            enabled.append(name)
    media = value('homecam') == 'true' or (
        value('homecam') == 'auto' and bool(context.environment.get('HOMECAM_BACKEND_URL', '')))
    if media:
        enabled.append('homecam')
    if value('fall_monitor') != 'false' and (
            value('fall_monitor') == 'true' or Path(value('fall_config')).expanduser().is_file()):
        enabled.append('fall')
        for peer in ('manager', 'bridge', 'vlm'):
            key = 'fall_' + peer + '_runtime_id'
            options[key] = options[key] or str(uuid4())
    if value('speech') == 'true':
        enabled.append('speech')
    # Shared microphone setup is local wiring, not a ROS readiness prerequisite.
    # A failure affects only the two microphone consumers, never navigation.
    actions = [SetEnvironmentVariable('need_compile', 'False')]
    audio_env = {}
    if media and 'speech' in enabled and value('speech_input_device') == '0':
        try:
            source = shared_xfm_source(dict(context.environment))
            audio_env = {'MALBUT_SHARED_MICROPHONE': source, 'PULSE_SOURCE': source}
        except RuntimeError as error:
            for name in ('homecam', 'speech'):
                enabled.remove(name)
                context.locals.malbut_modules[name] = str(error)
            actions.append(LogInfo(msg=str(error)))
    for name in enabled:
        settings = dict(options)
        if name == 'robot' and value('relocalization') != 'true':
            settings['restore_pose'] = 'false'
        if name == 'speech':
            settings = {
                'python_executable': value('speech_python_executable'),
                'stt_model_path': value('stt_model_path'),
                'stt_library_path': value('stt_library_path'),
                'input_device': value('speech_input_device'),
                'output_device': value('speech_output_device'),
                'cpp_threads': value('stt_cpp_threads'),
                'input_has_aec': value('speech_input_has_aec'),
                'agent_provider': value('speech_agent_provider'),
                'agent_user_id': value('speech_agent_user_id'),
                'agent_conversation_db': value('speech_agent_conversation_db'),
                'manager_commands': value('speech_manager_commands'),
                'navigation_targets': value('speech_navigation_targets'),
                'preflight_timeout_s': value('speech_preflight_timeout_s'),
                'peer_timeout_s': value('speech_peer_timeout_s'),
                'preflight_only': 'false',
            }
        actions.append(include(
            package_file('malbut_bringup', f'launch/{name}.launch.py'), settings,
            environment=audio_env if name in ('homecam', 'speech') else None))
    if value('web_panel') == 'true':
        actions.append(Node(
            package='malbut_bringup', executable='robot_web_panel',
            name='robot_web_panel', output='screen', parameters=[{
                'use_sim_time': False, 'manage_bringup': False,
                'map_directory': value('map_directory'),
                'map_topic': value('patrol_costmap_topic'),
                'robot_frame': value('robot_frame'), 'rgb_topic': value('rgb_topic'),
            }]))
    startup = actions
    if value('resource_monitor') == 'true':
        return record_first(startup, value('resource_log_root'))
    return startup


def generate_launch_description():
    return LaunchDescription([
        *declarations(defaults()),
        DeclareLaunchArgument('resource_monitor', default_value='true', choices=['true', 'false']),
        DeclareLaunchArgument('resource_log_root',
                              default_value=str(Path.home() / '.ros/malbut/resource_logs')),
        OpaqueFunction(function=_setup),
    ])
