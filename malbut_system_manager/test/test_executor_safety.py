"""Safety-focused unit tests for asynchronous downstream execution."""

from types import SimpleNamespace

import pytest
from action_msgs.srv import CancelGoal

import malbut_system_manager.mission_executor as executor_module
from malbut_system_manager.mission_executor import MissionExecutor
from malbut_system_manager.models import (
    CapabilityManifest,
    CommandKind,
    ExecutionMode,
    ExecutionResource,
    MissionPriority,
    MissionRecord,
    TerminalOutcome,
)


class _Future:
    """Small controllable future implementing the callback API in use."""

    def __init__(self, *, result=None, error=None, done=False):
        self._result = result
        self._error = error
        self._done = done
        self._callbacks = []

    @classmethod
    def completed(cls, result=None, error=None):
        """Create an already-completed fake future."""
        return cls(result=result, error=error, done=True)

    def add_done_callback(self, callback):
        """Invoke immediately when done or save the callback for resolve."""
        if self._done:
            callback(self)
        else:
            self._callbacks.append(callback)

    def result(self):
        """Return the stored result or raise the stored exception."""
        if self._error is not None:
            raise self._error
        return self._result

    def resolve(self, result=None, error=None):
        """Complete the future and notify all registered callbacks."""
        self._result = result
        self._error = error
        self._done = True
        callbacks = list(self._callbacks)
        self._callbacks.clear()
        for callback in callbacks:
            callback(self)


class _GoalHandle:
    """Accepted downstream goal with controllable cancel responses."""

    accepted = True

    def __init__(self, cancel_acceptance=(True,)):
        self._cancel_acceptance = iter(cancel_acceptance)
        self.cancel_calls = 0
        self.result_future = _Future()

    def get_result_async(self):
        """Return the pending terminal-result future."""
        return self.result_future

    def cancel_goal_async(self):
        """Return the next configured cancellation response."""
        self.cancel_calls += 1
        accepted = next(self._cancel_acceptance)
        response = SimpleNamespace(
            goals_canceling=[object()] if accepted else [],
        )
        return _Future.completed(response)


class _DelayedCancelGoalHandle(_GoalHandle):
    """Accepted goal with manually completed cancellation responses."""

    def __init__(self):
        super().__init__()
        self.cancel_futures = []

    def cancel_goal_async(self):
        """Return a new pending cancellation future."""
        self.cancel_calls += 1
        future = _Future()
        self.cancel_futures.append(future)
        return future


class _UnmonitorableGoalHandle(_GoalHandle):
    """Accepted goal whose result future cannot be created."""

    def get_result_async(self):
        """Simulate loss of the result-monitoring path."""
        raise RuntimeError('result monitoring unavailable')


class _Client:
    """Fake ActionClient that accepts each sent goal immediately."""

    def __init__(self, cancel_acceptance=(True,)):
        self.cancel_acceptance = cancel_acceptance
        self.goal_handles = []
        self.destroyed = False

    def server_is_ready(self):
        """Report a ready downstream server."""
        return True

    def send_goal_async(self, _request, *, feedback_callback):
        """Create and return one accepted downstream goal."""
        del feedback_callback
        goal_handle = _GoalHandle(self.cancel_acceptance)
        self.goal_handles.append(goal_handle)
        return _Future.completed(goal_handle)

    def destroy(self):
        """Record release of the fake graph entity."""
        self.destroyed = True


class _PendingClient(_Client):
    """Fake client whose goal response is controlled by the test."""

    def __init__(self):
        super().__init__()
        self.goal_response = _Future()

    def send_goal_async(self, _request, *, feedback_callback):
        """Return a pending goal-response future."""
        del feedback_callback
        return self.goal_response


class _FixedGoalClient(_Client):
    """Fake client returning a caller-provided accepted goal handle."""

    def __init__(self, goal_handle):
        super().__init__()
        self.goal_handle = goal_handle

    def send_goal_async(self, _request, *, feedback_callback):
        """Return the configured goal handle immediately."""
        del feedback_callback
        self.goal_handles.append(self.goal_handle)
        return _Future.completed(self.goal_handle)


class _Logger:
    """Minimal logger accepted by feedback error handling."""

    def warning(self, _message):
        """Ignore warnings in unit tests."""


class _Timer:
    """Controllable one-shot view over rclpy's periodic timer API."""

    def __init__(self, callback):
        self.callback = callback
        self.canceled = False

    def cancel(self):
        """Mark the timer canceled."""
        self.canceled = True

    def fire(self):
        """Run the callback once unless canceled."""
        if not self.canceled:
            self.callback()


class _Node:
    """Minimal node stand-in needed by MissionExecutor."""

    def __init__(self):
        self._logger = _Logger()
        self.timers = []

    def get_logger(self):
        """Return the fake logger."""
        return self._logger

    def create_timer(self, _period, callback, **_kwargs):
        """Create a controllable fake timer."""
        timer = _Timer(callback)
        self.timers.append(timer)
        return timer

    def destroy_timer(self, timer):
        """Remove a destroyed timer from the live collection."""
        if timer in self.timers:
            self.timers.remove(timer)


def _mission(mission_id, command_name=None):
    capability = CapabilityManifest(
        capability_id=f'capability_{mission_id}',
        title=mission_id,
        description=f'Test capability for {mission_id}',
        command_kind=CommandKind.ACTION,
        command_name=command_name or f'/test/{mission_id}',
        command_type='test_interfaces/action/Test',
        execution_mode=ExecutionMode.FOREGROUND,
        priority=MissionPriority.NORMAL,
        resources=frozenset({ExecutionResource.BASE}),
        input_fields={},
        interface_type=object,
        source_path=f'/test/{mission_id}.yaml',
    )
    return MissionRecord(
        mission_id=mission_id,
        capability=capability,
        arguments={},
    )


def _executor(
    *,
    node=None,
    goal_response_timeout_s=0.0,
    cancel_completion_timeout_s=0.0,
):
    node = node or _Node()
    events = SimpleNamespace(
        terminal=[],
        cancel_rejected=[],
        dispatch_timeout=[],
    )
    executor = MissionExecutor(
        node,
        object(),
        on_feedback=lambda *_args: None,
        on_terminal=lambda *args: events.terminal.append(args),
        on_cancel_rejected=(
            lambda *args: events.cancel_rejected.append(args)
        ),
        on_dispatch_timeout=(
            lambda *args: events.dispatch_timeout.append(args)
        ),
        goal_response_timeout_s=goal_response_timeout_s,
        cancel_completion_timeout_s=cancel_completion_timeout_s,
    )
    return executor, events


def test_cancel_rejection_resets_executor_for_a_later_retry(monkeypatch):
    """A rejected cancel must not permanently suppress another request."""
    client = _Client(cancel_acceptance=(False, True))
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    executor, events = _executor()
    mission = _mission('retry-cancel')
    executor.start(mission, object())
    goal_handle = client.goal_handles[0]

    assert executor.cancel(mission.mission_id)
    assert goal_handle.cancel_calls == 1
    assert len(events.cancel_rejected) == 1

    assert executor.cancel(mission.mission_id)
    assert goal_handle.cancel_calls == 2
    assert len(events.cancel_rejected) == 1
    assert executor.active_count == 1


def test_action_client_creation_failure_aborts_without_a_live_run(
    monkeypatch,
):
    """Abort the mission if ActionClient construction raises an error."""

    def fail_client_creation(*_args, **_kwargs):
        raise RuntimeError('invalid downstream type support')

    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        fail_client_creation,
    )
    executor, events = _executor()
    mission = _mission('client-failure')

    executor.start(mission, object())

    assert mission.generation == 1
    assert executor.active_count == 0
    assert len(events.terminal) == 1
    event = events.terminal[0]
    assert event[:3] == (
        'client-failure',
        1,
        TerminalOutcome.ABORTED,
    )
    assert 'Failed to create downstream Action client' in event[4]
    assert 'invalid downstream type support' in event[4]


def test_immediate_goal_response_leaves_no_response_watchdog(monkeypatch):
    """An already-completed response must not leave a periodic timer."""
    node = _Node()
    client = _Client()
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    executor, _events = _executor(
        node=node,
        goal_response_timeout_s=5.0,
    )

    executor.start(_mission('immediate-response'), object())

    assert node.timers == []
    assert executor.active_count == 1


def test_result_monitor_failure_retains_control_and_requests_cancel(
    monkeypatch,
):
    """An accepted goal must never be dropped after monitor failure."""
    goal_handle = _UnmonitorableGoalHandle()
    client = _FixedGoalClient(goal_handle)
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    executor, events = _executor()

    executor.start(_mission('monitor-failure'), object())

    assert len(events.dispatch_timeout) == 1
    assert goal_handle.cancel_calls == 1
    assert executor.active_count == 1


def test_accepted_cancel_waits_for_actual_terminal_result(monkeypatch):
    """Cancel acceptance alone must not complete the managed mission."""
    client = _Client()
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    monkeypatch.setattr(
        executor_module,
        'message_to_yaml',
        lambda _message: 'success: false\n',
    )
    executor, events = _executor()
    mission = _mission('terminal-cancel')
    executor.start(mission, object())
    goal_handle = client.goal_handles[0]

    assert executor.cancel(mission.mission_id)
    assert goal_handle.cancel_calls == 1
    assert events.terminal == []
    assert executor.active_count == 1

    wrapped_result = SimpleNamespace(
        status=executor_module.GoalStatus.STATUS_CANCELED,
        result=object(),
    )
    goal_handle.result_future.resolve(wrapped_result)

    assert executor.active_count == 0
    assert events.terminal[0][:3] == (
        'terminal-cancel',
        1,
        TerminalOutcome.CANCELED,
    )


def test_shutdown_requests_every_cancel_and_destroy_releases_clients(
    monkeypatch,
):
    """Shutdown must cancel all live runs before releasing clients."""
    clients = []

    def build_client(*_args, **_kwargs):
        client = _Client()
        clients.append(client)
        return client

    monkeypatch.setattr(executor_module, 'ActionClient', build_client)
    executor, events = _executor()
    first = _mission('first-shutdown')
    second = _mission('second-shutdown')
    executor.start(first, object())
    executor.start(second, object())

    active_at_shutdown = executor.begin_shutdown()

    assert active_at_shutdown == 2
    assert [client.goal_handles[0].cancel_calls for client in clients] == [
        1,
        1,
    ]
    assert events.terminal == []

    executor.destroy()

    assert executor.active_count == 0
    assert all(client.destroyed for client in clients)


def test_goal_response_timeout_reports_and_cancels_a_late_acceptance(
    monkeypatch,
):
    """A timed-out dispatch must retain control of a late accepted goal."""
    node = _Node()
    client = _PendingClient()
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    executor, events = _executor(
        node=node,
        goal_response_timeout_s=5.0,
    )
    mission = _mission('late-acceptance')
    executor.start(mission, object())

    assert len(node.timers) == 1
    node.timers[0].fire()
    assert events.dispatch_timeout[0][:2] == ('late-acceptance', 1)

    goal_handle = _GoalHandle()
    client.goal_response.resolve(goal_handle)

    assert goal_handle.cancel_calls == 1
    assert executor.active_count == 1


def test_cancel_before_goal_response_preserves_compensating_cancel(
    monkeypatch,
):
    """A public cancel before acceptance must cancel a late accepted goal."""
    node = _Node()
    client = _PendingClient()
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    executor, events = _executor(
        node=node,
        goal_response_timeout_s=5.0,
        cancel_completion_timeout_s=5.0,
    )
    mission = _mission('cancel-before-response')
    executor.start(mission, object())

    assert executor.cancel(mission.mission_id)
    assert len(node.timers) == 1
    node.timers[0].fire()
    assert len(events.dispatch_timeout) == 1

    goal_handle = _GoalHandle()
    client.goal_response.resolve(goal_handle)

    assert goal_handle.cancel_calls == 1


def test_cancel_after_dispatch_timeout_has_a_bounded_resolution(
    monkeypatch,
):
    """A later preemption cannot wait forever on an unknown dispatch."""
    node = _Node()
    client = _PendingClient()
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    executor, events = _executor(
        node=node,
        goal_response_timeout_s=5.0,
        cancel_completion_timeout_s=5.0,
    )
    mission = _mission('timed-out-then-canceled')
    executor.start(mission, object())
    node.timers[0].fire()

    assert executor.cancel(mission.mission_id)
    assert len(node.timers) == 1
    node.timers[0].fire()
    assert len(events.cancel_rejected) == 1

    goal_handle = _GoalHandle()
    client.goal_response.resolve(goal_handle)
    assert goal_handle.cancel_calls == 1


@pytest.mark.parametrize(
    ('response', 'error'),
    [
        (SimpleNamespace(goals_canceling=[]), None),
        (SimpleNamespace(goals_canceling=[object()]), None),
        (None, RuntimeError('late cancel response failed')),
    ],
)
def test_stale_cancel_response_cannot_disarm_a_new_attempt(
    monkeypatch,
    response,
    error,
):
    """Only the current cancel attempt may mutate its watchdog state."""
    node = _Node()
    goal_handle = _DelayedCancelGoalHandle()
    client = _FixedGoalClient(goal_handle)
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    executor, events = _executor(
        node=node,
        cancel_completion_timeout_s=5.0,
    )
    mission = _mission('stale-cancel-response')
    executor.start(mission, object())

    assert executor.cancel(mission.mission_id)
    first_timer = node.timers[0]
    first_timer.fire()
    assert len(events.cancel_rejected) == 1

    assert executor.cancel(mission.mission_id)
    second_timer = node.timers[0]
    assert second_timer is not first_timer
    assert goal_handle.cancel_calls == 2

    goal_handle.cancel_futures[0].resolve(response, error)

    assert node.timers == [second_timer]
    assert len(events.cancel_rejected) == 1
    second_timer.fire()
    assert len(events.cancel_rejected) == 2


def test_cancel_completion_timeout_reports_unresolved_downstream(
    monkeypatch,
):
    """An accepted cancel still needs a bounded terminal response."""
    node = _Node()
    client = _Client()
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    executor, events = _executor(
        node=node,
        cancel_completion_timeout_s=5.0,
    )
    mission = _mission('cancel-timeout')
    executor.start(mission, object())

    assert executor.cancel(mission.mission_id)
    assert len(node.timers) == 1
    node.timers[0].fire()

    assert events.cancel_rejected[0][:2] == ('cancel-timeout', 1)
    assert 'terminal state' in events.cancel_rejected[0][2]
    assert executor.active_count == 1


def test_already_terminal_cancel_response_waits_for_result(
    monkeypatch,
):
    """GOAL_TERMINATED is a result race, not cancellation rejection."""
    node = _Node()
    goal_handle = _DelayedCancelGoalHandle()
    client = _FixedGoalClient(goal_handle)
    monkeypatch.setattr(
        executor_module,
        'ActionClient',
        lambda *_args, **_kwargs: client,
    )
    monkeypatch.setattr(
        executor_module,
        'message_to_yaml',
        lambda _message: 'success: true\n',
    )
    executor, events = _executor(
        node=node,
        cancel_completion_timeout_s=5.0,
    )
    mission = _mission('terminal-cancel-race')
    executor.start(mission, object())
    executor.cancel(mission.mission_id)

    goal_handle.cancel_futures[0].resolve(
        SimpleNamespace(
            return_code=CancelGoal.Response.ERROR_GOAL_TERMINATED,
            goals_canceling=[],
        )
    )

    assert events.cancel_rejected == []
    assert len(node.timers) == 1
    goal_handle.result_future.resolve(
        SimpleNamespace(
            status=executor_module.GoalStatus.STATUS_SUCCEEDED,
            result=object(),
        )
    )
    assert executor.active_count == 0
    assert events.terminal[0][2] is TerminalOutcome.SUCCEEDED
