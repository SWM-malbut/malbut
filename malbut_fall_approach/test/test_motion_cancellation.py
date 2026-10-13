"""Do not release BASE merely because Nav2 acknowledged cancellation."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip('rclpy')
pytest.importorskip('nav2_msgs.action')
pytest.importorskip('tf2_ros')
interfaces = pytest.importorskip('malbut_interfaces.action')
if not hasattr(interfaces, 'FallApproach'):
    pytest.skip('malbut_interfaces without FallApproach', allow_module_level=True)

from malbut_fall_approach import fall_approach_node as module  # noqa: E402


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(module.rclpy, 'ok', lambda: True)
    return now


def future_at(clock, at, value=None):
    future = Mock()
    future.done.side_effect = lambda: clock[0] >= at
    future.result.return_value = value
    return future


def test_cancel_ack_does_not_finish_before_nav2_terminal(clock):
    node = object.__new__(module.FallApproachNode)
    node.get_logger = Mock()
    child = Mock()
    child.cancel_goal_async.return_value = future_at(clock, 0)
    child.get_result_async.return_value = future_at(clock, 7)
    node.child = child
    node._settle_child()
    assert clock[0] >= 7
    assert node.child is None
    child.cancel_goal_async.assert_called_once()


def test_late_acceptance_is_canceled_and_settled_before_outer_terminal(clock):
    node = object.__new__(module.FallApproachNode)
    node.get_logger = Mock()
    node.child = None
    child = Mock(accepted=True)
    child.cancel_goal_async.return_value = future_at(clock, 1)
    child.get_result_async.return_value = future_at(clock, 7)
    client = Mock()
    client.send_goal_async.return_value = future_at(clock, 1, child)
    with pytest.raises(module.Canceled):
        node._call(client, object(), SimpleNamespace(is_cancel_requested=True), 10)
    assert clock[0] >= 7
    assert node.child is None
    child.cancel_goal_async.assert_called_once()
