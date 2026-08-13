"""Behavioral tests for weighted-idleness roaming selection."""

import pytest

from malbut_roaming.geometry import Point2D
from malbut_roaming.grid_map import Candidate
from malbut_roaming.policy import PolicyConfig, RoamingPolicy


def _config(**overrides):
    values = {
        'minimum_goal_distance': 1.0,
        'maximum_goal_distance': 10.0,
        'preferred_goal_distance': 4.0,
        'distance_scale': 1.5,
        'open_clearance': 1.0,
        'peripheral_clearance': 0.7,
        'peripheral_probability': 0.0,
        'revisit_horizon_seconds': 60.0,
        'recent_goal_radius': 2.0,
        'recent_memory_size': 3,
        'failure_cooldown_seconds': 30.0,
        'idleness_weight': 3.0,
        'distance_weight': 1.0,
        'clearance_weight': 1.0,
        'novelty_weight': 2.0,
        'top_k': 1,
        'temperature': 0.5,
    }
    values.update(overrides)
    return PolicyConfig(**values)


def _candidate(cell_x, x, clearance):
    return Candidate(cell_x, 0, x, 0.0, clearance)


@pytest.mark.parametrize(
    ('overrides', 'message'),
    [
        ({'minimum_goal_distance': -1.0}, 'non-negative'),
        ({'maximum_goal_distance': 1.0}, 'exceed minimum'),
        ({'preferred_goal_distance': 11.0}, 'in bounds'),
        ({'distance_scale': 0.0}, 'positive'),
        ({'peripheral_clearance': -0.1}, 'non-negative'),
        ({'open_clearance': 0.7}, 'exceed peripheral'),
        ({'peripheral_probability': 1.1}, r'\[0, 1\]'),
        ({'recent_memory_size': 0}, 'positive'),
        ({'top_k': 0}, 'positive'),
        ({'temperature': float('nan')}, 'finite'),
    ],
)
def test_invalid_policy_configuration_is_rejected(overrides, message):
    with pytest.raises(ValueError, match=message):
        _config(**overrides).validate()


def test_goal_distance_bounds_are_enforced_inclusively():
    """Only candidates inside the configured travel annulus are eligible."""
    policy = RoamingPolicy(_config(), random_seed=87)
    current = Point2D(0.0, 0.0)
    candidates = (
        _candidate(1, 0.9, 1.2),
        _candidate(2, 1.0, 1.2),
        _candidate(3, 10.0, 1.2),
        _candidate(4, 10.1, 1.2),
    )
    selected, _mode = policy.select(candidates, current, 10.0)
    assert selected.key in {(2, 0), (3, 0)}


def test_recent_success_yields_to_an_equally_useful_unvisited_region():
    """A reached goal must not be selected repeatedly while alternatives exist."""
    policy = RoamingPolicy(_config(), random_seed=87)
    candidates = (
        _candidate(1, 3.0, 1.2),
        _candidate(2, 4.0, 1.2),
        _candidate(3, 5.0, 1.2),
    )
    first, mode = policy.select(candidates, Point2D(0.0, 0.0), 10.0)
    assert mode == 'open'
    policy.record_success(first, 10.0)
    second, _mode = policy.select(candidates, Point2D(0.0, 0.0), 11.0)
    assert second.key != first.key


def test_peripheral_mode_and_mode_fallback_use_only_safe_candidates():
    """Corner-like visits are explicit and missing classes fall back cleanly."""
    peripheral = _candidate(1, 3.0, 0.6)
    open_space = _candidate(2, 4.0, 1.4)
    policy = RoamingPolicy(
        _config(peripheral_probability=1.0),
        random_seed=87,
    )
    selected, mode = policy.select(
        (peripheral, open_space),
        Point2D(0.0, 0.0),
        10.0,
    )
    assert mode == 'peripheral'
    assert selected == peripheral

    open_only = RoamingPolicy(
        _config(peripheral_probability=1.0),
        random_seed=87,
    )
    selected, mode = open_only.select(
        (open_space,),
        Point2D(0.0, 0.0),
        10.0,
    )
    assert mode == 'open'
    assert selected == open_space


def test_failed_goal_is_suppressed_only_until_its_cooldown_expires():
    """An unreachable sample must not loop forever or stay banned forever."""
    config = _config(failure_cooldown_seconds=30.0)
    policy = RoamingPolicy(config, random_seed=87)
    failed = _candidate(1, 4.0, 1.2)
    alternative = _candidate(2, 7.0, 1.2)
    current = Point2D(0.0, 0.0)
    policy.record_failure(failed, 10.0)

    selected, _mode = policy.select((failed, alternative), current, 39.9)
    assert selected == alternative
    selected, _mode = policy.select((failed,), current, 40.0)
    assert selected == failed


def test_no_eligible_destination_returns_none():
    policy = RoamingPolicy(_config(), random_seed=87)
    assert policy.select(
        (_candidate(1, 0.2, 1.2),),
        Point2D(0.0, 0.0),
        0.0,
    ) is None
