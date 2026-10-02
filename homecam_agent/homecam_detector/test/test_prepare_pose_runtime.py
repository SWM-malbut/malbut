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
elif args and args[0] == '-':
    code = sys.stdin.read()
    if "distribution('onnxruntime')" in code:
        sys.exit(0 if os.environ.get('POSE_TEST_LOCAL_CPU') == '1' else 1)
    if "distribution('onnxruntime-gpu')" in code:
        sys.exit(0 if os.environ.get('POSE_TEST_LOCAL_GPU') == '1' else 1)
    if 'import rclpy' in code and (
            os.environ.get('POSE_TEST_BROKEN_IMPORT') == '1' or
            (args[1] and os.environ.get('POSE_TEST_CUDA_MISSING') == '1')):
        sys.exit(1)
elif 'install' in args and os.environ.get('POSE_TEST_PIP_FAILURE') == '1':
    sys.exit(1)
''')
    fake.chmod(0o700)
    script = tmp_path / 'installer.sh'
    tegra = tmp_path / 'nv_tegra_release'
    script.write_text(SCRIPT.read_text().replace('/usr/bin/python3', str(fake))
                      .replace('/etc/nv_tegra_release', str(tegra))
                      .replace('$(uname -m)', '${POSE_TEST_ARCH:-x86_64}'))
    log = tmp_path / 'calls.jsonl'
    env = dict(os.environ, ROS_DISTRO='humble', XDG_CACHE_HOME=str(tmp_path / 'cache'),
               POSE_TEST_LOG=str(log), POSE_TEST_PYTHON=str(fake))
    def run(*args, **extra):
        if extra.pop('tegra', None):
            tegra.write_text('# R36 (release), REVISION: 4.3\n')
        if log.exists():
            log.unlink()
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


def test_cuda_uses_canonical_runtime_and_explicit_local_wheel(installer, tmp_path):
    wheel = tmp_path / 'compatible.whl'
    wheel.touch()
    result, calls = installer('--cuda-wheel', wheel)
    assert result.returncode == 0, result.stderr
    assert any(call[:2] == ['-m', 'venv'] and call[-1].endswith('/runtime') for call in calls)
    assert ['-m', 'pip', '--isolated', 'install', '--no-cache-dir',
            '--force-reinstall', '--no-deps', str(wheel)] in calls
    assert not any('onnxruntime>=1.17,<2' in call for call in calls)
    assert 'Bringup and preflight use this runtime by default' in result.stdout


@pytest.mark.parametrize('flag', ['POSE_TEST_CUDA_MISSING', 'POSE_TEST_PIP_FAILURE',
                                 'POSE_TEST_BROKEN_IMPORT'])
def test_cuda_failure_is_not_reported_as_prepared(installer, tmp_path, flag):
    wheel = tmp_path / 'compatible.whl'
    wheel.touch()
    result, calls = installer('--cuda-wheel', wheel, **{flag: '1'})
    assert result.returncode != 0
    assert 'Fall pose Python:' not in result.stdout
    assert not any('onnxruntime>=1.17,<2' in call for call in calls)


@pytest.mark.parametrize('architecture,package', [
    ('x86_64', 'onnxruntime-gpu==1.23.2'),
    ('aarch64', 'https://github.com/ultralytics/assets/releases/download/v0.0.0/'
     'onnxruntime_gpu-1.23.0-cp310-cp310-linux_aarch64.whl'),
])
def test_gpu_build_installs_only_ort_not_cuda_or_torch(installer, architecture, package):
    result, calls = installer('--gpu', POSE_TEST_ARCH=architecture, tegra=True)
    assert result.returncode == 0, result.stderr
    assert ['-m', 'pip', '--isolated', 'install', '--no-cache-dir',
            '--force-reinstall', '--no-deps', package] in calls
    assert ['-m', 'pip', '--isolated', 'install', '--no-cache-dir',
            'numpy==1.23.5', 'onnxruntime-gpu'] in calls
    packages = [arg for call in calls if 'install' in call for arg in call]
    assert not any('torch' in arg or 'nvidia-' in arg or '[cuda' in arg for arg in packages)


def test_matching_gpu_wheel_and_venv_are_reused(installer):
    result, _ = installer('--gpu')
    assert result.returncode == 0, result.stderr
    result, calls = installer('--gpu', POSE_TEST_LOCAL_GPU='1')
    assert result.returncode == 0, result.stderr
    assert not any(call[:2] == ['-m', 'venv'] for call in calls)
    assert not any('--force-reinstall' in call for call in calls)


@pytest.mark.parametrize('local_cpu', ['0', '1'])
def test_gpu_migrates_only_venv_local_cpu_wheel(installer, local_cpu):
    result, calls = installer('--gpu', POSE_TEST_LOCAL_CPU=local_cpu)
    assert result.returncode == 0, result.stderr
    uninstall = ['-m', 'pip', '--isolated', 'uninstall', '-y', 'onnxruntime']
    assert (uninstall in calls) == (local_cpu == '1')
    assert any('--force-reinstall' in call for call in calls)
    code = SCRIPT.read_text()
    assert "dist.locate_file('')" in code and 'is_relative_to(Path(sys.prefix)' in code


@pytest.mark.parametrize('architecture', ['aarch64', 'armv7l'])
def test_unrecognised_gpu_target_needs_explicit_wheel(installer, architecture):
    result, calls = installer('--gpu', POSE_TEST_ARCH=architecture)
    assert result.returncode != 0 and calls == []
    assert '--cuda-wheel' in result.stderr


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
