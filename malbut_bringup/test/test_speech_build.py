"""Exercise the robot build entrypoint without pip, CUDA, or a ROS build."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from test_deployment import PACKAGES, ROOT


@pytest.fixture
def robot_build(tmp_path):
    """Mock external tools while executing the script's actual Python guards."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    command = bin_dir / 'mock-command'
    command.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, shutil, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'mock-command':
    name, args = args[0], args[1:]
with open(os.environ['BUILD_EVENTS'], 'a') as stream:
    stream.write(json.dumps({'name': name, 'args': args,
                             'path': os.environ['PATH']}) + '\\n')
stage = name
if name == 'cmake':
    stage = 'native-build' if '--build' in args else 'configure'
elif name == 'python' and 'pip' in args:
    stage = 'pip'
if os.environ.get('FAIL_STAGE') == stage:
    sys.exit(19)
if name == 'system-python' and args[:2] == ['-m', 'venv']:
    runtime = pathlib.Path(args[-1])
    (runtime / 'bin').mkdir(parents=True)
    shutil.copyfile(os.environ['MOCK_COMMAND'], runtime / 'bin/python')
    (runtime / 'bin/python').chmod(0o755)
    (runtime / 'pyvenv.cfg').write_text('include-system-site-packages = true\\n')
elif name in ('system-python', 'python') and args[0] == '-c':
    sys.version_info = tuple(map(int, os.environ.get('MOCK_PYTHON_VERSION', '3.10').split('.')))
    if name == 'python':
        sys.prefix = str(pathlib.Path(sys.argv[0]).parents[1])
        sys._base_executable = '/usr/bin/python3'
        if os.environ.get('INVALID_VENV') == '1':
            sys.prefix = sys.base_prefix
    sys.argv = ['-c', *args[2:]]
    exec(args[1])
''')
    command.chmod(0o755)
    for name in ('cmake', 'git', 'nvcc', 'colcon'):
        (bin_dir / name).symlink_to(command)
    # BASH_ENV shadows the fixed system interpreter only in this test shell.
    bash_env = tmp_path / 'mock-system-python.sh'
    bash_env.write_text(
        'function /usr/bin/python3() { "$MOCK_COMMAND" system-python "$@"; }\n')
    cache = tmp_path / 'cache with spaces'
    whisper = cache / 'malbut_speech/whisper.cpp'
    (whisper / 'include').mkdir(parents=True)
    (whisper / 'include/whisper.h').touch()
    events = tmp_path / 'events.jsonl'
    env = {**os.environ, 'ROS_DISTRO': 'humble',
           'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
           'BASH_ENV': str(bash_env), 'MOCK_COMMAND': str(command),
           'BUILD_EVENTS': str(events), 'XDG_CACHE_HOME': str(cache)}
    for variable in ('MALBUT_BUILD_SPEECH', 'MALBUT_SPEECH_RUNTIME',
                     'MALBUT_STT_BUILD_DIR', 'WHISPER_CPP_SOURCE_DIR'):
        env.pop(variable, None)

    def run(layout='malbut', overrides=None, missing_tool=None):
        if missing_tool:
            (bin_dir / missing_tool).unlink()
        robot = tmp_path / 'robot workspace/src' / layout
        robot.mkdir(parents=True)
        for name in (*PACKAGES, 'malbut_yolo/vendor/yolo_ros/yolo_ros',
                     'malbut_yolo/vendor/yolo_ros/yolo_msgs'):
            directory = robot / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'package.xml').write_text('<package/>')
        # Cloud behavior is covered separately by test_deployment.py.
        cloud_script = robot / 'homecam_agent/scripts/build_robot_cloud.sh'
        cloud_script.parent.mkdir(parents=True)
        cloud_script.write_text('#!/bin/bash\nexit 0\n')
        script = robot / 'build.sh'
        shutil.copyfile(ROOT / 'build.sh', script)
        result = subprocess.run(['bash', str(script)], text=True,
                                capture_output=True, env={**env, **(overrides or {})})
        calls = [json.loads(line) for line in events.read_text().splitlines()] \
            if events.exists() else []
        return result, calls, robot, cache / 'malbut_speech'

    return run


@pytest.mark.parametrize('layout', ['malbut', 'malbut/malbut_test'])
def test_default_build_prepares_isolated_speech_before_colcon(robot_build, layout):
    result, calls, robot, cache = robot_build(layout)
    assert result.returncode == 0, result.stderr
    configure, native = [call['args'] for call in calls if call['name'] == 'cmake']
    assert configure == [
        '-S', str(robot / 'malbut_stt/native'), '-B', str(cache / 'whisper-cpp-build'),
        '-DWHISPER_CPP_SOURCE_DIR=' + str(cache / 'whisper.cpp'),
        '-DGGML_CUDA=ON', '-DGGML_METAL=OFF', '-DCMAKE_CUDA_ARCHITECTURES=87',
        '-DCMAKE_BUILD_TYPE=Release',
    ]
    assert native == ['--build', str(cache / 'whisper-cpp-build'),
                      '--target', 'malbut_whisper', '--parallel', '2']
    creation = [call['args'] for call in calls
                if call['name'] == 'system-python' and '-m' in call['args']]
    assert creation == [['-m', 'venv', '--system-site-packages', str(cache / 'runtime')]]
    install = [call['args'] for call in calls
               if call['name'] == 'python' and 'pip' in call['args']]
    assert install == [[
        '-m', 'pip', '--isolated', 'install',
        '-r', str(robot / 'malbut_stt/requirements-whisper-cpp.txt'),
        '-r', str(robot / 'malbut_tts/requirements-api.txt'),
    ]]
    assert calls[-1]['name'] == 'colcon'
    assert calls[-1]['path'] == '/usr/bin:/bin'
    assert all('pip' not in call['args'] for call in calls if call['name'] != 'python')
    assert not (robot / '.venv').exists()


@pytest.mark.parametrize('layout', ['malbut', 'malbut/malbut_test'])
@pytest.mark.parametrize('failure', ['configure', 'native-build', 'pip'])
def test_speech_failure_stops_colcon(robot_build, layout, failure):
    result, calls, _, _ = robot_build(layout, {'FAIL_STAGE': failure})
    assert result.returncode == 19
    assert all(call['name'] != 'colcon' for call in calls)
    if failure != 'pip':
        assert all('pip' not in call['args'] for call in calls)


@pytest.mark.parametrize('overrides, message', [
    ({'MALBUT_BUILD_SPEECH': 'yes'}, 'MALBUT_BUILD_SPEECH must be'),
    ({'MOCK_PYTHON_VERSION': '3.9'}, 'Python 3.10'),
    ({'INVALID_VENV': '1'}, 'dedicated virtualenv'),
])
def test_invalid_speech_environment_fails_before_pip(robot_build, overrides, message):
    result, calls, _, _ = robot_build(overrides=overrides)
    assert result.returncode != 0
    assert message in result.stderr
    assert all(call['name'] != 'colcon' and 'pip' not in call['args'] for call in calls)


def test_missing_source_does_not_download_or_install(robot_build, tmp_path):
    result, calls, _, _ = robot_build(overrides={
        'WHISPER_CPP_SOURCE_DIR': str(tmp_path / 'missing checkout')})
    assert result.returncode != 0
    assert 'no automatic download' in result.stderr
    assert all(call['name'] == 'system-python' for call in calls)


def test_missing_cuda_compiler_stops_before_install(robot_build):
    result, calls, _, _ = robot_build(missing_tool='nvcc')
    assert result.returncode != 0
    assert 'Missing nvcc' in result.stderr
    assert all(call['name'] == 'system-python' for call in calls)


def test_explicit_external_paths_reach_build_and_runtime(robot_build, tmp_path):
    source = tmp_path / 'custom source'
    (source / 'include').mkdir(parents=True)
    (source / 'include/whisper.h').touch()
    native = tmp_path / 'custom build'
    runtime = tmp_path / 'custom runtime'
    result, calls, _, _ = robot_build(overrides={
        'WHISPER_CPP_SOURCE_DIR': str(source),
        'MALBUT_STT_BUILD_DIR': str(native),
        'MALBUT_SPEECH_RUNTIME': str(runtime),
    })
    assert result.returncode == 0, result.stderr
    configure = next(call['args'] for call in calls if call['name'] == 'cmake')
    assert configure[configure.index('-B') + 1] == str(native)
    assert '-DWHISPER_CPP_SOURCE_DIR=' + str(source) in configure
    assert (runtime / 'bin/python').exists()


def test_native_build_cannot_dirty_source_checkout(robot_build, tmp_path):
    source = tmp_path / 'cache with spaces/malbut_speech/whisper.cpp'
    result, calls, _, _ = robot_build(overrides={
        'MALBUT_STT_BUILD_DIR': str(source / 'build')})
    assert result.returncode != 0
    assert 'separate from the whisper.cpp checkout' in result.stderr
    assert all(call['name'] == 'system-python' for call in calls)
    assert not (source / 'build').exists()


def test_explicit_skip_does_not_touch_speech_environment(robot_build):
    result, calls, _, cache = robot_build(overrides={'MALBUT_BUILD_SPEECH': '0'})
    assert result.returncode == 0, result.stderr
    assert [call['name'] for call in calls] == ['colcon']
    assert not (cache / 'runtime').exists()
    assert not (cache / 'whisper-cpp-build').exists()
