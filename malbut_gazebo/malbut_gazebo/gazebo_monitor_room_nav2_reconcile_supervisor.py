"""
Pure supervisor for durable reconciliation of one exact ambiguous Nav2 goal.

This module has no ROS imports and no goal-start capability.  Its transport
surface contains only exact-UUID status observation, exact-UUID get-result,
and an explicitly requested one-shot exact cancellation.  Terminal goal
evidence never rewrites the immutable core operation and never releases the
safety block without a later trusted quiescence proof.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import time
from typing import Any, Mapping

from malbut_gazebo.gazebo_monitor_room_nav2_adapter import (
    Nav2CancelReport,
    Nav2CancelRequest,
    Nav2GoalQuery,
    Nav2GoalReport,
)
from malbut_gazebo.gazebo_monitor_room_nav2_reconcile_store import (
    GazeboMonitorRoomNav2ReconcileConflictError,
    GazeboMonitorRoomNav2ReconcileFenceError,
    GazeboMonitorRoomNav2ReconcileLeaseError,
    GazeboMonitorRoomNav2ReconcileStore,
    Nav2ReconcileObservation,
)
from malbut_gazebo.gazebo_monitor_room_store import GazeboMonitorRoomStore


_REPORT_KEYS = frozenset(
    {
        'operation_id',
        'goal_uuid',
        'binding_digest',
        'fence_epoch',
        'status',
        'evidence_digest',
    }
)
_DIGEST_CHARS = frozenset('0123456789abcdef')
_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_QUIESCENCE_OUTCOMES = frozenset({'quiescent', 'not_quiescent'})
_OBSERVE_EXCEPTION_DIGEST = hashlib.sha256(
    b'malbut-nav2-reconcile-observe-exception-v1'
).hexdigest()
_OBSERVE_MALFORMED_DIGEST = hashlib.sha256(
    b'malbut-nav2-reconcile-observe-malformed-v1'
).hexdigest()
_CANCEL_EXCEPTION_DIGEST = hashlib.sha256(
    b'malbut-nav2-reconcile-cancel-exception-v1'
).hexdigest()
_CANCEL_MALFORMED_DIGEST = hashlib.sha256(
    b'malbut-nav2-reconcile-cancel-malformed-v1'
).hexdigest()
_MISSING_QUIESCENCE_DIGEST = hashlib.sha256(
    b'malbut-nav2-reconcile-quiescence-validator-missing-v1'
).hexdigest()


class GazeboMonitorRoomNav2ReconcileSupervisorError(RuntimeError):
    """Content-free supervisor configuration or clock failure."""


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            f'invalid_{name}'
        )
    return value


def _scoped_attempt_id(value: str, suffix: str) -> str:
    scoped = f'{value}.{suffix}'
    return _identifier(scoped, 'attempt_id')


def _digest(value: Any, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _DIGEST_CHARS for character in value)
    ):
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            f'invalid_{name}'
        )
    return value


def _goal_uuid(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 32
        or value == '0' * 32
        or any(character not in _DIGEST_CHARS for character in value)
    ):
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_goal_uuid'
        )
    return value


def _timestamp(value: Any, name: str) -> float:
    if type(value) not in (int, float):
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            f'invalid_{name}'
        )
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            f'invalid_{name}'
        )
    return 0.0 if normalized == 0.0 else normalized


def _hash_json(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, OverflowError):
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_canonical_value'
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def _boottime() -> float:
    try:
        return _timestamp(
            time.clock_gettime(time.CLOCK_BOOTTIME), 'now'
        )
    except GazeboMonitorRoomNav2ReconcileSupervisorError:
        raise
    except Exception:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'clock_unavailable'
        ) from None


def _same_exact_fields(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    try:
        left_values = vars(left)
        right_values = vars(right)
    except TypeError:
        return False
    if left_values.keys() != right_values.keys():
        return False
    return all(
        type(left_values[name]) is type(right_values[name])  # noqa: E721
        and left_values[name] == right_values[name]
        for name in left_values
    )


def _canonical_query(value: Nav2GoalQuery) -> Nav2GoalQuery:
    if type(value) is not Nav2GoalQuery:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_goal_query'
        )
    try:
        canonical = Nav2GoalQuery(
            operation_id=value.operation_id,
            worker_id=value.worker_id,
            fence_epoch=value.fence_epoch,
            goal_uuid=value.goal_uuid,
            binding_digest=value.binding_digest,
        )
        canonical.request_fingerprint
    except Exception:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_goal_query'
        ) from None
    if not _same_exact_fields(canonical, value):
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'goal_query_changed'
        )
    return canonical


def _canonical_cancel(value: Nav2CancelRequest) -> Nav2CancelRequest:
    if type(value) is not Nav2CancelRequest:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_cancel_request'
        )
    try:
        canonical = Nav2CancelRequest(
            operation_id=value.operation_id,
            worker_id=value.worker_id,
            fence_epoch=value.fence_epoch,
            cancel_request_id=value.cancel_request_id,
            goal_uuid=value.goal_uuid,
            binding_digest=value.binding_digest,
        )
        canonical.request_fingerprint
        canonical.wire_payload_digest
    except Exception:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_cancel_request'
        ) from None
    if not _same_exact_fields(canonical, value):
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'cancel_request_changed'
        )
    return canonical


@dataclass(frozen=True)
class Nav2ReconcileQuiescenceRequest:
    """Exact terminal goal binding presented to a trusted quiet validator."""

    operation_id: str
    worker_id: str
    fence_epoch: int
    goal_uuid: str
    binding_digest: str
    source_anchor_digest: str
    terminal_status: str
    terminal_evidence_digest: str
    terminal_observed_at: float
    quiescence_not_before: float
    runtime_mode: str = field(default='gazebo', init=False)
    use_sim_time: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        """Validate the exact terminal-to-quiescence binding."""
        _identifier(self.operation_id, 'operation_id')
        _identifier(self.worker_id, 'worker_id')
        if type(self.fence_epoch) is not int or self.fence_epoch < 1:
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'invalid_fence_epoch'
            )
        _goal_uuid(self.goal_uuid)
        _digest(self.binding_digest, 'binding_digest')
        _digest(self.source_anchor_digest, 'source_anchor_digest')
        if (
            type(self.terminal_status) is not str
            or self.terminal_status
            not in {'succeeded', 'aborted', 'canceled'}
        ):
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'invalid_terminal_status'
            )
        _digest(
            self.terminal_evidence_digest,
            'terminal_evidence_digest',
        )
        terminal_at = _timestamp(
            self.terminal_observed_at, 'terminal_observed_at'
        )
        not_before = _timestamp(
            self.quiescence_not_before, 'quiescence_not_before'
        )
        if not_before < terminal_at:
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'invalid_quiescence_time'
            )

    @property
    def request_fingerprint(self) -> str:
        """Bind the exact trusted-quiescence request."""
        canonical = _canonical_quiescence_request(self)
        return _hash_json(
            {
                'contract': 'malbut-nav2-reconcile-quiescence-request-v1',
                **vars(canonical),
            }
        )


def _canonical_quiescence_request(
    value: Nav2ReconcileQuiescenceRequest,
) -> Nav2ReconcileQuiescenceRequest:
    if type(value) is not Nav2ReconcileQuiescenceRequest:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_quiescence_request'
        )
    try:
        canonical = Nav2ReconcileQuiescenceRequest(
            operation_id=value.operation_id,
            worker_id=value.worker_id,
            fence_epoch=value.fence_epoch,
            goal_uuid=value.goal_uuid,
            binding_digest=value.binding_digest,
            source_anchor_digest=value.source_anchor_digest,
            terminal_status=value.terminal_status,
            terminal_evidence_digest=value.terminal_evidence_digest,
            terminal_observed_at=value.terminal_observed_at,
            quiescence_not_before=value.quiescence_not_before,
        )
    except Exception:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_quiescence_request'
        ) from None
    if not _same_exact_fields(canonical, value):
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'quiescence_request_changed'
        )
    return canonical


@dataclass(frozen=True)
class Nav2ReconcileQuiescenceValidation:
    """Exact result from the closed trusted quiescence authority seam."""

    operation_id: str
    goal_uuid: str
    binding_digest: str
    fence_epoch: int
    request_fingerprint: str
    checked_at: float
    outcome: str
    evidence_digest: str

    def __post_init__(self) -> None:
        """Validate a bounded quiescence result without raw content."""
        _identifier(self.operation_id, 'operation_id')
        _goal_uuid(self.goal_uuid)
        _digest(self.binding_digest, 'binding_digest')
        if type(self.fence_epoch) is not int or self.fence_epoch < 1:
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'invalid_fence_epoch'
            )
        _digest(self.request_fingerprint, 'request_fingerprint')
        object.__setattr__(
            self, 'checked_at', _timestamp(self.checked_at, 'checked_at')
        )
        if (
            type(self.outcome) is not str
            or self.outcome not in _QUIESCENCE_OUTCOMES
        ):
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'invalid_quiescence_outcome'
            )
        _digest(self.evidence_digest, 'evidence_digest')


def _canonical_quiescence_validation(
    value: Nav2ReconcileQuiescenceValidation,
) -> Nav2ReconcileQuiescenceValidation:
    if type(value) is not Nav2ReconcileQuiescenceValidation:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_quiescence_validation'
        )
    try:
        canonical = Nav2ReconcileQuiescenceValidation(
            operation_id=value.operation_id,
            goal_uuid=value.goal_uuid,
            binding_digest=value.binding_digest,
            fence_epoch=value.fence_epoch,
            request_fingerprint=value.request_fingerprint,
            checked_at=value.checked_at,
            outcome=value.outcome,
            evidence_digest=value.evidence_digest,
        )
    except Exception:
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'invalid_quiescence_validation'
        ) from None
    if not _same_exact_fields(canonical, value):
        raise GazeboMonitorRoomNav2ReconcileSupervisorError(
            'quiescence_validation_changed'
        )
    return canonical


class Nav2ReconcileTransport(ABC):
    """Narrow transport with no navigation-goal start operation."""

    @abstractmethod
    def observe_status(self, query: Nav2GoalQuery):
        """Read exact-UUID status without causing navigation."""

    @abstractmethod
    def get_result(self, query: Nav2GoalQuery):
        """Read exact-UUID retained result without causing navigation."""

    @abstractmethod
    def cancel_goal(self, request: Nav2CancelRequest):
        """Issue one exact cancellation only after a durable claim."""


class TrustedNav2ReconcileQuiescenceValidator(ABC):
    """Closed seam for evidence stronger than standard Nav2 absence."""

    @abstractmethod
    def validate_quiescence(
        self,
        request: Nav2ReconcileQuiescenceRequest,
        *,
        checked_at: float,
    ) -> Nav2ReconcileQuiescenceValidation:
        """Prove or reject exact-goal quiescence without a side effect."""


class _FailClosedQuiescenceValidator(
    TrustedNav2ReconcileQuiescenceValidator
):
    """Default authority that can never release a safety block."""

    def validate_quiescence(self, request, *, checked_at):
        return Nav2ReconcileQuiescenceValidation(
            operation_id=request.operation_id,
            goal_uuid=request.goal_uuid,
            binding_digest=request.binding_digest,
            fence_epoch=request.fence_epoch,
            request_fingerprint=request.request_fingerprint,
            checked_at=checked_at,
            outcome='not_quiescent',
            evidence_digest=_MISSING_QUIESCENCE_DIGEST,
        )


_FAIL_CLOSED_QUIESCENCE_VALIDATOR = _FailClosedQuiescenceValidator()


class GazeboMonitorRoomNav2ReconcileSupervisor:
    """Reconcile one immutable core unknown through exact narrow calls."""

    def __init__(
        self,
        core_store: GazeboMonitorRoomStore,
        reconcile_store: GazeboMonitorRoomNav2ReconcileStore,
        transport: Nav2ReconcileTransport,
        *,
        worker_id: str,
        lease_seconds: float = 5.0,
        quiescence_validator=None,
        clock=None,
    ) -> None:
        """Bind collaborators; construction makes no transport call."""
        if type(core_store) is not GazeboMonitorRoomStore:
            raise TypeError('core_store must be GazeboMonitorRoomStore')
        if type(reconcile_store) is not GazeboMonitorRoomNav2ReconcileStore:
            raise TypeError(
                'reconcile_store must be '
                'GazeboMonitorRoomNav2ReconcileStore'
            )
        if not isinstance(transport, Nav2ReconcileTransport):
            raise TypeError('transport must be Nav2ReconcileTransport')
        validator = (
            _FAIL_CLOSED_QUIESCENCE_VALIDATOR
            if quiescence_validator is None
            else quiescence_validator
        )
        if not isinstance(
            validator, TrustedNav2ReconcileQuiescenceValidator
        ):
            raise TypeError(
                'quiescence_validator must be trusted validator'
            )
        normalized_lease = _timestamp(lease_seconds, 'lease_seconds')
        if normalized_lease <= 0.0 or normalized_lease > 300.0:
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'invalid_lease_seconds'
            )
        selected_clock = _boottime if clock is None else clock
        if not callable(selected_clock):
            raise TypeError('clock must be callable')
        if reconcile_store.core_store_namespace != core_store.store_namespace:
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'store_namespace_mismatch'
            )
        self._core_store = core_store
        self._store = reconcile_store
        self._observe_status = transport.observe_status
        self._get_result = transport.get_result
        self._cancel_goal = transport.cancel_goal
        self._validate_quiescence = validator.validate_quiescence
        self._worker_id = _identifier(worker_id, 'worker_id')
        self._lease_seconds = normalized_lease
        self._clock = selected_clock

    def _now(self) -> float:
        try:
            return _timestamp(self._clock(), 'now')
        except GazeboMonitorRoomNav2ReconcileSupervisorError:
            raise
        except Exception:
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'clock_unavailable'
            ) from None

    def _lease(self, operation_id: str):
        observation = self._store.observe(operation_id)
        if observation.state in {
            'released_quiescent', 'blocked_conflict'
        }:
            return None
        return self._store.acquire_lease(
            operation_id,
            worker_id=self._worker_id,
            expected_fence=observation.fence_epoch,
            lease_seconds=self._lease_seconds,
            now=self._now(),
        )

    def _goal_query(self, operation_id, fence_epoch):
        anchor = self._store.source_anchor(operation_id)
        return _canonical_query(
            Nav2GoalQuery(
                operation_id=anchor.operation_id,
                worker_id=self._worker_id,
                fence_epoch=fence_epoch,
                goal_uuid=anchor.goal_uuid,
                binding_digest=anchor.binding_digest,
            )
        )

    def reconcile_once(
        self, operation_id: str, *, attempt_id: str
    ) -> Nav2ReconcileObservation:
        """Perform at most one claimed status read and one claimed result read."""
        normalized_operation = _identifier(operation_id, 'operation_id')
        normalized_attempt = _identifier(attempt_id, 'attempt_id')
        observation = self._store.observe(normalized_operation)
        if observation.state != 'blocked_unresolved':
            return observation
        self._store.assert_source_unchanged(
            self._core_store, normalized_operation
        )
        lease = self._lease(normalized_operation)
        if lease is None:
            return self._store.observe(normalized_operation)
        query = self._goal_query(normalized_operation, lease.fence_epoch)
        try:
            status_observation = self._perform_observe(
                query,
                attempt_id=_scoped_attempt_id(
                    normalized_attempt, 'status'
                ),
                call=self._observe_status,
            )
        except (
            GazeboMonitorRoomNav2ReconcileFenceError,
            GazeboMonitorRoomNav2ReconcileLeaseError,
        ):
            return self._store.observe(normalized_operation)
        if status_observation.state != 'blocked_unresolved':
            return status_observation
        try:
            return self._perform_observe(
                query,
                attempt_id=_scoped_attempt_id(
                    normalized_attempt, 'result'
                ),
                call=self._get_result,
            )
        except (
            GazeboMonitorRoomNav2ReconcileFenceError,
            GazeboMonitorRoomNav2ReconcileLeaseError,
        ):
            return self._store.observe(normalized_operation)

    def _perform_observe(
        self,
        query: Nav2GoalQuery,
        *,
        attempt_id: str,
        call,
    ) -> Nav2ReconcileObservation:
        canonical = _canonical_query(query)
        fingerprint = canonical.request_fingerprint
        claim = self._store.claim_attempt(
            canonical.operation_id,
            attempt_id=attempt_id,
            kind='observe',
            worker_id=canonical.worker_id,
            fence_epoch=canonical.fence_epoch,
            request_fingerprint=fingerprint,
            now=self._now(),
        )
        if not claim.claimed:
            return self._store.observe(canonical.operation_id)
        self._store.assert_source_unchanged(
            self._core_store, canonical.operation_id
        )
        self._store.assert_attempt_current(
            claim.token, now=self._now()
        )
        boundary = _canonical_query(canonical)
        if boundary.request_fingerprint != fingerprint:
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'goal_query_changed'
            )
        try:
            raw_report = call(boundary)
        except Exception:
            return self._record_observe_unknown(
                claim.token, _OBSERVE_EXCEPTION_DIGEST
            )
        try:
            report = self._goal_report(raw_report, boundary)
            if (
                _canonical_query(boundary).request_fingerprint
                != fingerprint
            ):
                raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                    'goal_query_changed'
                )
        except Exception:
            return self._record_observe_unknown(
                claim.token, _OBSERVE_MALFORMED_DIGEST
            )
        self._store.assert_source_unchanged(
            self._core_store, canonical.operation_id
        )
        return self._record_goal_or_discard(
            claim.token,
            status=report.status,
            evidence_digest=report.evidence_digest,
        )

    @staticmethod
    def _goal_report(raw_report, query):
        if type(raw_report) is not dict or (
            frozenset(raw_report.keys()) != _REPORT_KEYS
        ):
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'invalid_goal_report'
            )
        report = Nav2GoalReport(**raw_report)
        canonical = Nav2GoalReport(
            operation_id=report.operation_id,
            goal_uuid=report.goal_uuid,
            binding_digest=report.binding_digest,
            fence_epoch=report.fence_epoch,
            status=report.status,
            evidence_digest=report.evidence_digest,
        )
        if (
            not _same_exact_fields(canonical, report)
            or canonical.operation_id != query.operation_id
            or canonical.goal_uuid != query.goal_uuid
            or canonical.binding_digest != query.binding_digest
            or canonical.fence_epoch != query.fence_epoch
        ):
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'goal_report_mismatch'
            )
        return canonical

    def _record_observe_unknown(self, token, evidence):
        return self._record_goal_or_discard(
            token, status='unknown', evidence_digest=evidence
        )

    def _record_goal_or_discard(
        self, token, *, status, evidence_digest
    ) -> Nav2ReconcileObservation:
        try:
            return self._store.record_goal_observation(
                token,
                status=status,
                evidence_digest=evidence_digest,
                now=self._now(),
            )
        except (
            GazeboMonitorRoomNav2ReconcileFenceError,
            GazeboMonitorRoomNav2ReconcileLeaseError,
        ):
            return self._store.observe(token.operation_id)

    def cancel_once(
        self, operation_id: str, *, cancel_request_id: str
    ) -> Nav2ReconcileObservation:
        """Issue at most one exact cancel for one explicit durable identity."""
        normalized_operation = _identifier(operation_id, 'operation_id')
        normalized_cancel = _identifier(
            cancel_request_id, 'cancel_request_id'
        )
        observation = self._store.observe(normalized_operation)
        if observation.state != 'blocked_unresolved':
            return observation
        self._store.assert_source_unchanged(
            self._core_store, normalized_operation
        )
        lease = self._lease(normalized_operation)
        if lease is None:
            return self._store.observe(normalized_operation)
        anchor = self._store.source_anchor(normalized_operation)
        request = _canonical_cancel(
            Nav2CancelRequest(
                operation_id=anchor.operation_id,
                worker_id=self._worker_id,
                fence_epoch=lease.fence_epoch,
                cancel_request_id=normalized_cancel,
                goal_uuid=anchor.goal_uuid,
                binding_digest=anchor.binding_digest,
            )
        )
        request_fingerprint = request.request_fingerprint
        wire_digest = request.wire_payload_digest
        claim = self._store.claim_attempt(
            normalized_operation,
            attempt_id=normalized_cancel,
            kind='cancel',
            worker_id=self._worker_id,
            fence_epoch=lease.fence_epoch,
            request_fingerprint=request_fingerprint,
            wire_payload_digest=wire_digest,
            now=self._now(),
        )
        if not claim.claimed:
            return self._store.observe(normalized_operation)
        self._store.assert_source_unchanged(
            self._core_store, normalized_operation
        )
        self._store.assert_attempt_current(
            claim.token, now=self._now()
        )
        boundary = _canonical_cancel(request)
        if (
            boundary.request_fingerprint != request_fingerprint
            or boundary.wire_payload_digest != wire_digest
        ):
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'cancel_request_changed'
            )
        try:
            raw_report = self._cancel_goal(boundary)
        except Exception:
            return self._record_cancel_unknown(
                claim.token, _CANCEL_EXCEPTION_DIGEST
            )
        try:
            report = self._cancel_report(raw_report, boundary)
            refreshed = _canonical_cancel(boundary)
            if (
                refreshed.request_fingerprint != request_fingerprint
                or refreshed.wire_payload_digest != wire_digest
            ):
                raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                    'cancel_request_changed'
                )
        except Exception:
            return self._record_cancel_unknown(
                claim.token, _CANCEL_MALFORMED_DIGEST
            )
        self._store.assert_source_unchanged(
            self._core_store, normalized_operation
        )
        return self._record_cancel_or_discard(
            claim.token,
            status=report.status,
            evidence_digest=report.evidence_digest,
        )

    @staticmethod
    def _cancel_report(raw_report, request):
        if type(raw_report) is not dict or (
            frozenset(raw_report.keys()) != _REPORT_KEYS
        ):
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'invalid_cancel_report'
            )
        report = Nav2CancelReport(**raw_report)
        canonical = Nav2CancelReport(
            operation_id=report.operation_id,
            goal_uuid=report.goal_uuid,
            binding_digest=report.binding_digest,
            fence_epoch=report.fence_epoch,
            status=report.status,
            evidence_digest=report.evidence_digest,
        )
        if (
            not _same_exact_fields(canonical, report)
            or canonical.operation_id != request.operation_id
            or canonical.goal_uuid != request.goal_uuid
            or canonical.binding_digest != request.binding_digest
            or canonical.fence_epoch != request.fence_epoch
        ):
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'cancel_report_mismatch'
            )
        return canonical

    def _record_cancel_unknown(self, token, evidence):
        return self._record_cancel_or_discard(
            token, status='unknown', evidence_digest=evidence
        )

    def _record_cancel_or_discard(
        self, token, *, status, evidence_digest
    ) -> Nav2ReconcileObservation:
        try:
            return self._store.record_cancel_observation(
                token,
                status=status,
                evidence_digest=evidence_digest,
                now=self._now(),
            )
        except (
            GazeboMonitorRoomNav2ReconcileFenceError,
            GazeboMonitorRoomNav2ReconcileLeaseError,
        ):
            return self._store.observe(token.operation_id)

    def establish_quiescence_once(
        self, operation_id: str, *, attempt_id: str
    ) -> Nav2ReconcileObservation:
        """Release only after dwell and one exact trusted quiet validation."""
        normalized_operation = _identifier(operation_id, 'operation_id')
        normalized_attempt = _identifier(attempt_id, 'attempt_id')
        observation = self._store.observe(normalized_operation)
        if observation.state != 'blocked_terminal_observed':
            return observation
        if self._now() < observation.quiescence_not_before:
            return observation
        self._store.assert_source_unchanged(
            self._core_store, normalized_operation
        )
        lease = self._lease(normalized_operation)
        if lease is None:
            return self._store.observe(normalized_operation)
        observation = lease.observation
        request = _canonical_quiescence_request(
            Nav2ReconcileQuiescenceRequest(
                operation_id=observation.operation_id,
                worker_id=self._worker_id,
                fence_epoch=lease.fence_epoch,
                goal_uuid=observation.goal_uuid,
                binding_digest=self._store.source_anchor(
                    normalized_operation
                ).binding_digest,
                source_anchor_digest=observation.source_anchor_digest,
                terminal_status=observation.terminal_status,
                terminal_evidence_digest=(
                    observation.terminal_evidence_digest
                ),
                terminal_observed_at=observation.terminal_observed_at,
                quiescence_not_before=observation.quiescence_not_before,
            )
        )
        fingerprint = request.request_fingerprint
        claim = self._store.claim_attempt(
            normalized_operation,
            attempt_id=normalized_attempt,
            kind='quiescence',
            worker_id=self._worker_id,
            fence_epoch=lease.fence_epoch,
            request_fingerprint=fingerprint,
            now=self._now(),
        )
        if not claim.claimed:
            return self._store.observe(normalized_operation)
        self._store.assert_source_unchanged(
            self._core_store, normalized_operation
        )
        boundary_at = self._now()
        self._store.assert_attempt_current(
            claim.token, now=boundary_at
        )
        boundary = _canonical_quiescence_request(request)
        if boundary.request_fingerprint != fingerprint:
            raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                'quiescence_request_changed'
            )
        try:
            validation = self._validate_quiescence(
                boundary, checked_at=boundary_at
            )
            validation = _canonical_quiescence_validation(validation)
            refreshed = _canonical_quiescence_request(boundary)
            if (
                refreshed.request_fingerprint != fingerprint
                or validation.operation_id != boundary.operation_id
                or validation.goal_uuid != boundary.goal_uuid
                or validation.binding_digest != boundary.binding_digest
                or validation.fence_epoch != boundary.fence_epoch
                or validation.request_fingerprint != fingerprint
                or validation.checked_at != boundary_at
            ):
                raise GazeboMonitorRoomNav2ReconcileSupervisorError(
                    'quiescence_validation_mismatch'
                )
        except Exception:
            return self._store.observe(normalized_operation)
        if validation.outcome != 'quiescent':
            return self._store.observe(normalized_operation)
        self._store.assert_source_unchanged(
            self._core_store, normalized_operation
        )
        try:
            return self._store.record_quiescence(
                claim.token,
                evidence_digest=validation.evidence_digest,
                now=self._now(),
            )
        except (
            GazeboMonitorRoomNav2ReconcileFenceError,
            GazeboMonitorRoomNav2ReconcileLeaseError,
            GazeboMonitorRoomNav2ReconcileConflictError,
        ):
            return self._store.observe(normalized_operation)
