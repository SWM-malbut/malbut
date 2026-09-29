"""Offline tests: no ROS graph, GPU, credentials or Cloud calls."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from benchmark_fall_pose import cpu_interval
from plot_pose_timeline import load_condition


def test_cpu_percent_is_not_capped_to_one_core():
    assert cpu_interval((100, 10), (100.5, 12.625)) == 525


@pytest.mark.parametrize('end', [(100, 11), (99, 11), (101, 9)])
def test_non_monotonic_clocks_are_rejected(end):
    with pytest.raises(ValueError):
        cpu_interval((100, 10), end)


def write_replay(root, samples):
    (root / 'manifest.json').write_text(json.dumps({'mode': 'realtime'}))
    row = {'case_id': 'test', 'repeat': 0, 'errors': [], 'wall_s': 1.2,
           'cpu_percent': 123, 'processed_fps': 4, 'inference_count': 2,
           'conversion_count': 2, 'dropped_before_callback': 0}
    (root / 'summary.json').write_text(json.dumps([row]))
    (root / 'test-0.json').write_text(json.dumps({
        'resource_samples': samples, 'raw': {'inference_ms': [8, 10]}}))


def test_cannot_invent_timeline_from_replay_average(tmp_path):
    write_replay(tmp_path, [])
    with pytest.raises(ValueError, match='missing interval'):
        load_condition(tmp_path)


@pytest.mark.parametrize('times', [[.5, .4], [.5, 1.3]])
def test_rejects_invalid_sample_timestamps(tmp_path, times):
    write_replay(tmp_path, [{'elapsed_s': t} for t in times])
    with pytest.raises(ValueError, match='timestamp'):
        load_condition(tmp_path)


def test_retains_zero_gpu_usage_and_missing_measurements(tmp_path):
    samples = [{'elapsed_s': .5, 'cpu_percent': 120, 'gpu_percent': 0},
               {'elapsed_s': 1, 'cpu_percent': 126, 'gpu_percent': None}]
    write_replay(tmp_path, samples)
    result = load_condition(tmp_path)
    assert [p['gpu_percent'] for p in result['points']] == [0, None]
    assert result['cpu_median'] == 123
    assert result['inference_ms_median'] == 9
    assert result['boundaries'] == [1.2]
