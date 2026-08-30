"""Unit tests for the pure text-to-Gazebo campaign rules."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from malbut_scenarios.text_gazebo_campaign_core import (
    MAX_CAMPAIGN_CASES,
    CampaignCase,
    CampaignCaseId,
    CampaignCaseResult,
    CampaignProfile,
    CampaignProvenance,
    CampaignResult,
    CampaignVerdict,
    CaseErrorCode,
    CaseExecution,
    CaseExecutionStatus,
    CaseVerdict,
    CleanupOutcome,
    ExpectedProductOutcome,
    ObservedProductOutcome,
    TextGazeboCampaignError,
    campaign_profile_binding,
    run_campaign,
)
from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboFaultProfile,
    TextGazeboScenarioProfile,
)


_COMMIT = '1' * 40
_SOURCE_DIGEST = '2' * 64
_INSTALLED_DIGEST = '3' * 64
_EVIDENCE_DIGEST = '4' * 64


@pytest.mark.parametrize(
    'profile,fault',
    (
        (
            CampaignProfile.DUPLICATE_REQUEST,
            TextGazeboFaultProfile.DUPLICATE_REQUEST,
        ),
        (
            CampaignProfile.CONCURRENT_APPROVAL,
            TextGazeboFaultProfile.CONCURRENT_APPROVAL,
        ),
        (
            CampaignProfile.COMPETING_WORKERS,
            TextGazeboFaultProfile.COMPETING_WORKERS,
        ),
    ),
)
def test_exactly_once_case_tokens_keep_semantics_separate_from_faults(
    profile,
    fault,
) -> None:
    binding = campaign_profile_binding(profile)

    assert binding.scenario_profile is (
        TextGazeboScenarioProfile.HAPPY_LIVING_ROOM
    )
    assert binding.fault_profile is fault


def _provenance() -> CampaignProvenance:
    return CampaignProvenance(
        commit=_COMMIT,
        source_tree_digest=_SOURCE_DIGEST,
        installed_digest=_INSTALLED_DIGEST,
    )


def _case(
    suffix: str,
    expected: ExpectedProductOutcome = ExpectedProductOutcome.SUCCEEDED,
    profile: CampaignProfile = CampaignProfile.HAPPY_PATH,
) -> CampaignCase:
    return CampaignCase(
        case_id=CampaignCaseId(f'happy-{suffix}'),
        profile=profile,
        expected_outcome=expected,
    )


def _execution(
    *,
    observed: ObservedProductOutcome = ObservedProductOutcome.SUCCEEDED,
    status: CaseExecutionStatus = CaseExecutionStatus.COMPLETED,
    cleanup: CleanupOutcome = CleanupOutcome.CLEAN,
    provenance: CampaignProvenance | None = None,
) -> CaseExecution:
    return CaseExecution(
        status=status,
        observed_outcome=observed,
        cleanup=cleanup,
        provenance=provenance or _provenance(),
        evidence_digest=_EVIDENCE_DIGEST,
    )


class _FakeExecutor:
    """Return scripted executions while recording deterministic order."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[CampaignCase, CampaignProvenance]] = []

    def execute(
        self,
        case: CampaignCase,
        provenance: CampaignProvenance,
    ) -> CaseExecution:
        """Record one call and return or raise the next scripted value."""
        self.calls.append((case, provenance))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


def test_happy_cases_run_sequentially_and_pass() -> None:
    """All clean matching cases run in order and produce one overall pass."""
    cases = [
        _case('living', profile=CampaignProfile.HAPPY_LIVING_ROOM),
        _case('kitchen', profile=CampaignProfile.HAPPY_KITCHEN),
        _case('bedroom', profile=CampaignProfile.HAPPY_BEDROOM),
    ]
    provenance = _provenance()
    executor = _FakeExecutor([_execution(), _execution(), _execution()])

    result = run_campaign(cases, provenance, executor)

    assert [call[0] for call in executor.calls] == cases
    assert all(call[1] is provenance for call in executor.calls)
    assert result.verdict is CampaignVerdict.PASSED
    assert result.stopped_early is False
    assert [item.verdict for item in result.cases] == [
        CaseVerdict.PASSED,
        CaseVerdict.PASSED,
        CaseVerdict.PASSED,
    ]


@pytest.mark.parametrize('cases', [[], ()])
def test_empty_case_collections_are_rejected(cases: object) -> None:
    """An empty campaign never reaches an executor."""
    executor = _FakeExecutor([])

    with pytest.raises(
        TextGazeboCampaignError,
        match='^campaign_cases_empty$',
    ) as raised:
        run_campaign(cases, _provenance(), executor)  # type: ignore[arg-type]

    assert raised.value.code == 'campaign_cases_empty'
    assert executor.calls == []


@pytest.mark.parametrize('cases', ['', b'', None, 1])
def test_invalid_case_collections_are_rejected(cases: object) -> None:
    """Scalar and text-shaped collections fail with one stable code."""
    executor = _FakeExecutor([])

    with pytest.raises(
        TextGazeboCampaignError,
        match='^campaign_cases_invalid$',
    ) as raised:
        run_campaign(cases, _provenance(), executor)  # type: ignore[arg-type]

    assert raised.value.code == 'campaign_cases_invalid'
    assert executor.calls == []


def test_excessive_case_count_is_rejected_before_execution() -> None:
    """A bounded campaign cannot be expanded past its public limit."""
    cases = [_case(f'case-{index}') for index in range(MAX_CAMPAIGN_CASES + 1)]
    executor = _FakeExecutor([])

    with pytest.raises(
        TextGazeboCampaignError,
        match='^campaign_cases_excessive$',
    ):
        run_campaign(cases, _provenance(), executor)

    assert executor.calls == []


def test_case_iteration_is_bounded_at_the_campaign_limit() -> None:
    """An unbounded source is sampled only far enough to prove excess."""
    reads = []

    def unbounded_cases():
        while True:
            reads.append(len(reads))
            yield _case(f'case-{len(reads)}')

    with pytest.raises(
        TextGazeboCampaignError,
        match='^campaign_cases_excessive$',
    ):
        run_campaign(unbounded_cases(), _provenance(), _FakeExecutor([]))

    assert len(reads) == MAX_CAMPAIGN_CASES + 1


def test_duplicate_case_id_is_rejected_before_execution() -> None:
    """Two cases cannot write results under the same campaign identity."""
    duplicate = _case('duplicate')
    executor = _FakeExecutor([])

    with pytest.raises(
        TextGazeboCampaignError,
        match='^campaign_case_duplicate$',
    ):
        run_campaign([duplicate, duplicate], _provenance(), executor)

    assert executor.calls == []


@pytest.mark.parametrize(
    'value',
    [
        '',
        'Uppercase',
        'space value',
        '../escape',
        '/absolute',
        'two..dots',
        'trailing-',
        'a' * 49,
    ],
)
def test_case_id_rejects_unsafe_shapes(value: str) -> None:
    """Case identifiers cannot carry paths, whitespace, or unbounded data."""
    with pytest.raises(
        ValueError,
        match='^case_id must use the public identifier format$',
    ):
        CampaignCaseId(value)


def test_case_requires_allowlisted_profile_enum() -> None:
    """A raw profile string cannot bypass the current profile allowlist."""
    with pytest.raises(
        TypeError,
        match='^profile must be a CampaignProfile$',
    ):
        CampaignCase(
            case_id=CampaignCaseId('happy-one'),
            profile='happy_path',  # type: ignore[arg-type]
            expected_outcome=ExpectedProductOutcome.SUCCEEDED,
        )


@pytest.mark.parametrize(
    ('field', 'value', 'message'),
    [
        ('commit', '1' * 39, 'commit must be a full lowercase Git object id'),
        ('commit', 'G' * 40, 'commit must be a full lowercase Git object id'),
        (
            'source_tree_digest',
            'x' * 64,
            'source_tree_digest must be a lowercase SHA-256',
        ),
        (
            'installed_digest',
            '3' * 63,
            'installed_digest must be a lowercase SHA-256',
        ),
    ],
)
def test_provenance_rejects_noncanonical_values(
    field: str,
    value: str,
    message: str,
) -> None:
    """Provenance binding accepts only full canonical content IDs."""
    values = {
        'commit': _COMMIT,
        'source_tree_digest': _SOURCE_DIGEST,
        'installed_digest': _INSTALLED_DIGEST,
    }
    values[field] = value

    with pytest.raises(ValueError, match=f'^{message}$'):
        CampaignProvenance(**values)


def test_expected_block_is_a_product_success_for_that_test() -> None:
    """A blocked product outcome can correctly yield a passed test verdict."""
    case = _case('blocked', ExpectedProductOutcome.BLOCKED)
    executor = _FakeExecutor([
        _execution(observed=ObservedProductOutcome.BLOCKED),
    ])

    result = run_campaign([case], _provenance(), executor)

    assert result.verdict is CampaignVerdict.PASSED
    assert result.cases[0].expected_outcome is ExpectedProductOutcome.BLOCKED
    assert result.cases[0].observed_outcome is ObservedProductOutcome.BLOCKED
    assert result.cases[0].verdict is CaseVerdict.PASSED
    assert result.cases[0].as_dict()['product'] == {
        'expected': 'blocked',
        'observed': 'blocked',
    }


def test_product_mismatch_fails_test_but_clean_campaign_continues() -> None:
    """A clean mismatch fails overall without hiding later case evidence."""
    cases = [_case('one'), _case('two')]
    executor = _FakeExecutor([
        _execution(observed=ObservedProductOutcome.FAILED),
        _execution(),
    ])

    result = run_campaign(cases, _provenance(), executor)

    assert len(executor.calls) == 2
    assert result.verdict is CampaignVerdict.FAILED
    assert result.stopped_early is False
    assert result.cases[0].error_code is (
        CaseErrorCode.PRODUCT_OUTCOME_MISMATCH
    )
    assert result.cases[0].verdict is CaseVerdict.FAILED
    assert result.cases[1].verdict is CaseVerdict.PASSED


def test_executor_failure_cannot_pass_but_clean_run_continues() -> None:
    """Executor failure is distinct from an expected product failure."""
    cases = [_case('one'), _case('two')]
    executor = _FakeExecutor([
        _execution(status=CaseExecutionStatus.FAILED),
        _execution(),
    ])

    result = run_campaign(cases, _provenance(), executor)

    assert len(executor.calls) == 2
    assert result.verdict is CampaignVerdict.FAILED
    assert result.cases[0].error_code is CaseErrorCode.EXECUTOR_FAILED
    assert result.cases[1].verdict is CaseVerdict.PASSED


def test_cleanup_failure_stops_following_cases_fail_closed() -> None:
    """Unclean resources prohibit execution of every subsequent case."""
    cases = [_case('one'), _case('two'), _case('three')]
    executor = _FakeExecutor([
        _execution(cleanup=CleanupOutcome.INCOMPLETE),
        _execution(),
        _execution(),
    ])

    result = run_campaign(cases, _provenance(), executor)

    assert len(executor.calls) == 1
    assert result.verdict is CampaignVerdict.FAILED
    assert result.stopped_early is True
    assert result.cases[0].error_code is CaseErrorCode.CLEANUP_INCOMPLETE
    assert result.cases[0].verdict is CaseVerdict.FAILED
    assert [item.verdict for item in result.cases[1:]] == [
        CaseVerdict.NOT_RUN,
        CaseVerdict.NOT_RUN,
    ]
    assert all(
        item.error_code is CaseErrorCode.PREVIOUS_CASE_UNSAFE
        for item in result.cases[1:]
    )


def test_executor_exception_is_redacted_and_stops_safely() -> None:
    """Private exception content is discarded and later cases do not run."""
    private = '/private/recovery/token-secret'
    executor = _FakeExecutor([RuntimeError(private), _execution()])

    result = run_campaign(
        [_case('one'), _case('two')],
        _provenance(),
        executor,
    )

    assert len(executor.calls) == 1
    assert result.stopped_early is True
    assert result.cases[0].error_code is CaseErrorCode.EXECUTOR_EXCEPTION
    assert result.cases[0].cleanup is CleanupOutcome.NOT_OBSERVED
    assert private not in repr(result)
    assert private not in repr(result.cases[0])
    assert private not in str(result.as_dict())


def test_invalid_execution_result_fails_closed_and_stops() -> None:
    """An adapter returning an untyped value cannot authorize more cases."""
    executor = _FakeExecutor([{'private': 'value'}, _execution()])

    result = run_campaign(
        [_case('one'), _case('two')],
        _provenance(),
        executor,
    )

    assert len(executor.calls) == 1
    assert result.verdict is CampaignVerdict.FAILED
    assert result.stopped_early is True
    assert result.cases[0].error_code is (
        CaseErrorCode.EXECUTION_RESULT_INVALID
    )


def test_provenance_mismatch_fails_and_stops_before_mixing_runs() -> None:
    """One case from another build cannot enter or extend the campaign."""
    mismatched = replace(_provenance(), installed_digest='5' * 64)
    executor = _FakeExecutor([
        _execution(provenance=mismatched),
        _execution(),
    ])

    result = run_campaign(
        [_case('one'), _case('two')],
        _provenance(),
        executor,
    )

    assert len(executor.calls) == 1
    assert result.verdict is CampaignVerdict.FAILED
    assert result.stopped_early is True
    assert result.cases[0].error_code is CaseErrorCode.PROVENANCE_MISMATCH
    assert result.cases[1].verdict is CaseVerdict.NOT_RUN


def test_invalid_executor_is_rejected_with_stable_error() -> None:
    """An object without the executor port fails before any case starts."""
    with pytest.raises(
        TextGazeboCampaignError,
        match='^campaign_executor_invalid$',
    ) as raised:
        run_campaign([_case('one')], _provenance(), object())

    assert raised.value.code == 'campaign_executor_invalid'


def test_raw_case_values_fail_before_executor() -> None:
    """Only validated CampaignCase values can enter orchestration."""
    executor = _FakeExecutor([])

    with pytest.raises(
        TextGazeboCampaignError,
        match='^campaign_case_invalid$',
    ):
        run_campaign(
            [{'case_id': 'happy-one'}],  # type: ignore[list-item]
            _provenance(),
            executor,
        )

    assert executor.calls == []


def test_invalid_provenance_value_fails_with_stable_error() -> None:
    """The orchestrator accepts only a validated provenance aggregate."""
    executor = _FakeExecutor([])

    with pytest.raises(
        TextGazeboCampaignError,
        match='^campaign_provenance_invalid$',
    ):
        run_campaign(
            [_case('one')],
            {'commit': _COMMIT},  # type: ignore[arg-type]
            executor,
        )

    assert executor.calls == []


def test_result_repr_redacts_identifiers_and_digests() -> None:
    """Common diagnostic repr output cannot disclose supplied values."""
    case_id = CampaignCaseId('private-shaped-but-valid')
    case = CampaignCase(
        case_id=case_id,
        profile=CampaignProfile.HAPPY_PATH,
        expected_outcome=ExpectedProductOutcome.SUCCEEDED,
    )
    executor = _FakeExecutor([_execution()])

    result = run_campaign([case], _provenance(), executor)
    rendered = ' '.join((
        repr(case_id),
        repr(_provenance()),
        repr(case),
        repr(_execution()),
        repr(result.cases[0]),
        repr(result),
    ))

    for private_value in (
        case_id.value,
        _COMMIT,
        _SOURCE_DIGEST,
        _INSTALLED_DIGEST,
        _EVIDENCE_DIGEST,
    ):
        assert private_value not in rendered


def test_campaign_result_rejects_false_pass_claim() -> None:
    """A caller cannot construct PASS around a failed case result."""
    failed = CampaignCaseResult(
        case_id=CampaignCaseId('happy-one'),
        profile=CampaignProfile.HAPPY_PATH,
        expected_outcome=ExpectedProductOutcome.SUCCEEDED,
        observed_outcome=ObservedProductOutcome.FAILED,
        cleanup=CleanupOutcome.CLEAN,
        verdict=CaseVerdict.FAILED,
        error_code=CaseErrorCode.PRODUCT_OUTCOME_MISMATCH,
        evidence_digest=_EVIDENCE_DIGEST,
    )

    with pytest.raises(
        ValueError,
        match='^campaign verdict is inconsistent with cases$',
    ):
        CampaignResult(
            provenance=_provenance(),
            cases=(failed,),
            verdict=CampaignVerdict.PASSED,
            stopped_early=False,
        )


def test_case_result_rejects_inconsistent_pass_claim() -> None:
    """A passed test cannot disagree with its observed product outcome."""
    with pytest.raises(
        ValueError,
        match='^passed case result is inconsistent$',
    ):
        CampaignCaseResult(
            case_id=CampaignCaseId('happy-one'),
            profile=CampaignProfile.HAPPY_PATH,
            expected_outcome=ExpectedProductOutcome.SUCCEEDED,
            observed_outcome=ObservedProductOutcome.FAILED,
            cleanup=CleanupOutcome.CLEAN,
            verdict=CaseVerdict.PASSED,
            error_code=CaseErrorCode.NONE,
            evidence_digest=_EVIDENCE_DIGEST,
        )


def test_case_result_rejects_false_product_mismatch_claim() -> None:
    """A mismatch code cannot accompany equal product outcomes."""
    with pytest.raises(
        ValueError,
        match='^product mismatch result is inconsistent$',
    ):
        CampaignCaseResult(
            case_id=CampaignCaseId('happy-one'),
            profile=CampaignProfile.HAPPY_PATH,
            expected_outcome=ExpectedProductOutcome.SUCCEEDED,
            observed_outcome=ObservedProductOutcome.SUCCEEDED,
            cleanup=CleanupOutcome.CLEAN,
            verdict=CaseVerdict.FAILED,
            error_code=CaseErrorCode.PRODUCT_OUTCOME_MISMATCH,
            evidence_digest=_EVIDENCE_DIGEST,
        )


def test_campaign_result_rejects_not_run_without_early_stop() -> None:
    """NOT_RUN is valid only as the suffix of a stopped campaign."""
    executed = run_campaign(
        [_case('one')],
        _provenance(),
        _FakeExecutor([_execution()]),
    ).cases[0]
    skipped = CampaignCaseResult(
        case_id=CampaignCaseId('happy-two'),
        profile=CampaignProfile.HAPPY_PATH,
        expected_outcome=ExpectedProductOutcome.SUCCEEDED,
        observed_outcome=ObservedProductOutcome.NOT_OBSERVED,
        cleanup=CleanupOutcome.NOT_OBSERVED,
        verdict=CaseVerdict.NOT_RUN,
        error_code=CaseErrorCode.PREVIOUS_CASE_UNSAFE,
        evidence_digest=None,
    )

    with pytest.raises(
        ValueError,
        match='^non-stopped campaign cannot contain NOT_RUN$',
    ):
        CampaignResult(
            provenance=_provenance(),
            cases=(executed, skipped),
            verdict=CampaignVerdict.FAILED,
            stopped_early=False,
        )


def test_campaign_result_rejects_duplicate_case_ids() -> None:
    """A direct aggregate cannot bypass unique campaign case identity."""
    passed = run_campaign(
        [_case('one')],
        _provenance(),
        _FakeExecutor([_execution()]),
    ).cases[0]

    with pytest.raises(
        ValueError,
        match='^campaign result case IDs must be unique$',
    ):
        CampaignResult(
            provenance=_provenance(),
            cases=(passed, passed),
            verdict=CampaignVerdict.PASSED,
            stopped_early=False,
        )


def test_core_imports_no_ros_sqlite_or_process_modules() -> None:
    """Static imports preserve the pure core dependency boundary."""
    source = (
        Path(__file__).resolve().parents[1]
        / 'malbut_scenarios'
        / 'text_gazebo_campaign_core.py'
    ).read_text(encoding='utf-8')
    tree = ast.parse(source)
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split('.')[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split('.')[0])

    forbidden = {
        'launch',
        'rclpy',
        'ros2cli',
        'sqlite3',
        'subprocess',
    }
    assert imported_roots.isdisjoint(forbidden)


def test_unknown_public_error_is_normalized_without_details() -> None:
    """Unexpected error text cannot escape through the stable exception."""
    private = '/private/token-value'
    error = TextGazeboCampaignError(private)

    assert error.code == 'campaign_unexpected_failure'
    assert str(error) == 'campaign_unexpected_failure'
    assert private not in repr(error)
