"""Tests for the read-only Nav2 NavigateToPose status observer."""

import ast
import hashlib
import inspect
import json
import threading
import time
from types import SimpleNamespace

import pytest

from malbut_scenarios import nav2_goal_status_observer as observer_module
from malbut_scenarios.nav2_goal_status_observer import (
    NAVIGATE_TO_POSE_STATUS_TOPIC,
    Nav2GoalStatusCollector,
    Nav2GoalStatusObserver,
)


def _message(goal_uuid, status):
    return SimpleNamespace(status_list=[SimpleNamespace(
        goal_info=SimpleNamespace(
            goal_id=SimpleNamespace(uuid=goal_uuid),
        ),
        status=status,
    )])


def test_collector_hashes_goal_uuid_and_tracks_terminal_status() -> None:
    """Retain a digest and count, never the original ROS UUID."""
    collector = Nav2GoalStatusCollector()
    goal_uuid = bytes(range(1, 17))
    raw_hex = goal_uuid.hex()

    collector.observe(_message(goal_uuid, 1))
    assert collector.snapshot().distinct_goal_count == 0

    collector.begin_window()
    collector.observe(_message(goal_uuid, 1))
    collector.observe(_message(goal_uuid, 2))
    collector.observe(_message(goal_uuid, 4))
    evidence = collector.end_window()

    expected_digest = hashlib.sha256(goal_uuid).hexdigest()
    assert evidence.status_topic == NAVIGATE_TO_POSE_STATUS_TOPIC
    assert evidence.distinct_goal_count == 1
    assert evidence.status_message_count == 3
    assert evidence.terminal_goal_count == 1
    assert evidence.goals[0].goal_uuid_sha256 == expected_digest
    assert evidence.goals[0].latest_status == 'succeeded'
    assert evidence.goals[0].latest_status_code == 4
    assert evidence.goals[0].status_observation_count == 3
    rendered = json.dumps(evidence.to_public_dict(), sort_keys=True)
    assert expected_digest in rendered
    assert raw_hex not in rendered
    assert raw_hex not in repr(evidence)


def test_collector_counts_distinct_goals_and_rejects_malformed_entries() -> None:
    """Malformed or zero identifiers cannot become successful evidence."""
    collector = Nav2GoalStatusCollector()
    collector.begin_window()
    collector.observe(SimpleNamespace(status_list=[
        _message(bytes(range(1, 17)), 2).status_list[0],
        _message(bytes(range(17, 33)), 6).status_list[0],
        _message(bytes(16), 4).status_list[0],
        _message(bytes(range(1, 17)), 99).status_list[0],
        SimpleNamespace(status=4),
    ]))
    collector.observe(SimpleNamespace(status_list=None))
    evidence = collector.end_window()

    assert evidence.distinct_goal_count == 2
    assert evidence.terminal_goal_count == 1
    assert evidence.status_message_count == 2
    assert evidence.rejected_status_entry_count == 4


def test_observation_window_cannot_overlap_or_collect_after_close() -> None:
    """A run has one explicit collection boundary."""
    collector = Nav2GoalStatusCollector()
    collector.begin_window()
    with pytest.raises(RuntimeError, match='window is active'):
        collector.begin_window()
    first = collector.end_window()
    collector.observe(_message(bytes(range(1, 17)), 4))

    assert first.distinct_goal_count == 0
    assert collector.snapshot().distinct_goal_count == 0
    with pytest.raises(RuntimeError, match='window is inactive'):
        collector.end_window()


def test_constructor_has_zero_ros_io_and_start_close_join_are_bounded() -> None:
    """ROS ownership begins on start and ends inside the owned thread."""
    events = []
    callback_ready = threading.Event()
    goal_uuid = bytes(range(1, 17))

    class Owner:
        def __init__(self, callback):
            self._callback = callback
            self._sent = False

        def spin_once(self, timeout_seconds):
            events.append(('spin', timeout_seconds))
            if callback_ready.wait(timeout=timeout_seconds):
                if not self._sent:
                    self._callback(_message(goal_uuid, 4))
                    self._sent = True

        def close(self):
            events.append(('owner.close', threading.current_thread().name))

    def owner_factory(domain_id, callback):
        events.append(('owner.create', domain_id))
        return Owner(callback)

    observer = Nav2GoalStatusObserver(
        77,
        startup_wait_seconds=1.0,
        _owner_factory=owner_factory,
    )
    assert events == []
    assert observer.is_alive is False
    assert observer.is_ready is False

    observer.start()
    observer.begin_window()
    callback_ready.set()
    deadline = time.monotonic() + 1.0
    while observer.snapshot().distinct_goal_count == 0:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    evidence = observer.end_window()
    observer.close()

    assert observer.join(timeout=1.0) is True
    assert evidence.distinct_goal_count == 1
    assert observer.is_alive is False
    assert observer.is_ready is False
    assert events[0] == ('owner.create', 77)
    assert events[-1] == (
        'owner.close',
        'malbut-nav2-goal-status-observer',
    )


def test_start_failure_is_reported_and_leaves_no_ready_observer() -> None:
    """A subscriber construction failure is terminal and fail-closed."""
    private_error = RuntimeError('private DDS diagnostic')

    def owner_factory(_domain_id, _callback):
        raise private_error

    observer = Nav2GoalStatusObserver(
        12,
        startup_wait_seconds=1.0,
        _owner_factory=owner_factory,
    )
    with pytest.raises(RuntimeError, match='startup failed'):
        observer.start()

    assert observer.join(timeout=1.0) is True
    assert observer.is_ready is False
    assert observer.last_error is private_error
    with pytest.raises(RuntimeError, match='observer failed'):
        observer.raise_if_failed()
    with pytest.raises(RuntimeError, match='not ready'):
        observer.begin_window()


def test_start_timeout_closes_late_owner_and_can_be_joined() -> None:
    """A slow ROS constructor cannot make the caller wait indefinitely."""
    release = threading.Event()
    owner_closed = threading.Event()

    class Owner:
        def spin_once(self, _timeout_seconds):
            raise AssertionError('a timed-out owner must not spin')

        def close(self):
            owner_closed.set()

    def owner_factory(_domain_id, _callback):
        release.wait(timeout=1.0)
        return Owner()

    observer = Nav2GoalStatusObserver(
        12,
        startup_wait_seconds=0.05,
        _owner_factory=owner_factory,
    )
    started_at = time.monotonic()
    with pytest.raises(TimeoutError, match='startup timed out'):
        observer.start()
    assert time.monotonic() - started_at < 0.5

    release.set()
    assert observer.join(timeout=1.0) is True
    assert owner_closed.is_set()
    assert observer.is_ready is False


@pytest.mark.parametrize('domain_id', [0, 101, True, 1.5, '42'])
def test_domain_id_is_explicit_and_bounded(domain_id) -> None:
    """Reject an ambiguous or unsupported ROS domain before ROS starts."""
    with pytest.raises(ValueError, match='domain ID'):
        Nav2GoalStatusObserver(domain_id)


@pytest.mark.parametrize('value', [0, -1, 31, float('inf'), True, '1'])
def test_runtime_waits_are_finite_and_bounded(value) -> None:
    """Startup and join waits cannot silently become unbounded."""
    with pytest.raises(ValueError, match='startup_wait_seconds'):
        Nav2GoalStatusObserver(42, startup_wait_seconds=value)

    observer = Nav2GoalStatusObserver(42)
    with pytest.raises(ValueError, match='join timeout'):
        observer.join(timeout=value)


def test_module_contains_subscription_only_ros_authority() -> None:
    """Prevent this evidence observer from gaining actuation primitives."""
    source = inspect.getsource(observer_module)
    tree = ast.parse(source)
    attribute_names = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }

    assert 'create_subscription' in attribute_names
    assert attribute_names.isdisjoint({
        'create_publisher',
        'create_client',
        'create_service',
        'publish',
        'send_goal',
        'send_goal_async',
        'cancel_goal',
        'cancel_goal_async',
    })
