import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from malbut_resource_monitor.resources import (
    LinuxSampler, Tegra, classify, cpu_percent, cpu_ticks, parse_tegrastats,
)
from malbut_resource_monitor.ros_observer import ActionEvents, channel_name
from malbut_resource_monitor.store import Store
from malbut_resource_monitor.viewer import LogServer


def test_cpu_excludes_guest_and_first_sample_is_not_zero():
    ticks = cpu_ticks('cpu 100 10 20 70 10 5 5 0 99 5\ncpu0 1 0 0 1 0 0 0 0\n')
    assert ticks['cpu'] == (220, 80)
    assert cpu_percent(ticks['cpu'], None) is None
    assert cpu_percent((320, 100), (220, 80)) == 80
    assert cpu_percent((220, 80), (220, 80)) is None


@pytest.mark.parametrize('gpu', ['99%@[1098,1098]', '99%@1098'])
def test_nvidia_units_and_multiple_gpu_clocks(gpu):
    parsed = parse_tegrastats(
        f'RAM 1234/7800MB (lfb 2x4MB) CPU [2%@729,off] EMC_FREQ 34%@1600 '
        f'GR3D_FREQ {gpu} cpu@45.5C VDD_IN 5123mW/5345mW '
        'VDD_SOC 1000/1200 VIN_SYS_5V0 3W/4W')
    assert parsed['gpu_percent'] == 99
    assert parsed['gpu0_mhz'] == 1098
    assert parsed['emc_percent'] == 34
    assert parsed['emc_mhz'] == 1600
    assert parsed['temp.cpu_c'] == 45.5
    assert parsed['power.VDD_IN_mw'] == 5123
    assert parsed['power_avg.VDD_SOC_mw'] == 1200
    assert parsed['power.VIN_SYS_5V0_mw'] == 3000


def test_missing_or_stale_tegrastats_does_not_invent_zero_or_repeat():
    tegra = Tegra.__new__(Tegra)
    tegra.lock, tegra.interval, tegra.latest = threading.Lock(), 1, None
    assert tegra.sample()['gpu_percent'] is None
    tegra.latest = (time.monotonic() - 10, {'gpu_percent': 80})
    assert tegra.sample()['gpu_percent'] is None
    tegra.latest = (time.monotonic(), {'gpu_percent': 80})
    assert tegra.sample()['gpu_percent'] == 80
    assert parse_tegrastats('CPU [5%@1000]')['gpu_percent'] is None


def test_real_proc_sampler_paths_and_no_fake_per_process_gpu(tmp_path):
    store = Store(tmp_path, 1, os.getpid())
    sampler = LinuxSampler(os.getpid(), store)
    first = sampler.sample()
    second = sampler.sample()
    assert first['cpu_percent'] is None
    assert second['ram_used_mib'] > 0
    catalog = store.metadata['process_catalog']
    own = next(value for value in catalog.values() if value['pid'] == os.getpid())
    assert own['executable'].startswith('/')
    assert own['group'] == 'observer'
    records = [json.loads(line) for line in
               (store.path / 'processes/observer.jsonl').read_text().splitlines()]
    assert all(row['gpu_percent'] is None for row in records)
    assert records[0]['cpu_percent'] is None
    assert 'argv' not in own
    store.close('test')
    assert (store.path.stat().st_mode & 0o777) == 0o700
    assert ((store.path / 'metadata.json').stat().st_mode & 0o777) == 0o600


def test_shared_nav2_not_falsely_split_by_node():
    assert classify(['/opt/ros/humble/lib/rclcpp_components/component_container_isolated',
                     '--ros-args', '-r', '__node:=nav2_container'], 5, 10) == 'nav2_shared'
    assert classify(['/usr/bin/python3', '/ws/install/malbut_stt/lib/malbut_stt/stt'],
                    5, 10) == 'speech'


def status(state, goal=1):
    return SimpleNamespace(status_list=[SimpleNamespace(
        status=state, goal_info=SimpleNamespace(
            goal_id=SimpleNamespace(uuid=[goal] * 16),
            stamp=SimpleNamespace(sec=123, nanosec=456)))])


def test_action_dedup_historic_goal_and_no_fabricated_request_time(tmp_path):
    store = Store(tmp_path, 1, os.getpid())
    events = ActionEvents(store)
    for state in (1, 2, 2, 3, 5, 5):
        events.observe('/follow_person', status(state))
    events.observe('/follow_person', status(4, goal=2))
    channel = channel_name('actions', '/follow_person')
    records = [json.loads(line) for line in
               (store.path / (channel + '.jsonl')).read_text().splitlines()]
    assert [r['state'] for r in records] == [
        'ACCEPTED', 'EXECUTING', 'CANCELING', 'CANCELED', 'SUCCEEDED']
    assert all(r['request_wall_ns'] is None and r['motion_start_wall_ns'] is None for r in records)
    assert records[-1]['initial_terminal']
    assert records[0]['accepted_ros_ns'] == 123000000456
    assert channel_name('topics', '/a/b') != channel_name('topics', '/a_b')
    store.close('test')


def test_viewer_reads_exact_samples_and_blocks_path_traversal(tmp_path):
    store = Store(tmp_path, 1, os.getpid())
    store.register('system', kind='system', label='system')
    store.write('system', {'cpu_percent': 31.25, 'gpu_percent': None})
    store.close('test')
    server = LogServer(('127.0.0.1', 0), tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        query = urlencode({'session': store.path.name, 'channel': 'system'})
        with urlopen(base + '/api/data?' + query) as response:
            result = json.load(response)
        assert result['rows'][0]['cpu_percent'] == 31.25
        assert result['rows'][0]['gpu_percent'] is None
        assert not result['truncated']
        for session, channel in (('../elsewhere', 'system'), (store.path.name, '../../secret')):
            with pytest.raises(HTTPError) as error:
                urlopen(base + '/api/data?' + urlencode({'session': session, 'channel': channel}))
            assert error.value.code == 400
        with urlopen(base + '/') as response:
            assert '관측' in response.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_deployment_wiring_only():
    deployment = Path(__file__).resolve().parents[2]
    launch = (deployment / 'malbut_bringup/launch/robot.launch.py').read_text()
    assert "'resource_monitor': 'true'" in launch
    assert "return record_first(startup, value('resource_log_root'))" in launch
    assert '"$robot_source_dir/malbut_resource_monitor"' in (deployment / 'build.sh').read_text()
