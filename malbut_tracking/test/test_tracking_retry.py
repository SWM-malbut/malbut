"""Check bounded retry backoff with real policy methods and no motion server."""

from concurrent.futures import Future
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path
import pytest

from malbut_tracking import navigation
from malbut_tracking.follow_policy import FollowCommand, FollowSettings
from malbut_tracking.geometry import Point2D
from malbut_tracking.navigation import MotionMode, Nav2PathClient
from malbut_tracking.person_follower_node import FollowState, PersonFollowerNode


def _follower():
    parameters = {
        'observation_loss_debounce_s': 0.75,
        'sensor_transform_queue_timeout_s': 0.30,
        'approach_prediction_horizon_s': 0.75,
        'approach_speed_threshold_mps': 0.10,
    }
    node = SimpleNamespace(
        _settings=FollowSettings(1.0, 0.2, 0.1, 0.75),
        _active_goal=object(), _state=FollowState.TRACKING,
        _tracking_source='camera', _lidar_proximity_guard_until_s=0.0,
        _last_motion_target=Point2D(3.0, 0.0),
        _last_motion_command=FollowCommand.NAVIGATE,
        _last_motion_velocity=None, _last_motion_bearing_only=False,
        _last_motion_source_stamp_ns=20_000_000_000,
        _motion_generation=1,
        _tracking_retry_pending=False, _tracking_retry_context=None,
        _tracking_retry_timer=Mock(), _latest_global_costmap=None,
        _tracking_plan_timer=Mock(), _tracking_plan_pending=False,
        _next_tracking_plan_ns=0, _line_fallback_immediate=False,
        _static_job_context=None, _line_fallback_pending=False,
        _nav2=Mock(mode=MotionMode.NAVIGATE), _path_planner=Mock(busy=False),
        _request_tracking_path=Mock(), _request_line_fallback=Mock(),
        _align_with_target=Mock(), _publish_track_markers=Mock(),
        _warn_periodically=Mock(),
        _robot_pose=Mock(return_value=(Point2D(0.0, 0.0), 0.0)),
        _now_seconds=Mock(return_value=20.0),
        get_parameter=lambda key: SimpleNamespace(value=parameters[key]),
    )
    for name in (
        '_observation_is_current', '_apply_tracking_motion',
        '_schedule_tracking_navigation_retry', '_on_tracking_retry_timer',
        '_reset_tracking_plan_cadence', '_tracking_plan_due',
        '_on_tracking_plan_timer', '_plan_latest_observation_if_pending',
        '_set_state',
    ):
        setattr(node, name, MethodType(getattr(PersonFollowerNode, name), node))
    node._cancel_tracking_retry = Mock(
        side_effect=MethodType(PersonFollowerNode._cancel_tracking_retry, node),
    )
    node._schedule_tracking_navigation_retry()
    return node


def _cadence_follower(monkeypatch):
    node = _follower()
    node._cancel_tracking_retry()
    node._cancel_tracking_retry.reset_mock()
    node.get_logger = Mock()
    node._publish_status = Mock()
    steady_ns = [10_000_000_000]
    monkeypatch.setattr(
        'malbut_tracking.person_follower_node._monotonic_nanoseconds',
        lambda: steady_ns[0],
    )

    def advance(seconds):
        steady_ns[0] = 10_000_000_000 + round(seconds * 1_000_000_000)
        node._now_seconds.return_value = 20.0 + seconds

    return node, advance


def _observe(node, x=3.0, *, robot_x=0.0, now=20.0, stamp_ns=20_000_000_000):
    node._apply_tracking_motion(
        Point2D(robot_x, 0.0), Point2D(x, 0.0), now,
        source_stamp_ns=stamp_ns,
    )


def test_same_and_jittering_observations_keep_first_retry_deadline_and_baseline():
    """New images update data without restarting identical failed work."""
    node = _follower()
    baseline = node._tracking_retry_context
    for index, x in enumerate((3.0, 3.02, 2.99, 3.04, 3.06)):
        _observe(node, x, stamp_ns=20_000_000_000 + index)
    assert node._tracking_retry_pending
    assert node._tracking_retry_context is baseline
    assert baseline == (Point2D(3.0, 0.0), FollowCommand.NAVIGATE)
    assert node._last_motion_target == Point2D(3.06, 0.0)
    assert node._last_motion_source_stamp_ns == 20_000_000_004
    node._tracking_retry_timer.reset.assert_called_once_with()
    node._cancel_tracking_retry.assert_not_called()
    node._request_tracking_path.assert_not_called()


def test_small_changes_accumulate_against_failed_target_until_retry_is_useful():
    """The baseline stays fixed so slow motion eventually releases backoff."""
    node = _follower()
    for x in (3.03, 3.06, 3.09):
        _observe(node, x)
    node._request_tracking_path.assert_not_called()
    _observe(node, 3.12)
    assert not node._tracking_retry_pending
    assert node._tracking_retry_context is None
    node._cancel_tracking_retry.assert_called_once_with()
    assert node._request_tracking_path.call_args.args[1] == Point2D(3.12, 0.0)


def test_meaningful_change_respects_costmap_cell_size():
    """Sub-cell noise cannot defeat backoff on a coarse map."""
    node = _follower()
    node._latest_global_costmap = SimpleNamespace(resolution=0.2)
    node._line_fallback_pending = True
    _observe(node, 3.15)
    node._request_line_fallback.assert_not_called()
    _observe(node, 3.21)
    node._request_line_fallback.assert_called_once()
    assert not node._tracking_retry_pending


@pytest.mark.parametrize('robot_x,command', [
    (3.0, FollowCommand.HOLD),
    (2.0, FollowCommand.ALIGN),
    (2.5, FollowCommand.RETREAT),
])
def test_hold_alignment_and_retreat_do_not_wait_for_forward_retry(robot_x, command):
    """A safety/distance-band change bypasses even an unchanged target's delay."""
    node = _follower()
    _observe(node, robot_x=robot_x)
    assert node._last_motion_command == command
    assert not node._tracking_retry_pending
    node._cancel_tracking_retry.assert_called_once_with()
    if command == FollowCommand.HOLD:
        node._nav2.cancel.assert_called_once_with()
        node._request_tracking_path.assert_not_called()
    elif command == FollowCommand.ALIGN:
        node._align_with_target.assert_called_once()
        node._request_tracking_path.assert_not_called()
    else:
        assert node._request_tracking_path.call_args.args[2].command == command


def test_retry_timer_uses_latest_observation_not_original_failure_snapshot():
    """The deadline is fixed, but the eventual plan uses fresh target data."""
    node = _follower()
    _observe(node, 3.06, now=20.2, stamp_ns=20_200_000_000)
    generation = node._motion_generation
    node._now_seconds.return_value = 20.3
    node._on_tracking_retry_timer()
    node._request_tracking_path.assert_called_once()
    args = node._request_tracking_path.call_args.args
    assert args[1] == Point2D(3.06, 0.0)
    assert args[6] == 20_200_000_000
    assert node._motion_generation == generation  # Timer is not a new measurement.
    assert not node._tracking_retry_pending


def test_repeated_failure_does_not_extend_deadline_or_replace_baseline():
    """Several failure callbacks share one retry instead of perpetually postponing it."""
    node = _follower()
    first_context = node._tracking_retry_context
    node._last_motion_target = Point2D(3.04, 0.0)
    node._schedule_tracking_navigation_retry()
    node._schedule_tracking_navigation_retry()
    node._tracking_retry_timer.reset.assert_called_once_with()
    assert node._tracking_retry_context is first_context


def test_stale_changed_observation_does_not_release_backoff_or_overwrite_latest():
    """An old image is not a meaningful target change despite large displacement."""
    node = _follower()
    _observe(node, 5.0, stamp_ns=19_000_000_000)
    assert node._tracking_retry_pending
    assert node._last_motion_target == Point2D(3.0, 0.0)
    node._cancel_tracking_retry.assert_not_called()
    node._request_tracking_path.assert_not_called()


def test_expired_latest_observation_is_not_replayed_by_retry_timer():
    """The backoff timer does not bypass the normal capture-age safety check."""
    node = _follower()
    node._now_seconds.return_value = 21.0
    node._on_tracking_retry_timer()
    assert not node._tracking_retry_pending
    node._request_tracking_path.assert_not_called()


def test_already_canceled_timer_callback_does_not_replay_observation():
    """A queued callback after cancellation is not permission for a new plan."""
    node = _follower()
    node._cancel_tracking_retry()
    node._cancel_tracking_retry.reset_mock()
    node._on_tracking_retry_timer()
    node._robot_pose.assert_not_called()
    node._request_tracking_path.assert_not_called()
    node._cancel_tracking_retry.assert_not_called()


def test_planning_slot_keeps_latest_input_without_postponing_its_deadline(monkeypatch):
    """Rapid accepted images coalesce at 5 Hz, without stale replay or a queue."""
    node, advance = _cadence_follower(monkeypatch)
    _observe(node)
    node._request_tracking_path.assert_called_once()  # First target is immediate.
    assert node._next_tracking_plan_ns == 10_200_000_000
    for offset in (0.04, 0.08, 0.12, 0.16, 0.19):
        advance(offset)
        _observe(node, 3.0 + offset, now=20.0 + offset,
                 stamp_ns=20_000_000_000 + round(offset * 1e9))
    node._request_tracking_path.assert_called_once()
    assert node._motion_generation == 7  # All six observations were accepted.
    node._tracking_plan_timer.reset.assert_called_once_with()
    assert node._tracking_plan_timer.timer_period_ns == 160_000_000
    assert node._next_tracking_plan_ns == 10_200_000_000
    advance(0.20)
    node._robot_pose.return_value = (Point2D(0.2, 0.0), 0.0)
    node._on_tracking_plan_timer()
    assert node._request_tracking_path.call_count == 2
    args = node._request_tracking_path.call_args.args
    assert args[0] == Point2D(0.2, 0.0)  # Fresh robot TF at the planning slot.
    assert args[1] == Point2D(3.19, 0.0)
    assert args[6] == 20_190_000_000  # Original capture timestamp is preserved.
    assert node._motion_generation == 7
    assert not node._tracking_plan_pending
    advance(1.0)
    node._on_tracking_plan_timer()  # A canceled/queued callback cannot poll.
    assert node._request_tracking_path.call_count == 2


def test_fast_completed_plan_waits_for_slot_but_slow_plan_has_no_catchup(monkeypatch):
    """Completion chains share the gate and never create parallel planner work."""
    node, advance = _cadence_follower(monkeypatch)
    _observe(node)
    completed_generation = node._motion_generation
    node._path_planner.busy = True
    for offset in (0.01, 0.02, 0.03):
        advance(offset)
        _observe(node, 3.0 + offset)
    assert not node._tracking_plan_pending  # In-flight job owns its completion.
    node._request_tracking_path.assert_called_once()
    advance(0.05)
    node._path_planner.busy = False
    node._plan_latest_observation_if_pending(completed_generation)
    assert node._tracking_plan_pending
    node._request_tracking_path.assert_called_once()
    advance(0.2)
    node._on_tracking_plan_timer()
    assert node._request_tracking_path.call_count == 2
    completed_generation = node._motion_generation
    node._path_planner.busy = True
    advance(0.8)
    _observe(node, 3.2, now=20.8, stamp_ns=20_800_000_000)
    node._path_planner.busy = False
    node._plan_latest_observation_if_pending(completed_generation)
    assert node._request_tracking_path.call_count == 3
    assert node._next_tracking_plan_ns == 11_000_000_000  # Now + .2, not catchup.
    assert not node._tracking_plan_pending


def test_static_search_cycles_share_the_same_cadence(monkeypatch):
    """The budget gate runs before the static worker, not only before Nav2 calls."""
    node, advance = _cadence_follower(monkeypatch)
    original_parameter = node.get_parameter
    parameters = {'static_occupied_threshold': 50, 'static_planning_budget_s': 0.02}
    node.get_parameter = lambda key: (
        SimpleNamespace(value=parameters[key]) if key in parameters
        else original_parameter(key)
    )
    node._latest_global_costmap = SimpleNamespace(resolution=0.05)
    node._latest_static_map = object()
    node._static_job = None
    node._static_worker = Mock()
    node._compute_static_path = Mock()
    node._wake_static_plan = Mock()
    _observe(node)
    node._static_worker.submit.assert_called_once()
    first = node._static_job
    advance(0.03)
    _observe(node, 3.03)
    assert node._static_job is first
    PersonFollowerNode._on_static_plan_ready(node)
    node._request_tracking_path.assert_called_once()
    advance(0.04)
    _observe(node, 3.04)
    assert node._static_job is None
    node._static_worker.submit.assert_called_once()
    assert node._tracking_plan_pending
    advance(0.2)
    node._on_tracking_plan_timer()
    assert node._static_worker.submit.call_count == 2
    assert node._static_worker.submit.call_args.args[3] == Point2D(3.04, 0.0)


@pytest.mark.parametrize('robot_x,command', [
    (3.0, FollowCommand.HOLD), (2.0, FollowCommand.ALIGN),
    (2.5, FollowCommand.RETREAT),
])
def test_motion_regime_changes_bypass_slot_and_clear_deferred_plan(
    monkeypatch, robot_x, command,
):
    """Distance-band changes react now, never after an obsolete forward slot."""
    node, advance = _cadence_follower(monkeypatch)
    _observe(node)
    advance(0.02)
    _observe(node)
    assert node._tracking_plan_pending
    advance(0.03)
    _observe(node, robot_x=robot_x)
    assert node._last_motion_command == command
    assert not node._tracking_plan_pending
    if command == FollowCommand.HOLD:
        node._nav2.cancel.assert_called_once()
    elif command == FollowCommand.ALIGN:
        node._align_with_target.assert_called_once()
    else:
        assert node._request_tracking_path.call_count == 2
    calls = node._request_tracking_path.call_count
    advance(0.2)
    node._on_tracking_plan_timer()
    assert node._request_tracking_path.call_count == calls


def test_pending_slot_drops_expired_source_without_extending_sensor_age(monkeypatch):
    """A valid but old observation can expire while waiting for a planning slot."""
    node, advance = _cadence_follower(monkeypatch)
    _observe(node)
    advance(0.05)
    _observe(node, now=20.05, stamp_ns=19_310_000_000)
    assert node._tracking_plan_pending
    advance(0.2)
    node._on_tracking_plan_timer()
    node._request_tracking_path.assert_called_once()
    assert not node._tracking_plan_pending
    assert node._last_motion_source_stamp_ns == 19_310_000_000


@pytest.mark.parametrize('state', [FollowState.STOPPED, FollowState.RECOVERING])
def test_cancel_or_recovery_discards_pending_plan_and_reacquires_immediately(
    monkeypatch, state,
):
    """No deferred tracking plan survives a state transition."""
    node, advance = _cadence_follower(monkeypatch)
    _observe(node)
    advance(0.02)
    _observe(node)
    node._set_state(state)
    assert not node._tracking_plan_pending
    assert node._next_tracking_plan_ns == 0
    node._on_tracking_plan_timer()
    node._request_tracking_path.assert_called_once()
    if state == FollowState.RECOVERING:
        node._set_state(FollowState.TRACKING)
        _observe(node)
        assert node._request_tracking_path.call_count == 2


def test_failed_cycle_fallback_is_immediate_but_replacements_are_limited(monkeypatch):
    """The same cycle can try its local alternative without waiting 200 ms."""
    node, advance = _cadence_follower(monkeypatch)
    _observe(node)
    node._path_planner.busy = True  # Timed-out job still drains on Nav2.
    node._line_fallback_pending = True
    node._line_fallback_immediate = True
    advance(0.05)
    node._plan_latest_observation_if_pending(-1)
    node._request_line_fallback.assert_called_once()
    assert not node._line_fallback_immediate
    assert node._next_tracking_plan_ns == 10_200_000_000
    advance(0.06)
    _observe(node, 3.1)
    node._request_line_fallback.assert_called_once()
    assert node._tracking_plan_pending
    advance(0.2)
    node._on_tracking_plan_timer()
    assert node._request_line_fallback.call_count == 2
    node._request_tracking_path.assert_called_once()  # No second owned compute.


@pytest.mark.parametrize('previous,x,latest_x,command', [
    (FollowCommand.NAVIGATE, 0.6, 0.65, FollowCommand.RETREAT),
    (FollowCommand.RETREAT, 3.0, 3.1, FollowCommand.NAVIGATE),
])
def test_direction_reversal_invalidates_inflight_plan_and_stops_old_motion(
    monkeypatch, previous, x, latest_x, command,
):
    """A late successful old plan cannot undo a newer direction decision."""
    node, _ = _cadence_follower(monkeypatch)
    node._last_motion_command = previous
    node._planning_shutdown = False
    node._static_job_context = object()
    node._line_fallback_pending = True
    response, result = Future(), Future()
    client = Mock()
    client.send_goal_async.return_value = response
    monkeypatch.setattr(navigation, 'ActionClient', lambda *args: client)
    node._path_planner = Nav2PathClient(
        node, 'compute_path',
        on_idle=lambda: PersonFollowerNode._on_path_planner_idle(node),
    )
    old_callback = Mock()
    assert node._path_planner.compute(PoseStamped(), 'GridBased', old_callback)
    handle = Mock(accepted=True)
    handle.get_result_async.return_value = result
    response.set_result(handle)

    _observe(node, x)
    assert node._last_motion_command == command
    assert node._static_job_context is None
    assert not node._line_fallback_pending
    node._nav2.cancel.assert_called_once()
    handle.cancel_goal_async.assert_called_once()
    assert node._path_planner.busy  # Cancellation request is not completion.
    node._request_tracking_path.assert_not_called()
    _observe(node, latest_x)
    node._nav2.cancel.assert_called_once()  # Same direction does not cancel again.

    result.set_result(SimpleNamespace(
        status=GoalStatus.STATUS_SUCCEEDED, result=SimpleNamespace(path=Path()),
    ))
    old_callback.assert_not_called()
    assert not node._path_planner.busy
    node._request_tracking_path.assert_called_once()
    args = node._request_tracking_path.call_args.args
    assert args[1] == Point2D(latest_x, 0.0)
    assert args[2].command == command


def test_same_direction_keeps_current_motion_and_inflight_plan(monkeypatch):
    """Normal forward refreshes do not repeatedly stop the robot."""
    node, _ = _cadence_follower(monkeypatch)
    node._path_planner.busy = True
    for x in (3.0, 3.1, 3.2):
        _observe(node, x)
    node._path_planner.cancel.assert_not_called()
    node._nav2.cancel.assert_not_called()
    node._request_tracking_path.assert_not_called()


def test_bearing_only_close_observation_stops_an_existing_retreat(monkeypatch):
    """Uncertain RGB-only depth must not keep a previous reverse path moving."""
    node, _ = _cadence_follower(monkeypatch)
    node._last_motion_command = FollowCommand.RETREAT
    node._apply_tracking_motion(
        Point2D(0.0, 0.0), Point2D(0.6, 0.0), 20.0,
        bearing_only=True, source_stamp_ns=20_000_000_000,
    )
    node._path_planner.cancel.assert_called_once()
    node._nav2.cancel.assert_called_once()
    node._request_tracking_path.assert_not_called()
    assert not node._tracking_plan_pending
