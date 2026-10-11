"""All active follower regimes refresh without parallel jobs or stale motion."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_tracking.geometry import Point2D
from malbut_tracking.person_follower_node import (
    FollowState, PersonFollowerNode, RecoveryPhase,
)


def _follower(state=FollowState.TRACKING):
    node = SimpleNamespace(
        _active_goal=object(), _state=state,
        _path_planner=Mock(busy=False), _recovery_retry_timer=Mock(),
        _recovery_phase=RecoveryPhase.REACHING_LAST_POSITION,
        _last_goal_position=Point2D(1.0, 0.0),
        _plan_latest_observation_if_pending=Mock(),
        _request_recovery_path=Mock(), _request_last_seen_recovery=Mock(),
        _now_seconds=lambda: 20.0,
    )
    node._recovery_retry_timer.is_canceled.return_value = True
    return node


def test_motion_tick_reconsiders_latest_observation_without_faking_a_new_one():
    """ALIGN/RETREAT/HOLD use the same current-source policy as forward drive."""
    node = _follower()
    PersonFollowerNode._on_motion_refresh(node)
    node._plan_latest_observation_if_pending.assert_called_once_with(-1)


@pytest.mark.parametrize('phase', [
    RecoveryPhase.FINISHING_WAYPOINT, RecoveryPhase.REACHING_LAST_POSITION,
])
def test_recovery_routes_are_refreshable_in_both_translation_phases(phase):
    """Keeping the waypoint never means freezing its obstacle-aware route."""
    node = _follower(FollowState.RECOVERING)
    node._recovery_phase = phase
    PersonFollowerNode._on_motion_refresh(node)
    if phase == RecoveryPhase.FINISHING_WAYPOINT:
        node._request_recovery_path.assert_called_once_with(Point2D(1.0, 0.0))
    else:
        node._request_last_seen_recovery.assert_called_once_with(20.0)


@pytest.mark.parametrize('blocked', ['busy', 'backoff', 'canceled', 'idle', 'spin'])
def test_refresh_does_not_queue_plans_or_restart_search_rotations(blocked):
    """One planner owns work; canceled/idle actions and search remain untouched."""
    node = _follower(FollowState.RECOVERING)
    if blocked == 'busy':
        node._path_planner.busy = True
    elif blocked == 'backoff':
        node._recovery_retry_timer.is_canceled.return_value = False
    elif blocked == 'canceled':
        node._active_goal = None
    elif blocked == 'idle':
        node._state = FollowState.IDLE
    else:
        node._recovery_phase = RecoveryPhase.SCANNING
    PersonFollowerNode._on_motion_refresh(node)
    node._request_recovery_path.assert_not_called()
    node._request_last_seen_recovery.assert_not_called()
    node._plan_latest_observation_if_pending.assert_not_called()


def test_late_recovery_route_cannot_replace_the_next_recovery_phase():
    """A completed old waypoint plan cannot undo a turn or renewed tracking."""
    from nav_msgs.msg import Path
    node = _follower(FollowState.RECOVERING)
    node._recovery_phase = RecoveryPhase.TURNING_TO_TARGET
    node._dispatch_tracking_path = Mock()
    PersonFollowerNode._on_tracking_path(
        node, Path(), 'old plan', None, 'last_seen_recovery', True,
        None, 1, 1, RecoveryPhase.FINISHING_WAYPOINT,
    )
    node._dispatch_tracking_path.assert_not_called()
