"""
Terminal-only, simulation-only consumption of room confirmations.

This module deliberately cannot call ROS, Nav2, cameras, files, or network
services.  It owns a tiny append-only SQLite ledger for one bounded pure
Python simulation.  A later physical mission runner must use a different
outbox, lease, fencing, and reconciliation contract.
"""

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Optional, Protocol

from malbut_agent_server.monitor_room_coverage import (
    CoveragePlan,
    CoveragePlanningResult,
    CoverageSample,
    DEFAULT_COVERAGE_PROFILE,
    PLANNER_REVISION,
    build_monitor_room_coverage_plan,
)
from malbut_agent_server.monitor_room_target import Effects, TargetBinding
from malbut_agent_server.schemas import ValidationError, validate_user_id


SIMULATION_LEDGER_SCHEMA_VERSION = 4
SIMULATION_CONTRACT_VERSION = 4
SIMULATION_PROFILE_REVISION = PLANNER_REVISION
SIMULATION_ASSURANCE_LEVEL = 'trusted_simulation_harness'

_LEGACY_SIMULATION_PROFILE_REVISION = 'monitor-room-pure-v1'

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_INVALIDATION_CODES = frozenset(
    {
        'simulation_binding_upgrade_required',
        'simulation_confirmation_expired',
        'simulation_conversation_changed',
        'simulation_conversation_inactive',
        'simulation_conversation_not_found',
        'simulation_effects_changed',
        'simulation_target_changed',
    }
)


SIMULATION_SCHEMA_METADATA_TABLE_SQL = '''
CREATE TABLE monitor_room_simulation_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    activated_at REAL NOT NULL CHECK (activated_at >= 0),
    activation_epoch TEXT NOT NULL
    CHECK (
        length(activation_epoch) = 64
        AND activation_epoch NOT GLOB '*[^0-9a-f]*'
    )
)
'''


SIMULATION_PREACTIVATION_PROPOSALS_TABLE_SQL = '''
CREATE TABLE monitor_room_simulation_preactivation_proposals (
    proposal_fingerprint TEXT NOT NULL PRIMARY KEY,
    activation_epoch TEXT NOT NULL,
    snapshot_rowid INTEGER NOT NULL CHECK (snapshot_rowid > 0),
    snapshotted_at REAL NOT NULL CHECK (snapshotted_at >= 0),
    CHECK (
        length(activation_epoch) = 64
        AND activation_epoch NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(proposal_fingerprint) = 64
        AND proposal_fingerprint NOT GLOB '*[^0-9a-f]*'
    )
)
'''


SIMULATION_WRITE_FENCE_TABLE_SQL = '''
CREATE TABLE monitor_room_simulation_write_fence (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    fence INTEGER NOT NULL CHECK (fence = 0)
)
'''


SIMULATION_ELIGIBILITY_TABLE_SQL = '''
CREATE TABLE monitor_room_simulation_eligibility (
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    contract_version INTEGER NOT NULL CHECK (contract_version = 3),
    activation_epoch TEXT NOT NULL,
    confirmation_rowid INTEGER NOT NULL CHECK (confirmation_rowid > 0),
    proposal_fingerprint TEXT NOT NULL,
    target_binding_digest TEXT NOT NULL,
    effects_digest TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (confirmation_request_id)
        REFERENCES confirmation_intents (confirmation_request_id)
        ON DELETE CASCADE,
    CHECK (
        length(activation_epoch) = 64
        AND activation_epoch NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(proposal_fingerprint) = 64
        AND proposal_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(target_binding_digest) = 64
        AND target_binding_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(effects_digest) = 64
        AND effects_digest NOT GLOB '*[^0-9a-f]*'
    )
)
'''


SIMULATION_LEDGER_TABLE_SQL = '''
CREATE TABLE monitor_room_simulation_ledger (
    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    confirmation_result_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL UNIQUE,
    consume_request_id TEXT NOT NULL,
    consume_fingerprint TEXT NOT NULL,
    actor_binding_digest TEXT NOT NULL,
    owner_binding_digest TEXT NOT NULL,
    proposal_fingerprint TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    target_binding_digest TEXT NOT NULL,
    effects_digest TEXT NOT NULL,
    simulation_profile_revision TEXT NOT NULL,
    confirmation_issued_at REAL NOT NULL,
    confirmation_expires_at REAL NOT NULL,
    completed_at REAL NOT NULL,
    tool_call_id TEXT UNIQUE,
    mission_id TEXT UNIQUE,
    operation_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN ('succeeded', 'failed', 'invalidated')
    ),
    result_code TEXT NOT NULL,
    confirmation_spent INTEGER NOT NULL DEFAULT 1
        CHECK (confirmation_spent = 1),
    simulation_authority_issued INTEGER NOT NULL,
    simulation INTEGER NOT NULL DEFAULT 1 CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL DEFAULT 0
        CHECK (physical_effects = 0),
    viewer_live INTEGER NOT NULL DEFAULT 0 CHECK (viewer_live = 0),
    authority_kind TEXT NOT NULL,
    CHECK (confirmation_expires_at > confirmation_issued_at),
    CHECK (
        length(consume_fingerprint) = 64
        AND consume_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(actor_binding_digest) = 64
        AND actor_binding_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(owner_binding_digest) = 64
        AND owner_binding_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(proposal_fingerprint) = 64
        AND proposal_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(arguments_digest) = 64
        AND arguments_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(target_binding_digest) = 64
        AND target_binding_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(effects_digest) = 64
        AND effects_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        (state IN ('succeeded', 'failed')
         AND simulation_authority_issued = 1
         AND authority_kind = 'simulation_only'
         AND tool_call_id IS NOT NULL
         AND mission_id IS NOT NULL
         AND operation_id IS NOT NULL)
        OR
        (state = 'invalidated'
         AND result_code IN (
             'simulation_binding_upgrade_required',
             'simulation_confirmation_expired',
             'simulation_conversation_changed',
             'simulation_conversation_inactive',
             'simulation_conversation_not_found',
             'simulation_effects_changed',
             'simulation_target_changed'
         )
         AND simulation_authority_issued = 0
         AND authority_kind = 'none'
         AND tool_call_id IS NULL
         AND mission_id IS NULL
         AND operation_id IS NULL)
    ),
    CHECK (
        (state = 'succeeded' AND result_code = 'simulation_succeeded')
        OR
        (state = 'failed' AND result_code IN (
            'simulation_failed', 'simulation_result_invalid'
        ))
        OR state = 'invalidated'
    )
)
'''


SIMULATION_APPROVAL_CONSUME_INDEX_SQL = '''
CREATE UNIQUE INDEX monitor_room_simulation_approval_consume_idx
ON monitor_room_simulation_ledger (
    actor_binding_digest,
    consume_request_id
)
'''


SIMULATION_NO_UPDATE_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_no_update
BEFORE UPDATE ON monitor_room_simulation_ledger
BEGIN
    SELECT RAISE(ABORT, 'simulation ledger is immutable');
END
'''


SIMULATION_NO_DELETE_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_no_delete
BEFORE DELETE ON monitor_room_simulation_ledger
BEGIN
    SELECT RAISE(ABORT, 'simulation ledger is immutable');
END
'''


SIMULATION_NO_REPLACE_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_no_replace
BEFORE INSERT ON monitor_room_simulation_ledger
WHEN EXISTS (
    SELECT 1 FROM monitor_room_simulation_ledger
    WHERE confirmation_request_id = NEW.confirmation_request_id
       OR confirmation_result_id = NEW.confirmation_result_id
       OR decision_id = NEW.decision_id
       OR (
           actor_binding_digest = NEW.actor_binding_digest
           AND consume_request_id = NEW.consume_request_id
       )
       OR (
           NEW.tool_call_id IS NOT NULL
           AND tool_call_id = NEW.tool_call_id
       )
       OR (
           NEW.mission_id IS NOT NULL
           AND mission_id = NEW.mission_id
       )
       OR (
           NEW.operation_id IS NOT NULL
           AND operation_id = NEW.operation_id
       )
)
BEGIN
    SELECT RAISE(ABORT, 'simulation ledger identity is immutable');
END
'''


SIMULATION_ELIGIBILITY_GUARD_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_eligibility_guard
BEFORE INSERT ON monitor_room_simulation_eligibility
WHEN NOT EXISTS (
    SELECT 1
    FROM confirmation_intents AS confirmation
    CROSS JOIN monitor_room_simulation_schema_metadata AS metadata
    WHERE metadata.singleton = 1
      AND confirmation.rowid = NEW.confirmation_rowid
      AND confirmation.confirmation_request_id = NEW.confirmation_request_id
      AND confirmation.schema_version = 3
      AND confirmation.state = 'pending'
      AND NEW.contract_version = 3
      AND NEW.activation_epoch = metadata.activation_epoch
      AND NEW.proposal_fingerprint = confirmation.proposal_fingerprint
      AND NEW.target_binding_digest = confirmation.target_binding_digest
      AND NEW.effects_digest = confirmation.effects_digest
      AND NEW.created_at = confirmation.created_at
      AND NOT EXISTS (
          SELECT 1
          FROM monitor_room_simulation_preactivation_proposals AS denied
          WHERE denied.proposal_fingerprint = NEW.proposal_fingerprint
      )
)
BEGIN
    SELECT RAISE(ABORT, 'simulation eligibility is not activatable');
END
'''


SIMULATION_ELIGIBILITY_NO_UPDATE_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_eligibility_no_update
BEFORE UPDATE ON monitor_room_simulation_eligibility
BEGIN
    SELECT RAISE(ABORT, 'simulation eligibility is immutable');
END
'''


SIMULATION_METADATA_NO_UPDATE_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_metadata_no_update
BEFORE UPDATE ON monitor_room_simulation_schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'simulation metadata is immutable');
END
'''


SIMULATION_METADATA_NO_DELETE_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_metadata_no_delete
BEFORE DELETE ON monitor_room_simulation_schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'simulation metadata is immutable');
END
'''


SIMULATION_METADATA_NO_REPLACE_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_metadata_no_replace
BEFORE INSERT ON monitor_room_simulation_schema_metadata
WHEN EXISTS (
    SELECT 1 FROM monitor_room_simulation_schema_metadata
    WHERE singleton = NEW.singleton
)
BEGIN
    SELECT RAISE(ABORT, 'simulation metadata is immutable');
END
'''


SIMULATION_PREACTIVATION_NO_UPDATE_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_preactivation_no_update
BEFORE UPDATE ON monitor_room_simulation_preactivation_proposals
BEGIN
    SELECT RAISE(ABORT, 'simulation preactivation proposal is immutable');
END
'''


SIMULATION_PREACTIVATION_NO_DELETE_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_preactivation_no_delete
BEFORE DELETE ON monitor_room_simulation_preactivation_proposals
BEGIN
    SELECT RAISE(ABORT, 'simulation preactivation proposal is immutable');
END
'''


SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_preactivation_no_insert
BEFORE INSERT ON monitor_room_simulation_preactivation_proposals
BEGIN
    SELECT RAISE(ABORT, 'simulation preactivation proposal is immutable');
END
'''


# Keep the exact v3 definitions available solely to authenticate a migration.
# A v3 terminal row is copied as an audit-only legacy receipt; it is never
# upgraded into semantic-plan evidence.
_V3_SIMULATION_SCHEMA_METADATA_TABLE_SQL = (
    SIMULATION_SCHEMA_METADATA_TABLE_SQL
)
_V3_SIMULATION_ELIGIBILITY_TABLE_SQL = SIMULATION_ELIGIBILITY_TABLE_SQL
_V3_SIMULATION_LEDGER_TABLE_SQL = SIMULATION_LEDGER_TABLE_SQL
_V3_SIMULATION_ELIGIBILITY_GUARD_TRIGGER_SQL = (
    SIMULATION_ELIGIBILITY_GUARD_TRIGGER_SQL
)


SIMULATION_SCHEMA_METADATA_TABLE_SQL = '''
CREATE TABLE monitor_room_simulation_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 4),
    activated_at REAL NOT NULL CHECK (activated_at >= 0),
    activation_epoch TEXT NOT NULL
    CHECK (
        length(activation_epoch) = 64
        AND activation_epoch NOT GLOB '*[^0-9a-f]*'
    )
)
'''


SIMULATION_ELIGIBILITY_TABLE_SQL = '''
CREATE TABLE monitor_room_simulation_eligibility (
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    contract_version INTEGER NOT NULL CHECK (contract_version = 4),
    activation_epoch TEXT NOT NULL,
    confirmation_rowid INTEGER NOT NULL CHECK (confirmation_rowid > 0),
    proposal_fingerprint TEXT NOT NULL,
    target_binding_digest TEXT NOT NULL,
    effects_digest TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (confirmation_request_id)
        REFERENCES confirmation_intents (confirmation_request_id)
        ON DELETE CASCADE,
    CHECK (
        length(activation_epoch) = 64
        AND activation_epoch NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(proposal_fingerprint) = 64
        AND proposal_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(target_binding_digest) = 64
        AND target_binding_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(effects_digest) = 64
        AND effects_digest NOT GLOB '*[^0-9a-f]*'
    )
)
'''


SIMULATION_LEDGER_TABLE_SQL = f'''
CREATE TABLE monitor_room_simulation_ledger (
    schema_version INTEGER NOT NULL CHECK (schema_version IN (3, 4)),
    record_kind TEXT NOT NULL CHECK (
        record_kind IN (
            'legacy_unplanned',
            'planned',
            'planning_failed',
            'invalidated'
        )
    ),
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    confirmation_result_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL UNIQUE,
    consume_request_id TEXT NOT NULL,
    consume_fingerprint TEXT NOT NULL,
    actor_binding_digest TEXT NOT NULL,
    owner_binding_digest TEXT NOT NULL,
    proposal_fingerprint TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    target_binding_digest TEXT NOT NULL,
    effects_digest TEXT NOT NULL,
    planner_revision TEXT,
    profile_digest TEXT,
    plan_digest TEXT,
    result_digest TEXT,
    sample_count INTEGER,
    component_count INTEGER,
    receipt_digest TEXT,
    confirmation_issued_at REAL NOT NULL,
    confirmation_expires_at REAL NOT NULL,
    completed_at REAL NOT NULL,
    tool_call_id TEXT UNIQUE,
    mission_id TEXT UNIQUE,
    operation_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN ('succeeded', 'failed', 'invalidated')
    ),
    result_code TEXT NOT NULL,
    confirmation_spent INTEGER NOT NULL DEFAULT 1
        CHECK (confirmation_spent = 1),
    simulation_authority_issued INTEGER NOT NULL,
    simulation INTEGER NOT NULL DEFAULT 1 CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL DEFAULT 0
        CHECK (physical_effects = 0),
    viewer_live INTEGER NOT NULL DEFAULT 0 CHECK (viewer_live = 0),
    nav2_validated INTEGER NOT NULL DEFAULT 0 CHECK (nav2_validated = 0),
    camera_coverage_validated INTEGER NOT NULL DEFAULT 0
        CHECK (camera_coverage_validated = 0),
    coverage_achieved INTEGER NOT NULL DEFAULT 0
        CHECK (coverage_achieved = 0),
    authority_kind TEXT NOT NULL,
    CHECK (
        typeof(confirmation_issued_at) IN ('integer', 'real')
        AND confirmation_issued_at >= 0
        AND confirmation_issued_at <= 1.7976931348623157e308
    ),
    CHECK (
        typeof(confirmation_expires_at) IN ('integer', 'real')
        AND confirmation_expires_at > confirmation_issued_at
        AND confirmation_expires_at <= 1.7976931348623157e308
    ),
    CHECK (
        typeof(completed_at) IN ('integer', 'real')
        AND completed_at >= confirmation_issued_at
        AND completed_at <= 1.7976931348623157e308
    ),
    CHECK (
        state = 'invalidated'
        OR completed_at < confirmation_expires_at
    ),
    CHECK (
        length(consume_fingerprint) = 64
        AND consume_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(actor_binding_digest) = 64
        AND actor_binding_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(owner_binding_digest) = 64
        AND owner_binding_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(proposal_fingerprint) = 64
        AND proposal_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(arguments_digest) = 64
        AND arguments_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(target_binding_digest) = 64
        AND target_binding_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        length(effects_digest) = 64
        AND effects_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        profile_digest IS NULL
        OR (
            length(profile_digest) = 64
            AND profile_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK (
        plan_digest IS NULL
        OR (
            length(plan_digest) = 64
            AND plan_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK (
        result_digest IS NULL
        OR (
            length(result_digest) = 64
            AND result_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK (
        receipt_digest IS NULL
        OR (
            length(receipt_digest) = 64
            AND receipt_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK (
        (state IN ('succeeded', 'failed')
         AND simulation_authority_issued = 1
         AND authority_kind = 'simulation_only'
         AND tool_call_id IS NOT NULL
         AND mission_id IS NOT NULL
         AND operation_id IS NOT NULL)
        OR
        (state = 'invalidated'
         AND result_code IN (
             'simulation_binding_upgrade_required',
             'simulation_confirmation_expired',
             'simulation_conversation_changed',
             'simulation_conversation_inactive',
             'simulation_conversation_not_found',
             'simulation_effects_changed',
             'simulation_target_changed'
         )
         AND simulation_authority_issued = 0
         AND authority_kind = 'none'
         AND tool_call_id IS NULL
         AND mission_id IS NULL
         AND operation_id IS NULL)
    ),
    CHECK (
        (
            record_kind = 'legacy_unplanned'
            AND schema_version = 3
            AND planner_revision IS NULL
            AND profile_digest IS NULL
            AND plan_digest IS NULL
            AND result_digest IS NULL
            AND sample_count IS NULL
            AND component_count IS NULL
            AND receipt_digest IS NULL
            AND (
                (state = 'succeeded'
                 AND result_code = 'simulation_succeeded')
                OR
                (state = 'failed'
                 AND result_code IN (
                     'simulation_failed',
                     'simulation_result_invalid'
                 ))
                OR state = 'invalidated'
            )
        )
        OR
        (
            record_kind = 'planned'
            AND schema_version = 4
            AND state = 'succeeded'
            AND result_code = 'semantic_sample_plan_created'
            AND planner_revision = '{PLANNER_REVISION}'
            AND profile_digest = '{DEFAULT_COVERAGE_PROFILE.digest}'
            AND plan_digest IS NOT NULL
            AND result_digest IS NOT NULL
            AND typeof(sample_count) = 'integer'
            AND sample_count BETWEEN 1 AND 4096
            AND typeof(component_count) = 'integer'
            AND component_count BETWEEN 1 AND 128
            AND receipt_digest IS NOT NULL
        )
        OR
        (
            record_kind = 'planning_failed'
            AND schema_version = 4
            AND state = 'failed'
            AND result_code IN (
                'semantic_sample_planning_failed',
                'semantic_sample_result_invalid'
            )
            AND planner_revision = '{PLANNER_REVISION}'
            AND profile_digest = '{DEFAULT_COVERAGE_PROFILE.digest}'
            AND plan_digest IS NULL
            AND result_digest IS NOT NULL
            AND typeof(sample_count) = 'integer'
            AND sample_count = 0
            AND typeof(component_count) = 'integer'
            AND component_count = 0
            AND receipt_digest IS NOT NULL
        )
        OR
        (
            record_kind = 'invalidated'
            AND schema_version = 4
            AND state = 'invalidated'
            AND planner_revision IS NULL
            AND profile_digest IS NULL
            AND plan_digest IS NULL
            AND result_digest IS NULL
            AND sample_count IS NULL
            AND component_count IS NULL
            AND receipt_digest IS NOT NULL
        )
    )
)
'''


SIMULATION_ELIGIBILITY_GUARD_TRIGGER_SQL = '''
CREATE TRIGGER monitor_room_simulation_eligibility_guard
BEFORE INSERT ON monitor_room_simulation_eligibility
WHEN NOT EXISTS (
    SELECT 1
    FROM confirmation_intents AS confirmation
    CROSS JOIN monitor_room_simulation_schema_metadata AS metadata
    WHERE metadata.singleton = 1
      AND confirmation.rowid = NEW.confirmation_rowid
      AND confirmation.confirmation_request_id = NEW.confirmation_request_id
      AND confirmation.schema_version = 3
      AND confirmation.state = 'pending'
      AND NEW.contract_version = 4
      AND NEW.activation_epoch = metadata.activation_epoch
      AND NEW.proposal_fingerprint = confirmation.proposal_fingerprint
      AND NEW.target_binding_digest = confirmation.target_binding_digest
      AND NEW.effects_digest = confirmation.effects_digest
      AND NEW.created_at = confirmation.created_at
      AND NOT EXISTS (
          SELECT 1
          FROM monitor_room_simulation_preactivation_proposals AS denied
          WHERE denied.proposal_fingerprint = NEW.proposal_fingerprint
      )
)
BEGIN
    SELECT RAISE(ABORT, 'simulation eligibility is not activatable');
END
'''


class SimulationExecutionSchemaError(RuntimeError):
    """Raised when the private simulation ledger is incompatible."""


class SimulationExecutionNotFoundError(ValidationError):
    """Hide whether a confirmation or owner selector was wrong."""


class SimulationApprovalRequiredError(ValidationError):
    """Raised when the durable confirmation is not an approval."""


class SimulationAssuranceError(ValidationError):
    """Raised when no trusted simulation-only actor proof is present."""


class SimulationConsumeConflictError(ValidationError):
    """Raised when an idempotency identifier is reused differently."""


class SimulationExecutionAlreadyConsumedError(ValidationError):
    """Raised when another request already spent the confirmation."""


class SimulationExecutionContractUpgradeRequiredError(ValidationError):
    """Raised when a preserved v3 receipt is audit-only under v4."""


class SimulationExecutionTrustVerifier(Protocol):
    """Configuration-time trust root for one simulation preflight."""

    def verify_receipt(
        self,
        approval: 'VerifiedSimulationApproval',
        request: 'SimulationConsumeRequest',
        now: float,
    ) -> None:
        """Authenticate an exact terminal-receipt lookup."""
        ...

    def verify(
        self,
        approval: 'VerifiedSimulationApproval',
        request: 'SimulationConsumeRequest',
        now: float,
    ) -> TargetBinding:
        """Authenticate the actor and freshly observed semantic target."""
        ...


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValidationError(f'{field_name} is invalid')
    return value


def _digest(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise ValidationError(f'{field_name} is invalid')
    return value


def _timestamp(value: Any, field_name: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError(f'{field_name} is invalid') from None
    if not math.isfinite(normalized) or normalized < 0:
        raise ValidationError(f'{field_name} is invalid')
    return 0.0 if normalized == 0 else normalized


def _hash_json(value: Dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _terminal_receipt_digest(row: Any) -> str:
    """Bind every content-free v4 terminal column except this digest."""
    return _hash_json(
        {
            'schema_version': int(row['schema_version']),
            'record_kind': row['record_kind'],
            'confirmation_request_id': row['confirmation_request_id'],
            'confirmation_result_id': row['confirmation_result_id'],
            'decision_id': row['decision_id'],
            'consume_request_id': row['consume_request_id'],
            'consume_fingerprint': row['consume_fingerprint'],
            'actor_binding_digest': row['actor_binding_digest'],
            'owner_binding_digest': row['owner_binding_digest'],
            'proposal_fingerprint': row['proposal_fingerprint'],
            'arguments_digest': row['arguments_digest'],
            'target_binding_digest': row['target_binding_digest'],
            'effects_digest': row['effects_digest'],
            'planner_revision': row['planner_revision'],
            'profile_digest': row['profile_digest'],
            'plan_digest': row['plan_digest'],
            'result_digest': row['result_digest'],
            'sample_count': row['sample_count'],
            'component_count': row['component_count'],
            'confirmation_issued_at': float(
                row['confirmation_issued_at']
            ),
            'confirmation_expires_at': float(
                row['confirmation_expires_at']
            ),
            'completed_at': float(row['completed_at']),
            'tool_call_id': row['tool_call_id'],
            'mission_id': row['mission_id'],
            'operation_id': row['operation_id'],
            'state': row['state'],
            'result_code': row['result_code'],
            'confirmation_spent': int(row['confirmation_spent']),
            'simulation_authority_issued': int(
                row['simulation_authority_issued']
            ),
            'simulation': int(row['simulation']),
            'physical_authorized': int(row['physical_authorized']),
            'physical_effects': int(row['physical_effects']),
            'viewer_live': int(row['viewer_live']),
            'nav2_validated': int(row['nav2_validated']),
            'camera_coverage_validated': int(
                row['camera_coverage_validated']
            ),
            'coverage_achieved': int(row['coverage_achieved']),
            'authority_kind': row['authority_kind'],
        }
    )


@dataclass(frozen=True)
class VerifiedSimulationApproval:
    """Structural claim trusted only after configured proof verification."""

    user_id: str
    principal_binding_digest: str = field(repr=False)
    confirmation_request_id: str
    confirmation_result_id: str
    proposal_fingerprint: str
    verified_at: float
    expires_at: float
    assurance_level: str = SIMULATION_ASSURANCE_LEVEL
    simulation_only: bool = True
    physical_authorized: bool = False
    _binding_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Reject client-shaped or physical authorization claims."""
        object.__setattr__(self, 'user_id', validate_user_id(self.user_id))
        object.__setattr__(
            self,
            'principal_binding_digest',
            _digest(
                self.principal_binding_digest,
                'principal_binding_digest',
            ),
        )
        for name in ('confirmation_request_id', 'confirmation_result_id'):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'proposal_fingerprint',
            _digest(self.proposal_fingerprint, 'proposal_fingerprint'),
        )
        verified_at = _timestamp(self.verified_at, 'verified_at')
        expires_at = _timestamp(self.expires_at, 'expires_at')
        if expires_at <= verified_at:
            raise ValidationError('simulation approval is not current')
        object.__setattr__(self, 'verified_at', verified_at)
        object.__setattr__(self, 'expires_at', expires_at)
        if self.assurance_level != SIMULATION_ASSURANCE_LEVEL:
            raise ValidationError('simulation assurance is unsupported')
        if (
            self.simulation_only is not True
            or self.physical_authorized is not False
        ):
            raise ValidationError(
                'simulation approval cannot authorize physical execution'
            )
        object.__setattr__(
            self,
            '_binding_digest',
            _hash_json(
                {
                    'user_id': self.user_id,
                    'principal_binding_digest': (
                        self.principal_binding_digest
                    ),
                    'confirmation_request_id': (
                        self.confirmation_request_id
                    ),
                    'confirmation_result_id': self.confirmation_result_id,
                    'proposal_fingerprint': self.proposal_fingerprint,
                    'verified_at': self.verified_at,
                    'expires_at': self.expires_at,
                    'assurance_level': self.assurance_level,
                    'simulation_only': True,
                    'physical_authorized': False,
                }
            ),
        )

    @property
    def binding_digest(self) -> str:
        """Return the request-specific server approval binding."""
        return self._binding_digest


@dataclass(frozen=True)
class SimulationConsumeRequest:
    """Confirmation-scoped selector and signed fresh-target claim."""

    consume_request_id: str
    confirmation_request_id: str
    confirmation_result_id: str
    proposal_fingerprint: str
    current_target: TargetBinding = field(repr=False)
    target_observed_at: float
    target_evidence_expires_at: float
    trust_proof: str = field(default='', repr=False)
    profile_revision: str = SIMULATION_PROFILE_REVISION
    profile_digest: str = DEFAULT_COVERAGE_PROFILE.digest
    _consume_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind idempotency to the complete current target and effects."""
        for name in (
            'consume_request_id',
            'confirmation_request_id',
            'confirmation_result_id',
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'proposal_fingerprint',
            _digest(self.proposal_fingerprint, 'proposal_fingerprint'),
        )
        if not isinstance(self.current_target, TargetBinding):
            raise ValidationError(
                'simulation consume requires a trusted current target'
            )
        observed_at = _timestamp(
            self.target_observed_at,
            'target_observed_at',
        )
        evidence_expires_at = _timestamp(
            self.target_evidence_expires_at,
            'target_evidence_expires_at',
        )
        if evidence_expires_at <= observed_at:
            raise ValidationError('simulation target evidence is not current')
        object.__setattr__(self, 'target_observed_at', observed_at)
        object.__setattr__(
            self,
            'target_evidence_expires_at',
            evidence_expires_at,
        )
        if (
            not isinstance(self.trust_proof, str)
            or (
                self.trust_proof != ''
                and not re.fullmatch(r'[0-9a-f]{64}', self.trust_proof)
            )
        ):
            raise ValidationError('simulation trust proof is invalid')
        if self.profile_revision != SIMULATION_PROFILE_REVISION:
            raise ValidationError('simulation profile is unsupported')
        if self.profile_digest != DEFAULT_COVERAGE_PROFILE.digest:
            raise ValidationError('simulation profile is unsupported')
        fingerprint = _hash_json(
            {
                'schema_version': SIMULATION_LEDGER_SCHEMA_VERSION,
                'consume_request_id': self.consume_request_id,
                'confirmation_request_id': self.confirmation_request_id,
                'confirmation_result_id': self.confirmation_result_id,
                'proposal_fingerprint': self.proposal_fingerprint,
                'target_binding_digest': (
                    self.current_target.binding_digest
                ),
                'effects_digest': self.current_target.effects_digest,
                'target_observed_at': observed_at,
                'target_evidence_expires_at': evidence_expires_at,
                'profile_revision': self.profile_revision,
                'profile_digest': self.profile_digest,
            }
        )
        object.__setattr__(self, '_consume_fingerprint', fingerprint)

    @property
    def consume_fingerprint(self) -> str:
        """Return the exact idempotency payload digest."""
        return self._consume_fingerprint


class _SimulationTestTrustHarness:
    """Explicit test-only HMAC issuer/verifier for the pure simulator."""

    def __init__(
        self,
        secret: bytes,
        *,
        max_target_age_seconds: float = 5.0,
    ) -> None:
        """Create a stable test trust root from at least 32 random bytes."""
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError(
                'test simulation secret must be at least 32 bytes'
            )
        self._secret = secret
        self._max_target_age_seconds = _timestamp(
            max_target_age_seconds,
            'max_target_age_seconds',
        )
        if self._max_target_age_seconds <= 0:
            raise ValueError('max_target_age_seconds must be positive')

    @staticmethod
    def _proof_payload(
        approval: VerifiedSimulationApproval,
        request: SimulationConsumeRequest,
    ) -> bytes:
        return json.dumps(
            {
                'contract': 'malbut-test-simulation-trust-v1',
                'approval_binding_digest': approval.binding_digest,
                'consume_fingerprint': request.consume_fingerprint,
                'target_binding_digest': request.current_target.binding_digest,
                'effects_digest': request.current_target.effects_digest,
                'target_observed_at': request.target_observed_at,
                'target_evidence_expires_at': (
                    request.target_evidence_expires_at
                ),
            },
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')

    def _sign(
        self,
        approval: VerifiedSimulationApproval,
        request: SimulationConsumeRequest,
    ) -> str:
        return hmac.new(
            self._secret,
            self._proof_payload(approval, request),
            hashlib.sha256,
        ).hexdigest()

    def issue(
        self,
        *,
        user_id: str,
        principal_binding_digest: str,
        confirmation_request_id: str,
        confirmation_result_id: str,
        proposal_fingerprint: str,
        verified_at: float,
        approval_expires_at: float,
        consume_request_id: str,
        current_target: TargetBinding,
        target_observed_at: float,
    ) -> tuple[VerifiedSimulationApproval, SimulationConsumeRequest]:
        """Issue test evidence; never wire this method to an API route."""
        observed_at = _timestamp(target_observed_at, 'target_observed_at')
        approval = VerifiedSimulationApproval(
            user_id=user_id,
            principal_binding_digest=principal_binding_digest,
            confirmation_request_id=confirmation_request_id,
            confirmation_result_id=confirmation_result_id,
            proposal_fingerprint=proposal_fingerprint,
            verified_at=verified_at,
            expires_at=approval_expires_at,
        )
        request = SimulationConsumeRequest(
            consume_request_id=consume_request_id,
            confirmation_request_id=confirmation_request_id,
            confirmation_result_id=confirmation_result_id,
            proposal_fingerprint=proposal_fingerprint,
            current_target=current_target,
            target_observed_at=observed_at,
            target_evidence_expires_at=(
                observed_at + self._max_target_age_seconds
            ),
        )
        return approval, replace(
            request,
            trust_proof=self._sign(approval, request),
        )

    def verify(
        self,
        approval: VerifiedSimulationApproval,
        request: SimulationConsumeRequest,
        now: float,
    ) -> TargetBinding:
        """Verify the test actor binding and fresh target atomically."""
        self.verify_receipt(approval, request, now)
        normalized_now = _timestamp(now, 'server time')
        if (
            normalized_now < request.target_observed_at
            or normalized_now >= request.target_evidence_expires_at
            or (
                normalized_now - request.target_observed_at
                > self._max_target_age_seconds
            )
        ):
            raise SimulationAssuranceError(
                'trusted simulation target evidence is not current'
            )
        return request.current_target

    def verify_receipt(
        self,
        approval: VerifiedSimulationApproval,
        request: SimulationConsumeRequest,
        now: float,
    ) -> None:
        """Verify exact test evidence without renewing target freshness."""
        normalized_now = _timestamp(now, 'server time')
        if (
            normalized_now < approval.verified_at
            or normalized_now >= approval.expires_at
        ):
            raise SimulationAssuranceError(
                'verified simulation approval is not current'
            )
        expected = self._sign(approval, replace(request, trust_proof=''))
        if not hmac.compare_digest(request.trust_proof, expected):
            raise SimulationAssuranceError(
                'trusted simulation evidence is invalid'
            )


@dataclass(frozen=True)
class DurableSimulationExecution:
    """One immutable terminal simulation or invalidation receipt."""

    record_kind: str
    confirmation_request_id: str = field(repr=False)
    confirmation_result_id: str = field(repr=False)
    decision_id: str = field(repr=False)
    consume_request_id: str = field(repr=False)
    consume_fingerprint: str = field(repr=False)
    actor_binding_digest: str = field(repr=False)
    proposal_fingerprint: str = field(repr=False)
    target_binding_digest: str = field(repr=False)
    effects_digest: str = field(repr=False)
    planner_revision: Optional[str]
    profile_digest: Optional[str]
    plan_digest: Optional[str]
    result_digest: Optional[str]
    sample_count: Optional[int]
    component_count: Optional[int]
    receipt_digest: Optional[str] = field(repr=False)
    state: str
    result_code: str
    completed_at: float
    tool_call_id: Optional[str]
    mission_id: Optional[str]
    operation_id: Optional[str]
    simulation_authority_issued: bool
    replayed: bool = False
    schema_version: int = SIMULATION_LEDGER_SCHEMA_VERSION

    def to_public_dict(self) -> Dict[str, Any]:
        """Return a content-free result with physical effects denied."""
        return {
            'schema_version': self.schema_version,
            'state': self.state,
            'code': self.result_code,
            'record_kind': self.record_kind,
            'tool_call_id': self.tool_call_id,
            'mission_id': self.mission_id,
            'operation_id': self.operation_id,
            'completed_at': self.completed_at,
            'replayed': self.replayed,
            'authority': {
                'kind': (
                    'simulation_only'
                    if self.simulation_authority_issued
                    else 'none'
                ),
                'issued': self.simulation_authority_issued,
                'consume_once': True,
                'reusable': False,
                'physical_authorized': False,
            },
            'simulation': True,
            'physical_effects': False,
            'viewer_live': False,
            'nav2_validated': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
            'execution_authorized': False,
            'coverage_plan': {
                'planner_revision': self.planner_revision,
                'profile_digest': self.profile_digest,
                'plan_digest': self.plan_digest,
                'result_digest': self.result_digest,
                'sample_count': self.sample_count,
                'component_count': self.component_count,
            },
        }


def _expected_schema_sql(*, legacy_v3: bool) -> Dict[str, tuple[str, str]]:
    """Return the complete exact private-schema object contract."""
    return {
        'monitor_room_simulation_schema_metadata': (
            'table',
            _V3_SIMULATION_SCHEMA_METADATA_TABLE_SQL
            if legacy_v3 else SIMULATION_SCHEMA_METADATA_TABLE_SQL,
        ),
        'monitor_room_simulation_preactivation_proposals': (
            'table', SIMULATION_PREACTIVATION_PROPOSALS_TABLE_SQL
        ),
        'monitor_room_simulation_write_fence': (
            'table', SIMULATION_WRITE_FENCE_TABLE_SQL
        ),
        'monitor_room_simulation_eligibility': (
            'table',
            _V3_SIMULATION_ELIGIBILITY_TABLE_SQL
            if legacy_v3 else SIMULATION_ELIGIBILITY_TABLE_SQL,
        ),
        'monitor_room_simulation_ledger': (
            'table',
            _V3_SIMULATION_LEDGER_TABLE_SQL
            if legacy_v3 else SIMULATION_LEDGER_TABLE_SQL,
        ),
        'monitor_room_simulation_approval_consume_idx': (
            'index', SIMULATION_APPROVAL_CONSUME_INDEX_SQL
        ),
        'monitor_room_simulation_no_update': (
            'trigger', SIMULATION_NO_UPDATE_TRIGGER_SQL
        ),
        'monitor_room_simulation_no_delete': (
            'trigger', SIMULATION_NO_DELETE_TRIGGER_SQL
        ),
        'monitor_room_simulation_no_replace': (
            'trigger', SIMULATION_NO_REPLACE_TRIGGER_SQL
        ),
        'monitor_room_simulation_eligibility_guard': (
            'trigger',
            _V3_SIMULATION_ELIGIBILITY_GUARD_TRIGGER_SQL
            if legacy_v3 else SIMULATION_ELIGIBILITY_GUARD_TRIGGER_SQL,
        ),
        'monitor_room_simulation_eligibility_no_update': (
            'trigger', SIMULATION_ELIGIBILITY_NO_UPDATE_TRIGGER_SQL
        ),
        'monitor_room_simulation_metadata_no_update': (
            'trigger', SIMULATION_METADATA_NO_UPDATE_TRIGGER_SQL
        ),
        'monitor_room_simulation_metadata_no_delete': (
            'trigger', SIMULATION_METADATA_NO_DELETE_TRIGGER_SQL
        ),
        'monitor_room_simulation_metadata_no_replace': (
            'trigger', SIMULATION_METADATA_NO_REPLACE_TRIGGER_SQL
        ),
        'monitor_room_simulation_preactivation_no_update': (
            'trigger', SIMULATION_PREACTIVATION_NO_UPDATE_TRIGGER_SQL
        ),
        'monitor_room_simulation_preactivation_no_delete': (
            'trigger', SIMULATION_PREACTIVATION_NO_DELETE_TRIGGER_SQL
        ),
        'monitor_room_simulation_preactivation_no_insert': (
            'trigger', SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL
        ),
    }


def _validate_exact_schema_sql_locked(
    connection: sqlite3.Connection,
    *,
    legacy_v3: bool,
) -> None:
    expected = _expected_schema_sql(legacy_v3=legacy_v3)
    for name, (object_type, sql) in expected.items():
        row = connection.execute(
            'SELECT type, sql FROM sqlite_master WHERE name = ?',
            (name,),
        ).fetchone()
        if (
            row is None
            or row['type'] != object_type
            or str(row['sql']).strip() != sql.strip()
        ):
            raise SimulationExecutionSchemaError(
                'simulation execution schema is incompatible'
            )


def _validate_v3_schema_for_migration_locked(
    connection: sqlite3.Connection,
) -> None:
    """Authenticate every v3 object and invariant before replacement."""
    _validate_exact_schema_sql_locked(connection, legacy_v3=True)
    actual_custom = {
        (row['type'], row['name'], row['tbl_name'])
        for row in connection.execute(
            '''
            SELECT type, name, tbl_name FROM sqlite_master
            WHERE type IN ('index', 'trigger')
              AND tbl_name IN (
                  'monitor_room_simulation_schema_metadata',
                  'monitor_room_simulation_preactivation_proposals',
                  'monitor_room_simulation_write_fence',
                  'monitor_room_simulation_eligibility',
                  'monitor_room_simulation_ledger'
              )
              AND sql IS NOT NULL
            '''
        ).fetchall()
    }
    expected_custom = {
        (object_type, name, (
            'monitor_room_simulation_ledger'
            if name in {
                'monitor_room_simulation_approval_consume_idx',
                'monitor_room_simulation_no_update',
                'monitor_room_simulation_no_delete',
                'monitor_room_simulation_no_replace',
            }
            else 'monitor_room_simulation_eligibility'
            if name in {
                'monitor_room_simulation_eligibility_guard',
                'monitor_room_simulation_eligibility_no_update',
            }
            else 'monitor_room_simulation_schema_metadata'
            if name.startswith('monitor_room_simulation_metadata_')
            else 'monitor_room_simulation_preactivation_proposals'
        ))
        for name, (object_type, _sql) in _expected_schema_sql(
            legacy_v3=True,
        ).items()
        if object_type in ('index', 'trigger')
    }
    if actual_custom != expected_custom:
        raise SimulationExecutionSchemaError(
            'simulation execution schema has unexpected objects'
        )
    metadata = connection.execute(
        '''
        SELECT singleton, schema_version, activated_at, activation_epoch,
               typeof(singleton), typeof(schema_version),
               typeof(activated_at), typeof(activation_epoch)
        FROM monitor_room_simulation_schema_metadata
        '''
    ).fetchall()
    if (
        len(metadata) != 1
        or tuple(metadata[0][0:2]) != (1, 3)
        or metadata[0][4] != 'integer'
        or metadata[0][5] != 'integer'
        or metadata[0][6] not in ('integer', 'real')
        or metadata[0][7] != 'text'
        or not math.isfinite(float(metadata[0][2]))
        or float(metadata[0][2]) < 0
        or not isinstance(metadata[0][3], str)
        or not re.fullmatch(r'[0-9a-f]{64}', metadata[0][3])
    ):
        raise SimulationExecutionSchemaError(
            'simulation execution metadata is incompatible'
        )
    foreign_keys = connection.execute(
        'PRAGMA foreign_key_list(monitor_room_simulation_eligibility)'
    ).fetchall()
    if (
        len(foreign_keys) != 1
        or foreign_keys[0]['table'] != 'confirmation_intents'
        or foreign_keys[0]['from'] != 'confirmation_request_id'
        or foreign_keys[0]['to'] != 'confirmation_request_id'
        or str(foreign_keys[0]['on_delete']).upper() != 'CASCADE'
        or connection.execute(
            'PRAGMA foreign_key_list(monitor_room_simulation_ledger)'
        ).fetchall()
        or connection.execute(
            'PRAGMA foreign_key_list('
            'monitor_room_simulation_preactivation_proposals)'
        ).fetchall()
    ):
        raise SimulationExecutionSchemaError(
            'simulation execution ownership is incompatible'
        )
    invalid = connection.execute(
        '''
        SELECT 1 FROM monitor_room_simulation_ledger
        WHERE typeof(confirmation_issued_at) NOT IN ('integer', 'real')
           OR confirmation_issued_at < 0
           OR confirmation_issued_at > 1.7976931348623157e308
           OR typeof(confirmation_expires_at) NOT IN ('integer', 'real')
           OR confirmation_expires_at <= confirmation_issued_at
           OR confirmation_expires_at > 1.7976931348623157e308
           OR typeof(completed_at) NOT IN ('integer', 'real')
           OR completed_at < confirmation_issued_at
           OR completed_at > 1.7976931348623157e308
           OR (
               state != 'invalidated'
               AND completed_at >= confirmation_expires_at
           )
           OR schema_version != 3
           OR simulation_profile_revision != ?
           OR simulation != 1
           OR physical_authorized != 0
           OR physical_effects != 0
           OR viewer_live != 0
           OR confirmation_spent != 1
           OR simulation_authority_issued NOT IN (0, 1)
           OR (
               state = 'invalidated'
               AND (
                   simulation_authority_issued != 0
                   OR authority_kind != 'none'
                   OR tool_call_id IS NOT NULL
                   OR mission_id IS NOT NULL
                   OR operation_id IS NOT NULL
               )
           )
           OR (
               state IN ('succeeded', 'failed')
               AND (
                   simulation_authority_issued != 1
                   OR authority_kind != 'simulation_only'
                   OR tool_call_id IS NULL
                   OR mission_id IS NULL
                   OR operation_id IS NULL
               )
           )
        LIMIT 1
        ''',
        (_LEGACY_SIMULATION_PROFILE_REVISION,),
    ).fetchone()
    if invalid is not None:
        raise SimulationExecutionSchemaError(
            'simulation execution flags are incompatible'
        )
    for row in connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_ledger
        WHERE state IN ('succeeded', 'failed')
        '''
    ).fetchall():
        if (
            row['tool_call_id'] != _execution_id('simulation-tool', row)
            or row['mission_id']
            != _execution_id('simulation-mission', row)
            or row['operation_id']
            != _execution_id('simulation-operation', row)
        ):
            raise SimulationExecutionSchemaError(
                'simulation v3 identifiers are incompatible'
            )
    invalid_eligibility = connection.execute(
        '''
        SELECT 1
        FROM monitor_room_simulation_eligibility AS eligibility
        LEFT JOIN confirmation_intents AS confirmation
          ON confirmation.rowid = eligibility.confirmation_rowid
         AND confirmation.confirmation_request_id =
             eligibility.confirmation_request_id
        CROSS JOIN monitor_room_simulation_schema_metadata AS metadata
        WHERE confirmation.rowid IS NULL
           OR eligibility.contract_version != 3
           OR eligibility.activation_epoch != metadata.activation_epoch
           OR eligibility.created_at != confirmation.created_at
           OR confirmation.schema_version != 3
           OR eligibility.proposal_fingerprint !=
              confirmation.proposal_fingerprint
           OR eligibility.target_binding_digest !=
              confirmation.target_binding_digest
           OR eligibility.effects_digest != confirmation.effects_digest
           OR EXISTS (
               SELECT 1
               FROM monitor_room_simulation_preactivation_proposals AS denied
               WHERE denied.proposal_fingerprint =
                     eligibility.proposal_fingerprint
           )
        LIMIT 1
        '''
    ).fetchone()
    invalid_preactivation = connection.execute(
        '''
        SELECT 1
        FROM monitor_room_simulation_preactivation_proposals AS denied
        CROSS JOIN monitor_room_simulation_schema_metadata AS metadata
        WHERE denied.activation_epoch != metadata.activation_epoch
           OR denied.snapshot_rowid <= 0
           OR denied.snapshotted_at != metadata.activated_at
        LIMIT 1
        '''
    ).fetchone()
    fence = connection.execute(
        '''
        SELECT singleton, fence, typeof(singleton), typeof(fence)
        FROM monitor_room_simulation_write_fence
        '''
    ).fetchall()
    if (
        invalid_eligibility is not None
        or invalid_preactivation is not None
        or len(fence) != 1
        or tuple(fence[0]) != (1, 0, 'integer', 'integer')
    ):
        raise SimulationExecutionSchemaError(
            'simulation v3 migration source is incompatible'
        )


def _create_v4_schema_locked(
    connection: sqlite3.Connection,
    *,
    activated_at: float,
    activation_epoch: str,
) -> None:
    """Create empty v4 objects, leaving denylist insertion to the caller."""
    connection.execute(SIMULATION_SCHEMA_METADATA_TABLE_SQL)
    connection.execute(SIMULATION_PREACTIVATION_PROPOSALS_TABLE_SQL)
    connection.execute(SIMULATION_WRITE_FENCE_TABLE_SQL)
    connection.execute(SIMULATION_ELIGIBILITY_TABLE_SQL)
    connection.execute(SIMULATION_LEDGER_TABLE_SQL)
    connection.execute(SIMULATION_APPROVAL_CONSUME_INDEX_SQL)
    connection.execute(SIMULATION_NO_UPDATE_TRIGGER_SQL)
    connection.execute(SIMULATION_NO_DELETE_TRIGGER_SQL)
    connection.execute(SIMULATION_NO_REPLACE_TRIGGER_SQL)
    connection.execute(SIMULATION_ELIGIBILITY_GUARD_TRIGGER_SQL)
    connection.execute(SIMULATION_ELIGIBILITY_NO_UPDATE_TRIGGER_SQL)
    connection.execute(SIMULATION_METADATA_NO_UPDATE_TRIGGER_SQL)
    connection.execute(SIMULATION_METADATA_NO_DELETE_TRIGGER_SQL)
    connection.execute(SIMULATION_METADATA_NO_REPLACE_TRIGGER_SQL)
    connection.execute(SIMULATION_PREACTIVATION_NO_UPDATE_TRIGGER_SQL)
    connection.execute(SIMULATION_PREACTIVATION_NO_DELETE_TRIGGER_SQL)
    connection.execute(
        '''
        INSERT INTO monitor_room_simulation_schema_metadata (
            singleton, schema_version, activated_at, activation_epoch
        ) VALUES (1, 4, ?, ?)
        ''',
        (activated_at, activation_epoch),
    )
    connection.execute(
        '''
        INSERT INTO monitor_room_simulation_write_fence (
            singleton, fence
        ) VALUES (1, 0)
        '''
    )


def _migrate_v3_to_v4_locked(
    connection: sqlite3.Connection,
    *,
    activated_at: float,
) -> None:
    """Preserve v3 terminals as unplanned audit receipts and fence inputs."""
    _validate_v3_schema_for_migration_locked(connection)
    temp_names = (
        'monitor_room_simulation_v3_terminal_snapshot',
        'monitor_room_simulation_v4_denied_snapshot',
    )
    placeholders = ', '.join('?' for _name in temp_names)
    if connection.execute(
        'SELECT 1 FROM sqlite_temp_master WHERE name IN ('
        + placeholders
        + ') LIMIT 1',
        temp_names,
    ).fetchone() is not None:
        raise SimulationExecutionSchemaError(
            'simulation migration workspace is unavailable'
        )
    connection.execute(
        '''
        CREATE TEMP TABLE monitor_room_simulation_v3_terminal_snapshot
        AS SELECT rowid AS legacy_terminal_rowid, *
        FROM monitor_room_simulation_ledger
        '''
    )
    connection.execute(
        '''
        CREATE TEMP TABLE monitor_room_simulation_v4_denied_snapshot (
            proposal_fingerprint TEXT NOT NULL PRIMARY KEY,
            snapshot_rowid INTEGER NOT NULL
        )
        '''
    )
    connection.execute(
        '''
        INSERT INTO monitor_room_simulation_v4_denied_snapshot
        SELECT proposal_fingerprint, snapshot_rowid
        FROM monitor_room_simulation_preactivation_proposals
        '''
    )
    connection.execute(
        '''
        INSERT OR IGNORE INTO monitor_room_simulation_v4_denied_snapshot
        SELECT proposal_fingerprint, legacy_terminal_rowid
        FROM monitor_room_simulation_v3_terminal_snapshot
        '''
    )
    connection.execute(
        '''
        INSERT OR IGNORE INTO monitor_room_simulation_v4_denied_snapshot
        SELECT proposal_fingerprint, rowid FROM confirmation_intents
        '''
    )
    for trigger in (
        'monitor_room_simulation_no_update',
        'monitor_room_simulation_no_delete',
        'monitor_room_simulation_no_replace',
        'monitor_room_simulation_eligibility_guard',
        'monitor_room_simulation_eligibility_no_update',
        'monitor_room_simulation_metadata_no_update',
        'monitor_room_simulation_metadata_no_delete',
        'monitor_room_simulation_metadata_no_replace',
        'monitor_room_simulation_preactivation_no_update',
        'monitor_room_simulation_preactivation_no_delete',
        'monitor_room_simulation_preactivation_no_insert',
    ):
        connection.execute(f'DROP TRIGGER {trigger}')
    connection.execute(
        'DROP INDEX monitor_room_simulation_approval_consume_idx'
    )
    for table in (
        'monitor_room_simulation_eligibility',
        'monitor_room_simulation_ledger',
        'monitor_room_simulation_write_fence',
        'monitor_room_simulation_preactivation_proposals',
        'monitor_room_simulation_schema_metadata',
    ):
        connection.execute(f'DROP TABLE {table}')
    activation_epoch = secrets.token_hex(32)
    _create_v4_schema_locked(
        connection,
        activated_at=activated_at,
        activation_epoch=activation_epoch,
    )
    connection.execute(
        '''
        INSERT INTO monitor_room_simulation_preactivation_proposals (
            proposal_fingerprint,
            activation_epoch,
            snapshot_rowid,
            snapshotted_at
        )
        SELECT proposal_fingerprint, ?, snapshot_rowid, ?
        FROM monitor_room_simulation_v4_denied_snapshot
        ''',
        (activation_epoch, activated_at),
    )
    connection.execute(
        '''
        INSERT INTO monitor_room_simulation_ledger (
            schema_version, record_kind,
            confirmation_request_id, confirmation_result_id, decision_id,
            consume_request_id, consume_fingerprint,
            actor_binding_digest, owner_binding_digest,
            proposal_fingerprint, arguments_digest,
            target_binding_digest, effects_digest,
            planner_revision, profile_digest, plan_digest, result_digest,
            sample_count, component_count, receipt_digest,
            confirmation_issued_at, confirmation_expires_at, completed_at,
            tool_call_id, mission_id, operation_id,
            state, result_code, confirmation_spent,
            simulation_authority_issued, simulation,
            physical_authorized, physical_effects, viewer_live,
            nav2_validated, camera_coverage_validated, coverage_achieved,
            authority_kind
        )
        SELECT
            3, 'legacy_unplanned',
            confirmation_request_id, confirmation_result_id, decision_id,
            consume_request_id, consume_fingerprint,
            actor_binding_digest, owner_binding_digest,
            proposal_fingerprint, arguments_digest,
            target_binding_digest, effects_digest,
            NULL, NULL, NULL, NULL, NULL, NULL, NULL,
            confirmation_issued_at, confirmation_expires_at, completed_at,
            tool_call_id, mission_id, operation_id,
            state, result_code, confirmation_spent,
            simulation_authority_issued, simulation,
            physical_authorized, physical_effects, viewer_live,
            0, 0, 0, authority_kind
        FROM monitor_room_simulation_v3_terminal_snapshot
        '''
    )
    connection.execute(SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL)
    connection.execute(
        'DROP TABLE monitor_room_simulation_v3_terminal_snapshot'
    )
    connection.execute(
        'DROP TABLE monitor_room_simulation_v4_denied_snapshot'
    )


def prepare_simulation_schema_locked(
    connection: sqlite3.Connection,
    *,
    activated_at: float,
) -> None:
    """Create, migrate, or strictly validate the execution-owned schema."""
    normalized_activation_time = _timestamp(activated_at, 'activated_at')
    expected = {
        name: object_type
        for name, (object_type, _sql) in _expected_schema_sql(
            legacy_v3=False,
        ).items()
    }
    placeholders = ', '.join('?' for _name in expected)
    rows = connection.execute(
        'SELECT type, name FROM sqlite_master WHERE name IN ('
        + placeholders
        + ')',
        tuple(expected),
    ).fetchall()
    found = {str(row['name']): str(row['type']) for row in rows}
    if not found:
        activation_epoch = secrets.token_hex(32)
        _create_v4_schema_locked(
            connection,
            activated_at=normalized_activation_time,
            activation_epoch=activation_epoch,
        )
        connection.execute(
            '''
            INSERT INTO monitor_room_simulation_preactivation_proposals (
                proposal_fingerprint,
                activation_epoch,
                snapshot_rowid,
                snapshotted_at
            )
            SELECT proposal_fingerprint, ?, rowid, ?
            FROM confirmation_intents
            ''',
            (activation_epoch, normalized_activation_time),
        )
        connection.execute(SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL)
    elif found != expected:
        raise SimulationExecutionSchemaError(
            'simulation execution schema is incomplete'
        )
    else:
        metadata = connection.execute(
            '''
            SELECT schema_version
            FROM monitor_room_simulation_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        if metadata is not None and metadata['schema_version'] == 3:
            _migrate_v3_to_v4_locked(
                connection,
                activated_at=normalized_activation_time,
            )
        elif metadata is None or metadata['schema_version'] != 4:
            raise SimulationExecutionSchemaError(
                'simulation execution metadata is incompatible'
            )
    validate_simulation_schema_locked(connection)


def validate_simulation_schema_locked(
    connection: sqlite3.Connection,
) -> None:
    """Reject changed tables, indexes, triggers, metadata, or flags."""
    _validate_exact_schema_sql_locked(connection, legacy_v3=False)
    metadata = connection.execute(
        '''
        SELECT singleton, schema_version, activated_at,
               activation_epoch,
               typeof(singleton) AS singleton_type,
               typeof(schema_version) AS version_type,
               typeof(activated_at) AS activated_at_type,
               typeof(activation_epoch) AS activation_epoch_type
        FROM monitor_room_simulation_schema_metadata
        '''
    ).fetchall()
    if (
        len(metadata) != 1
        or metadata[0]['singleton'] != 1
        or metadata[0]['schema_version'] != 4
        or metadata[0]['singleton_type'] != 'integer'
        or metadata[0]['version_type'] != 'integer'
        or metadata[0]['activated_at_type'] not in ('integer', 'real')
        or metadata[0]['activation_epoch_type'] != 'text'
        or not math.isfinite(float(metadata[0]['activated_at']))
        or float(metadata[0]['activated_at']) < 0
        or not isinstance(metadata[0]['activation_epoch'], str)
        or not re.fullmatch(
            r'[0-9a-f]{64}',
            metadata[0]['activation_epoch'],
        )
    ):
        raise SimulationExecutionSchemaError(
            'simulation execution metadata is incompatible'
        )
    foreign_keys = connection.execute(
        'PRAGMA foreign_key_list(monitor_room_simulation_eligibility)'
    ).fetchall()
    if len(foreign_keys) != 1:
        raise SimulationExecutionSchemaError(
            'simulation eligibility ownership is incompatible'
        )
    foreign_key = foreign_keys[0]
    if (
        foreign_key['table'] != 'confirmation_intents'
        or foreign_key['from'] != 'confirmation_request_id'
        or foreign_key['to'] != 'confirmation_request_id'
        or str(foreign_key['on_delete']).upper() != 'CASCADE'
    ):
        raise SimulationExecutionSchemaError(
            'simulation eligibility ownership is incompatible'
        )
    if connection.execute(
        'PRAGMA foreign_key_list(monitor_room_simulation_ledger)'
    ).fetchall():
        raise SimulationExecutionSchemaError(
            'simulation terminal ledger must be independent'
        )
    custom_objects = connection.execute(
        '''
        SELECT type, name, tbl_name FROM sqlite_master
        WHERE type IN ('index', 'trigger')
          AND tbl_name IN (
              'monitor_room_simulation_schema_metadata',
              'monitor_room_simulation_preactivation_proposals',
              'monitor_room_simulation_write_fence',
              'monitor_room_simulation_eligibility',
              'monitor_room_simulation_ledger'
          )
          AND sql IS NOT NULL
        '''
    ).fetchall()
    actual_custom = {
        (row['type'], row['name'], row['tbl_name'])
        for row in custom_objects
    }
    expected_custom = {
        (
            'index',
            'monitor_room_simulation_approval_consume_idx',
            'monitor_room_simulation_ledger',
        ),
        (
            'trigger',
            'monitor_room_simulation_no_update',
            'monitor_room_simulation_ledger',
        ),
        (
            'trigger',
            'monitor_room_simulation_no_delete',
            'monitor_room_simulation_ledger',
        ),
        (
            'trigger',
            'monitor_room_simulation_no_replace',
            'monitor_room_simulation_ledger',
        ),
        (
            'trigger',
            'monitor_room_simulation_eligibility_guard',
            'monitor_room_simulation_eligibility',
        ),
        (
            'trigger',
            'monitor_room_simulation_eligibility_no_update',
            'monitor_room_simulation_eligibility',
        ),
        (
            'trigger',
            'monitor_room_simulation_metadata_no_update',
            'monitor_room_simulation_schema_metadata',
        ),
        (
            'trigger',
            'monitor_room_simulation_metadata_no_delete',
            'monitor_room_simulation_schema_metadata',
        ),
        (
            'trigger',
            'monitor_room_simulation_metadata_no_replace',
            'monitor_room_simulation_schema_metadata',
        ),
        (
            'trigger',
            'monitor_room_simulation_preactivation_no_update',
            'monitor_room_simulation_preactivation_proposals',
        ),
        (
            'trigger',
            'monitor_room_simulation_preactivation_no_delete',
            'monitor_room_simulation_preactivation_proposals',
        ),
        (
            'trigger',
            'monitor_room_simulation_preactivation_no_insert',
            'monitor_room_simulation_preactivation_proposals',
        ),
    }
    if actual_custom != expected_custom:
        raise SimulationExecutionSchemaError(
            'simulation execution schema has unexpected objects'
        )
    if connection.execute(
        'PRAGMA foreign_key_list('
        'monitor_room_simulation_preactivation_proposals)'
    ).fetchall():
        raise SimulationExecutionSchemaError(
            'simulation preactivation proposals must be independent'
        )
    invalid_preactivation = connection.execute(
        '''
        SELECT 1
        FROM monitor_room_simulation_preactivation_proposals AS denied
        CROSS JOIN monitor_room_simulation_schema_metadata AS metadata
        WHERE denied.activation_epoch != metadata.activation_epoch
           OR denied.snapshot_rowid <= 0
           OR denied.snapshotted_at != metadata.activated_at
        LIMIT 1
        '''
    ).fetchone()
    if invalid_preactivation is not None:
        raise SimulationExecutionSchemaError(
            'simulation preactivation proposals are incompatible'
        )
    fence_rows = connection.execute(
        '''
        SELECT singleton, fence,
               typeof(singleton) AS singleton_type,
               typeof(fence) AS fence_type
        FROM monitor_room_simulation_write_fence
        '''
    ).fetchall()
    if (
        len(fence_rows) != 1
        or fence_rows[0]['singleton'] != 1
        or fence_rows[0]['fence'] != 0
        or fence_rows[0]['singleton_type'] != 'integer'
        or fence_rows[0]['fence_type'] != 'integer'
    ):
        raise SimulationExecutionSchemaError(
            'simulation write fence is incompatible'
        )
    invalid = connection.execute(
        '''
        SELECT 1 FROM monitor_room_simulation_ledger
        WHERE typeof(confirmation_issued_at) NOT IN ('integer', 'real')
           OR confirmation_issued_at < 0
           OR confirmation_issued_at > 1.7976931348623157e308
           OR typeof(confirmation_expires_at) NOT IN ('integer', 'real')
           OR confirmation_expires_at <= confirmation_issued_at
           OR confirmation_expires_at > 1.7976931348623157e308
           OR typeof(completed_at) NOT IN ('integer', 'real')
           OR completed_at < confirmation_issued_at
           OR completed_at > 1.7976931348623157e308
           OR (
               state != 'invalidated'
               AND completed_at >= confirmation_expires_at
           )
           OR simulation != 1
           OR physical_authorized != 0
           OR physical_effects != 0
           OR viewer_live != 0
           OR nav2_validated != 0
           OR camera_coverage_validated != 0
           OR coverage_achieved != 0
           OR confirmation_spent != 1
           OR simulation_authority_issued NOT IN (0, 1)
           OR (
               state = 'invalidated'
               AND (
                   simulation_authority_issued != 0
                   OR authority_kind != 'none'
                   OR tool_call_id IS NOT NULL
                   OR mission_id IS NOT NULL
                   OR operation_id IS NOT NULL
               )
           )
           OR (
               state IN ('succeeded', 'failed')
               AND (
                   simulation_authority_issued != 1
                   OR authority_kind != 'simulation_only'
                   OR tool_call_id IS NULL
                   OR mission_id IS NULL
                   OR operation_id IS NULL
               )
           )
           OR (
               record_kind = 'legacy_unplanned'
               AND (
                   schema_version != 3
                   OR planner_revision IS NOT NULL
                   OR profile_digest IS NOT NULL
                   OR plan_digest IS NOT NULL
                   OR result_digest IS NOT NULL
                   OR sample_count IS NOT NULL
                   OR component_count IS NOT NULL
                   OR receipt_digest IS NOT NULL
               )
           )
           OR (
               record_kind = 'planned'
               AND (
                   schema_version != 4
                   OR state != 'succeeded'
                   OR result_code != 'semantic_sample_plan_created'
                   OR planner_revision IS NULL
                   OR planner_revision != ?
                   OR profile_digest IS NULL
                   OR profile_digest != ?
                   OR plan_digest IS NULL
                   OR result_digest IS NULL
                   OR typeof(sample_count) != 'integer'
                   OR sample_count NOT BETWEEN 1 AND 4096
                   OR typeof(component_count) != 'integer'
                   OR component_count NOT BETWEEN 1 AND 128
                   OR receipt_digest IS NULL
               )
           )
           OR (
               record_kind = 'planning_failed'
               AND (
                   schema_version != 4
                   OR state != 'failed'
                   OR result_code NOT IN (
                       'semantic_sample_planning_failed',
                       'semantic_sample_result_invalid'
                   )
                   OR planner_revision IS NULL
                   OR planner_revision != ?
                   OR profile_digest IS NULL
                   OR profile_digest != ?
                   OR plan_digest IS NOT NULL
                   OR result_digest IS NULL
                   OR typeof(sample_count) != 'integer'
                   OR sample_count != 0
                   OR typeof(component_count) != 'integer'
                   OR component_count != 0
                   OR receipt_digest IS NULL
               )
           )
           OR (
               record_kind = 'invalidated'
               AND (
                   schema_version != 4
                   OR state != 'invalidated'
                   OR planner_revision IS NOT NULL
                   OR profile_digest IS NOT NULL
                   OR plan_digest IS NOT NULL
                   OR result_digest IS NOT NULL
                   OR sample_count IS NOT NULL
                   OR component_count IS NOT NULL
                   OR receipt_digest IS NULL
               )
           )
           OR record_kind NOT IN (
               'legacy_unplanned', 'planned',
               'planning_failed', 'invalidated'
           )
        LIMIT 1
        ''',
        (
            PLANNER_REVISION,
            DEFAULT_COVERAGE_PROFILE.digest,
            PLANNER_REVISION,
            DEFAULT_COVERAGE_PROFILE.digest,
        ),
    ).fetchone()
    if invalid is not None:
        raise SimulationExecutionSchemaError(
            'simulation execution flags are incompatible'
        )
    digest_rows = connection.execute(
        '''
        SELECT record_kind, result_code, profile_digest, plan_digest,
               result_digest, sample_count, component_count
        FROM monitor_room_simulation_ledger
        WHERE record_kind IN ('planned', 'planning_failed')
        '''
    ).fetchall()
    for row in digest_rows:
        try:
            expected_result_digest = (
                _planned_result_digest(
                    row['plan_digest'],
                    row['profile_digest'],
                    row['sample_count'],
                    row['component_count'],
                )
                if row['record_kind'] == 'planned'
                else _planning_failure_result_digest(row['result_code'])
            )
        except (TypeError, ValidationError):
            raise SimulationExecutionSchemaError(
                'simulation result digest is incompatible'
            ) from None
        if row['result_digest'] != expected_result_digest:
            raise SimulationExecutionSchemaError(
                'simulation result digest is incompatible'
            )
    terminal_rows = connection.execute(
        'SELECT * FROM monitor_room_simulation_ledger'
    ).fetchall()
    for row in terminal_rows:
        if row['state'] in ('succeeded', 'failed') and (
            row['tool_call_id'] != _execution_id('simulation-tool', row)
            or row['mission_id']
            != _execution_id('simulation-mission', row)
            or row['operation_id']
            != _execution_id('simulation-operation', row)
        ):
            raise SimulationExecutionSchemaError(
                'simulation execution identifiers are incompatible'
            )
        if row['schema_version'] == 4:
            try:
                expected_receipt_digest = _terminal_receipt_digest(row)
            except (OverflowError, TypeError, ValueError):
                raise SimulationExecutionSchemaError(
                    'simulation terminal receipt is incompatible'
                ) from None
            if row['receipt_digest'] != expected_receipt_digest:
                raise SimulationExecutionSchemaError(
                    'simulation terminal receipt is incompatible'
                )
    invalid_eligibility = connection.execute(
        '''
        SELECT 1
        FROM monitor_room_simulation_eligibility AS eligibility
        LEFT JOIN confirmation_intents AS confirmation
          ON confirmation.rowid = eligibility.confirmation_rowid
         AND confirmation.confirmation_request_id =
             eligibility.confirmation_request_id
        CROSS JOIN monitor_room_simulation_schema_metadata AS metadata
        WHERE confirmation.rowid IS NULL
           OR eligibility.contract_version != 4
           OR eligibility.activation_epoch != metadata.activation_epoch
           OR eligibility.created_at != confirmation.created_at
           OR confirmation.schema_version != 3
           OR eligibility.proposal_fingerprint !=
              confirmation.proposal_fingerprint
           OR eligibility.target_binding_digest !=
              confirmation.target_binding_digest
           OR eligibility.effects_digest != confirmation.effects_digest
           OR EXISTS (
               SELECT 1
               FROM monitor_room_simulation_preactivation_proposals AS denied
               WHERE denied.proposal_fingerprint =
                     eligibility.proposal_fingerprint
           )
        LIMIT 1
        '''
    ).fetchone()
    if invalid_eligibility is not None:
        raise SimulationExecutionSchemaError(
            'simulation eligibility is incompatible'
        )


def _mark_confirmation_simulation_eligible_locked(
    connection: sqlite3.Connection,
    *,
    confirmation_request_id: str,
) -> None:
    """Bind one post-activation confirmation to the current v4 epoch."""
    normalized_request = _identifier(
        confirmation_request_id,
        'confirmation_request_id',
    )
    metadata = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    confirmation = connection.execute(
        '''
        SELECT rowid AS confirmation_rowid, *
        FROM confirmation_intents
        WHERE confirmation_request_id = ?
        ''',
        (normalized_request,),
    ).fetchone()
    if metadata is None or confirmation is None:
        raise SimulationConsumeConflictError(
            'simulation eligibility conflict'
        )
    try:
        confirmation_rowid = int(confirmation['confirmation_rowid'])
        created_at = _timestamp(confirmation['created_at'], 'created_at')
        proposal = _digest(
            confirmation['proposal_fingerprint'],
            'proposal_fingerprint',
        )
        target_digest = _digest(
            confirmation['target_binding_digest'],
            'target_binding_digest',
        )
        effects_digest = _digest(
            confirmation['effects_digest'],
            'effects_digest',
        )
        activation_epoch = _digest(
            metadata['activation_epoch'],
            'activation_epoch',
        )
    except (TypeError, ValueError, ValidationError):
        raise SimulationConsumeConflictError(
            'simulation eligibility conflict'
        ) from None
    if (
        int(confirmation['schema_version']) != 3
        or confirmation['state'] != 'pending'
        or connection.execute(
            '''
            SELECT 1
            FROM monitor_room_simulation_preactivation_proposals
            WHERE proposal_fingerprint = ?
            ''',
            (proposal,),
        ).fetchone() is not None
    ):
        raise SimulationConsumeConflictError(
            'simulation eligibility conflict'
        )
    try:
        connection.execute(
            '''
            INSERT INTO monitor_room_simulation_eligibility (
                confirmation_request_id,
                contract_version,
                activation_epoch,
                confirmation_rowid,
                proposal_fingerprint,
                target_binding_digest,
                effects_digest,
                created_at
            ) VALUES (?, 4, ?, ?, ?, ?, ?, ?)
            ''',
            (
                normalized_request,
                activation_epoch,
                confirmation_rowid,
                proposal,
                target_digest,
                effects_digest,
                created_at,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise SimulationConsumeConflictError(
            'simulation eligibility conflict'
        ) from error


def _target_from_confirmation_row(row: sqlite3.Row) -> TargetBinding:
    def stored_bool(name: str) -> bool:
        value = row[name]
        if type(value) is not int or value not in (0, 1):
            raise SimulationExecutionSchemaError(
                'stored simulation target is invalid'
            )
        return value == 1

    try:
        effects = Effects(
            schema_version=int(row['effects_schema_version']),
            physical_navigation=stored_bool(
                'effect_physical_navigation'
            ),
            camera_capture=stored_bool('effect_camera_capture'),
            external_video_stream=stored_bool(
                'effect_external_video_stream'
            ),
            video_recording=stored_bool('effect_video_recording'),
            audio_capture=stored_bool('effect_audio_capture'),
            max_duration_seconds=int(
                row['effect_max_duration_seconds']
            ),
            coverage_mode=str(row['effect_coverage_mode']),
            viewer_scope=str(row['effect_viewer_scope']),
            talkback_allowed=stored_bool('effect_talkback_allowed'),
        )
        target = TargetBinding(
            schema_version=int(row['target_binding_schema_version']),
            device_id=str(row['target_device_id']),
            device_binding_revision=str(
                row['target_device_binding_revision']
            ),
            source_revision=str(row['target_source_revision']),
            map_id=str(row['target_map_id']),
            map_revision=str(row['target_map_revision']),
            semantic_revision=str(row['target_semantic_revision']),
            frame_id=str(row['target_frame_id']),
            room_id=str(row['target_room_id']),
            room_name=str(row['target_room_name']),
            room_category=str(row['target_room_category']),
            source_arguments_digest=str(
                row['target_source_arguments_digest']
            ),
            geometry_json=str(row['target_geometry_json']),
            geometry_digest=str(row['target_geometry_digest']),
            representative_point=(
                float(row['target_representative_x']),
                float(row['target_representative_y']),
            ),
            clearance_m=float(row['target_clearance_m']),
            area_m2=float(row['target_area_m2']),
            effects=effects,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        ValidationError,
        SimulationExecutionSchemaError,
    ):
        raise SimulationExecutionSchemaError(
            'stored simulation target is invalid'
        ) from None
    if (
        target.binding_digest != row['target_binding_digest']
        or target.effects_digest != row['effects_digest']
        or target.source_arguments_digest != row['arguments_digest']
    ):
        raise SimulationExecutionSchemaError(
            'stored simulation target does not match its digests'
        )
    return target


def _owner_binding_digest(row: sqlite3.Row) -> str:
    return _hash_json(
        {
            'user_id': row['user_id'],
            'conversation_id': row['conversation_id'],
            'session_instance_id': row['session_instance_id'],
            'generation': int(row['generation']),
            'revision': int(row['revision']),
            'ordinal': int(row['ordinal']),
        }
    )


def _execution_id(prefix: str, row: sqlite3.Row) -> str:
    digest = hashlib.sha256(
        (
            f'{prefix}\0{row["confirmation_request_id"]}\0'
            f'{row["proposal_fingerprint"]}'
        ).encode('utf-8')
    ).hexdigest()[:40]
    return f'{prefix}-{digest}'


def _record_from_row(
    row: sqlite3.Row,
    *,
    replayed: bool = False,
) -> DurableSimulationExecution:
    if row is None:
        raise SimulationExecutionSchemaError(
            'stored simulation execution is missing'
        )
    try:
        issued_at = _timestamp(
            row['confirmation_issued_at'],
            'confirmation_issued_at',
        )
        expires_at = _timestamp(
            row['confirmation_expires_at'],
            'confirmation_expires_at',
        )
        completed_at = _timestamp(row['completed_at'], 'completed_at')
    except ValidationError:
        raise SimulationExecutionSchemaError(
            'stored simulation execution time is invalid'
        ) from None
    if (
        expires_at <= issued_at
        or completed_at < issued_at
        or (
            row['state'] != 'invalidated'
            and completed_at >= expires_at
        )
    ):
        raise SimulationExecutionSchemaError(
            'stored simulation execution time is invalid'
        )
    if row['state'] in ('succeeded', 'failed') and (
        row['tool_call_id'] != _execution_id('simulation-tool', row)
        or row['mission_id'] != _execution_id('simulation-mission', row)
        or row['operation_id']
        != _execution_id('simulation-operation', row)
    ):
        raise SimulationExecutionSchemaError(
            'stored simulation execution identifiers are invalid'
        )
    if row['schema_version'] == 4:
        try:
            expected_receipt_digest = _terminal_receipt_digest(row)
        except (OverflowError, TypeError, ValueError):
            raise SimulationExecutionSchemaError(
                'stored simulation terminal receipt is invalid'
            ) from None
        if row['receipt_digest'] != expected_receipt_digest:
            raise SimulationExecutionSchemaError(
                'stored simulation terminal receipt is invalid'
            )
    if row['record_kind'] in ('planned', 'planning_failed'):
        if (
            type(row['sample_count']) is not int
            or type(row['component_count']) is not int
        ):
            raise SimulationExecutionSchemaError(
                'stored simulation coverage counts are invalid'
            )
        try:
            expected_result_digest = (
                _planned_result_digest(
                    row['plan_digest'],
                    row['profile_digest'],
                    row['sample_count'],
                    row['component_count'],
                )
                if row['record_kind'] == 'planned'
                else _planning_failure_result_digest(row['result_code'])
            )
        except (TypeError, ValidationError):
            raise SimulationExecutionSchemaError(
                'stored simulation result digest is invalid'
            ) from None
        if row['result_digest'] != expected_result_digest:
            raise SimulationExecutionSchemaError(
                'stored simulation result digest is invalid'
            )
    return DurableSimulationExecution(
        schema_version=int(row['schema_version']),
        record_kind=str(row['record_kind']),
        confirmation_request_id=str(row['confirmation_request_id']),
        confirmation_result_id=str(row['confirmation_result_id']),
        decision_id=str(row['decision_id']),
        consume_request_id=str(row['consume_request_id']),
        consume_fingerprint=str(row['consume_fingerprint']),
        actor_binding_digest=str(row['actor_binding_digest']),
        proposal_fingerprint=str(row['proposal_fingerprint']),
        target_binding_digest=str(row['target_binding_digest']),
        effects_digest=str(row['effects_digest']),
        planner_revision=row['planner_revision'],
        profile_digest=row['profile_digest'],
        plan_digest=row['plan_digest'],
        result_digest=row['result_digest'],
        sample_count=(
            None
            if row['sample_count'] is None
            else int(row['sample_count'])
        ),
        component_count=(
            None
            if row['component_count'] is None
            else int(row['component_count'])
        ),
        receipt_digest=row['receipt_digest'],
        state=str(row['state']),
        result_code=str(row['result_code']),
        completed_at=completed_at,
        tool_call_id=row['tool_call_id'],
        mission_id=row['mission_id'],
        operation_id=row['operation_id'],
        simulation_authority_issued=(
            row['simulation_authority_issued'] == 1
        ),
        replayed=replayed,
    )


def _existing_execution_locked(
    connection: sqlite3.Connection,
    approval: VerifiedSimulationApproval,
    request: SimulationConsumeRequest,
) -> Optional[DurableSimulationExecution]:
    """Return only an exact receipt; reissued actor proofs are new scope."""
    row = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_ledger
        WHERE confirmation_request_id = ?
        ''',
        (request.confirmation_request_id,),
    ).fetchone()
    if row is not None:
        if (
            row['actor_binding_digest']
            != approval.binding_digest
            or row['confirmation_result_id']
            != request.confirmation_result_id
            or row['proposal_fingerprint']
            != request.proposal_fingerprint
        ):
            raise SimulationExecutionNotFoundError(
                'simulation confirmation was not found'
            )
        if (
            row['schema_version'] == 3
            and row['record_kind'] == 'legacy_unplanned'
        ):
            raise SimulationExecutionContractUpgradeRequiredError(
                'legacy simulation receipt is audit-only'
            )
        if row['consume_request_id'] == request.consume_request_id:
            if row['consume_fingerprint'] != request.consume_fingerprint:
                raise SimulationConsumeConflictError(
                    'simulation consume request conflict'
                )
            if (
                row['record_kind'] in ('planned', 'planning_failed')
                and (
                    row['arguments_digest']
                    != request.current_target.source_arguments_digest
                    or row['target_binding_digest']
                    != request.current_target.binding_digest
                    or row['effects_digest']
                    != request.current_target.effects_digest
                    or (
                        row['planner_revision']
                        != request.profile_revision
                        or row['profile_digest']
                        != request.profile_digest
                    )
                )
            ):
                raise SimulationExecutionSchemaError(
                    'stored simulation execution binding is invalid'
                )
            return _record_from_row(row, replayed=True)
        raise SimulationExecutionAlreadyConsumedError(
            'simulation confirmation is already consumed'
        )
    owner = connection.execute(
        '''
        SELECT confirmation_request_id, consume_fingerprint
        FROM monitor_room_simulation_ledger
        WHERE actor_binding_digest = ? AND consume_request_id = ?
        ''',
        (
            approval.binding_digest,
            request.consume_request_id,
        ),
    ).fetchone()
    if owner is not None:
        raise SimulationConsumeConflictError(
            'simulation consume request conflict'
        )
    return None


def _insert_terminal_locked(
    connection: sqlite3.Connection,
    *,
    approval: VerifiedSimulationApproval,
    request: SimulationConsumeRequest,
    confirmation: sqlite3.Row,
    completed_at: float,
    state: str,
    result_code: str,
    authority_issued: bool,
    record_kind: str,
    planner_revision: Optional[str] = None,
    profile_digest: Optional[str] = None,
    plan_digest: Optional[str] = None,
    result_digest: Optional[str] = None,
    sample_count: Optional[int] = None,
    component_count: Optional[int] = None,
) -> DurableSimulationExecution:
    tool_call_id = None
    mission_id = None
    operation_id = None
    authority_kind = 'none'
    if authority_issued:
        tool_call_id = _execution_id('simulation-tool', confirmation)
        mission_id = _execution_id('simulation-mission', confirmation)
        operation_id = _execution_id('simulation-operation', confirmation)
        authority_kind = 'simulation_only'
    insert_values = {
        'schema_version': 4,
        'record_kind': record_kind,
        'confirmation_request_id': request.confirmation_request_id,
        'confirmation_result_id': request.confirmation_result_id,
        'decision_id': confirmation['decision_id'],
        'consume_request_id': request.consume_request_id,
        'consume_fingerprint': request.consume_fingerprint,
        'actor_binding_digest': approval.binding_digest,
        'owner_binding_digest': _owner_binding_digest(confirmation),
        'proposal_fingerprint': request.proposal_fingerprint,
        'arguments_digest': confirmation['arguments_digest'],
        'target_binding_digest': confirmation['target_binding_digest'],
        'effects_digest': confirmation['effects_digest'],
        'planner_revision': planner_revision,
        'profile_digest': profile_digest,
        'plan_digest': plan_digest,
        'result_digest': result_digest,
        'sample_count': sample_count,
        'component_count': component_count,
        'confirmation_issued_at': float(confirmation['issued_at']),
        'confirmation_expires_at': float(confirmation['expires_at']),
        'completed_at': float(completed_at),
        'tool_call_id': tool_call_id,
        'mission_id': mission_id,
        'operation_id': operation_id,
        'state': state,
        'result_code': result_code,
        'confirmation_spent': 1,
        'simulation_authority_issued': int(authority_issued),
        'simulation': 1,
        'physical_authorized': 0,
        'physical_effects': 0,
        'viewer_live': 0,
        'nav2_validated': 0,
        'camera_coverage_validated': 0,
        'coverage_achieved': 0,
        'authority_kind': authority_kind,
    }
    insert_values['receipt_digest'] = _terminal_receipt_digest(
        insert_values
    )
    try:
        connection.execute(
            '''
            INSERT INTO monitor_room_simulation_ledger (
                schema_version, record_kind,
                confirmation_request_id,
                confirmation_result_id,
                decision_id,
                consume_request_id,
                consume_fingerprint,
                actor_binding_digest,
                owner_binding_digest,
                proposal_fingerprint,
                arguments_digest,
                target_binding_digest,
                effects_digest,
                planner_revision,
                profile_digest,
                plan_digest,
                result_digest,
                sample_count,
                component_count,
                receipt_digest,
                confirmation_issued_at,
                confirmation_expires_at,
                completed_at,
                tool_call_id,
                mission_id,
                operation_id,
                state,
                result_code,
                confirmation_spent,
                simulation_authority_issued,
                simulation,
                physical_authorized,
                physical_effects,
                viewer_live,
                nav2_validated,
                camera_coverage_validated,
                coverage_achieved,
                authority_kind
            ) VALUES (
                4, :record_kind,
                :confirmation_request_id, :confirmation_result_id,
                :decision_id, :consume_request_id, :consume_fingerprint,
                :actor_binding_digest, :owner_binding_digest,
                :proposal_fingerprint, :arguments_digest,
                :target_binding_digest, :effects_digest,
                :planner_revision, :profile_digest, :plan_digest,
                :result_digest, :sample_count, :component_count,
                :receipt_digest,
                :confirmation_issued_at, :confirmation_expires_at,
                :completed_at, :tool_call_id, :mission_id, :operation_id,
                :state, :result_code, 1, :simulation_authority_issued,
                1, 0, 0, 0, 0, 0, 0, :authority_kind
            )
            ''',
            insert_values,
        )
    except sqlite3.IntegrityError as error:
        raise SimulationConsumeConflictError(
            'simulation execution conflict'
        ) from error
    row = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_ledger
        WHERE confirmation_request_id = ?
        ''',
        (request.confirmation_request_id,),
    ).fetchone()
    return _record_from_row(row)


def _invalidation_code(
    context_code: Optional[str],
) -> Optional[str]:
    return {
        None: None,
        'confirmation_conversation_changed': (
            'simulation_conversation_changed'
        ),
        'confirmation_conversation_inactive': (
            'simulation_conversation_inactive'
        ),
        'confirmation_conversation_not_found': (
            'simulation_conversation_not_found'
        ),
    }.get(context_code, 'simulation_conversation_changed')


def _planning_failure_result_digest(result_code: str) -> str:
    """Bind a content-free typed planner failure to the fixed profile."""
    return _hash_json(
        {
            'contract': 'monitor-room-coverage-planning-failure-v1',
            'code': result_code,
            'planner_revision': PLANNER_REVISION,
            'profile_digest': DEFAULT_COVERAGE_PROFILE.digest,
            'simulation': True,
            'physical_effects': False,
            'viewer_live': False,
            'nav2_validated': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
            'execution_authorized': False,
        }
    )


def _planned_result_digest(
    plan_digest: str,
    profile_digest: str,
    sample_count: int,
    component_count: int,
) -> str:
    """Recompute the core's coordinate-free success result digest."""
    return _hash_json(
        {
            'schema_version': 1,
            'code': 'semantic_sample_plan_created',
            'planner_revision': PLANNER_REVISION,
            'profile_digest': _digest(
                profile_digest,
                'profile_digest',
            ),
            'plan_digest': _digest(plan_digest, 'plan_digest'),
            'sample_count': sample_count,
            'component_count': component_count,
            'simulation': True,
            'physical_effects': False,
            'viewer_live': False,
            'nav2_validated': False,
            'camera_coverage_validated': False,
            'coverage_achieved': False,
            'execution_authorized': False,
        }
    )


def _canonical_coverage_plan(plan: CoveragePlan) -> CoveragePlan:
    """Reconstruct the core value so cached digests cannot be forged."""
    if type(plan) is not CoveragePlan or type(plan.samples) is not tuple:
        raise ValueError('invalid coverage plan')
    samples = tuple(
        CoverageSample(
            index=sample.index,
            polygon_ordinal=sample.polygon_ordinal,
            row_ordinal=sample.row_ordinal,
            x_mm=sample.x_mm,
            y_mm=sample.y_mm,
            frame_id=sample.frame_id,
            schema_version=sample.schema_version,
        )
        for sample in plan.samples
        if type(sample) is CoverageSample
    )
    if len(samples) != len(plan.samples):
        raise ValueError('invalid coverage plan')
    canonical = CoveragePlan(
        profile=plan.profile,
        target_binding_digest=plan.target_binding_digest,
        source_arguments_digest=plan.source_arguments_digest,
        geometry_digest=plan.geometry_digest,
        effects_digest=plan.effects_digest,
        samples=samples,
        component_count=plan.component_count,
        candidate_upper_bound=plan.candidate_upper_bound,
        geometry_test_upper_bound=plan.geometry_test_upper_bound,
        frame_id=plan.frame_id,
        schema_version=plan.schema_version,
    )
    if canonical.digest != plan.digest or canonical != plan:
        raise ValueError('invalid coverage plan')
    return canonical


def _coverage_summary(
    result: Any,
    target: TargetBinding,
    request: SimulationConsumeRequest,
) -> Dict[str, Any]:
    """Validate and extract only the coordinate-free core evidence."""
    if type(result) is not CoveragePlanningResult:
        raise ValueError('invalid coverage result')
    plan = result.plan
    if (
        type(plan) is not CoveragePlan
        or plan.profile != DEFAULT_COVERAGE_PROFILE
        or plan.planner_revision != PLANNER_REVISION
        or plan.planner_revision != request.profile_revision
        or plan.profile.digest != request.profile_digest
        or plan.target_binding_digest != target.binding_digest
        or plan.source_arguments_digest != target.source_arguments_digest
        or plan.geometry_digest != target.geometry_digest
        or plan.effects_digest != target.effects_digest
        or result.code != 'semantic_sample_plan_created'
        or result.simulation is not True
        or result.physical_effects is not False
        or result.viewer_live is not False
        or result.nav2_validated is not False
        or result.camera_coverage_validated is not False
        or result.coverage_achieved is not False
        or result.execution_authorized is not False
    ):
        raise ValueError('invalid coverage result')
    plan = _canonical_coverage_plan(plan)
    plan_digest = _digest(plan.digest, 'plan_digest')
    result_digest = _digest(result.result_digest, 'result_digest')
    profile_digest = _digest(plan.profile.digest, 'profile_digest')
    if (
        type(plan.sample_count) is not int
        or not 1 <= plan.sample_count <= 4096
        or type(plan.component_count) is not int
        or not 1 <= plan.component_count <= 128
    ):
        raise ValueError('invalid coverage result')
    if result_digest != _planned_result_digest(
        plan_digest,
        profile_digest,
        plan.sample_count,
        plan.component_count,
    ):
        raise ValueError('invalid coverage result')
    return {
        'planner_revision': plan.planner_revision,
        'profile_digest': profile_digest,
        'plan_digest': plan_digest,
        'result_digest': result_digest,
        'sample_count': plan.sample_count,
        'component_count': plan.component_count,
    }


def _consume_approved_monitor_room_simulation_locked(
    connection: sqlite3.Connection,
    *,
    approval: VerifiedSimulationApproval,
    request: SimulationConsumeRequest,
    verifier: SimulationExecutionTrustVerifier,
    now: float,
    fresh_clock: Callable[[], float],
    context_classifier: Callable[
        [sqlite3.Row, float], Optional[str]
    ],
    fresh_plan_validator: Optional[
        Callable[[CoveragePlan], bool]
    ] = None,
    fresh_planned_hook: Optional[
        Callable[
            [DurableSimulationExecution, CoveragePlan, TargetBinding],
            None,
        ]
    ] = None,
) -> DurableSimulationExecution:
    """Consume and finish one pure simulation in the caller transaction."""
    if not connection.in_transaction:
        raise SimulationAssuranceError(
            'simulation execution requires an owned write transaction'
        )
    if (
        verifier is None
        or not callable(getattr(verifier, 'verify', None))
        or not callable(getattr(verifier, 'verify_receipt', None))
    ):
        raise SimulationAssuranceError(
            'trusted simulation verifier is not configured'
        )
    # Acquire SQLite's write reservation before any lookup or pure simulator
    # call.  This also serializes accidental DEFERRED callers of this private
    # seam; the public store uses BEGIN IMMEDIATE.
    fence = connection.execute(
        '''
        UPDATE monitor_room_simulation_write_fence
        SET fence = fence
        WHERE singleton = 1
        '''
    )
    if fence.rowcount != 1:
        raise SimulationExecutionSchemaError(
            'simulation write fence is unavailable'
        )
    if not isinstance(approval, VerifiedSimulationApproval):
        raise SimulationAssuranceError(
            'verified simulation approval is required'
        )
    if not isinstance(request, SimulationConsumeRequest):
        raise TypeError('request must be a SimulationConsumeRequest')
    if (
        request.profile_revision != PLANNER_REVISION
        or request.profile_digest != DEFAULT_COVERAGE_PROFILE.digest
    ):
        raise SimulationAssuranceError(
            'simulation planner profile is not current'
        )
    normalized_now = _timestamp(now, 'server time')
    verifier.verify_receipt(approval, request, normalized_now)
    if (
        normalized_now < approval.verified_at
        or normalized_now >= approval.expires_at
    ):
        raise SimulationAssuranceError(
            'verified simulation approval is not current'
        )
    if (
        approval.confirmation_request_id
        != request.confirmation_request_id
        or approval.confirmation_result_id
        != request.confirmation_result_id
        or approval.proposal_fingerprint
        != request.proposal_fingerprint
    ):
        raise SimulationExecutionNotFoundError(
            'simulation confirmation was not found'
        )
    existing = _existing_execution_locked(connection, approval, request)
    if existing is not None:
        return existing
    verified_target = verifier.verify(approval, request, normalized_now)
    if not isinstance(verified_target, TargetBinding):
        raise SimulationAssuranceError(
            'trusted simulation verifier returned invalid target evidence'
        )
    if (
        verified_target.binding_digest
        != request.current_target.binding_digest
        or verified_target.effects_digest
        != request.current_target.effects_digest
    ):
        raise SimulationAssuranceError(
            'trusted simulation verifier changed target evidence'
        )
    confirmation = connection.execute(
        '''
        SELECT rowid AS confirmation_rowid, *
        FROM confirmation_intents
        WHERE confirmation_request_id = ?
        ''',
        (request.confirmation_request_id,),
    ).fetchone()
    if (
        confirmation is None
        or confirmation['user_id'] != approval.user_id
        or confirmation['confirmation_result_id']
        != request.confirmation_result_id
        or confirmation['proposal_fingerprint']
        != request.proposal_fingerprint
    ):
        raise SimulationExecutionNotFoundError(
            'simulation confirmation was not found'
        )
    if (
        int(confirmation['schema_version']) != 3
        or confirmation['tool_name'] != 'monitor_room'
        or confirmation['state'] != 'resolved'
        or confirmation['disposition'] != 'approve'
        or confirmation['requested_disposition'] != 'approve'
        or confirmation['result_code']
        != 'confirmation_approval_recorded_no_execution'
        or confirmation['confirmation_result_id'] is None
    ):
        raise SimulationApprovalRequiredError(
            'an approved monitor_room confirmation is required'
        )
    issued_at = _timestamp(confirmation['issued_at'], 'issued_at')
    expires_at = _timestamp(confirmation['expires_at'], 'expires_at')
    resolved_at = _timestamp(confirmation['resolved_at'], 'resolved_at')
    if not issued_at <= resolved_at < expires_at:
        raise SimulationExecutionSchemaError(
            'stored confirmation time is invalid'
        )
    if approval.verified_at < resolved_at:
        raise SimulationAssuranceError(
            'simulation approval predates the durable confirmation'
        )
    invalidation = None
    eligibility = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_eligibility
        WHERE confirmation_request_id = ?
        ''',
        (request.confirmation_request_id,),
    ).fetchone()
    activation = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    if (
        eligibility is None
        or activation is None
        or eligibility['contract_version'] != SIMULATION_CONTRACT_VERSION
        or eligibility['activation_epoch']
        != activation['activation_epoch']
        or eligibility['confirmation_rowid']
        != confirmation['confirmation_rowid']
        or eligibility['created_at'] != confirmation['created_at']
        or eligibility['proposal_fingerprint']
        != confirmation['proposal_fingerprint']
        or eligibility['target_binding_digest']
        != confirmation['target_binding_digest']
        or eligibility['effects_digest'] != confirmation['effects_digest']
        or connection.execute(
            '''
            SELECT 1
            FROM monitor_room_simulation_preactivation_proposals
            WHERE proposal_fingerprint = ?
            ''',
            (request.proposal_fingerprint,),
        ).fetchone() is not None
    ):
        invalidation = 'simulation_binding_upgrade_required'
    context_code = context_classifier(confirmation, normalized_now)
    if invalidation is None:
        invalidation = _invalidation_code(context_code)
    if invalidation is None and normalized_now >= expires_at:
        invalidation = 'simulation_confirmation_expired'
    stored_target = _target_from_confirmation_row(confirmation)
    if invalidation is None and (
        stored_target.effects_digest
        != verified_target.effects_digest
    ):
        invalidation = 'simulation_effects_changed'
    if invalidation is None and (
        stored_target.binding_digest
        != verified_target.binding_digest
    ):
        invalidation = 'simulation_target_changed'
    fresh_now = _timestamp(fresh_clock(), 'server time')
    if fresh_now < normalized_now:
        raise SimulationAssuranceError('server clock moved backwards')
    if invalidation is None:
        fresh_context = context_classifier(confirmation, fresh_now)
        invalidation = _invalidation_code(fresh_context)
    if invalidation is None and fresh_now >= expires_at:
        invalidation = 'simulation_confirmation_expired'
    if invalidation is None:
        fresh_target = verifier.verify(approval, request, fresh_now)
        if (
            not isinstance(fresh_target, TargetBinding)
            or fresh_target.binding_digest != verified_target.binding_digest
            or fresh_target.effects_digest != verified_target.effects_digest
        ):
            raise SimulationAssuranceError(
                'trusted simulation target evidence changed'
            )
    if invalidation is None and fresh_now >= approval.expires_at:
        raise SimulationAssuranceError(
            'verified simulation approval is not current'
        )
    if invalidation is not None:
        if invalidation not in _INVALIDATION_CODES:
            raise SimulationExecutionSchemaError(
                'simulation invalidation code is unsupported'
            )
        return _insert_terminal_locked(
            connection,
            approval=approval,
            request=request,
            confirmation=confirmation,
            completed_at=fresh_now,
            state='invalidated',
            result_code=invalidation,
            authority_issued=False,
            record_kind='invalidated',
        )
    state = 'succeeded'
    result_code = 'semantic_sample_plan_created'
    record_kind = 'planned'
    summary = None
    plan = None
    try:
        result = build_monitor_room_coverage_plan(stored_target)
    except Exception:
        state = 'failed'
        result_code = 'semantic_sample_planning_failed'
        record_kind = 'planning_failed'
    else:
        try:
            summary = _coverage_summary(result, stored_target, request)
            plan = result.plan
        except Exception:
            state = 'failed'
            result_code = 'semantic_sample_result_invalid'
            record_kind = 'planning_failed'
    if summary is not None and fresh_plan_validator is not None:
        accepted = fresh_plan_validator(plan)
        if type(accepted) is not bool:
            raise SimulationAssuranceError(
                'simulation plan validator returned an invalid decision'
            )
        if not accepted:
            state = 'failed'
            result_code = 'semantic_sample_result_invalid'
            record_kind = 'planning_failed'
            summary = None
            plan = None
    if summary is None:
        summary = {
            'planner_revision': PLANNER_REVISION,
            'profile_digest': DEFAULT_COVERAGE_PROFILE.digest,
            'plan_digest': None,
            'result_digest': _planning_failure_result_digest(result_code),
            'sample_count': 0,
            'component_count': 0,
        }
    receipt = _insert_terminal_locked(
        connection,
        approval=approval,
        request=request,
        confirmation=confirmation,
        completed_at=fresh_now,
        state=state,
        result_code=result_code,
        authority_issued=True,
        record_kind=record_kind,
        planner_revision=summary['planner_revision'],
        profile_digest=summary['profile_digest'],
        plan_digest=summary['plan_digest'],
        result_digest=summary['result_digest'],
        sample_count=summary['sample_count'],
        component_count=summary['component_count'],
    )
    if record_kind == 'planned' and fresh_planned_hook is not None:
        if type(plan) is not CoveragePlan:
            raise SimulationAssuranceError(
                'simulation plan hook requires a canonical coverage plan'
            )
        fresh_planned_hook(receipt, plan, stored_target)
    return receipt


def replayed(record: DurableSimulationExecution) -> DurableSimulationExecution:
    """Return an explicit historical receipt without changing storage."""
    if not isinstance(record, DurableSimulationExecution):
        raise TypeError('record must be a DurableSimulationExecution')
    return replace(record, replayed=True)


__all__ = [
    'DurableSimulationExecution',
    'SIMULATION_ASSURANCE_LEVEL',
    'SIMULATION_CONTRACT_VERSION',
    'SIMULATION_LEDGER_SCHEMA_VERSION',
    'SIMULATION_PROFILE_REVISION',
    'SimulationApprovalRequiredError',
    'SimulationAssuranceError',
    'SimulationConsumeConflictError',
    'SimulationConsumeRequest',
    'SimulationExecutionAlreadyConsumedError',
    'SimulationExecutionContractUpgradeRequiredError',
    'SimulationExecutionNotFoundError',
    'SimulationExecutionSchemaError',
    'SimulationExecutionTrustVerifier',
    'VerifiedSimulationApproval',
    'prepare_simulation_schema_locked',
    'validate_simulation_schema_locked',
]
