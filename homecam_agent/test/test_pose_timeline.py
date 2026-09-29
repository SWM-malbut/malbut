"""Offline tests: no ROS graph, GPU, credentials or Cloud calls."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from benchmark_fall_pose import cpu_interval
from plot_pose_timeline import load_condition, validate_comparison


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


def condition():
    manifest = {'model_sha256': 'model', 'media_sha256': 'media', 'camera_input': '640x400',
                'cases': ['case'], 'repeats': 1, 'runner_sha256': 'runner',
                'ort': 'version', 'opencv': 'version', 'options': {}}
    return {'manifest': manifest, 'replay_order': [('case', 0)]}


def test_stage_comparison_accepts_different_execution_options():
    before, after = condition(), condition()
    after['manifest']['options'] = {'execution_provider': 'cuda'}
    validate_comparison([before, after])


@pytest.mark.parametrize('field', ['model_sha256', 'media_sha256', 'camera_input', 'cases',
                                 'repeats', 'runner_sha256', 'ort', 'opencv'])
def test_stage_comparison_rejects_changed_inputs(field):
    before, after = condition(), condition()
    after['manifest'][field] = 'changed'
    with pytest.raises(ValueError, match=field):
        validate_comparison([before, after])


def test_stage_comparison_rejects_different_replay_order():
    before, after = condition(), condition()
    after['replay_order'] = [('case', 1)]
    with pytest.raises(ValueError, match='replay order'):
        validate_comparison([before, after])


def test_stage_comparison_requires_measurement_identity():
    before, after = condition(), condition()
    for item in (before, after):
        del item['manifest']['runner_sha256']
    with pytest.raises(ValueError, match='runner_sha256'):
        validate_comparison([before, after])
