"""Run an ordered campaign through the installed SWM25-133 boundary."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time
from typing import Callable, Optional, Sequence

from malbut_scenarios.text_gazebo_campaign_core import (
    MAX_CAMPAIGN_CASES,
    CampaignCase,
    CampaignCaseId,
    CampaignProfile,
    CampaignProvenance,
    CampaignResult,
    CampaignVerdict,
    CaseExecution,
    CaseExecutionStatus,
    CaseVerdict,
    CleanupOutcome,
    ExpectedProductOutcome,
    ObservedProductOutcome,
    TextGazeboCampaignError,
    run_campaign,
)
from malbut_scenarios.text_gazebo_campaign_evidence import (
    CampaignCaseEvidence,
    CampaignCleanupAggregate,
    CampaignTestVerdict,
    CaseCleanupState,
    CaseErrorCode,
    CaseTestVerdict,
    ProductOutcome,
    TextGazeboCampaignManifest,
    TextGazeboCampaignReceipt,
    write_campaign_manifest,
)
from malbut_scenarios.text_gazebo_campaign_runtime import (
    InstalledTextGazeboAcceptanceRunner,
    TextGazeboCampaignCheckConfig,
    TextGazeboCampaignRunRequest,
    TextGazeboCampaignRunResult,
    TextGazeboCampaignRunnerConfig,
    TextGazeboCampaignRuntimeError,
)


_FULL_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_PROFILE_OUTCOMES = {
    CampaignProfile.HAPPY_PATH: ExpectedProductOutcome.SUCCEEDED,
}


class TextGazeboCampaignCLIError(RuntimeError):
    """Expose one bounded CLI failure without retaining private values."""

    _CODES = frozenset({
        'campaign_arguments_invalid',
        'campaign_evidence_invalid',
        'campaign_evidence_publish_failed',
        'campaign_install_invalid',
        'campaign_source_invalid',
        'campaign_unexpected_failure',
    })

    def __init__(self, code: str) -> None:
        """Normalize every CLI failure to a content-free public code."""
        normalized = (
            code if code in self._CODES else 'campaign_unexpected_failure'
        )
        super().__init__(normalized)
        self.code = normalized


class _SafeArgumentParser(argparse.ArgumentParser):
    """Convert invalid input to one bounded error without printing usage."""

    def error(self, message: str) -> None:
        del message
        raise TextGazeboCampaignCLIError('campaign_arguments_invalid')


@dataclass(frozen=True, slots=True)
class _CaseObservation:
    """Private in-process binding between core and runtime case results."""

    result: Optional[TextGazeboCampaignRunResult]
    duration_seconds: float


class _InstalledCampaignExecutor:
    """Adapt the installed one-shot runner to the pure campaign port."""

    def __init__(
        self,
        runner: InstalledTextGazeboAcceptanceRunner,
        *,
        case_evidence_directory: Path,
        ros_domain_id: int,
        gui: bool,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._case_evidence_directory = case_evidence_directory
        self._ros_domain_id = ros_domain_id
        self._gui = gui
        self._monotonic = monotonic
        self._ordinals: dict[CampaignCaseId, int] = {}
        self.observations: dict[CampaignCaseId, _CaseObservation] = {}

    def bind_cases(self, cases: tuple[CampaignCase, ...]) -> None:
        """Bind deterministic child filenames to validated ordered cases."""
        self._ordinals = {
            case.case_id: ordinal
            for ordinal, case in enumerate(cases, start=1)
        }

    def execute(
        self,
        case: CampaignCase,
        provenance: CampaignProvenance,
    ) -> CaseExecution:
        """Run exactly one child without importing any execution subsystem."""
        ordinal = self._ordinals.get(case.case_id)
        if ordinal is None:
            raise TextGazeboCampaignCLIError(
                'campaign_arguments_invalid'
            )
        evidence_path = (
            self._case_evidence_directory / f'case-{ordinal:03d}.json'
        )
        started = self._monotonic()
        result: Optional[TextGazeboCampaignRunResult] = None
        try:
            candidate = self._runner.run(TextGazeboCampaignRunRequest(
                ros_domain_id=self._ros_domain_id,
                evidence_path=evidence_path,
                gui=self._gui,
            ))
            if not isinstance(candidate, TextGazeboCampaignRunResult):
                raise TextGazeboCampaignCLIError(
                    'campaign_unexpected_failure'
                )
            result = candidate
            return CaseExecution(
                status=CaseExecutionStatus.COMPLETED,
                observed_outcome=ObservedProductOutcome.SUCCEEDED,
                cleanup=CleanupOutcome.CLEAN,
                provenance=CampaignProvenance(
                    commit=result.commit,
                    source_tree_digest=result.source_tree_digest,
                    installed_digest=result.installed_digest,
                ),
                evidence_digest=result.manifest_digest,
            )
        finally:
            elapsed = max(0.0, self._monotonic() - started)
            if result is not None:
                elapsed = max(elapsed, result.elapsed_seconds)
            self.observations[case.case_id] = _CaseObservation(
                result=result,
                duration_seconds=elapsed,
            )


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description=(
            'Check or run an ordered installed text-to-Gazebo campaign. '
            'Execution is default-off and simulation-only.'
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        '--check',
        action='store_true',
        help='attest the installed SWM25-133 runner without starting Nav2',
    )
    mode.add_argument(
        '--run',
        action='store_true',
        help='run the ordered simulation campaign',
    )
    parser.add_argument(
        '--execute-approved-simulation',
        action='store_true',
        help='explicitly arm simulation execution; required with --run',
    )
    parser.add_argument(
        '--source-commit',
        required=True,
        help='full commit SHA expected in both source and install',
    )
    parser.add_argument(
        '--source-tree',
        required=True,
        type=Path,
        help='canonical absolute path to the clean source checkout',
    )
    parser.add_argument(
        '--evidence',
        type=Path,
        help='new absolute aggregate evidence path; required with --run',
    )
    parser.add_argument(
        '--ros-domain-id',
        type=int,
        help='isolated ROS domain from 1 through 100; required with --run',
    )
    parser.add_argument(
        '--gui',
        action='store_true',
        help='show the Gazebo GUI during an explicitly armed run',
    )
    parser.add_argument(
        '--case-profile',
        action='append',
        default=[],
        choices=tuple(profile.value for profile in CampaignProfile),
        help=(
            'append one allowlisted ordered case profile '
            f'(maximum {MAX_CAMPAIGN_CASES})'
        ),
    )
    return parser


def _canonical_directory(path: object, code: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TextGazeboCampaignCLIError(code)
    try:
        metadata = os.lstat(path)
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise TextGazeboCampaignCLIError(code) from None
    if (
        canonical != path
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        raise TextGazeboCampaignCLIError(code)
    return canonical


def _validate_arguments(args: argparse.Namespace) -> Path:
    if (
        not isinstance(args.source_commit, str)
        or _FULL_COMMIT.fullmatch(args.source_commit) is None
    ):
        raise TextGazeboCampaignCLIError('campaign_arguments_invalid')
    source_tree = _canonical_directory(
        args.source_tree,
        'campaign_source_invalid',
    )
    if (
        not args.case_profile
        or len(args.case_profile) > MAX_CAMPAIGN_CASES
    ):
        raise TextGazeboCampaignCLIError('campaign_arguments_invalid')
    if args.check:
        if (
            args.execute_approved_simulation
            or args.evidence is not None
            or args.ros_domain_id is not None
            or args.gui
        ):
            raise TextGazeboCampaignCLIError(
                'campaign_arguments_invalid'
            )
        return source_tree
    if (
        not args.execute_approved_simulation
        or not isinstance(args.evidence, Path)
        or not args.evidence.is_absolute()
        or args.evidence.name in {'', '.', '..'}
        or type(args.ros_domain_id) is not int
        or not 1 <= args.ros_domain_id <= 100
    ):
        raise TextGazeboCampaignCLIError('campaign_arguments_invalid')
    return source_tree


def _discover_installed_prefix() -> Path:
    """Resolve the installed package prefix selected by the ament index."""
    try:
        from ament_index_python.packages import get_package_prefix

        selected = Path(get_package_prefix('malbut_scenarios'))
    except (ImportError, LookupError, OSError, TypeError, ValueError):
        raise TextGazeboCampaignCLIError(
            'campaign_install_invalid'
        ) from None
    prefix = _canonical_directory(selected, 'campaign_install_invalid')
    try:
        Path(__file__).resolve(strict=True).relative_to(prefix)
    except (OSError, RuntimeError, ValueError):
        raise TextGazeboCampaignCLIError(
            'campaign_install_invalid'
        ) from None
    return prefix


def _cases(profile_values: Sequence[str]) -> tuple[CampaignCase, ...]:
    if not profile_values or len(profile_values) > MAX_CAMPAIGN_CASES:
        raise TextGazeboCampaignCLIError('campaign_arguments_invalid')
    cases = []
    try:
        for ordinal, value in enumerate(profile_values, start=1):
            profile = CampaignProfile(value)
            cases.append(CampaignCase(
                case_id=CampaignCaseId(f'case-{ordinal:03d}'),
                profile=profile,
                expected_outcome=_PROFILE_OUTCOMES[profile],
            ))
    except (KeyError, TypeError, ValueError):
        raise TextGazeboCampaignCLIError(
            'campaign_arguments_invalid'
        ) from None
    return tuple(cases)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError:
            raise TextGazeboCampaignCLIError(
                'campaign_evidence_invalid'
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise TextGazeboCampaignCLIError(
                'campaign_evidence_invalid'
            )


def _preflight_evidence(path: Path) -> Path:
    """Verify the operator-created aggregate evidence boundary."""
    try:
        _reject_symlink_components(path.parent)
        parent = path.parent.resolve(strict=True)
        metadata = os.lstat(path.parent)
        if (
            parent != path.parent
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OSError
        try:
            os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError
        return parent
    except TextGazeboCampaignCLIError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise TextGazeboCampaignCLIError(
            'campaign_evidence_invalid'
        ) from None


def _new_case_evidence_directory(parent: Path) -> Path:
    try:
        selected = Path(tempfile.mkdtemp(
            prefix='.swm25-134-cases-',
            dir=parent,
        ))
        os.chmod(selected, 0o700)
        canonical = selected.resolve(strict=True)
        metadata = os.lstat(selected)
        if (
            canonical != selected
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OSError
        return canonical
    except (OSError, RuntimeError, ValueError):
        raise TextGazeboCampaignCLIError(
            'campaign_evidence_invalid'
        ) from None


def _case_evidence(
    ordinal: int,
    result,
    observation: Optional[_CaseObservation],
) -> CampaignCaseEvidence:
    child = None if observation is None else observation.result
    summary = None if child is None else child.child_manifest
    if result.error_code.value == 'provenance_mismatch':
        summary = None
    duration = 0.0 if observation is None else observation.duration_seconds
    expected = ProductOutcome(result.expected_outcome.value)
    observed = ProductOutcome(result.observed_outcome.value)
    verdict = {
        CaseVerdict.PASSED: CaseTestVerdict.PASSED,
        CaseVerdict.FAILED: CaseTestVerdict.FAILED,
        CaseVerdict.NOT_RUN: CaseTestVerdict.PARTIAL,
    }[result.verdict]
    return CampaignCaseEvidence(
        ordinal=ordinal,
        case_id=result.case_id.value,
        profile=result.profile.value,
        expected_outcome=expected,
        observed_outcome=observed,
        test_verdict=verdict,
        error_code=CaseErrorCode(result.error_code.value),
        child_manifest=summary,
        duration_seconds=duration,
        cleanup=CaseCleanupState(result.cleanup.value),
    )


def _manifest(
    campaign: CampaignResult,
    executor: _InstalledCampaignExecutor,
    elapsed_seconds: float,
) -> TextGazeboCampaignManifest:
    case_evidence = tuple(
        _case_evidence(
            ordinal,
            result,
            executor.observations.get(result.case_id),
        )
        for ordinal, result in enumerate(campaign.cases, start=1)
    )
    clean = sum(
        item.cleanup is CaseCleanupState.CLEAN for item in case_evidence
    )
    incomplete = sum(
        item.cleanup is CaseCleanupState.INCOMPLETE for item in case_evidence
    )
    not_observed = sum(
        item.cleanup is CaseCleanupState.NOT_OBSERVED
        for item in case_evidence
    )
    child_summaries = tuple(
        item.child_manifest
        for item in case_evidence
        if item.child_manifest is not None
    )
    cleanup = CampaignCleanupAggregate(
        completed=(incomplete == 0 and not_observed == 0),
        clean_case_count=clean,
        incomplete_case_count=incomplete,
        not_observed_case_count=not_observed,
        owned_processes_remaining=sum(
            item.owned_processes_remaining for item in child_summaries
        ),
        ros_nodes_remaining=sum(
            item.ros_nodes_remaining for item in child_summaries
        ),
        owned_sockets_remaining=sum(
            item.owned_sockets_remaining for item in child_summaries
        ),
        forced_termination_count=sum(
            item.forced_termination_count for item in child_summaries
        ),
    )
    minimum_duration = sum(item.duration_seconds for item in case_evidence)
    if elapsed_seconds < minimum_duration:
        raise TextGazeboCampaignCLIError('campaign_evidence_invalid')
    receipt = TextGazeboCampaignReceipt(
        campaign_id='campaign-' + secrets.token_hex(16),
        commit=campaign.provenance.commit,
        source_tree_digest=campaign.provenance.source_tree_digest,
        installed_digest=campaign.provenance.installed_digest,
        cases=case_evidence,
        test_verdict=(
            CampaignTestVerdict.PASSED
            if campaign.verdict is CampaignVerdict.PASSED
            else CampaignTestVerdict.FAILED
        ),
        stopped_early=campaign.stopped_early,
        total_duration_seconds=elapsed_seconds,
        cleanup=cleanup,
    )
    return TextGazeboCampaignManifest(receipt)


def _check(
    *,
    installed_prefix: Path,
    source_tree: Path,
    source_commit: str,
) -> object:
    config = TextGazeboCampaignCheckConfig(
        installed_prefix=installed_prefix,
        source_tree=source_tree,
        source_commit=source_commit,
    )
    return InstalledTextGazeboAcceptanceRunner(config).check()


def _run(
    args: argparse.Namespace,
    *,
    installed_prefix: Path,
    source_tree: Path,
    cases: tuple[CampaignCase, ...],
) -> TextGazeboCampaignManifest:
    parent = _preflight_evidence(args.evidence)
    check = _check(
        installed_prefix=installed_prefix,
        source_tree=source_tree,
        source_commit=args.source_commit,
    )
    case_directory = _new_case_evidence_directory(parent)
    provenance = CampaignProvenance(
        commit=check.commit,
        source_tree_digest=check.source_tree_digest,
        installed_digest=check.installed_digest,
    )
    runner = InstalledTextGazeboAcceptanceRunner(
        TextGazeboCampaignRunnerConfig(
            installed_prefix=installed_prefix,
            source_tree=source_tree,
            source_commit=args.source_commit,
            source_tree_digest=check.source_tree_digest,
            installed_digest=check.installed_digest,
        )
    )
    executor = _InstalledCampaignExecutor(
        runner,
        case_evidence_directory=case_directory,
        ros_domain_id=args.ros_domain_id,
        gui=args.gui,
    )
    executor.bind_cases(cases)
    started = time.monotonic()
    campaign = run_campaign(cases, provenance, executor)
    manifest = _manifest(
        campaign,
        executor,
        max(0.0, time.monotonic() - started),
    )
    try:
        write_campaign_manifest(args.evidence, manifest)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise TextGazeboCampaignCLIError(
            'campaign_evidence_publish_failed'
        ) from None
    return manifest


def _safe_code(error: BaseException) -> str:
    if isinstance(
        error,
        (
            TextGazeboCampaignCLIError,
            TextGazeboCampaignError,
            TextGazeboCampaignRuntimeError,
        ),
    ):
        return error.code
    if isinstance(error, KeyboardInterrupt):
        return 'interrupted'
    return 'campaign_unexpected_failure'


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Check provenance or execute one explicitly armed simulation campaign."""
    try:
        args = _parser().parse_args(argv)
        source_tree = _validate_arguments(args)
        cases = _cases(args.case_profile)
        installed_prefix = _discover_installed_prefix()
        if args.check:
            result = _check(
                installed_prefix=installed_prefix,
                source_tree=source_tree,
                source_commit=args.source_commit,
            )
            print(json.dumps({
                'installed_digest': result.installed_digest,
                'case_count': len(cases),
                'mode': 'check',
                'nav2_start_count': result.nav2_start_count,
                'physical_authorized': False,
                'simulation': True,
                'source_tree_digest': result.source_tree_digest,
                'status': 'ok',
            }, ensure_ascii=True, sort_keys=True))
            return 0
        manifest = _run(
            args,
            installed_prefix=installed_prefix,
            source_tree=source_tree,
            cases=cases,
        )
        receipt = manifest.receipt
        response = {
            'case_count': len(receipt.cases),
            'manifest_digest': manifest.digest(),
            'mode': 'run',
            'physical_authorized': False,
            'simulation': True,
            'status': (
                'succeeded'
                if receipt.test_verdict is CampaignTestVerdict.PASSED
                else 'failed'
            ),
            'stopped_early': receipt.stopped_early,
            'test_verdict': receipt.test_verdict.value,
        }
        if receipt.test_verdict is CampaignTestVerdict.PASSED:
            print(json.dumps(response, ensure_ascii=True, sort_keys=True))
            return 0
        response['error_code'] = 'campaign_failed'
        print(
            json.dumps(response, ensure_ascii=True, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    except SystemExit as error:
        # argparse uses SystemExit(0) after printing a requested help page.
        # Invalid arguments are already normalized by _SafeArgumentParser.
        if error.code in (0, None):
            return 0
        print(json.dumps({
            'error_code': 'campaign_arguments_invalid',
            'status': 'failed',
        }, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 1
    except BaseException as error:  # CLI security boundary
        print(json.dumps({
            'error_code': _safe_code(error),
            'status': 'failed',
        }, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
