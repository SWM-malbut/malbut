"""
Server-internal authority for one approved Gazebo room simulation.

The public confirmation surfaces remain non-authorizing.  This module is a
private server boundary which re-reads an immutable resolved confirmation,
authenticates current Homecam semantics, and issues only the proof consumed by
the durable simulation ledger.  It does not import ROS or perform I/O while it
is being configured.
"""

import hashlib
import hmac
import json
import math
import re
import sqlite3
import threading
import time
from dataclasses import replace
from typing import Any, Callable, Protocol, Tuple
import weakref

from malbut_agent_server.conversation import (
    DurableConfirmationIntent,
    SQLiteConversationStore,
)
from malbut_agent_server.execution_ledger import (
    SIMULATION_ASSURANCE_LEVEL,
    SimulationAssuranceError,
    SimulationConsumeRequest,
    VerifiedSimulationApproval,
)
from malbut_agent_server.homecam_semantic import (
    VerifiedSemanticSnapshotEvidence,
)
from malbut_agent_server.gazebo_execution_outbox import (
    GazeboSimulationConsumeResult,
)
from malbut_agent_server.monitor_room_target import TargetBinding
from malbut_agent_server.schemas import ValidationError, validate_user_id


_LOWER_SHA256 = re.compile(r'[0-9a-f]{64}')
_PROOF_CONTRACT = 'malbut-server-gazebo-simulation-authority-v1'
_PRINCIPAL_CONTRACT = 'malbut-server-confirmation-principal-v1'
_CONSUME_ID_CONTRACT = 'malbut-server-gazebo-consume-id-v1'
_VERIFIER_SEAL_LOCK = threading.RLock()
_VERIFIER_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)
_CONSUMER_SEAL_LOCK = threading.RLock()
_CONSUMER_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)


class GazeboSimulationSemanticEvidenceSource(Protocol):
    """Return resolver-issued, signed Homecam semantic evidence."""

    def fetch_snapshot_evidence(
        self,
    ) -> VerifiedSemanticSnapshotEvidence:
        """Fetch one current authenticated semantic projection."""
        ...


def _canonical_json(value: Any) -> bytes:
    """Encode a bounded internal value for stable hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')


def _sha256(value: Any) -> str:
    """Return the canonical JSON SHA-256 digest."""
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _wall_time(value: Any) -> float:
    """Normalize one trusted wall-clock observation."""
    invalid = False
    normalized = 0.0
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            invalid = True
        else:
            normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        invalid = True
    if invalid or not math.isfinite(normalized) or normalized < 0:
        raise SimulationAssuranceError(
            'trusted simulation time is invalid'
        )
    return 0.0 if normalized == 0 else normalized


def _digest(value: Any) -> bool:
    """Return whether ``value`` is one exact lowercase SHA-256 string."""
    return type(value) is str and _LOWER_SHA256.fullmatch(value) is not None


def _principal_binding(record: DurableConfirmationIntent) -> str:
    """Bind the exact durable actor response and confirmation identity."""
    return _sha256(
        {
            'contract': _PRINCIPAL_CONTRACT,
            'user_id': record.user_id,
            'response_channel': record.response_channel,
            'assurance_level': record.assurance_level,
            'provenance_ref': record.provenance_ref,
            'verifier_ref': record.verifier_ref,
            'confirmation': {
                'schema_version': record.schema_version,
                'confirmation_request_id': (
                    record.confirmation_request_id
                ),
                'confirmation_result_id': record.confirmation_result_id,
                'response_id': record.response_id,
                'response_fingerprint': record.response_fingerprint,
                'decision_id': record.decision_id,
                'proposal_fingerprint': record.proposal_fingerprint,
                'arguments_digest': record.arguments_digest,
                'target_binding_digest': record.target_binding_digest,
                'effects_digest': record.effects_digest,
                'issued_at': record.issued_at,
                'resolved_at': record.resolved_at,
                'expires_at': record.expires_at,
            },
        }
    )


def _consume_request_id(record: DurableConfirmationIntent) -> str:
    """Derive the restart-stable consume-once identifier."""
    digest = _sha256(
        {
            'contract': _CONSUME_ID_CONTRACT,
            'user_id': record.user_id,
            'confirmation_request_id': record.confirmation_request_id,
            'confirmation_result_id': record.confirmation_result_id,
            'proposal_fingerprint': record.proposal_fingerprint,
            'target_binding_digest': record.target_binding_digest,
            'effects_digest': record.effects_digest,
        }
    )
    return f'gazebo-simulation-consume-{digest}'


def _proof_payload(
    approval: VerifiedSimulationApproval,
    request: SimulationConsumeRequest,
) -> bytes:
    """Bind every ledger-visible actor, target, effect, and time field."""
    return _canonical_json(
        {
            'contract': _PROOF_CONTRACT,
            'approval_binding_digest': approval.binding_digest,
            'consume_fingerprint': request.consume_fingerprint,
            'owner_user_id': approval.user_id,
            'principal_binding_digest': (
                approval.principal_binding_digest
            ),
            'target_binding_digest': request.current_target.binding_digest,
            'effects_digest': request.current_target.effects_digest,
            'verified_at': approval.verified_at,
            'approval_expires_at': approval.expires_at,
            'target_observed_at': request.target_observed_at,
            'target_evidence_expires_at': (
                request.target_evidence_expires_at
            ),
        }
    )


def _require_resolved_approval(
    record: Any,
    *,
    user_id: str,
) -> DurableConfirmationIntent:
    """Reject every non-current-shape or non-approved intent uniformly."""
    if (
        type(record) is not DurableConfirmationIntent
        or record.schema_version != 3
        or record.user_id != user_id
        or record.tool_name != 'monitor_room'
        or record.state != 'resolved'
        or record.disposition != 'approve'
        or record.requested_disposition != 'approve'
        or record.result_code
        != 'confirmation_approval_recorded_no_execution'
        or record.confirmation_result_id is None
        or record.response_id is None
        or not _digest(record.response_fingerprint)
        or record.response_channel not in {'voice', 'ui_in_process'}
        or record.assurance_level
        not in {'local_speech_binding', 'unverified_in_process_ui'}
        or (
            record.response_channel == 'voice'
            and record.assurance_level != 'local_speech_binding'
        )
        or (
            record.response_channel == 'ui_in_process'
            and record.assurance_level != 'unverified_in_process_ui'
        )
        or not _digest(record.provenance_ref)
        or record.verifier_ref is not None
        or record.resolved_at is None
        or not record.issued_at <= record.resolved_at < record.expires_at
    ):
        raise SimulationAssuranceError(
            'approved Gazebo simulation confirmation is required'
        )
    return record


class ServerGazeboSimulationExecutionVerifier:
    """Sealed HMAC trust root for one fixed server principal."""

    __slots__ = (
        '_capability',
        '_clock',
        '_fetch_semantic',
        '_semantic_source',
        '_user_id',
        '__weakref__',
    )

    def __init__(
        self,
        process_capability: bytes,
        *,
        user_id: str,
        semantic_evidence_source: GazeboSimulationSemanticEvidenceSource,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Fix the owner, signed-semantic source, clock, and capability."""
        if (
            type(process_capability) is not bytes
            or len(process_capability) < 32
        ):
            raise ValueError(
                'Gazebo simulation process capability is invalid'
            )
        normalized_user = validate_user_id(user_id)
        fetch = getattr(
            semantic_evidence_source,
            'fetch_snapshot_evidence',
            None,
        )
        if not callable(fetch):
            raise TypeError(
                'semantic_evidence_source must provide '
                'fetch_snapshot_evidence()'
            )
        if not callable(clock):
            raise TypeError('clock must be callable')
        object.__setattr__(self, '_capability', process_capability)
        object.__setattr__(self, '_user_id', normalized_user)
        object.__setattr__(self, '_semantic_source', semantic_evidence_source)
        object.__setattr__(self, '_fetch_semantic', fetch)
        object.__setattr__(self, '_clock', clock)
        with _VERIFIER_SEAL_LOCK:
            _VERIFIER_SEALS[self] = (
                process_capability,
                normalized_user,
                semantic_evidence_source,
                fetch,
                clock,
            )

    def __setattr__(self, _name: str, _value: Any) -> None:
        """Keep the configured trust root sealed for its lifetime."""
        raise AttributeError('Gazebo simulation verifier is sealed')

    def _matches_configuration(
        self,
        *,
        user_id: str,
        semantic_evidence_source: GazeboSimulationSemanticEvidenceSource,
    ) -> bool:
        """Check exact fixed collaborator identities without exposing them."""
        try:
            ServerGazeboSimulationExecutionVerifier._attest_configuration(
                self
            )
            return (
                user_id == self._user_id
                and semantic_evidence_source is self._semantic_source
            )
        except SimulationAssuranceError:
            return False

    def _attest_configuration(self) -> None:
        """Match every current field against an external immutable seal."""
        expected = None
        try:
            with _VERIFIER_SEAL_LOCK:
                expected = _VERIFIER_SEALS.get(self)
            current = (
                object.__getattribute__(self, '_capability'),
                object.__getattribute__(self, '_user_id'),
                object.__getattribute__(self, '_semantic_source'),
                object.__getattribute__(self, '_fetch_semantic'),
                object.__getattribute__(self, '_clock'),
            )
        except Exception:
            expected = None
            current = None
        if (
            type(self) is not ServerGazeboSimulationExecutionVerifier
            or expected is None
            or current is None
            or len(expected) != 5
            or current[0] != expected[0]
            or current[1] != expected[1]
            or current[2] is not expected[2]
            or current[3] is not expected[3]
            or current[4] is not expected[4]
        ):
            raise SimulationAssuranceError(
                'Gazebo simulation authority changed'
            )

    def _current_time(self, ledger_now: Any) -> float:
        """Cross-check ledger time against the fixed server clock."""
        ServerGazeboSimulationExecutionVerifier._attest_configuration(self)
        normalized_ledger = _wall_time(ledger_now)
        try:
            current = _wall_time(self._clock())
        except Exception:
            raise SimulationAssuranceError(
                'trusted simulation time is unavailable'
            ) from None
        if current < normalized_ledger:
            raise SimulationAssuranceError(
                'trusted simulation clock moved backwards'
            )
        return current

    def _sign(
        self,
        approval: VerifiedSimulationApproval,
        request: SimulationConsumeRequest,
    ) -> str:
        """Return one private capability proof for exact immutable DTOs."""
        ServerGazeboSimulationExecutionVerifier._attest_configuration(self)
        return hmac.new(
            self._capability,
            _proof_payload(approval, request),
            hashlib.sha256,
        ).hexdigest()

    def _issue(
        self,
        record: DurableConfirmationIntent,
    ) -> tuple[VerifiedSimulationApproval, SimulationConsumeRequest]:
        """Issue stable proof material from one immutable durable approval."""
        ServerGazeboSimulationExecutionVerifier._attest_configuration(self)
        record = _require_resolved_approval(
            record,
            user_id=self._user_id,
        )
        try:
            target = record.reconstruct_target_binding()
            approval = VerifiedSimulationApproval(
                user_id=self._user_id,
                principal_binding_digest=_principal_binding(record),
                confirmation_request_id=record.confirmation_request_id,
                confirmation_result_id=record.confirmation_result_id,
                proposal_fingerprint=record.proposal_fingerprint,
                verified_at=record.resolved_at,
                expires_at=record.expires_at,
            )
            unsigned = SimulationConsumeRequest(
                consume_request_id=_consume_request_id(record),
                confirmation_request_id=record.confirmation_request_id,
                confirmation_result_id=record.confirmation_result_id,
                proposal_fingerprint=record.proposal_fingerprint,
                current_target=target,
                target_observed_at=record.resolved_at,
                target_evidence_expires_at=record.expires_at,
            )
            request = replace(
                unsigned,
                trust_proof=(
                    ServerGazeboSimulationExecutionVerifier._sign(
                        self, approval, unsigned
                    )
                ),
            )
        except (TypeError, ValueError, ValidationError):
            raise SimulationAssuranceError(
                'approved Gazebo simulation confirmation is invalid'
            ) from None
        return approval, request

    def verify_receipt(
        self,
        approval: VerifiedSimulationApproval,
        request: SimulationConsumeRequest,
        now: float,
    ) -> None:
        """Authenticate exact proof, owner, bindings, and stable times."""
        ServerGazeboSimulationExecutionVerifier._attest_configuration(self)
        normalized_now = (
            ServerGazeboSimulationExecutionVerifier._current_time(
                self, now
            )
        )
        invalid = False
        expected = ''
        try:
            if (
                type(approval) is not VerifiedSimulationApproval
                or type(request) is not SimulationConsumeRequest
                or type(request.current_target) is not TargetBinding
            ):
                raise ValueError
            canonical_target = TargetBinding.from_private_dict(
                request.current_target.to_private_dict()
            )
            canonical_approval = VerifiedSimulationApproval(
                user_id=approval.user_id,
                principal_binding_digest=(
                    approval.principal_binding_digest
                ),
                confirmation_request_id=(
                    approval.confirmation_request_id
                ),
                confirmation_result_id=approval.confirmation_result_id,
                proposal_fingerprint=approval.proposal_fingerprint,
                verified_at=approval.verified_at,
                expires_at=approval.expires_at,
                assurance_level=approval.assurance_level,
                simulation_only=approval.simulation_only,
                physical_authorized=approval.physical_authorized,
            )
            canonical_request = SimulationConsumeRequest(
                consume_request_id=request.consume_request_id,
                confirmation_request_id=request.confirmation_request_id,
                confirmation_result_id=request.confirmation_result_id,
                proposal_fingerprint=request.proposal_fingerprint,
                current_target=canonical_target,
                target_observed_at=request.target_observed_at,
                target_evidence_expires_at=(
                    request.target_evidence_expires_at
                ),
                profile_revision=request.profile_revision,
                profile_digest=request.profile_digest,
            )
            invalid = (
                approval.binding_digest
                != canonical_approval.binding_digest
                or request.current_target.binding_digest
                != canonical_target.binding_digest
                or request.current_target.effects_digest
                != canonical_target.effects_digest
                or request.consume_fingerprint
                != canonical_request.consume_fingerprint
                or approval.user_id != self._user_id
                or approval.assurance_level != SIMULATION_ASSURANCE_LEVEL
                or approval.simulation_only is not True
                or approval.physical_authorized is not False
                or approval.confirmation_request_id
                != request.confirmation_request_id
                or approval.confirmation_result_id
                != request.confirmation_result_id
                or approval.proposal_fingerprint
                != request.proposal_fingerprint
                or request.target_observed_at != approval.verified_at
                or request.target_evidence_expires_at != approval.expires_at
                or normalized_now < approval.verified_at
                or normalized_now >= approval.expires_at
                or not _digest(approval.principal_binding_digest)
                or not _digest(request.trust_proof)
            )
            if not invalid:
                expected = (
                    ServerGazeboSimulationExecutionVerifier._sign(
                        self,
                        canonical_approval,
                        canonical_request,
                    )
                )
        except Exception:
            invalid = True
        if invalid or not hmac.compare_digest(request.trust_proof, expected):
            raise SimulationAssuranceError(
                'trusted Gazebo simulation evidence is invalid'
            ) from None

    def verify(
        self,
        approval: VerifiedSimulationApproval,
        request: SimulationConsumeRequest,
        now: float,
    ) -> TargetBinding:
        """Fetch fresh signed semantics and validate the exact target."""
        ServerGazeboSimulationExecutionVerifier._attest_configuration(self)
        ServerGazeboSimulationExecutionVerifier.verify_receipt(
            self, approval, request, now
        )
        normalized_now = (
            ServerGazeboSimulationExecutionVerifier._current_time(
                self, now
            )
        )
        try:
            raw_evidence = self._fetch_semantic()
            if type(raw_evidence) is not VerifiedSemanticSnapshotEvidence:
                raise ValueError
            evidence = raw_evidence.canonical_copy()
            if normalized_now * 1000.0 >= evidence.expires_at_ms:
                raise ValueError
            target = request.current_target
            if (
                type(target) is not TargetBinding
                or not target.matches_snapshot(evidence.snapshot)
            ):
                raise ValueError
            rooms = tuple(
                room
                for room in evidence.snapshot.rooms
                if room.room_id == target.room_id
            )
            if len(rooms) != 1:
                raise ValueError
            room = rooms[0]
            canonical = TargetBinding(
                device_id=evidence.snapshot.device_id,
                device_binding_revision=(
                    evidence.snapshot.device_binding_revision
                ),
                source_revision=evidence.snapshot.source_revision,
                map_id=evidence.snapshot.map_id,
                map_revision=evidence.snapshot.map_revision,
                semantic_revision=evidence.snapshot.semantic_revision,
                frame_id=evidence.snapshot.frame_id,
                room_id=room.room_id,
                room_name=room.name,
                room_category=room.category,
                source_arguments_digest=target.source_arguments_digest,
                geometry_json=room.geometry_json,
                geometry_digest=room.geometry_digest,
                representative_point=room.representative_point,
                clearance_m=room.clearance_m,
                area_m2=room.area_m2,
                effects=target.effects,
            )
            if (
                canonical.binding_digest != target.binding_digest
                or canonical.effects_digest != target.effects_digest
            ):
                raise ValueError
        except Exception:
            raise SimulationAssuranceError(
                'trusted Gazebo simulation target is not current'
            ) from None
        return canonical


class ServerGazeboSimulationApprovalConsumer:
    """Sealed consumer for one store, owner, verifier, and semantic source."""

    __slots__ = (
        '_semantic_source',
        '_store',
        '_user_id',
        '_verifier',
        '__weakref__',
    )

    def __init__(
        self,
        store: SQLiteConversationStore,
        verifier: ServerGazeboSimulationExecutionVerifier,
        *,
        user_id: str,
        semantic_evidence_source: GazeboSimulationSemanticEvidenceSource,
    ) -> None:
        """Bind the exact server store and already-configured trust root."""
        if type(store) is not SQLiteConversationStore:
            raise TypeError('store must be SQLiteConversationStore')
        if type(verifier) is not ServerGazeboSimulationExecutionVerifier:
            raise TypeError(
                'verifier must be ServerGazeboSimulationExecutionVerifier'
            )
        normalized_user = validate_user_id(user_id)
        if not (
            ServerGazeboSimulationExecutionVerifier
            ._matches_configuration(
                verifier,
                user_id=normalized_user,
                semantic_evidence_source=semantic_evidence_source,
            )
        ):
            raise ValueError('Gazebo simulation authority does not match')
        if (
            object.__getattribute__(
                store,
                '_simulation_execution_verifier',
            )
            is not verifier
        ):
            raise ValueError('store simulation authority does not match')
        object.__setattr__(self, '_store', store)
        object.__setattr__(self, '_verifier', verifier)
        object.__setattr__(self, '_user_id', normalized_user)
        object.__setattr__(self, '_semantic_source', semantic_evidence_source)
        with _CONSUMER_SEAL_LOCK:
            _CONSUMER_SEALS[self] = (
                store,
                verifier,
                normalized_user,
                semantic_evidence_source,
            )

    def __setattr__(self, _name: str, _value: Any) -> None:
        """Keep every authority collaborator fixed for its lifetime."""
        raise AttributeError('Gazebo simulation consumer is sealed')

    def consume(
        self,
        confirmation_request_id: str,
    ) -> GazeboSimulationConsumeResult:
        """Consume one exact approved intent and atomically enqueue Gazebo."""
        ServerGazeboSimulationApprovalConsumer._attest_configuration(self)
        if (
            object.__getattribute__(
                self._store,
                '_simulation_execution_verifier',
            )
            is not self._verifier
            or not (
                ServerGazeboSimulationExecutionVerifier
                ._matches_configuration(
                    self._verifier,
                    user_id=self._user_id,
                    semantic_evidence_source=self._semantic_source,
                )
            )
        ):
            raise SimulationAssuranceError(
                'Gazebo simulation authority changed'
            )
        try:
            record = SQLiteConversationStore.get_confirmation_intent(
                self._store,
                self._user_id,
                confirmation_request_id,
            )
        except sqlite3.Error:
            raise SimulationAssuranceError(
                'Gazebo simulation confirmation is unavailable'
            ) from None
        approval, request = (
            ServerGazeboSimulationExecutionVerifier._issue(
                self._verifier, record
            )
        )
        return (
            SQLiteConversationStore
            .consume_approved_monitor_room_gazebo_simulation(
                self._store,
                approval=approval,
                request=request,
            )
        )

    def _attest_configuration(self) -> None:
        """Match the fixed store and authority against an external seal."""
        expected = None
        try:
            with _CONSUMER_SEAL_LOCK:
                expected = _CONSUMER_SEALS.get(self)
            current = (
                object.__getattribute__(self, '_store'),
                object.__getattribute__(self, '_verifier'),
                object.__getattribute__(self, '_user_id'),
                object.__getattribute__(self, '_semantic_source'),
            )
        except Exception:
            expected = None
            current = None
        if (
            type(self) is not ServerGazeboSimulationApprovalConsumer
            or expected is None
            or current is None
            or len(expected) != 4
            or current[0] is not expected[0]
            or current[1] is not expected[1]
            or current[2] != expected[2]
            or current[3] is not expected[3]
        ):
            raise SimulationAssuranceError(
                'Gazebo simulation authority changed'
            )
        ServerGazeboSimulationExecutionVerifier._attest_configuration(
            current[1]
        )


__all__ = [
    'GazeboSimulationSemanticEvidenceSource',
    'ServerGazeboSimulationApprovalConsumer',
    'ServerGazeboSimulationExecutionVerifier',
]
