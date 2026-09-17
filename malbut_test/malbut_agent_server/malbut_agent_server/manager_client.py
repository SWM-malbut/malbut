"""Observe one public Manager Action per explicit developer request."""

from dataclasses import dataclass, field
import math
from threading import RLock
import time
from typing import Any, Callable, Optional
from uuid import UUID, uuid4


EXECUTE_MISSION_ACTION = '/malbut/mission/execute'
PROGRESS_STATES = {'PENDING', 'RUNNING', 'CANCELING', 'SUSPENDED'}


@dataclass
class _Request:
    request_id: str
    capability_id: str
    arguments_yaml: str
    goal_uuid: UUID = field(default_factory=uuid4)
    state: str = 'SUBMITTING'
    kind: str = 'submitted'
    accepted: Optional[bool] = None
    terminal: bool = False
    finished: bool = False
    mission_id: Optional[str] = None
    reason: str = ''
    result_yaml: Optional[str] = None
    feedback_yaml: Optional[str] = None
    ros_status: Optional[int] = None
    goal_handle: Any = None
    goal_pending: bool = False
    goal_deadline: Optional[float] = None
    cancel_requested: bool = False
    cancel_sent: bool = False
    cancel_deadline: Optional[float] = None

    def snapshot(self) -> dict:
        return {
            'request_id': self.request_id,
            'goal_id': self.goal_uuid.hex,
            'capability_id': self.capability_id,
            'kind': self.kind,
            'state': self.state,
            'accepted': self.accepted,
            'terminal': self.terminal,
            'cancel_requested': self.cancel_requested,
            'mission_id': self.mission_id,
            'reason': self.reason,
            'result_yaml': self.result_yaml,
            'feedback_yaml': self.feedback_yaml,
            'ros_status': self.ros_status,
        }


class ManagerClient:
    """Submit, observe and cancel without retrying a possibly sent Goal.

    Events describe ROS communication, not physical completion. Request records
    are process-local. ROS imports and graph entities exist only after opt-in
    construction. The event callback should return promptly.
    """

    def __init__(
        self, node, *, on_event: Callable[[dict], None],
        goal_response_timeout_s: float = 5.0,
    ):
        if (not math.isfinite(goal_response_timeout_s)
                or goal_response_timeout_s <= 0):
            raise ValueError(
                'goal_response_timeout_s must be positive and finite',
            )
        from action_msgs.msg import GoalStatus
        from malbut_interfaces.action import ExecuteMission
        from rclpy.action import ActionClient
        from unique_identifier_msgs.msg import UUID as GoalUUID

        self._node = node
        self._on_event = on_event
        self._timeout_s = goal_response_timeout_s
        self._goal_status = GoalStatus
        self._action_type = ExecuteMission
        self._uuid_type = GoalUUID
        self._lock = RLock()
        self._requests: dict[str, _Request] = {}
        self._closed = False
        self._client = ActionClient(
            node, ExecuteMission, EXECUTE_MISSION_ACTION,
        )
        try:
            self._timer = node.create_timer(
                min(0.1, goal_response_timeout_s / 2), self._check_timeouts,
            )
        except Exception:
            self._client.destroy()
            raise

    def submit(
        self, capability_id: str, arguments: dict,
        request_id: Optional[str] = None,
    ) -> str:
        """Send once and return the local ID without waiting for a result."""
        import yaml

        if not isinstance(capability_id, str) or not capability_id.strip():
            raise ValueError('capability_id must be a nonblank string')
        if not isinstance(arguments, dict) or any(
            not isinstance(key, str) for key in arguments
        ):
            raise ValueError('arguments must be a mapping with string keys')
        try:
            arguments_yaml = yaml.safe_dump(arguments, allow_unicode=True)
        except (yaml.YAMLError, TypeError, ValueError) as error:
            raise ValueError('arguments require YAML-safe values') from error
        if request_id is None:
            request_id = uuid4().hex
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError('request_id must be a nonblank string')
        with self._lock:
            self._require_open()
            existing = self._requests.get(request_id)
            if existing is not None:
                if (existing.capability_id != capability_id
                        or existing.arguments_yaml != arguments_yaml):
                    raise ValueError(
                        'request_id already identifies different input',
                    )
                return request_id
            record = _Request(request_id, capability_id, arguments_yaml)
            self._requests[request_id] = record
            try:
                ready = self._client.server_is_ready()
            except Exception:
                ready = False
            if not ready:
                self._finish(record, 'unavailable', 'UNAVAILABLE',
                             'Manager Action server is not available')
                return request_id
            goal = self._action_type.Goal()
            goal.capability_id = capability_id
            goal.arguments_yaml = arguments_yaml
            goal_id = self._uuid_type(uuid=list(record.goal_uuid.bytes))
            record.goal_pending = True
            record.goal_deadline = time.monotonic() + self._timeout_s
            self._emit(record, 'submitted')
            if self._closed:
                record.goal_pending = False
                record.goal_deadline = None
                return request_id
            try:
                future = self._client.send_goal_async(
                    goal, goal_uuid=goal_id,
                    feedback_callback=lambda message: self._feedback(
                        record, message,
                    ),
                )
                future.add_done_callback(
                    lambda done: self._accepted(record, done),
                )
            except Exception:
                record.goal_pending = False
                record.goal_deadline = None
                self._unknown(record, 'Goal submission outcome is unknown')
            return request_id

    def snapshot(self, request_id: str) -> dict:
        """Return a detached, JSON-serializable observation of this process."""
        with self._lock:
            return self._requests[request_id].snapshot()

    def cancel(self, request_id: str) -> dict:
        """Remember cancellation while the original Goal response is late."""
        with self._lock:
            self._require_open()
            record = self._requests[request_id]
            if record.terminal:
                self._emit(record, 'cancel_rejected',
                           reason='Request is no longer being observed')
            else:
                if not record.cancel_requested:
                    record.cancel_requested = True
                    self._emit(record, 'cancel_requested')
                if record.goal_handle is not None:
                    self._send_cancel(record)
                elif not record.goal_pending:
                    self._emit(record, 'cancel_unknown', reason=(
                        'No Goal handle is available for cancellation'
                    ))
            return record.snapshot()

    def close(self) -> None:
        """Stop observation and release clients; this does not stop a robot."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._timer.cancel()
            try:
                self._node.destroy_timer(self._timer)
            finally:
                self._client.destroy()

    def _accepted(self, record, future):
        with self._lock:
            if self._ignore(record):
                return
            record.goal_pending = False
            record.goal_deadline = None
            try:
                handle = future.result()
                if bytes(handle.goal_id.uuid).hex() != record.goal_uuid.hex:
                    raise ValueError('Goal identity mismatch')
                accepted = handle.accepted
                if not isinstance(accepted, bool):
                    raise ValueError('Invalid acceptance response')
            except Exception:
                self._unknown(record, 'Invalid or unavailable Goal response')
                return
            record.accepted = accepted
            if not accepted:
                self._finish(record, 'rejected', 'REJECTED',
                             'Manager did not accept the Action Goal')
                return
            record.goal_handle = handle
            if record.state not in PROGRESS_STATES:
                record.state = 'ACCEPTED'
            self._emit(record, 'accepted', reason='')
            if self._closed:
                return
            try:
                handle.get_result_async().add_done_callback(
                    lambda done: self._result(record, done),
                )
            except Exception:
                self._unknown(record, 'Result observation is unavailable')
            if record.cancel_requested:
                self._send_cancel(record)

    def _feedback(self, record, message):
        with self._lock:
            if self._ignore(record):
                return
            try:
                feedback = message.feedback
                valid = (
                    bytes(message.goal_id.uuid).hex() == record.goal_uuid.hex
                    and feedback.mission_id == record.goal_uuid.hex
                    and feedback.state in PROGRESS_STATES
                    and isinstance(feedback.feedback_yaml, str)
                )
            except (AttributeError, TypeError, ValueError):
                valid = False
            if not valid:
                self._unknown(record, 'Invalid feedback identity or format')
                return
            record.mission_id = feedback.mission_id
            record.feedback_yaml = feedback.feedback_yaml
            # A timeout remains uncertain until the matching Goal is accepted.
            if record.state == 'UNKNOWN' and record.goal_handle is None:
                return
            record.state = feedback.state
            self._emit(record, 'progress', reason='')

    def _result(self, record, future):
        with self._lock:
            if self._ignore(record):
                return
            try:
                wrapped = future.result()
                result = wrapped.result
                if (result.mission_id != record.goal_uuid.hex
                        or not isinstance(result.result_yaml, str)
                        or not isinstance(result.message, str)
                        or type(wrapped.status) is not int):
                    raise ValueError('Result identity or format is invalid')
                kinds = {
                    self._goal_status.STATUS_SUCCEEDED: (
                        'succeeded', 'SUCCEEDED',
                    ),
                    self._goal_status.STATUS_ABORTED: ('failed', 'FAILED'),
                    self._goal_status.STATUS_CANCELED: (
                        'canceled', 'CANCELED',
                    ),
                }
                kind, state = kinds[wrapped.status]
            except Exception:
                record.finished = True
                self._unknown(record, 'Final result is unavailable or invalid')
                return
            record.mission_id = result.mission_id
            record.result_yaml = result.result_yaml
            record.ros_status = wrapped.status
            self._finish(record, kind, state, result.message)

    def _send_cancel(self, record):
        if record.cancel_sent or self._closed or record.terminal:
            return
        record.cancel_sent = True
        record.cancel_deadline = time.monotonic() + self._timeout_s
        try:
            record.goal_handle.cancel_goal_async().add_done_callback(
                lambda done: self._canceled(record, done),
            )
        except Exception:
            record.cancel_deadline = None
            self._emit(record, 'cancel_unknown',
                       reason='Cancellation request outcome is unknown')

    def _canceled(self, record, future):
        with self._lock:
            if self._closed or record.terminal:
                return
            record.cancel_deadline = None
            try:
                response = future.result()
                matches = any(
                    bytes(info.goal_id.uuid).hex() == record.goal_uuid.hex
                    for info in response.goals_canceling
                )
                if response.return_code == 0 and matches:
                    self._emit(record, 'cancel_accepted', reason='')
                elif response.return_code in (1, 2, 3):
                    record.cancel_sent = False
                    self._emit(record, 'cancel_rejected', reason=(
                        'Manager cancellation '
                        f'return_code={response.return_code}'
                    ))
                else:
                    raise ValueError('Invalid cancellation response')
            except Exception:
                self._emit(record, 'cancel_unknown', reason=(
                    'Cancellation response is unavailable or invalid'
                ))

    def _check_timeouts(self):
        with self._lock:
            if self._closed:
                return
            now = time.monotonic()
            for record in tuple(self._requests.values()):
                if record.terminal:
                    continue
                if (record.goal_deadline is not None
                        and now >= record.goal_deadline):
                    record.goal_deadline = None
                    self._unknown(
                        record, 'Goal response timed out; no start retry',
                    )
                if (record.cancel_deadline is not None
                        and now >= record.cancel_deadline):
                    record.cancel_deadline = None
                    self._emit(record, 'cancel_unknown',
                               reason='Cancellation response timed out')

    def _unknown(self, record, reason):
        record.state = 'UNKNOWN'
        self._emit(record, 'unknown', reason=reason)

    def _finish(self, record, kind, state, reason):
        record.state = state
        record.terminal = True
        record.finished = True
        record.goal_deadline = None
        record.cancel_deadline = None
        self._emit(record, kind, reason=reason)

    def _emit(self, record, kind, *, reason=None):
        record.kind = kind
        if reason is not None:
            record.reason = reason
        try:
            self._on_event(record.snapshot())
        except Exception as error:
            self._node.get_logger().error(
                f'Manager event callback failed: {type(error).__name__}',
            )

    def _ignore(self, record):
        return self._closed or record.finished

    def _require_open(self):
        if self._closed:
            raise RuntimeError('Manager client is closed')
