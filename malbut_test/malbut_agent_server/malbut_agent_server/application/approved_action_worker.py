"""Dispatch one approved named-navigation action with no automatic resend."""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

from malbut_agent_server.domain.robot_action import (
    ActionState,
    DispatchAuthorization,
    RobotAction,
)
from malbut_agent_server.named_target import (
    BoundNamedTarget,
    NamedTargetResolver,
)
from malbut_agent_server.ports.action_repository import (
    ActionClaim,
    ActionRepositoryPort,
    DispatchIntent,
)
from malbut_agent_server.ports.approved_navigation_executor import (
    ApprovedNavigationError,
    ApprovedNavigationExecutorPort,
    ApprovedNavigationOutcomeUnknown,
    ApprovedNavigationRejected,
    ApprovedNavigationStatus,
)
from malbut_agent_server.robot_state_source import (
    RobotStateEvidence,
    RobotStateSource,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.tools import validate_tool_arguments


_SUCCESS_STATES = frozenset({'succeeded', 'success', 'completed'})
_CANCELED_STATES = frozenset({'canceled', 'cancelled'})
_FAILED_STATES = frozenset({'failed', 'failure', 'rejected', 'error'})
_MINIMUM_LEASE_OVERHEAD_SECONDS = 75.0


class ApprovedActionWorker:
    """
    Run one durable, simulation-only action through bounded execution.

    ``prepare`` is intentionally called before the dispatch-time checks.  It
    is a read-only adapter operation, so a failed check still causes no robot
    effect.  The external ``start`` call is reachable only after the exact
    authorization evidence has been committed as a dispatch intent.
    """

    def __init__(
        self,
        repository: ActionRepositoryPort,
        executor: ApprovedNavigationExecutorPort,
        state_source: RobotStateSource,
        safety_policy: SafetyPolicy,
        target_resolver: NamedTargetResolver,
        *,
        worker_id: str,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        lease_for_seconds: float = 240.0,
        maximum_state_age_seconds: float = 2.0,
        status_deadline_seconds: float = 120.0,
        maximum_status_polls: int = 512,
        status_poll_interval_seconds: float = 0.25,
    ) -> None:
        """Bind deterministic policy, evidence, and bounded polling inputs."""
        self._repository = repository
        self._executor = executor
        self._state_source = state_source
        self._safety_policy = safety_policy
        self._target_resolver = target_resolver
        self._worker_id = _identifier(worker_id, 'worker_id')
        if not callable(clock):
            raise TypeError('clock must be callable')
        self._clock = clock
        if not callable(monotonic_clock):
            raise TypeError('monotonic_clock must be callable')
        self._monotonic_clock = monotonic_clock
        if not callable(sleeper):
            raise TypeError('sleeper must be callable')
        self._sleeper = sleeper
        self._lease_for_seconds = _positive_finite(
            lease_for_seconds,
            'lease_for_seconds',
        )
        self._maximum_state_age_seconds = _positive_finite(
            maximum_state_age_seconds,
            'maximum_state_age_seconds',
        )
        self._status_deadline_seconds = _positive_finite(
            status_deadline_seconds,
            'status_deadline_seconds',
        )
        if (
            self._lease_for_seconds
            < self._status_deadline_seconds
            + _MINIMUM_LEASE_OVERHEAD_SECONDS
        ):
            raise ValueError(
                'lease_for_seconds must cover adapter I/O and status deadline'
            )
        self._status_poll_interval_seconds = _positive_finite(
            status_poll_interval_seconds,
            'status_poll_interval_seconds',
        )
        if (
            type(maximum_status_polls) is not int
            or maximum_status_polls < 1
            or maximum_status_polls > 10000
        ):
            raise ValueError('maximum_status_polls is invalid')
        self._maximum_status_polls = maximum_status_polls

    def recover_uncertain_after_restart(self) -> int:
        """Seal previously attempted work as UNKNOWN before any new claim."""
        return self._repository.recover_uncertain_after_restart(
            now=self._now(),
        )

    def run_once(self) -> Optional[RobotAction]:
        """Claim and finish at most one action, or return ``None`` if idle."""
        claim_at = self._now()
        claim = self._repository.claim_next(
            self._worker_id,
            now=claim_at,
            lease_for=self._lease_for_seconds,
        )
        if claim is None:
            return None
        if self._clock_predates_action(claim, claim_at):
            return self._block_clock_rollback(claim, claim_at)
        if claim_at >= claim.action.dispatch_expires_at:
            return self._block(claim, 'action_dispatch_expired')

        arguments = self._validated_arguments(claim)
        if arguments is None:
            return self._block(claim, 'tool_arguments_invalid')
        if claim.action.binding.tool_name != 'navigate':
            return self._block(claim, 'unsupported_action_tool')

        location = arguments['location']
        try:
            prepared = self._executor.prepare(
                location,
                claim.action.binding.target_binding_digest,
            )
        except Exception:
            # The prepare contract is read-only.  Any adapter failure here is
            # therefore a definite pre-dispatch failure, never UNKNOWN.
            return self._block(claim, 'navigation_prepare_failed')

        try:
            return self._run_prepared(
                claim,
                arguments,
                location,
                prepared,
            )
        finally:
            # ``release`` only retires a read-only prepared capability.  Its
            # failure must never repeat or reinterpret an external start.
            try:
                self._executor.release(prepared)
            except Exception:
                pass

    def _run_prepared(
        self,
        claim: ActionClaim,
        arguments: dict,
        location: str,
        prepared: object,
    ) -> RobotAction:
        """Validate and consume one successfully prepared capability."""
        try:
            evidence = self._state_source.read()
            if not isinstance(evidence, RobotStateEvidence):
                raise TypeError('state source returned malformed evidence')
        except Exception:
            return self._block(claim, 'robot_state_unavailable')
        evidence_checked_at = self._now()
        if self._clock_predates_action(claim, evidence_checked_at):
            return self._block_clock_rollback(claim, evidence_checked_at)
        if evidence_checked_at >= claim.action.dispatch_expires_at:
            return self._block(claim, 'action_dispatch_expired')
        if evidence.observed_at < claim.action.created_at:
            return self._block(claim, 'robot_state_predates_approval')
        if evidence.observed_at > evidence_checked_at:
            return self._block(claim, 'robot_state_from_future')
        if (
            evidence_checked_at - evidence.observed_at
            > self._maximum_state_age_seconds
        ):
            return self._block(claim, 'robot_state_stale')

        binding = claim.action.binding
        if (
            binding.confirmation_safety_policy_revision
            != self._safety_policy.policy_revision
        ):
            return self._block(claim, 'safety_policy_revision_changed')

        try:
            current_target = self._target_resolver.resolve(location)
            if not isinstance(current_target, BoundNamedTarget):
                raise TypeError('target resolver returned malformed target')
        except Exception:
            return self._block(claim, 'target_unavailable')
        if (
            current_target.binding_digest != binding.target_binding_digest
            or current_target.room_name != binding.target_room_name
            or current_target.room_category != binding.target_room_category
        ):
            return self._block(claim, 'target_binding_changed')

        try:
            safety = self._safety_policy.evaluate_confirmed_action(
                tool_name=binding.tool_name,
                arguments=arguments,
                robot_state=evidence.state,
                state_trusted=evidence.trusted,
            )
            if not isinstance(safety.allowed, bool):
                raise TypeError('safety result is malformed')
        except Exception:
            return self._block(claim, 'safety_evaluation_failed')
        if not safety.allowed:
            return self._block(claim, f'safety_{safety.code}')

        # Sample again after target and policy checks so the evidence is still
        # fresh at the actual durable authorization boundary.
        authorized_at = self._now()
        if self._clock_predates_action(claim, authorized_at):
            return self._block_clock_rollback(claim, authorized_at)
        if authorized_at >= claim.action.dispatch_expires_at:
            return self._block(claim, 'action_dispatch_expired')
        if evidence.observed_at > authorized_at:
            return self._block(claim, 'robot_state_from_future')
        if (
            authorized_at - evidence.observed_at
            > self._maximum_state_age_seconds
        ):
            return self._block(claim, 'robot_state_stale')

        try:
            authorization = DispatchAuthorization(
                state_evidence_id=evidence.evidence_id,
                state_observed_at=evidence.observed_at,
                safety_policy_revision=self._safety_policy.policy_revision,
                target_binding_digest=current_target.binding_digest,
                authorized_at=authorized_at,
                simulation=True,
                physical_authorized=False,
            )
        except (TypeError, ValueError):
            return self._block(claim, 'dispatch_authorization_invalid')
        intent = self._repository.record_dispatch_intent(
            claim,
            authorization,
            now=authorized_at,
        )
        return self._start_and_observe(intent, prepared)

    def _validated_arguments(
        self,
        claim: ActionClaim,
    ) -> Optional[dict]:
        try:
            return validate_tool_arguments(
                claim.action.binding.tool_name,
                claim.action.binding.arguments_dict(),
            )
        except (ValidationError, TypeError, ValueError):
            return None

    def _start_and_observe(
        self,
        intent: DispatchIntent,
        prepared: object,
    ) -> RobotAction:
        try:
            handle = self._executor.start(
                prepared,
                committed_intent_id=intent.intent_id,
            )
        except ApprovedNavigationOutcomeUnknown:
            return self._finish(
                intent,
                ActionState.UNKNOWN,
                'navigation_start_outcome_unknown',
            )
        except ApprovedNavigationRejected:
            return self._finish(
                intent,
                ActionState.FAILED,
                'navigation_start_rejected',
            )
        except ApprovedNavigationError as error:
            state = (
                ActionState.FAILED
                if error.outcome_known
                else ActionState.UNKNOWN
            )
            return self._finish(
                intent,
                state,
                (
                    'navigation_start_failed'
                    if error.outcome_known
                    else 'navigation_start_outcome_unknown'
                ),
            )
        except Exception:
            # Once start has been attempted, an untyped failure is ambiguous.
            return self._finish(
                intent,
                ActionState.UNKNOWN,
                'navigation_start_outcome_unknown',
            )

        # If this persistence operation fails, the durable dispatch intent
        # remains the crash-recovery source of truth.  It is never safe to
        # invoke start again in this process.
        intent = self._repository.mark_started(intent, now=self._now())
        return self._observe_bounded(intent, handle)

    def _observe_bounded(
        self,
        intent: DispatchIntent,
        handle: object,
    ) -> RobotAction:
        deadline = (
            self._monotonic_now() + self._status_deadline_seconds
        )
        for poll_index in range(self._maximum_status_polls):
            if self._monotonic_now() >= deadline:
                return self._finish(
                    intent,
                    ActionState.UNKNOWN,
                    'navigation_status_deadline_exceeded',
                )
            try:
                status = self._executor.status(handle)
            except Exception:
                # A read failure says nothing reliable about an operation
                # that was already accepted.  Only an exact terminal status
                # may turn STARTED into success, failure, or cancellation.
                return self._finish(
                    intent,
                    ActionState.UNKNOWN,
                    'navigation_status_outcome_unknown',
                )
            try:
                terminal = _terminal_result(status)
            except (TypeError, ValueError):
                return self._finish(
                    intent,
                    ActionState.UNKNOWN,
                    'navigation_status_outcome_unknown',
                )
            if terminal is not None:
                state, result_code = terminal
                return self._finish(intent, state, result_code)
            if poll_index + 1 < self._maximum_status_polls:
                remaining = deadline - self._monotonic_now()
                if remaining <= 0:
                    return self._finish(
                        intent,
                        ActionState.UNKNOWN,
                        'navigation_status_deadline_exceeded',
                    )
                try:
                    self._sleeper(min(
                        self._status_poll_interval_seconds,
                        remaining,
                    ))
                except Exception:
                    return self._finish(
                        intent,
                        ActionState.UNKNOWN,
                        'navigation_status_outcome_unknown',
                    )

        return self._finish(
            intent,
            ActionState.UNKNOWN,
            'navigation_status_poll_limit',
        )

    def _block(self, claim: ActionClaim, result_code: str) -> RobotAction:
        return self._repository.block(
            claim,
            result_code=result_code,
            now=self._now(),
        )

    @staticmethod
    def _clock_predates_action(
        claim: ActionClaim,
        timestamp: float,
    ) -> bool:
        """Return whether a sampled wall clock predates durable action time."""
        return timestamp < max(
            claim.action.created_at,
            claim.action.updated_at,
        )

    def _block_clock_rollback(
        self,
        claim: ActionClaim,
        observed_now: float,
    ) -> RobotAction:
        """Block with a timestamp that cannot violate domain chronology."""
        return self._repository.block(
            claim,
            result_code='action_clock_rollback',
            now=max(
                observed_now,
                claim.action.created_at,
                claim.action.updated_at,
            ),
        )

    def _finish(
        self,
        intent: DispatchIntent,
        state: ActionState,
        result_code: str,
    ) -> RobotAction:
        return self._repository.finish(
            intent,
            state,
            result_code=result_code,
            now=self._now(),
        )

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError('clock returned an invalid timestamp')
        return float(value)

    def _monotonic_now(self) -> float:
        value = self._monotonic_clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError(
                'monotonic_clock returned an invalid timestamp'
            )
        return float(value)


class ApprovedActionWorkerRuntime:
    """
    Own one non-daemon polling thread around ``run_once``.

    Restart recovery completes before the first claim.  Later recovery errors
    put the live thread into recovery-only mode until the ledger is healthy.
    Closing only signals the loop; callers retain explicit ownership of the
    bounded ``join`` before closing SQLite or adapters.
    """

    def __init__(
        self,
        worker: ApprovedActionWorker,
        *,
        idle_wait_seconds: float = 0.25,
        startup_wait_seconds: float = 5.0,
    ) -> None:
        """Create a stopped runtime with bounded idle and startup waits."""
        if not isinstance(worker, ApprovedActionWorker):
            raise TypeError('worker must be ApprovedActionWorker')
        self._worker = worker
        self._idle_wait_seconds = _positive_finite(
            idle_wait_seconds,
            'idle_wait_seconds',
        )
        self._startup_wait_seconds = _positive_finite(
            startup_wait_seconds,
            'startup_wait_seconds',
        )
        self._condition = threading.Condition()
        self._closing = False
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[Exception] = None
        self._startup_complete = False
        self._startup_ready = False

    @property
    def last_error(self) -> Optional[Exception]:
        """Expose the last loop error without raising across the thread."""
        with self._condition:
            return self._last_error

    @property
    def is_alive(self) -> bool:
        """Return whether the owned worker thread is still running."""
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    @property
    def is_ready(self) -> bool:
        """Return whether recovery is healthy enough to claim work."""
        with self._condition:
            return self._startup_ready

    def start(self) -> None:
        """Start only after restart recovery succeeds within the bound."""
        with self._condition:
            if self._thread is not None:
                raise RuntimeError('approved action runtime already started')
            if self._closing:
                raise RuntimeError('approved action runtime is closed')
            self._thread = threading.Thread(
                target=self._run,
                name='malbut-approved-action-worker',
                daemon=False,
            )
            self._thread.start()
            deadline = time.monotonic() + self._startup_wait_seconds
            while not self._startup_complete:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = TimeoutError(
                        'approved action startup recovery timed out'
                    )
                    self._last_error = error
                    self._startup_complete = True
                    self._startup_ready = False
                    self._closing = True
                    self._condition.notify_all()
                    raise error
                self._condition.wait(timeout=remaining)
            if not self._startup_ready:
                self._closing = True
                error = self._last_error
                if error is None:
                    error = RuntimeError(
                        'approved action startup recovery did not complete'
                    )
                    self._last_error = error
                raise RuntimeError(
                    'approved action startup recovery failed'
                ) from error

    def wake(self) -> None:
        """Wake an idle worker after an approval transaction commits."""
        with self._condition:
            self._condition.notify_all()

    def close(self) -> None:
        """Signal shutdown without hiding the caller-owned join step."""
        with self._condition:
            self._closing = True
            self._condition.notify_all()

    def join(self, timeout: Optional[float] = None) -> bool:
        """Join the owned thread and report whether it fully stopped."""
        with self._condition:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        try:
            self._worker.recover_uncertain_after_restart()
        except Exception as error:
            with self._condition:
                if not self._startup_complete:
                    self._last_error = error
                    self._startup_complete = True
                    self._startup_ready = False
                self._condition.notify_all()
            return

        with self._condition:
            # ``start`` may already have timed out while an adapter recovery
            # call was blocked.  Such a runtime must never become ready or
            # proceed to its first claim after the caller observed failure.
            if self._startup_complete:
                return
            self._startup_complete = True
            self._startup_ready = True
            self._condition.notify_all()
            if self._closing:
                return

        while True:
            with self._condition:
                if self._closing:
                    return
            try:
                result = self._worker.run_once()
            except Exception as error:
                with self._condition:
                    self._last_error = error
                result = None
            if result is not None:
                continue
            with self._condition:
                if self._closing:
                    return
                self._condition.wait(timeout=self._idle_wait_seconds)
                if self._closing:
                    return
            # An old process may have held an unexpired sent lease during the
            # startup recovery pass.  Reconcile again before the next idle
            # claim so it becomes UNKNOWN once that lease actually expires.
            while True:
                try:
                    self._worker.recover_uncertain_after_restart()
                except Exception as error:
                    with self._condition:
                        self._last_error = error
                        self._startup_ready = False
                        if self._closing:
                            return
                        self._condition.wait(
                            timeout=self._idle_wait_seconds,
                        )
                        if self._closing:
                            return
                    # Stay alive but perform recovery only.  No action may be
                    # claimed while the durable ledger is unhealthy.
                    continue
                with self._condition:
                    if self._closing:
                        return
                    self._startup_ready = True
                break


def _terminal_result(
    status: ApprovedNavigationStatus,
) -> Optional[tuple[ActionState, str]]:
    if not isinstance(status, ApprovedNavigationStatus):
        raise TypeError('executor status must be ApprovedNavigationStatus')
    if not status.terminal:
        return None
    normalized = status.state.casefold()
    if normalized in _SUCCESS_STATES:
        return (
            ActionState.SUCCEEDED,
            _safe_result_code(status.result_code, 'navigation_succeeded'),
        )
    if normalized in _CANCELED_STATES:
        return (
            ActionState.CANCELED,
            _safe_result_code(status.result_code, 'navigation_canceled'),
        )
    if normalized in _FAILED_STATES:
        return (
            ActionState.FAILED,
            _safe_result_code(status.result_code, 'navigation_failed'),
        )
    return (ActionState.UNKNOWN, 'navigation_terminal_state_unknown')


def _safe_result_code(value: Optional[str], fallback: str) -> str:
    if (
        isinstance(value, str)
        and value
        and len(value) <= 128
        and not any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        return value
    return fallback


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{field_name} must be a string')
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        )
    ):
        raise ValueError(f'{field_name} is invalid')
    return normalized


def _positive_finite(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
        or value > 3600
    ):
        raise ValueError(f'{field_name} is invalid')
    return float(value)


__all__ = ['ApprovedActionWorker', 'ApprovedActionWorkerRuntime']
