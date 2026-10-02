import json
import sys
import threading
from types import SimpleNamespace

from malbut_resource_monitor.gpu import JtopSampler, snapshot
from malbut_resource_monitor.resources import LinuxSampler
from malbut_resource_monitor.store import Store


def proc_entry(root, pid, start=123, launch='tracking'):
    path = root / str(pid)
    path.mkdir(exist_ok=True, parents=True)
    # After comm, field 3=state, 4=ppid, 22=starttime.
    fields = ['S', '1'] + ['0'] * 17 + [str(start)]
    (path / 'stat').write_text(f'{pid} (process name) ' + ' '.join(fields))
    (path / 'cmdline').write_bytes(b'/test/malbut/yolo_node\0')
    (path / 'status').write_text('VmRSS: 4096 kB\nVmSwap: 0 kB\n')
    (path / 'environ').write_bytes(
        b'SECRET=never-save-me\0' +
        (f'MALBUT_MEASUREMENT_LAUNCH={launch}\0'.encode() if launch else b''))


def test_jtop_units_and_unavailable_values(tmp_path):
    proc_entry(tmp_path, 42)
    jetson = SimpleNamespace(
        gpu={'ga10b': {'status': {'load': 37.5}, 'freq': {'cur': 918000}},
             'missing': {'status': {'load': -1}, 'freq': {'cur': None}}},
        processes=[[42, 'user', 'GPU', 'G', 20, 'S', 5, 1024, 2048, 'yolo'],
                   [43, 'user', 'GPU', 'G', 20, 'S', 5, 1024, 10, 'exited'],
                   [42, 'user', 'GPU', 'G', 20, 'S', 5, 1024, -1, 'invalid'], []])
    system, processes = snapshot(jetson, tmp_path)
    assert system == {'gpu.ga10b_percent': 37.5, 'gpu.ga10b_mhz': 918,
                      'gpu.missing_percent': None, 'gpu.missing_mhz': None}
    assert processes == {'42-123': {'gpu_memory_mib': 2}}
    assert 'gpu_percent' not in processes['42-123']


def test_jtop_stale_error_and_recovery_do_not_reuse_values(tmp_path, monkeypatch):
    proc_entry(tmp_path, 42)
    clock = [100]
    monkeypatch.setattr('malbut_resource_monitor.gpu.time.monotonic', lambda: clock[0])
    sampler = JtopSampler.__new__(JtopSampler)
    sampler.lock, sampler.latest, sampler.error = threading.Lock(), None, None
    sampler.interval, sampler.proc = 1, tmp_path
    assert sampler.sample() == ({'jtop_available': False, 'jtop_age_s': None,
                                 'jtop_error': None}, {})
    jetson = SimpleNamespace(gpu={}, processes=[
        [42, 'user', 'GPU', 'G', 20, 'S', 5, 1024, 2048, 'yolo']])
    sampler._capture(jetson)
    assert sampler.sample()[1]['42-123']['gpu_memory_mib'] == 2
    clock[0] += 4
    assert sampler.sample()[1] == {}
    assert not sampler.sample()[0]['jtop_available']
    sampler._capture(SimpleNamespace())
    assert 'AttributeError' in sampler.sample()[0]['jtop_error']
    sampler._capture(jetson)
    assert sampler.sample()[0]['jtop_available']


def test_missing_jtop_is_optional(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jtop', None)
    sampler = JtopSampler(1)
    sampler.thread.join(timeout=2)
    assert not sampler.thread.is_alive()
    system, processes = sampler.sample()
    assert not system['jtop_available'] and 'ModuleNotFoundError' in system['jtop_error']
    assert processes == {}
    sampler.close()


def test_launch_logs_share_exact_samples_and_gpu_uses_process_identity(tmp_path):
    proc = tmp_path / 'proc'
    proc_entry(proc, 42)
    proc_entry(proc, 43, launch=None)
    store = Store(tmp_path / 'logs', 1, 42)
    sampler = LinuxSampler(42, store, proc=proc, sys=tmp_path / 'sys')
    sampler.sample({'42-123': {'gpu_memory_mib': 12}})
    catalog = store.metadata['process_catalog']
    assert catalog['42-123']['launch'] == 'tracking'
    assert catalog['42-123']['launch_source'] == 'environment'
    assert catalog['43-123']['launch'] is None
    launch = json.loads((store.path / 'launches/tracking.jsonl').read_text())
    process = json.loads((store.path / 'processes/perception.jsonl').read_text().splitlines()[0])
    assert launch == process
    assert launch['gpu_memory_mib'] == 12 and launch['gpu_percent'] is None
    # Fork/exec may expose the marker on a later sample; retry unknown membership.
    proc_entry(proc, 43, launch='fall')
    # Reused PID must not receive the cached GPU sample or old launch.
    proc_entry(proc, 42, start=456, launch='robot')
    sampler.sample({'42-123': {'gpu_memory_mib': 12}})
    assert catalog['43-123']['launch'] == 'fall'
    assert catalog['42-456']['launch'] == 'robot'
    new = json.loads((store.path / 'launches/robot.jsonl').read_text())
    assert new['gpu_memory_mib'] is None and new['gpu_memory_source'] is None
    store.close('test')
    assert 'never-save-me' not in (store.path / 'metadata.json').read_text()
