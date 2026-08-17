"""
Durable Gazebo-only execution handoff for approved room monitoring.

The conversation database is the authority for creating this outbox.  One
row and its ordered, coordinate-bearing child rows are appended in the same
SQLite transaction that freshly consumes an approved confirmation.  Public
enqueue receipts stay coordinate-free; only the privileged leased claim
contains the exact data required to construct Gazebo ``PrepareOperation``.

This module deliberately does not import or call Gazebo, ROS, Nav2, camera,
network, or gateway code.  Its authority is simulation-only and every stored
record fixes ``physical_authorized`` and ``physical_effects`` to false.
"""

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import threading
import unicodedata
import uuid
import weakref
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from malbut_agent_server.execution_ledger import (
    DurableSimulationExecution,
    SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL,
)
from malbut_agent_server.homecam_semantic import (
    VerifiedSemanticSnapshotEvidence,
)
from malbut_agent_server.monitor_room_coverage import (
    CoveragePlan,
    CoverageSample,
    DEFAULT_COVERAGE_PROFILE,
)
from malbut_agent_server.monitor_room_target import TargetBinding
from malbut_agent_server.robot_state import (
    GazeboSimulationAdmissionEvidence,
    GazeboSimulationStateEvidence,
    ServerGazeboSimulationAdmissionSource,
    TrustedRobotStateEvidence,
    TrustedRobotStateError,
    TrustedRobotStateSource,
    trusted_boottime_ns,
)
from malbut_agent_server.schemas import ValidationError, validate_user_id


GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION = 1
GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES = DEFAULT_COVERAGE_PROFILE.max_samples
GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS = 8
GAZEBO_EXECUTION_OUTBOX_MIN_LEASE_SECONDS = 1
GAZEBO_EXECUTION_OUTBOX_MAX_LEASE_SECONDS = 300
GAZEBO_EXECUTION_OUTBOX_ACTIVATION_SENTINEL = hashlib.sha256(
    b'malbut-gazebo-execution-outbox-activation-v1'
).hexdigest()

_NANOSECONDS_PER_SECOND = 1_000_000_000
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_CLAIM_TOKEN = re.compile(r'^[A-Za-z0-9_-]{32,128}$')
_POLICY_SEAL_LOCK = threading.RLock()
_POLICY_SEALS: 'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]' = (
    weakref.WeakKeyDictionary()
)


class GazeboExecutionOutboxError(ValidationError):
    """Base error for the private Gazebo execution handoff."""


class GazeboExecutionOutboxSchemaError(GazeboExecutionOutboxError):
    """Raised when durable Gazebo outbox structure or data drifts."""


class GazeboExecutionOutboxAssuranceError(GazeboExecutionOutboxError):
    """Raised when current trusted semantic/robot evidence is missing."""


class GazeboExecutionOutboxConflictError(GazeboExecutionOutboxError):
    """Raised for idempotency, claim-fence, or acknowledgement conflicts."""


class GazeboExecutionOutboxUpgradeRequiredError(
    GazeboExecutionOutboxError
):
    """Raised when an existing pure receipt cannot be elevated later."""


class GazeboSemanticEvidenceSource(Protocol):
    """Fixed source for one current, authenticated Homecam snapshot."""

    def fetch_snapshot_evidence(
        self,
    ) -> VerifiedSemanticSnapshotEvidence:
        """Return current resolver-issued semantic evidence."""
        ...


@dataclass(frozen=True)
class GazeboExecutionSample:
    """One private fixed-point map sample for a privileged worker."""

    index: int
    polygon_ordinal: int
    row_ordinal: int
    x_mm: int = field(repr=False)
    y_mm: int = field(repr=False)
    frame_id: str = 'map'

    def __post_init__(self) -> None:
        """Reuse the planner's exact fixed-point sample validation."""
        try:
            sample = CoverageSample(
                index=self.index,
                polygon_ordinal=self.polygon_ordinal,
                row_ordinal=self.row_ordinal,
                x_mm=self.x_mm,
                y_mm=self.y_mm,
                frame_id=self.frame_id,
            )
        except (TypeError, ValidationError, ValueError):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution sample is invalid'
            ) from None
        if sample.index >= GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES:
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution sample exceeds the operation bound'
            )

    def to_private_dict(self) -> Dict[str, Any]:
        """Return the exact coordinate-bearing prepare representation."""
        return {
            'index': self.index,
            'polygon_ordinal': self.polygon_ordinal,
            'row_ordinal': self.row_ordinal,
            'x_mm': self.x_mm,
            'y_mm': self.y_mm,
            'frame_id': self.frame_id,
        }


@dataclass(frozen=True)
class GazeboExecutionEnqueue:
    """Coordinate-free durable result of one atomic Gazebo enqueue."""

    outbox_id: str
    operation_id: str
    prepare_request_id: str = field(repr=False)
    state: str
    sample_count: int
    created_boottime_ns: int = field(repr=False)
    deadline_boottime_ns: int = field(repr=False)
    replayed: bool = False
    schema_version: int = GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Keep public identity separate from private prepare bindings."""
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION
            or not _valid_prefixed_identifier(
                self.outbox_id, 'gazebo-execution-outbox-'
            )
            or not _valid_prefixed_identifier(
                self.operation_id, 'gazebo-operation-'
            )
            or not _valid_prefixed_identifier(
                self.prepare_request_id, 'gazebo-prepare-'
            )
            or self.state not in {'pending', 'claimed', 'prepared', 'expired'}
            or type(self.sample_count) is not int
            or not 1 <= self.sample_count
            <= GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES
            or not _valid_ns(self.created_boottime_ns, minimum=0)
            or not _valid_ns(self.deadline_boottime_ns, minimum=1)
            or self.deadline_boottime_ns <= self.created_boottime_ns
            or type(self.replayed) is not bool
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution enqueue is invalid'
            )

    def to_public_dict(self) -> Dict[str, Any]:
        """Return a coordinate-free, explicitly simulation-only view."""
        return {
            'schema_version': self.schema_version,
            'outbox_id': self.outbox_id,
            'operation_id': self.operation_id,
            'state': self.state,
            'sample_count': self.sample_count,
            'replayed': self.replayed,
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        }


@dataclass(frozen=True)
class GazeboSimulationConsumeResult:
    """One simulation receipt and its optional fresh Gazebo enqueue."""

    receipt: DurableSimulationExecution
    enqueue: Optional[GazeboExecutionEnqueue]

    def __post_init__(self) -> None:
        """Require a Gazebo enqueue exactly for planned receipts."""
        if type(self.receipt) is not DurableSimulationExecution:
            raise TypeError('receipt must be DurableSimulationExecution')
        if self.enqueue is not None and type(
            self.enqueue
        ) is not GazeboExecutionEnqueue:
            raise TypeError('enqueue must be GazeboExecutionEnqueue')
        if (self.receipt.record_kind == 'planned') != (
            self.enqueue is not None
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo consume result is incomplete'
            )

    def to_public_dict(self) -> Dict[str, Any]:
        """Return the coordinate-free consume and enqueue result."""
        return {
            'simulation_receipt': self.receipt.to_public_dict(),
            'gazebo_execution': (
                None
                if self.enqueue is None
                else self.enqueue.to_public_dict()
            ),
        }


@dataclass(frozen=True)
class GazeboExecutionClaim:
    """Private lease for exact Gazebo prepare materialization."""

    outbox_id: str
    operation_id: str
    prepare_request_id: str
    claim_request_id: str = field(repr=False)
    claim_token: str = field(repr=False)
    claim_fence: int
    attempt_number: int
    robot_id: str = field(repr=False)
    map_id: str = field(repr=False)
    map_revision: str = field(repr=False)
    semantic_revision: str = field(repr=False)
    zones_digest: str = field(repr=False)
    target_binding_digest: str = field(repr=False)
    effects_digest: str = field(repr=False)
    profile_digest: str = field(repr=False)
    plan_digest: str = field(repr=False)
    host_boot_id: str = field(repr=False)
    ordered_semantic_samples: Tuple[GazeboExecutionSample, ...] = field(
        repr=False
    )
    deadline_boottime_ns: int = field(repr=False)
    claimed_boottime_ns: int
    lease_expires_boottime_ns: int
    schema_version: int = GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate the complete private, leased prepare payload."""
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION
            or not _valid_prefixed_identifier(
                self.outbox_id, 'gazebo-execution-outbox-'
            )
            or not _valid_prefixed_identifier(
                self.operation_id, 'gazebo-operation-'
            )
            or not _valid_prefixed_identifier(
                self.prepare_request_id, 'gazebo-prepare-'
            )
            or _identifier(self.claim_request_id, 'claim_request_id')
            != self.claim_request_id
            or type(self.claim_token) is not str
            or not _CLAIM_TOKEN.fullmatch(self.claim_token)
            or type(self.claim_fence) is not int
            or not 1 <= self.claim_fence
            <= GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS
            or self.attempt_number != self.claim_fence
            or type(self.ordered_semantic_samples) is not tuple
            or not 1 <= len(self.ordered_semantic_samples)
            <= GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES
            or any(
                type(sample) is not GazeboExecutionSample
                or sample.index != index
                for index, sample in enumerate(
                    self.ordered_semantic_samples
                )
            )
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution claim is invalid'
            )
        for name in (
            'robot_id',
            'map_id',
            'map_revision',
            'semantic_revision',
        ):
            _identifier(getattr(self, name), name)
        for name in (
            'zones_digest',
            'target_binding_digest',
            'effects_digest',
            'profile_digest',
            'plan_digest',
        ):
            _digest(getattr(self, name), name)
        _canonical_boot_id(self.host_boot_id)
        if (
            not _valid_ns(self.claimed_boottime_ns, minimum=0)
            or not _valid_ns(self.lease_expires_boottime_ns, minimum=1)
            or not _valid_ns(self.deadline_boottime_ns, minimum=1)
            or not self.claimed_boottime_ns
            < self.lease_expires_boottime_ns
            <= self.deadline_boottime_ns
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution claim lease is invalid'
            )

    @property
    def deadline(self) -> float:
        """Return Gazebo's CLOCK_BOOTTIME seconds representation."""
        return self.deadline_boottime_ns / _NANOSECONDS_PER_SECOND

    def to_private_prepare_dict(self) -> Dict[str, Any]:
        """Return the exact private fields consumed by a trusted adapter."""
        return {
            'prepare_request_id': self.prepare_request_id,
            'operation_id': self.operation_id,
            'robot_id': self.robot_id,
            'map_id': self.map_id,
            'map_revision': self.map_revision,
            'semantic_revision': self.semantic_revision,
            'zones_digest': self.zones_digest,
            'target_binding_digest': self.target_binding_digest,
            'effects_digest': self.effects_digest,
            'profile_digest': self.profile_digest,
            'plan_digest': self.plan_digest,
            'host_boot_id': self.host_boot_id,
            'ordered_semantic_samples': [
                sample.to_private_dict()
                for sample in self.ordered_semantic_samples
            ],
            'deadline': self.deadline,
            'runtime_mode': 'gazebo',
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'viewer_live': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
        }


@dataclass(frozen=True)
class GazeboExecutionAcknowledgement:
    """Durable proof that a privileged adapter acknowledged prepare."""

    outbox_id: str
    operation_id: str
    prepare_request_id: str
    prepare_fingerprint: str = field(repr=False)
    claim_fence: int
    prepared_boottime_ns: int
    state: str = 'prepared'
    schema_version: int = GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate one simulation-only prepare acknowledgement."""
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION
            or not _valid_prefixed_identifier(
                self.outbox_id, 'gazebo-execution-outbox-'
            )
            or not _valid_prefixed_identifier(
                self.operation_id, 'gazebo-operation-'
            )
            or not _valid_prefixed_identifier(
                self.prepare_request_id, 'gazebo-prepare-'
            )
            or type(self.prepare_fingerprint) is not str
            or not _HEX_DIGEST.fullmatch(self.prepare_fingerprint)
            or type(self.claim_fence) is not int
            or not 1 <= self.claim_fence
            <= GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS
            or not _valid_ns(self.prepared_boottime_ns, minimum=0)
            or self.state != 'prepared'
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution acknowledgement is invalid'
            )

    def to_public_dict(self) -> Dict[str, Any]:
        """Return identifiers and non-authorizing state only."""
        return {
            'schema_version': self.schema_version,
            'outbox_id': self.outbox_id,
            'operation_id': self.operation_id,
            'state': self.state,
            'claim_fence': self.claim_fence,
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
        }


@dataclass(frozen=True)
class GazeboPreparedExecutionAuthority:
    """Durably rederived selector for one prepared Gazebo operation."""

    confirmation_request_id: str = field(repr=False)
    outbox_id: str = field(repr=False)
    operation_id: str = field(repr=False)
    claim_fence: int
    owner_binding_digest: str = field(repr=False)
    prepare_fingerprint: str = field(repr=False)
    acknowledgement_fingerprint: str = field(repr=False)
    host_boot_id: str = field(repr=False)
    prepared_boottime_ns: int = field(repr=False)
    deadline_boottime_ns: int = field(repr=False)
    execution_scope: str
    schema_version: int = GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION
    runtime_mode: str = field(default='gazebo', init=False)
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)
    physical_effects: bool = field(default=False, init=False)
    viewer_live: bool = field(default=False, init=False)
    camera_coverage_validated: bool = field(default=False, init=False)
    coverage_achieved: bool = field(default=False, init=False)
    _binding_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Validate and bind the exact source, ACK, owner, and deadline."""
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION
            or not _valid_prefixed_identifier(
                self.outbox_id, 'gazebo-execution-outbox-'
            )
            or not _valid_prefixed_identifier(
                self.operation_id, 'gazebo-operation-'
            )
            or type(self.claim_fence) is not int
            or not 1 <= self.claim_fence
            <= GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS
            or not _valid_ns(self.prepared_boottime_ns, minimum=0)
            or not _valid_ns(self.deadline_boottime_ns, minimum=1)
            or self.prepared_boottime_ns >= self.deadline_boottime_ns
            or self.execution_scope not in {'drive', 'observe', 'cancel'}
            or self.runtime_mode != 'gazebo'
            or self.simulation is not True
            or self.physical_authorized is not False
            or self.physical_effects is not False
            or self.viewer_live is not False
            or self.camera_coverage_validated is not False
            or self.coverage_achieved is not False
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Prepared Gazebo execution authority is invalid'
            )
        _identifier(
            self.confirmation_request_id,
            'confirmation_request_id',
        )
        for name in (
            'owner_binding_digest',
            'prepare_fingerprint',
            'acknowledgement_fingerprint',
        ):
            _digest(getattr(self, name), name)
        _canonical_boot_id(self.host_boot_id)
        object.__setattr__(
            self,
            '_binding_digest',
            _canonical_hash(
                {
                    'contract': (
                        'gazebo-prepared-execution-authority-v1'
                    ),
                    'schema_version': self.schema_version,
                    'confirmation_request_id': (
                        self.confirmation_request_id
                    ),
                    'outbox_id': self.outbox_id,
                    'operation_id': self.operation_id,
                    'claim_fence': self.claim_fence,
                    'owner_binding_digest': self.owner_binding_digest,
                    'prepare_fingerprint': self.prepare_fingerprint,
                    'acknowledgement_fingerprint': (
                        self.acknowledgement_fingerprint
                    ),
                    'host_boot_id': self.host_boot_id,
                    'prepared_boottime_ns': self.prepared_boottime_ns,
                    'deadline_boottime_ns': self.deadline_boottime_ns,
                    'execution_scope': self.execution_scope,
                    'runtime_mode': 'gazebo',
                    'simulation': True,
                    'physical_authorized': False,
                    'physical_effects': False,
                    'viewer_live': False,
                    'camera_coverage_validated': False,
                    'coverage_achieved': False,
                }
            ),
        )

    @property
    def binding_digest(self) -> str:
        """Return the durable ACK and owner-bound selector identity."""
        expected = _canonical_hash(
            {
                'contract': 'gazebo-prepared-execution-authority-v1',
                'schema_version': self.schema_version,
                'confirmation_request_id': self.confirmation_request_id,
                'outbox_id': self.outbox_id,
                'operation_id': self.operation_id,
                'claim_fence': self.claim_fence,
                'owner_binding_digest': self.owner_binding_digest,
                'prepare_fingerprint': self.prepare_fingerprint,
                'acknowledgement_fingerprint': (
                    self.acknowledgement_fingerprint
                ),
                'host_boot_id': self.host_boot_id,
                'prepared_boottime_ns': self.prepared_boottime_ns,
                'deadline_boottime_ns': self.deadline_boottime_ns,
                'execution_scope': self.execution_scope,
                'runtime_mode': self.runtime_mode,
                'simulation': self.simulation,
                'physical_authorized': self.physical_authorized,
                'physical_effects': self.physical_effects,
                'viewer_live': self.viewer_live,
                'camera_coverage_validated': (
                    self.camera_coverage_validated
                ),
                'coverage_achieved': self.coverage_achieved,
            }
        )
        if expected != self._binding_digest:
            raise GazeboExecutionOutboxSchemaError(
                'Prepared Gazebo execution authority changed'
            )
        return expected


@dataclass(frozen=True)
class _VerifiedGazeboExecutionContext:
    """Policy-issued current evidence; never accepted from an API caller."""

    robot_id: str
    host_boot_id: str
    semantic_evidence: VerifiedSemanticSnapshotEvidence = field(repr=False)
    robot_state_evidence: (
        TrustedRobotStateEvidence | GazeboSimulationStateEvidence
    ) = field(repr=False)
    semantic_content_sha256: str
    zones_digest: str
    robot_evidence_digest: str
    robot_private_digest: str
    created_boottime_ns: int
    deadline_boottime_ns: int
    _policy_token: object = field(repr=False, compare=False)


def _identifier(value: Any, field_name: str) -> str:
    if type(value) is not str or not _SAFE_IDENTIFIER.fullmatch(value):
        raise GazeboExecutionOutboxAssuranceError(
            f'{field_name} is invalid'
        )
    return value


def _digest(value: Any, field_name: str) -> str:
    if type(value) is not str or not _HEX_DIGEST.fullmatch(value):
        raise GazeboExecutionOutboxAssuranceError(
            f'{field_name} is invalid'
        )
    return value


def _valid_prefixed_identifier(value: Any, prefix: str) -> bool:
    return (
        type(value) is str
        and value.startswith(prefix)
        and _SAFE_IDENTIFIER.fullmatch(value) is not None
    )


def _valid_ns(value: Any, *, minimum: int) -> bool:
    return (
        type(value) is int
        and minimum <= value <= _MAX_SQLITE_INTEGER
    )


def _wall_timestamp(value: Any, field_name: str) -> float:
    if type(value) not in (int, float):
        raise GazeboExecutionOutboxAssuranceError(
            f'{field_name} is invalid'
        )
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise GazeboExecutionOutboxAssuranceError(
            f'{field_name} is invalid'
        ) from None
    if not math.isfinite(result) or result < 0:
        raise GazeboExecutionOutboxAssuranceError(
            f'{field_name} is invalid'
        )
    return 0.0 if result == 0 else result


def _canonical_hash(value: Dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('ascii')
    except (OverflowError, TypeError, ValueError):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution payload is not canonical'
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def _canonical_boot_id(value: Any) -> str:
    if type(value) is not str:
        raise GazeboExecutionOutboxAssuranceError(
            'host boot identity is invalid'
        )
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise GazeboExecutionOutboxAssuranceError(
            'host boot identity is invalid'
        ) from None
    if normalized != value.lower():
        raise GazeboExecutionOutboxAssuranceError(
            'host boot identity is invalid'
        )
    return normalized


def _read_local_boot_id() -> str:
    """Read Linux boot identity from the protected kernel interface."""
    try:
        with Path('/proc/sys/kernel/random/boot_id').open(
            'r', encoding='ascii'
        ) as stream:
            value = stream.read(64).strip()
    except (OSError, UnicodeError):
        raise GazeboExecutionOutboxAssuranceError(
            'host boot identity is unavailable'
        ) from None
    return _canonical_boot_id(value)


def _normalized_zone(value: Any) -> str:
    if type(value) is not str:
        return ''
    normalized = unicodedata.normalize('NFKC', value)
    return ' '.join(normalized.split()).casefold()


class GazeboSimulationExecutionPolicy:
    """Fixed composition-root trust policy for one Gazebo deployment."""

    __slots__ = (
        '_robot_id',
        '_expected_device_id',
        '_semantic_evidence_source',
        '_robot_state_source',
        '_simulation_admission_source',
        '_simulation_user_id',
        '_minimum_navigation_battery',
        '_expected_host_boot_id',
        '_boottime_ns',
        '_boot_id_source',
        '_policy_token',
        '__weakref__',
    )

    def __init__(
        self,
        *,
        robot_id: str,
        expected_device_id: str,
        semantic_evidence_source: Optional[
            GazeboSemanticEvidenceSource
        ] = None,
        robot_state_source: Optional[TrustedRobotStateSource] = None,
        simulation_admission_source: Optional[
            ServerGazeboSimulationAdmissionSource
        ] = None,
        minimum_navigation_battery: float = 15.0,
    ) -> None:
        """Capture local boot/clock roots; never accept them per request."""
        self._initialize(
            robot_id=robot_id,
            expected_device_id=expected_device_id,
            semantic_evidence_source=semantic_evidence_source,
            robot_state_source=robot_state_source,
            simulation_admission_source=simulation_admission_source,
            minimum_navigation_battery=minimum_navigation_battery,
            expected_host_boot_id=_read_local_boot_id(),
            boottime_ns=trusted_boottime_ns,
            boot_id_source=_read_local_boot_id,
        )

    @classmethod
    def _for_test(
        cls,
        *,
        robot_id: str,
        expected_device_id: str,
        semantic_evidence_source: GazeboSemanticEvidenceSource,
        robot_state_source: TrustedRobotStateSource,
        expected_host_boot_id: str,
        boottime_ns: Callable[[], int],
        minimum_navigation_battery: float = 15.0,
    ) -> 'GazeboSimulationExecutionPolicy':
        """Provide deterministic trusted roots only to focused tests."""
        instance = cls.__new__(cls)
        instance._initialize(
            robot_id=robot_id,
            expected_device_id=expected_device_id,
            semantic_evidence_source=semantic_evidence_source,
            robot_state_source=robot_state_source,
            simulation_admission_source=None,
            minimum_navigation_battery=minimum_navigation_battery,
            expected_host_boot_id=expected_host_boot_id,
            boottime_ns=boottime_ns,
            boot_id_source=lambda: expected_host_boot_id,
        )
        return instance

    @classmethod
    def _for_gazebo_admission_test(
        cls,
        *,
        robot_id: str,
        expected_device_id: str,
        simulation_admission_source: (
            ServerGazeboSimulationAdmissionSource
        ),
        expected_host_boot_id: str,
        boottime_ns: Callable[[], int],
    ) -> 'GazeboSimulationExecutionPolicy':
        """Compose deterministic non-physical admission in focused tests."""
        instance = cls.__new__(cls)
        instance._initialize(
            robot_id=robot_id,
            expected_device_id=expected_device_id,
            semantic_evidence_source=None,
            robot_state_source=None,
            simulation_admission_source=simulation_admission_source,
            minimum_navigation_battery=15.0,
            expected_host_boot_id=expected_host_boot_id,
            boottime_ns=boottime_ns,
            boot_id_source=lambda: expected_host_boot_id,
        )
        return instance

    def _initialize(
        self,
        *,
        robot_id: str,
        expected_device_id: str,
        semantic_evidence_source: Optional[GazeboSemanticEvidenceSource],
        robot_state_source: Optional[TrustedRobotStateSource],
        simulation_admission_source: Optional[
            ServerGazeboSimulationAdmissionSource
        ],
        minimum_navigation_battery: float,
        expected_host_boot_id: str,
        boottime_ns: Callable[[], int],
        boot_id_source: Callable[[], str],
    ) -> None:
        normalized_robot = _identifier(robot_id, 'robot_id')
        normalized_device = _identifier(
            expected_device_id, 'expected_device_id'
        )
        if normalized_robot != normalized_device:
            raise ValueError(
                'Gazebo robot_id must equal the fixed '
                'Homecam/RobotState device'
            )
        simulation_profile = simulation_admission_source is not None
        if simulation_profile:
            if (
                type(simulation_admission_source)
                is not ServerGazeboSimulationAdmissionSource
                or semantic_evidence_source is not None
                or robot_state_source is not None
            ):
                raise TypeError('simulation admission composition is invalid')
            if (
                simulation_admission_source.expected_device_id
                != normalized_device
                or simulation_admission_source.expected_host_boot_id
                != _canonical_boot_id(expected_host_boot_id)
            ):
                raise ValueError(
                    'simulation admission binding does not match policy'
                )
        else:
            if not callable(
                getattr(
                    semantic_evidence_source,
                    'fetch_snapshot_evidence',
                    None,
                )
            ):
                raise TypeError('semantic_evidence_source is invalid')
            if not callable(getattr(robot_state_source, 'read', None)):
                raise TypeError('robot_state_source is invalid')
        if (
            type(minimum_navigation_battery) not in (int, float)
            or not math.isfinite(float(minimum_navigation_battery))
            or not 0 <= float(minimum_navigation_battery) <= 100
        ):
            raise ValueError('minimum_navigation_battery is invalid')
        if not callable(boottime_ns) or not callable(boot_id_source):
            raise TypeError('trusted boot sources are invalid')
        self._robot_id = normalized_robot
        self._expected_device_id = normalized_device
        self._semantic_evidence_source = semantic_evidence_source
        self._robot_state_source = robot_state_source
        self._simulation_admission_source = simulation_admission_source
        self._simulation_user_id = (
            simulation_admission_source.expected_user_id
            if simulation_admission_source is not None
            else None
        )
        self._minimum_navigation_battery = float(
            minimum_navigation_battery
        )
        self._expected_host_boot_id = _canonical_boot_id(
            expected_host_boot_id
        )
        self._boottime_ns = boottime_ns
        self._boot_id_source = boot_id_source
        self._policy_token = object()
        with _POLICY_SEAL_LOCK:
            _POLICY_SEALS[self] = _POLICY_SEAL_VALUE(self)

    def _seal_value(self) -> Tuple[Any, ...]:
        return (
            self._robot_id,
            self._expected_device_id,
            id(self._semantic_evidence_source),
            id(self._robot_state_source),
            id(self._simulation_admission_source),
            self._simulation_user_id,
            self._minimum_navigation_battery,
            self._expected_host_boot_id,
            id(self._boottime_ns),
            id(self._boot_id_source),
            id(self._policy_token),
        )

    def _require_sealed(self) -> None:
        invalid = False
        try:
            current = _POLICY_SEAL_VALUE(self)
            with _POLICY_SEAL_LOCK:
                expected = _POLICY_SEALS.get(self)
            invalid = (
                type(self) is not GazeboSimulationExecutionPolicy
                or expected is None
                or current != expected
            )
        except Exception:
            invalid = True
        if invalid:
            raise GazeboExecutionOutboxAssuranceError(
                'Gazebo execution policy configuration changed'
            )

    @property
    def robot_id(self) -> str:
        """Return the fixed deployment robot identity."""
        return self._robot_id

    @property
    def expected_host_boot_id(self) -> str:
        """Return the boot identity captured by the trust policy."""
        return self._expected_host_boot_id

    def current_boottime_ns(self) -> int:
        """Read strict deployment BOOTTIME through the captured trust root."""
        _POLICY_REQUIRE_SEALED(self)
        try:
            value = self._boottime_ns()
        except Exception:
            raise GazeboExecutionOutboxAssuranceError(
                'trusted BOOTTIME is unavailable'
            ) from None
        if not _valid_ns(value, minimum=0):
            raise GazeboExecutionOutboxAssuranceError(
                'trusted BOOTTIME is invalid'
            )
        return value

    def current_host_boot_id(self) -> str:
        """Re-read and validate the protected host boot identity."""
        _POLICY_REQUIRE_SEALED(self)
        try:
            value = self._boot_id_source()
        except Exception:
            raise GazeboExecutionOutboxAssuranceError(
                'host boot identity is unavailable'
            ) from None
        current = _canonical_boot_id(value)
        if current != self._expected_host_boot_id:
            raise GazeboExecutionOutboxAssuranceError(
                'host boot identity changed'
            )
        return current

    def _verify_semantic_room(
        self,
        target: TargetBinding,
        evidence: VerifiedSemanticSnapshotEvidence,
    ) -> None:
        snapshot = evidence.snapshot
        if (
            target.device_id != self._expected_device_id
            or snapshot.device_id != self._expected_device_id
            or not target.matches_snapshot(snapshot)
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'fresh semantic evidence changed the target binding'
            )
        rooms = tuple(
            room for room in snapshot.rooms
            if room.room_id == target.room_id
        )
        if len(rooms) != 1:
            raise GazeboExecutionOutboxAssuranceError(
                'fresh semantic evidence no longer contains the room'
            )
        room = rooms[0]
        if (
            room.name != target.room_name
            or room.category != target.room_category
            or room.geometry_json != target.geometry_json
            or room.geometry_digest != target.geometry_digest
            or room.representative_point != target.representative_point
            or room.clearance_m != target.clearance_m
            or room.area_m2 != target.area_m2
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'fresh semantic room geometry changed'
            )

    def _verify_gazebo_admission_for_enqueue(
        self,
        target: TargetBinding,
        *,
        wall_now: float,
    ) -> _VerifiedGazeboExecutionContext:
        """Verify the narrow, non-physical Gazebo admission profile."""
        source = self._simulation_admission_source
        if (
            type(source) is not ServerGazeboSimulationAdmissionSource
            or self._simulation_user_id is None
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'Gazebo simulation admission source is unavailable'
            )
        normalized_wall = _wall_timestamp(wall_now, 'server time')
        start_boottime = _POLICY_CURRENT_BOOTTIME_NS(self)
        try:
            admission = (
                _POLICY_SIMULATION_ADMISSION_ISSUE_FOR_TARGET(
                    source,
                    user_id=self._simulation_user_id,
                    target=target,
                )
            )
        except Exception:
            raise GazeboExecutionOutboxAssuranceError(
                'fresh Gazebo simulation admission is unavailable'
            ) from None
        end_boottime = _POLICY_CURRENT_BOOTTIME_NS(self)
        _POLICY_CURRENT_HOST_BOOT_ID(self)
        if end_boottime < start_boottime:
            raise GazeboExecutionOutboxAssuranceError(
                'trusted BOOTTIME moved backwards'
            )
        if (
            type(admission) is not GazeboSimulationAdmissionEvidence
            or admission.user_id != self._simulation_user_id
            or admission.device_id != self._expected_device_id
            or admission.host_boot_id != self._expected_host_boot_id
            or admission.physical_authority is not False
            or admission.physical_authorized is not False
            or admission.physical_effects is not False
            or not admission.matches_target(target)
            or normalized_wall * 1000.0
            >= admission.semantic_expires_at_ms
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'fresh Gazebo simulation admission changed the target'
            )
        try:
            readiness = admission.require_ready(end_boottime)
            semantic = admission.semantic_evidence.canonical_copy()
            robot = admission.robot_state_evidence
        except Exception:
            raise GazeboExecutionOutboxAssuranceError(
                'fresh Gazebo simulation admission is stale'
            ) from None
        if (
            readiness.navigation_available is not True
            or readiness.localization_ok is not True
            or type(robot) is not GazeboSimulationStateEvidence
            or robot.physical_authority is not False
            or robot.device_id != target.device_id
            or robot.map_id != target.map_id
            or robot.map_revision != target.map_revision
            or robot.host_boot_id != self._expected_host_boot_id
            or semantic.content_sha256
            != admission.semantic_content_sha256
            or semantic.snapshot.zones_digest != admission.zones_digest
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'Gazebo simulation readiness or binding changed'
            )
        effects = target.effects
        if not effects.gazebo_simulation_navigation:
            raise GazeboExecutionOutboxAssuranceError(
                'confirmed effects are not the Gazebo simulation profile'
            )
        duration_ns = effects.max_duration_seconds * _NANOSECONDS_PER_SECOND
        deadline = end_boottime + duration_ns
        if not _valid_ns(deadline, minimum=1):
            raise GazeboExecutionOutboxAssuranceError(
                'Gazebo execution deadline is invalid'
            )
        return _VerifiedGazeboExecutionContext(
            robot_id=self._robot_id,
            host_boot_id=self._expected_host_boot_id,
            semantic_evidence=semantic,
            robot_state_evidence=robot,
            semantic_content_sha256=semantic.content_sha256,
            zones_digest=semantic.snapshot.zones_digest,
            robot_evidence_digest=robot.evidence_digest,
            robot_private_digest=_canonical_hash(robot.to_private_dict()),
            created_boottime_ns=end_boottime,
            deadline_boottime_ns=deadline,
            _policy_token=self._policy_token,
        )

    def verify_for_enqueue(
        self,
        target: TargetBinding,
        *,
        wall_now: float,
    ) -> _VerifiedGazeboExecutionContext:
        """Bind fresh semantic and robot evidence to one fixed operation."""
        if type(target) is not TargetBinding:
            raise GazeboExecutionOutboxAssuranceError(
                'canonical target binding is required'
            )
        _POLICY_REQUIRE_SEALED(self)
        _POLICY_CURRENT_HOST_BOOT_ID(self)
        if self._simulation_admission_source is not None:
            return _POLICY_VERIFY_GAZEBO_ADMISSION(
                self,
                target,
                wall_now=wall_now,
            )
        normalized_wall = _wall_timestamp(wall_now, 'server time')
        start_boottime = _POLICY_CURRENT_BOOTTIME_NS(self)
        try:
            semantic = (
                self._semantic_evidence_source
                .fetch_snapshot_evidence()
            )
        except Exception:
            raise GazeboExecutionOutboxAssuranceError(
                'fresh semantic evidence is unavailable'
            ) from None
        if type(semantic) is not VerifiedSemanticSnapshotEvidence:
            raise GazeboExecutionOutboxAssuranceError(
                'semantic source returned an invalid evidence type'
            )
        try:
            semantic = semantic.canonical_copy()
        except Exception:
            raise GazeboExecutionOutboxAssuranceError(
                'fresh semantic evidence is not canonical'
            ) from None
        if normalized_wall * 1000.0 >= semantic.expires_at_ms:
            raise GazeboExecutionOutboxAssuranceError(
                'fresh semantic evidence expired'
            )
        self._verify_semantic_room(target, semantic)
        try:
            robot = self._robot_state_source.read()
        except Exception:
            raise GazeboExecutionOutboxAssuranceError(
                'fresh robot state evidence is unavailable'
            ) from None
        if type(robot) is not TrustedRobotStateEvidence:
            raise GazeboExecutionOutboxAssuranceError(
                'robot state source returned an invalid evidence type'
            )
        end_boottime = _POLICY_CURRENT_BOOTTIME_NS(self)
        if end_boottime < start_boottime:
            raise GazeboExecutionOutboxAssuranceError(
                'trusted BOOTTIME moved backwards'
            )
        if (
            robot.device_id != self._expected_device_id
            or robot.map_id != target.map_id
            or robot.map_revision != target.map_revision
            or robot.host_boot_id != self._expected_host_boot_id
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'fresh robot state binding changed'
            )
        try:
            state = robot.require_complete_for_monitor_room(end_boottime)
        except (TrustedRobotStateError, TypeError, ValueError):
            raise GazeboExecutionOutboxAssuranceError(
                'fresh robot state is incomplete or stale'
            ) from None
        if (
            state.emergency_stop
            or not state.navigation_available
            or not state.localization_ok
            or state.battery_percent is None
            or state.battery_percent < self._minimum_navigation_battery
            or state.privacy_mode
            or not state.camera_available
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'fresh robot state does not permit room monitoring'
            )
        target_zone_keys = {
            _normalized_zone(target.room_id),
            _normalized_zone(target.room_name),
            _normalized_zone(target.room_category),
        }
        forbidden = {
            _normalized_zone(zone) for zone in state.forbidden_zones
        }
        if (target_zone_keys - {''}) & forbidden:
            raise GazeboExecutionOutboxAssuranceError(
                'fresh robot state forbids the target room'
            )
        effects = target.effects
        if (
            effects.physical_navigation is not True
            or effects.camera_capture is not True
            or effects.external_video_stream is not True
            or effects.video_recording is not False
            or effects.audio_capture is not False
            or effects.talkback_allowed is not False
            or effects.coverage_mode != 'whole_room'
            or effects.viewer_scope != 'requesting_user'
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'confirmed effects are not the Gazebo monitor-room profile'
            )
        duration_ns = effects.max_duration_seconds * _NANOSECONDS_PER_SECOND
        deadline = end_boottime + duration_ns
        if not _valid_ns(deadline, minimum=1):
            raise GazeboExecutionOutboxAssuranceError(
                'Gazebo execution deadline is invalid'
            )
        return _VerifiedGazeboExecutionContext(
            robot_id=self._robot_id,
            host_boot_id=self._expected_host_boot_id,
            semantic_evidence=semantic,
            robot_state_evidence=robot,
            semantic_content_sha256=semantic.content_sha256,
            zones_digest=semantic.snapshot.zones_digest,
            robot_evidence_digest=robot.evidence_digest,
            robot_private_digest=_canonical_hash(
                robot.to_private_dict()
            ),
            created_boottime_ns=end_boottime,
            deadline_boottime_ns=deadline,
            _policy_token=self._policy_token,
        )

    def require_context(
        self,
        context: _VerifiedGazeboExecutionContext,
    ) -> None:
        """Reject contexts not minted by this configured policy instance."""
        _POLICY_REQUIRE_SEALED(self)
        _POLICY_CURRENT_HOST_BOOT_ID(self)
        simulation_profile = self._simulation_admission_source is not None
        expected_robot_type = (
            GazeboSimulationStateEvidence
            if simulation_profile
            else TrustedRobotStateEvidence
        )
        if (
            type(context) is not _VerifiedGazeboExecutionContext
            or context._policy_token is not self._policy_token
            or context.robot_id != self._robot_id
            or context.host_boot_id != self._expected_host_boot_id
            or type(context.semantic_evidence)
            is not VerifiedSemanticSnapshotEvidence
            or type(context.robot_state_evidence)
            is not expected_robot_type
            or context.semantic_content_sha256
            != context.semantic_evidence.content_sha256
            or context.zones_digest
            != context.semantic_evidence.snapshot.zones_digest
            or context.robot_evidence_digest
            != context.robot_state_evidence.evidence_digest
            or not _valid_ns(context.created_boottime_ns, minimum=0)
            or not _valid_ns(context.deadline_boottime_ns, minimum=1)
            or context.deadline_boottime_ns <= context.created_boottime_ns
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'Gazebo execution policy context is invalid'
            )
        try:
            semantic = context.semantic_evidence.canonical_copy()
            robot_private_digest = _canonical_hash(
                context.robot_state_evidence.to_private_dict()
            )
            current_boottime = _POLICY_CURRENT_BOOTTIME_NS(self)
            if simulation_profile:
                readiness = (
                    context.robot_state_evidence
                    .require_ready(current_boottime)
                )
                state = None
            else:
                state = (
                    context.robot_state_evidence
                    .require_complete_for_monitor_room(current_boottime)
                )
                readiness = None
        except Exception:
            raise GazeboExecutionOutboxAssuranceError(
                'Gazebo execution policy evidence changed'
            ) from None
        if (
            semantic.content_sha256 != context.semantic_content_sha256
            or semantic.snapshot.zones_digest != context.zones_digest
            or robot_private_digest != context.robot_private_digest
            or current_boottime < context.created_boottime_ns
            or current_boottime >= context.deadline_boottime_ns
            or (
                simulation_profile
                and (
                    readiness is None
                    or readiness.navigation_available is not True
                    or readiness.localization_ok is not True
                    or context.robot_state_evidence.physical_authority
                    is not False
                )
            )
            or (
                not simulation_profile
                and (
                    state is None
                    or state.emergency_stop
                    or not state.navigation_available
                    or not state.localization_ok
                    or state.privacy_mode
                    or not state.camera_available
                )
            )
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'Gazebo execution policy evidence changed'
            )


# Capture every policy method used at a trust boundary once, at module load.
# Calling these unbound originals prevents an instance attribute or a later
# class monkeypatch from replacing the boot/deadline decision after the
# policy was composed.
_POLICY_SEAL_VALUE = GazeboSimulationExecutionPolicy._seal_value
_POLICY_REQUIRE_SEALED = GazeboSimulationExecutionPolicy._require_sealed
_POLICY_CURRENT_BOOTTIME_NS = (
    GazeboSimulationExecutionPolicy.current_boottime_ns
)
_POLICY_CURRENT_HOST_BOOT_ID = (
    GazeboSimulationExecutionPolicy.current_host_boot_id
)
_POLICY_VERIFY_GAZEBO_ADMISSION = (
    GazeboSimulationExecutionPolicy._verify_gazebo_admission_for_enqueue
)
_POLICY_SIMULATION_ADMISSION_ISSUE_FOR_TARGET = (
    ServerGazeboSimulationAdmissionSource.issue_for_target
)


GAZEBO_OUTBOX_METADATA_TABLE_SQL = '''
CREATE TABLE monitor_room_gazebo_outbox_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    activated_at REAL NOT NULL,
    activation_epoch TEXT NOT NULL,
    preactivation_count INTEGER NOT NULL,
    preactivation_digest TEXT NOT NULL,
    CHECK (
        typeof(activated_at) IN ('integer', 'real')
        AND activated_at >= 0
        AND activated_at <= 1.7976931348623157e308
    ),
    CHECK (
        length(activation_epoch) = 64
        AND activation_epoch NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        typeof(preactivation_count) = 'integer'
        AND preactivation_count >= 0
    ),
    CHECK (
        length(preactivation_digest) = 64
        AND preactivation_digest NOT GLOB '*[^0-9a-f]*'
    )
)
'''


GAZEBO_OUTBOX_PREACTIVATION_TABLE_SQL = '''
CREATE TABLE monitor_room_gazebo_outbox_preactivation_sources (
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    source_receipt_fingerprint TEXT NOT NULL UNIQUE,
    record_kind TEXT NOT NULL,
    CHECK (
        length(source_receipt_fingerprint) = 64
        AND source_receipt_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (record_kind IN (
        'legacy_unplanned', 'invalidated', 'planned', 'planning_failed'
    ))
)
'''


GAZEBO_OUTBOX_TABLE_SQL = f'''
CREATE TABLE monitor_room_gazebo_execution_outbox (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    outbox_id TEXT NOT NULL PRIMARY KEY,
    outbox_fingerprint TEXT NOT NULL UNIQUE,
    confirmation_request_id TEXT NOT NULL UNIQUE,
    source_receipt_digest TEXT NOT NULL UNIQUE,
    source_consume_fingerprint TEXT NOT NULL,
    source_proposal_fingerprint TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    prepare_request_id TEXT NOT NULL UNIQUE,
    robot_id TEXT NOT NULL,
    map_id TEXT NOT NULL,
    map_revision TEXT NOT NULL,
    semantic_revision TEXT NOT NULL,
    zones_digest TEXT NOT NULL,
    semantic_content_sha256 TEXT NOT NULL,
    semantic_map_generation INTEGER NOT NULL,
    semantic_authorization_generation INTEGER NOT NULL,
    target_binding_digest TEXT NOT NULL,
    source_arguments_digest TEXT NOT NULL,
    geometry_digest TEXT NOT NULL,
    effects_digest TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    component_count INTEGER NOT NULL,
    candidate_upper_bound INTEGER NOT NULL,
    geometry_test_upper_bound INTEGER NOT NULL,
    robot_state_evidence_digest TEXT NOT NULL,
    robot_state_instance_id TEXT NOT NULL,
    robot_state_sequence INTEGER NOT NULL,
    robot_state_valid_until_boottime_ns INTEGER NOT NULL,
    host_boot_id TEXT NOT NULL,
    max_duration_seconds INTEGER NOT NULL,
    created_wall REAL NOT NULL,
    evidence_boottime_ns INTEGER NOT NULL,
    created_boottime_ns INTEGER NOT NULL,
    deadline_boottime_ns INTEGER NOT NULL,
    state TEXT NOT NULL,
    terminal_code TEXT,
    attempt_count INTEGER NOT NULL,
    claim_fence INTEGER NOT NULL,
    current_claim_request_id TEXT,
    current_claim_request_fingerprint TEXT,
    current_claim_token TEXT,
    current_lease_seconds INTEGER,
    claimed_boottime_ns INTEGER,
    lease_expires_boottime_ns INTEGER,
    prepared_boottime_ns INTEGER,
    prepare_fingerprint TEXT,
    last_transition_boottime_ns INTEGER NOT NULL,
    runtime_mode TEXT NOT NULL CHECK (runtime_mode = 'gazebo'),
    simulation INTEGER NOT NULL CHECK (simulation = 1),
    gazebo_execution_authorized INTEGER NOT NULL
        CHECK (gazebo_execution_authorized = 1),
    physical_authorized INTEGER NOT NULL CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL CHECK (physical_effects = 0),
    viewer_live INTEGER NOT NULL CHECK (viewer_live = 0),
    camera_coverage_validated INTEGER NOT NULL
        CHECK (camera_coverage_validated = 0),
    coverage_achieved INTEGER NOT NULL CHECK (coverage_achieved = 0),
    CHECK (
        length(outbox_fingerprint) = 64
        AND outbox_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(source_receipt_digest) = 64
        AND source_receipt_digest NOT GLOB '*[^0-9a-f]*'
        AND length(source_consume_fingerprint) = 64
        AND source_consume_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(source_proposal_fingerprint) = 64
        AND source_proposal_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        operation_id GLOB 'gazebo-operation-*'
        AND operation_id NOT GLOB 'simulation-operation-*'
        AND prepare_request_id GLOB 'gazebo-prepare-*'
        AND outbox_id GLOB 'gazebo-execution-outbox-*'
    ),
    CHECK (
        length(zones_digest) = 64
        AND zones_digest NOT GLOB '*[^0-9a-f]*'
        AND length(semantic_content_sha256) = 64
        AND semantic_content_sha256 NOT GLOB '*[^0-9a-f]*'
        AND length(target_binding_digest) = 64
        AND target_binding_digest NOT GLOB '*[^0-9a-f]*'
        AND length(source_arguments_digest) = 64
        AND source_arguments_digest NOT GLOB '*[^0-9a-f]*'
        AND length(geometry_digest) = 64
        AND geometry_digest NOT GLOB '*[^0-9a-f]*'
        AND length(effects_digest) = 64
        AND effects_digest NOT GLOB '*[^0-9a-f]*'
        AND length(profile_digest) = 64
        AND profile_digest NOT GLOB '*[^0-9a-f]*'
        AND length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
        AND length(robot_state_evidence_digest) = 64
        AND robot_state_evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        typeof(semantic_map_generation) = 'integer'
        AND semantic_map_generation >= 1
        AND typeof(semantic_authorization_generation) = 'integer'
        AND semantic_authorization_generation >= 1
        AND typeof(sample_count) = 'integer'
        AND sample_count BETWEEN 1 AND {GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES}
        AND typeof(component_count) = 'integer'
        AND component_count BETWEEN 1 AND 128
        AND typeof(candidate_upper_bound) = 'integer'
        AND candidate_upper_bound >= sample_count
        AND typeof(geometry_test_upper_bound) = 'integer'
        AND geometry_test_upper_bound >= 1
        AND typeof(robot_state_sequence) = 'integer'
        AND robot_state_sequence >= 0
        AND typeof(max_duration_seconds) = 'integer'
        AND max_duration_seconds BETWEEN 1 AND 3600
    ),
    CHECK (
        typeof(created_wall) IN ('integer', 'real')
        AND created_wall >= 0
        AND created_wall <= 1.7976931348623157e308
        AND typeof(evidence_boottime_ns) = 'integer'
        AND evidence_boottime_ns >= 0
        AND typeof(created_boottime_ns) = 'integer'
        AND created_boottime_ns >= evidence_boottime_ns
        AND typeof(deadline_boottime_ns) = 'integer'
        AND deadline_boottime_ns > created_boottime_ns
        AND deadline_boottime_ns - evidence_boottime_ns
            = max_duration_seconds * 1000000000
        AND typeof(robot_state_valid_until_boottime_ns) = 'integer'
        AND robot_state_valid_until_boottime_ns > created_boottime_ns
        AND typeof(last_transition_boottime_ns) = 'integer'
        AND last_transition_boottime_ns >= created_boottime_ns
    ),
    CHECK (state IN ('pending', 'claimed', 'prepared', 'expired')),
    CHECK (
        (state != 'expired' AND terminal_code IS NULL)
        OR (state = 'expired' AND terminal_code IN (
            'deadline_expired', 'delivery_attempts_exhausted',
            'host_boot_changed'
        ))
    ),
    CHECK (
        typeof(attempt_count) = 'integer'
        AND attempt_count BETWEEN 0 AND {GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS}
        AND typeof(claim_fence) = 'integer'
        AND claim_fence = attempt_count
    ),
    CHECK (
        (attempt_count = 0
         AND current_claim_request_id IS NULL
         AND current_claim_request_fingerprint IS NULL
         AND current_claim_token IS NULL
         AND current_lease_seconds IS NULL
         AND claimed_boottime_ns IS NULL
         AND lease_expires_boottime_ns IS NULL)
        OR
        (attempt_count > 0
         AND current_claim_request_id IS NOT NULL
         AND current_claim_request_fingerprint IS NOT NULL
         AND current_claim_token IS NOT NULL
         AND current_lease_seconds BETWEEN
             {GAZEBO_EXECUTION_OUTBOX_MIN_LEASE_SECONDS}
             AND {GAZEBO_EXECUTION_OUTBOX_MAX_LEASE_SECONDS}
         AND typeof(claimed_boottime_ns) = 'integer'
         AND typeof(lease_expires_boottime_ns) = 'integer'
         AND claimed_boottime_ns >= created_boottime_ns
         AND lease_expires_boottime_ns > claimed_boottime_ns
         AND lease_expires_boottime_ns <= deadline_boottime_ns)
    ),
    CHECK (
        (state != 'prepared'
         AND prepared_boottime_ns IS NULL
         AND prepare_fingerprint IS NULL)
        OR
        (state = 'prepared'
         AND typeof(prepared_boottime_ns) = 'integer'
         AND prepared_boottime_ns >= claimed_boottime_ns
         AND prepared_boottime_ns < lease_expires_boottime_ns
         AND prepared_boottime_ns < deadline_boottime_ns
         AND length(prepare_fingerprint) = 64
         AND prepare_fingerprint NOT GLOB '*[^0-9a-f]*')
    )
)
'''


GAZEBO_OUTBOX_SAMPLES_TABLE_SQL = f'''
CREATE TABLE monitor_room_gazebo_execution_samples (
    outbox_id TEXT NOT NULL,
    sample_index INTEGER NOT NULL,
    polygon_ordinal INTEGER NOT NULL,
    row_ordinal INTEGER NOT NULL,
    x_mm INTEGER NOT NULL,
    y_mm INTEGER NOT NULL,
    frame_id TEXT NOT NULL CHECK (frame_id = 'map'),
    sample_digest TEXT NOT NULL,
    PRIMARY KEY (outbox_id, sample_index),
    FOREIGN KEY (outbox_id)
        REFERENCES monitor_room_gazebo_execution_outbox (outbox_id)
        ON DELETE RESTRICT,
    CHECK (
        typeof(sample_index) = 'integer'
        AND sample_index BETWEEN 0
            AND {GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES - 1}
        AND typeof(polygon_ordinal) = 'integer'
        AND polygon_ordinal >= 0
        AND typeof(row_ordinal) = 'integer'
        AND row_ordinal >= 0
        AND typeof(x_mm) = 'integer'
        AND typeof(y_mm) = 'integer'
        AND length(sample_digest) = 64
        AND sample_digest NOT GLOB '*[^0-9a-f]*'
    )
)
'''


GAZEBO_OUTBOX_CLAIMS_TABLE_SQL = f'''
CREATE TABLE monitor_room_gazebo_execution_claims (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    claim_request_id TEXT NOT NULL PRIMARY KEY,
    claim_request_fingerprint TEXT NOT NULL UNIQUE,
    outbox_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    claim_fence INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    claim_token TEXT NOT NULL UNIQUE,
    lease_seconds INTEGER NOT NULL,
    claimed_boottime_ns INTEGER NOT NULL,
    lease_expires_boottime_ns INTEGER NOT NULL,
    deadline_boottime_ns INTEGER NOT NULL,
    simulation INTEGER NOT NULL CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL CHECK (physical_effects = 0),
    FOREIGN KEY (outbox_id)
        REFERENCES monitor_room_gazebo_execution_outbox (outbox_id)
        ON DELETE RESTRICT,
    UNIQUE (outbox_id, claim_fence),
    CHECK (
        length(claim_request_fingerprint) = 64
        AND claim_request_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND operation_id GLOB 'gazebo-operation-*'
        AND typeof(claim_fence) = 'integer'
        AND claim_fence BETWEEN 1 AND {GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS}
        AND attempt_number = claim_fence
        AND lease_seconds BETWEEN
            {GAZEBO_EXECUTION_OUTBOX_MIN_LEASE_SECONDS}
            AND {GAZEBO_EXECUTION_OUTBOX_MAX_LEASE_SECONDS}
        AND typeof(claimed_boottime_ns) = 'integer'
        AND typeof(lease_expires_boottime_ns) = 'integer'
        AND typeof(deadline_boottime_ns) = 'integer'
        AND claimed_boottime_ns < lease_expires_boottime_ns
        AND lease_expires_boottime_ns <= deadline_boottime_ns
    )
)
'''


GAZEBO_OUTBOX_ACKS_TABLE_SQL = '''
CREATE TABLE monitor_room_gazebo_execution_acknowledgements (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    acknowledgement_id TEXT NOT NULL PRIMARY KEY,
    acknowledgement_fingerprint TEXT NOT NULL UNIQUE,
    outbox_id TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL UNIQUE,
    prepare_request_id TEXT NOT NULL UNIQUE,
    claim_request_id TEXT NOT NULL,
    claim_request_fingerprint TEXT NOT NULL,
    claim_fence INTEGER NOT NULL,
    claim_token TEXT NOT NULL,
    claim_token_digest TEXT NOT NULL,
    prepare_fingerprint TEXT NOT NULL,
    prepared_boottime_ns INTEGER NOT NULL,
    simulation INTEGER NOT NULL CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL CHECK (physical_effects = 0),
    FOREIGN KEY (outbox_id)
        REFERENCES monitor_room_gazebo_execution_outbox (outbox_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (claim_request_id)
        REFERENCES monitor_room_gazebo_execution_claims (claim_request_id)
        ON DELETE RESTRICT,
    CHECK (
        length(acknowledgement_fingerprint) = 64
        AND acknowledgement_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(claim_request_fingerprint) = 64
        AND claim_request_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(claim_token_digest) = 64
        AND claim_token_digest NOT GLOB '*[^0-9a-f]*'
        AND length(prepare_fingerprint) = 64
        AND prepare_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND typeof(claim_fence) = 'integer'
        AND claim_fence >= 1
        AND typeof(prepared_boottime_ns) = 'integer'
        AND prepared_boottime_ns >= 0
    )
)
'''


GAZEBO_OUTBOX_PENDING_INDEX_SQL = '''
CREATE INDEX monitor_room_gazebo_execution_pending_idx
ON monitor_room_gazebo_execution_outbox (
    state, created_boottime_ns, outbox_id
)
'''


GAZEBO_OUTBOX_INSERT_GUARD_SQL = '''
CREATE TRIGGER monitor_room_gazebo_execution_insert_guard
BEFORE INSERT ON monitor_room_gazebo_execution_outbox
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM monitor_room_gazebo_outbox_preactivation_sources AS old
        WHERE old.confirmation_request_id = NEW.confirmation_request_id
    ) THEN RAISE(ABORT, 'Gazebo outbox cannot backfill an old receipt') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM monitor_room_simulation_ledger AS source
        WHERE source.schema_version = 4
          AND source.record_kind = 'planned'
          AND source.confirmation_request_id = NEW.confirmation_request_id
          AND source.receipt_digest = NEW.source_receipt_digest
          AND source.consume_fingerprint = NEW.source_consume_fingerprint
          AND source.proposal_fingerprint = NEW.source_proposal_fingerprint
          AND source.target_binding_digest = NEW.target_binding_digest
          AND source.effects_digest = NEW.effects_digest
          AND source.profile_digest = NEW.profile_digest
          AND source.plan_digest = NEW.plan_digest
          AND source.sample_count = NEW.sample_count
          AND source.component_count = NEW.component_count
          AND source.simulation = 1
          AND source.physical_authorized = 0
          AND source.physical_effects = 0
    ) THEN RAISE(ABORT, 'Gazebo outbox source is invalid') END;
    SELECT CASE WHEN
        NEW.state != 'pending'
        OR NEW.terminal_code IS NOT NULL
        OR NEW.attempt_count != 0
        OR NEW.claim_fence != 0
        OR NEW.current_claim_request_id IS NOT NULL
        OR NEW.current_claim_request_fingerprint IS NOT NULL
        OR NEW.current_claim_token IS NOT NULL
        OR NEW.current_lease_seconds IS NOT NULL
        OR NEW.claimed_boottime_ns IS NOT NULL
        OR NEW.lease_expires_boottime_ns IS NOT NULL
        OR NEW.prepared_boottime_ns IS NOT NULL
        OR NEW.prepare_fingerprint IS NOT NULL
        OR NEW.last_transition_boottime_ns != NEW.created_boottime_ns
    THEN RAISE(ABORT, 'Gazebo outbox initial state is invalid') END;
END
'''


_GAZEBO_OUTBOX_IDENTITY_COLUMNS = (
    'schema_version', 'outbox_id', 'outbox_fingerprint',
    'confirmation_request_id', 'source_receipt_digest',
    'source_consume_fingerprint', 'source_proposal_fingerprint',
    'operation_id', 'prepare_request_id', 'robot_id', 'map_id',
    'map_revision', 'semantic_revision', 'zones_digest',
    'semantic_content_sha256', 'semantic_map_generation',
    'semantic_authorization_generation', 'target_binding_digest',
    'source_arguments_digest', 'geometry_digest', 'effects_digest',
    'profile_digest', 'plan_digest', 'sample_count', 'component_count',
    'candidate_upper_bound', 'geometry_test_upper_bound',
    'robot_state_evidence_digest', 'robot_state_instance_id',
    'robot_state_sequence', 'robot_state_valid_until_boottime_ns',
    'host_boot_id', 'max_duration_seconds', 'created_wall',
    'evidence_boottime_ns', 'created_boottime_ns',
    'deadline_boottime_ns', 'runtime_mode',
    'simulation', 'gazebo_execution_authorized', 'physical_authorized',
    'physical_effects', 'viewer_live', 'camera_coverage_validated',
    'coverage_achieved',
)


GAZEBO_OUTBOX_IDENTITY_NO_UPDATE_SQL = (
    'CREATE TRIGGER monitor_room_gazebo_execution_identity_no_update\n'
    'BEFORE UPDATE ON monitor_room_gazebo_execution_outbox\n'
    'WHEN '
    + '\n   OR '.join(
        f'NEW.{column} IS NOT OLD.{column}'
        for column in _GAZEBO_OUTBOX_IDENTITY_COLUMNS
    )
    + "\nBEGIN\n"
      "    SELECT RAISE(ABORT, 'Gazebo outbox identity is immutable');\n"
      "END"
)


GAZEBO_OUTBOX_TRANSITION_GUARD_SQL = '''
CREATE TRIGGER monitor_room_gazebo_execution_transition_guard
BEFORE UPDATE ON monitor_room_gazebo_execution_outbox
WHEN NOT (
    (OLD.state = 'pending' AND NEW.state = 'claimed'
     AND NEW.attempt_count = 1 AND NEW.claim_fence = 1
     AND NEW.terminal_code IS NULL)
    OR
    (OLD.state = 'claimed' AND NEW.state = 'claimed'
     AND NEW.attempt_count = OLD.attempt_count + 1
     AND NEW.claim_fence = OLD.claim_fence + 1
     AND OLD.lease_expires_boottime_ns <= NEW.claimed_boottime_ns
     AND NEW.terminal_code IS NULL)
    OR
    (OLD.state = 'claimed' AND NEW.state = 'prepared'
     AND NEW.attempt_count = OLD.attempt_count
     AND NEW.claim_fence = OLD.claim_fence
     AND NEW.current_claim_request_id IS OLD.current_claim_request_id
     AND NEW.current_claim_request_fingerprint
         IS OLD.current_claim_request_fingerprint
     AND NEW.current_claim_token IS OLD.current_claim_token
     AND NEW.current_lease_seconds IS OLD.current_lease_seconds
     AND NEW.claimed_boottime_ns IS OLD.claimed_boottime_ns
     AND NEW.lease_expires_boottime_ns
         IS OLD.lease_expires_boottime_ns
     AND NEW.terminal_code IS NULL)
    OR
    (OLD.state IN ('pending', 'claimed') AND NEW.state = 'expired'
     AND NEW.attempt_count = OLD.attempt_count
     AND NEW.claim_fence = OLD.claim_fence
     AND NEW.current_claim_request_id IS OLD.current_claim_request_id
     AND NEW.current_claim_request_fingerprint
         IS OLD.current_claim_request_fingerprint
     AND NEW.current_claim_token IS OLD.current_claim_token
     AND NEW.current_lease_seconds IS OLD.current_lease_seconds
     AND NEW.claimed_boottime_ns IS OLD.claimed_boottime_ns
     AND NEW.lease_expires_boottime_ns
         IS OLD.lease_expires_boottime_ns
     AND NEW.terminal_code IN (
         'deadline_expired', 'delivery_attempts_exhausted',
         'host_boot_changed'
     ))
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo outbox transition is invalid');
END
'''


GAZEBO_OUTBOX_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_execution_no_delete
BEFORE DELETE ON monitor_room_gazebo_execution_outbox
BEGIN
    SELECT RAISE(ABORT, 'Gazebo outbox rows are append-only');
END
'''


GAZEBO_OUTBOX_SAMPLE_INSERT_GUARD_SQL = '''
CREATE TRIGGER monitor_room_gazebo_sample_insert_guard
BEFORE INSERT ON monitor_room_gazebo_execution_samples
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM monitor_room_gazebo_execution_outbox AS event
        WHERE event.outbox_id = NEW.outbox_id
          AND event.state = 'pending'
          AND event.attempt_count = 0
          AND NEW.sample_index < event.sample_count
    ) THEN RAISE(ABORT, 'Gazebo sample source is invalid') END;
END
'''


GAZEBO_OUTBOX_SAMPLE_NO_UPDATE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_sample_no_update
BEFORE UPDATE ON monitor_room_gazebo_execution_samples
BEGIN
    SELECT RAISE(ABORT, 'Gazebo samples are immutable');
END
'''


GAZEBO_OUTBOX_SAMPLE_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_sample_no_delete
BEFORE DELETE ON monitor_room_gazebo_execution_samples
BEGIN
    SELECT RAISE(ABORT, 'Gazebo samples are append-only');
END
'''


GAZEBO_OUTBOX_CLAIM_INSERT_GUARD_SQL = f'''
CREATE TRIGGER monitor_room_gazebo_claim_insert_guard
BEFORE INSERT ON monitor_room_gazebo_execution_claims
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM monitor_room_gazebo_execution_outbox AS event
        WHERE event.outbox_id = NEW.outbox_id
          AND event.operation_id = NEW.operation_id
          AND event.host_boot_id != ''
          AND event.deadline_boottime_ns = NEW.deadline_boottime_ns
          AND NEW.claim_fence = event.claim_fence + 1
          AND NEW.attempt_number = NEW.claim_fence
          AND NEW.claimed_boottime_ns < event.deadline_boottime_ns
          AND NEW.lease_expires_boottime_ns
              <= event.deadline_boottime_ns
          AND (
              event.state = 'pending'
              OR (event.state = 'claimed'
                  AND event.lease_expires_boottime_ns
                      <= NEW.claimed_boottime_ns)
          )
          AND event.claim_fence
              < {GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS}
    ) THEN RAISE(ABORT, 'Gazebo claim source is invalid') END;
END
'''


GAZEBO_OUTBOX_CLAIM_NO_UPDATE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_claim_no_update
BEFORE UPDATE ON monitor_room_gazebo_execution_claims
BEGIN
    SELECT RAISE(ABORT, 'Gazebo claims are immutable');
END
'''


GAZEBO_OUTBOX_CLAIM_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_claim_no_delete
BEFORE DELETE ON monitor_room_gazebo_execution_claims
BEGIN
    SELECT RAISE(ABORT, 'Gazebo claims are append-only');
END
'''


GAZEBO_OUTBOX_ACK_INSERT_GUARD_SQL = '''
CREATE TRIGGER monitor_room_gazebo_ack_insert_guard
BEFORE INSERT ON monitor_room_gazebo_execution_acknowledgements
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM monitor_room_gazebo_execution_outbox AS event
        JOIN monitor_room_gazebo_execution_claims AS claim
          ON claim.claim_request_id = NEW.claim_request_id
         AND claim.outbox_id = event.outbox_id
        WHERE event.outbox_id = NEW.outbox_id
          AND event.operation_id = NEW.operation_id
          AND event.prepare_request_id = NEW.prepare_request_id
          AND event.state = 'claimed'
          AND event.current_claim_request_id = NEW.claim_request_id
          AND event.current_claim_request_fingerprint
              = NEW.claim_request_fingerprint
          AND event.claim_fence = NEW.claim_fence
          AND claim.claim_fence = NEW.claim_fence
          AND event.current_claim_token = NEW.claim_token
          AND claim.claim_token = NEW.claim_token
          AND NEW.prepared_boottime_ns >= claim.claimed_boottime_ns
          AND NEW.prepared_boottime_ns
              < claim.lease_expires_boottime_ns
          AND NEW.prepared_boottime_ns < event.deadline_boottime_ns
    ) THEN RAISE(ABORT, 'Gazebo acknowledgement source is invalid') END;
END
'''


GAZEBO_OUTBOX_ACK_NO_UPDATE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_ack_no_update
BEFORE UPDATE ON monitor_room_gazebo_execution_acknowledgements
BEGIN
    SELECT RAISE(ABORT, 'Gazebo acknowledgements are immutable');
END
'''


GAZEBO_OUTBOX_ACK_NO_DELETE_SQL = '''
CREATE TRIGGER monitor_room_gazebo_ack_no_delete
BEFORE DELETE ON monitor_room_gazebo_execution_acknowledgements
BEGIN
    SELECT RAISE(ABORT, 'Gazebo acknowledgements are append-only');
END
'''


def _immutable_trigger(name: str, table: str, operation: str) -> str:
    return (
        f'CREATE TRIGGER {name}\n'
        f'BEFORE {operation} ON {table}\n'
        'BEGIN\n'
        "    SELECT RAISE(ABORT, 'Gazebo activation data is immutable');\n"
        'END'
    )


GAZEBO_OUTBOX_PREACTIVATION_NO_UPDATE_SQL = _immutable_trigger(
    'monitor_room_gazebo_preactivation_no_update',
    'monitor_room_gazebo_outbox_preactivation_sources',
    'UPDATE',
)
GAZEBO_OUTBOX_PREACTIVATION_NO_DELETE_SQL = _immutable_trigger(
    'monitor_room_gazebo_preactivation_no_delete',
    'monitor_room_gazebo_outbox_preactivation_sources',
    'DELETE',
)
GAZEBO_OUTBOX_PREACTIVATION_NO_INSERT_SQL = _immutable_trigger(
    'monitor_room_gazebo_preactivation_no_insert',
    'monitor_room_gazebo_outbox_preactivation_sources',
    'INSERT',
)
GAZEBO_OUTBOX_METADATA_NO_UPDATE_SQL = _immutable_trigger(
    'monitor_room_gazebo_metadata_no_update',
    'monitor_room_gazebo_outbox_schema_metadata',
    'UPDATE',
)
GAZEBO_OUTBOX_METADATA_NO_DELETE_SQL = _immutable_trigger(
    'monitor_room_gazebo_metadata_no_delete',
    'monitor_room_gazebo_outbox_schema_metadata',
    'DELETE',
)
GAZEBO_OUTBOX_METADATA_NO_INSERT_SQL = '''
CREATE TRIGGER monitor_room_gazebo_metadata_no_insert
BEFORE INSERT ON monitor_room_gazebo_outbox_schema_metadata
WHEN EXISTS (
    SELECT 1 FROM monitor_room_gazebo_outbox_schema_metadata
)
BEGIN
    SELECT RAISE(ABORT, 'Gazebo activation metadata is immutable');
END
'''


def _expected_schema_objects() -> Dict[str, Tuple[str, str]]:
    return {
        'monitor_room_gazebo_outbox_schema_metadata': (
            'table', GAZEBO_OUTBOX_METADATA_TABLE_SQL
        ),
        'monitor_room_gazebo_outbox_preactivation_sources': (
            'table', GAZEBO_OUTBOX_PREACTIVATION_TABLE_SQL
        ),
        'monitor_room_gazebo_execution_outbox': (
            'table', GAZEBO_OUTBOX_TABLE_SQL
        ),
        'monitor_room_gazebo_execution_samples': (
            'table', GAZEBO_OUTBOX_SAMPLES_TABLE_SQL
        ),
        'monitor_room_gazebo_execution_claims': (
            'table', GAZEBO_OUTBOX_CLAIMS_TABLE_SQL
        ),
        'monitor_room_gazebo_execution_acknowledgements': (
            'table', GAZEBO_OUTBOX_ACKS_TABLE_SQL
        ),
        'monitor_room_gazebo_execution_pending_idx': (
            'index', GAZEBO_OUTBOX_PENDING_INDEX_SQL
        ),
        'monitor_room_gazebo_execution_insert_guard': (
            'trigger', GAZEBO_OUTBOX_INSERT_GUARD_SQL
        ),
        'monitor_room_gazebo_execution_identity_no_update': (
            'trigger', GAZEBO_OUTBOX_IDENTITY_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_execution_transition_guard': (
            'trigger', GAZEBO_OUTBOX_TRANSITION_GUARD_SQL
        ),
        'monitor_room_gazebo_execution_no_delete': (
            'trigger', GAZEBO_OUTBOX_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_sample_insert_guard': (
            'trigger', GAZEBO_OUTBOX_SAMPLE_INSERT_GUARD_SQL
        ),
        'monitor_room_gazebo_sample_no_update': (
            'trigger', GAZEBO_OUTBOX_SAMPLE_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_sample_no_delete': (
            'trigger', GAZEBO_OUTBOX_SAMPLE_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_claim_insert_guard': (
            'trigger', GAZEBO_OUTBOX_CLAIM_INSERT_GUARD_SQL
        ),
        'monitor_room_gazebo_claim_no_update': (
            'trigger', GAZEBO_OUTBOX_CLAIM_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_claim_no_delete': (
            'trigger', GAZEBO_OUTBOX_CLAIM_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_ack_insert_guard': (
            'trigger', GAZEBO_OUTBOX_ACK_INSERT_GUARD_SQL
        ),
        'monitor_room_gazebo_ack_no_update': (
            'trigger', GAZEBO_OUTBOX_ACK_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_ack_no_delete': (
            'trigger', GAZEBO_OUTBOX_ACK_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_preactivation_no_update': (
            'trigger', GAZEBO_OUTBOX_PREACTIVATION_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_preactivation_no_delete': (
            'trigger', GAZEBO_OUTBOX_PREACTIVATION_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_preactivation_no_insert': (
            'trigger', GAZEBO_OUTBOX_PREACTIVATION_NO_INSERT_SQL
        ),
        'monitor_room_gazebo_metadata_no_update': (
            'trigger', GAZEBO_OUTBOX_METADATA_NO_UPDATE_SQL
        ),
        'monitor_room_gazebo_metadata_no_delete': (
            'trigger', GAZEBO_OUTBOX_METADATA_NO_DELETE_SQL
        ),
        'monitor_room_gazebo_metadata_no_insert': (
            'trigger', GAZEBO_OUTBOX_METADATA_NO_INSERT_SQL
        ),
    }


def _activation_anchor_value(preactivation_digest: str) -> int:
    _digest(preactivation_digest, 'preactivation_digest')
    return int(preactivation_digest[:15], 16) + 1


def _preactivation_source_value(row: sqlite3.Row) -> Dict[str, str]:
    values = {
        'confirmation_request_id': str(row['confirmation_request_id']),
        'record_kind': str(row['record_kind']),
        'receipt_digest': (
            None
            if row['receipt_digest'] is None
            else str(row['receipt_digest'])
        ),
        'consume_fingerprint': str(row['consume_fingerprint']),
        'proposal_fingerprint': str(row['proposal_fingerprint']),
    }
    return {
        'confirmation_request_id': values['confirmation_request_id'],
        'record_kind': values['record_kind'],
        'source_receipt_fingerprint': _canonical_hash(
            {
                'contract': 'gazebo-outbox-preactivation-source-v1',
                **values,
            }
        ),
    }


def prepare_gazebo_execution_outbox_schema_locked(
    connection: sqlite3.Connection,
    *,
    activated_at: float,
) -> None:
    """Activate once, snapshot old receipts, or fail on any schema drift."""
    if not connection.in_transaction:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo outbox schema requires a write transaction'
        )
    normalized_time = _wall_timestamp(activated_at, 'activated_at')
    expected = _expected_schema_objects()
    placeholders = ','.join('?' for _ in expected)
    objects = connection.execute(
        f'''
        SELECT name, type FROM sqlite_master
        WHERE name IN ({placeholders})
        ''',
        tuple(expected),
    ).fetchall()
    sentinel = connection.execute(
        '''
        SELECT proposal_fingerprint
        FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (GAZEBO_EXECUTION_OUTBOX_ACTIVATION_SENTINEL,),
    ).fetchone()
    if objects:
        if {str(row['name']) for row in objects} != set(expected):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo outbox schema is incomplete'
            )
        if sentinel is None:
            metadata = connection.execute(
                '''
                SELECT *
                FROM monitor_room_gazebo_outbox_schema_metadata
                WHERE singleton = 1
                '''
            ).fetchone()
            counts = (
                connection.execute(
                    'SELECT COUNT(*) FROM '
                    'monitor_room_gazebo_outbox_preactivation_sources'
                ).fetchone()[0],
                connection.execute(
                    'SELECT COUNT(*) FROM '
                    'monitor_room_gazebo_execution_outbox'
                ).fetchone()[0],
                connection.execute(
                    'SELECT COUNT(*) FROM '
                    'monitor_room_gazebo_execution_samples'
                ).fetchone()[0],
                connection.execute(
                    'SELECT COUNT(*) FROM '
                    'monitor_room_gazebo_execution_claims'
                ).fetchone()[0],
                connection.execute(
                    'SELECT COUNT(*) FROM '
                    'monitor_room_gazebo_execution_acknowledgements'
                ).fetchone()[0],
                connection.execute(
                    '''
                    SELECT COUNT(*) FROM monitor_room_simulation_ledger
                    WHERE schema_version = 4
                      AND record_kind IN ('planned', 'planning_failed')
                    '''
                ).fetchone()[0],
            )
            metadata_valid = False
            if metadata is not None:
                try:
                    metadata_valid = (
                        metadata['singleton'] == 1
                        and metadata['schema_version'] == 1
                        and type(metadata['activation_epoch']) is str
                        and _HEX_DIGEST.fullmatch(
                            metadata['activation_epoch']
                        ) is not None
                        and type(metadata['preactivation_digest']) is str
                        and _HEX_DIGEST.fullmatch(
                            metadata['preactivation_digest']
                        ) is not None
                        and metadata['preactivation_count'] == 0
                        and metadata['preactivation_digest']
                        == _canonical_hash(
                            {
                                'schema_version': 1,
                                'activated_at': _wall_timestamp(
                                    metadata['activated_at'],
                                    'activated_at',
                                ),
                                'activation_epoch': (
                                    metadata['activation_epoch']
                                ),
                                'sources': [],
                            }
                        )
                    )
                except (TypeError, ValidationError, ValueError):
                    metadata_valid = False
            if (
                not metadata_valid
                or any(int(count) != 0 for count in counts)
            ):
                raise GazeboExecutionOutboxSchemaError(
                    'Gazebo outbox activation anchor is missing'
                )
            _install_activation_anchor_locked(
                connection,
                metadata['preactivation_digest'],
            )
        validate_gazebo_execution_outbox_schema_locked(connection)
        return
    if sentinel is not None:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo outbox schema was removed after activation'
        )
    for sql in (
        GAZEBO_OUTBOX_METADATA_TABLE_SQL,
        GAZEBO_OUTBOX_PREACTIVATION_TABLE_SQL,
        GAZEBO_OUTBOX_TABLE_SQL,
        GAZEBO_OUTBOX_SAMPLES_TABLE_SQL,
        GAZEBO_OUTBOX_CLAIMS_TABLE_SQL,
        GAZEBO_OUTBOX_ACKS_TABLE_SQL,
        GAZEBO_OUTBOX_PENDING_INDEX_SQL,
        GAZEBO_OUTBOX_INSERT_GUARD_SQL,
        GAZEBO_OUTBOX_IDENTITY_NO_UPDATE_SQL,
        GAZEBO_OUTBOX_TRANSITION_GUARD_SQL,
        GAZEBO_OUTBOX_NO_DELETE_SQL,
        GAZEBO_OUTBOX_SAMPLE_INSERT_GUARD_SQL,
        GAZEBO_OUTBOX_SAMPLE_NO_UPDATE_SQL,
        GAZEBO_OUTBOX_SAMPLE_NO_DELETE_SQL,
        GAZEBO_OUTBOX_CLAIM_INSERT_GUARD_SQL,
        GAZEBO_OUTBOX_CLAIM_NO_UPDATE_SQL,
        GAZEBO_OUTBOX_CLAIM_NO_DELETE_SQL,
        GAZEBO_OUTBOX_ACK_INSERT_GUARD_SQL,
        GAZEBO_OUTBOX_ACK_NO_UPDATE_SQL,
        GAZEBO_OUTBOX_ACK_NO_DELETE_SQL,
    ):
        connection.execute(sql)
    existing_rows = connection.execute(
        '''
        SELECT confirmation_request_id, record_kind, receipt_digest,
               consume_fingerprint, proposal_fingerprint
        FROM monitor_room_simulation_ledger
        ORDER BY confirmation_request_id
        '''
    ).fetchall()
    preactivation = [
        _preactivation_source_value(row) for row in existing_rows
    ]
    for source in preactivation:
        connection.execute(
            '''
            INSERT INTO monitor_room_gazebo_outbox_preactivation_sources (
                confirmation_request_id, source_receipt_fingerprint,
                record_kind
            ) VALUES (?, ?, ?)
            ''',
            (
                source['confirmation_request_id'],
                source['source_receipt_fingerprint'],
                source['record_kind'],
            ),
        )
    for sql in (
        GAZEBO_OUTBOX_PREACTIVATION_NO_UPDATE_SQL,
        GAZEBO_OUTBOX_PREACTIVATION_NO_DELETE_SQL,
        GAZEBO_OUTBOX_PREACTIVATION_NO_INSERT_SQL,
        GAZEBO_OUTBOX_METADATA_NO_UPDATE_SQL,
        GAZEBO_OUTBOX_METADATA_NO_DELETE_SQL,
        GAZEBO_OUTBOX_METADATA_NO_INSERT_SQL,
    ):
        connection.execute(sql)
    activation_epoch = secrets.token_hex(32)
    preactivation_digest = _canonical_hash(
        {
            'schema_version': GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION,
            'activated_at': normalized_time,
            'activation_epoch': activation_epoch,
            'sources': [
                [
                    source['confirmation_request_id'],
                    source['source_receipt_fingerprint'],
                    source['record_kind'],
                ]
                for source in preactivation
            ],
        }
    )
    connection.execute(
        '''
        INSERT INTO monitor_room_gazebo_outbox_schema_metadata (
            singleton, schema_version, activated_at, activation_epoch,
            preactivation_count, preactivation_digest
        ) VALUES (1, 1, ?, ?, ?, ?)
        ''',
        (
            normalized_time,
            activation_epoch,
            len(preactivation),
            preactivation_digest,
        ),
    )
    _install_activation_anchor_locked(connection, preactivation_digest)
    validate_gazebo_execution_outbox_schema_locked(connection)


def _install_activation_anchor_locked(
    connection: sqlite3.Connection,
    preactivation_digest: str,
) -> None:
    simulation = connection.execute(
        '''
        SELECT activation_epoch, activated_at
        FROM monitor_room_simulation_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    if simulation is None:
        raise GazeboExecutionOutboxSchemaError(
            'simulation activation source is missing'
        )
    connection.execute(
        'DROP TRIGGER monitor_room_simulation_preactivation_no_insert'
    )
    connection.execute(
        '''
        INSERT INTO monitor_room_simulation_preactivation_proposals (
            proposal_fingerprint, activation_epoch,
            snapshot_rowid, snapshotted_at
        ) VALUES (?, ?, ?, ?)
        ''',
        (
            GAZEBO_EXECUTION_OUTBOX_ACTIVATION_SENTINEL,
            simulation['activation_epoch'],
            _activation_anchor_value(preactivation_digest),
            simulation['activated_at'],
        ),
    )
    connection.execute(SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL)


def _schema_table_for_object(name: str) -> str:
    if name == 'monitor_room_gazebo_execution_pending_idx':
        return 'monitor_room_gazebo_execution_outbox'
    prefixes = {
        'monitor_room_gazebo_execution_': (
            'monitor_room_gazebo_execution_outbox'
        ),
        'monitor_room_gazebo_sample_': (
            'monitor_room_gazebo_execution_samples'
        ),
        'monitor_room_gazebo_claim_': (
            'monitor_room_gazebo_execution_claims'
        ),
        'monitor_room_gazebo_ack_': (
            'monitor_room_gazebo_execution_acknowledgements'
        ),
        'monitor_room_gazebo_preactivation_': (
            'monitor_room_gazebo_outbox_preactivation_sources'
        ),
        'monitor_room_gazebo_metadata_': (
            'monitor_room_gazebo_outbox_schema_metadata'
        ),
    }
    for prefix, table in prefixes.items():
        if name.startswith(prefix):
            return table
    raise GazeboExecutionOutboxSchemaError(
        'Gazebo outbox schema object is unknown'
    )


def validate_gazebo_execution_outbox_schema_locked(
    connection: sqlite3.Connection,
) -> None:
    """Fail closed on DDL, activation, payload, claim, or ACK drift."""
    expected = _expected_schema_objects()
    for name, (object_type, exact_sql) in expected.items():
        row = connection.execute(
            'SELECT type, sql FROM sqlite_master WHERE name = ?',
            (name,),
        ).fetchone()
        if (
            row is None
            or row['type'] != object_type
            or str(row['sql']).strip() != exact_sql.strip()
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo outbox schema is incompatible'
            )
    tables = {
        name for name, (kind, _sql) in expected.items()
        if kind == 'table'
    }
    placeholders = ','.join('?' for _ in tables)
    custom = {
        (str(row['type']), str(row['name']), str(row['tbl_name']))
        for row in connection.execute(
            f'''
            SELECT type, name, tbl_name FROM sqlite_master
            WHERE type IN ('index', 'trigger')
              AND tbl_name IN ({placeholders})
              AND sql IS NOT NULL
            ''',
            tuple(tables),
        ).fetchall()
    }
    expected_custom = {
        (kind, name, _schema_table_for_object(name))
        for name, (kind, _sql) in expected.items()
        if kind in {'index', 'trigger'}
    }
    if custom != expected_custom:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo outbox schema has unexpected objects'
        )
    metadata_rows = connection.execute(
        '''
        SELECT *, typeof(singleton) AS singleton_type,
               typeof(schema_version) AS version_type,
               typeof(activated_at) AS activated_type,
               typeof(activation_epoch) AS epoch_type,
               typeof(preactivation_count) AS count_type,
               typeof(preactivation_digest) AS digest_type
        FROM monitor_room_gazebo_outbox_schema_metadata
        '''
    ).fetchall()
    if len(metadata_rows) != 1:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo outbox metadata is incompatible'
        )
    metadata = metadata_rows[0]
    if (
        metadata['singleton'] != 1
        or metadata['schema_version'] != 1
        or metadata['singleton_type'] != 'integer'
        or metadata['version_type'] != 'integer'
        or metadata['activated_type'] not in {'integer', 'real'}
        or metadata['epoch_type'] != 'text'
        or metadata['count_type'] != 'integer'
        or metadata['digest_type'] != 'text'
        or type(metadata['activation_epoch']) is not str
        or not _HEX_DIGEST.fullmatch(metadata['activation_epoch'])
        or type(metadata['preactivation_digest']) is not str
        or not _HEX_DIGEST.fullmatch(metadata['preactivation_digest'])
        or type(metadata['preactivation_count']) is not int
        or metadata['preactivation_count'] < 0
    ):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo outbox metadata is incompatible'
        )
    activated_at = _wall_timestamp(metadata['activated_at'], 'activated_at')
    simulation = connection.execute(
        '''
        SELECT activation_epoch, activated_at
        FROM monitor_room_simulation_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    sentinel = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (GAZEBO_EXECUTION_OUTBOX_ACTIVATION_SENTINEL,),
    ).fetchone()
    if (
        simulation is None
        or sentinel is None
        or sentinel['activation_epoch'] != simulation['activation_epoch']
        or sentinel['snapshotted_at'] != simulation['activated_at']
        or type(sentinel['snapshot_rowid']) is not int
        or sentinel['snapshot_rowid']
        != _activation_anchor_value(metadata['preactivation_digest'])
    ):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo outbox activation anchor is incompatible'
        )
    snapshot = connection.execute(
        '''
        SELECT confirmation_request_id, source_receipt_fingerprint,
               record_kind
        FROM monitor_room_gazebo_outbox_preactivation_sources
        ORDER BY confirmation_request_id
        '''
    ).fetchall()
    snapshot_digest = _canonical_hash(
        {
            'schema_version': GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION,
            'activated_at': activated_at,
            'activation_epoch': metadata['activation_epoch'],
            'sources': [
                [
                    row['confirmation_request_id'],
                    row['source_receipt_fingerprint'],
                    row['record_kind'],
                ]
                for row in snapshot
            ],
        }
    )
    if (
        len(snapshot) != metadata['preactivation_count']
        or snapshot_digest != metadata['preactivation_digest']
        or any(
            type(row['confirmation_request_id']) is not str
            or not _SAFE_IDENTIFIER.fullmatch(
                row['confirmation_request_id']
            )
            or type(row['source_receipt_fingerprint']) is not str
            or not _HEX_DIGEST.fullmatch(
                row['source_receipt_fingerprint']
            )
            for row in snapshot
        )
    ):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo outbox preactivation snapshot is incompatible'
        )
    expected_fks = {
        'monitor_room_gazebo_execution_outbox': (),
        'monitor_room_gazebo_execution_samples': (
            ('monitor_room_gazebo_execution_outbox', 'outbox_id',
             'outbox_id', 'RESTRICT'),
        ),
        'monitor_room_gazebo_execution_claims': (
            ('monitor_room_gazebo_execution_outbox', 'outbox_id',
             'outbox_id', 'RESTRICT'),
        ),
        'monitor_room_gazebo_execution_acknowledgements': (
            ('monitor_room_gazebo_execution_claims', 'claim_request_id',
             'claim_request_id', 'RESTRICT'),
            ('monitor_room_gazebo_execution_outbox', 'outbox_id',
             'outbox_id', 'RESTRICT'),
        ),
    }
    for table, wanted in expected_fks.items():
        actual = tuple(
            (
                row['table'], row['from'], row['to'],
                str(row['on_delete']).upper(),
            )
            for row in connection.execute(
                f'PRAGMA foreign_key_list({table})'
            ).fetchall()
        )
        if sorted(actual) != sorted(wanted):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo outbox ownership is incompatible'
            )
    for row in connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_outbox
        ORDER BY outbox_id
        '''
    ).fetchall():
        _validate_outbox_row_locked(connection, row)


def _stable_id(prefix: str, receipt_digest: str, activation_epoch: str) -> str:
    digest = hashlib.sha256(
        b'malbut-gazebo-execution-id-v1\0'
        + prefix.encode('ascii')
        + b'\0'
        + receipt_digest.encode('ascii')
        + b'\0'
        + activation_epoch.encode('ascii')
    ).hexdigest()
    return f'{prefix}-{digest}'


def _immutable_outbox_payload(values: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'schema_version': GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION,
        'contract': 'monitor-room-gazebo-execution-outbox-v1',
        **{
            name: values[name]
            for name in _GAZEBO_OUTBOX_IDENTITY_COLUMNS
            if name not in {
                'schema_version',
                'outbox_fingerprint',
            }
        },
    }


def _sample_digest(outbox_id: str, sample: CoverageSample) -> str:
    return _canonical_hash(
        {
            'contract': 'monitor-room-gazebo-sample-v1',
            'outbox_id': outbox_id,
            'sample': sample.to_private_dict(),
        }
    )


def _receipt_source_matches(
    row: sqlite3.Row,
    receipt: DurableSimulationExecution,
) -> bool:
    return (
        row['schema_version'] == 4
        and row['record_kind'] == 'planned'
        and row['confirmation_request_id']
        == receipt.confirmation_request_id
        and row['confirmation_result_id']
        == receipt.confirmation_result_id
        and row['consume_request_id'] == receipt.consume_request_id
        and row['consume_fingerprint'] == receipt.consume_fingerprint
        and row['actor_binding_digest'] == receipt.actor_binding_digest
        and row['proposal_fingerprint'] == receipt.proposal_fingerprint
        and row['target_binding_digest']
        == receipt.target_binding_digest
        and row['effects_digest'] == receipt.effects_digest
        and row['profile_digest'] == receipt.profile_digest
        and row['plan_digest'] == receipt.plan_digest
        and row['sample_count'] == receipt.sample_count
        and row['component_count'] == receipt.component_count
        and row['receipt_digest'] == receipt.receipt_digest
        and row['simulation'] == 1
        and row['physical_authorized'] == 0
        and row['physical_effects'] == 0
    )


def record_gazebo_execution_outbox_locked(
    connection: sqlite3.Connection,
    *,
    receipt: DurableSimulationExecution,
    plan: CoveragePlan,
    target: TargetBinding,
    policy: GazeboSimulationExecutionPolicy,
    context: _VerifiedGazeboExecutionContext,
    created_wall: float,
) -> GazeboExecutionEnqueue:
    """Append one exact outbox and all samples in the caller transaction."""
    if not connection.in_transaction:
        raise GazeboExecutionOutboxAssuranceError(
            'Gazebo enqueue requires an owned write transaction'
        )
    validate_gazebo_execution_outbox_schema_locked(connection)
    if type(policy) is not GazeboSimulationExecutionPolicy:
        raise GazeboExecutionOutboxAssuranceError(
            'fixed Gazebo execution policy is required'
        )
    policy.require_context(context)
    if (
        type(receipt) is not DurableSimulationExecution
        or receipt.replayed is not False
        or receipt.schema_version != 4
        or receipt.record_kind != 'planned'
        or receipt.state != 'succeeded'
        or receipt.receipt_digest is None
        or receipt.operation_id is None
        or not receipt.operation_id.startswith('simulation-operation-')
    ):
        raise GazeboExecutionOutboxUpgradeRequiredError(
            'Gazebo execution requires one fresh planned receipt'
        )
    if (
        type(plan) is not CoveragePlan
        or plan.profile != DEFAULT_COVERAGE_PROFILE
        or not 1 <= plan.sample_count
        <= GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES
        or type(target) is not TargetBinding
        or plan.target_binding_digest != target.binding_digest
        or plan.source_arguments_digest
        != target.source_arguments_digest
        or plan.geometry_digest != target.geometry_digest
        or plan.effects_digest != target.effects_digest
        or receipt.target_binding_digest != target.binding_digest
        or receipt.effects_digest != target.effects_digest
        or receipt.profile_digest != plan.profile.digest
        or receipt.plan_digest != plan.digest
        or receipt.sample_count != plan.sample_count
        or receipt.component_count != plan.component_count
    ):
        raise GazeboExecutionOutboxAssuranceError(
            'coverage plan does not match the fresh receipt'
        )
    source = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_ledger
        WHERE confirmation_request_id = ?
        ''',
        (receipt.confirmation_request_id,),
    ).fetchone()
    if source is None or not _receipt_source_matches(source, receipt):
        raise GazeboExecutionOutboxAssuranceError(
            'durable simulation source changed'
        )
    if connection.execute(
        '''
        SELECT 1
        FROM monitor_room_gazebo_outbox_preactivation_sources
        WHERE confirmation_request_id = ?
        ''',
        (receipt.confirmation_request_id,),
    ).fetchone() is not None:
        raise GazeboExecutionOutboxUpgradeRequiredError(
            'preactivation simulation receipts are audit-only'
        )
    existing = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_outbox
        WHERE confirmation_request_id = ? OR source_receipt_digest = ?
        ''',
        (receipt.confirmation_request_id, receipt.receipt_digest),
    ).fetchone()
    if existing is not None:
        raise GazeboExecutionOutboxUpgradeRequiredError(
            'only the fresh consume transaction may create an outbox'
        )
    semantic = context.semantic_evidence.canonical_copy()
    robot = context.robot_state_evidence
    if (
        semantic.snapshot.zones_digest != context.zones_digest
        or semantic.content_sha256 != context.semantic_content_sha256
        or robot.evidence_digest != context.robot_evidence_digest
        or context.robot_id != target.device_id
        or robot.device_id != target.device_id
        or robot.map_id != target.map_id
        or robot.map_revision != target.map_revision
        or context.host_boot_id != robot.host_boot_id
    ):
        raise GazeboExecutionOutboxAssuranceError(
            'execution evidence does not match the coverage target'
        )
    normalized_wall = _wall_timestamp(created_wall, 'created_wall')
    if normalized_wall * 1000.0 >= semantic.expires_at_ms:
        raise GazeboExecutionOutboxAssuranceError(
            'semantic evidence expired before durable enqueue'
        )
    boundary_boottime = _POLICY_CURRENT_BOOTTIME_NS(policy)
    if (
        boundary_boottime < context.created_boottime_ns
        or boundary_boottime >= context.deadline_boottime_ns
        or boundary_boottime >= robot.valid_until_boottime_ns
        or _POLICY_CURRENT_HOST_BOOT_ID(policy) != context.host_boot_id
    ):
        raise GazeboExecutionOutboxAssuranceError(
            'Gazebo execution evidence expired before durable enqueue'
        )
    metadata = connection.execute(
        '''
        SELECT activation_epoch
        FROM monitor_room_gazebo_outbox_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    if metadata is None:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo activation metadata is missing'
        )
    operation_id = _stable_id(
        'gazebo-operation',
        receipt.receipt_digest,
        metadata['activation_epoch'],
    )
    prepare_request_id = _stable_id(
        'gazebo-prepare',
        receipt.receipt_digest,
        metadata['activation_epoch'],
    )
    outbox_id = _stable_id(
        'gazebo-execution-outbox',
        receipt.receipt_digest,
        metadata['activation_epoch'],
    )
    values: Dict[str, Any] = {
        'schema_version': GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION,
        'outbox_id': outbox_id,
        'confirmation_request_id': receipt.confirmation_request_id,
        'source_receipt_digest': receipt.receipt_digest,
        'source_consume_fingerprint': receipt.consume_fingerprint,
        'source_proposal_fingerprint': receipt.proposal_fingerprint,
        'operation_id': operation_id,
        'prepare_request_id': prepare_request_id,
        'robot_id': context.robot_id,
        'map_id': target.map_id,
        'map_revision': target.map_revision,
        'semantic_revision': target.semantic_revision,
        'zones_digest': context.zones_digest,
        'semantic_content_sha256': context.semantic_content_sha256,
        'semantic_map_generation': semantic.map_generation,
        'semantic_authorization_generation': (
            semantic.authorization_generation
        ),
        'target_binding_digest': target.binding_digest,
        'source_arguments_digest': target.source_arguments_digest,
        'geometry_digest': target.geometry_digest,
        'effects_digest': target.effects_digest,
        'profile_digest': plan.profile.digest,
        'plan_digest': plan.digest,
        'sample_count': plan.sample_count,
        'component_count': plan.component_count,
        'candidate_upper_bound': plan.candidate_upper_bound,
        'geometry_test_upper_bound': plan.geometry_test_upper_bound,
        'robot_state_evidence_digest': robot.evidence_digest,
        'robot_state_instance_id': robot.instance_id,
        'robot_state_sequence': robot.sequence,
        'robot_state_valid_until_boottime_ns': (
            robot.valid_until_boottime_ns
        ),
        'host_boot_id': context.host_boot_id,
        'max_duration_seconds': target.effects.max_duration_seconds,
        'created_wall': normalized_wall,
        'evidence_boottime_ns': context.created_boottime_ns,
        'created_boottime_ns': boundary_boottime,
        'deadline_boottime_ns': context.deadline_boottime_ns,
        'runtime_mode': 'gazebo',
        'simulation': 1,
        'gazebo_execution_authorized': 1,
        'physical_authorized': 0,
        'physical_effects': 0,
        'viewer_live': 0,
        'camera_coverage_validated': 0,
        'coverage_achieved': 0,
    }
    values['outbox_fingerprint'] = _canonical_hash(
        _immutable_outbox_payload(values)
    )
    insert_failed = False
    try:
        connection.execute(
            '''
            INSERT INTO monitor_room_gazebo_execution_outbox (
                schema_version, outbox_id, outbox_fingerprint,
                confirmation_request_id, source_receipt_digest,
                source_consume_fingerprint, source_proposal_fingerprint,
                operation_id, prepare_request_id, robot_id,
                map_id, map_revision, semantic_revision, zones_digest,
                semantic_content_sha256, semantic_map_generation,
                semantic_authorization_generation,
                target_binding_digest, source_arguments_digest,
                geometry_digest, effects_digest, profile_digest,
                plan_digest, sample_count, component_count,
                candidate_upper_bound, geometry_test_upper_bound,
                robot_state_evidence_digest, robot_state_instance_id,
                robot_state_sequence,
                robot_state_valid_until_boottime_ns, host_boot_id,
                max_duration_seconds, created_wall,
                evidence_boottime_ns, created_boottime_ns,
                deadline_boottime_ns,
                state, terminal_code, attempt_count, claim_fence,
                current_claim_request_id,
                current_claim_request_fingerprint,
                current_claim_token, current_lease_seconds,
                claimed_boottime_ns, lease_expires_boottime_ns,
                prepared_boottime_ns, prepare_fingerprint,
                last_transition_boottime_ns, runtime_mode, simulation,
                gazebo_execution_authorized, physical_authorized,
                physical_effects, viewer_live,
                camera_coverage_validated, coverage_achieved
            ) VALUES (
                :schema_version, :outbox_id, :outbox_fingerprint,
                :confirmation_request_id, :source_receipt_digest,
                :source_consume_fingerprint, :source_proposal_fingerprint,
                :operation_id, :prepare_request_id, :robot_id,
                :map_id, :map_revision, :semantic_revision, :zones_digest,
                :semantic_content_sha256, :semantic_map_generation,
                :semantic_authorization_generation,
                :target_binding_digest, :source_arguments_digest,
                :geometry_digest, :effects_digest, :profile_digest,
                :plan_digest, :sample_count, :component_count,
                :candidate_upper_bound, :geometry_test_upper_bound,
                :robot_state_evidence_digest, :robot_state_instance_id,
                :robot_state_sequence,
                :robot_state_valid_until_boottime_ns, :host_boot_id,
                :max_duration_seconds, :created_wall,
                :evidence_boottime_ns, :created_boottime_ns,
                :deadline_boottime_ns,
                'pending', NULL, 0, 0, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL, :created_boottime_ns,
                'gazebo', 1, 1, 0, 0, 0, 0, 0
            )
            ''',
            values,
        )
        for sample in plan.samples:
            connection.execute(
                '''
                INSERT INTO monitor_room_gazebo_execution_samples (
                    outbox_id, sample_index, polygon_ordinal,
                    row_ordinal, x_mm, y_mm, frame_id, sample_digest
                ) VALUES (?, ?, ?, ?, ?, ?, 'map', ?)
                ''',
                (
                    outbox_id,
                    sample.index,
                    sample.polygon_ordinal,
                    sample.row_ordinal,
                    sample.x_mm,
                    sample.y_mm,
                    _sample_digest(outbox_id, sample),
                ),
            )
    except sqlite3.IntegrityError:
        insert_failed = True
    if insert_failed:
        raise GazeboExecutionOutboxConflictError(
            'Gazebo execution enqueue conflict'
        )
    row = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_outbox
        WHERE outbox_id = ?
        ''',
        (outbox_id,),
    ).fetchone()
    _validate_outbox_row_locked(connection, row)
    return _enqueue_from_row(row)


def get_gazebo_execution_enqueue_for_receipt_locked(
    connection: sqlite3.Connection,
    *,
    receipt: DurableSimulationExecution,
) -> Optional[GazeboExecutionEnqueue]:
    """Return exact replay only; never synthesize an outbox from a receipt."""
    validate_gazebo_execution_outbox_schema_locked(connection)
    if type(receipt) is not DurableSimulationExecution:
        raise TypeError('receipt must be DurableSimulationExecution')
    if receipt.record_kind != 'planned':
        return None
    if receipt.receipt_digest is None:
        raise GazeboExecutionOutboxUpgradeRequiredError(
            'legacy simulation receipt is audit-only'
        )
    row = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_outbox
        WHERE confirmation_request_id = ?
          AND source_receipt_digest = ?
        ''',
        (receipt.confirmation_request_id, receipt.receipt_digest),
    ).fetchone()
    if row is None:
        raise GazeboExecutionOutboxUpgradeRequiredError(
            'an existing pure simulation receipt cannot be elevated'
        )
    source = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_ledger
        WHERE confirmation_request_id = ?
        ''',
        (receipt.confirmation_request_id,),
    ).fetchone()
    if source is None or not _receipt_source_matches(source, receipt):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo replay source changed'
        )
    _validate_outbox_row_locked(connection, row)
    return replace(_enqueue_from_row(row), replayed=True)


def _enqueue_from_row(row: sqlite3.Row) -> GazeboExecutionEnqueue:
    return GazeboExecutionEnqueue(
        outbox_id=row['outbox_id'],
        operation_id=row['operation_id'],
        prepare_request_id=row['prepare_request_id'],
        state=row['state'],
        sample_count=row['sample_count'],
        created_boottime_ns=row['created_boottime_ns'],
        deadline_boottime_ns=row['deadline_boottime_ns'],
    )


def _claim_record_fingerprint(values: Dict[str, Any]) -> str:
    return _canonical_hash(
        {
            'contract': 'monitor-room-gazebo-claim-v1',
            'schema_version': GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION,
            'claim_request_id': values['claim_request_id'],
            'outbox_id': values['outbox_id'],
            'operation_id': values['operation_id'],
            'claim_fence': values['claim_fence'],
            'attempt_number': values['attempt_number'],
            'claim_token': values['claim_token'],
            'lease_seconds': values['lease_seconds'],
            'claimed_boottime_ns': values['claimed_boottime_ns'],
            'lease_expires_boottime_ns': (
                values['lease_expires_boottime_ns']
            ),
            'deadline_boottime_ns': values['deadline_boottime_ns'],
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
        }
    )


def _acknowledgement_values(
    event: sqlite3.Row,
    claim: sqlite3.Row,
    *,
    prepare_fingerprint: str,
    prepared_boottime_ns: int,
) -> Dict[str, Any]:
    acknowledgement_id = _stable_id(
        'gazebo-prepare-ack',
        event['source_receipt_digest'],
        event['outbox_fingerprint'],
    )
    values: Dict[str, Any] = {
        'schema_version': GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION,
        'acknowledgement_id': acknowledgement_id,
        'outbox_id': event['outbox_id'],
        'operation_id': event['operation_id'],
        'prepare_request_id': event['prepare_request_id'],
        'claim_request_id': claim['claim_request_id'],
        'claim_request_fingerprint': (
            claim['claim_request_fingerprint']
        ),
        'claim_fence': claim['claim_fence'],
        'claim_token': claim['claim_token'],
        'claim_token_digest': hashlib.sha256(
            claim['claim_token'].encode('ascii')
        ).hexdigest(),
        'prepare_fingerprint': prepare_fingerprint,
        'prepared_boottime_ns': prepared_boottime_ns,
    }
    values['acknowledgement_fingerprint'] = _canonical_hash(
        {
            'contract': 'monitor-room-gazebo-prepare-ack-v1',
            **values,
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
        }
    )
    return values


def _samples_for_outbox_locked(
    connection: sqlite3.Connection,
    outbox_id: str,
) -> Tuple[GazeboExecutionSample, ...]:
    rows = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_samples
        WHERE outbox_id = ? ORDER BY sample_index
        ''',
        (outbox_id,),
    ).fetchall()
    samples = []
    for row in rows:
        sample = GazeboExecutionSample(
            index=row['sample_index'],
            polygon_ordinal=row['polygon_ordinal'],
            row_ordinal=row['row_ordinal'],
            x_mm=row['x_mm'],
            y_mm=row['y_mm'],
            frame_id=row['frame_id'],
        )
        planner_sample = CoverageSample(**sample.to_private_dict())
        if row['sample_digest'] != _sample_digest(
            outbox_id, planner_sample
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution sample digest changed'
            )
        samples.append(sample)
    return tuple(samples)


def _claim_from_rows(
    event: sqlite3.Row,
    claim: sqlite3.Row,
    samples: Tuple[GazeboExecutionSample, ...],
) -> GazeboExecutionClaim:
    return GazeboExecutionClaim(
        outbox_id=event['outbox_id'],
        operation_id=event['operation_id'],
        prepare_request_id=event['prepare_request_id'],
        claim_request_id=claim['claim_request_id'],
        claim_token=claim['claim_token'],
        claim_fence=claim['claim_fence'],
        attempt_number=claim['attempt_number'],
        robot_id=event['robot_id'],
        map_id=event['map_id'],
        map_revision=event['map_revision'],
        semantic_revision=event['semantic_revision'],
        zones_digest=event['zones_digest'],
        target_binding_digest=event['target_binding_digest'],
        effects_digest=event['effects_digest'],
        profile_digest=event['profile_digest'],
        plan_digest=event['plan_digest'],
        host_boot_id=event['host_boot_id'],
        ordered_semantic_samples=samples,
        deadline_boottime_ns=event['deadline_boottime_ns'],
        claimed_boottime_ns=claim['claimed_boottime_ns'],
        lease_expires_boottime_ns=(
            claim['lease_expires_boottime_ns']
        ),
    )


def _validate_outbox_row_locked(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> None:
    if row is None:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution outbox row is missing'
        )
    try:
        for name in (
            'confirmation_request_id', 'robot_id', 'map_id',
            'map_revision', 'semantic_revision',
            'robot_state_instance_id',
        ):
            _identifier(row[name], name)
        for name in (
            'outbox_fingerprint', 'source_receipt_digest',
            'source_consume_fingerprint', 'source_proposal_fingerprint',
            'zones_digest', 'semantic_content_sha256',
            'target_binding_digest', 'source_arguments_digest',
            'geometry_digest', 'effects_digest', 'profile_digest',
            'plan_digest', 'robot_state_evidence_digest',
        ):
            _digest(row[name], name)
        _canonical_boot_id(row['host_boot_id'])
        _enqueue_from_row(row)
    except (TypeError, ValueError, ValidationError):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution outbox identity is invalid'
        ) from None
    if (
        row['schema_version'] != GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION
        or not _valid_prefixed_identifier(
            row['outbox_id'], 'gazebo-execution-outbox-'
        )
        or not _valid_prefixed_identifier(
            row['operation_id'], 'gazebo-operation-'
        )
        or not _valid_prefixed_identifier(
            row['prepare_request_id'], 'gazebo-prepare-'
        )
        or row['runtime_mode'] != 'gazebo'
        or row['simulation'] != 1
        or row['gazebo_execution_authorized'] != 1
        or row['physical_authorized'] != 0
        or row['physical_effects'] != 0
        or row['viewer_live'] != 0
        or row['camera_coverage_validated'] != 0
        or row['coverage_achieved'] != 0
        or row['outbox_fingerprint'] != _canonical_hash(
            _immutable_outbox_payload(dict(row))
        )
    ):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution outbox binding changed'
        )
    source = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_ledger
        WHERE confirmation_request_id = ?
        ''',
        (row['confirmation_request_id'],),
    ).fetchone()
    if (
        source is None
        or source['schema_version'] != 4
        or source['record_kind'] != 'planned'
        or source['receipt_digest'] != row['source_receipt_digest']
        or source['consume_fingerprint']
        != row['source_consume_fingerprint']
        or source['proposal_fingerprint']
        != row['source_proposal_fingerprint']
        or source['target_binding_digest']
        != row['target_binding_digest']
        or source['effects_digest'] != row['effects_digest']
        or source['profile_digest'] != row['profile_digest']
        or source['plan_digest'] != row['plan_digest']
        or source['sample_count'] != row['sample_count']
        or source['component_count'] != row['component_count']
        or source['simulation'] != 1
        or source['physical_authorized'] != 0
        or source['physical_effects'] != 0
        or connection.execute(
            '''
            SELECT 1
            FROM monitor_room_gazebo_outbox_preactivation_sources
            WHERE confirmation_request_id = ?
            ''',
            (row['confirmation_request_id'],),
        ).fetchone() is not None
    ):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution source binding changed'
        )
    private_samples = _samples_for_outbox_locked(
        connection, row['outbox_id']
    )
    if len(private_samples) != row['sample_count']:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution sample set is incomplete'
        )
    try:
        coverage_plan = CoveragePlan(
            profile=DEFAULT_COVERAGE_PROFILE,
            target_binding_digest=row['target_binding_digest'],
            source_arguments_digest=row['source_arguments_digest'],
            geometry_digest=row['geometry_digest'],
            effects_digest=row['effects_digest'],
            samples=tuple(
                CoverageSample(**sample.to_private_dict())
                for sample in private_samples
            ),
            component_count=row['component_count'],
            candidate_upper_bound=row['candidate_upper_bound'],
            geometry_test_upper_bound=(
                row['geometry_test_upper_bound']
            ),
        )
    except (TypeError, ValueError, ValidationError):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution coverage plan is invalid'
        ) from None
    if coverage_plan.digest != row['plan_digest']:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution plan digest changed'
        )
    claims = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_claims
        WHERE outbox_id = ? ORDER BY claim_fence
        ''',
        (row['outbox_id'],),
    ).fetchall()
    if (
        len(claims) != row['attempt_count']
        or [claim['claim_fence'] for claim in claims]
        != list(range(1, len(claims) + 1))
    ):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution claim history is incomplete'
        )
    for claim in claims:
        if (
            claim['schema_version'] != 1
            or claim['outbox_id'] != row['outbox_id']
            or claim['operation_id'] != row['operation_id']
            or claim['attempt_number'] != claim['claim_fence']
            or type(claim['claim_token']) is not str
            or not _CLAIM_TOKEN.fullmatch(claim['claim_token'])
            or claim['deadline_boottime_ns']
            != row['deadline_boottime_ns']
            or claim['simulation'] != 1
            or claim['physical_authorized'] != 0
            or claim['physical_effects'] != 0
            or claim['claim_request_fingerprint']
            != _claim_record_fingerprint(dict(claim))
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution claim binding changed'
            )
    if row['attempt_count'] == 0:
        if row['state'] not in {'pending', 'expired'}:
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution state has no claim'
            )
    else:
        current = claims[-1]
        if (
            row['current_claim_request_id']
            != current['claim_request_id']
            or row['current_claim_request_fingerprint']
            != current['claim_request_fingerprint']
            or row['current_claim_token'] != current['claim_token']
            or row['claim_fence'] != current['claim_fence']
            or row['current_lease_seconds'] != current['lease_seconds']
            or row['claimed_boottime_ns']
            != current['claimed_boottime_ns']
            or row['lease_expires_boottime_ns']
            != current['lease_expires_boottime_ns']
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution current claim changed'
            )
    acknowledgements = connection.execute(
        '''
        SELECT *
        FROM monitor_room_gazebo_execution_acknowledgements
        WHERE outbox_id = ?
        ''',
        (row['outbox_id'],),
    ).fetchall()
    if row['state'] == 'prepared':
        if len(acknowledgements) != 1 or not claims:
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution acknowledgement is missing'
            )
        ack = acknowledgements[0]
        expected_ack = _acknowledgement_values(
            row,
            claims[-1],
            prepare_fingerprint=row['prepare_fingerprint'],
            prepared_boottime_ns=row['prepared_boottime_ns'],
        )
        if any(
            ack[name] != value
            for name, value in expected_ack.items()
        ) or (
            ack['simulation'] != 1
            or ack['physical_authorized'] != 0
            or ack['physical_effects'] != 0
        ):
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution acknowledgement changed'
            )
    elif acknowledgements:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution acknowledgement is premature'
        )
    if row['state'] == 'expired':
        if row['terminal_code'] not in {
            'deadline_expired',
            'delivery_attempts_exhausted',
            'host_boot_changed',
        }:
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo execution terminal code is invalid'
            )
    elif row['terminal_code'] is not None:
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo execution terminal code is premature'
        )


def _terminalize_unclaimable_locked(
    connection: sqlite3.Connection,
    *,
    expected_host_boot_id: str,
    now_boottime_ns: int,
    expected_outbox_id: Optional[str] = None,
) -> None:
    rows = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_outbox
        WHERE state IN ('pending', 'claimed')
          AND (? IS NULL OR outbox_id = ?)
        ORDER BY created_boottime_ns, outbox_id
        ''',
        (expected_outbox_id, expected_outbox_id),
    ).fetchall()
    for row in rows:
        code = None
        transition = now_boottime_ns
        if row['host_boot_id'] != expected_host_boot_id:
            code = 'host_boot_changed'
            transition = row['last_transition_boottime_ns']
        elif now_boottime_ns >= row['deadline_boottime_ns']:
            code = 'deadline_expired'
        elif (
            row['state'] == 'claimed'
            and row['attempt_count']
            >= GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS
            and now_boottime_ns >= row['lease_expires_boottime_ns']
        ):
            code = 'delivery_attempts_exhausted'
        if code is None:
            continue
        cursor = connection.execute(
            '''
            UPDATE monitor_room_gazebo_execution_outbox
            SET state = 'expired', terminal_code = ?,
                last_transition_boottime_ns = ?
            WHERE outbox_id = ? AND state IN ('pending', 'claimed')
              AND claim_fence = ?
            ''',
            (
                code,
                transition,
                row['outbox_id'],
                row['claim_fence'],
            ),
        )
        if cursor.rowcount != 1:
            raise GazeboExecutionOutboxConflictError(
                'Gazebo terminal transition changed'
            )


def claim_gazebo_execution_locked(
    connection: sqlite3.Connection,
    *,
    policy: GazeboSimulationExecutionPolicy,
    claim_request_id: str,
    lease_seconds: int,
    expected_outbox_id: Optional[str] = None,
    expected_operation_id: Optional[str] = None,
    expected_confirmation_request_id: Optional[str] = None,
) -> Optional[GazeboExecutionClaim]:
    """Lease one targeted or oldest payload with exact replay safety."""
    if not connection.in_transaction:
        raise GazeboExecutionOutboxAssuranceError(
            'Gazebo claim requires an owned write transaction'
        )
    if type(policy) is not GazeboSimulationExecutionPolicy:
        raise GazeboExecutionOutboxAssuranceError(
            'fixed Gazebo execution policy is required'
        )
    normalized_request = _identifier(
        claim_request_id, 'claim_request_id'
    )
    target_values = (
        expected_outbox_id,
        expected_operation_id,
        expected_confirmation_request_id,
    )
    targeted = any(value is not None for value in target_values)
    if targeted and not all(value is not None for value in target_values):
        raise GazeboExecutionOutboxAssuranceError(
            'Gazebo claim target binding is incomplete'
        )
    normalized_outbox = None
    normalized_operation = None
    normalized_confirmation = None
    if targeted:
        normalized_outbox = _identifier(
            expected_outbox_id, 'expected_outbox_id'
        )
        normalized_operation = _identifier(
            expected_operation_id, 'expected_operation_id'
        )
        normalized_confirmation = _identifier(
            expected_confirmation_request_id,
            'expected_confirmation_request_id',
        )
        if (
            not _valid_prefixed_identifier(
                normalized_outbox,
                'gazebo-execution-outbox-',
            )
            or not _valid_prefixed_identifier(
                normalized_operation,
                'gazebo-operation-',
            )
        ):
            raise GazeboExecutionOutboxAssuranceError(
                'Gazebo claim target binding is invalid'
            )
    if (
        type(lease_seconds) is not int
        or not GAZEBO_EXECUTION_OUTBOX_MIN_LEASE_SECONDS
        <= lease_seconds
        <= GAZEBO_EXECUTION_OUTBOX_MAX_LEASE_SECONDS
    ):
        raise ValueError('lease_seconds is invalid')
    validate_gazebo_execution_outbox_schema_locked(connection)
    _POLICY_CURRENT_HOST_BOOT_ID(policy)
    now = _POLICY_CURRENT_BOOTTIME_NS(policy)
    _terminalize_unclaimable_locked(
        connection,
        expected_host_boot_id=policy.expected_host_boot_id,
        now_boottime_ns=now,
        expected_outbox_id=(normalized_outbox if targeted else None),
    )
    prior = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_claims
        WHERE claim_request_id = ?
        ''',
        (normalized_request,),
    ).fetchone()
    if prior is not None:
        event = connection.execute(
            '''
            SELECT * FROM monitor_room_gazebo_execution_outbox
            WHERE outbox_id = ?
            ''',
            (prior['outbox_id'],),
        ).fetchone()
        _validate_outbox_row_locked(connection, event)
        if (
            prior['lease_seconds'] != lease_seconds
            or event['robot_id'] != policy.robot_id
            or event['host_boot_id'] != policy.expected_host_boot_id
            or event['current_claim_request_id'] != normalized_request
            or event['claim_fence'] != prior['claim_fence']
            or (
                targeted
                and (
                    event['outbox_id'] != normalized_outbox
                    or event['operation_id'] != normalized_operation
                    or event['confirmation_request_id']
                    != normalized_confirmation
                )
            )
        ):
            raise GazeboExecutionOutboxConflictError(
                'Gazebo claim request conflicts'
            )
        if event['state'] == 'prepared':
            return None
        if event['state'] != 'claimed':
            raise GazeboExecutionOutboxConflictError(
                'Gazebo claim is no longer current'
            )
        if (
            now >= prior['lease_expires_boottime_ns']
            or now >= event['deadline_boottime_ns']
        ):
            return None
        return _claim_from_rows(
            event,
            prior,
            _samples_for_outbox_locked(connection, event['outbox_id']),
        )
    if targeted:
        row = connection.execute(
            '''
            SELECT * FROM monitor_room_gazebo_execution_outbox
            WHERE outbox_id = ?
            ''',
            (normalized_outbox,),
        ).fetchone()
        if row is None:
            raise GazeboExecutionOutboxConflictError(
                'Gazebo claim target was not found'
            )
        _validate_outbox_row_locked(connection, row)
        if (
            row['operation_id'] != normalized_operation
            or row['confirmation_request_id']
            != normalized_confirmation
            or row['robot_id'] != policy.robot_id
        ):
            raise GazeboExecutionOutboxConflictError(
                'Gazebo claim target conflicts'
            )
        if (
            row['state'] in {'prepared', 'expired'}
            or row['host_boot_id'] != policy.expected_host_boot_id
            or row['deadline_boottime_ns'] <= now
        ):
            validate_gazebo_execution_outbox_schema_locked(connection)
            return None
        if (
            row['state'] == 'claimed'
            and row['lease_expires_boottime_ns'] > now
        ):
            validate_gazebo_execution_outbox_schema_locked(connection)
            return None
        if (
            row['state'] not in {'pending', 'claimed'}
            or row['claim_fence']
            >= GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS
        ):
            raise GazeboExecutionOutboxConflictError(
                'Gazebo claim target is unavailable'
            )
    else:
        row = connection.execute(
            '''
            SELECT * FROM monitor_room_gazebo_execution_outbox
            WHERE robot_id = ? AND host_boot_id = ?
              AND state IN ('pending', 'claimed')
              AND deadline_boottime_ns > ?
              AND claim_fence < ?
              AND (
                  state = 'pending'
                  OR lease_expires_boottime_ns <= ?
              )
            ORDER BY created_boottime_ns, outbox_id
            LIMIT 1
            ''',
            (
                policy.robot_id,
                policy.expected_host_boot_id,
                now,
                GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS,
                now,
            ),
        ).fetchone()
    if row is None:
        validate_gazebo_execution_outbox_schema_locked(connection)
        return None
    _validate_outbox_row_locked(connection, row)
    next_fence = row['claim_fence'] + 1
    lease_expires = min(
        now + lease_seconds * _NANOSECONDS_PER_SECOND,
        row['deadline_boottime_ns'],
    )
    if lease_expires <= now:
        raise GazeboExecutionOutboxConflictError(
            'Gazebo execution deadline expired'
        )
    values: Dict[str, Any] = {
        'schema_version': GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION,
        'claim_request_id': normalized_request,
        'outbox_id': row['outbox_id'],
        'operation_id': row['operation_id'],
        'claim_fence': next_fence,
        'attempt_number': next_fence,
        'claim_token': secrets.token_urlsafe(32),
        'lease_seconds': lease_seconds,
        'claimed_boottime_ns': now,
        'lease_expires_boottime_ns': lease_expires,
        'deadline_boottime_ns': row['deadline_boottime_ns'],
    }
    values['claim_request_fingerprint'] = _claim_record_fingerprint(values)
    claim_failed = False
    try:
        connection.execute(
            '''
            INSERT INTO monitor_room_gazebo_execution_claims (
                schema_version, claim_request_id,
                claim_request_fingerprint, outbox_id, operation_id,
                claim_fence, attempt_number, claim_token,
                lease_seconds, claimed_boottime_ns,
                lease_expires_boottime_ns, deadline_boottime_ns,
                simulation, physical_authorized, physical_effects
            ) VALUES (
                1, :claim_request_id, :claim_request_fingerprint,
                :outbox_id, :operation_id, :claim_fence,
                :attempt_number, :claim_token, :lease_seconds,
                :claimed_boottime_ns, :lease_expires_boottime_ns,
                :deadline_boottime_ns, 1, 0, 0
            )
            ''',
            values,
        )
        cursor = connection.execute(
            '''
            UPDATE monitor_room_gazebo_execution_outbox
            SET state = 'claimed', terminal_code = NULL,
                attempt_count = ?, claim_fence = ?,
                current_claim_request_id = ?,
                current_claim_request_fingerprint = ?,
                current_claim_token = ?, current_lease_seconds = ?,
                claimed_boottime_ns = ?,
                lease_expires_boottime_ns = ?,
                prepared_boottime_ns = NULL,
                prepare_fingerprint = NULL,
                last_transition_boottime_ns = ?
            WHERE outbox_id = ? AND state = ? AND claim_fence = ?
            ''',
            (
                next_fence,
                next_fence,
                normalized_request,
                values['claim_request_fingerprint'],
                values['claim_token'],
                lease_seconds,
                now,
                lease_expires,
                now,
                row['outbox_id'],
                row['state'],
                row['claim_fence'],
            ),
        )
    except sqlite3.IntegrityError:
        claim_failed = True
    if claim_failed:
        raise GazeboExecutionOutboxConflictError(
            'Gazebo execution claim conflict'
        )
    if cursor.rowcount != 1:
        raise GazeboExecutionOutboxConflictError(
            'Gazebo execution claim changed'
        )
    stored_event = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_outbox
        WHERE outbox_id = ?
        ''',
        (row['outbox_id'],),
    ).fetchone()
    stored_claim = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_claims
        WHERE claim_request_id = ?
        ''',
        (normalized_request,),
    ).fetchone()
    _validate_outbox_row_locked(connection, stored_event)
    return _claim_from_rows(
        stored_event,
        stored_claim,
        _samples_for_outbox_locked(connection, row['outbox_id']),
    )


def _acknowledgement_from_row(
    row: sqlite3.Row,
) -> GazeboExecutionAcknowledgement:
    return GazeboExecutionAcknowledgement(
        outbox_id=row['outbox_id'],
        operation_id=row['operation_id'],
        prepare_request_id=row['prepare_request_id'],
        prepare_fingerprint=row['prepare_fingerprint'],
        claim_fence=row['claim_fence'],
        prepared_boottime_ns=row['prepared_boottime_ns'],
    )


def acknowledge_gazebo_execution_locked(
    connection: sqlite3.Connection,
    *,
    policy: GazeboSimulationExecutionPolicy,
    outbox_id: str,
    claim_token: str,
    claim_fence: int,
    prepare_fingerprint: str,
) -> GazeboExecutionAcknowledgement:
    """Bind an exact prepare ACK to the current lease and all row payload."""
    if not connection.in_transaction:
        raise GazeboExecutionOutboxAssuranceError(
            'Gazebo acknowledgement requires an owned write transaction'
        )
    if type(policy) is not GazeboSimulationExecutionPolicy:
        raise GazeboExecutionOutboxAssuranceError(
            'fixed Gazebo execution policy is required'
        )
    normalized_outbox = _identifier(outbox_id, 'outbox_id')
    normalized_token = claim_token
    normalized_prepare = _digest(
        prepare_fingerprint, 'prepare_fingerprint'
    )
    if (
        not _valid_prefixed_identifier(
            normalized_outbox, 'gazebo-execution-outbox-'
        )
        or type(normalized_token) is not str
        or not _CLAIM_TOKEN.fullmatch(normalized_token)
        or type(claim_fence) is not int
        or not 1 <= claim_fence <= GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS
    ):
        raise GazeboExecutionOutboxConflictError(
            'Gazebo acknowledgement is invalid'
        )
    validate_gazebo_execution_outbox_schema_locked(connection)
    _POLICY_CURRENT_HOST_BOOT_ID(policy)
    event = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_outbox
        WHERE outbox_id = ?
        ''',
        (normalized_outbox,),
    ).fetchone()
    if event is None:
        raise GazeboExecutionOutboxConflictError(
            'Gazebo execution outbox was not found'
        )
    _validate_outbox_row_locked(connection, event)
    exact = (
        event['robot_id'] == policy.robot_id
        and event['host_boot_id'] == policy.expected_host_boot_id
        and event['claim_fence'] == claim_fence
        and type(event['current_claim_token']) is str
        and hmac.compare_digest(
            event['current_claim_token'], normalized_token
        )
    )
    if event['state'] == 'prepared':
        if (
            not exact
            or event['prepare_fingerprint'] != normalized_prepare
        ):
            raise GazeboExecutionOutboxConflictError(
                'Gazebo acknowledgement conflicts'
            )
        ack = connection.execute(
            '''
            SELECT *
            FROM monitor_room_gazebo_execution_acknowledgements
            WHERE outbox_id = ?
            ''',
            (normalized_outbox,),
        ).fetchone()
        if ack is None:
            raise GazeboExecutionOutboxSchemaError(
                'Gazebo acknowledgement row is missing'
            )
        return _acknowledgement_from_row(ack)
    if event['state'] != 'claimed' or not exact:
        raise GazeboExecutionOutboxConflictError(
            'Gazebo acknowledgement claim is stale'
        )
    now = _POLICY_CURRENT_BOOTTIME_NS(policy)
    if (
        now < event['claimed_boottime_ns']
        or now >= event['lease_expires_boottime_ns']
        or now >= event['deadline_boottime_ns']
    ):
        raise GazeboExecutionOutboxConflictError(
            'Gazebo acknowledgement claim lease expired'
        )
    claim = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_claims
        WHERE claim_request_id = ?
        ''',
        (event['current_claim_request_id'],),
    ).fetchone()
    if (
        claim is None
        or claim['outbox_id'] != event['outbox_id']
        or claim['operation_id'] != event['operation_id']
        or claim['claim_fence'] != claim_fence
        or claim['claim_request_fingerprint']
        != event['current_claim_request_fingerprint']
        or not hmac.compare_digest(
            claim['claim_token'], normalized_token
        )
    ):
        raise GazeboExecutionOutboxSchemaError(
            'Gazebo acknowledgement claim binding changed'
        )
    # Reconstructing this DTO revalidates the full prepare binding and every
    # ordered private sample before an ACK can become terminal.
    _claim_from_rows(
        event,
        claim,
        _samples_for_outbox_locked(connection, event['outbox_id']),
    )
    values = _acknowledgement_values(
        event,
        claim,
        prepare_fingerprint=normalized_prepare,
        prepared_boottime_ns=now,
    )
    acknowledgement_failed = False
    try:
        connection.execute(
            '''
            INSERT INTO
                monitor_room_gazebo_execution_acknowledgements (
                    schema_version, acknowledgement_id,
                    acknowledgement_fingerprint, outbox_id,
                    operation_id, prepare_request_id,
                    claim_request_id, claim_request_fingerprint,
                    claim_fence, claim_token, claim_token_digest,
                    prepare_fingerprint, prepared_boottime_ns,
                    simulation, physical_authorized, physical_effects
                ) VALUES (
                    1, :acknowledgement_id,
                    :acknowledgement_fingerprint, :outbox_id,
                    :operation_id, :prepare_request_id,
                    :claim_request_id, :claim_request_fingerprint,
                    :claim_fence, :claim_token, :claim_token_digest,
                    :prepare_fingerprint, :prepared_boottime_ns,
                    1, 0, 0
                )
            ''',
            values,
        )
        cursor = connection.execute(
            '''
            UPDATE monitor_room_gazebo_execution_outbox
            SET state = 'prepared', terminal_code = NULL,
                prepared_boottime_ns = ?, prepare_fingerprint = ?,
                last_transition_boottime_ns = ?
            WHERE outbox_id = ? AND state = 'claimed'
              AND claim_fence = ? AND current_claim_token = ?
              AND current_claim_request_fingerprint = ?
            ''',
            (
                now,
                normalized_prepare,
                now,
                normalized_outbox,
                claim_fence,
                normalized_token,
                claim['claim_request_fingerprint'],
            ),
        )
    except sqlite3.IntegrityError:
        acknowledgement_failed = True
    if acknowledgement_failed:
        raise GazeboExecutionOutboxConflictError(
            'Gazebo acknowledgement conflict'
        )
    if cursor.rowcount != 1:
        raise GazeboExecutionOutboxConflictError(
            'Gazebo acknowledgement changed'
        )
    stored = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_outbox
        WHERE outbox_id = ?
        ''',
        (normalized_outbox,),
    ).fetchone()
    _validate_outbox_row_locked(connection, stored)
    ack = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_acknowledgements
        WHERE outbox_id = ?
        ''',
        (normalized_outbox,),
    ).fetchone()
    return _acknowledgement_from_row(ack)


def resolve_prepared_gazebo_execution_locked(
    connection: sqlite3.Connection,
    *,
    policy: GazeboSimulationExecutionPolicy,
    confirmation_request_id: str,
    expected_user_id: str,
    execution_scope: str,
) -> GazeboPreparedExecutionAuthority:
    """
    Rederive a prepared selector from the durable source and exact ACK.

    The server-side caller supplies only the confirmation identifier and its
    fixed authenticated user and a closed server-selected scope.  The
    operation, outbox, fence, and per-request owner binding are derived here
    rather than trusted as execution inputs.  Only ``drive`` expires; exact
    read-only observation and cancellation/reconciliation remain available.
    """
    if not connection.in_transaction:
        raise GazeboExecutionOutboxAssuranceError(
            'Prepared Gazebo execution resolution requires a transaction'
        )
    if type(policy) is not GazeboSimulationExecutionPolicy:
        raise GazeboExecutionOutboxAssuranceError(
            'fixed Gazebo execution policy is required'
        )
    normalized_confirmation = _identifier(
        confirmation_request_id,
        'confirmation_request_id',
    )
    normalized_user = validate_user_id(expected_user_id)
    if (
        type(execution_scope) is not str
        or execution_scope not in {'drive', 'observe', 'cancel'}
    ):
        raise GazeboExecutionOutboxAssuranceError(
            'Gazebo execution scope is invalid'
        )
    validate_gazebo_execution_outbox_schema_locked(connection)
    current_boot_id = _POLICY_CURRENT_HOST_BOOT_ID(policy)
    current_boottime_ns = _POLICY_CURRENT_BOOTTIME_NS(policy)
    row = connection.execute(
        '''
        SELECT * FROM monitor_room_gazebo_execution_outbox
        WHERE confirmation_request_id = ?
        ''',
        (normalized_confirmation,),
    ).fetchone()
    if row is None:
        raise GazeboExecutionOutboxConflictError(
            'Prepared Gazebo execution was not found'
        )
    _validate_outbox_row_locked(connection, row)
    source = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_ledger
        WHERE confirmation_request_id = ?
        ''',
        (row['confirmation_request_id'],),
    ).fetchone()
    confirmation = connection.execute(
        '''
        SELECT user_id, conversation_id, session_instance_id,
               generation, revision, ordinal
        FROM confirmation_intents
        WHERE confirmation_request_id = ?
        ''',
        (row['confirmation_request_id'],),
    ).fetchone()
    acknowledgement = connection.execute(
        '''
        SELECT *
        FROM monitor_room_gazebo_execution_acknowledgements
        WHERE outbox_id = ?
        ''',
        (row['outbox_id'],),
    ).fetchone()
    if (
        row['state'] != 'prepared'
        or row['confirmation_request_id'] != normalized_confirmation
        or row['robot_id'] != policy.robot_id
        or row['host_boot_id'] != policy.expected_host_boot_id
        or row['host_boot_id'] != current_boot_id
        or current_boottime_ns < row['prepared_boottime_ns']
        or (
            execution_scope == 'drive'
            and current_boottime_ns >= row['deadline_boottime_ns']
        )
        or source is None
        or confirmation is None
        or confirmation['user_id'] != normalized_user
        or source['owner_binding_digest'] != _canonical_hash(
            {
                'user_id': confirmation['user_id'],
                'conversation_id': confirmation['conversation_id'],
                'session_instance_id': (
                    confirmation['session_instance_id']
                ),
                'generation': int(confirmation['generation']),
                'revision': int(confirmation['revision']),
                'ordinal': int(confirmation['ordinal']),
            }
        )
        or source['confirmation_request_id']
        != row['confirmation_request_id']
        or acknowledgement is None
        or acknowledgement['outbox_id'] != row['outbox_id']
        or acknowledgement['operation_id'] != row['operation_id']
        or acknowledgement['claim_fence'] != row['claim_fence']
        or acknowledgement['prepare_fingerprint']
        != row['prepare_fingerprint']
    ):
        raise GazeboExecutionOutboxConflictError(
            'Prepared Gazebo execution selector does not match'
        )
    authority = GazeboPreparedExecutionAuthority(
        confirmation_request_id=row['confirmation_request_id'],
        outbox_id=row['outbox_id'],
        operation_id=row['operation_id'],
        claim_fence=row['claim_fence'],
        owner_binding_digest=source['owner_binding_digest'],
        prepare_fingerprint=acknowledgement['prepare_fingerprint'],
        acknowledgement_fingerprint=(
            acknowledgement['acknowledgement_fingerprint']
        ),
        host_boot_id=row['host_boot_id'],
        prepared_boottime_ns=row['prepared_boottime_ns'],
        deadline_boottime_ns=row['deadline_boottime_ns'],
        execution_scope=execution_scope,
    )
    authority.binding_digest
    return authority


__all__ = [
    'GAZEBO_EXECUTION_OUTBOX_MAX_ATTEMPTS',
    'GAZEBO_EXECUTION_OUTBOX_MAX_LEASE_SECONDS',
    'GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES',
    'GAZEBO_EXECUTION_OUTBOX_MIN_LEASE_SECONDS',
    'GAZEBO_EXECUTION_OUTBOX_SCHEMA_VERSION',
    'GazeboExecutionAcknowledgement',
    'GazeboExecutionClaim',
    'GazeboExecutionEnqueue',
    'GazeboPreparedExecutionAuthority',
    'GazeboExecutionOutboxAssuranceError',
    'GazeboExecutionOutboxConflictError',
    'GazeboExecutionOutboxError',
    'GazeboExecutionOutboxSchemaError',
    'GazeboExecutionOutboxUpgradeRequiredError',
    'GazeboExecutionSample',
    'GazeboSemanticEvidenceSource',
    'GazeboSimulationConsumeResult',
    'GazeboSimulationExecutionPolicy',
    'acknowledge_gazebo_execution_locked',
    'claim_gazebo_execution_locked',
    'get_gazebo_execution_enqueue_for_receipt_locked',
    'prepare_gazebo_execution_outbox_schema_locked',
    'record_gazebo_execution_outbox_locked',
    'resolve_prepared_gazebo_execution_locked',
    'validate_gazebo_execution_outbox_schema_locked',
]
