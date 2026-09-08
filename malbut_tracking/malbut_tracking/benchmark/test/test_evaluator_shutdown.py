"""Regressions for benchmark completion without executor self-deadlock."""

import io
import json
from types import SimpleNamespace

from malbut_tracking.benchmark import evaluator


def test_finalize_writes_results_without_shutting_down_inside_callback(tmp_path, monkeypatch):
    """Completing a sample must return so the main loop can stop the executor."""
    node = evaluator.PersonTrackingBenchmark.__new__(evaluator.PersonTrackingBenchmark)
    node._finalized = False
    node._measurement_start_s = 5.0
    node._measurement_end_s = 25.0
    node._measurement_duration_s = 20.0
    node._now_seconds = lambda: 25.0
    canceled = []
    node._action_goal_handle = SimpleNamespace(cancel_goal_async=lambda: canceled.append(True))
    node._prediction_sample_count = 2
    node._prediction_error_sum = 0.4
    node._scenario_name = 'test'
    node._world_name = 'arena'
    node._trajectory_name = 'loop'
    node._desired_distance_m = 1.0
    node._path_progress_m = 2.0
    node._collision_count = 0
    node._sample_count = 2
    node._distance_error_sum = 0.6
    node._tracking_valid_count = 2
    node._prediction_outside_count = 0
    node._latencies_ms = [10.0, 20.0]
    node._latencies_by_source_ms = {'camera': [10.0, 20.0]}
    node._result_directory = tmp_path
    node._sample_stream = io.StringIO()
    node._event_stream = io.StringIO()
    node.get_logger = lambda: SimpleNamespace(info=lambda _: None)
    shutdown_calls = []
    monkeypatch.setattr(evaluator.rclpy, 'ok', lambda: True)
    monkeypatch.setattr(evaluator.rclpy, 'shutdown', lambda: shutdown_calls.append(True))

    node._finalize('COMPLETED', '20-second measurement completed')
    assert node._finalized
    assert not shutdown_calls
    assert canceled == [True]
    assert node._sample_stream.closed
    assert node._event_stream.closed
    summary = json.loads((tmp_path / 'summary.json').read_text())
    assert summary['status'] == 'COMPLETED'
    assert summary['scenario']['duration_s'] == 20.0
    assert summary['sample_count'] == 2
    node._finalize('INTERRUPTED', 'must not overwrite completed result')
    assert canceled == [True]
    assert not shutdown_calls


def test_main_exits_after_final_callback_returns_then_shuts_down(monkeypatch):
    """No further spin is needed once finalization has returned to main."""
    events = []
    node = SimpleNamespace(
        _finalized=False,
        destroy_node=lambda: events.append('destroy'),
    )
    monkeypatch.setattr(evaluator, 'PersonTrackingBenchmark', lambda: node)
    monkeypatch.setattr(evaluator.rclpy, 'init', lambda **_: events.append('init'))
    monkeypatch.setattr(evaluator.rclpy, 'ok', lambda: True)

    def spin_once(current):
        assert current is node
        assert not current._finalized
        events.append('callback_start')
        current._finalized = True
        events.append('callback_return')

    def shutdown():
        assert events[-1] == 'destroy'
        events.append('shutdown')

    monkeypatch.setattr(evaluator.rclpy, 'spin_once', spin_once)
    monkeypatch.setattr(evaluator.rclpy, 'shutdown', shutdown)
    evaluator.main()
    assert events == ['init', 'callback_start', 'callback_return', 'destroy', 'shutdown']
