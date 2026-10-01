"""Offline deployment checks: metadata only by default; local children opt-in."""

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from malbut_agent_server import fall_preflight as preflight
from malbut_agent_server import fall_preflight_probe as probe
from test_fall_runtime import config


@pytest.fixture
def deployment(tmp_path):
    data = config(tmp_path)
    data['tracking'] = None
    cfg = tmp_path / 'runtime.json'
    cfg.write_text(json.dumps(data))
    key = Path(data['cloud_key_file'])
    key.write_text('test-only-not-a-real-credential')
    key.chmod(0o600)
    journal = Path(data['journal_path'])
    journal.parent.mkdir(mode=0o700)
    model = tmp_path / 'test-pose.onnx'
    model.write_bytes(b'placeholder-not-a-model')
    python = tmp_path / 'python'
    python.symlink_to(sys.executable)
    prefix = tmp_path / 'install'
    for name, executable in preflight.PACKAGE_EXECUTABLES.items():
        (prefix / 'share' / name).mkdir(parents=True)
        if executable:
            path = prefix / 'lib' / name / executable
            path.parent.mkdir(parents=True)
            path.write_text('not executed')
            path.chmod(0o700)
    return SimpleNamespace(data=data, config=cfg, key=key, journal=journal, model=model,
                           python=python, prefix=prefix, tmp=tmp_path)


def inspect(d, **kwargs):
    return asyncio.run(preflight.inspect(d.config, d.model, d.python,
                                        prefix_lookup=lambda _: d.prefix, **kwargs))


def checks(report):
    return {c['name']: c for c in report['checks']}


def test_default_does_not_open_key_or_journal_or_launch_child(deployment, monkeypatch):
    d = deployment
    d.journal.write_bytes(b'not-opened')
    d.journal.chmod(0o600)
    original_open = os.open
    original_path_open = Path.open
    def guarded_open(path, *args, **kwargs):
        assert Path(path) == d.config
        return original_open(path, *args, **kwargs)
    def guarded_path_open(path, *args, **kwargs):
        assert path not in (d.key, d.journal)
        return original_path_open(path, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected subprocess/network/DB access')
    monkeypatch.setattr(os, 'open', guarded_open)
    monkeypatch.setattr(Path, 'open', guarded_path_open)
    import builtins
    import socket
    import sqlite3
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    report = inspect(d)
    assert report['checks_passed'] is True
    assert report['deployment_verified'] is False
    assert report['key_contents_read'] is False
    assert report['journal_opened'] is False
    assert report['cloud_requests'] == 0 and report['camera_started'] is False
    assert checks(report)['tracking']['reason'] == 'disabled'
    assert checks(report)['runtime_probe']['reason'] == 'not_requested'
    encoded = json.dumps(report)
    assert 'test-only-not-a-real-credential' not in encoded
    assert str(d.key) not in encoded and str(d.journal) not in encoded


def test_missing_db_is_not_created_and_registration_is_not_claimed(deployment):
    report = inspect(deployment)
    assert report['checks_passed']
    assert not deployment.journal.exists()
    assert checks(report)['journal_file']['reason'] == 'will_be_created_by_runtime'
    assert 'server_registration_settings_and_consent' in report['unverified']


@pytest.mark.parametrize('field', ['journal_path', 'cloud_key_file'])
def test_invalid_path_characters_are_failed_checks_not_tracebacks(deployment, field):
    d = deployment
    d.data[field] = '/test/invalid\x00path'
    d.config.write_text(json.dumps(d.data))
    report = inspect(d)
    assert not report['checks_passed']
    assert 'invalid\x00path' not in json.dumps(report)


@pytest.mark.parametrize('kind', ['missing', 'json', 'too_large', 'empty', 'symlink', 'fifo',
                                'directory', 'invalid_utf8'])
def test_bad_config_fails_safely_without_dumping_content(deployment, kind):
    d = deployment
    d.config.unlink()
    if kind == 'json':
        d.config.write_text('{sensitive-invalid-configuration')
    elif kind == 'too_large':
        d.config.write_bytes(b'x' * 16385)
    elif kind == 'empty':
        d.config.touch()
    elif kind == 'symlink':
        d.config.symlink_to(d.key)
    elif kind == 'fifo':
        os.mkfifo(d.config)
    elif kind == 'directory':
        d.config.mkdir()
    elif kind == 'invalid_utf8':
        d.config.write_bytes(b'\xff')
    report = inspect(d)
    assert not report['checks_passed']
    assert checks(report)['configuration']['reason'] == 'invalid_or_missing'
    assert 'sensitive-invalid-configuration' not in json.dumps(report)
    assert 'cloud_key_metadata' not in checks(report)


@pytest.mark.parametrize('mode,reason', [(0o600, 'ok'), (0o640, 'ok'),
                                      (0o644, 'unsafe_owner_or_permissions'),
                                      (0o660, 'unsafe_owner_or_permissions')])
def test_key_permissions_match_runtime_metadata_rules(deployment, mode, reason):
    deployment.key.chmod(mode)
    assert checks(inspect(deployment))['cloud_key_metadata']['reason'] == reason


@pytest.mark.parametrize('kind,reason', [('missing', 'missing'), ('empty', 'empty_file'),
    ('symlink', 'not_regular_file'), ('directory', 'not_regular_file'),
    ('too_large', 'too_large')])
def test_bad_key_metadata(deployment, kind, reason):
    key = deployment.key
    key.unlink()
    if kind == 'empty':
        key.touch(mode=0o600)
    elif kind == 'symlink':
        key.symlink_to(deployment.model)
    elif kind == 'directory':
        key.mkdir()
    elif kind == 'too_large':
        key.write_bytes(b'x' * 4097)
        key.chmod(0o600)
    assert checks(inspect(deployment))['cloud_key_metadata']['reason'] == reason


def test_key_wrong_owner_and_unreadable_are_rejected(deployment, monkeypatch):
    monkeypatch.setattr(os, 'getuid', lambda: deployment.key.stat().st_uid + 1)
    if deployment.key.stat().st_uid != 0:
        assert preflight.credential_reason(deployment.key) == 'unsafe_owner_or_permissions'
    monkeypatch.setattr(os, 'access', lambda *a: False)
    assert preflight.credential_reason(deployment.key) == 'not_accessible'


@pytest.mark.parametrize('kind', ['public_directory', 'public_file', 'symlink_file',
                                'symlink_directory', 'missing_directory'])
def test_database_paths_must_be_private_no_creation_or_open(deployment, kind):
    d = deployment
    if kind == 'public_directory':
        d.journal.parent.chmod(0o755)
    elif kind == 'public_file':
        d.journal.touch(mode=0o644)
    elif kind == 'symlink_file':
        d.journal.symlink_to(d.key)
    elif kind == 'symlink_directory':
        d.journal.parent.rmdir()
        actual = d.tmp / 'real-private'
        actual.mkdir(mode=0o700)
        d.journal.parent.symlink_to(actual, target_is_directory=True)
    elif kind == 'missing_directory':
        d.journal.parent.rmdir()
    report = inspect(d)
    assert not report['checks_passed']
    assert any(c['status'] == 'failed' and c['name'].startswith('journal_')
               for c in report['checks'])


def test_placeholder_device_and_missing_package_are_not_ready(deployment):
    d = deployment
    d.data['device_id'] = 'REPLACE_WITH_REGISTERED_DEVICE_ID'
    d.config.write_text(json.dumps(d.data))
    def missing(_):
        raise LookupError('arbitrary exception text must not be printed')
    report = asyncio.run(preflight.inspect(d.config, d.model, d.python, prefix_lookup=missing))
    assert not report['checks_passed']
    assert checks(report)['device_id']['reason'] == 'placeholder'
    assert checks(report)['package.malbut_fall_coordinator']['reason'] == 'package_not_installed'
    assert 'arbitrary exception' not in json.dumps(report)


def test_unexecutable_package_and_missing_pose_skip_model_probe(deployment):
    d = deployment
    (d.prefix / 'lib/malbut_fall_coordinator/fall_coordinator').chmod(0o600)
    d.model.unlink()
    calls = []
    async def runner(command, env):
        calls.append(command)
        return 'ok'
    report = inspect(d, probe=True, probe_runner=runner)
    assert not report['checks_passed'] and len(calls) == 1
    assert checks(report)['package.malbut_fall_coordinator']['reason'] == 'not_accessible'
    assert checks(report)['pose_probe']['reason'] == 'files_unavailable'


def enable_tracking(d):
    source = d.tmp / 'sam-source'
    source.mkdir()
    checkpoint = d.tmp / 'sam.pt'
    checkpoint.write_bytes(b'not-a-real-checkpoint')
    deps = d.tmp / 'deps'
    deps.mkdir()
    d.data['tracking'] = dict(python_executable=str(d.python), source_path=str(source),
                             checkpoint_path=str(checkpoint), python_paths=[str(deps)])
    d.config.write_text(json.dumps(d.data))


def test_model_probes_use_runtime_paths_without_keys_and_keep_venv_symlink(deployment,
                                                                        monkeypatch):
    d = deployment
    enable_tracking(d)
    monkeypatch.setenv('OPENAI_API_KEY', 'forbidden-secret')
    monkeypatch.setenv('OLLAMA_API_KEY', 'forbidden-secret')
    monkeypatch.setenv('LD_PRELOAD', 'forbidden-secret')
    monkeypatch.setenv('PYTHONPATH', 'overlay-only')
    calls = []
    async def runner(command, env):
        calls.append((command, env))
        return 'ok'
    report = inspect(d, probe=True, probe_runner=runner)
    assert report['checks_passed'] and not report['deployment_verified']
    assert len(calls) == 3
    assert calls[1][0][0] == str(d.python)  # NOT resolved to base interpreter
    assert calls[2][0][0] == str(d.python)
    for command, env in calls:
        assert 'forbidden-secret' not in json.dumps([command, env])
        assert str(d.key) not in command
        assert env['PYTHONDONTWRITEBYTECODE'] == '1'
    assert 'overlay-only' in calls[0][1]['PYTHONPATH']
    assert 'overlay-only' not in calls[2][1]['PYTHONPATH']
    assert 'sam_tracking_worker' in calls[2][0][2]
    assert '--checkpoint' in calls[2][0]


def test_tracking_requires_all_local_assets_and_failed_loading_is_visible(deployment):
    d = deployment
    enable_tracking(d)
    Path(d.data['tracking']['checkpoint_path']).unlink()
    async def runner(*a):
        return 'dependencies_or_model_failed'
    report = inspect(d, probe=True, probe_runner=runner)
    assert not report['checks_passed']
    assert checks(report)['tracking_probe']['reason'] == 'files_unavailable'
    assert checks(report)['runtime_probe']['status'] == 'failed'
    assert not d.journal.exists()


@pytest.mark.parametrize('program,expected', [
    ('print(\'{"ready":true}\')', 'ok'),
    ('import sys,time;sys.stdout.write(\'{"ready":\');sys.stdout.flush();'
     'time.sleep(.02);sys.stdout.write(\'true}\\n\')', 'ok'),
    ('import sys;print(\'{"ready":true}\');sys.exit(2)', 'dependencies_or_model_failed'),
    ('print("private diagnostics")', 'dependencies_or_model_failed'),
    ('print("x" * 1000000)', 'invalid_probe_output'),
    ('import sys;assert sys.stdin.read()=="";print(\'{"ready":true}\')', 'ok'),
])
def test_real_probe_protocol_handles_partial_output_overflow_and_eof(program, expected,
                                                                   capfd):
    result = asyncio.run(asyncio.wait_for(preflight.run_probe(
        [sys.executable, '-c', program], preflight.runtime_environment(), timeout_s=2), 4))
    assert result == expected
    assert 'private diagnostics' not in str(capfd.readouterr())


def test_probe_timeout_kills_and_reaps_child(monkeypatch):
    children = []
    original = asyncio.create_subprocess_exec
    async def capture(*a, **k):
        p = await original(*a, **k)
        children.append(p)
        return p
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', capture)
    result = asyncio.run(preflight.run_probe(
        [sys.executable, '-c', 'import time;time.sleep(30)'], {}, timeout_s=.05))
    assert result == 'probe_timeout' and children[0].returncode is not None


def test_cancellation_during_launch_reaps_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []
    async def scenario():
        launched, release = asyncio.Event(), asyncio.Event()
        async def slow_launch(*a, **k):
            p = await original(*a, **k)
            children.append(p)
            launched.set()
            await release.wait()
            return p
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', slow_launch)
        task = asyncio.create_task(preflight.run_probe(
            [sys.executable, '-c', 'import time;time.sleep(30)'], {}))
        await launched.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    asyncio.run(scenario())
    assert children[0].returncode is not None


def test_unavailable_probe_never_exposes_exception():
    assert asyncio.run(preflight.run_probe(['/nonexistent/test-python'], {})) == 'probe_unavailable'


def test_probe_hides_import_output_and_does_not_initialize_ros(monkeypatch, capsys):
    def imports():
        print('third-party initialization message')
    monkeypatch.setattr(probe, 'runtime_imports', imports)
    assert probe.main(['runtime']) == 0
    output = capsys.readouterr()
    assert output.out == '{"ready":true}\n'
    assert 'third-party' in output.err  # parent sends this to DEVNULL
    def failed():
        raise ValueError('must-not-be-reported')
    monkeypatch.setattr(probe, 'runtime_imports', failed)
    assert probe.main(['runtime']) == 2
    assert 'must-not-be-reported' not in str(capsys.readouterr())


@pytest.mark.parametrize('provider', ['auto', 'cpu', 'cuda'])
def test_pose_probe_loads_requested_estimator_without_inference(monkeypatch, capsys, provider):
    calls = []
    threads = []
    def estimator(path, **kwargs):
        calls.append((path, kwargs))
        return object()  # no predict()/infer() method
    for name in ('cv_bridge', 'cv2', 'rclpy'):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'cv2', SimpleNamespace(setNumThreads=threads.append))
    monkeypatch.setitem(sys.modules, 'homecam_detector.pose',
                        SimpleNamespace(PersonPoseEstimator=estimator))
    assert probe.main(['pose', '--model', '/test/model.onnx',
                       '--pose-execution-provider', provider]) == 0
    assert calls == [('/test/model.onnx', dict(keep_aspect=True, execution_provider=provider,
                                              intra_op_num_threads=2, allow_spinning=False))]
    assert threads == [1]
    assert capsys.readouterr().out == '{"ready":true}\n'


def test_cli_defaults_overrides_and_exit_status(monkeypatch, capsys, tmp_path):
    calls = []
    async def fake_inspect(*a, **k):
        calls.append((a, k))
        return dict(checks_passed=False, checks=[], deployment_verified=False)
    monkeypatch.setattr(preflight, 'inspect', fake_inspect)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    monkeypatch.delenv('MALBUT_FALL_POSE_MODEL', raising=False)
    monkeypatch.delenv('MALBUT_FALL_POSE_PYTHON', raising=False)
    monkeypatch.setenv('MALBUT_FALL_CONFIG', str(tmp_path / 'runtime.json'))
    assert preflight.main(['--json']) == 2
    assert not json.loads(capsys.readouterr().out)['deployment_verified']
    args, options = calls[0]
    assert args == (tmp_path / 'runtime.json', tmp_path / 'malbut_perception/yolo26s-pose.onnx',
                    tmp_path / 'malbut_fall_pose/runtime/bin/python')
    defaults = dict(pose_execution_provider='auto', pose_intra_op_num_threads=2,
                    pose_allow_spinning=False, pose_opencv_num_threads=1)
    assert options == dict(probe=False, **defaults)
    monkeypatch.setenv('MALBUT_FALL_POSE_MODEL', '/custom/pose.onnx')
    monkeypatch.setenv('MALBUT_FALL_POSE_PYTHON', '/custom/bin/python')
    assert preflight.main(['--probe', '--config', '/custom/config.json',
                          '--pose-execution-provider', 'cuda']) == 2
    defaults['pose_execution_provider'] = 'cuda'
    assert calls[1] == ((Path('/custom/config.json'), Path('/custom/pose.onnx'),
                         Path('/custom/bin/python')), dict(probe=True, **defaults))
    assert '준비가 필요한' in capsys.readouterr().out


def test_pose_probe_command_checks_selected_runtime_and_provider(deployment):
    calls = []
    async def runner(command, env):
        calls.append(command)
        return 'dependencies_or_model_failed' if 'pose' in command else 'ok'
    report = inspect(deployment, probe=True, pose_execution_provider='cuda',
                     pose_intra_op_num_threads=3, pose_allow_spinning=True,
                     pose_opencv_num_threads=2, probe_runner=runner)
    command = calls[1]
    assert command[0] == str(deployment.python)
    assert command[command.index('--pose-execution-provider') + 1] == 'cuda'
    assert command[command.index('--pose-intra-op-num-threads') + 1] == '3'
    assert command[command.index('--pose-allow-spinning') + 1] == 'true'
    assert command[command.index('--pose-opencv-num-threads') + 1] == '2'
    assert checks(report)['pose_probe']['status'] == 'failed'


def test_source_and_robot_mirror_are_identical():
    repo = Path(__file__).resolve().parents[2]
    for rel in ('malbut_agent_server/fall_preflight.py',
                'malbut_agent_server/fall_preflight_probe.py',
                'malbut_agent_server/adapters/outbound/sam_tracking.py', 'setup.py',
                'docs/fall/fall_robot_preparation.md'):
        assert (repo / 'malbut_agent_server' / rel).read_bytes() == (
            repo / 'malbut_test/malbut_agent_server' / rel).read_bytes()
