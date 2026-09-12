"""Check map validation and process ownership without launching ROS or a robot."""

from pathlib import Path
import signal
import threading
from unittest.mock import Mock

import pytest
import yaml

from malbut_bringup.web_runtime import RuntimeSupervisor, SavedMapCatalog


def _map(directory, name='home.yaml', **changes):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'home.pgm').write_bytes(b'P5\n2 2\n255\n\xff\xff\x00\x80')
    metadata = {'image': 'home.pgm', 'resolution': 0.05, 'origin': [0, 0, 0],
                'negate': 0, 'occupied_thresh': 0.65, 'free_thresh': 0.25}
    metadata.update(changes)
    path = directory / name
    path.write_text(yaml.safe_dump(metadata), encoding='utf-8')
    return path


def test_catalog_lists_valid_images_without_modifying_maps(tmp_path):
    """An absent directory is empty; existing valid maps keep their contents."""
    catalog = SavedMapCatalog(tmp_path / 'maps')
    assert catalog.list_maps() == []
    path = _map(catalog.directory)
    before = path.read_bytes()
    assert catalog.resolve('home.yaml') == path
    assert catalog.list_maps() == [{'id': 'home.yaml', 'name': 'home', 'path': str(path)}]
    assert path.read_bytes() == before
    _map(catalog.directory, 'broken.yaml', resolution=-1)
    assert len(catalog.list_maps()) == 1
    (catalog.directory / 'home.pgm').write_bytes(b'not an image')
    assert catalog.list_maps() == []


def test_catalog_rejects_path_traversal_symlinks_and_unsafe_yaml(tmp_path):
    """Neither YAML nor its image may resolve outside the configured directory."""
    catalog = SavedMapCatalog(tmp_path / 'maps')
    valid = _map(catalog.directory)
    external = _map(tmp_path / 'outside')
    (catalog.directory / 'linked.yaml').symlink_to(external)
    for value in ('../outside/home.yaml', str(valid), 'linked.yaml', None):
        with pytest.raises(ValueError):
            catalog.resolve(value)
    _map(catalog.directory, image=str(external.parent / 'home.pgm'))
    with pytest.raises(ValueError, match='inside'):
        catalog.resolve('home.yaml')
    (catalog.directory / 'image.pgm').symlink_to(external.parent / 'home.pgm')
    _map(catalog.directory, image='image.pgm')
    with pytest.raises(ValueError, match='inside'):
        catalog.resolve('home.yaml')
    valid.write_text('!!python/object/apply:os.system [echo unsafe]', encoding='utf-8')
    with pytest.raises(ValueError):
        catalog.resolve('home.yaml')


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """Replace all process creation and group signals with in-memory mocks."""
    catalog = SavedMapCatalog(tmp_path / 'maps')
    _map(catalog.directory)
    process = Mock(pid=43210)
    process.poll.return_value = None
    popen = Mock(return_value=process)
    alive = {'value': True, 'ignore': False}

    def killpg(pid, sig):
        assert pid == process.pid
        if not alive['value']:
            raise ProcessLookupError()
        if sig and not alive['ignore']:
            alive['value'] = False
            process.poll.return_value = 0

    signals = Mock(side_effect=killpg)
    monkeypatch.setattr('malbut_bringup.web_runtime.subprocess.Popen', popen)
    monkeypatch.setattr('malbut_bringup.web_runtime.os.killpg', signals)
    supervisor = RuntimeSupervisor(catalog, tmp_path / 'logs', (
        (signal.SIGINT, 0), (signal.SIGTERM, 0), (signal.SIGKILL, 0)))
    yield supervisor, popen, process, signals, alive
    alive['value'] = False
    supervisor.close()


def test_navigation_launch_uses_only_fixed_argv_and_explicit_selected_map(runtime):
    """No shell, arbitrary launch, or overlapping process can be requested."""
    supervisor, popen, _, signals, _ = runtime
    with pytest.raises(ValueError):
        supervisor.start('anything')
    assert supervisor.start('navigation', 'home.yaml', False).result()['state'] == 'RUNNING'
    args, kwargs = popen.call_args
    assert args[0] == [
        'ros2', 'launch', 'malbut_bringup', 'robot.launch.py', 'mode:=navigation',
        'web_panel:=false', 'start_hardware:=false',
        f'map:={supervisor.catalog.resolve("home.yaml")}', 'publish_debug_image:=true']
    assert kwargs['start_new_session'] is True
    assert not kwargs.get('shell', False)
    with pytest.raises(RuntimeError, match='Stop'):
        supervisor.start('mapping')
    assert Path(supervisor.snapshot()['log_path']).is_file()
    assert supervisor.stop().result()['state'] == 'STOPPED'
    assert any(call.args[1] == signal.SIGINT for call in signals.call_args_list)


def test_start_stop_race_is_serialized_without_blocking_caller(runtime):
    """A stop submitted during process creation waits in the worker, not the caller."""
    supervisor, popen, process, _, _ = runtime
    entered, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return process

    popen.side_effect = delayed
    started = supervisor.start('mapping')
    assert entered.wait(2)
    stopped = supervisor.stop()
    assert not stopped.done()
    assert supervisor.stop() is stopped
    assert supervisor.snapshot()['state'] == 'STOPPING'
    with pytest.raises(RuntimeError):
        supervisor.start('navigation', 'home.yaml')
    release.set()
    started.result(timeout=2)
    assert stopped.result(timeout=2)['state'] == 'STOPPED'
    assert popen.call_count == 1


def test_dead_leader_never_discards_remaining_process_group(runtime):
    """A failed launch with surviving descendants still requires owned cleanup."""
    supervisor, _, process, signals, alive = runtime
    supervisor.start('mapping').result()
    process.poll.return_value = 1
    assert supervisor.snapshot()['state'] == 'ERROR'
    with pytest.raises(RuntimeError):
        supervisor.start('mapping')
    alive['ignore'] = True
    with pytest.raises(RuntimeError, match='ownership retained'):
        supervisor.stop().result()
    with pytest.raises(RuntimeError):
        supervisor.start('mapping')
    sent = [call.args[1] for call in signals.call_args_list if call.args[1]]
    assert sent == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    alive['ignore'] = False
    assert supervisor.stop().result()['state'] == 'STOPPED'


def test_failed_spawn_reports_error_without_claiming_external_processes(runtime):
    """A missing ros2 executable is visible and sends no process-group signal."""
    supervisor, popen, _, signals, _ = runtime
    popen.side_effect = FileNotFoundError('ros2 not found')
    with pytest.raises(FileNotFoundError):
        supervisor.start('mapping').result()
    assert supervisor.snapshot()['state'] == 'ERROR'
    supervisor.stop().result()
    signals.assert_not_called()


def test_error_exposes_only_bounded_owned_log_tail(runtime):
    """Bringup startup failure details must reach the web without unbounded reads."""
    supervisor, _, process, _, _ = runtime
    supervisor.start('mapping').result()
    path = Path(supervisor.snapshot()['log_path'])
    path.write_text('old line\n' * 2000 + 'missing runtime Python\n')
    assert supervisor.snapshot()['log_tail'] == ''
    process.poll.return_value = 1
    status = supervisor.snapshot()
    assert status['state'] == 'ERROR'
    assert status['log_tail'].endswith('missing runtime Python\n')
    assert len(status['log_tail'].encode()) <= 8192
