"""Pure orchestration rules for sequential text-to-Gazebo campaigns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Dict, Protocol, Sequence, Tuple

from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboExecutionProfile,
    TextGazeboFaultProfile,
    TextGazeboSafetyProfile,
    TextGazeboScenarioProfile,
)


MAX_CAMPAIGN_CASES = 32

_CASE_ID = re.compile(r'[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z')
_GIT_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')


class TextGazeboCampaignError(RuntimeError):
    """Expose one stable campaign error without caller-provided details."""

    _CODES = frozenset({
        'campaign_case_invalid',
        'campaign_cases_empty',
        'campaign_cases_excessive',
        'campaign_cases_invalid',
        'campaign_case_duplicate',
        'campaign_executor_invalid',
        'campaign_provenance_invalid',
        'campaign_unexpected_failure',
    })

    def __init__(self, code: str) -> None:
        """Normalize every public exception to a bounded error code."""
        normalized = (
            code if code in self._CODES else 'campaign_unexpected_failure'
        )
        super().__init__(normalized)
        self.code = normalized


class CampaignProfile(str, Enum):
    """Explicit allowlist of campaign case behavior profiles."""

    HAPPY_PATH = 'happy_path'
    HAPPY_LIVING_ROOM = 'happy_living_room'
    HAPPY_KITCHEN = 'happy_kitchen'
    HAPPY_BEDROOM = 'happy_bedroom'
    DUPLICATE_REQUEST = 'duplicate_request'
    CONCURRENT_APPROVAL = 'concurrent_approval'
    COMPETING_WORKERS = 'competing_workers'
    STALE_STATE = 'stale_state'
    EMERGENCY_STOP = 'emergency_stop'
    MAP_REVISION_CHANGED = 'map_revision_changed'
    NAV2_UNAVAILABLE = 'nav2_unavailable'
    START_RESPONSE_LOST = 'start_response_lost'
    TERMINAL_STATUS_RESPONSE_LOST = 'terminal_status_response_lost'


class ExpectedProductOutcome(str, Enum):
    """Product outcome a campaign case is designed to demonstrate."""

    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    BLOCKED = 'blocked'
    UNKNOWN = 'unknown'


class SafetyBlockCode(str, Enum):
    """Allowlisted dispatch-time block result, or no block at all."""

    NONE = 'none'
    ROBOT_STATE_STALE = 'robot_state_stale'
    SAFETY_EMERGENCY_STOP = 'safety_emergency_stop'
    TARGET_BINDING_CHANGED = 'target_binding_changed'


class UnknownResultCode(str, Enum):
    """Allowlisted outcome-unknown result, or no unknown result at all."""

    NONE = 'none'
    NAVIGATION_START_OUTCOME_UNKNOWN = 'navigation_start_outcome_unknown'
    NAVIGATION_STATUS_OUTCOME_UNKNOWN = 'navigation_status_outcome_unknown'


@dataclass(frozen=True, slots=True)
class CampaignProfileBinding:
    """Bind one public token to semantic, pressure, and safety behavior."""

    scenario_profile: TextGazeboScenarioProfile
    fault_profile: TextGazeboFaultProfile
    safety_profile: TextGazeboSafetyProfile = TextGazeboSafetyProfile.NONE
    expected_outcome: ExpectedProductOutcome = (
        ExpectedProductOutcome.SUCCEEDED
    )
    expected_block_code: SafetyBlockCode = SafetyBlockCode.NONE
    execution_profile: TextGazeboExecutionProfile = (
        TextGazeboExecutionProfile.NONE
    )
    expected_unknown_result_code: UnknownResultCode = UnknownResultCode.NONE


_PROFILE_BINDINGS = MappingProxyType({
    CampaignProfile.HAPPY_PATH: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_PATH,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.SUCCEEDED,
        SafetyBlockCode.NONE,
    ),
    CampaignProfile.HAPPY_LIVING_ROOM: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.SUCCEEDED,
        SafetyBlockCode.NONE,
    ),
    CampaignProfile.HAPPY_KITCHEN: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_KITCHEN,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.SUCCEEDED,
        SafetyBlockCode.NONE,
    ),
    CampaignProfile.HAPPY_BEDROOM: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_BEDROOM,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.SUCCEEDED,
        SafetyBlockCode.NONE,
    ),
    CampaignProfile.DUPLICATE_REQUEST: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.DUPLICATE_REQUEST,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.SUCCEEDED,
        SafetyBlockCode.NONE,
    ),
    CampaignProfile.CONCURRENT_APPROVAL: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.CONCURRENT_APPROVAL,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.SUCCEEDED,
        SafetyBlockCode.NONE,
    ),
    CampaignProfile.COMPETING_WORKERS: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.COMPETING_WORKERS,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.SUCCEEDED,
        SafetyBlockCode.NONE,
    ),
    CampaignProfile.STALE_STATE: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.STALE_STATE,
        ExpectedProductOutcome.BLOCKED,
        SafetyBlockCode.ROBOT_STATE_STALE,
    ),
    CampaignProfile.EMERGENCY_STOP: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.EMERGENCY_STOP,
        ExpectedProductOutcome.BLOCKED,
        SafetyBlockCode.SAFETY_EMERGENCY_STOP,
    ),
    CampaignProfile.MAP_REVISION_CHANGED: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.MAP_REVISION_CHANGED,
        ExpectedProductOutcome.BLOCKED,
        SafetyBlockCode.TARGET_BINDING_CHANGED,
    ),
    CampaignProfile.NAV2_UNAVAILABLE: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.UNKNOWN,
        SafetyBlockCode.NONE,
        TextGazeboExecutionProfile.NAV2_UNAVAILABLE,
        UnknownResultCode.NAVIGATION_START_OUTCOME_UNKNOWN,
    ),
    CampaignProfile.START_RESPONSE_LOST: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.UNKNOWN,
        SafetyBlockCode.NONE,
        TextGazeboExecutionProfile.START_RESPONSE_LOST,
        UnknownResultCode.NAVIGATION_START_OUTCOME_UNKNOWN,
    ),
    CampaignProfile.TERMINAL_STATUS_RESPONSE_LOST: CampaignProfileBinding(
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM,
        TextGazeboFaultProfile.NONE,
        TextGazeboSafetyProfile.NONE,
        ExpectedProductOutcome.UNKNOWN,
        SafetyBlockCode.NONE,
        TextGazeboExecutionProfile.TERMINAL_STATUS_RESPONSE_LOST,
        UnknownResultCode.NAVIGATION_STATUS_OUTCOME_UNKNOWN,
    ),
})


def campaign_profile_binding(
    profile: CampaignProfile,
) -> CampaignProfileBinding:
    """Return the immutable semantic/fault binding for a case token."""
    if not isinstance(profile, CampaignProfile):
        raise TypeError('profile must be a CampaignProfile')
    return _PROFILE_BINDINGS[profile]


class ObservedProductOutcome(str, Enum):
    """Product outcome observed from an installed case execution."""

    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    BLOCKED = 'blocked'
    UNKNOWN = 'unknown'
    NOT_OBSERVED = 'not_observed'


class CaseExecutionStatus(str, Enum):
    """Whether the case executor completed its observation contract."""

    COMPLETED = 'completed'
    FAILED = 'failed'


class CleanupOutcome(str, Enum):
    """Whether owned resources were proven clean after one case."""

    CLEAN = 'clean'
    INCOMPLETE = 'incomplete'
    NOT_OBSERVED = 'not_observed'


class CaseVerdict(str, Enum):
    """Test verdict kept separate from the product outcome."""

    PASSED = 'passed'
    FAILED = 'failed'
    NOT_RUN = 'not_run'


class CampaignVerdict(str, Enum):
    """Aggregate campaign verdict."""

    PASSED = 'passed'
    FAILED = 'failed'


class CaseErrorCode(str, Enum):
    """Stable, content-free reason for a non-passing case."""

    NONE = 'none'
    PRODUCT_OUTCOME_MISMATCH = 'product_outcome_mismatch'
    EXECUTOR_FAILED = 'executor_failed'
    EXECUTOR_EXCEPTION = 'executor_exception'
    EXECUTION_RESULT_INVALID = 'execution_result_invalid'
    CLEANUP_INCOMPLETE = 'cleanup_incomplete'
    PROVENANCE_MISMATCH = 'provenance_mismatch'
    PREVIOUS_CASE_UNSAFE = 'previous_case_unsafe'


def _require_enum(value: object, expected: type[Enum], name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f'{name} must be a {expected.__name__}')


def _require_digest(value: object, name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f'{name} must be a lowercase SHA-256')


@dataclass(frozen=True, repr=False, slots=True)
class CampaignCaseId:
    """Bounded public identifier that cannot contain paths or whitespace."""

    value: str

    def __post_init__(self) -> None:
        """Reject empty, oversized, or structurally unsafe identifiers."""
        if (
            not isinstance(self.value, str)
            or len(self.value) > 48
            or not _CASE_ID.fullmatch(self.value)
        ):
            raise ValueError('case_id must use the public identifier format')

    def __repr__(self) -> str:
        """Avoid rendering even a caller-selected public identifier."""
        return 'CampaignCaseId(redacted=True)'


@dataclass(frozen=True, repr=False, slots=True)
class CampaignProvenance:
    """Exact clean source and installed-artifact binding for a campaign."""

    commit: str
    source_tree_digest: str
    installed_digest: str

    def __post_init__(self) -> None:
        """Require a full object ID and canonical SHA-256 digests."""
        if (
            not isinstance(self.commit, str)
            or not _GIT_COMMIT.fullmatch(self.commit)
        ):
            raise ValueError('commit must be a full lowercase Git object id')
        _require_digest(self.source_tree_digest, 'source_tree_digest')
        _require_digest(self.installed_digest, 'installed_digest')

    def as_dict(self) -> Dict[str, str]:
        """Return the content-free provenance projection."""
        return {
            'commit': self.commit,
            'installed_digest': self.installed_digest,
            'source_tree_digest': self.source_tree_digest,
        }

    def __repr__(self) -> str:
        """Avoid expanding source or installation identifiers in logs."""
        return 'CampaignProvenance(bound=True)'


@dataclass(frozen=True, repr=False, slots=True)
class CampaignCase:
    """One allowlisted product expectation in a campaign."""

    case_id: CampaignCaseId
    profile: CampaignProfile
    expected_outcome: ExpectedProductOutcome
    expected_block_code: SafetyBlockCode = SafetyBlockCode.NONE
    expected_unknown_result_code: UnknownResultCode = UnknownResultCode.NONE

    def __post_init__(self) -> None:
        """Require strongly typed identifiers, profiles, and expectations."""
        if not isinstance(self.case_id, CampaignCaseId):
            raise TypeError('case_id must be a CampaignCaseId')
        _require_enum(self.profile, CampaignProfile, 'profile')
        _require_enum(
            self.expected_outcome,
            ExpectedProductOutcome,
            'expected_outcome',
        )
        _require_enum(
            self.expected_block_code,
            SafetyBlockCode,
            'expected_block_code',
        )
        _require_enum(
            self.expected_unknown_result_code,
            UnknownResultCode,
            'expected_unknown_result_code',
        )
        if (
            self.expected_outcome is not ExpectedProductOutcome.BLOCKED
            and self.expected_block_code is not SafetyBlockCode.NONE
        ):
            raise ValueError(
                'expected block code must match the product outcome'
            )
        if (
            self.expected_outcome is ExpectedProductOutcome.UNKNOWN
        ) is (
            self.expected_unknown_result_code is UnknownResultCode.NONE
        ):
            raise ValueError(
                'expected unknown code must match the product outcome'
            )

    def __repr__(self) -> str:
        """Show only allowlisted control values, never the case identifier."""
        return (
            'CampaignCase('
            f'profile={self.profile.value!r}, '
            f'expected_outcome={self.expected_outcome.value!r}, '
            f'expected_block_code={self.expected_block_code.value!r})'
        )


@dataclass(frozen=True, repr=False, slots=True)
class CaseExecution:
    """Content-free facts returned by one installed case executor."""

    status: CaseExecutionStatus
    observed_outcome: ObservedProductOutcome
    cleanup: CleanupOutcome
    provenance: CampaignProvenance
    evidence_digest: str
    observed_block_code: SafetyBlockCode = SafetyBlockCode.NONE
    observed_unknown_result_code: UnknownResultCode = UnknownResultCode.NONE

    def __post_init__(self) -> None:
        """Reject loosely typed or non-content-addressed execution facts."""
        _require_enum(self.status, CaseExecutionStatus, 'status')
        _require_enum(
            self.observed_outcome,
            ObservedProductOutcome,
            'observed_outcome',
        )
        _require_enum(self.cleanup, CleanupOutcome, 'cleanup')
        if not isinstance(self.provenance, CampaignProvenance):
            raise TypeError('provenance must be a CampaignProvenance')
        _require_digest(self.evidence_digest, 'evidence_digest')
        _require_enum(
            self.observed_block_code,
            SafetyBlockCode,
            'observed_block_code',
        )
        _require_enum(
            self.observed_unknown_result_code,
            UnknownResultCode,
            'observed_unknown_result_code',
        )
        if (
            self.observed_outcome is not ObservedProductOutcome.BLOCKED
            and self.observed_block_code is not SafetyBlockCode.NONE
        ):
            raise ValueError(
                'observed block code must match the product outcome'
            )
        if (
            self.observed_outcome is ObservedProductOutcome.UNKNOWN
        ) is (
            self.observed_unknown_result_code is UnknownResultCode.NONE
        ):
            raise ValueError(
                'observed unknown code must match the product outcome'
            )

    def __repr__(self) -> str:
        """Render only bounded states, not provenance or evidence values."""
        return (
            'CaseExecution('
            f'status={self.status.value!r}, '
            f'observed_outcome={self.observed_outcome.value!r}, '
            f'observed_block_code={self.observed_block_code.value!r}, '
            'observed_unknown_result_code='
            f'{self.observed_unknown_result_code.value!r}, '
            f'cleanup={self.cleanup.value!r})'
        )


class CampaignCaseExecutor(Protocol):
    """Port implemented by fake and installed SWM25-133 adapters."""

    def execute(
        self,
        case: CampaignCase,
        provenance: CampaignProvenance,
    ) -> CaseExecution:
        """Execute one case and return bounded, content-free facts."""
        ...


@dataclass(frozen=True, repr=False, slots=True)
class CampaignCaseResult:
    """Product expectation, observation, and test verdict for one case."""

    case_id: CampaignCaseId
    profile: CampaignProfile
    expected_outcome: ExpectedProductOutcome
    observed_outcome: ObservedProductOutcome
    cleanup: CleanupOutcome
    verdict: CaseVerdict
    error_code: CaseErrorCode
    evidence_digest: str | None
    expected_block_code: SafetyBlockCode = SafetyBlockCode.NONE
    observed_block_code: SafetyBlockCode = SafetyBlockCode.NONE
    expected_unknown_result_code: UnknownResultCode = UnknownResultCode.NONE
    observed_unknown_result_code: UnknownResultCode = UnknownResultCode.NONE

    def __post_init__(self) -> None:
        """Keep every result value bounded and structurally typed."""
        if not isinstance(self.case_id, CampaignCaseId):
            raise TypeError('case_id must be a CampaignCaseId')
        for name, expected in (
            ('profile', CampaignProfile),
            ('expected_outcome', ExpectedProductOutcome),
            ('observed_outcome', ObservedProductOutcome),
            ('cleanup', CleanupOutcome),
            ('verdict', CaseVerdict),
            ('error_code', CaseErrorCode),
            ('expected_block_code', SafetyBlockCode),
            ('observed_block_code', SafetyBlockCode),
            ('expected_unknown_result_code', UnknownResultCode),
            ('observed_unknown_result_code', UnknownResultCode),
        ):
            _require_enum(getattr(self, name), expected, name)
        if (
            self.expected_outcome is not ExpectedProductOutcome.BLOCKED
            and self.expected_block_code is not SafetyBlockCode.NONE
        ):
            raise ValueError(
                'expected block code must match the product outcome'
            )
        if (
            self.observed_outcome is not ObservedProductOutcome.BLOCKED
            and self.observed_block_code is not SafetyBlockCode.NONE
        ):
            raise ValueError(
                'observed block code must match the product outcome'
            )
        if (
            self.expected_outcome is ExpectedProductOutcome.UNKNOWN
        ) is (
            self.expected_unknown_result_code is UnknownResultCode.NONE
        ):
            raise ValueError(
                'expected unknown code must match the product outcome'
            )
        if (
            self.observed_outcome is ObservedProductOutcome.UNKNOWN
        ) is (
            self.observed_unknown_result_code is UnknownResultCode.NONE
        ):
            raise ValueError(
                'observed unknown code must match the product outcome'
            )
        if self.evidence_digest is not None:
            _require_digest(self.evidence_digest, 'evidence_digest')
        if self.verdict is CaseVerdict.PASSED:
            if (
                self.error_code is not CaseErrorCode.NONE
                or self.cleanup is not CleanupOutcome.CLEAN
                or self.evidence_digest is None
                or self.observed_outcome.value
                != self.expected_outcome.value
                or (
                    self.expected_block_code is not SafetyBlockCode.NONE
                    and self.observed_block_code
                    is not self.expected_block_code
                )
                or (
                    self.expected_unknown_result_code
                    is not UnknownResultCode.NONE
                    and self.observed_unknown_result_code
                    is not self.expected_unknown_result_code
                )
            ):
                raise ValueError('passed case result is inconsistent')
        elif self.verdict is CaseVerdict.NOT_RUN:
            if (
                self.error_code is not CaseErrorCode.PREVIOUS_CASE_UNSAFE
                or self.observed_outcome
                is not ObservedProductOutcome.NOT_OBSERVED
                or self.cleanup is not CleanupOutcome.NOT_OBSERVED
                or self.evidence_digest is not None
            ):
                raise ValueError('not-run case result is inconsistent')
        else:
            if self.error_code in {
                CaseErrorCode.NONE,
                CaseErrorCode.PREVIOUS_CASE_UNSAFE,
            }:
                raise ValueError(
                    'failed case result requires a failure error code'
                )
            if self.error_code is CaseErrorCode.PRODUCT_OUTCOME_MISMATCH:
                if (
                    self.cleanup is not CleanupOutcome.CLEAN
                    or self.evidence_digest is None
                    or self.observed_outcome
                    is ObservedProductOutcome.NOT_OBSERVED
                    or self.observed_outcome.value
                    == self.expected_outcome.value
                    and (
                        self.expected_block_code is SafetyBlockCode.NONE
                        or self.observed_block_code
                        is self.expected_block_code
                    )
                    and (
                        self.expected_unknown_result_code
                        is UnknownResultCode.NONE
                        or self.observed_unknown_result_code
                        is self.expected_unknown_result_code
                    )
                ):
                    raise ValueError(
                        'product mismatch result is inconsistent'
                    )
            elif self.error_code is CaseErrorCode.EXECUTOR_FAILED:
                if (
                    self.cleanup is not CleanupOutcome.CLEAN
                    or self.evidence_digest is None
                ):
                    raise ValueError(
                        'executor failure result is inconsistent'
                    )
            elif self.error_code in {
                CaseErrorCode.EXECUTOR_EXCEPTION,
                CaseErrorCode.EXECUTION_RESULT_INVALID,
            }:
                if (
                    self.observed_outcome
                    is not ObservedProductOutcome.NOT_OBSERVED
                    or self.cleanup is not CleanupOutcome.NOT_OBSERVED
                    or self.evidence_digest is not None
                ):
                    raise ValueError(
                        'unobserved failure result is inconsistent'
                    )
            elif self.error_code is CaseErrorCode.CLEANUP_INCOMPLETE:
                if self.cleanup is CleanupOutcome.CLEAN:
                    raise ValueError(
                        'cleanup failure result is inconsistent'
                    )
            elif self.error_code is CaseErrorCode.PROVENANCE_MISMATCH:
                if self.evidence_digest is None:
                    raise ValueError(
                        'provenance mismatch result is inconsistent'
                    )

    def as_dict(self) -> Dict[str, object]:
        """Return distinct product and test projections for aggregation."""
        return {
            'case_id': self.case_id.value,
            'cleanup': self.cleanup.value,
            'error_code': self.error_code.value,
            'evidence_digest': self.evidence_digest,
            'product': {
                'expected_block_code': self.expected_block_code.value,
                'expected': self.expected_outcome.value,
                'observed_block_code': self.observed_block_code.value,
                'expected_unknown_result_code': (
                    self.expected_unknown_result_code.value
                ),
                'observed_unknown_result_code': (
                    self.observed_unknown_result_code.value
                ),
                'observed': self.observed_outcome.value,
            },
            'profile': self.profile.value,
            'test_verdict': self.verdict.value,
        }

    def __repr__(self) -> str:
        """Render only bounded verdict states, never caller identifiers."""
        return (
            'CampaignCaseResult('
            f'verdict={self.verdict.value!r}, '
            f'error_code={self.error_code.value!r})'
        )


@dataclass(frozen=True, repr=False, slots=True)
class CampaignResult:
    """Aggregate result for a validated, ordered campaign."""

    provenance: CampaignProvenance
    cases: Tuple[CampaignCaseResult, ...]
    verdict: CampaignVerdict
    stopped_early: bool

    def __post_init__(self) -> None:
        """Reject aggregate verdicts inconsistent with their case results."""
        if not isinstance(self.provenance, CampaignProvenance):
            raise TypeError('provenance must be a CampaignProvenance')
        if not isinstance(self.cases, tuple) or not self.cases:
            raise ValueError('cases must be a non-empty tuple')
        if len(self.cases) > MAX_CAMPAIGN_CASES:
            raise ValueError('cases exceed the campaign limit')
        if any(
            not isinstance(item, CampaignCaseResult)
            for item in self.cases
        ):
            raise TypeError('cases must contain CampaignCaseResult values')
        case_ids = tuple(item.case_id for item in self.cases)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError('campaign result case IDs must be unique')
        _require_enum(self.verdict, CampaignVerdict, 'verdict')
        if type(self.stopped_early) is not bool:
            raise TypeError('stopped_early must be a bool')
        not_run = tuple(
            index
            for index, item in enumerate(self.cases)
            if item.verdict is CaseVerdict.NOT_RUN
        )
        if self.stopped_early:
            if (
                not not_run
                or not_run[0] == 0
                or not_run
                != tuple(range(not_run[0], len(self.cases)))
                or self.cases[not_run[0] - 1].verdict
                is not CaseVerdict.FAILED
            ):
                raise ValueError(
                    'stopped campaign result is inconsistent'
                )
        elif not_run:
            raise ValueError('non-stopped campaign cannot contain NOT_RUN')
        all_passed = all(
            item.verdict is CaseVerdict.PASSED for item in self.cases
        )
        expected_verdict = (
            CampaignVerdict.PASSED
            if all_passed and not self.stopped_early
            else CampaignVerdict.FAILED
        )
        if self.verdict is not expected_verdict:
            raise ValueError('campaign verdict is inconsistent with cases')

    def as_dict(self) -> Dict[str, object]:
        """Return the ordered, content-free aggregate projection."""
        return {
            'cases': [item.as_dict() for item in self.cases],
            'provenance': self.provenance.as_dict(),
            'stopped_early': self.stopped_early,
            'test_verdict': self.verdict.value,
        }

    def __repr__(self) -> str:
        """Render aggregate counts and verdict without case details."""
        return (
            'CampaignResult('
            f'case_count={len(self.cases)!r}, '
            f'verdict={self.verdict.value!r}, '
            f'stopped_early={self.stopped_early!r})'
        )


def _validate_cases(
    cases: Sequence[CampaignCase],
) -> Tuple[CampaignCase, ...]:
    if isinstance(cases, (str, bytes)):
        raise TextGazeboCampaignError('campaign_cases_invalid')
    try:
        iterator = iter(cases)
        collected = []
        for unused_index in range(MAX_CAMPAIGN_CASES + 1):
            del unused_index
            try:
                collected.append(next(iterator))
            except StopIteration:
                break
        normalized = tuple(collected)
    except Exception:
        raise TextGazeboCampaignError('campaign_cases_invalid') from None
    if not normalized:
        raise TextGazeboCampaignError('campaign_cases_empty')
    if len(normalized) > MAX_CAMPAIGN_CASES:
        raise TextGazeboCampaignError('campaign_cases_excessive')
    if any(not isinstance(item, CampaignCase) for item in normalized):
        raise TextGazeboCampaignError('campaign_case_invalid')
    case_ids = [item.case_id for item in normalized]
    if len(set(case_ids)) != len(case_ids):
        raise TextGazeboCampaignError('campaign_case_duplicate')
    return normalized


def _not_run(case: CampaignCase) -> CampaignCaseResult:
    return CampaignCaseResult(
        case_id=case.case_id,
        profile=case.profile,
        expected_outcome=case.expected_outcome,
        observed_outcome=ObservedProductOutcome.NOT_OBSERVED,
        cleanup=CleanupOutcome.NOT_OBSERVED,
        verdict=CaseVerdict.NOT_RUN,
        error_code=CaseErrorCode.PREVIOUS_CASE_UNSAFE,
        evidence_digest=None,
        expected_block_code=case.expected_block_code,
        observed_block_code=SafetyBlockCode.NONE,
        expected_unknown_result_code=case.expected_unknown_result_code,
        observed_unknown_result_code=UnknownResultCode.NONE,
    )


def _failed_execution(
    case: CampaignCase,
    error_code: CaseErrorCode,
) -> CampaignCaseResult:
    return CampaignCaseResult(
        case_id=case.case_id,
        profile=case.profile,
        expected_outcome=case.expected_outcome,
        observed_outcome=ObservedProductOutcome.NOT_OBSERVED,
        cleanup=CleanupOutcome.NOT_OBSERVED,
        verdict=CaseVerdict.FAILED,
        error_code=error_code,
        evidence_digest=None,
        expected_block_code=case.expected_block_code,
        observed_block_code=SafetyBlockCode.NONE,
        expected_unknown_result_code=case.expected_unknown_result_code,
        observed_unknown_result_code=UnknownResultCode.NONE,
    )


def _evaluate_execution(
    case: CampaignCase,
    execution: CaseExecution,
    provenance: CampaignProvenance,
) -> CampaignCaseResult:
    if execution.provenance != provenance:
        return CampaignCaseResult(
            case_id=case.case_id,
            profile=case.profile,
            expected_outcome=case.expected_outcome,
            observed_outcome=execution.observed_outcome,
            cleanup=execution.cleanup,
            verdict=CaseVerdict.FAILED,
            error_code=CaseErrorCode.PROVENANCE_MISMATCH,
            evidence_digest=execution.evidence_digest,
            expected_block_code=case.expected_block_code,
            observed_block_code=execution.observed_block_code,
            expected_unknown_result_code=case.expected_unknown_result_code,
            observed_unknown_result_code=(
                execution.observed_unknown_result_code
            ),
        )
    if execution.cleanup is not CleanupOutcome.CLEAN:
        return CampaignCaseResult(
            case_id=case.case_id,
            profile=case.profile,
            expected_outcome=case.expected_outcome,
            observed_outcome=execution.observed_outcome,
            cleanup=execution.cleanup,
            verdict=CaseVerdict.FAILED,
            error_code=CaseErrorCode.CLEANUP_INCOMPLETE,
            evidence_digest=execution.evidence_digest,
            expected_block_code=case.expected_block_code,
            observed_block_code=execution.observed_block_code,
            expected_unknown_result_code=case.expected_unknown_result_code,
            observed_unknown_result_code=(
                execution.observed_unknown_result_code
            ),
        )
    if execution.status is not CaseExecutionStatus.COMPLETED:
        return CampaignCaseResult(
            case_id=case.case_id,
            profile=case.profile,
            expected_outcome=case.expected_outcome,
            observed_outcome=execution.observed_outcome,
            cleanup=execution.cleanup,
            verdict=CaseVerdict.FAILED,
            error_code=CaseErrorCode.EXECUTOR_FAILED,
            evidence_digest=execution.evidence_digest,
            expected_block_code=case.expected_block_code,
            observed_block_code=execution.observed_block_code,
            expected_unknown_result_code=case.expected_unknown_result_code,
            observed_unknown_result_code=(
                execution.observed_unknown_result_code
            ),
        )
    expected = case.expected_outcome.value
    if (
        execution.observed_outcome.value != expected
        or (
            case.expected_block_code is not SafetyBlockCode.NONE
            and execution.observed_block_code
            is not case.expected_block_code
        )
        or (
            case.expected_unknown_result_code is not UnknownResultCode.NONE
            and execution.observed_unknown_result_code
            is not case.expected_unknown_result_code
        )
    ):
        return CampaignCaseResult(
            case_id=case.case_id,
            profile=case.profile,
            expected_outcome=case.expected_outcome,
            observed_outcome=execution.observed_outcome,
            cleanup=execution.cleanup,
            verdict=CaseVerdict.FAILED,
            error_code=CaseErrorCode.PRODUCT_OUTCOME_MISMATCH,
            evidence_digest=execution.evidence_digest,
            expected_block_code=case.expected_block_code,
            observed_block_code=execution.observed_block_code,
            expected_unknown_result_code=case.expected_unknown_result_code,
            observed_unknown_result_code=(
                execution.observed_unknown_result_code
            ),
        )
    return CampaignCaseResult(
        case_id=case.case_id,
        profile=case.profile,
        expected_outcome=case.expected_outcome,
        observed_outcome=execution.observed_outcome,
        cleanup=execution.cleanup,
        verdict=CaseVerdict.PASSED,
        error_code=CaseErrorCode.NONE,
        evidence_digest=execution.evidence_digest,
        expected_block_code=case.expected_block_code,
        observed_block_code=execution.observed_block_code,
        expected_unknown_result_code=case.expected_unknown_result_code,
        observed_unknown_result_code=execution.observed_unknown_result_code,
    )


def run_campaign(
    cases: Sequence[CampaignCase],
    provenance: CampaignProvenance,
    executor: CampaignCaseExecutor,
) -> CampaignResult:
    """Run validated cases sequentially and fail closed on unsafe residue."""
    normalized = _validate_cases(cases)
    if not isinstance(provenance, CampaignProvenance):
        raise TextGazeboCampaignError('campaign_provenance_invalid')
    try:
        execute = getattr(executor, 'execute', None)
    except Exception:
        raise TextGazeboCampaignError('campaign_executor_invalid') from None
    if not callable(execute):
        raise TextGazeboCampaignError('campaign_executor_invalid')

    results = []
    stopped_early = False
    for index, case in enumerate(normalized):
        try:
            execution = execute(case, provenance)
        except Exception:
            result = _failed_execution(
                case,
                CaseErrorCode.EXECUTOR_EXCEPTION,
            )
        else:
            if not isinstance(execution, CaseExecution):
                result = _failed_execution(
                    case,
                    CaseErrorCode.EXECUTION_RESULT_INVALID,
                )
            else:
                result = _evaluate_execution(case, execution, provenance)
        results.append(result)

        must_stop = (
            result.cleanup is not CleanupOutcome.CLEAN
            or result.error_code
            in {
                CaseErrorCode.EXECUTOR_EXCEPTION,
                CaseErrorCode.EXECUTION_RESULT_INVALID,
                CaseErrorCode.PROVENANCE_MISMATCH,
            }
        )
        if must_stop and index + 1 < len(normalized):
            stopped_early = True
            results.extend(_not_run(item) for item in normalized[index + 1:])
            break

    verdict = (
        CampaignVerdict.PASSED
        if all(item.verdict is CaseVerdict.PASSED for item in results)
        and not stopped_early
        else CampaignVerdict.FAILED
    )
    return CampaignResult(
        provenance=provenance,
        cases=tuple(results),
        verdict=verdict,
        stopped_early=stopped_early,
    )


__all__ = [
    'MAX_CAMPAIGN_CASES',
    'CampaignCase',
    'CampaignCaseExecutor',
    'CampaignCaseId',
    'CampaignCaseResult',
    'CampaignProfile',
    'CampaignProfileBinding',
    'CampaignProvenance',
    'CampaignResult',
    'CampaignVerdict',
    'CaseErrorCode',
    'CaseExecution',
    'CaseExecutionStatus',
    'CaseVerdict',
    'CleanupOutcome',
    'ExpectedProductOutcome',
    'ObservedProductOutcome',
    'SafetyBlockCode',
    'UnknownResultCode',
    'TextGazeboCampaignError',
    'campaign_profile_binding',
    'run_campaign',
]
