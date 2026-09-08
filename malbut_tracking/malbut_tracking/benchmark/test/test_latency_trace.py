"""Tests for exact, wall-clock benchmark trace correlation."""

from malbut_tracking.benchmark.evaluator import (
    CommandTraceTiming,
    SensorTraceTiming,
    WallClockTraceCorrelator,
    _latency_summary,
    _normalized_sensor_source,
    _valid_command_timing,
)


def _sensor(receipt=1_000, published=1_500):
    return SensorTraceTiming(receipt, published, sequence=7)


def _command(dispatch=3_000, source='camera'):
    return CommandTraceTiming(
        planning_started_steady_time_ns=1_700,
        planning_finished_steady_time_ns=2_900,
        dispatch_steady_time_ns=dispatch,
        dispatch_sim_time_s=42.0,
        sequence=11,
        source=source,
    )


def test_fused_follower_sources_preserve_the_owning_sensor_stamp():
    assert _normalized_sensor_source('camera') == 'camera'
    assert _normalized_sensor_source('camera_lidar') == 'camera'
    assert _normalized_sensor_source('bearing') == 'camera'
    assert _normalized_sensor_source('lidar') == 'lidar'
    assert _normalized_sensor_source('lidar_proximity') == 'lidar'
    assert _normalized_sensor_source('last_seen_recovery') is None


def test_trace_correlation_accepts_either_cross_process_arrival_order():
    stamp = (12, 345)
    sensor_first = WallClockTraceCorrelator()
    assert sensor_first.add_sensor('camera', stamp, _sensor()) is None
    match = sensor_first.add_command('camera_lidar', stamp, _command())
    assert match == (('camera', *stamp), _sensor(), _command())

    command_first = WallClockTraceCorrelator()
    assert command_first.add_command(
        'lidar', stamp, _command(source='lidar')
    ) is None
    match = command_first.add_sensor('lidar', stamp, _sensor())
    assert match == (
        ('lidar', *stamp),
        _sensor(),
        _command(source='lidar'),
    )


def test_only_first_navigation_command_is_measured_per_observation():
    correlator = WallClockTraceCorrelator()
    stamp = (19, 27)
    correlator.add_sensor('camera', stamp, _sensor())
    first = correlator.add_command('camera', stamp, _command(dispatch=3_000))
    assert first is not None
    assert correlator.add_command(
        'camera', stamp, _command(dispatch=4_000)
    ) is None
    assert correlator.add_sensor('camera', stamp, _sensor()) is None


def test_pending_duplicate_keeps_earliest_dispatch():
    correlator = WallClockTraceCorrelator()
    stamp = (23, 99)
    correlator.add_command('camera', stamp, _command(dispatch=4_000))
    correlator.add_command('camera', stamp, _command(dispatch=3_000))
    match = correlator.add_sensor('camera', stamp, _sensor())
    assert match is not None
    assert match[2].dispatch_steady_time_ns == 3_000


def test_impossible_monotonic_planner_stage_order_is_rejected():
    """A planner result cannot finish after its command was dispatched."""
    correlator = WallClockTraceCorrelator()
    stamp = (31, 41)
    invalid = CommandTraceTiming(
        planning_started_steady_time_ns=1_700,
        planning_finished_steady_time_ns=3_100,
        dispatch_steady_time_ns=3_000,
        dispatch_sim_time_s=42.0,
        sequence=11,
        source='camera',
    )
    assert not _valid_command_timing(invalid)
    assert correlator.add_command('camera', stamp, invalid) is None
    assert correlator.add_sensor('camera', stamp, _sensor()) is None

    valid = _command()
    match = correlator.add_command('camera', stamp, valid)
    assert match == (('camera', *stamp), _sensor(), valid)


def test_latency_summary_keeps_combined_and_per_source_shape_stable():
    assert _latency_summary([]) == {
        'median': None,
        'p95': None,
        'max': None,
        'sample_count': 0,
    }
    assert _latency_summary([1.0, 3.0, 2.0]) == {
        'median': 2.0,
        'p95': 3.0,
        'max': 3.0,
        'sample_count': 3,
    }
