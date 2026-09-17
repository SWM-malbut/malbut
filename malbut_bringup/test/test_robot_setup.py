"""Verify setup acquisition and preservation without OS installs or downloads."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

from test_deployment import PACKAGES, ROOT


REVISION = 'da54572229bcf64ba367d96c7ef15770376c4280'
MODEL = b'tiny verified speech model fixture\n'
MODEL_SHA256 = '1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b'


def _checkout(path, revision=REVISION, dirty=False):
    (path / '.git').mkdir(parents=True)
    (path / '.git/revision').write_text(revision)
    (path / 'include').mkdir()
    (path / 'include/whisper.h').write_text('fixture header\n')
    if dirty:
        (path / '.git/dirty').write_text(' M include/whisper.h\n')


@pytest.fixture
def robot_setup(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    command = bin_dir / 'mock-command'
    command.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'mock-command':
    name, args = args[0], args[1:]
with open(os.environ['SETUP_EVENTS'], 'a') as stream:
    stream.write(json.dumps({'name': name, 'args': args}) + '\\n')
if name == 'system-python' and args[0] == '-c':
    sys.version_info = (3, 10)
    sys.argv = ['-c', *args[2:]]
    exec(args[1])
elif name == 'git':
    if args[0] == 'clone':
        root = pathlib.Path(args[-1])
        (root / '.git').mkdir(parents=True)
        (root / '.git/revision').write_text('unselected')
        if os.environ.get('FAIL_STAGE') == 'clone':
            sys.exit(19)
        (root / 'include').mkdir()
        (root / 'include/whisper.h').write_text('fixture header\\n')
    elif args[0] == '-C':
        root = pathlib.Path(args[1])
        if args[2] == 'checkout':
            (root / '.git/revision').write_text(args[-1])
        elif args[2] == 'rev-parse':
            print((root / '.git/revision').read_text())
        elif args[2] == 'status':
            dirty = root / '.git/dirty'
            if dirty.exists():
                print(dirty.read_text())
elif name == 'curl':
    output = pathlib.Path(args[args.index('--output') + 1])
    content = pathlib.Path(os.environ['MODEL_FIXTURE']).read_bytes()
    if os.environ.get('FAIL_STAGE') in ('download', 'checksum'):
        content = b'incomplete or corrupt download'
    output.write_bytes(content)
    if os.environ.get('FAIL_STAGE') == 'download':
        sys.exit(19)
''')
    command.chmod(0o755)
    for name in ('sudo', 'apt-get', 'rosdep', 'git', 'curl', 'nvcc', 'cmake'):
        (bin_dir / name).symlink_to(command)
    bash_env = tmp_path / 'mock-system-python.sh'
    bash_env.write_text(
        'function /usr/bin/python3() { "$MOCK_COMMAND" system-python "$@"; }\n')
    robot = tmp_path / 'robot workspace/src/malbut'
    for name in (*PACKAGES, 'malbut_yolo/vendor/yolo_ros/yolo_ros',
                 'malbut_yolo/vendor/yolo_ros/yolo_msgs',
                 'homecam_agent/homecam_media_agent', 'homecam_agent/homecam_detector'):
        directory = robot / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'package.xml').write_text('<package/>')
    dependencies = robot / 'homecam_agent/scripts/install_dependencies.sh'
    dependencies.parent.mkdir(parents=True)
    dependencies.write_text('#!/bin/bash\n"$MOCK_COMMAND" homecam-dependencies\n')
    fixture = tmp_path / 'tiny-model.bin'
    fixture.write_bytes(MODEL)
    cache = tmp_path / 'cache with spaces/malbut_speech'
    source = cache / 'whisper.cpp'
    model = cache / 'models/ggml-small.bin'
    events = tmp_path / 'events.jsonl'
    env = {**os.environ, 'ROS_DISTRO': 'humble',
           'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
           'BASH_ENV': str(bash_env), 'MOCK_COMMAND': str(command),
           'SETUP_EVENTS': str(events), 'XDG_CACHE_HOME': str(cache.parent),
           'MODEL_FIXTURE': str(fixture)}
    for variable in ('WHISPER_CPP_SOURCE_DIR', 'MALBUT_STT_MODEL_PATH'):
        env.pop(variable, None)

    def run(overrides=None):
        script = robot / 'setup.sh'
        # Exercise the real checksum guard with a small, deterministic payload.
        script.write_text((ROOT / 'setup.sh').read_text().replace(
            MODEL_SHA256, hashlib.sha256(MODEL).hexdigest()))
        events.write_text('')
        result = subprocess.run(['bash', str(script)], text=True,
                                capture_output=True, env={**env, **(overrides or {})})
        calls = [json.loads(line) for line in events.read_text().splitlines()]
        return result, calls

    return run, source, model, robot


def test_setup_acquires_assets_once_and_includes_local_homecam_packages(robot_setup):
    run, source, model, robot = robot_setup
    first, calls = run()
    assert first.returncode == 0, first.stderr
    assert (source / '.git/revision').read_text() == REVISION
    assert model.read_bytes() == MODEL
    assert len([call for call in calls if call['name'] == 'curl']) == 1
    assert len([call for call in calls
                if call['name'] == 'git' and call['args'][0] == 'clone']) == 1
    assert any(call['name'] == 'homecam-dependencies' for call in calls)
    rosdep = next(call['args'] for call in calls
                  if call['name'] == 'rosdep' and 'install' in call['args'])
    assert str(robot / 'homecam_agent') in rosdep
    second, reused = run()
    assert second.returncode == 0, second.stderr
    assert all(call['name'] != 'curl' for call in reused)
    assert all(call['args'][0] == '-C' and call['args'][2] != 'checkout'
               for call in reused if call['name'] == 'git')


@pytest.mark.parametrize('failure', ['download', 'checksum'])
def test_failed_model_acquisition_keeps_destination_absent(robot_setup, failure):
    run, source, model, _ = robot_setup
    _checkout(source)
    result, _ = run({'FAIL_STAGE': failure})
    assert result.returncode != 0
    assert not model.exists()
    assert list(model.parent.iterdir()) == []
    assert (source / '.git/revision').read_text() == REVISION


def test_failed_clone_does_not_publish_partial_checkout(robot_setup):
    run, source, model, _ = robot_setup
    result, _ = run({'FAIL_STAGE': 'clone'})
    assert result.returncode != 0
    assert not source.exists()
    assert not model.exists()
    assert not list(source.parent.iterdir())


@pytest.mark.parametrize('revision, dirty', [(REVISION, True), ('wrong-revision', False)])
def test_existing_source_is_never_reset(robot_setup, revision, dirty):
    run, source, model, _ = robot_setup
    _checkout(source, revision, dirty)
    before = {str(path.relative_to(source)): path.read_bytes()
              for path in source.rglob('*') if path.is_file()}
    result, calls = run()
    assert result.returncode != 0
    assert all(call['name'] != 'curl' for call in calls)
    assert all(call['args'][2] != 'checkout' for call in calls if call['name'] == 'git')
    assert before == {str(path.relative_to(source)): path.read_bytes()
                      for path in source.rglob('*') if path.is_file()}
    assert not model.exists()


def test_corrupt_existing_model_is_preserved_for_explicit_repair(robot_setup):
    run, source, model, _ = robot_setup
    _checkout(source)
    model.parent.mkdir(parents=True)
    model.write_bytes(b'user-owned bad model')
    result, calls = run()
    assert result.returncode != 0
    assert model.read_bytes() == b'user-owned bad model'
    assert all(call['name'] != 'curl' for call in calls)


def test_explicit_source_and_model_paths_are_honored(robot_setup, tmp_path):
    run, default_source, default_model, _ = robot_setup
    source = tmp_path / 'custom speech source'
    model = tmp_path / 'custom models/small.bin'
    result, _ = run({'WHISPER_CPP_SOURCE_DIR': str(source),
                     'MALBUT_STT_MODEL_PATH': str(model)})
    assert result.returncode == 0, result.stderr
    assert (source / '.git/revision').read_text() == REVISION
    assert model.read_bytes() == MODEL
    assert not default_source.exists()
    assert not default_model.exists()
