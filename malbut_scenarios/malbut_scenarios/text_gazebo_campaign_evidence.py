"""Content-free aggregate evidence for text-to-Gazebo campaigns."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Dict, Optional, Tuple

from malbut_scenarios.text_gazebo_campaign_core import (
    CampaignProfile,
    SafetyBlockCode,
    UnknownResultCode,
    campaign_profile_binding,
)
from malbut_scenarios.text_gazebo_evidence import (
    ProductOutcome as ChildProductOutcome,
    ExecutionFaultObservation,
    SafetyFaultObservation,
    CleanupEvidence,
    ConfirmationState,
    DispatchState,
    EvidenceCounts,
    EvidenceDurations,
    NavigationState,
    PressureEvidence,
    ReadinessState,
    RobotActionState,
    StableStates,
    TextGazeboEvidenceManifest,
    TextGazeboEvidenceReceipt,
    TestStatus,
    execution_fault_observation_for,
    pressure_evidence_for,
)
from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboExecutionProfile,
    TextGazeboFaultProfile,
    TextGazeboSafetyProfile,
    TextGazeboScenarioProfile,
    execution_contract,
    safety_contract,
    scenario_spec,
)


CAMPAIGN_EVIDENCE_FORMAT = (
    'malbut.text-gazebo-campaign-evidence.v5'
)
CHILD_EVIDENCE_FORMAT = 'malbut.text-gazebo-e2e-evidence.v6'
MAX_CAMPAIGN_CASES = 32
MAX_CHILD_MANIFEST_BYTES = 64 * 1024
MAX_DURATION_SECONDS = 86_400.0
MAX_EVIDENCE_COUNT = 1_000_000
CAMPAIGN_PROFILE_ALLOWLIST = frozenset(
    profile.value for profile in CampaignProfile
)

_CAMPAIGN_ID = re.compile(r'campaign-[0-9a-f]{32}\Z')
_CASE_ID = re.compile(r'[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z')
_PROFILE = re.compile(r'[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z')
_RUN_ID = re.compile(r'run-[0-9a-f]{32}\Z')
_GIT_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_EMPTY_GOAL_SET_DIGEST = hashlib.sha256(b'[]').hexdigest()

_CHILD_MANIFEST_KEYS = frozenset({
    'format',
    'receipt',
    'receipt_digest',
})
_CHILD_RECEIPT_KEYS = frozenset({
    'cleanup',
    'block_result_code',
    'commit',
    'counts',
    'durations',
    'execution_fault_observation',
    'execution_profile',
    'fault_profile',
    'fault_observation',
    'goal_set_digest',
    'installed_digest',
    'physical_authorized',
    'pressure',
    'product_outcome',
    'run_id',
    'runtime_binding_digest',
    'scenario_profile',
    'safety_profile',
    'simulation',
    'source_tree_digest',
    'states',
    'target_binding_digest',
    'test_status',
    'unknown_result_code',
})
_CHILD_STATE_KEYS = frozenset({
    'confirmation',
    'dispatch',
    'navigation',
    'readiness',
    'robot_action',
})
_CHILD_COUNT_KEYS = frozenset({
    'agent_proposal_count',
    'approved_confirmation_count',
    'confirmation_count',
    'dispatch_intent_count',
    'nav2_goal_count',
    'preapproval_nav2_goal_count',
    'replay_additional_effect_count',
    'robot_action_count',
    'robot_web_start_count',
    'robot_web_verified_target_count',
    'terminal_result_count',
})
_CHILD_DURATION_KEYS = frozenset({
    'cleanup_seconds',
    'execution_seconds',
    'readiness_seconds',
    'total_seconds',
})
_CHILD_CLEANUP_KEYS = frozenset({
    'completed',
    'forced_termination_count',
    'owned_processes_remaining',
    'owned_sockets_remaining',
    'ros_nodes_remaining',
})
_CHILD_PRESSURE_KEYS = frozenset({
    'approval_attempt_count',
    'pressure_contender_count',
    'pressure_nonwinner_count',
    'pressure_winner_count',
    'request_attempt_count',
    'worker_contender_count',
})
_CHILD_FAULT_OBSERVATION_KEYS = frozenset({
    'fault_application_count',
    'map_switch_count',
    'observed',
})
_CHILD_EXECUTION_FAULT_OBSERVATION_KEYS = frozenset({
    'fault_application_count',
    'observed',
    'start_forward_count',
    'start_response_drop_count',
    'terminal_status_response_drop_count',
    'unavailable_endpoint_count',
})


class CampaignEvidenceError(ValueError):
    """Expose only a stable code for untrusted child evidence failures."""

    _CODES = frozenset({
        'child_manifest_digest_invalid',
        'child_manifest_encoding_invalid',
        'child_manifest_format_invalid',
        'child_manifest_json_invalid',
        'child_manifest_payload_invalid',
        'child_manifest_schema_invalid',
        'child_manifest_success_invalid',
        'child_manifest_too_large',
        'child_manifest_unexpected_failure',
    })

    def __init__(self, code: str) -> None:
        """Normalize errors without retaining payload-derived details."""
        normalized = (
            code
            if code in self._CODES
            else 'child_manifest_unexpected_failure'
        )
        super().__init__(normalized)
        self.code = normalized


class ProductOutcome(str, Enum):
    """Bounded product outcomes expected or observed by a case."""

    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    BLOCKED = 'blocked'
    UNKNOWN = 'unknown'
    NOT_OBSERVED = 'not_observed'


class CaseTestVerdict(str, Enum):
    """Test verdict kept separate from the product outcome."""

    PASSED = 'passed'
    FAILED = 'failed'
    PARTIAL = 'partial'


class CampaignTestVerdict(str, Enum):
    """Aggregate test verdict for a complete campaign receipt."""

    PASSED = 'passed'
    FAILED = 'failed'
    PARTIAL = 'partial'


class CaseCleanupState(str, Enum):
    """Bounded cleanup observation for one ordered campaign case."""

    CLEAN = 'clean'
    INCOMPLETE = 'incomplete'
    NOT_OBSERVED = 'not_observed'


class CaseErrorCode(str, Enum):
    """Stable reason for a non-passing case without private details."""

    NONE = 'none'
    PRODUCT_OUTCOME_MISMATCH = 'product_outcome_mismatch'
    EXECUTOR_FAILED = 'executor_failed'
    EXECUTOR_EXCEPTION = 'executor_exception'
    EXECUTION_RESULT_INVALID = 'execution_result_invalid'
    CLEANUP_INCOMPLETE = 'cleanup_incomplete'
    PROVENANCE_MISMATCH = 'provenance_mismatch'
    PREVIOUS_CASE_UNSAFE = 'previous_case_unsafe'


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(',', ':'),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _require_enum(value: object, expected: type[Enum], name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f'{name} must be a {expected.__name__}')


def _require_digest(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f'{name} must be a lowercase SHA-256')


def _require_count(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_EVIDENCE_COUNT
    ):
        raise ValueError(f'{name} must be a bounded non-negative integer')


def _duration(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite duration')
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized < 0.0
        or normalized > MAX_DURATION_SECONDS
    ):
        raise ValueError(f'{name} must be a finite bounded duration')
    return 0.0 if normalized == 0.0 else normalized


@dataclass(frozen=True, repr=False, slots=True)
class ChildManifestSummary:
    """Validated digest-only projection of one SWM25-133 manifest."""

    manifest_digest: str
    receipt_digest: str
    run_id: str
    commit: str
    source_tree_digest: str
    installed_digest: str
    goal_set_digest: str
    runtime_binding_digest: str
    target_binding_digest: str
    scenario_profile: TextGazeboScenarioProfile
    cleanup_complete: bool
    owned_processes_remaining: int
    ros_nodes_remaining: int
    owned_sockets_remaining: int
    forced_termination_count: int
    simulation: bool
    physical_authorized: bool
    exact_success: bool
    total_duration_seconds: float
    fault_profile: TextGazeboFaultProfile = TextGazeboFaultProfile.NONE
    pressure: PressureEvidence = field(
        default_factory=lambda: pressure_evidence_for(
            TextGazeboFaultProfile.NONE
        )
    )
    safety_profile: TextGazeboSafetyProfile = TextGazeboSafetyProfile.NONE
    execution_profile: TextGazeboExecutionProfile = (
        TextGazeboExecutionProfile.NONE
    )
    product_outcome: ProductOutcome = ProductOutcome.SUCCEEDED
    block_result_code: SafetyBlockCode = SafetyBlockCode.NONE
    unknown_result_code: UnknownResultCode = UnknownResultCode.NONE
    test_status: TestStatus = TestStatus.PASSED
    fault_observation: SafetyFaultObservation = field(
        default_factory=lambda: SafetyFaultObservation(
            observed=False,
            fault_application_count=0,
            map_switch_count=0,
        )
    )
    execution_fault_observation: ExecutionFaultObservation = field(
        default_factory=lambda: execution_fault_observation_for(
            TextGazeboExecutionProfile.NONE
        )
    )

    def __post_init__(self) -> None:
        """Reject summaries that could support an unproven success claim."""
        for name in (
            'manifest_digest',
            'receipt_digest',
            'source_tree_digest',
            'installed_digest',
            'goal_set_digest',
            'runtime_binding_digest',
            'target_binding_digest',
        ):
            _require_digest(getattr(self, name), name)
        _require_enum(
            self.scenario_profile,
            TextGazeboScenarioProfile,
            'scenario_profile',
        )
        _require_enum(
            self.fault_profile,
            TextGazeboFaultProfile,
            'fault_profile',
        )
        _require_enum(
            self.safety_profile,
            TextGazeboSafetyProfile,
            'safety_profile',
        )
        _require_enum(
            self.execution_profile,
            TextGazeboExecutionProfile,
            'execution_profile',
        )
        _require_enum(
            self.product_outcome,
            ProductOutcome,
            'product_outcome',
        )
        _require_enum(
            self.block_result_code,
            SafetyBlockCode,
            'block_result_code',
        )
        _require_enum(
            self.unknown_result_code,
            UnknownResultCode,
            'unknown_result_code',
        )
        _require_enum(self.test_status, TestStatus, 'test_status')
        if not isinstance(
            self.fault_observation,
            SafetyFaultObservation,
        ):
            raise TypeError(
                'fault_observation must be a SafetyFaultObservation'
            )
        if not isinstance(
            self.execution_fault_observation,
            ExecutionFaultObservation,
        ):
            raise TypeError(
                'execution_fault_observation must be an '
                'ExecutionFaultObservation'
            )
        if not isinstance(self.pressure, PressureEvidence):
            raise TypeError('pressure must be a PressureEvidence')
        if self.pressure != pressure_evidence_for(self.fault_profile):
            raise ValueError(
                'pressure must match the exact fault profile contract'
            )
        if (
            not isinstance(self.run_id, str)
            or _RUN_ID.fullmatch(self.run_id) is None
        ):
            raise ValueError('run_id must use the public run format')
        if (
            not isinstance(self.commit, str)
            or _GIT_COMMIT.fullmatch(self.commit) is None
        ):
            raise ValueError('commit must be a full lowercase Git object id')
        for name in (
            'cleanup_complete',
            'simulation',
            'physical_authorized',
            'exact_success',
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f'{name} must be a bool')
        for name in (
            'owned_processes_remaining',
            'ros_nodes_remaining',
            'owned_sockets_remaining',
            'forced_termination_count',
        ):
            _require_count(getattr(self, name), name)
        object.__setattr__(
            self,
            'total_duration_seconds',
            _duration(
                self.total_duration_seconds,
                'total_duration_seconds',
            ),
        )
        if not (
            self.cleanup_complete
            and self.owned_processes_remaining == 0
            and self.ros_nodes_remaining == 0
            and self.owned_sockets_remaining == 0
            and self.forced_termination_count == 0
            and self.simulation
            and not self.physical_authorized
        ):
            raise ValueError(
                'child summary must prove clean simulation completion'
            )
        contract = safety_contract(self.safety_profile)
        execution = execution_contract(self.execution_profile)
        expected_block_code = (
            SafetyBlockCode.NONE
            if contract.result_code is None
            else SafetyBlockCode(contract.result_code)
        )
        expected_outcome = ProductOutcome.SUCCEEDED
        if self.safety_profile is not TextGazeboSafetyProfile.NONE:
            expected_outcome = ProductOutcome.BLOCKED
        if self.execution_profile is not TextGazeboExecutionProfile.NONE:
            expected_outcome = ProductOutcome.UNKNOWN
        expected_unknown_code = (
            UnknownResultCode.NONE
            if execution.result_code is None
            else UnknownResultCode(execution.result_code)
        )
        if (
            self.test_status is not TestStatus.PASSED
            or self.product_outcome is not expected_outcome
            or self.block_result_code is not expected_block_code
            or self.unknown_result_code is not expected_unknown_code
            or self.exact_success
            is not (self.product_outcome is ProductOutcome.SUCCEEDED)
            or (
                self.safety_profile is not TextGazeboSafetyProfile.NONE
                and (
                    self.fault_profile is not TextGazeboFaultProfile.NONE
                    or self.execution_profile
                    is not TextGazeboExecutionProfile.NONE
                )
            )
            or (
                self.execution_profile
                is not TextGazeboExecutionProfile.NONE
                and (
                    self.fault_profile is not TextGazeboFaultProfile.NONE
                    or self.safety_profile
                    is not TextGazeboSafetyProfile.NONE
                )
            )
            or self.fault_observation.observed
            is not (
                self.safety_profile is not TextGazeboSafetyProfile.NONE
            )
            or self.fault_observation.fault_application_count
            != contract.fault_application_count
            or self.fault_observation.map_switch_count
            != contract.map_switch_count
            or self.execution_fault_observation
            != execution_fault_observation_for(self.execution_profile)
            or (
                self.product_outcome is ProductOutcome.SUCCEEDED
                and self.goal_set_digest == _EMPTY_GOAL_SET_DIGEST
            )
            or (
                self.product_outcome is ProductOutcome.BLOCKED
                and self.goal_set_digest != _EMPTY_GOAL_SET_DIGEST
            )
            or (
                self.product_outcome is ProductOutcome.UNKNOWN
                and (
                    (
                        execution.expected_nav2_goal_count == 0
                        and self.goal_set_digest != _EMPTY_GOAL_SET_DIGEST
                    )
                    or (
                        execution.expected_nav2_goal_count > 0
                        and self.goal_set_digest == _EMPTY_GOAL_SET_DIGEST
                    )
                )
            )
        ):
            raise ValueError(
                'child summary product and safety contract is invalid'
            )

    def __repr__(self) -> str:
        """Avoid rendering child identifiers or provenance in diagnostics."""
        return (
            'ChildManifestSummary('
            f'manifest_digest={self.manifest_digest!r}, '
            f'product_outcome={self.product_outcome.value!r})'
        )


@dataclass(frozen=True, repr=False, slots=True)
class CampaignCaseEvidence:
    """One ordered case result with an optional validated child binding."""

    ordinal: int
    case_id: str
    profile: str
    expected_outcome: ProductOutcome
    observed_outcome: ProductOutcome
    test_verdict: CaseTestVerdict
    error_code: CaseErrorCode
    child_manifest: Optional[ChildManifestSummary]
    duration_seconds: float
    cleanup: CaseCleanupState
    expected_block_code: SafetyBlockCode = SafetyBlockCode.NONE
    observed_block_code: SafetyBlockCode = SafetyBlockCode.NONE
    expected_unknown_result_code: UnknownResultCode = UnknownResultCode.NONE
    observed_unknown_result_code: UnknownResultCode = UnknownResultCode.NONE

    def __post_init__(self) -> None:
        """Require bounded public labels and internally consistent facts."""
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or not 1 <= self.ordinal <= MAX_CAMPAIGN_CASES
        ):
            raise ValueError('ordinal must be a bounded 1-based integer')
        for value, pattern, name in (
            (self.case_id, _CASE_ID, 'case_id'),
            (self.profile, _PROFILE, 'profile'),
        ):
            if (
                not isinstance(value, str)
                or len(value) > 48
                or pattern.fullmatch(value) is None
            ):
                raise ValueError(
                    f'{name} must use the public identifier format'
                )
        if self.profile not in CAMPAIGN_PROFILE_ALLOWLIST:
            raise ValueError('profile is not allowlisted')
        _require_enum(
            self.expected_outcome,
            ProductOutcome,
            'expected_outcome',
        )
        _require_enum(
            self.observed_outcome,
            ProductOutcome,
            'observed_outcome',
        )
        _require_enum(self.test_verdict, CaseTestVerdict, 'test_verdict')
        _require_enum(self.error_code, CaseErrorCode, 'error_code')
        _require_enum(self.cleanup, CaseCleanupState, 'cleanup')
        _require_enum(
            self.expected_block_code,
            SafetyBlockCode,
            'expected_block_code',
        )
        _require_enum(
            self.observed_block_code,
            SafetyBlockCode,
            'observed_block_code',
        )
        _require_enum(
            self.expected_unknown_result_code,
            UnknownResultCode,
            'expected_unknown_result_code',
        )
        _require_enum(
            self.observed_unknown_result_code,
            UnknownResultCode,
            'observed_unknown_result_code',
        )
        if (
            self.expected_outcome is ProductOutcome.BLOCKED
        ) is (self.expected_block_code is SafetyBlockCode.NONE):
            raise ValueError(
                'expected block code must match the product outcome'
            )
        if self.observed_outcome is ProductOutcome.NOT_OBSERVED:
            if self.observed_block_code is not SafetyBlockCode.NONE:
                raise ValueError(
                    'an unobserved product cannot claim a block code'
                )
        elif (
            self.observed_outcome is ProductOutcome.BLOCKED
        ) is (self.observed_block_code is SafetyBlockCode.NONE):
            raise ValueError(
                'observed block code must match the product outcome'
            )
        if (
            self.expected_outcome is ProductOutcome.UNKNOWN
        ) is (
            self.expected_unknown_result_code is UnknownResultCode.NONE
        ):
            raise ValueError(
                'expected unknown code must match the product outcome'
            )
        if self.observed_outcome is ProductOutcome.NOT_OBSERVED:
            if self.observed_unknown_result_code is not UnknownResultCode.NONE:
                raise ValueError(
                    'an unobserved product cannot claim an unknown code'
                )
        elif (
            self.observed_outcome is ProductOutcome.UNKNOWN
        ) is (
            self.observed_unknown_result_code is UnknownResultCode.NONE
        ):
            raise ValueError(
                'observed unknown code must match the product outcome'
            )
        if (
            self.child_manifest is not None
            and not isinstance(self.child_manifest, ChildManifestSummary)
        ):
            raise TypeError(
                'child_manifest must be a ChildManifestSummary or None'
            )
        object.__setattr__(
            self,
            'duration_seconds',
            _duration(self.duration_seconds, 'duration_seconds'),
        )
        if self.child_manifest is not None:
            binding = campaign_profile_binding(CampaignProfile(self.profile))
            if (
                self.child_manifest.scenario_profile
                is not binding.scenario_profile
                or self.child_manifest.fault_profile
                is not binding.fault_profile
                or self.child_manifest.safety_profile
                is not binding.safety_profile
                or self.child_manifest.execution_profile
                is not binding.execution_profile
            ):
                raise ValueError(
                    'child scenario profile, fault profile, safety profile, '
                    'and execution profile must match '
                    'the campaign case binding'
                )
            if (
                self.expected_outcome.value
                != binding.expected_outcome.value
                or self.expected_block_code
                is not binding.expected_block_code
                or self.expected_unknown_result_code
                is not binding.expected_unknown_result_code
                or self.observed_outcome
                is not self.child_manifest.product_outcome
                or self.observed_block_code
                is not self.child_manifest.block_result_code
                or self.observed_unknown_result_code
                is not self.child_manifest.unknown_result_code
            ):
                raise ValueError(
                    'case outcome and block code must match child evidence'
                )
            if (
                Decimal(str(self.duration_seconds))
                < Decimal(str(self.child_manifest.total_duration_seconds))
            ):
                raise ValueError('case duration must cover child duration')
        if self.expected_outcome is ProductOutcome.NOT_OBSERVED:
            raise ValueError('expected outcome cannot be not_observed')
        if self.test_verdict is CaseTestVerdict.PASSED:
            if (
                self.observed_outcome is not self.expected_outcome
                or self.observed_block_code
                is not self.expected_block_code
                or self.observed_unknown_result_code
                is not self.expected_unknown_result_code
                or self.observed_outcome is ProductOutcome.NOT_OBSERVED
                or self.error_code is not CaseErrorCode.NONE
                or self.child_manifest is None
                or self.cleanup is not CaseCleanupState.CLEAN
            ):
                raise ValueError('passed case evidence is inconsistent')
        elif self.test_verdict is CaseTestVerdict.PARTIAL:
            if not (
                self.observed_outcome is ProductOutcome.NOT_OBSERVED
                or self.child_manifest is None
                or self.cleanup is CaseCleanupState.NOT_OBSERVED
            ):
                raise ValueError('partial case evidence is inconsistent')
            if self.error_code is CaseErrorCode.NONE:
                raise ValueError('partial case evidence requires an error')
        elif self.error_code is CaseErrorCode.NONE:
            raise ValueError('failed case evidence requires an error')

    @property
    def child_manifest_digest(self) -> Optional[str]:
        """Return the child binding without exposing its run identifier."""
        if self.child_manifest is None:
            return None
        return self.child_manifest.manifest_digest

    @property
    def target_binding_digest(self) -> Optional[str]:
        """Return the content-bound semantic target without its identity."""
        if self.child_manifest is None:
            return None
        return self.child_manifest.target_binding_digest

    @property
    def scenario_profile(self) -> Optional[str]:
        """Return the bounded semantic profile proven by the child."""
        if self.child_manifest is None:
            return None
        return self.child_manifest.scenario_profile.value

    @property
    def fault_profile(self) -> Optional[str]:
        """Return the bounded pressure behavior proven by the child."""
        if self.child_manifest is None:
            return None
        return self.child_manifest.fault_profile.value

    @property
    def safety_profile(self) -> Optional[str]:
        """Return the bounded dispatch-time Safety profile when observed."""
        if self.child_manifest is None:
            return None
        return self.child_manifest.safety_profile.value

    @property
    def execution_profile(self) -> Optional[str]:
        """Return the bounded execution profile proven by the child."""
        if self.child_manifest is None:
            return None
        return self.child_manifest.execution_profile.value

    @property
    def execution_fault_observation(self) -> Optional[Dict[str, object]]:
        """Return exact content-free execution fault counters."""
        if self.child_manifest is None:
            return None
        return self.child_manifest.execution_fault_observation.as_dict()

    @property
    def pressure(self) -> Optional[Dict[str, int]]:
        """Return exact content-free contention counters when observed."""
        if self.child_manifest is None:
            return None
        return self.child_manifest.pressure.as_dict()

    def as_dict(self) -> Dict[str, object]:
        """Return the fixed, ordered, content-free case projection."""
        return {
            'case_id': self.case_id,
            'child_manifest_digest': self.child_manifest_digest,
            'cleanup': self.cleanup.value,
            'duration_seconds': self.duration_seconds,
            'error_code': self.error_code.value,
            'expected_block_code': self.expected_block_code.value,
            'expected_unknown_result_code': (
                self.expected_unknown_result_code.value
            ),
            'expected_outcome': self.expected_outcome.value,
            'execution_fault_observation': self.execution_fault_observation,
            'execution_profile': self.execution_profile,
            'fault_profile': self.fault_profile,
            'observed_block_code': self.observed_block_code.value,
            'observed_unknown_result_code': (
                self.observed_unknown_result_code.value
            ),
            'observed_outcome': self.observed_outcome.value,
            'ordinal': self.ordinal,
            'pressure': self.pressure,
            'profile': self.profile,
            'scenario_profile': self.scenario_profile,
            'safety_profile': self.safety_profile,
            'target_binding_digest': self.target_binding_digest,
            'test_verdict': self.test_verdict.value,
        }

    def __repr__(self) -> str:
        """Render only bounded control states and the explicit order."""
        return (
            'CampaignCaseEvidence('
            f'ordinal={self.ordinal!r}, '
            f'test_verdict={self.test_verdict.value!r})'
        )


@dataclass(frozen=True, slots=True)
class CampaignCleanupAggregate:
    """Aggregate cleanup facts without process, node, or socket names."""

    completed: bool
    clean_case_count: int
    incomplete_case_count: int
    not_observed_case_count: int
    owned_processes_remaining: int
    ros_nodes_remaining: int
    owned_sockets_remaining: int
    forced_termination_count: int

    def __post_init__(self) -> None:
        """Require an explicit verdict and bounded aggregate counts."""
        if type(self.completed) is not bool:
            raise TypeError('completed must be a bool')
        for name in self.__dataclass_fields__:
            if name != 'completed':
                _require_count(getattr(self, name), name)
        clean = (
            self.incomplete_case_count == 0
            and self.not_observed_case_count == 0
            and self.owned_processes_remaining == 0
            and self.ros_nodes_remaining == 0
            and self.owned_sockets_remaining == 0
            and self.forced_termination_count == 0
        )
        if self.completed is not clean:
            raise ValueError('cleanup completed flag is inconsistent')

    @property
    def case_count(self) -> int:
        """Return the total number of cases represented by the aggregate."""
        return (
            self.clean_case_count
            + self.incomplete_case_count
            + self.not_observed_case_count
        )

    def as_dict(self) -> Dict[str, object]:
        """Return the exact public cleanup projection."""
        return {
            'clean_case_count': self.clean_case_count,
            'completed': self.completed,
            'forced_termination_count': self.forced_termination_count,
            'incomplete_case_count': self.incomplete_case_count,
            'not_observed_case_count': self.not_observed_case_count,
            'owned_processes_remaining': self.owned_processes_remaining,
            'owned_sockets_remaining': self.owned_sockets_remaining,
            'ros_nodes_remaining': self.ros_nodes_remaining,
        }


@dataclass(frozen=True, repr=False, slots=True)
class TextGazeboCampaignReceipt:
    """Immutable aggregate facts for one ordered simulation campaign."""

    campaign_id: str
    commit: str
    source_tree_digest: str
    installed_digest: str
    cases: Tuple[CampaignCaseEvidence, ...]
    test_verdict: CampaignTestVerdict
    stopped_early: bool
    total_duration_seconds: float
    cleanup: CampaignCleanupAggregate
    simulation: bool = field(default=True, init=False)
    physical_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Bind order, provenance, verdict, duration, and cleanup exactly."""
        if (
            not isinstance(self.campaign_id, str)
            or _CAMPAIGN_ID.fullmatch(self.campaign_id) is None
        ):
            raise ValueError(
                'campaign_id must use the public campaign format'
            )
        if (
            not isinstance(self.commit, str)
            or _GIT_COMMIT.fullmatch(self.commit) is None
        ):
            raise ValueError('commit must be a full lowercase Git object id')
        _require_digest(self.source_tree_digest, 'source_tree_digest')
        _require_digest(self.installed_digest, 'installed_digest')
        if not isinstance(self.cases, tuple) or not self.cases:
            raise ValueError('cases must be a non-empty tuple')
        if len(self.cases) > MAX_CAMPAIGN_CASES:
            raise ValueError('cases exceed the campaign limit')
        if any(
            not isinstance(case, CampaignCaseEvidence)
            for case in self.cases
        ):
            raise TypeError('cases must contain CampaignCaseEvidence values')
        expected_ordinals = tuple(range(1, len(self.cases) + 1))
        actual_ordinals = tuple(case.ordinal for case in self.cases)
        if actual_ordinals != expected_ordinals:
            raise ValueError('case ordinals must match tuple order')
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError('case identifiers must be unique')
        _require_enum(
            self.test_verdict,
            CampaignTestVerdict,
            'test_verdict',
        )
        if type(self.stopped_early) is not bool:
            raise TypeError('stopped_early must be a bool')
        object.__setattr__(
            self,
            'total_duration_seconds',
            _duration(
                self.total_duration_seconds,
                'total_duration_seconds',
            ),
        )
        if not isinstance(self.cleanup, CampaignCleanupAggregate):
            raise TypeError('cleanup must be a CampaignCleanupAggregate')
        self._validate_child_bindings()
        self._validate_aggregate()

    def _validate_child_bindings(self) -> None:
        run_ids = []
        manifest_digests = []
        goal_set_digests = []
        target_by_location: Dict[str, str] = {}
        location_by_target: Dict[str, str] = {}
        for case in self.cases:
            child = case.child_manifest
            if child is None:
                continue
            if (
                child.commit != self.commit
                or child.source_tree_digest != self.source_tree_digest
                or child.installed_digest != self.installed_digest
            ):
                raise ValueError(
                    'child provenance must match campaign provenance'
                )
            run_ids.append(child.run_id)
            manifest_digests.append(child.manifest_digest)
            if child.product_outcome is ProductOutcome.SUCCEEDED:
                if child.goal_set_digest == _EMPTY_GOAL_SET_DIGEST:
                    raise ValueError(
                        'a successful child requires a non-empty goal set'
                    )
                goal_set_digests.append(child.goal_set_digest)
            elif child.product_outcome is ProductOutcome.UNKNOWN:
                contract = execution_contract(child.execution_profile)
                if contract.expected_nav2_goal_count == 0:
                    if child.goal_set_digest != _EMPTY_GOAL_SET_DIGEST:
                        raise ValueError(
                            'a zero-goal child requires the empty goal set'
                        )
                else:
                    if child.goal_set_digest == _EMPTY_GOAL_SET_DIGEST:
                        raise ValueError(
                            'an observed goal requires a non-empty goal set'
                        )
                    goal_set_digests.append(child.goal_set_digest)
            elif child.goal_set_digest != _EMPTY_GOAL_SET_DIGEST:
                raise ValueError(
                    'a blocked child requires the empty goal-set digest'
                )
            binding = campaign_profile_binding(
                CampaignProfile(case.profile)
            )
            location = scenario_spec(binding.scenario_profile).location
            previous_target = target_by_location.setdefault(
                location,
                child.target_binding_digest,
            )
            if previous_target != child.target_binding_digest:
                raise ValueError(
                    'one semantic target must keep one binding digest'
                )
            previous_location = location_by_target.setdefault(
                child.target_binding_digest,
                location,
            )
            if previous_location != location:
                raise ValueError(
                    'different semantic targets must use different bindings'
                )
        if len(set(run_ids)) != len(run_ids):
            raise ValueError('child run identifiers must be unique')
        if len(set(manifest_digests)) != len(manifest_digests):
            raise ValueError('child manifest digests must be unique')
        if len(set(goal_set_digests)) != len(goal_set_digests):
            raise ValueError('child goal-set digests must be unique')

    def _validate_aggregate(self) -> None:
        case_cleanup = {
            state: sum(case.cleanup is state for case in self.cases)
            for state in CaseCleanupState
        }
        if (
            self.cleanup.case_count != len(self.cases)
            or self.cleanup.clean_case_count
            != case_cleanup[CaseCleanupState.CLEAN]
            or self.cleanup.incomplete_case_count
            != case_cleanup[CaseCleanupState.INCOMPLETE]
            or self.cleanup.not_observed_case_count
            != case_cleanup[CaseCleanupState.NOT_OBSERVED]
        ):
            raise ValueError('cleanup aggregate does not match cases')

        any_failed = any(
            case.test_verdict is CaseTestVerdict.FAILED
            or (
                case.observed_outcome
                is not ProductOutcome.NOT_OBSERVED
                and case.observed_outcome is not case.expected_outcome
            )
            or (
                case.observed_outcome
                is not ProductOutcome.NOT_OBSERVED
                and case.observed_block_code
                is not case.expected_block_code
            )
            for case in self.cases
        )
        any_partial = any(
            case.test_verdict is CaseTestVerdict.PARTIAL
            for case in self.cases
        )
        case_duration = sum(
            (Decimal(str(case.duration_seconds)) for case in self.cases),
            Decimal('0'),
        )
        if Decimal(str(self.total_duration_seconds)) < case_duration:
            raise ValueError('campaign duration must cover case durations')
        if any_partial and not self.stopped_early:
            raise ValueError('partial campaign cases require stopped_early')
        if self.stopped_early:
            partial_indexes = tuple(
                index
                for index, case in enumerate(self.cases)
                if case.test_verdict is CaseTestVerdict.PARTIAL
            )
            if (
                not partial_indexes
                or partial_indexes[0] == 0
                or partial_indexes
                != tuple(range(partial_indexes[0], len(self.cases)))
                or self.cases[partial_indexes[0] - 1].test_verdict
                is not CaseTestVerdict.FAILED
                or any(
                    case.error_code
                    is not CaseErrorCode.PREVIOUS_CASE_UNSAFE
                    or case.observed_outcome
                    is not ProductOutcome.NOT_OBSERVED
                    or case.cleanup is not CaseCleanupState.NOT_OBSERVED
                    or case.child_manifest is not None
                    for case in self.cases[partial_indexes[0]:]
                )
            ):
                raise ValueError(
                    'stopped campaign cases must end in a NOT_RUN suffix'
                )
        if any_failed or not self.cleanup.completed:
            expected = CampaignTestVerdict.FAILED
        elif any_partial or self.stopped_early:
            expected = CampaignTestVerdict.PARTIAL
        else:
            expected = CampaignTestVerdict.PASSED
        if self.test_verdict is not expected:
            raise ValueError('campaign verdict is inconsistent with cases')
        if self.test_verdict is CampaignTestVerdict.PASSED:
            if any(
                case.test_verdict is not CaseTestVerdict.PASSED
                or case.observed_outcome is not case.expected_outcome
                or case.observed_block_code
                is not case.expected_block_code
                or case.child_manifest is None
                or case.cleanup is not CaseCleanupState.CLEAN
                for case in self.cases
            ):
                raise ValueError('passed campaign requires complete cases')

    def as_dict(self) -> Dict[str, object]:
        """Return the fixed, content-free aggregate receipt schema."""
        return {
            'campaign_id': self.campaign_id,
            'cases': [case.as_dict() for case in self.cases],
            'cleanup': self.cleanup.as_dict(),
            'commit': self.commit,
            'installed_digest': self.installed_digest,
            'physical_authorized': self.physical_authorized,
            'simulation': self.simulation,
            'source_tree_digest': self.source_tree_digest,
            'stopped_early': self.stopped_early,
            'test_verdict': self.test_verdict.value,
            'total_duration_seconds': self.total_duration_seconds,
        }

    def canonical_json(self) -> str:
        """Serialize the aggregate deterministically without extra space."""
        return _canonical_json(self.as_dict())

    def digest(self) -> str:
        """Bind the complete canonical receipt with SHA-256."""
        return _sha256(self.canonical_json())

    def __repr__(self) -> str:
        """Avoid expanding campaign identifiers and case evidence."""
        return (
            'TextGazeboCampaignReceipt('
            f'case_count={len(self.cases)!r}, '
            f'test_verdict={self.test_verdict.value!r})'
        )


@dataclass(frozen=True, repr=False, slots=True)
class TextGazeboCampaignManifest:
    """Versioned envelope for one immutable campaign receipt."""

    receipt: TextGazeboCampaignReceipt
    format: str = field(default=CAMPAIGN_EVIDENCE_FORMAT, init=False)
    receipt_digest: str = field(init=False)

    def __post_init__(self) -> None:
        """Bind the manifest to exactly one validated campaign receipt."""
        if not isinstance(self.receipt, TextGazeboCampaignReceipt):
            raise TypeError('receipt must be a TextGazeboCampaignReceipt')
        object.__setattr__(self, 'receipt_digest', self.receipt.digest())

    def as_dict(self) -> Dict[str, object]:
        """Return the fixed, versioned campaign manifest schema."""
        return {
            'format': self.format,
            'receipt': self.receipt.as_dict(),
            'receipt_digest': self.receipt_digest,
        }

    def canonical_json(self) -> str:
        """Serialize the complete manifest canonically for storage."""
        return _canonical_json(self.as_dict())

    def digest(self) -> str:
        """Return the digest of the complete canonical manifest."""
        return _sha256(self.canonical_json())

    def __repr__(self) -> str:
        """Render only a content digest, never the campaign identifier."""
        return (
            'TextGazeboCampaignManifest('
            f'digest={self.digest()!r})'
        )


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> Dict[str, object]:
    value: Dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = item
    return value


def _reject_json_constant(unused_value: str) -> object:
    del unused_value
    raise ValueError


def _exact_keys(
    value: object,
    expected: frozenset[str],
) -> Dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != expected:
        raise CampaignEvidenceError('child_manifest_schema_invalid')
    return value


def _child_receipt(value: Dict[str, object]) -> TextGazeboEvidenceReceipt:
    try:
        states_value = _exact_keys(value['states'], _CHILD_STATE_KEYS)
        counts_value = _exact_keys(value['counts'], _CHILD_COUNT_KEYS)
        durations_value = _exact_keys(
            value['durations'],
            _CHILD_DURATION_KEYS,
        )
        cleanup_value = _exact_keys(
            value['cleanup'],
            _CHILD_CLEANUP_KEYS,
        )
        pressure_value = _exact_keys(
            value['pressure'],
            _CHILD_PRESSURE_KEYS,
        )
        fault_observation_value = _exact_keys(
            value['fault_observation'],
            _CHILD_FAULT_OBSERVATION_KEYS,
        )
        execution_fault_observation_value = _exact_keys(
            value['execution_fault_observation'],
            _CHILD_EXECUTION_FAULT_OBSERVATION_KEYS,
        )
        states = StableStates(
            readiness=ReadinessState(states_value['readiness']),
            confirmation=ConfirmationState(states_value['confirmation']),
            robot_action=RobotActionState(states_value['robot_action']),
            dispatch=DispatchState(states_value['dispatch']),
            navigation=NavigationState(states_value['navigation']),
        )
        counts = EvidenceCounts(**counts_value)
        durations = EvidenceDurations(**durations_value)
        cleanup = CleanupEvidence(**cleanup_value)
        pressure = PressureEvidence(**pressure_value)
        fault_observation = SafetyFaultObservation(
            **fault_observation_value
        )
        execution_fault_observation = ExecutionFaultObservation(
            **execution_fault_observation_value
        )
        return TextGazeboEvidenceReceipt(
            run_id=value['run_id'],
            commit=value['commit'],
            source_tree_digest=value['source_tree_digest'],
            installed_digest=value['installed_digest'],
            goal_set_digest=value['goal_set_digest'],
            runtime_binding_digest=value['runtime_binding_digest'],
            target_binding_digest=value['target_binding_digest'],
            scenario_profile=TextGazeboScenarioProfile(
                value['scenario_profile']
            ),
            fault_profile=TextGazeboFaultProfile(
                value['fault_profile']
            ),
            safety_profile=TextGazeboSafetyProfile(
                value['safety_profile']
            ),
            execution_profile=TextGazeboExecutionProfile(
                value['execution_profile']
            ),
            product_outcome=ChildProductOutcome(
                value['product_outcome']
            ),
            test_status=TestStatus(value['test_status']),
            block_result_code=value['block_result_code'],
            unknown_result_code=value['unknown_result_code'],
            fault_observation=fault_observation,
            execution_fault_observation=execution_fault_observation,
            states=states,
            counts=counts,
            durations=durations,
            cleanup=cleanup,
            pressure=pressure,
        )
    except CampaignEvidenceError:
        raise
    except Exception:
        raise CampaignEvidenceError(
            'child_manifest_success_invalid'
        ) from None


def parse_child_manifest(payload: bytes) -> ChildManifestSummary:
    """Strictly validate bounded canonical SWM25-133 evidence bytes."""
    if not isinstance(payload, bytes) or not payload:
        raise CampaignEvidenceError('child_manifest_payload_invalid')
    if len(payload) > MAX_CHILD_MANIFEST_BYTES:
        raise CampaignEvidenceError('child_manifest_too_large')
    try:
        text = payload.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        raise CampaignEvidenceError(
            'child_manifest_encoding_invalid'
        ) from None
    if not text.endswith('\n') or '\n' in text[:-1] or '\r' in text:
        raise CampaignEvidenceError('child_manifest_json_invalid')
    try:
        value = json.loads(
            text[:-1],
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        raise CampaignEvidenceError('child_manifest_json_invalid') from None
    try:
        manifest_value = _exact_keys(value, _CHILD_MANIFEST_KEYS)
        receipt_value = _exact_keys(
            manifest_value['receipt'],
            _CHILD_RECEIPT_KEYS,
        )
        if manifest_value['format'] != CHILD_EVIDENCE_FORMAT:
            raise CampaignEvidenceError('child_manifest_format_invalid')
        canonical = _canonical_json(manifest_value)
        if text != canonical + '\n':
            raise CampaignEvidenceError('child_manifest_schema_invalid')
        receipt_json = _canonical_json(receipt_value)
        if (
            not isinstance(manifest_value['receipt_digest'], str)
            or not _SHA256.fullmatch(manifest_value['receipt_digest'])
            or manifest_value['receipt_digest'] != _sha256(receipt_json)
        ):
            raise CampaignEvidenceError('child_manifest_digest_invalid')
        receipt = _child_receipt(receipt_value)
        duration_total = Decimal(str(receipt.durations.total_seconds))
        duration_parts = sum(
            (
                Decimal(str(value))
                for value in (
                    receipt.durations.readiness_seconds,
                    receipt.durations.execution_seconds,
                    receipt.durations.cleanup_seconds,
                )
            ),
            Decimal('0'),
        )
        if duration_total < duration_parts:
            raise CampaignEvidenceError(
                'child_manifest_success_invalid'
            )
        typed_manifest = TextGazeboEvidenceManifest(receipt)
        if typed_manifest.canonical_json() != canonical:
            raise CampaignEvidenceError('child_manifest_schema_invalid')
        if (
            receipt.product_outcome is ChildProductOutcome.SUCCEEDED
            and receipt.goal_set_digest == _EMPTY_GOAL_SET_DIGEST
        ) or (
            receipt.product_outcome is ChildProductOutcome.BLOCKED
            and receipt.goal_set_digest != _EMPTY_GOAL_SET_DIGEST
        ):
            raise CampaignEvidenceError(
                'child_manifest_success_invalid'
            )
        cleanup = receipt.cleanup
        return ChildManifestSummary(
            manifest_digest=_sha256(canonical),
            receipt_digest=typed_manifest.receipt_digest,
            run_id=receipt.run_id,
            commit=receipt.commit,
            source_tree_digest=receipt.source_tree_digest,
            installed_digest=receipt.installed_digest,
            goal_set_digest=receipt.goal_set_digest,
            runtime_binding_digest=receipt.runtime_binding_digest,
            target_binding_digest=receipt.target_binding_digest,
            scenario_profile=receipt.scenario_profile,
            fault_profile=receipt.fault_profile,
            safety_profile=receipt.safety_profile,
            execution_profile=receipt.execution_profile,
            product_outcome=ProductOutcome(
                receipt.product_outcome.value
            ),
            block_result_code=(
                SafetyBlockCode.NONE
                if receipt.block_result_code is None
                else SafetyBlockCode(receipt.block_result_code)
            ),
            unknown_result_code=(
                UnknownResultCode.NONE
                if receipt.unknown_result_code is None
                else UnknownResultCode(receipt.unknown_result_code)
            ),
            test_status=receipt.test_status,
            fault_observation=receipt.fault_observation,
            execution_fault_observation=(
                receipt.execution_fault_observation
            ),
            pressure=receipt.pressure,
            cleanup_complete=cleanup.completed,
            owned_processes_remaining=(
                cleanup.owned_processes_remaining
            ),
            ros_nodes_remaining=cleanup.ros_nodes_remaining,
            owned_sockets_remaining=cleanup.owned_sockets_remaining,
            forced_termination_count=cleanup.forced_termination_count,
            simulation=receipt.simulation,
            physical_authorized=receipt.physical_authorized,
            exact_success=(
                receipt.product_outcome is ChildProductOutcome.SUCCEEDED
            ),
            total_duration_seconds=receipt.durations.total_seconds,
        )
    except CampaignEvidenceError:
        raise
    except Exception:
        raise CampaignEvidenceError(
            'child_manifest_unexpected_failure'
        ) from None


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(
                'campaign evidence path cannot contain a symbolic link'
            )


def _private_parent(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError('campaign evidence path must be a pathlib.Path')
    if not path.is_absolute():
        raise ValueError('campaign evidence path must be absolute')
    if path.name in {'', '.', '..'}:
        raise ValueError('campaign evidence filename is invalid')
    parent = path.parent
    _reject_symlink_components(parent)
    created = False
    try:
        metadata = os.lstat(parent)
    except FileNotFoundError:
        parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(parent, 0o700)
        created = True
        metadata = os.lstat(parent)
    _reject_symlink_components(parent)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError('campaign evidence parent must be a directory')
    if metadata.st_uid != os.getuid():
        raise PermissionError(
            'campaign evidence parent must be owned by this user'
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        if created:
            raise RuntimeError('new campaign evidence parent is not private')
        raise PermissionError('campaign evidence parent mode must be 0700')
    return parent


def write_campaign_manifest(
    path: Path,
    manifest: TextGazeboCampaignManifest,
) -> str:
    """Atomically publish one owner-only campaign without overwriting."""
    if not isinstance(manifest, TextGazeboCampaignManifest):
        raise TypeError(
            'manifest must be a TextGazeboCampaignManifest'
        )
    destination = path
    parent = _private_parent(destination)
    expected_parent = os.lstat(parent)
    directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    directory_flags |= getattr(os, 'O_NOFOLLOW', 0)
    directory_descriptor = os.open(parent, directory_flags)
    temporary_name: Optional[str] = None
    temporary_descriptor = -1
    try:
        parent_metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid()
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or (parent_metadata.st_dev, parent_metadata.st_ino)
            != (expected_parent.st_dev, expected_parent.st_ino)
        ):
            raise PermissionError(
                'campaign evidence parent changed during write'
            )
        try:
            existing = os.stat(
                destination.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise ValueError(
                    'campaign evidence destination cannot be a symbolic link'
                )
            raise FileExistsError(
                'campaign evidence destination already exists'
            )

        temporary_name = (
            f'.{destination.name}.{secrets.token_hex(16)}.tmp'
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        temporary_descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(temporary_descriptor, 0o600)
        payload = (manifest.canonical_json() + '\n').encode('utf-8')
        with os.fdopen(temporary_descriptor, 'wb') as stream:
            temporary_descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            try:
                existing = os.stat(
                    destination.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISLNK(existing.st_mode):
                raise ValueError(
                    'campaign evidence destination cannot be a symbolic link'
                ) from None
            raise FileExistsError(
                'campaign evidence destination already exists'
            ) from None
        os.fsync(directory_descriptor)
        result_descriptor = os.open(
            destination.name,
            os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0),
            dir_fd=directory_descriptor,
        )
        try:
            result = os.fstat(result_descriptor)
        finally:
            os.close(result_descriptor)
        if (
            not stat.S_ISREG(result.st_mode)
            or result.st_uid != os.getuid()
            or stat.S_IMODE(result.st_mode) != 0o600
        ):
            raise RuntimeError(
                'published campaign evidence permissions are invalid'
            )
        path_parent = os.lstat(parent)
        path_result = os.lstat(destination)
        if (
            (path_parent.st_dev, path_parent.st_ino)
            != (parent_metadata.st_dev, parent_metadata.st_ino)
            or (path_result.st_dev, path_result.st_ino)
            != (result.st_dev, result.st_ino)
        ):
            raise RuntimeError(
                'campaign evidence path changed during publish'
            )
        return manifest.digest()
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        os.close(directory_descriptor)
