"""Sampling and device controls; synthetic tests are not model accuracy evidence."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from benchmark_deferred_tracking import profile_settings, rows_after_seed


def fixtures(seed=6):
    return (dict(schedule=[0, 6, 12], seed=None if seed is None else dict(source_frame=seed)),
            [dict(source_frame=i, captured_at=i / 24) for i in (0, 6, 12)])


def test_no_discovery_does_not_start_tracking():
    assert rows_after_seed(*fixtures(None)) == []


def test_exact_seed_and_original_times_preserved():
    case, rows = fixtures()
    assert rows_after_seed(case, rows) == rows[1:]
    assert rows_after_seed(case, rows)[0]['captured_at'] == .25


def test_missing_exact_seed_is_not_replaced_with_nearest():
    with pytest.raises(ValueError):
        rows_after_seed(*fixtures(7))


def test_schedule_cannot_drop_frames():
    case, rows = fixtures()
    with pytest.raises(ValueError):
        rows_after_seed(case, rows[:-1])


def test_duplicate_timestamp_is_rejected():
    case, rows = fixtures()
    rows[-1]['captured_at'] = rows[-2]['captured_at']
    with pytest.raises(ValueError):
        rows_after_seed(case, rows)


@pytest.mark.parametrize('name', ['cpu-fp32', 'cuda-fp32', 'cuda-bf16'])
def test_device_comparison_keeps_postprocess_and_offload_conditions(name):
    profile = profile_settings(name)
    assert profile['fill_hole_area'] == 0
    assert profile['offload_video_to_cpu'] is True
    assert profile['offload_state_to_cpu'] is True
    assert profile['compiled'] is False
    assert profile['allow_tf32'] is False


def test_unknown_profile_cannot_silently_fallback():
    with pytest.raises(ValueError):
        profile_settings('cuda-auto')
