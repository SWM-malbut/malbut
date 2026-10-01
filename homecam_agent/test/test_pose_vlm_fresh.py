"""Budget invariants of the opt-in fresh comparison; no network or secrets."""
import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('fresh_comparison', SCRIPTS/'evaluate_pose_vlm_fresh.py')
fresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fresh)


def test_budget_holds_unknown_calls_and_never_reuses_them():
    budget = fresh.Budget('1')
    for _ in range(20):
        budget.acquire()
        budget.settle(None)
    with pytest.raises(RuntimeError, match='blocked'):
        budget.acquire()
    assert budget.state()['held_unknown_usd'] == '1.00'


def test_actual_usage_reconciles_reservation():
    budget = fresh.Budget('1')
    budget.acquire()
    budget.settle('.001')
    assert budget.known == fresh.Decimal('.001')
    assert budget.held == 0


@pytest.mark.parametrize('amount', ['0', '1.01', '-1', 'NaN', 'Infinity'])
def test_authorization_ceiling_is_one_dollar(amount):
    with pytest.raises(ValueError):
        fresh.Budget(amount)


def test_unknown_is_not_counted_as_normal_or_correct():
    row = dict(case_id='one', purpose='crosscheck', assessment=None,
        outcome='timeout', localization_failed=None, latency_s=None, pose_candidate=False)
    report = fresh.summary([row], {'one': 'normal_activity'}, fresh.Budget('1'))
    assert report['scene_correct'] == 0
    assert report['normal']['vlm_normal'] == 0
    assert report['normal']['vlm_unknown'] == 1


def test_input_canvas_preserves_aspect_and_existing_robot_pixels():
    import numpy as np
    original = np.full((400,640,3), 255, np.uint8)
    assert fresh.camera_bgr(original) is original
    wide = fresh.camera_bgr(np.full((720,1280,3), 255, np.uint8))
    assert wide.shape == (400,640,3)
    assert not wide[:20].any() and not wide[380:].any()
    assert np.all(wide[20:380] == 255)
