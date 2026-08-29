"""
Observe NavigateToPose status without owning navigation authority.

The observer subscribes to the standard Nav2 action status topic only after
``start`` is called.  Construction, imports, and evidence access perform no
ROS graph I/O.  Goal UUIDs are hashed at the callback boundary and are never
retained in public evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import threading
from typing import Callable, Optional, Protocol


NAVIGATE_TO_POSE_STATUS_TOPIC = '/navigate_to_pose/_action/status'
_STATUS_NAMES = {
    0: 'unknown',
    1: 'accepted',
    2: 'executing',
    3: 'canceling',
    4: 'succeeded',
    5: 'canceled',
    6: 'aborted',
}
_TERMINAL_STATUS_CODES = frozenset({4, 5, 6})
_MIN_DOMAIN_ID = 1
_MAX_DOMAIN_ID = 100
_MAX_RUNTIME_WAIT_SECONDS = 30.0
_ROS_SPIN_PERIOD_SECONDS = 0.05
_ROS_SHUTDOWN_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class GoalStatusEvidence:
    """Content-free evidence for one distinct Nav2 goal."""

    goal_uuid_sha256: str
    latest_status: str
    latest_status_code: int
    terminal: bool
    status_observation_count: int

    def to_public_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation without a raw goal ID."""
        return {
            'goal_uuid_sha256': self.goal_uuid_sha256,
            'latest_status': self.latest_status,
            'latest_status_code': self.latest_status_code,
            'terminal': self.terminal,
            'status_observation_count': self.status_observation_count,
        }


@dataclass(frozen=True)
class Nav2GoalStatusEvidence:
    """Immutable evidence collected inside one explicit observation window."""

    status_topic: str
    distinct_goal_count: int
    status_message_count: int
    rejected_status_entry_count: int
    goals: tuple[GoalStatusEvidence, ...]

    @property
    def terminal_goal_count(self) -> int:
        """Return the number of distinct goals with a known terminal state."""
        return sum(goal.terminal for goal in self.goals)

    def to_public_dict(self) -> dict[str, object]:
        """Return deterministic content-free evidence for a JSON report."""
        return {
            'status_topic': self.status_topic,
            'distinct_goal_count': self.distinct_goal_count,
            'status_message_count': self.status_message_count,
            'rejected_status_entry_count': (
                self.rejected_status_entry_count
            ),
            'terminal_goal_count': self.terminal_goal_count,
            'goals': [goal.to_public_dict() for goal in self.goals],
        }


@dataclass
class _MutableGoalStatus:
    latest_status_code: int
    status_observation_count: int = 1


class Nav2GoalStatusCollector:
    """Reduce ROS-shaped status messages to thread-safe public evidence."""

    def __init__(self) -> None:
        """Create an inactive collector without retaining ROS objects."""
        self._lock = threading.RLock()
        self._active = False
        self._status_message_count = 0
        self._rejected_status_entry_count = 0
        self._goals: dict[str, _MutableGoalStatus] = {}

    @property
    def active(self) -> bool:
        """Return whether callbacks belong to the current run window."""
        with self._lock:
            return self._active

    def begin_window(self) -> None:
        """Open a new empty run window before any run process starts."""
        with self._lock:
            if self._active:
                raise RuntimeError('Nav2 goal observation window is active')
            self._status_message_count = 0
            self._rejected_status_entry_count = 0
            self._goals.clear()
            self._active = True

    def end_window(self) -> Nav2GoalStatusEvidence:
        """Close the run window and return its immutable evidence."""
        with self._lock:
            if not self._active:
                raise RuntimeError('Nav2 goal observation window is inactive')
            self._active = False
            return self._snapshot_locked()

    def observe(self, message: object) -> None:
        """Consume one GoalStatusArray-shaped message while the window is open."""
        with self._lock:
            if not self._active:
                return
            self._status_message_count += 1
            try:
                entries = tuple(message.status_list)
            except (AttributeError, TypeError):
                self._rejected_status_entry_count += 1
                return
            for entry in entries:
                parsed = _parse_status_entry(entry)
                if parsed is None:
                    self._rejected_status_entry_count += 1
                    continue
                digest, status_code = parsed
                current = self._goals.get(digest)
                if current is None:
                    self._goals[digest] = _MutableGoalStatus(status_code)
                    continue
                current.latest_status_code = status_code
                current.status_observation_count += 1

    def snapshot(self) -> Nav2GoalStatusEvidence:
        """Return immutable evidence without opening or closing the window."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> Nav2GoalStatusEvidence:
        goals = tuple(
            GoalStatusEvidence(
                goal_uuid_sha256=digest,
                latest_status=_STATUS_NAMES[record.latest_status_code],
                latest_status_code=record.latest_status_code,
                terminal=(
                    record.latest_status_code in _TERMINAL_STATUS_CODES
                ),
                status_observation_count=(
                    record.status_observation_count
                ),
            )
            for digest, record in sorted(self._goals.items())
        )
        return Nav2GoalStatusEvidence(
            status_topic=NAVIGATE_TO_POSE_STATUS_TOPIC,
            distinct_goal_count=len(goals),
            status_message_count=self._status_message_count,
            rejected_status_entry_count=(
                self._rejected_status_entry_count
            ),
            goals=goals,
        )


class _RosSubscriptionOwner(Protocol):
    """Narrow owner used by the observer thread."""

    def spin_once(self, timeout_seconds: float) -> None:
        """Wait for at most one bounded subscription callback."""

    def close(self) -> None:
        """Destroy subscriber, node, executor, and private ROS context."""


_RosOwnerFactory = Callable[
    [int, Callable[[object], None]],
    _RosSubscriptionOwner,
]


class Nav2GoalStatusObserver:
    """Own a bounded read-only ROS subscription on an isolated domain."""

    def __init__(
        self,
        domain_id: int,
        *,
        startup_wait_seconds: float = 5.0,
        _owner_factory: Optional[_RosOwnerFactory] = None,
    ) -> None:
        """Validate configuration without initializing ROS or a thread."""
        if (
            isinstance(domain_id, bool)
            or not isinstance(domain_id, int)
            or not _MIN_DOMAIN_ID <= domain_id <= _MAX_DOMAIN_ID
        ):
            raise ValueError('ROS domain ID is invalid')
        self._domain_id = domain_id
        self._startup_wait_seconds = _bounded_wait(
            startup_wait_seconds,
            'startup_wait_seconds',
        )
        self._owner_factory = (
            _default_ros_owner_factory
            if _owner_factory is None
            else _owner_factory
        )
        if not callable(self._owner_factory):
            raise TypeError('ROS owner factory must be callable')
        self._collector = Nav2GoalStatusCollector()
        self._condition = threading.Condition()
        self._closing = False
        self._startup_complete = False
        self._startup_ready = False
        self._last_error: Optional[Exception] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def is_alive(self) -> bool:
        """Return whether the owned subscription thread is running."""
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    @property
    def is_ready(self) -> bool:
        """Return whether the ROS subscriber was created successfully."""
        with self._condition:
            return self._startup_ready

    @property
    def last_error(self) -> Optional[Exception]:
        """Expose a private runtime error for the owning supervisor."""
        with self._condition:
            return self._last_error

    def start(self) -> None:
        """Create the read-only subscriber within one bounded startup wait."""
        with self._condition:
            if self._thread is not None:
                raise RuntimeError('Nav2 goal status observer already started')
            if self._closing:
                raise RuntimeError('Nav2 goal status observer is closed')
            self._thread = threading.Thread(
                target=self._run,
                name='malbut-nav2-goal-status-observer',
                daemon=False,
            )
            self._thread.start()
            deadline = (
                _monotonic_seconds() + self._startup_wait_seconds
            )
            while not self._startup_complete:
                remaining = deadline - _monotonic_seconds()
                if remaining <= 0.0:
                    error = TimeoutError(
                        'Nav2 goal status observer startup timed out'
                    )
                    self._last_error = error
                    self._closing = True
                    self._condition.notify_all()
                    raise error
                self._condition.wait(timeout=remaining)
            if not self._startup_ready:
                self._closing = True
                self._condition.notify_all()
                raise RuntimeError(
                    'Nav2 goal status observer startup failed'
                ) from self._last_error

    def begin_window(self) -> None:
        """Begin one explicit run window after the subscriber is ready."""
        with self._condition:
            if not self._startup_ready or self._closing:
                raise RuntimeError('Nav2 goal status observer is not ready')
        self._collector.begin_window()

    def end_window(self) -> Nav2GoalStatusEvidence:
        """End the current run window and return public-safe evidence."""
        return self._collector.end_window()

    def snapshot(self) -> Nav2GoalStatusEvidence:
        """Return a thread-safe snapshot of the current run window."""
        return self._collector.snapshot()

    def raise_if_failed(self) -> None:
        """Fail the supervisor when the subscription thread has failed."""
        with self._condition:
            error = self._last_error
        if error is not None:
            raise RuntimeError('Nav2 goal status observer failed') from error

    def close(self) -> None:
        """Request shutdown; the caller must perform a bounded join."""
        with self._condition:
            self._closing = True
            self._condition.notify_all()

    def join(self, timeout: float) -> bool:
        """Wait a bounded interval for ROS resources to be destroyed."""
        bounded_timeout = _bounded_wait(timeout, 'join timeout')
        with self._condition:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=bounded_timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        owner = None
        try:
            owner = self._owner_factory(
                self._domain_id,
                self._collector.observe,
            )
            with self._condition:
                self._startup_complete = True
                self._startup_ready = True
                self._condition.notify_all()
            while True:
                with self._condition:
                    if self._closing:
                        break
                owner.spin_once(_ROS_SPIN_PERIOD_SECONDS)
        except Exception as error:  # noqa: B902 - thread boundary
            with self._condition:
                self._last_error = error
                self._startup_complete = True
                self._startup_ready = False
                self._closing = True
                self._condition.notify_all()
        finally:
            if owner is not None:
                try:
                    owner.close()
                except Exception as error:  # noqa: B902
                    with self._condition:
                        if self._last_error is None:
                            self._last_error = error
            with self._condition:
                self._startup_ready = False
                self._condition.notify_all()


class _RclpyGoalStatusOwner:
    """Thread-confined rclpy owner with subscription-only authority."""

    def __init__(
        self,
        domain_id: int,
        callback: Callable[[object], None],
    ) -> None:
        """Initialize one private ROS context and status subscriber."""
        from action_msgs.msg import GoalStatusArray
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_action_status_default
        from rclpy.signals import SignalHandlerOptions

        self._rclpy = rclpy
        self._context = Context()
        self._node = None
        self._executor = None
        self._subscription = None
        try:
            rclpy.init(
                context=self._context,
                domain_id=domain_id,
                signal_handler_options=SignalHandlerOptions.NO,
            )
            self._node = rclpy.create_node(
                'malbut_nav2_goal_status_observer',
                context=self._context,
            )
            self._subscription = self._node.create_subscription(
                GoalStatusArray,
                NAVIGATE_TO_POSE_STATUS_TOPIC,
                callback,
                qos_profile_action_status_default,
            )
            self._executor = SingleThreadedExecutor(
                context=self._context,
            )
            self._executor.add_node(self._node)
        except Exception:
            self.close()
            raise

    def spin_once(self, timeout_seconds: float) -> None:
        """Run at most one bounded status callback."""
        if self._executor is None:
            raise RuntimeError('Nav2 status subscriber is closed')
        self._executor.spin_once(timeout_sec=timeout_seconds)

    def close(self) -> None:
        """Destroy all ROS resources on their owning thread."""
        first_error = None
        executor = self._executor
        node = self._node
        self._executor = None
        self._node = None
        self._subscription = None
        if executor is not None:
            if node is not None:
                try:
                    executor.remove_node(node)
                except Exception as error:
                    first_error = error
            try:
                executor.shutdown(
                    timeout_sec=_ROS_SHUTDOWN_TIMEOUT_SECONDS
                )
            except Exception as error:
                if first_error is None:
                    first_error = error
        if node is not None:
            try:
                node.destroy_node()
            except Exception as error:
                if first_error is None:
                    first_error = error
        try:
            self._context.try_shutdown()
        except Exception as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error


def _default_ros_owner_factory(
    domain_id: int,
    callback: Callable[[object], None],
) -> _RosSubscriptionOwner:
    """Create the concrete subscription owner only on explicit start."""
    return _RclpyGoalStatusOwner(domain_id, callback)


def _parse_status_entry(entry: object) -> Optional[tuple[str, int]]:
    try:
        status_code = entry.status
        raw_value = entry.goal_info.goal_id.uuid
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or status_code not in _STATUS_NAMES
            or isinstance(raw_value, (str, int, bool))
        ):
            return None
        raw_uuid = bytes(raw_value)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if len(raw_uuid) != 16 or not any(raw_uuid):
        return None
    return hashlib.sha256(raw_uuid).hexdigest(), status_code


def _bounded_wait(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 < float(value) <= _MAX_RUNTIME_WAIT_SECONDS
    ):
        raise ValueError(f'{name} is invalid')
    return float(value)


def _monotonic_seconds() -> float:
    import time

    return time.monotonic()


__all__ = [
    'GoalStatusEvidence',
    'NAVIGATE_TO_POSE_STATUS_TOPIC',
    'Nav2GoalStatusCollector',
    'Nav2GoalStatusEvidence',
    'Nav2GoalStatusObserver',
]
