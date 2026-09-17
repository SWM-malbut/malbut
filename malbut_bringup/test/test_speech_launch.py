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
def speech(monkeypatch):
    """Load the real launch module against the checked-in Jetson configuration."""
    spec = importlib.util.spec_from_file_location(
        'speech_launch', ROOT / 'malbut_bringup/launch/speech.launch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'get_package_share_directory',
                        lambda package: str(ROOT / package))
    return module


def _context(module, **overrides):
    context = LaunchContext()
    context.launch_configurations.update({
        'stt_model_path': '/models/ggml.bin',
        'stt_library_path': '/native/libmalbut_whisper.so',
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
    callback = next(action for action in timer.actions
                    if isinstance(action, OpaqueFunction))
    return callback.execute(context)


def _assert_shutdown(actions):
    assert len(actions) == 1
    assert isinstance(actions[0], EmitEvent)


def test_startup_waits_for_both_checks_and_preserves_jetson_settings(speech):
    """Only successful preflight and matching peer readiness may start capture."""
    context = _context(speech, input_device='2', output_device='3', cpp_threads='4',
                       input_has_aec='true', agent_provider='mock',
                       python_executable='/runtime with space/bin/python')
    actions = speech._setup(context)
    assert not any(isinstance(action, Node) for action in actions)
    preflight = _process(actions)
    assert [perform_substitutions(context, part) for part in preflight.cmd] == [
        '/runtime with space/bin/python', '-m', 'malbut_bringup.speech_process',
        '--startup-timeout-s', '120.0', '--',
        '/runtime with space/bin/python', '-m', 'malbut_bringup.speech_preflight',
        '--stt-model-path', '/models/ggml.bin', '--stt-library-path',
        '/native/libmalbut_whisper.so', '--input-device', '2', '--output-device', '3',
        '--cpp-threads', '4', '--agent-provider', 'mock',
    ]
    peers = _exit(actions, context, preflight)
    nodes = [action for action in peers if isinstance(action, Node)]
    assert [node.node_executable for node in nodes] == ['agent_communication', 'tts_node']
    agent, tts = nodes
    assert agent.node_package == 'malbut_agent_server'
    assert [perform_substitutions(context, part) for part in agent.cmd[1:3]] == [
        '--provider', 'mock']
    assert evaluate_parameters(context, tts._Node__parameters) == (
        {'backend': 'openai', 'output_device': 3},)
    graph_check = _process(peers)
    assert [perform_substitutions(context, part) for part in graph_check.cmd] == [
        '/runtime with space/bin/python', '-m', 'malbut_bringup.speech_preflight',
        '--wait-for-peers', '--timeout-s', '30.0',
    ]
    result = _exit(actions, context, graph_check)
    assert len(result) == 1
    stt = result[0]
    assert stt.node_executable == 'stt'
    params = evaluate_parameters(context, stt._Node__parameters)
    assert params[0] == ROOT / 'malbut_stt/config/jetson.yaml'
    assert params[1] == {
        'stt_model_path': '/models/ggml.bin',
        'stt_library_path': '/native/libmalbut_whisper.so',
        'device_index': 2, 'cpp_threads': 4, 'input_has_aec': True,
    }
    for node in [agent, tts]:
        assert perform_substitutions(context, node.process_description.prefix) == shlex.quote(
            '/runtime with space/bin/python')
    assert shlex.split(perform_substitutions(context, stt.process_description.prefix)) == [
        '/runtime with space/bin/python', '-m', 'malbut_bringup.speech_process',
        '--startup-timeout-s', '120.0', '--wait-for-ready', '--',
        '/runtime with space/bin/python',
    ]
    assert _timeout(actions, context) == []
    assert _timeout(peers, context) == []


def test_scoped_include_captures_settings_before_parent_scope_restores(speech):
    """Late callbacks must retain speech settings after a scoped include exits."""
    context = _context(speech, agent_provider='mock', input_has_aec='true')
    actions = speech._setup(context)
    # GroupAction pops the child scope before asynchronous process exits arrive.
    context.launch_configurations.clear()
    context.launch_configurations.update({
        'python_executable': '/yolo/bin/python', 'preflight_only': 'true',
        'agent_provider': 'openai', 'input_has_aec': 'false',
    })
    peers = _exit(actions, context, _process(actions))
    agent = next(item for item in peers if isinstance(item, Node)
                 and item.node_package == 'malbut_agent_server')
    assert [perform_substitutions(context, part) for part in agent.cmd[1:3]] == [
        '--provider', 'mock']
    stt = _exit(actions, context, _process(peers))[0]
    assert evaluate_parameters(context, stt._Node__parameters)[1]['input_has_aec'] is True


@pytest.mark.parametrize('server', ['manager', 'autoslam'])
def test_robot_control_must_be_ready_before_audio_preflight(speech, server):
    """Creating the manager process alone cannot start the speech pipeline."""
    context = _context(speech, control_server=server)
    actions = speech._setup(context)
    control = _process(actions)
    assert [perform_substitutions(context, part) for part in control.cmd][-4:] == [
        '--wait-for-control', server, '--timeout-s', '30.0']
    assert not any(isinstance(action, Node) for action in actions)
    preflight = _exit(actions, context, control)
    assert '--stt-model-path' in [
        perform_substitutions(context, part) for part in _process(preflight).cmd]
    assert _timeout(actions, context) == []
    assert not any(isinstance(action, Node) for action in preflight)
    peers = _exit(actions, context, _process(preflight))
    assert [item.node_executable for item in peers if isinstance(item, Node)] == [
        'agent_communication', 'tts_node']


@pytest.mark.parametrize('outcome', ['failed', 'timeout', 'shutdown'])
def test_unready_robot_control_never_starts_speech(speech, outcome):
    """Failure, timeout and shutdown all close admission to the next stage."""
    context = _context(speech, control_server='manager')
    actions = speech._setup(context)
    if outcome == 'shutdown':
        context._set_is_shutdown(True)
    else:
        with pytest.raises(RuntimeError, match='failed' if outcome == 'failed' else 'timed out'):
            if outcome == 'failed':
                _exit(actions, context, _process(actions), returncode=2)
            else:
                _timeout(actions, context)
    assert _exit(actions, context, _process(actions)) == []


@pytest.mark.parametrize('name', ['stt_model_path', 'stt_library_path', 'python_executable'])
def test_required_settings_fail_before_starting_processes(speech, name):
    """Empty runtime paths never silently select a development default."""
    with pytest.raises(RuntimeError, match=name):
        speech._setup(_context(speech, **{name: ''}))


@pytest.mark.parametrize('name', ['preflight_timeout_s', 'peer_timeout_s'])
@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf'])
def test_timeout_must_be_bounded(speech, name, value):
    """Reject settings that leave model loads or peer startup unbounded."""
    with pytest.raises(RuntimeError, match=name):
        speech._setup(_context(speech, **{name: value}))


def test_defaults_select_current_interpreter_and_openai(speech):
    """Use the invoking runtime, and require both checks by default."""
    context = _context(speech)
    assert context.launch_configurations['python_executable'] == sys.executable
    assert context.launch_configurations['agent_provider'] == 'openai'
    assert context.launch_configurations['preflight_only'] == 'false'
    assert context.launch_configurations['preflight_timeout_s'] == '120.0'
    assert context.launch_configurations['peer_timeout_s'] == '30.0'


def test_configured_deadlines_cover_each_startup_process(speech):
    """Bound the entire process lifetime, including imports before its own checks."""
    context = _context(speech, preflight_timeout_s='2.5', peer_timeout_s='1.5')
    actions = speech._setup(context)
    timer = next(action for action in actions if isinstance(action, TimerAction))
    assert timer.period == 2.5
    peers = _exit(actions, context, _process(actions))
    timer = next(action for action in peers if isinstance(action, TimerAction))
    assert timer.period == 1.5
    assert [perform_substitutions(context, part) for part in _process(peers).cmd][-1] == '1.5'


def test_missing_installed_jetson_config_stops_before_starting_peers(speech, monkeypatch):
    """Do not replace a missing robot preset with the node's development defaults."""
    monkeypatch.setattr(speech, 'get_package_share_directory', lambda _: '/missing/package')
    context = _context(speech)
    actions = speech._setup(context)
    with pytest.raises(RuntimeError, match='configuration is missing'):
        _exit(actions, context, _process(actions))


def test_preflight_only_exits_without_constructing_peers(speech, monkeypatch):
    """A passing one-shot check never opens a long-running speech pipeline."""
    context = _context(speech, preflight_only='true')
    monkeypatch.setattr(speech, 'get_package_share_directory',
                        lambda _: pytest.fail('unnecessary runtime lookup'))
    actions = speech._setup(context)
    _assert_shutdown(_exit(actions, context, _process(actions)))
    assert _timeout(actions, context) == []


@pytest.mark.parametrize('phase', ['preflight', 'peers'])
def test_failed_or_timed_out_check_never_starts_next_stage(speech, phase):
    """A deadline also prevents a late successful exit from reopening the gate."""
    for timed_out in (False, True):
        context = _context(speech)
        actions = speech._setup(context)
        stage_actions = actions
        if phase == 'peers':
            stage_actions = _exit(actions, context, _process(actions))
        process = _process(stage_actions)
        with pytest.raises(RuntimeError, match='timed out' if timed_out else 'failed'):
            if timed_out:
                _timeout(stage_actions, context)
            else:
                _exit(actions, context, process, returncode=1)
        assert _exit(actions, context, process) == []


@pytest.mark.parametrize('returncode', [0, 1])
@pytest.mark.parametrize('child', ['agent_communication', 'tts_node', 'stt'])
def test_any_runtime_child_exit_stops_speech(speech, child, returncode):
    """Even a clean child exit must not leave half a speech pipeline running."""
    context = _context(speech)
    actions = speech._setup(context)
    peers = _exit(actions, context, _process(actions))
    graph = _process(peers)
    nodes = [item for item in peers if isinstance(item, Node)]
    if child == 'stt':
        nodes.extend(_exit(actions, context, graph))
    process = next(node for node in nodes if node.node_executable == child)
    with pytest.raises(RuntimeError, match='runtime child exited'):
        _exit(actions, context, process, returncode=returncode)
    assert _exit(actions, context, graph) == []


@pytest.mark.parametrize('phase', ['preflight', 'peers'])
def test_shutdown_never_starts_new_nodes_or_emits_again(speech, phase):
    """Honor launch shutdown while either startup check is still running."""
    context = _context(speech)
    actions = speech._setup(context)
    stage_actions = actions
    if phase == 'peers':
        stage_actions = _exit(actions, context, _process(actions))
    context._set_is_shutdown(True)
    assert _exit(actions, context, _process(stage_actions)) == []
    assert _timeout(stage_actions, context) == []
    for node in [item for item in stage_actions if isinstance(item, Node)]:
        assert _exit(actions, context, node) == []


@pytest.mark.parametrize('mode,expected_code', [('failed', 1), ('timeout', 1), ('passed', 0)])
def test_real_launch_exit_status_and_preflight_only(speech, tmp_path, mode, expected_code):
    """Report failed checks to shell callers and terminate a stalled subprocess."""
    wrapper = tmp_path / 'python'
    wrapper.write_text('#!/bin/sh\n' + {
        'failed': 'exit 2\n', 'timeout': 'exec sleep 30\n', 'passed': 'exit 0\n',
    }[mode])
    wrapper.chmod(0o755)
    result = subprocess.run([
        'ros2', 'launch', str(ROOT / 'malbut_bringup/launch/speech.launch.py'),
        'stt_model_path:=/dummy/model.bin', 'stt_library_path:=/dummy/library.so',
        f'python_executable:={wrapper}', 'preflight_only:=true',
        'preflight_timeout_s:=0.2' if mode == 'timeout' else 'preflight_timeout_s:=5.0',
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == expected_code, result.stdout + result.stderr
    assert 'agent_communication' not in result.stdout + result.stderr
    if mode == 'timeout':
        assert 'Speech preflight timed out' in result.stdout + result.stderr


@pytest.mark.parametrize('control_code', [0, 2])
def test_real_launch_runs_control_check_before_any_audio_check(tmp_path, control_code):
    """Run the launch event loop; a rejected controller never opens audio."""
    events = tmp_path / 'events'
    wrapper = tmp_path / 'python'
    wrapper.write_text(
        '#!/usr/bin/python3\nimport pathlib, sys\n'
        'control = "--wait-for-control" in sys.argv\n'
        f'with pathlib.Path({str(events)!r}).open("a") as stream:\n'
        '    stream.write("control\\n" if control else "preflight\\n")\n'
        f'sys.exit({control_code} if control else 0)\n')
    wrapper.chmod(0o755)
    result = subprocess.run([
        'ros2', 'launch', str(ROOT / 'malbut_bringup/launch/speech.launch.py'),
        'stt_model_path:=/dummy/model.bin', 'stt_library_path:=/dummy/library.so',
        f'python_executable:={wrapper}', 'preflight_only:=true', 'control_server:=manager',
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == (0 if control_code == 0 else 1), result.stdout + result.stderr
    assert events.read_text().splitlines() == (
        ['control', 'preflight'] if control_code == 0 else ['control'])
