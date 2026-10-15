"""Keep peripheral reporting failures outside the mission ownership boundary."""

from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

from rclpy.task import Future

from malbut_interfaces.msg import SystemState
from malbut_system_manager.models import MissionCompletion, SchedulerEffects, TerminalOutcome
from malbut_system_manager.state_store import StateStore
from malbut_system_manager.system_manager_node import SystemManagerNode


def _manager():
    node = object.__new__(SystemManagerNode)
    node._lock = RLock()
    node._effects_lock = RLock()
    node._state = StateStore()
    node._state_publisher = Mock()
    node._state_publish_pending = False
    node.get_logger = Mock()
    return node


def test_state_publication_failure_is_retried_with_current_state():
    """Retry only failed reports and never replay an obsolete demand snapshot."""
    node = _manager()
    node._state_publisher.publish.side_effect = RuntimeError('temporary DDS error')
    node._publish_state()
    assert node._state_publish_pending
    assert node._state_publisher.publish.call_args.args[0].system_state == SystemState.BOOTING
    node._state.ready = True
    node._state_publisher.publish.side_effect = None
    node._retry_state_publication()
    assert not node._state_publish_pending
    assert node._state_publisher.publish.call_args.args[0].system_state == SystemState.IDLE
    node._retry_state_publication()
    assert node._state_publisher.publish.call_count == 2


def test_terminal_result_survives_reporting_failure_and_finishes_only_once():
    """A completed child still resolves the public mission if telemetry fails."""
    node = _manager()
    mission = SimpleNamespace(generation=1)
    node._state.get = Mock(side_effect=[mission, None])
    completion = MissionCompletion('mission', TerminalOutcome.SUCCEEDED)
    node._scheduler = Mock()
    node._scheduler.handle_terminal.return_value = SchedulerEffects(complete=[completion])
    done = Future()
    node._contexts = {'mission': SimpleNamespace(completion=done)}
    node._speaker_waiting = {}
    node._deferred_downstream_cancellations = set()
    node._speech = Mock()
    node._state_publisher.publish.side_effect = RuntimeError('temporary DDS error')
    node._on_downstream_terminal('mission', 1, TerminalOutcome.SUCCEEDED, '', '')
    assert done.done() and done.result() is completion
    assert node._state_publish_pending
    node._on_downstream_terminal('mission', 1, TerminalOutcome.SUCCEEDED, '', '')
    node._scheduler.handle_terminal.assert_called_once()
