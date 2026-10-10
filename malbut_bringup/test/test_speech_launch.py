"""Exercise speech launch gates without starting audio, API, or ROS processes."""

import importlib.util
from pathlib import Path
import shlex
import subprocess
import sys

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument, EmitEvent, ExecuteProcess, OpaqueFunction,
    RegisterEventHandler, TimerAction,
)
from launch.events.process import ProcessExited
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters
import pytest


ROOT = Path(__file__).parents[2]


@pytest.fixture
def speech(monkeypatch, tmp_path):
    """Load the real launch module against the checked-in Jetson configuration."""
    spec = importlib.util.spec_from_file_location(
        'speech_launch', ROOT / 'malbut_bringup/launch/speech.launch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'get_package_share_directory',
                        lambda package: str(ROOT / package))
    for name in ('ggml.bin', 'libmalbut_whisper.so'):
        (tmp_path / name).write_bytes(b'fixture; not loaded')
    module.assets = {
        'stt_model_path': str(tmp_path / 'ggml.bin'),
        'stt_library_path': str(tmp_path / 'libmalbut_whisper.so'),
        'python_executable': sys.executable,
    }
    return module


def _context(module, **overrides):
    context = LaunchContext()
    context.launch_configurations.update({
        **module.assets,
        **overrides,
    })
    for action in module.generate_launch_description().entities:
        if isinstance(action, DeclareLaunchArgument):
            action.execute(context)
    return context


def _exit(actions, context, process, returncode=0):
    event = ProcessExited(action=process, name='speech_child', cmd=[],
                          cwd=None, env=None, pid=1, returncode=returncode)
    result = []
    for registration in actions:
        if isinstance(registration, RegisterEventHandler):
            handler = registration.event_handler
            if handler.matches(event):
                result.extend(handler.handle(event, context) or [])
    return result


def _process(actions):
    return next(action for action in actions
                if isinstance(action, ExecuteProcess) and not isinstance(action, Node))


def _timeout(actions, context):
    timer = next(action for action in actions if isinstance(action, TimerAction))
    callback = next(action for action in timer.actions if isinstance(action, OpaqueFunction))
    return callback.execute(context)


@pytest.mark.parametrize('control_server', ['none', 'manager', 'autoslam'])
def test_all_speech_nodes_start_without_external_control_or_peer_gates(speech, control_server):
    context = _context(speech, control_server=control_server)
    actions = speech._setup(context)
    assert [item.node_executable for item in actions if isinstance(item, Node)] == [
        'agent_communication', 'tts_node', 'weather', 'stt', 'key_sync']
    assert not any(isinstance(item, ExecuteProcess) and not isinstance(item, Node)
                   for item in actions)
    assert not any(isinstance(item, TimerAction) for item in actions)


def test_audio_and_identity_settings_reach_the_correct_nodes(speech):
    context = _context(
        speech, input_device='2', output_device='3', cpp_threads='4', input_has_aec='true',
        agent_provider='mock', agent_user_id='trial-user',
        agent_conversation_db='/trial records/session.sqlite3',
        preflight_timeout_s='25')
    actions = speech._setup(context)
    agent, tts, weather, stt, key_sync = [item for item in actions if isinstance(item, Node)]
    assert weather.node_package == 'malbut_agent_server'
    assert key_sync.node_package == 'malbut_agent_server'
    # All settings are captured before the scoped parent restores its own values.
    context.launch_configurations.clear()
    command = [perform_substitutions(context, part) for part in agent.cmd[1:]]
    assert command == [
        '--provider', 'mock', '--user-id', 'trial-user',
        '--conversation-db', '/trial records/session.sqlite3',
        '--prerecorded-audio',
        '--enable-manager-commands', '--ros-args']
    assert evaluate_parameters(context, tts._Node__parameters) == (
        {'backend': 'openai', 'output_device': 3},)
    config, params = evaluate_parameters(context, stt._Node__parameters)
    assert config == ROOT / 'malbut_stt/config/jetson.yaml'
    assert params == {
        'stt_model_path': speech.assets['stt_model_path'],
        'stt_library_path': speech.assets['stt_library_path'],
        'device_index': 2, 'cpp_threads': 4, 'input_has_aec': True,
        'wake_chime_device_index': 3,
    }
    assert shlex.split(perform_substitutions(context, stt.process_description.prefix)) == [
        sys.executable, '-m', 'malbut_bringup.speech_process',
        '--startup-timeout-s', '25.0', '--wait-for-ready', '--', sys.executable]


def test_venv_symlink_and_spaces_are_not_resolved(speech, tmp_path):
    python = tmp_path / 'speech runtime/bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    context = _context(speech, python_executable=str(python))
    nodes = [item for item in speech._setup(context) if isinstance(item, Node)]
    for item in [*nodes[:3], nodes[4]]:
        assert perform_substitutions(context, item.process_description.prefix) == (
            shlex.quote(str(python)))


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_manager_command_opt_in_does_not_gate_launch(speech, enabled):
    context = _context(speech, manager_commands=enabled)
    agent = next(item for item in speech._setup(context)
                 if isinstance(item, Node) and item.node_executable == 'agent_communication')
    command = [perform_substitutions(context, part) for part in agent.cmd[1:]]
    assert ('--enable-manager-commands' in command) is (enabled == 'true')
    assert '--navigation-targets' not in command


@pytest.mark.parametrize('name', [
    'agent_user_id', 'agent_conversation_db', 'stt_model_path', 'stt_library_path',
    'python_executable'])
@pytest.mark.parametrize('blank', ['', ' \t'])
def test_required_settings_fail_before_process_creation(speech, name, blank):
    with pytest.raises(RuntimeError, match=name):
        speech._setup(_context(speech, **{name: blank}))


@pytest.mark.parametrize('name', ['preflight_timeout_s', 'peer_timeout_s'])
@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf'])
def test_timeout_must_be_bounded(speech, name, value):
    with pytest.raises(RuntimeError, match=name):
        speech._setup(_context(speech, **{name: value}))


@pytest.mark.parametrize('name', ['stt_model_path', 'stt_library_path', 'python_executable'])
def test_missing_local_asset_fails_only_this_module_setup(speech, name):
    with pytest.raises(RuntimeError, match='file not found'):
        speech._setup(_context(speech, **{name: '/missing/asset'}))


@pytest.mark.parametrize(
    'child', ['agent_communication', 'tts_node', 'weather', 'stt', 'key_sync'])
@pytest.mark.parametrize('code', [0, 1, -11])
def test_child_exit_is_reported_without_shutdown_or_respawn(speech, child, code):
    from launch.actions import LogInfo
    context = _context(speech)
    actions = speech._setup(context)
    node = next(item for item in actions
                if isinstance(item, Node) and item.node_executable == child)
    result = _exit(actions, context, node, returncode=code)
    assert len(result) == 1 and isinstance(result[0], LogInfo)
    context._set_is_shutdown(True)
    assert _exit(actions, context, node, returncode=code) == []


def test_preflight_only_stays_an_explicit_diagnostic(speech, monkeypatch):
    context = _context(speech, preflight_only='true')
    monkeypatch.setattr(speech, 'get_package_share_directory',
                        lambda _: pytest.fail('runtime nodes should not be constructed'))
    actions = speech._setup(context)
    assert not any(isinstance(item, Node) for item in actions)
    result = _exit(actions, context, _process(actions))
    assert len(result) == 1 and isinstance(result[0], EmitEvent)
    assert _timeout(actions, context) == []


@pytest.mark.parametrize('timed_out', [True, False])
def test_diagnostic_failure_prevents_late_success(speech, timed_out):
    context = _context(speech, preflight_only='true')
    actions = speech._setup(context)
    with pytest.raises(RuntimeError, match='timed out' if timed_out else 'failed'):
        if timed_out:
            _timeout(actions, context)
        else:
            _exit(actions, context, _process(actions), returncode=2)
    assert _exit(actions, context, _process(actions)) == []


def test_shutdown_never_starts_diagnostic_children(speech):
    context = _context(speech, preflight_only='true')
    actions = speech._setup(context)
    context._set_is_shutdown(True)
    assert _exit(actions, context, _process(actions)) == []
    assert _timeout(actions, context) == []


@pytest.mark.parametrize('mode,expected_code', [('failed', 1), ('timeout', 1), ('passed', 0)])
def test_real_launch_exit_status_for_explicit_diagnostic(speech, tmp_path, mode, expected_code):
    wrapper = tmp_path / 'python'
    wrapper.write_text('#!/bin/sh\n' + {
        'failed': 'exit 2\n', 'timeout': 'exec sleep 30\n', 'passed': 'exit 0\n',
    }[mode])
    wrapper.chmod(0o755)
    result = subprocess.run([
        'ros2', 'launch', str(ROOT / 'malbut_bringup/launch/speech.launch.py'),
        f'python_executable:={wrapper}', 'stt_model_path:=unused',
        'stt_library_path:=unused', 'preflight_only:=true', 'preflight_timeout_s:=0.2',
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == expected_code, result.stdout + result.stderr


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_prerecorded_option_routes_catalog_ids_and_custom_audio_directory(speech, enabled):
    context = _context(speech, prerecorded_audio=enabled, audio_directory='/robot/notices')
    actions = speech._setup(context)
    agent, tts, *_ = [action for action in actions if isinstance(action, Node)]
    command = [perform_substitutions(context, part) for part in agent.cmd[1:]]
    assert ('--prerecorded-audio' in command) is (enabled == 'true')
    assert ('--no-prerecorded-audio' in command) is (enabled == 'false')
    parameters = evaluate_parameters(context, tts._Node__parameters)
    assert parameters[0]['audio_directory'] == '/robot/notices'
