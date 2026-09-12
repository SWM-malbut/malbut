"""Check file preflight and isolated runtime preparation without installation."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from malbut_bringup.perception_setup import validate_perception_files


def _files(tmp_path):
    """Create readable model placeholders and executable Python placeholders."""
    paths = {}
    for name in ('python_executable', 'reid_python_executable',
                 'model_path', 'reid_model_path'):
        path = tmp_path / name
        path.write_bytes(b'placeholder, never execute')
        path.chmod(0o755 if name.endswith('python_executable') else 0o644)
        paths[name] = path
    return paths


def test_preflight_expands_paths_without_resolving_venv_python(tmp_path):
    """Keep the venv executable path even when its Python is a symlink."""
    files = _files(tmp_path)
    python_link = tmp_path / 'venv-python'
    python_link.symlink_to(files['python_executable'])
    files['python_executable'] = '~/' + os.path.relpath(python_link, Path.home())
    paths = validate_perception_files(**files)
    expanded = Path(paths['python_executable'])
    assert expanded.is_absolute()
    assert expanded.name == 'venv-python'
    assert expanded.samefile(python_link)
    assert paths['model_path'] == str(files['model_path'])


def test_robot_test_does_not_require_an_unused_osnet_model(tmp_path):
    """The box-only tracker can start without the disabled OSNet model file."""
    files = _files(tmp_path)
    files['reid_model_path'] = tmp_path / 'not-installed.onnx'
    paths = validate_perception_files(**files)
    assert paths['reid_python_executable'] == str(files['reid_python_executable'])
    assert 'reid_model_path' not in paths


def test_preflight_reports_all_missing_files_and_real_preparation_commands(tmp_path):
    """One failure explains all missing prerequisites without running commands."""
    paths = {name: tmp_path / name for name in (
        'python_executable', 'reid_python_executable', 'model_path', 'reid_model_path')}
    with pytest.raises(RuntimeError) as raised:
        validate_perception_files(**paths)
    message = str(raised.value)
    for name, path in paths.items():
        if name == 'reid_model_path':
            assert str(path) not in message
            continue
        assert name in message and str(path) in message
    for package, script in (
            ('malbut_yolo', 'prepare_runtime.sh'),
            ('malbut_reid', 'prepare_inference_runtime.sh')):
        assert f'$(ros2 pkg prefix {package})/share/{package}/scripts/{script}' in message


def test_nonexecutable_python_and_directory_model_are_rejected(tmp_path):
    """Existence alone is insufficient for a runtime or a model file."""
    files = _files(tmp_path)
    files['python_executable'].chmod(0o644)
    files['model_path'] = tmp_path
    with pytest.raises(RuntimeError) as raised:
        validate_perception_files(**files)
    message = str(raised.value)
    assert 'Python file is not executable' in message
    assert f'model_path: {tmp_path}' in message


def test_reid_preparation_installs_only_in_runtime_venv(tmp_path):
    """Mock Python and pip so the real shell script cannot install anything."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    calls = tmp_path / 'calls.jsonl'
    python = bin_dir / 'python3'
    python.write_text(
        f'#!{sys.executable}\n'
        'import json, os, pathlib, shutil, sys\n'
        'args = sys.argv[1:]\n'
        'with open(os.environ["PREPARE_CALLS"], "a") as log:\n'
        '    log.write(json.dumps([sys.argv[0], args]) + "\\n")\n'
        'if args[:2] == ["-m", "venv"]:\n'
        '    root = pathlib.Path(args[-1])\n'
        '    (root / "bin").mkdir(parents=True, exist_ok=True)\n'
        '    (root / "pyvenv.cfg").write_text("mock venv")\n'
        '    shutil.copyfile(__file__, root / "bin/python")\n'
        '    (root / "bin/python").chmod(0o755)\n'
        'elif args[:1] == ["-c"] and "version_info" in args[1]:\n'
        '    print("3.10")\n')
    python.chmod(0o755)
    uname = bin_dir / 'uname'
    uname.write_text('#!/bin/sh\nprintf "%s\\n" x86_64\n')
    uname.chmod(0o755)
    runtime = tmp_path / 'reid runtime'
    environment = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
                   'MALBUT_REID_RUNTIME': str(runtime), 'PREPARE_CALLS': str(calls)}
    script = Path(__file__).parents[2] / 'malbut_reid/scripts/prepare_inference_runtime.sh'
    for _ in range(2):
        result = subprocess.run(['bash', str(script)], env=environment,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    installations = [(executable, args) for executable, args in recorded
                     if args[:3] == ['-m', 'pip', 'install']]
    assert len(installations) == 2
    for executable, args in installations:
        assert executable == str(runtime / 'bin/python')
        assert '--user' not in args
    assert sum(args[:2] == ['-m', 'venv'] for _, args in recorded) == 1
    assert not any('uninstall' in args for _, args in recorded)
