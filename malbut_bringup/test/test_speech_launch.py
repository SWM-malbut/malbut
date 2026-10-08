"""Exercise speech launch and recovery without starting audio or API clients."""

import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from launch import LaunchContext, LaunchDescription, LaunchService
from launch.actions import (
    DeclareLaunchArgument, EmitEvent, ExecuteProcess, OpaqueFunction,
    RegisterEventHandler, TimerAction,
)
from launch.events.process import ProcessExited
from launch.event_handlers import OnProcessExit, OnProcessIO, OnProcessStart
from launch.events import Shutdown
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
from launch_ros.substitutions import ExecutableInPackage
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


@pytest.mark.parametrize('aec', [None, 'true', 'false'])
def test_audio_and_identity_settings_reach_the_correct_nodes(speech, aec):
    context = _context(
        speech, input_device='2', output_device='3', cpp_threads='4',
        agent_provider='mock', agent_user_id='trial-user',
        agent_conversation_db='/trial records/session.sqlite3',
        navigation_targets='/maps/targets.yaml', preflight_timeout_s='25',
        **({} if aec is None else {'input_has_aec': aec}))
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
        '--enable-manager-commands', '--navigation-targets', '/maps/targets.yaml', '--ros-args']
    assert evaluate_parameters(context, tts._Node__parameters) == (
        {'backend': 'openai', 'output_device': 3},)
    config, params = evaluate_parameters(context, stt._Node__parameters)
    assert config == ROOT / 'malbut_stt/config/jetson.yaml'
    assert params == {
        'stt_model_path': speech.assets['stt_model_path'],
        'stt_library_path': speech.assets['stt_library_path'],
        'device_index': 2, 'cpp_threads': 4, 'input_has_aec': aec == 'true',
        'wake_chime_device_index': 3,
    }
    assert shlex.split(perform_substitutions(context, stt.process_description.prefix)) == [
        sys.executable, '-m', 'malbut_bringup.speech_process',
        '--startup-timeout-s', '25.0', '--wait-for-ready',
        '--heartbeat-timeout-s', '10.0', '--', sys.executable]


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
    context = _context(speech, manager_commands=enabled, navigation_targets='/targets.yaml')
    agent = next(item for item in speech._setup(context)
                 if isinstance(item, Node) and item.node_executable == 'agent_communication')
    command = [perform_substitutions(context, part) for part in agent.cmd[1:]]
    assert ('--enable-manager-commands' in command) is (enabled == 'true')
    assert ('--navigation-targets' in command) is (enabled == 'true')


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
def test_child_exit_is_reported_without_shutting_down_peers(speech, child, code):
    from launch.actions import LogInfo
    context = _context(speech)
    actions = speech._setup(context)
    node = next(item for item in actions
                if isinstance(item, Node) and item.node_executable == child)
    result = _exit(actions, context, node, returncode=code)
    assert len(result) == 1 and isinstance(result[0], LogInfo)
    context._set_is_shutdown(True)
    assert _exit(actions, context, node, returncode=code) == []


def test_only_stt_restarts_with_a_bounded_delay(speech):
    nodes = [item for item in speech._setup(_context(speech)) if isinstance(item, Node)]
    for node in nodes:
        assert node._ExecuteLocal__respawn is (node.node_executable == 'stt')
        if node.node_executable == 'stt':
            assert node._ExecuteLocal__respawn_delay == 5.0


@pytest.mark.parametrize('phase', [
    'startup', 'runtime_clean', 'runtime_failed', 'shutdown_running', 'shutdown_backoff'])
def test_real_stt_recovery_preserves_peers_and_honors_shutdown(
        speech, monkeypatch, tmp_path, phase):
    """Run the actual nodes/prefix; replace only their hardware-dependent executables."""
    attempts = tmp_path / 'attempts'
    child = tmp_path / 'child.py'
    child.write_text(f'''
import os, pathlib, sys, time
if pathlib.Path(sys.argv[0]).name != 'stt':
    time.sleep(30)
    raise SystemExit(0)
attempts = pathlib.Path({str(attempts)!r})
with attempts.open('a') as stream:
    stream.write(str(os.getpid()) + '\\n')
attempt = len(attempts.read_text().splitlines())
if attempt > 1:
    try:
        os.kill(int(attempts.read_text().splitlines()[-2]), 0)
    except ProcessLookupError:
        pass
    else:
        raise RuntimeError('previous STT is still alive')
if attempt == 1 and {phase!r} in ('startup', 'shutdown_backoff'):
    raise SystemExit(2)
print('malbut_speech_capture_ready', flush=True)
if attempt == 1 and {phase!r}.startswith('runtime_'):
    raise SystemExit(0 if {phase!r} == 'runtime_clean' else 2)
print(f'fixture_ready attempt={{attempt}}', flush=True)
time.sleep(30)
''')
    for name in ('agent_communication', 'tts_node', 'weather', 'stt', 'key_sync'):
        (tmp_path / name).symlink_to(child)
    monkeypatch.setattr(ExecutableInPackage, 'perform', lambda self, context: str(
        tmp_path / perform_substitutions(context, self.executable)))
    monkeypatch.setenv('ROS_LOG_DIR', str(tmp_path / 'ros-log'))
    monkeypatch.setenv('PYTHONPATH', str(ROOT / 'malbut_bringup') + os.pathsep
                       + os.environ.get('PYTHONPATH', ''))
    actions = speech._setup(_context(speech, preflight_timeout_s='2.0'))
    nodes = [item for item in actions if isinstance(item, Node)]
    stt = next(item for item in nodes if item.node_executable == 'stt')
    # The production delay is asserted separately; keep the real-process test fast.
    stt._ExecuteLocal__respawn_delay = 0.2
    starts = {node: [] for node in nodes}
    stt_exits = []
    premature_peer_exits = []
    output = bytearray()
    completed = []

    def started(event, context):
        starts[event.action].append((event.pid, time.monotonic()))

    def stop():
        completed.append(True)
        return [EmitEvent(event=Shutdown(reason='recovery test complete'))]

    def exited(event, context):
        if context.is_shutdown:
            return []
        if event.action is stt:
            stt_exits.append(time.monotonic())
            if phase == 'shutdown_backoff':
                return stop()
        else:
            premature_peer_exits.append(event.action)
        return []

    def received(event):
        output.extend(event.text)
        expected = b'1' if phase == 'shutdown_running' else b'2'
        if b'fixture_ready attempt=' + expected + b'\n' in output and not completed:
            return stop()
        return []

    service = LaunchService()
    service.include_launch_description(LaunchDescription([
        RegisterEventHandler(OnProcessStart(on_start=started)),
        RegisterEventHandler(OnProcessExit(on_exit=exited)),
        RegisterEventHandler(OnProcessIO(target_action=stt, on_stdout=received)),
        *actions,
        TimerAction(period=8.0, actions=[EmitEvent(event=Shutdown(reason='test timeout'))]),
    ]))
    assert service.run() == 0
    assert completed, 'STT never reached the expected recovery/shutdown event'
    assert not premature_peer_exits
    assert all(len(starts[node]) == 1 for node in nodes if node is not stt)
    expected_attempts = 1 if phase.startswith('shutdown_') else 2
    assert len(starts[stt]) == expected_attempts
    child_pids = [int(pid) for pid in attempts.read_text().splitlines()]
    assert len(child_pids) == expected_attempts
    assert len(set(child_pids)) == expected_attempts
    if expected_attempts == 2:
        assert starts[stt][1][1] - stt_exits[0] >= 0.18
    for pid in child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


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


def test_resident_speech_keeps_jetson_parameters_in_owned_namespace(speech):
    """A bare YAML node key must not discard preset values after namespacing."""
    import yaml
    namespace = '/malbut/resident_voice_test'
    context = _context(speech, node_namespace=namespace, device_operations='true')
    actions = speech._setup(context)
    agent = next(action for action in actions
                 if isinstance(action, Node) and action.node_executable == 'agent_communication')
    assert '--enable-device-operations' in [
        perform_substitutions(context, part) for part in agent.cmd[1:]]
    stt = next(action for action in actions
               if isinstance(action, Node) and action.node_executable == 'stt')
    values = evaluate_parameters(context, stt._Node__parameters)
    expected = yaml.safe_load((ROOT / 'malbut_stt/config/jetson.yaml').read_text())[
        'malbut_stt']['ros__parameters']
    assert values[0] == expected
