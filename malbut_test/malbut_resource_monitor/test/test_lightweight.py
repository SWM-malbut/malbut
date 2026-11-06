"""Recorder overhead reductions must not change the meaning of a sample."""

import json
from pathlib import Path
import shutil

import pytest

from malbut_resource_monitor.resources import LinuxSampler
from malbut_resource_monitor.store import Store


def process(proc, pid=42, start=123, comm='python3', ticks=0, rss=4096, launch='robot'):
    path = proc / str(pid)
    path.mkdir(parents=True, exist_ok=True)
    fields = ['S', '1'] + ['0'] * 17 + [str(start)]
    fields[11] = str(ticks)
    (path / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields))
    (path / 'cmdline').write_bytes(b'/test/malbut/patrol_manager\0--key\0secret\0')
    (path / 'status').write_text(f'VmRSS: {rss} kB\nVmSwap: 0 kB\n')
    (path / 'environ').write_bytes(f'MALBUT_MEASUREMENT_LAUNCH={launch}\0'.encode())


def test_metadata_cache_keeps_live_measurements_and_refreshes_exec(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('malbut_resource_monitor.resources.time.monotonic', lambda: clock[0])
    proc = tmp_path / 'proc'
    process(proc)
    reads = []
    original_read = Path.read_bytes

    def count_read(path):
        reads.append(path.name)
        return original_read(path)

    monkeypatch.setattr(Path, 'read_bytes', count_read)
    store = Store(tmp_path / 'logs', 1, 42)
    sampler = LinuxSampler(42, store, proc=proc, sys=tmp_path / 'sys')
    sampler.sample()
    for step in range(1, 5):
        clock[0] += 1
        process(proc, ticks=step * sampler.hz, rss=8192, launch='patrol')
        sampler.sample()
    # Resource measurements are fresh every second; stable metadata is read once.
    assert reads.count('cmdline') == reads.count('environ') == 1
    rows = [json.loads(line) for line in
            (store.path / 'processes/patrol.jsonl').read_text().splitlines()]
    assert rows[0]['cpu_percent'] is None
    assert all(row['cpu_percent'] == pytest.approx(100) for row in rows[1:])
    assert rows[-1]['ram_rss_mib'] == 8
    assert store.metadata['process_catalog']['42-123']['launch'] == 'robot'
    clock[0] += 1
    sampler.sample()
    assert reads.count('cmdline') == reads.count('environ') == 2
    assert store.metadata['process_catalog']['42-123']['launch'] == 'patrol'
    # A changed comm (exec) refreshes classification before the five-second timeout.
    process(proc, comm='new binary', launch='tracking')
    (proc / '42/cmdline').write_bytes(b'/test/malbut/yolo_node\0')
    clock[0] += 1
    sampler.sample()
    assert store.metadata['process_catalog']['42-123']['group'] == 'perception'
    assert store.metadata['process_catalog']['42-123']['launch'] == 'tracking'
    assert 'secret' not in repr(sampler.descriptors)
    store.close('test')


def test_new_reused_and_exited_pids_do_not_wait_for_metadata_refresh(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('malbut_resource_monitor.resources.time.monotonic', lambda: clock[0])
    proc = tmp_path / 'proc'
    process(proc)
    store = Store(tmp_path / 'logs', 1, 42)
    sampler = LinuxSampler(42, store, proc=proc, sys=tmp_path / 'sys')
    sampler.sample()
    clock[0] += 1
    process(proc, start=456, launch='tracking')
    process(proc, pid=43, launch='fall')
    sampler.sample({'42-123': {'gpu_memory_mib': 12}})
    assert set(sampler.descriptors) == {'42-456', '43-123'}
    assert store.metadata['process_catalog']['42-456']['launch'] == 'tracking'
    assert store.metadata['process_catalog']['43-123']['launch'] == 'fall'
    reused = json.loads((store.path / 'launches/tracking.jsonl').read_text())
    assert reused['cpu_percent'] is None and reused['gpu_memory_mib'] is None
    shutil.rmtree(proc / '43')
    sampler.sample()
    assert '43-123' not in sampler.descriptors
    assert '43-123' not in sampler.process_metadata
    store.close('test')


def test_buffered_flush_batches_metadata_and_keeps_views_identical(tmp_path, monkeypatch):
    store = Store(tmp_path, 1, 42, buffered=True)
    writes = []
    original_write = store._write_metadata

    def count_write():
        writes.append(1)
        original_write()

    monkeypatch.setattr(store, '_write_metadata', count_write)
    store.register('processes/patrol', kind='process', label='patrol')
    store.register('launches/robot', kind='launch', label='robot')
    store.process('42-123', {'pid': 42})
    assert writes == []
    store.write_many(('processes/patrol', 'launches/robot'), {'cpu_percent': 10})
    store.flush()
    assert writes == [1]
    assert (store.path / 'processes/patrol.jsonl').read_bytes() == (
        store.path / 'launches/robot.jsonl').read_bytes()
    assert json.loads((store.path / 'metadata.json').read_text())['process_catalog']['42-123']
    store.write('speech', {'event': 'tts_playback', 'state': 'finished'})
    store.close('test')
    assert json.loads((store.path / 'speech.jsonl').read_text())['state'] == 'finished'
    assert json.loads((store.path / 'metadata.json').read_text())['finished']


def test_write_many_rejects_invalid_channels_before_writing(tmp_path):
    store = Store(tmp_path, 1, 42, buffered=True)
    with pytest.raises(ValueError, match='Invalid channel'):
        store.write_many(('system', '../outside'), {'cpu_percent': 10})
    assert not (store.path / 'system.jsonl').exists()
    store.close('test')


@pytest.mark.parametrize('mode', [None, 'full'])
def test_launch_passes_light_default_and_full_opt_in(mode):
    pytest.importorskip('launch_ros')
    from launch import LaunchContext
    from launch.utilities import perform_substitutions
    from launch_ros.actions import Node
    from malbut_resource_monitor.launch_support import record_first

    actions = record_first([], '/tmp/resource-test', **({'topic_mode': mode} if mode else {}))
    recorder, = [item for item in actions if isinstance(item, Node)]
    context = LaunchContext()
    context.extend_locals({'ros_specific_arguments': {'name': '__node:=resource_recorder'}})
    args = [perform_substitutions(context, part) for part in recorder.cmd[1:]]
    assert args[args.index('--topic-mode') + 1] == (mode or 'light')
