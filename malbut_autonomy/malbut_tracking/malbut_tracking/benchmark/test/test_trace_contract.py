"""Cross-package contracts for actual end-to-end latency measurement."""

from pathlib import Path


AUTONOMY_ROOT = Path(__file__).resolve().parents[4]


def test_trace_interfaces_carry_exact_ros_and_monotonic_timestamps():
    interface_root = AUTONOMY_ROOT / 'malbut_interfaces' / 'msg'
    sensor = (interface_root / 'SensorProcessingTrace.msg').read_text(
        encoding='utf-8'
    )
    command = (interface_root / 'TrackingCommandTrace.msg').read_text(
        encoding='utf-8'
    )
    assert 'builtin_interfaces/Time source_stamp' in sensor
    assert 'uint64 receipt_steady_time_ns' in sensor
    assert 'uint64 publish_steady_time_ns' in sensor
    assert 'uint64 planning_started_steady_time_ns' in command
    assert 'uint64 planning_finished_steady_time_ns' in command
    assert 'uint64 dispatch_steady_time_ns' in command


def test_sensor_front_ends_stamp_the_same_linux_monotonic_clock():
    perception = (
        AUTONOMY_ROOT
        / 'malbut_perception'
        / 'malbut_perception'
        / 'target_localizer_node.py'
    ).read_text(encoding='utf-8')
    lidar = (
        AUTONOMY_ROOT
        / 'malbut_lidar_preprocessor'
        / 'src'
        / 'lidar_foreground_preprocessor.cpp'
    ).read_text(encoding='utf-8')
    assert 'time.clock_gettime_ns(' in perception
    assert 'time.CLOCK_MONOTONIC' in perception
    assert "trace.source = 'camera'" in perception
    assert 'clock_gettime(CLOCK_MONOTONIC' in lidar
    assert 'trace.source = "lidar"' in lidar


def test_evaluator_uses_wall_clock_and_exact_observation_deduplication():
    evaluator = (
        AUTONOMY_ROOT
        / 'malbut_tracking'
        / 'malbut_tracking'
        / 'benchmark'
        / 'evaluator.py'
    ).read_text(encoding='utf-8')
    assert 'dispatch_ns - receipt_ns' in evaluator
    assert '_stamp_key(message.source_stamp)' in evaluator
    assert 'WallClockTraceCorrelator' in evaluator
    assert 'dispatch_s - source_s' not in evaluator
