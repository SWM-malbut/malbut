"""Asynchronous dynamic ROS Action and Service execution for managed missions."""

from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable

from action_msgs.msg import GoalStatus
from action_msgs.srv import CancelGoal
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rosidl_runtime_py.convert import message_to_yaml

from .models import (
    CommandKind,
    MissionRecord,
    TerminalOutcome,
)


FeedbackCallback = Callable[[str, int, str], None]
TerminalCallback = Callable[
    [str, int, TerminalOutcome, str, str],
    None,
]
CancelRejectedCallback = Callable[[str, int, str], None]
DispatchTimeoutCallback = Callable[[str, int, str], None]


@dataclass
class _Execution:
    mission_id: str
    generation: int
    client: Any
    command_kind: CommandKind = CommandKind.ACTION
    downstream_goal_handle: Any = None
    cancel_pending: bool = False
    cancel_sent: bool = False
    cancel_required: bool = False
    cancel_attempt: int = 0
    active_cancel_attempt: int | None = None
    dispatch_timed_out: bool = False
    goal_response_timer: Any = None
    cancel_completion_timer: Any = None


class MissionExecutor:
    """Cache dynamic clients and forward command events without blocking."""

    def __init__(
        self,
        node,
        callback_group,
        *,
        on_feedback: FeedbackCallback,
        on_terminal: TerminalCallback,
        on_cancel_rejected: CancelRejectedCallback,
        on_dispatch_timeout: DispatchTimeoutCallback,
        goal_response_timeout_s: float,
        cancel_completion_timeout_s: float,
    ) -> None:
        self._node = node
        self._callback_group = callback_group
        self._on_feedback = on_feedback
        self._on_terminal = on_terminal
        self._on_cancel_rejected = on_cancel_rejected
        self._on_dispatch_timeout = on_dispatch_timeout
        self._goal_response_timeout_s = goal_response_timeout_s
        self._cancel_completion_timeout_s = cancel_completion_timeout_s
        self._steady_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self._lock = RLock()
        self._clients: dict[tuple[str, str, str], Any] = {}
        self._runs: dict[str, _Execution] = {}

    def start(self, mission: MissionRecord, request_message: Any) -> None:
        """Dispatch a fresh downstream generation for a mission."""
        mission.generation += 1
        generation = mission.generation
        kind = mission.capability.command_kind
        try:
            client = self._client_for(mission)
        except Exception as error:
            self._on_terminal(
                mission.mission_id,
                generation,
                TerminalOutcome.ABORTED,
                '',
                f'Failed to create downstream {kind.value.title()} client: {error}',
            )
            return
        execution = _Execution(mission.mission_id, generation, client, kind)
        with self._lock:
            self._runs[mission.mission_id] = execution
        if kind is CommandKind.SERVICE:
            self._start_service(execution, request_message)
            return
        try:
            server_ready = client.server_is_ready()
        except Exception as error:
            self._finish(
                execution,
                TerminalOutcome.ABORTED,
                message=f'Could not inspect downstream Action server: {error}',
            )
            return
        if not server_ready:
            self._finish(
                execution,
                TerminalOutcome.ABORTED,
                message=(
                    f'Action server {mission.capability.command_name} '
                    'is unavailable'
                ),
            )
            return

        try:
            future = client.send_goal_async(
                request_message,
                feedback_callback=lambda message: self._feedback(
                    execution,
                    message,
                ),
            )
        except Exception as error:
            self._finish(
                execution,
                TerminalOutcome.ABORTED,
                message=f'Failed to send downstream goal: {error}',
            )
            return
        try:
            future.add_done_callback(
                lambda completed: self._goal_response(execution, completed)
            )
        except Exception as error:
            self._mark_unresolved(
                execution,
                f'Failed to monitor downstream goal response: {error}',
            )
            return

        with self._lock:
            awaiting_response = (
                self._is_current(execution)
                and execution.downstream_goal_handle is None
            )
        if awaiting_response:
            self._arm_timer(
                execution,
                'goal_response_timer',
                self._goal_response_timeout_s,
                self._goal_response_timeout,
            )
            with self._lock:
                response_arrived = (
                    not self._is_current(execution)
                    or execution.downstream_goal_handle is not None
                )
            if response_arrived:
                self._clear_timer(execution, 'goal_response_timer')

    def cancel(self, mission_id: str) -> bool:
        """Request downstream cancellation, including pre-acceptance races."""
        with self._lock:
            execution = self._runs.get(mission_id)
            if execution is None:
                return False
        self._request_cancel(execution)
        return True

    def is_current(self, mission_id: str, generation: int) -> bool:
        """Return whether a callback belongs to the current execution."""
        with self._lock:
            current = self._runs.get(mission_id)
            return current is not None and current.generation == generation

    @property
    def active_count(self) -> int:
        """Return the number of downstream generations not yet terminal."""
        with self._lock:
            return len(self._runs)

    def begin_shutdown(self) -> int:
        """Cancel Actions and await any already-dispatched Service responses."""
        with self._lock:
            mission_ids = list(self._runs)
        for mission_id in mission_ids:
            self.cancel(mission_id)
        return len(mission_ids)

    def destroy(self) -> None:
        """Release cached graph entities during node shutdown."""
        with self._lock:
            runs = list(self._runs.values())
            self._runs.clear()
            clients = list(self._clients.items())
            self._clients.clear()
        for execution in runs:
            self._clear_execution_timers(execution)
        for key, client in clients:
            if key[0] == CommandKind.SERVICE.value:
                self._node.destroy_client(client)
            else:
                client.destroy()

    def _client_for(self, mission: MissionRecord):
        manifest = mission.capability
        key = (
            manifest.command_kind.value,
            manifest.command_name,
            manifest.command_type,
        )
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                if manifest.command_kind is CommandKind.SERVICE:
                    client = self._node.create_client(
                        manifest.interface_type,
                        manifest.command_name,
                        callback_group=self._callback_group,
                    )
                else:
                    client = ActionClient(
                        self._node,
                        manifest.interface_type,
                        manifest.command_name,
                        callback_group=self._callback_group,
                    )
                self._clients[key] = client
            return client

    def _start_service(self, execution: _Execution, request: Any) -> None:
        try:
            ready = execution.client.service_is_ready()
        except Exception as error:
            self._finish(
                execution,
                TerminalOutcome.ABORTED,
                message=f'Could not inspect downstream Service: {error}',
            )
            return
        if not ready:
            self._finish(
                execution,
                TerminalOutcome.ABORTED,
                message='Downstream Service is unavailable',
            )
            return
        try:
            future = execution.client.call_async(request)
            future.add_done_callback(
                lambda completed: self._service_response(execution, completed)
            )
        except Exception as error:
            # A transport failure may occur after the request was sent.
            # Never retry a side effect or release its unresolved resources.
            self._mark_unresolved(
                execution,
                f'Downstream Service dispatch could not be resolved: {error}',
            )

    def _service_response(self, execution: _Execution, future) -> None:
        if not self._is_current(execution):
            return
        try:
            response = future.result()
            if response is None:
                raise RuntimeError('Service returned no response')
        except Exception as error:
            self._mark_unresolved(
                execution,
                f'Downstream Service response could not be resolved: {error}',
            )
            return
        try:
            payload = message_to_yaml(response)
        except Exception as error:
            self._finish(
                execution,
                TerminalOutcome.ABORTED,
                message=f'Could not serialize Service response: {error}',
            )
            return
        # The response confirms completion, even after a cancel/preempt request.
        # Payload fields (including any application-specific success flag) are
        # forwarded unchanged; the generic manager does not interpret them.
        outcome = (
            TerminalOutcome.CANCELED
            if execution.cancel_required
            else TerminalOutcome.SUCCEEDED
        )
        self._finish(
            execution,
            outcome,
            result_yaml=payload,
            message=(
                'Service completed before cancellation could finish; '
                'side effects are not undone'
                if execution.cancel_required else ''
            ),
        )

    def _goal_response(self, execution: _Execution, future) -> None:
        if not self._is_current(execution):
            return
        self._clear_timer(execution, 'goal_response_timer')
        try:
            goal_handle = future.result()
        except Exception as error:
            self._mark_unresolved(
                execution,
                f'Downstream goal request failed: {error}',
            )
            return
        if not goal_handle.accepted:
            self._finish(
                execution,
                TerminalOutcome.ABORTED,
                message='Downstream Action server rejected the goal',
            )
            return
        with self._lock:
            if not self._is_current(execution):
                return
            execution.downstream_goal_handle = goal_handle
            cancel_pending = (
                execution.cancel_required or execution.dispatch_timed_out
            )
        try:
            result_future = goal_handle.get_result_async()
            result_future.add_done_callback(
                lambda completed: self._result(execution, completed)
            )
        except Exception as error:
            self._mark_unresolved(
                execution,
                f'Failed to await downstream result: {error}',
            )
            return
        if cancel_pending:
            self._request_cancel(execution)

    def _feedback(self, execution: _Execution, message) -> None:
        if not self._is_current(execution):
            return
        try:
            payload = message_to_yaml(message.feedback)
        except Exception as error:
            self._node.get_logger().warning(
                f'Could not serialize feedback for {execution.mission_id}: '
                f'{error}'
            )
            return
        self._on_feedback(
            execution.mission_id,
            execution.generation,
            payload,
        )

    def _result(self, execution: _Execution, future) -> None:
        if not self._is_current(execution):
            return
        try:
            wrapped = future.result()
        except Exception as error:
            self._mark_unresolved(
                execution,
                f'Downstream result failed: {error}',
            )
            return
        outcome = _terminal_outcome(wrapped.status)
        try:
            payload = message_to_yaml(wrapped.result)
        except Exception as error:
            self._finish(
                execution,
                outcome,
                message=f'Could not serialize downstream result: {error}',
            )
            return
        self._finish(execution, outcome, result_yaml=payload)

    def _request_cancel(self, execution: _Execution) -> None:
        with self._lock:
            if not self._is_current(execution):
                return
            execution.cancel_required = True
            execution.cancel_pending = True
            if execution.command_kind is CommandKind.SERVICE:
                # ROS Services have no cancellation protocol. Keep the request
                # and its resources alive until the actual response arrives.
                return
            has_goal_handle = execution.downstream_goal_handle is not None
            if not has_goal_handle and not execution.dispatch_timed_out:
                return
            if execution.active_cancel_attempt is None:
                execution.cancel_attempt += 1
                execution.active_cancel_attempt = execution.cancel_attempt
            attempt = execution.active_cancel_attempt
        self._arm_timer(
            execution,
            'cancel_completion_timer',
            self._cancel_completion_timeout_s,
            lambda current: self._cancel_completion_timeout(
                current,
                attempt,
            ),
        )
        if has_goal_handle:
            self._send_cancel(execution, attempt)

    def _send_cancel(self, execution: _Execution, attempt: int) -> None:
        with self._lock:
            if (
                not self._is_current(execution)
                or execution.active_cancel_attempt != attempt
                or execution.cancel_sent
            ):
                return
            if execution.downstream_goal_handle is None:
                execution.cancel_pending = True
                return
            execution.cancel_sent = True
            goal_handle = execution.downstream_goal_handle
        try:
            future = goal_handle.cancel_goal_async()
            future.add_done_callback(
                lambda completed: self._cancel_response(
                    execution,
                    attempt,
                    completed,
                )
            )
        except Exception as error:
            if self._reset_cancel_request(execution, attempt):
                self._on_cancel_rejected(
                    execution.mission_id,
                    execution.generation,
                    f'Downstream cancellation request failed: {error}',
                )

    def _cancel_response(
        self,
        execution: _Execution,
        attempt: int,
        future,
    ) -> None:
        with self._lock:
            if (
                not self._is_current(execution)
                or execution.active_cancel_attempt != attempt
            ):
                return
        try:
            response = future.result()
            accepted = bool(response.goals_canceling)
            return_code = getattr(
                response,
                'return_code',
                (
                    CancelGoal.Response.ERROR_NONE
                    if accepted
                    else CancelGoal.Response.ERROR_REJECTED
                ),
            )
        except Exception as error:
            if self._reset_cancel_request(execution, attempt):
                self._on_cancel_rejected(
                    execution.mission_id,
                    execution.generation,
                    f'Downstream cancellation response failed: {error}',
                )
            return
        if return_code == CancelGoal.Response.ERROR_GOAL_TERMINATED:
            return
        if return_code != CancelGoal.Response.ERROR_NONE or not accepted:
            reason = {
                CancelGoal.Response.ERROR_REJECTED: (
                    'Downstream Action server rejected cancellation'
                ),
                CancelGoal.Response.ERROR_UNKNOWN_GOAL_ID: (
                    'Downstream Action server does not know the goal'
                ),
            }.get(
                return_code,
                'Downstream Action returned an invalid cancel response',
            )
            if self._reset_cancel_request(execution, attempt):
                self._on_cancel_rejected(
                    execution.mission_id,
                    execution.generation,
                    reason,
                )

    def _finish(
        self,
        execution: _Execution,
        outcome: TerminalOutcome,
        *,
        result_yaml: str = '',
        message: str = '',
    ) -> None:
        if not self._is_current(execution):
            return
        with self._lock:
            if not self._is_current(execution):
                return
            self._runs.pop(execution.mission_id, None)
        self._clear_execution_timers(execution)
        self._on_terminal(
            execution.mission_id,
            execution.generation,
            outcome,
            result_yaml,
            message,
        )

    def _is_current(self, execution: _Execution) -> bool:
        return self.is_current(execution.mission_id, execution.generation)

    def _reset_cancel_request(
        self,
        execution: _Execution,
        attempt: int,
    ) -> bool:
        with self._lock:
            if (
                not self._is_current(execution)
                or execution.active_cancel_attempt != attempt
            ):
                return False
            execution.cancel_pending = False
            execution.cancel_sent = False
            execution.active_cancel_attempt = None
            timer = execution.cancel_completion_timer
            execution.cancel_completion_timer = None
        self._destroy_timer(timer)
        return True

    def _goal_response_timeout(self, execution: _Execution) -> None:
        self._clear_timer(execution, 'goal_response_timer')
        with self._lock:
            if (
                not self._is_current(execution)
                or execution.downstream_goal_handle is not None
                or execution.dispatch_timed_out
            ):
                return
            execution.dispatch_timed_out = True
        self._on_dispatch_timeout(
            execution.mission_id,
            execution.generation,
            'Downstream Action goal response timed out',
        )

    def _mark_unresolved(
        self,
        execution: _Execution,
        message: str,
    ) -> None:
        with self._lock:
            if not self._is_current(execution):
                return
            first_failure = not execution.dispatch_timed_out
            execution.dispatch_timed_out = True
        self._clear_timer(execution, 'goal_response_timer')
        if first_failure:
            self._on_dispatch_timeout(
                execution.mission_id,
                execution.generation,
                message,
            )
        self._request_cancel(execution)

    def _cancel_completion_timeout(
        self,
        execution: _Execution,
        attempt: int,
    ) -> None:
        if not self._reset_cancel_request(execution, attempt):
            return
        self._on_cancel_rejected(
            execution.mission_id,
            execution.generation,
            'Downstream Action did not reach a terminal state after cancel',
        )

    def _arm_timer(
        self,
        execution: _Execution,
        attribute: str,
        timeout_s: float,
        callback: Callable[[_Execution], None],
    ) -> None:
        if timeout_s <= 0.0:
            return
        with self._lock:
            if (
                not self._is_current(execution)
                or getattr(execution, attribute) is not None
            ):
                return
            try:
                timer = self._node.create_timer(
                    timeout_s,
                    lambda: callback(execution),
                    callback_group=self._callback_group,
                    clock=self._steady_clock,
                )
            except Exception as error:
                self._node.get_logger().warning(
                    f'Could not create mission watchdog: {error}'
                )
                return
            setattr(execution, attribute, timer)

    def _clear_timer(self, execution: _Execution, attribute: str) -> None:
        with self._lock:
            timer = getattr(execution, attribute)
            setattr(execution, attribute, None)
        if timer is None:
            return
        self._destroy_timer(timer)

    def _destroy_timer(self, timer: Any) -> None:
        if timer is None:
            return
        try:
            timer.cancel()
            self._node.destroy_timer(timer)
        except Exception as error:
            self._node.get_logger().warning(
                f'Could not destroy mission watchdog: {error}'
            )

    def _clear_execution_timers(self, execution: _Execution) -> None:
        self._clear_timer(execution, 'goal_response_timer')
        self._clear_timer(execution, 'cancel_completion_timer')


def _terminal_outcome(status: int) -> TerminalOutcome:
    if status == GoalStatus.STATUS_SUCCEEDED:
        return TerminalOutcome.SUCCEEDED
    if status == GoalStatus.STATUS_CANCELED:
        return TerminalOutcome.CANCELED
    return TerminalOutcome.ABORTED
