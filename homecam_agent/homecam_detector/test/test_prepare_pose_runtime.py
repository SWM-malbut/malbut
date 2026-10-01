"""Run installer control flow with a fake Python; never install packages."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/prepare_fall_pose_runtime.sh'


@pytest.fixture
def installer(tmp_path):
    fake = tmp_path / 'fake_python'
    fake.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['POSE_TEST_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[:2] == ['-m', 'venv']:
    runtime = pathlib.Path(args[-1])
    (runtime / 'bin').mkdir(parents=True, exist_ok=True)
    (runtime / 'pyvenv.cfg').touch()
    target = runtime / 'bin/python'
    if not target.exists():
        target.symlink_to(os.environ['POSE_TEST_PYTHON'])
elif args and args[0] == '-c' and 'importlib.metadata' in args[1]:
    sys.exit(0 if os.environ.get('POSE_TEST_EXISTING') == '1' else 1)
elif args == ['-']:
    sys.stdin.read()
    sys.exit(1 if os.environ.get('POSE_TEST_MIXED') == '1' else 0)
elif args and args[0] == '-':
    sys.stdin.read()
    if os.environ.get('POSE_TEST_BROKEN_IMPORT') == '1':
        sys.exit(1)
    if args[1] and os.environ.get('POSE_TEST_CUDA_MISSING') == '1':
        sys.exit(1)
''')
    fake.chmod(0o700)
    script = tmp_path / 'installer.sh'
    script.write_text(SCRIPT.read_text().replace('/usr/bin/python3', str(fake)))
    log = tmp_path / 'calls.jsonl'
    env = dict(os.environ, ROS_DISTRO='humble', XDG_CACHE_HOME=str(tmp_path / 'cache'),
               POSE_TEST_LOG=str(log), POSE_TEST_PYTHON=str(fake))
    def run(*args, **extra):
        result = subprocess.run(['bash', str(script), *map(str, args)],
                                env=dict(env, **extra), capture_output=True, text=True, timeout=10)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return run


def test_fresh_default_installs_cpu_only_inside_runtime(installer):
    result, calls = installer()
    assert result.returncode == 0, result.stderr
    assert any('onnxruntime>=1.17,<2' in call for call in calls)
    assert any(call[:2] == ['-m', 'venv'] and call[-1].endswith('/runtime') for call in calls)


def test_existing_ort_is_not_overwritten_with_cpu_wheel(installer):
    result, calls = installer(POSE_TEST_EXISTING='1')
    assert result.returncode == 0, result.stderr
    installs = [call for call in calls if call[:2] == ['-m', 'pip']]
    assert installs == [['-m', 'pip', '--isolated', 'install', 'numpy<2']]


def test_broken_gpu_import_does_not_trigger_cpu_install(installer):
    result, calls = installer(POSE_TEST_EXISTING='1', POSE_TEST_BROKEN_IMPORT='1')
    assert result.returncode != 0
    assert not any('onnxruntime>=1.17,<2' in call for call in calls)
    assert 'Fall pose Python:' not in result.stdout


def test_cuda_uses_separate_runtime_and_explicit_local_wheel(installer, tmp_path):
    wheel = tmp_path / 'compatible.whl'
    wheel.touch()
    result, calls = installer('--cuda-wheel', wheel)
    assert result.returncode == 0, result.stderr
    assert any(call[:2] == ['-m', 'venv'] and call[-1].endswith('/runtime-cuda') for call in calls)
    assert ['-m', 'pip', '--isolated', 'install', 'numpy<2', str(wheel)] in calls
    assert not any('onnxruntime>=1.17,<2' in call for call in calls)
    assert 'export MALBUT_FALL_POSE_PYTHON=' in result.stdout


@pytest.mark.parametrize('flag', ['POSE_TEST_CUDA_MISSING', 'POSE_TEST_MIXED'])
def test_cuda_failure_is_not_reported_as_prepared(installer, tmp_path, flag):
    wheel = tmp_path / 'compatible.whl'
    wheel.touch()
    result, calls = installer('--cuda-wheel', wheel, **{flag: '1'})
    assert result.returncode != 0
    assert 'Fall pose Python:' not in result.stdout
    if flag == 'POSE_TEST_MIXED':
        assert not any(call[:2] == ['-m', 'pip'] for call in calls)


@pytest.mark.parametrize('args', [('--cuda-wheel', '/missing.whl'),
                                 ('--cuda-wheel', 'https://example.test/model.whl'),
                                 ('--cuda-wheel',), ('--unknown',)])
def test_bad_installer_arguments_fail_before_any_install(installer, args):
    result, calls = installer(*args)
    assert result.returncode != 0 and calls == []


def test_installer_refuses_non_venv_directory(installer, tmp_path):
    target = tmp_path / 'cache/malbut_fall_pose/runtime'
    target.mkdir(parents=True)
    result, calls = installer()
    assert result.returncode != 0 and 'non-venv' in result.stderr
    assert not any(call[:2] == ['-m', 'venv'] for call in calls)


def test_robot_installer_is_identical():
    assert SCRIPT.read_bytes() == (
        ROOT.parent / 'malbut_test/homecam_agent/scripts/prepare_fall_pose_runtime.sh').read_bytes()
