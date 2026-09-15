"""Small transport-free checks for latest-only Nav2 motion replacement."""

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from action_msgs.msg import GoalStatus
from nav_msgs.msg import Path
import pytest

from malbut_tracking import navigation
from malbut_tracking.navigation import MotionMode, Nav2MotionClient


class GoalHandle:
    """Expose acceptance, cancel acknowledgement, and terminal separately."""

    def __init__(self, accepted=True):
        self.accepted = accepted
        self.result = Future()
        self.cancel_response = Future()
        self.cancel_count = 0

    def get_result_async(self):
        return self.result

    def cancel_goal_async(self):
        self.cancel_count += 1
        return self.cancel_response

    def finish(self, status=GoalStatus.STATUS_CANCELED):
        self.result.set_result(SimpleNamespace(status=status))


class Client:
    """Keep each send pending until the test chooses its response order."""

    def __init__(self):
        self.goals = []
        self.responses = []

    def server_is_ready(self):
        return True

    def send_goal_async(self, goal, feedback_callback=None):
        self.goals.append(goal)
        response = Future()
        self.responses.append(response)
        return response

    def accept(self, index=-1, accepted=True):
        handle = GoalHandle(accepted)
        self.responses[index].set_result(handle)
        return handle


@pytest.fixture
def motion(monkeypatch):
    """Build the real adapter without creating a ROS node or driving robot."""
    path_client, spin_client = Client(), Client()
    monkeypatch.setattr(
        navigation, 'ActionClient',
        lambda node, action, name: path_client if name == 'path' else spin_client,
    )
    result = Mock()
    adapter = Nav2MotionClient(None, 'path', 'spin', result)
    return SimpleNamespace(
        adapter=adapter, path=path_client, spin=spin_client, result=result,
    )


def request_path(motion, marker=''):
    path = Path()
    path.header.frame_id = marker
    assert motion.adapter.follow_path(path, 'controller', 'checker')


def test_spin_replacement_waits_for_terminal_and_keeps_only_latest(motion):
    assert motion.adapter.spin(1.0, 3.0)
    first = motion.spin.accept()
    assert motion.adapter.spin(-0.6, 3.0)
    assert motion.adapter.spin(-0.8, 3.0)
    assert first.cancel_count == 1
    assert motion.adapter.stopping
    assert len(motion.spin.goals) == 1
    first.cancel_response.set_result(object())
    assert len(motion.spin.goals) == 1
    first.finish()
    assert len(motion.spin.goals) == 2
    assert motion.spin.goals[-1].target_yaw == pytest.approx(-0.8)
    assert motion.adapter.mode == MotionMode.SPIN
    assert not motion.adapter.stopping
    motion.result.assert_not_called()


@pytest.mark.parametrize('initial_spin', [True, False])
def test_switching_motion_servers_waits_for_previous_terminal(motion, initial_spin):
    if initial_spin:
        motion.adapter.spin(1.0, 3.0)
        first = motion.spin.accept()
        request_path(motion)
        destination = motion.path
    else:
        request_path(motion)
        first = motion.path.accept()
        motion.adapter.spin(1.0, 3.0)
        destination = motion.spin
    assert first.cancel_count == 1
    assert not destination.goals
    first.finish()
    assert len(destination.goals) == 1


def test_late_acceptance_is_canceled_before_queued_motion_runs(motion):
    motion.adapter.spin(1.0, 3.0)
    request_path(motion)
    assert motion.adapter.stopping
    first = motion.spin.accept()
    assert first.cancel_count == 1
    assert not motion.path.goals
    first.finish()
    assert len(motion.path.goals) == 1


def test_cancel_discards_replacement_but_keeps_stop_ownership(motion):
    motion.adapter.spin(1.0, 3.0)
    first = motion.spin.accept()
    motion.adapter.spin(-1.0, 3.0)
    motion.adapter.cancel()
    assert motion.adapter.mode is None
    assert motion.adapter.busy
    assert first.cancel_count == 1
    first.finish()
    assert not motion.adapter.busy
    assert not motion.adapter.stopping
    assert len(motion.spin.goals) == 1
    motion.result.assert_not_called()


def test_cancel_before_acceptance_does_not_forget_late_goal(motion):
    motion.adapter.spin(1.0, 3.0)
    motion.adapter.cancel()
    assert motion.adapter.busy
    first = motion.spin.accept()
    assert first.cancel_count == 1
    assert motion.adapter.busy
    first.finish()
    assert not motion.adapter.busy
    motion.result.assert_not_called()


def test_follow_path_replacement_does_not_cancel_active_controller(motion):
    request_path(motion, 'first')
    first = motion.path.accept()
    request_path(motion, 'second')
    assert len(motion.path.goals) == 2
    assert first.cancel_count == 0
    second = motion.path.accept()
    first.finish(GoalStatus.STATUS_ABORTED)
    motion.result.assert_not_called()
    second.finish(GoalStatus.STATUS_SUCCEEDED)
    assert not motion.adapter.busy
    motion.result.assert_called_once()
    assert motion.result.call_args.args[:2] == (
        MotionMode.NAVIGATE, GoalStatus.STATUS_SUCCEEDED,
    )


def test_path_updates_coalesce_while_initial_acceptance_is_pending(motion):
    request_path(motion, 'first')
    for index in range(100):
        request_path(motion, str(index))
    assert len(motion.path.goals) == 1
    first = motion.path.accept()
    assert len(motion.path.goals) == 2
    assert motion.path.goals[-1].path.header.frame_id == '99'
    assert first.cancel_count == 0


def test_path_updates_bound_inflight_replacements_until_old_result(motion):
    request_path(motion, 'first')
    first = motion.path.accept()
    request_path(motion, 'second')
    second = motion.path.accept()
    for index in range(100):
        request_path(motion, str(index))
    assert len(motion.path.goals) == 2
    first.finish(GoalStatus.STATUS_ABORTED)
    assert len(motion.path.goals) == 3
    assert motion.path.goals[-1].path.header.frame_id == '99'
    assert second.cancel_count == 0
    assert len(motion.adapter._requests) == 2


def test_switch_waits_for_all_paths_including_late_replacement(motion):
    request_path(motion)
    first = motion.path.accept()
    request_path(motion)
    motion.adapter.spin(1.0, 3.0)
    first.finish()
    assert not motion.spin.goals
    second = motion.path.accept()
    assert second.cancel_count == 1
    assert not motion.spin.goals
    second.finish()
    assert len(motion.spin.goals) == 1


def test_rejected_path_replacement_stops_previous_path_before_spin(motion):
    request_path(motion)
    first = motion.path.accept()
    request_path(motion)
    motion.path.accept(accepted=False)
    assert first.cancel_count == 1
    assert motion.adapter.stopping
    motion.result.assert_called_once()
    motion.adapter.spin(1.0, 3.0)
    assert not motion.spin.goals
    first.finish()
    assert len(motion.spin.goals) == 1


def test_unknown_terminal_result_does_not_release_motion_ownership(motion):
    motion.adapter.spin(1.0, 3.0)
    first = motion.spin.accept()
    motion.adapter.spin(-1.0, 3.0)
    first.result.set_exception(RuntimeError('transport lost'))
    assert motion.adapter.busy
    assert motion.adapter.stopping
    assert motion.adapter.mode is None
    motion.result.assert_called_once()
    assert 'unconfirmed' in motion.result.call_args.args[2]
    request_path(motion)
    assert not motion.path.goals
    assert len(motion.spin.goals) == 1
