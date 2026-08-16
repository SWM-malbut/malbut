"""Tests for patrol progress independent of ROS and Nav2."""

import pytest

from malbut_patrol.patrol_state import (
    PatrolCommand,
    PatrolProgress,
    PatrolState,
)
from malbut_patrol.route_loader import PatrolRoute, Schedule, Waypoint


def _waypoint(
    name,
    *,
    dwell=0.0,
    retries=0,
    failure='abort',
):
    return Waypoint(
        name=name,
        x=0.0,
        y=0.0,
        yaw=0.0,
        dwell_seconds=dwell,
        max_retries=retries,
        retry_backoff_seconds=2.0,
        on_failure=failure,
    )


def _route(
    *waypoints,
    cycles=1,
    schedule_mode='manual',
    interval=0.0,
):
    return PatrolRoute(
        name='test',
        map_id='test_map',
        frame_id='map',
        cycles_per_run=cycles,
        waypoints=tuple(waypoints),
        schedule=Schedule(
            mode=schedule_mode,
            interval_seconds=interval,
        ),
    )


def test_happy_path_honors_dwell_and_multiple_cycles():
    progress = PatrolProgress(
        _route(
            _waypoint('first', dwell=3.0),
            _waypoint('second'),
            cycles=2,
        )
    )

    assert progress.start().command == PatrolCommand.SEND_GOAL
    dwell = progress.goal_succeeded()
    assert dwell.command == PatrolCommand.START_DWELL
    assert dwell.duration_seconds == 3.0
    assert progress.state == PatrolState.DWELLING

    assert progress.dwell_elapsed().command == PatrolCommand.SEND_GOAL
    assert progress.current_waypoint.name == 'second'
    assert progress.goal_succeeded().command == PatrolCommand.SEND_GOAL
    assert progress.current_waypoint.name == 'first'
    assert progress.run_cycles_completed == 1

    progress.goal_succeeded()
    progress.dwell_elapsed()
    completed = progress.goal_succeeded()
    assert completed.command == PatrolCommand.NONE
    assert progress.state == PatrolState.COMPLETED
    assert progress.total_cycles_completed == 2


def test_retry_then_skip_advances_without_aborting():
    progress = PatrolProgress(
        _route(
            _waypoint('first', retries=1, failure='skip'),
            _waypoint('second'),
        )
    )
    progress.start()

    retry = progress.goal_failed('blocked')
    assert retry.command == PatrolCommand.START_RETRY_WAIT
    assert progress.current_retries == 1
    assert progress.retry_wait_elapsed().command == PatrolCommand.SEND_GOAL

    skipped = progress.goal_failed('still blocked')
    assert skipped.command == PatrolCommand.SEND_GOAL
    assert progress.current_waypoint.name == 'second'
    assert progress.current_retries == 0


def test_exhausted_abort_policy_aborts_run():
    progress = PatrolProgress(
        _route(_waypoint('only', failure='abort'))
    )
    progress.start()

    transition = progress.goal_failed('unreachable')

    assert transition.command == PatrolCommand.NONE
    assert progress.state == PatrolState.ABORTED
    assert not progress.is_active


def test_interval_mode_waits_then_starts_a_new_run():
    progress = PatrolProgress(
        _route(
            _waypoint('only'),
            schedule_mode='interval',
            interval=60.0,
        )
    )
    progress.start()

    wait = progress.goal_succeeded()
    assert wait.command == PatrolCommand.START_INTERVAL_WAIT
    assert wait.duration_seconds == 60.0
    assert progress.state == PatrolState.INTERVAL_WAIT
    assert progress.total_cycles_completed == 1

    next_run = progress.interval_elapsed()
    assert next_run.command == PatrolCommand.SEND_GOAL
    assert progress.state == PatrolState.NAVIGATING
    assert progress.run_cycles_completed == 0


def test_pause_navigation_waits_for_terminal_then_resends_same_waypoint():
    progress = PatrolProgress(
        _route(_waypoint('first'), _waypoint('second'))
    )
    progress.start()

    paused = progress.pause()
    assert paused.command == PatrolCommand.CANCEL_GOAL
    assert progress.state == PatrolState.PAUSING
    assert progress.current_waypoint.name == 'first'

    settled = progress.cancellation_finished(goal_succeeded=False)
    assert settled.command == PatrolCommand.NONE
    assert progress.state == PatrolState.PAUSED

    resumed = progress.resume()
    assert resumed.command == PatrolCommand.SEND_GOAL
    assert progress.state == PatrolState.NAVIGATING
    assert progress.current_waypoint.name == 'first'
    assert progress.current_retries == 0


def test_pause_dwell_restarts_dwell_without_resending_goal():
    progress = PatrolProgress(
        _route(_waypoint('only', dwell=4.0))
    )
    progress.start()
    progress.goal_succeeded()

    assert progress.pause().command == PatrolCommand.NONE
    resumed = progress.resume()

    assert resumed.command == PatrolCommand.START_DWELL
    assert resumed.duration_seconds == 4.0
    assert progress.state == PatrolState.DWELLING


def test_goal_success_during_pause_is_checkpointed_before_resuming():
    progress = PatrolProgress(
        _route(_waypoint('first'), _waypoint('second'))
    )
    progress.start()
    progress.pause()

    settled = progress.cancellation_finished(goal_succeeded=True)

    assert settled.command == PatrolCommand.NONE
    assert progress.state == PatrolState.PAUSED
    assert progress.current_waypoint.name == 'second'
    assert progress.resume().command == PatrolCommand.SEND_GOAL


def test_stop_navigation_waits_for_terminal_result():
    progress = PatrolProgress(_route(_waypoint('only')))
    progress.start()

    stopping = progress.stop()

    assert stopping.command == PatrolCommand.CANCEL_GOAL
    assert progress.state == PatrolState.STOPPING
    with pytest.raises(RuntimeError, match='already stopping'):
        progress.stop()

    stopped = progress.cancellation_finished(goal_succeeded=False)
    assert stopped.command == PatrolCommand.NONE
    assert progress.state == PatrolState.IDLE


def test_stop_while_pausing_uses_existing_cancellation():
    progress = PatrolProgress(_route(_waypoint('only')))
    progress.start()
    progress.pause()

    stopping = progress.stop()

    assert stopping.command == PatrolCommand.NONE
    assert progress.state == PatrolState.STOPPING
    progress.cancellation_finished(goal_succeeded=False)
    assert progress.state == PatrolState.IDLE


def test_unexpected_external_cancel_aborts_without_retry():
    progress = PatrolProgress(
        _route(_waypoint('only', retries=2, failure='skip'))
    )
    progress.start()

    canceled = progress.goal_canceled('operator canceled Nav2 goal')

    assert canceled.command == PatrolCommand.NONE
    assert progress.state == PatrolState.ABORTED
    assert progress.current_retries == 0


def test_stop_disarms_interval_repetition():
    progress = PatrolProgress(
        _route(
            _waypoint('only'),
            schedule_mode='interval',
            interval=10.0,
        )
    )
    progress.start()
    progress.goal_succeeded()
    assert progress.state == PatrolState.INTERVAL_WAIT

    stopped = progress.stop()

    assert stopped.command == PatrolCommand.NONE
    assert progress.state == PatrolState.IDLE
    assert not progress.is_active


def test_invalid_transitions_are_rejected():
    progress = PatrolProgress(_route(_waypoint('only')))

    with pytest.raises(RuntimeError, match='cannot pause'):
        progress.pause()
    with pytest.raises(RuntimeError, match='cannot resume'):
        progress.resume()
