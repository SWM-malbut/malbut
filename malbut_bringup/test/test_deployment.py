"""Check the Git-clone build entrypoint without compiling or running a robot."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[2] / 'malbut_test'
PACKAGES = (
    'malbut_bringup', 'malbut_interfaces', 'malbut_system_manager',
    'malbut_yolo', 'malbut_reid', 'malbut_tracking', 'malbut_patrol',
    'malbut_autoslam', 'malbut_depth_costmap',
)


def _cloud_build_fixture(robot, tmp_path, bin_dir):
    scripts = robot / 'homecam_agent/scripts'
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / 'homecam_agent/scripts/build_robot_cloud.sh',
                    scripts / 'build_robot_cloud.sh')
    (scripts / 'build_kvs_webrtc_sdk.sh').write_text(
        '#!/bin/bash\nset -eu\n'
        'printf "%s\\n" "$1" >> "$SDK_CALLS"\n')
    media = robot / 'homecam_agent/homecam_media_agent'
    media.mkdir()
    (media / 'package.xml').write_text('<package/>')
    pkgconfig = tmp_path / 'pkgconfig'
    pkgconfig.mkdir()
    for name in ('gstreamer-1.0', 'gstreamer-app-1.0', 'libcurl', 'openssl'):
        (pkgconfig / f'{name}.pc').write_text(
            f'Name: {name}\nDescription: test build dependency\nVersion: 1.0\n')
    colcon = bin_dir / 'colcon'
    colcon.write_text(
        '#!/usr/bin/python3\nimport json, os, sys\n'
        'with open(os.environ["BUILD_ARGUMENTS"], "a") as log:\n'
        '    log.write(json.dumps(sys.argv[1:]) + "\\n")\n')
    colcon.chmod(0o755)
    return {'PKG_CONFIG_LIBDIR': str(pkgconfig), 'PKG_CONFIG_PATH': '',
            'SDK_CALLS': str(tmp_path / 'sdk-calls')}


@pytest.mark.parametrize('layout', ['malbut/malbut_test', 'malbut'])
def test_build_selects_only_robot_copy_and_separate_output(tmp_path, layout):
    """Leave manufacturer/generated setup and the original packages untouched."""
    workspace = tmp_path / 'robot workspace'
    source = workspace / 'src'
    robot = source / layout
    robot.mkdir(parents=True)
    script = robot / 'build.sh'
    shutil.copyfile(ROOT / 'build.sh', script)
    expected = [robot / name for name in PACKAGES]
    expected += [robot / 'malbut_yolo/vendor/yolo_ros' / name
                 for name in ('yolo_ros', 'yolo_msgs')]
    for directory in expected:
        directory.mkdir(parents=True)
        (directory / 'package.xml').write_text('<package/>')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    cloud_env = _cloud_build_fixture(robot, tmp_path, bin_dir)
    recorded = tmp_path / 'arguments.json'
    env = {**os.environ, **cloud_env, 'ROS_DISTRO': 'humble',
           'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
           'BUILD_ARGUMENTS': str(recorded)}
    result = subprocess.run(['bash', str(script), '--parallel-workers', '1'],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    cloud_arguments, arguments = [json.loads(line)
                                  for line in recorded.read_text().splitlines()]
    assert cloud_arguments[cloud_arguments.index('--packages-select') + 1] == \
        'homecam_media_agent'
    assert '-DHOMECAM_ENABLE_KVS=ON' in cloud_arguments
    assert '-DHOMECAM_ENABLE_GSTREAMER=ON' in cloud_arguments
    assert cloud_arguments[cloud_arguments.index('--install-base') + 1] == \
        'install/malbut_test'
    assert Path(env['SDK_CALLS']).read_text().strip() == str(
        workspace / '.deps/amazon-kinesis-video-streams-webrtc-sdk-c-v1.19.1')
    start = arguments.index('--base-paths') + 1
    end = arguments.index('--build-base')
    assert arguments[start:end] == list(map(str, expected))
    assert arguments[end + 1] == 'build/malbut_test'
    assert arguments[arguments.index('--install-base') + 1] == 'install/malbut_test'
    assert arguments[arguments.index('--packages-up-to') + 1] == 'malbut_bringup'
    assert arguments[-2:] == ['--parallel-workers', '1']
    assert 'local_setup.zsh' in result.stdout


def test_missing_media_dependencies_stop_before_sdk_or_ros_build(tmp_path):
    robot = tmp_path / 'workspace/src/malbut'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    cloud_env = _cloud_build_fixture(robot, tmp_path, bin_dir)
    empty_pkgconfig = tmp_path / 'empty-pkgconfig'
    empty_pkgconfig.mkdir()
    recorded = tmp_path / 'build-arguments'
    env = {**os.environ, **cloud_env, 'ROS_DISTRO': 'humble',
           'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
           'PKG_CONFIG_LIBDIR': str(empty_pkgconfig),
           'BUILD_ARGUMENTS': str(recorded)}
    result = subprocess.run(
        ['bash', str(robot / 'homecam_agent/scripts/build_robot_cloud.sh')],
        env=env, text=True, capture_output=True)
    assert result.returncode == 1
    assert 'libgstreamer1.0-dev' in result.stderr
    assert not recorded.exists()
    assert not Path(env['SDK_CALLS']).exists()


def test_sdk_is_downloaded_once_and_reuses_incremental_build(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    git = bin_dir / 'git'
    git.write_text(
        '#!/usr/bin/python3\nimport json, os, sys\nfrom pathlib import Path\n'
        'with open(os.environ["SDK_COMMANDS"], "a") as log:\n'
        '    log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'if sys.argv[1] == "clone":\n'
        '    root = Path(sys.argv[-1])\n'
        '    (root / ".git").mkdir(parents=True)\n'
        '    (root / "certs").mkdir()\n'
        '    (root / "certs/cert.pem").write_text("test")\n'
        'elif "rev-parse" in sys.argv:\n'
        '    print("d7322f63af3c600ee7031b28436e3f8a12664272")\n')
    git.chmod(0o755)
    cmake = bin_dir / 'cmake'
    cmake.write_text(
        '#!/usr/bin/python3\nimport json, os, sys\n'
        'with open(os.environ["CMAKE_COMMANDS"], "a") as log:\n'
        '    log.write(json.dumps([os.environ.get("CMAKE_POLICY_VERSION_MINIMUM"),'
        ' sys.argv[1:]]) + "\\n")\n')
    cmake.chmod(0o755)
    sdk = tmp_path / 'sdk path'
    env = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
           'SDK_COMMANDS': str(tmp_path / 'git-commands'),
           'CMAKE_COMMANDS': str(tmp_path / 'cmake-commands')}
    for _ in range(2):
        result = subprocess.run(
            ['bash', str(ROOT / 'homecam_agent/scripts/build_kvs_webrtc_sdk.sh'),
             str(sdk)], env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
    git_calls = [json.loads(line) for line in Path(env['SDK_COMMANDS']).read_text().splitlines()]
    assert sum(call[0] == 'clone' for call in git_calls) == 1
    cmake_calls = [json.loads(line)
                   for line in Path(env['CMAKE_COMMANDS']).read_text().splitlines()]
    assert len(cmake_calls) == 4
    assert all(policy == '3.5' for policy, _ in cmake_calls)
    assert cmake_calls[1][1] == ['--build', str(sdk / 'build'), '--parallel']
    assert cmake_calls[3] == cmake_calls[1]


def test_robot_copy_retains_colcon_ignore():
    """A default parent-workspace scan must not find duplicate package names."""
    assert (ROOT / 'COLCON_IGNORE').is_file()


def test_robot_copy_keeps_runtime_without_simulation_evaluator():
    """The copied follower remains runnable without shipping its benchmark."""
    tracking = ROOT / 'malbut_tracking'
    assert not (tracking / 'malbut_tracking/benchmark').exists()
    for name in ('person_follower', 'person_localizer'):
        assert (tracking / 'scripts' / name).is_file()
