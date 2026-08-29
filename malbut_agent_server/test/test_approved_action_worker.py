"""Deterministic tests for approved simulation action dispatch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from threading import Event

import pytest

from malbut_agent_server.application.approved_action_worker import (
    ApprovedActionWorker,
    ApprovedActionWorkerRuntime,
)
from malbut_agent_server.domain.robot_action import (
    ActionBinding,
    ActionState,
    RobotAction,
)
from malbut_agent_server.named_target import BoundNamedTarget
from malbut_agent_server.ports.action_repository import (
    ActionClaim,
    DispatchIntent,
)
from malbut_agent_server.ports.approved_navigation_executor import (
    ApprovedNavigationOutcomeUnknown,
    ApprovedNavigationRejected,
    ApprovedNavigationStatus,
)
from malbut_agent_server.robot_state_source import RobotStateEvidence
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import RobotState


TARGET_DIGEST = 'a' * 64


def _arguments_digest(arguments: dict) -> str:
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _action(*, policy_revision: str = 'malbut-safety-v1') -> RobotAction:
    arguments = {'location': '거실'}
    binding = ActionBinding(
        confirmation_request_id='confirmation-1',
        proposal_fingerprint='b' * 64,
        arguments_digest=_arguments_digest(arguments),
        target_binding_digest=TARGET_DIGEST,
        user_id='user-1',
        conversation_id='conversation-1',
        session_instance_id='session-1',
        generation=1,
        conversation_revision=2,
        decision_id='decision-1',
        tool_name='navigate',
        arguments=arguments,
        target_room_name='거실',
        target_room_category='living_room',
        confirmation_state_evidence_id='confirmation-state-1',
        confirmation_state_observed_at=99.0,
        confirmation_safety_policy_revision=policy_revision,
    )
    return RobotAction(
        action_id='action-1',
        operation_id='operation-1',
        binding=binding,
        state=ActionState.PENDING_PREFLIGHT,
        revision=1,
        created_at=99.0,
        updated_at=99.0,
        dispatch_expires_at=200.0,
    )


class _Repository:
    def __init__(self, action: RobotAction, events: list[str]) -> None:
        self.action = action
        self.events = events
        self.authorization = None
        self.finished = Event()
        self.fail_mark_started = False

    def get(self, action_id: str):
        return self.action if self.action.action_id == action_id else None

    def find_by_confirmation(self, confirmation_request_id: str):
        if (
            self.action.binding.confirmation_request_id
            == confirmation_request_id
        ):
            return self.action
        return None

    def recover_uncertain_after_restart(self, *, now: float) -> int:
        del now
        self.events.append('recover')
        return 0

    def claim_next(
        self,
        worker_id: str,
        *,
        now: float,
        lease_for: float,
    ):
        self.events.append('claim')
        if self.action.state is not ActionState.PENDING_PREFLIGHT:
            return None
        self.action = self.action.transition(
            ActionState.CLAIMED,
            updated_at=now,
        )
        return ActionClaim(
            action=self.action,
            worker_id=worker_id,
            claim_token='claim-token',
            fence=1,
            lease_expires_at=now + lease_for,
        )

    def record_dispatch_intent(
        self,
        claim: ActionClaim,
        authorization,
        *,
        now: float,
    ) -> DispatchIntent:
        self.events.append('intent')
        assert claim.action is self.action
        self.authorization = authorization
        self.action = self.action.transition(
            ActionState.DISPATCH_INTENT,
            updated_at=now,
            dispatch_authorization=authorization,
        )
        return DispatchIntent(
            action=self.action,
            intent_id='intent-1',
            worker_id=claim.worker_id,
            claim_token=claim.claim_token,
            fence=claim.fence,
        )

    def block(
        self,
        claim: ActionClaim,
        *,
        result_code: str,
        now: float,
    ) -> RobotAction:
        self.events.append('block')
        assert claim.action is self.action
        self.action = self.action.transition(
            ActionState.BLOCKED,
            updated_at=now,
            result_code=result_code,
        )
        self.finished.set()
        return self.action

    def mark_started(
        self,
        intent: DispatchIntent,
        *,
        now: float,
    ) -> DispatchIntent:
        self.events.append('started')
        if self.fail_mark_started:
            raise RuntimeError('simulated commit loss')
        assert intent.action is self.action
        self.action = self.action.transition(
            ActionState.STARTED,
            updated_at=now,
        )
        return replace(intent, action=self.action)

    def finish(
        self,
        intent: DispatchIntent,
        state: ActionState,
        *,
        result_code: str,
        now: float,
    ) -> RobotAction:
        self.events.append('finish')
        assert intent.action is self.action
        self.action = self.action.transition(
            state,
            updated_at=now,
            result_code=result_code,
        )
        self.finished.set()
        return self.action


class _Executor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.start_error = None
        self.status_items = [
            ApprovedNavigationStatus(
                state='succeeded',
                terminal=True,
                result_code='arrived',
            )
        ]
        self.start_calls = 0
        self.release_calls = 0
        self.release_error = None

    def prepare(
        self,
        location: str,
        expected_target_binding_digest: str,
    ):
        self.events.append('prepare')
        assert location == '거실'
        assert expected_target_binding_digest == TARGET_DIGEST
        return object()

    def start(self, prepared, *, committed_intent_id: str):
        del prepared
        self.events.append('start')
        self.start_calls += 1
        assert committed_intent_id == 'intent-1'
        if self.start_error is not None:
            raise self.start_error
        return object()

    def status(self, handle):
        del handle
        self.events.append('status')
        item = self.status_items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def release(self, prepared) -> None:
        del prepared
        self.events.append('release')
        self.release_calls += 1
        if self.release_error is not None:
            raise self.release_error


class _StateSource:
    def __init__(
        self,
        events: list[str],
        *,
        state: RobotState | None = None,
        observed_at: float = 100.0,
        trusted: bool = True,
    ) -> None:
        self.events = events
        self.evidence = RobotStateEvidence(
            state=state or RobotState(
                battery_percent=80.0,
                navigation_available=True,
                localization_ok=True,
            ),
            observed_at=observed_at,
            evidence_id='fresh-state-1',
            trusted=trusted,
        )

    def read(self) -> RobotStateEvidence:
        self.events.append('state')
        return self.evidence


class _Resolver:
    def __init__(
        self,
        events: list[str],
        *,
        digest: str = TARGET_DIGEST,
    ) -> None:
        self.events = events
        self.digest = digest

    def resolve(self, location: str) -> BoundNamedTarget:
        self.events.append('target')
        assert location == '거실'
        return BoundNamedTarget(
            room_name='거실',
            room_category='living_room',
            binding_digest=self.digest,
        )


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class _SequenceClock:
    def __init__(self, *values: float) -> None:
        self.values = list(values)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        if not self.values:
            raise AssertionError('clock was sampled too many times')
        return self.values.pop(0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _worker(
    *,
    action: RobotAction | None = None,
    state: RobotState | None = None,
    observed_at: float = 100.0,
    target_digest: str = TARGET_DIGEST,
    clock: _Clock | None = None,
    monotonic_clock: _Clock | None = None,
    maximum_status_polls: int = 4,
    status_deadline_seconds: float = 30.0,
):
    events: list[str] = []
    clock = clock or _Clock()
    monotonic_clock = monotonic_clock or _Clock(0.0)
    repository = _Repository(action or _action(), events)
    executor = _Executor(events)
    worker = ApprovedActionWorker(
        repository,
        executor,
        _StateSource(
            events,
            state=state,
            observed_at=observed_at,
        ),
        SafetyPolicy(),
        _Resolver(events, digest=target_digest),
        worker_id='worker-1',
        clock=clock,
        monotonic_clock=monotonic_clock,
        sleeper=monotonic_clock.sleep,
        maximum_status_polls=maximum_status_polls,
        status_deadline_seconds=status_deadline_seconds,
    )
    return worker, repository, executor, events, monotonic_clock


def test_lease_covers_prestart_io_and_status_deadline() -> None:
    """Reject a lease that may expire during bounded adapter operations."""
    events: list[str] = []

    with pytest.raises(ValueError, match='cover adapter I/O'):
        ApprovedActionWorker(
            _Repository(_action(), events),
            _Executor(events),
            _StateSource(events),
            SafetyPolicy(),
            _Resolver(events),
            worker_id='worker-1',
            lease_for_seconds=194.9,
            status_deadline_seconds=120.0,
        )


def test_allowed_action_commits_intent_before_one_start_and_finishes() -> None:
    """Commit fresh evidence before exactly one external start."""
    worker, repository, executor, events, clock = _worker()
    executor.status_items = [
        ApprovedNavigationStatus(state='driving', terminal=False),
        ApprovedNavigationStatus(
            state='succeeded',
            terminal=True,
            result_code='arrived',
        ),
    ]

    result = worker.run_once()

    assert result is not None
    assert result.state is ActionState.SUCCEEDED
    assert result.result_code == 'arrived'
    assert executor.start_calls == 1
    assert events == [
        'claim',
        'prepare',
        'state',
        'target',
        'intent',
        'start',
        'started',
        'status',
        'status',
        'finish',
        'release',
    ]
    assert events.index('intent') < events.index('start')
    assert clock.sleeps == [0.25]
    assert repository.authorization.state_evidence_id == 'fresh-state-1'
    assert repository.authorization.simulation is True
    assert repository.authorization.physical_authorized is False
    assert executor.release_calls == 1


@pytest.mark.parametrize(
    ('action', 'state', 'observed_at', 'target_digest', 'result_code'),
    [
        (
            replace(_action(), dispatch_expires_at=100.0),
            None,
            100.0,
            TARGET_DIGEST,
            'action_dispatch_expired',
        ),
        (
            replace(_action(), created_at=90.0, updated_at=90.0),
            None,
            97.9,
            TARGET_DIGEST,
            'robot_state_stale',
        ),
        (_action(), None, 100.1, TARGET_DIGEST, 'robot_state_from_future'),
        (
            _action(),
            RobotState(
                battery_percent=80.0,
                navigation_available=True,
                localization_ok=True,
                emergency_stop=True,
            ),
            100.0,
            TARGET_DIGEST,
            'safety_emergency_stop',
        ),
        (
            _action(),
            None,
            100.0,
            'c' * 64,
            'target_binding_changed',
        ),
        (
            _action(policy_revision='old-policy'),
            None,
            100.0,
            TARGET_DIGEST,
            'safety_policy_revision_changed',
        ),
    ],
)
def test_fresh_preflight_failures_block_without_start(
    action,
    state,
    observed_at,
    target_digest,
    result_code,
) -> None:
    """Block stale, unsafe, changed, or expired approvals before intent."""
    worker, _repository, executor, events, _clock = _worker(
        action=action,
        state=state,
        observed_at=observed_at,
        target_digest=target_digest,
    )

    result = worker.run_once()

    assert result is not None
    assert result.state is ActionState.BLOCKED
    assert result.result_code == result_code
    assert executor.start_calls == 0
    assert 'intent' not in events


def test_claim_clock_before_latest_durable_time_blocks_without_prepare(
) -> None:
    """Reject a broken repository claim when wall time moved backward."""
    events: list[str] = []
    claimed_action = _action().transition(
        ActionState.CLAIMED,
        updated_at=101.0,
    )

    class FutureClaimRepository(_Repository):
        def claim_next(self, worker_id, *, now, lease_for):
            del now, lease_for
            self.events.append('claim')
            return ActionClaim(
                action=self.action,
                worker_id=worker_id,
                claim_token='claim-token',
                fence=1,
                lease_expires_at=200.0,
            )

    repository = FutureClaimRepository(claimed_action, events)
    executor = _Executor(events)
    clock = _SequenceClock(100.0)
    worker = ApprovedActionWorker(
        repository,
        executor,
        _StateSource(events),
        SafetyPolicy(),
        _Resolver(events),
        worker_id='worker-1',
        clock=clock,
        sleeper=clock.sleep,
    )

    result = worker.run_once()

    assert result is not None
    assert result.state is ActionState.BLOCKED
    assert result.result_code == 'action_clock_rollback'
    assert result.updated_at == 101.0
    assert events == ['claim', 'block']
    assert executor.start_calls == 0


def test_fresh_but_preapproval_robot_state_is_blocked() -> None:
    """Require state evidence sampled after the approved action exists."""
    worker, _repository, executor, events, _clock = _worker(
        observed_at=98.5,
    )

    result = worker.run_once()

    assert result is not None
    assert result.state is ActionState.BLOCKED
    assert result.result_code == 'robot_state_predates_approval'
    assert 'intent' not in events
    assert executor.start_calls == 0


@pytest.mark.parametrize(
    ('clock_values', 'expected_prefix'),
    [
        ((100.0, 98.0), ['claim', 'prepare', 'state']),
        ((100.0, 100.0, 98.0), [
            'claim', 'prepare', 'state', 'target'
        ]),
    ],
)
def test_preflight_clock_rollback_blocks_with_non_decreasing_timestamp(
    clock_values,
    expected_prefix,
) -> None:
    """Block evidence-check or authorization rollback before intent/start."""
    clock = _SequenceClock(*clock_values)
    worker, _repository, executor, events, _clock = _worker(clock=clock)

    result = worker.run_once()

    assert result is not None
    assert result.state is ActionState.BLOCKED
    assert result.result_code == 'action_clock_rollback'
    assert result.updated_at == 100.0
    assert events[:-2] == expected_prefix
    assert events[-2:] == ['block', 'release']
    assert 'intent' not in events
    assert executor.start_calls == 0


@pytest.mark.parametrize(
    ('bad_boundary', 'expected_code'),
    [
        ('state', 'robot_state_unavailable'),
        ('target', 'target_unavailable'),
        ('safety', 'safety_evaluation_failed'),
    ],
)
def test_malformed_preflight_boundaries_block_fail_closed(
    bad_boundary,
    expected_code,
) -> None:
    """Treat malformed trusted-boundary objects as definite blocks."""
    events: list[str] = []
    repository = _Repository(_action(), events)
    executor = _Executor(events)
    clock = _Clock()

    class BadStateSource:
        def read(self):
            return object()

    class BadTargetResolver:
        def resolve(self, _location):
            return object()

    class BadSafetyPolicy(SafetyPolicy):
        def evaluate_confirmed_action(self, **_values):
            raise RuntimeError('malformed safety boundary')

    state_source = (
        BadStateSource()
        if bad_boundary == 'state'
        else _StateSource(events)
    )
    target_resolver = (
        BadTargetResolver()
        if bad_boundary == 'target'
        else _Resolver(events)
    )
    safety_policy = (
        BadSafetyPolicy()
        if bad_boundary == 'safety'
        else SafetyPolicy()
    )
    worker = ApprovedActionWorker(
        repository,
        executor,
        state_source,
        safety_policy,
        target_resolver,
        worker_id='worker-1',
        clock=clock,
        sleeper=clock.sleep,
    )

    result = worker.run_once()

    assert result is not None
    assert result.state is ActionState.BLOCKED
    assert result.result_code == expected_code
    assert executor.start_calls == 0
    assert 'intent' not in events


def test_ambiguous_start_is_unknown_and_is_never_resent() -> None:
    """Seal an ambiguous start as UNKNOWN and do not claim it again."""
    worker, repository, executor, events, _clock = _worker()
    executor.start_error = ApprovedNavigationOutcomeUnknown(
        'start',
        'transport_error',
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first is not None
    assert first.state is ActionState.UNKNOWN
    assert first.result_code == 'navigation_start_outcome_unknown'
    assert second is None
    assert repository.action.state is ActionState.UNKNOWN
    assert executor.start_calls == 1
    assert executor.release_calls == 1
    assert events.count('start') == 1


def test_release_failure_never_retries_or_changes_known_result() -> None:
    """Ignore cleanup failure after one completed external attempt."""
    worker, repository, executor, events, _clock = _worker()
    executor.release_error = RuntimeError('prepared cache unavailable')

    first = worker.run_once()
    second = worker.run_once()

    assert first is not None
    assert first.state is ActionState.SUCCEEDED
    assert second is None
    assert repository.action.state is ActionState.SUCCEEDED
    assert executor.start_calls == 1
    assert executor.release_calls == 1
    assert events.count('start') == 1


def test_any_status_read_error_is_unknown_not_failed() -> None:
    """Refuse to infer a terminal failure from an observation error."""
    worker, _repository, executor, _events, _clock = _worker()
    executor.status_items = [ApprovedNavigationRejected('status_rejected')]

    result = worker.run_once()

    assert result is not None
    assert result.state is ActionState.UNKNOWN
    assert result.result_code == 'navigation_status_outcome_unknown'
    assert executor.start_calls == 1


def test_nonterminal_status_becomes_unknown_at_deadline() -> None:
    """Stop read-only polling at its deterministic deadline."""
    monotonic_clock = _Clock(0.0)
    worker, _repository, executor, _events, _clock = _worker(
        monotonic_clock=monotonic_clock,
        maximum_status_polls=10,
        status_deadline_seconds=0.3,
    )
    executor.status_items = [
        ApprovedNavigationStatus(state='driving', terminal=False)
        for _index in range(10)
    ]

    result = worker.run_once()

    assert result is not None
    assert result.state is ActionState.UNKNOWN
    assert result.result_code == 'navigation_status_deadline_exceeded'
    assert executor.start_calls == 1
    assert 0 < len(monotonic_clock.sleeps) < 10


def test_forward_wall_clock_jump_does_not_extend_status_deadline() -> None:
    """Bound polling with monotonic time despite a later wall-clock jump."""
    wall_clock = _SequenceClock(
        100.0,
        100.0,
        100.0,
        100.0,
        10000.0,
    )
    monotonic_clock = _Clock(0.0)
    worker, repository, executor, events, _clock = _worker(
        clock=wall_clock,
        monotonic_clock=monotonic_clock,
        maximum_status_polls=10,
        status_deadline_seconds=0.3,
    )
    executor.status_items = [
        ApprovedNavigationStatus(state='driving', terminal=False)
        for _index in range(10)
    ]

    result = worker.run_once()

    assert result is not None
    assert result.state is ActionState.UNKNOWN
    assert result.result_code == 'navigation_status_deadline_exceeded'
    assert result.updated_at == 10000.0
    assert repository.action is result
    assert executor.start_calls == 1
    assert executor.release_calls == 1
    assert events.count('status') < 10
    assert sum(monotonic_clock.sleeps) == pytest.approx(0.3)


def test_backward_wall_clock_jump_after_start_fails_without_resend() -> None:
    """Leave STARTED durable when a backward finish timestamp is rejected."""
    wall_clock = _SequenceClock(
        100.0,
        100.0,
        100.0,
        100.0,
        50.0,
        50.0,
    )
    monotonic_clock = _Clock(0.0)
    worker, repository, executor, events, _clock = _worker(
        clock=wall_clock,
        monotonic_clock=monotonic_clock,
        maximum_status_polls=10,
        status_deadline_seconds=0.3,
    )
    executor.status_items = [
        ApprovedNavigationStatus(state='driving', terminal=False)
        for _index in range(10)
    ]

    with pytest.raises(ValueError, match='updated_at predates'):
        worker.run_once()
    assert worker.run_once() is None

    assert repository.action.state is ActionState.STARTED
    assert executor.start_calls == 1
    assert executor.release_calls == 1
    assert events.count('start') == 1
    assert events.count('status') < 10
    assert sum(monotonic_clock.sleeps) == pytest.approx(0.3)


@pytest.mark.parametrize(
    'invalid_value',
    [True, -1.0, float('nan'), float('inf'), '0'],
)
def test_invalid_monotonic_clock_fails_after_one_start_without_resend(
    invalid_value,
) -> None:
    """Reject malformed elapsed-time samples without another start."""
    worker, repository, executor, _events, _clock = _worker(
        monotonic_clock=_Clock(invalid_value),
    )

    with pytest.raises(ValueError, match='monotonic_clock'):
        worker.run_once()

    assert repository.action.state is ActionState.STARTED
    assert executor.start_calls == 1
    assert executor.release_calls == 1


@pytest.mark.parametrize(
    ('status', 'expected_state'),
    [
        (
            ApprovedNavigationStatus(
                state='succeeded', terminal=True, result_code='arrived'
            ),
            ActionState.SUCCEEDED,
        ),
        (
            ApprovedNavigationStatus(
                state='failed', terminal=True, result_code='path_failed'
            ),
            ActionState.FAILED,
        ),
        (
            ApprovedNavigationStatus(
                state='canceled', terminal=True, result_code='user_cancel'
            ),
            ActionState.CANCELED,
        ),
    ],
)
def test_only_exact_terminal_status_sets_known_terminal_state(
    status,
    expected_state,
) -> None:
    """Persist success, failure, or cancellation only from exact status."""
    worker, _repository, executor, _events, _clock = _worker()
    executor.status_items = [status]

    result = worker.run_once()

    assert result is not None
    assert result.state is expected_state
    assert result.result_code == status.result_code


def test_mark_started_commit_loss_never_starts_twice() -> None:
    """Leave the durable intent for recovery after a post-start DB loss."""
    worker, repository, executor, events, _clock = _worker()
    repository.fail_mark_started = True

    with pytest.raises(RuntimeError, match='simulated commit loss'):
        worker.run_once()
    assert worker.run_once() is None

    assert repository.action.state is ActionState.DISPATCH_INTENT
    assert executor.start_calls == 1
    assert executor.release_calls == 1
    assert events.count('start') == 1


def test_runtime_recovers_once_before_claim_and_joins_non_daemon() -> None:
    """Recover before the first claim and close the owned thread cleanly."""
    worker, repository, _executor, events, _clock = _worker()
    runtime = ApprovedActionWorkerRuntime(worker, idle_wait_seconds=0.01)

    runtime.start()
    assert runtime.is_ready is True
    assert repository.finished.wait(timeout=1.0)
    runtime.close()

    assert runtime.join(timeout=1.0) is True
    assert runtime.is_alive is False
    assert events[0:2] == ['recover', 'claim']
    assert events.count('recover') >= 1
    assert runtime.last_error is None


def test_runtime_start_raises_when_recovery_fails_before_any_claim() -> None:
    """Never advertise readiness or claim after startup recovery fails."""
    events: list[str] = []

    class RecoveryFailureRepository(_Repository):
        def recover_uncertain_after_restart(self, *, now: float) -> int:
            del now
            self.events.append('recover')
            raise RuntimeError('recovery unavailable')

    repository = RecoveryFailureRepository(_action(), events)
    executor = _Executor(events)
    clock = _Clock()
    worker = ApprovedActionWorker(
        repository,
        executor,
        _StateSource(events),
        SafetyPolicy(),
        _Resolver(events),
        worker_id='worker-1',
        clock=clock,
        sleeper=clock.sleep,
    )
    runtime = ApprovedActionWorkerRuntime(
        worker,
        idle_wait_seconds=0.01,
        startup_wait_seconds=0.5,
    )

    with pytest.raises(
        RuntimeError,
        match='startup recovery failed',
    ) as error:
        runtime.start()

    assert isinstance(error.value.__cause__, RuntimeError)
    assert str(error.value.__cause__) == 'recovery unavailable'
    assert runtime.join(timeout=1.0) is True
    assert runtime.is_ready is False
    assert runtime.is_alive is False
    assert events == ['recover']
    assert executor.start_calls == 0


def test_runtime_start_timeout_never_claims_after_recovery_unblocks() -> None:
    """A timed-out startup remains closed after late recovery success."""
    events: list[str] = []
    release_recovery = Event()

    class SlowRecoveryRepository(_Repository):
        def recover_uncertain_after_restart(self, *, now: float) -> int:
            del now
            self.events.append('recover')
            if not release_recovery.wait(timeout=1.0):
                raise RuntimeError('test recovery release timed out')
            return 0

    repository = SlowRecoveryRepository(_action(), events)
    executor = _Executor(events)
    clock = _Clock()
    worker = ApprovedActionWorker(
        repository,
        executor,
        _StateSource(events),
        SafetyPolicy(),
        _Resolver(events),
        worker_id='worker-1',
        clock=clock,
        sleeper=clock.sleep,
    )
    runtime = ApprovedActionWorkerRuntime(
        worker,
        idle_wait_seconds=0.01,
        startup_wait_seconds=0.01,
    )

    with pytest.raises(TimeoutError, match='recovery timed out'):
        runtime.start()
    assert runtime.is_ready is False
    assert events == ['recover']

    release_recovery.set()
    assert runtime.join(timeout=1.0) is True
    assert runtime.is_ready is False
    assert events == ['recover']
    assert executor.start_calls == 0


def test_runtime_rechecks_expired_sent_work_before_next_idle_claim() -> None:
    """Reconcile an old lease again after it can actually expire."""
    events: list[str] = []

    class IdleRepository(_Repository):
        def __init__(self):
            super().__init__(_action(), events)
            self.recover_calls = 0
            self.periodic_recovery = Event()

        def claim_next(self, *_args, **_kwargs):
            events.append('claim')
            return None

        def recover_uncertain_after_restart(self, *, now: float) -> int:
            del now
            events.append('recover')
            self.recover_calls += 1
            if self.recover_calls >= 2:
                self.periodic_recovery.set()
            return 0

    repository = IdleRepository()
    executor = _Executor(events)
    clock = _Clock()
    worker = ApprovedActionWorker(
        repository,
        executor,
        _StateSource(events),
        SafetyPolicy(),
        _Resolver(events),
        worker_id='worker-1',
        clock=clock,
        sleeper=clock.sleep,
    )
    runtime = ApprovedActionWorkerRuntime(worker, idle_wait_seconds=0.01)

    runtime.start()
    assert repository.periodic_recovery.wait(timeout=1.0)
    runtime.close()

    assert runtime.join(timeout=1.0) is True
    assert events[0:3] == ['recover', 'claim', 'recover']
    assert repository.recover_calls >= 2


def test_periodic_recovery_failure_pauses_claims_then_resumes() -> None:
    """Stay alive but claim nothing until periodic recovery succeeds."""
    events: list[str] = []
    retry_entered = Event()
    allow_recovery = Event()
    resumed_claim = Event()

    class FlakyRecoveryRepository(_Repository):
        def __init__(self):
            super().__init__(_action(), events)
            self.recover_calls = 0
            self.claim_calls = 0

        def claim_next(self, *_args, **_kwargs):
            self.claim_calls += 1
            events.append('claim')
            if self.claim_calls >= 2:
                resumed_claim.set()
            return None

        def recover_uncertain_after_restart(self, *, now: float) -> int:
            del now
            self.recover_calls += 1
            events.append('recover')
            if self.recover_calls == 2:
                raise RuntimeError('temporary recovery failure')
            if self.recover_calls == 3:
                retry_entered.set()
                if not allow_recovery.wait(timeout=1.0):
                    raise RuntimeError('test recovery release timed out')
            return 0

    repository = FlakyRecoveryRepository()
    executor = _Executor(events)
    clock = _Clock()
    worker = ApprovedActionWorker(
        repository,
        executor,
        _StateSource(events),
        SafetyPolicy(),
        _Resolver(events),
        worker_id='worker-1',
        clock=clock,
        sleeper=clock.sleep,
    )
    runtime = ApprovedActionWorkerRuntime(worker, idle_wait_seconds=0.01)

    runtime.start()
    assert retry_entered.wait(timeout=1.0)
    assert runtime.is_alive is True
    assert runtime.is_ready is False
    assert repository.claim_calls == 1

    allow_recovery.set()
    assert resumed_claim.wait(timeout=1.0)
    assert runtime.is_ready is True
    runtime.close()

    assert runtime.join(timeout=1.0) is True
    assert repository.claim_calls >= 2
    assert executor.start_calls == 0


def test_close_stops_runtime_during_periodic_recovery_retry() -> None:
    """Wake and stop a degraded runtime without another claim or retry."""
    events: list[str] = []
    periodic_failure = Event()

    class FailingRecoveryRepository(_Repository):
        def __init__(self):
            super().__init__(_action(), events)
            self.recover_calls = 0
            self.claim_calls = 0

        def claim_next(self, *_args, **_kwargs):
            self.claim_calls += 1
            events.append('claim')
            return None

        def recover_uncertain_after_restart(self, *, now: float) -> int:
            del now
            self.recover_calls += 1
            events.append('recover')
            if self.recover_calls >= 2:
                periodic_failure.set()
                raise RuntimeError('persistent recovery failure')
            return 0

    repository = FailingRecoveryRepository()
    executor = _Executor(events)
    clock = _Clock()
    worker = ApprovedActionWorker(
        repository,
        executor,
        _StateSource(events),
        SafetyPolicy(),
        _Resolver(events),
        worker_id='worker-1',
        clock=clock,
        sleeper=clock.sleep,
    )
    runtime = ApprovedActionWorkerRuntime(worker, idle_wait_seconds=0.1)

    runtime.start()
    assert periodic_failure.wait(timeout=1.0)
    runtime.close()

    assert runtime.join(timeout=1.0) is True
    assert runtime.is_ready is False
    assert repository.claim_calls == 1
    assert repository.recover_calls == 2
    assert executor.start_calls == 0
