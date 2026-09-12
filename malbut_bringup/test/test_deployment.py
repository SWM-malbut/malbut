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
    'malbut_autoslam',
)


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
    colcon = bin_dir / 'colcon'
    colcon.write_text(
        '#!/usr/bin/python3\nimport json, os, sys\n'
        'from pathlib import Path\n'
        'Path(os.environ["BUILD_ARGUMENTS"]).write_text(json.dumps(sys.argv[1:]))\n')
    colcon.chmod(0o755)
    recorded = tmp_path / 'arguments.json'
    env = {**os.environ, 'ROS_DISTRO': 'humble',
           'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
           'BUILD_ARGUMENTS': str(recorded)}
    result = subprocess.run(['bash', str(script), '--parallel-workers', '1'],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    arguments = json.loads(recorded.read_text())
    start = arguments.index('--base-paths') + 1
    end = arguments.index('--build-base')
    assert arguments[start:end] == list(map(str, expected))
    assert arguments[end + 1] == 'build/malbut_test'
    assert arguments[arguments.index('--install-base') + 1] == 'install/malbut_test'
    assert arguments[arguments.index('--packages-up-to') + 1] == 'malbut_bringup'
    assert arguments[-2:] == ['--parallel-workers', '1']
    assert 'local_setup.zsh' in result.stdout


def test_robot_copy_retains_colcon_ignore():
    """A default parent-workspace scan must not find duplicate package names."""
    assert (ROOT / 'COLCON_IGNORE').is_file()


def test_robot_copy_keeps_runtime_without_simulation_evaluator():
    """The copied follower remains runnable without shipping its benchmark."""
    tracking = ROOT / 'malbut_tracking'
    assert not (tracking / 'malbut_tracking/benchmark').exists()
    for name in ('person_follower', 'person_localizer'):
        assert (tracking / 'scripts' / name).is_file()
