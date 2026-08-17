#!/usr/bin/env python3
"""Repeat representative SWM25-69~74 offline checks and save evidence."""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Sequence, Tuple


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parent


TestReference = Tuple[str, str]


STORY_TESTS: Dict[str, Dict[str, Any]] = {
    'SWM25-69': {
        'title': '대화·에이전트 안전 계약 경계',
        'scope': 'schema, Tool allowlist, current-turn intent, safety',
        'tests': [
            ('test_agent_contract.py',
             'test_request_rejects_unknown_or_non_finite_state'),
            ('test_agent_contract.py',
             'test_tool_allowlist_contains_no_low_level_motion_control'),
            ('test_agent_contract.py',
             'test_selected_tool_schemas_are_strict_and_ordered'),
            ('test_agent_contract.py',
             'test_model_proposal_never_executes_with_untrusted_state'),
            ('test_agent_contract.py',
             'test_navigation_requires_current_turn_destination_intent'),
            ('test_agent_contract.py',
             'test_camera_tool_respects_privacy_mode'),
        ],
    },
    'SWM25-70': {
        'title': '멀티턴 대화 세션',
        'scope': 'lifecycle, order, isolation, reset, expiry, concurrency',
        'tests': [
            ('test_conversation.py',
             'test_create_get_close_delete_lifecycle_and_ordered_messages'),
            ('test_conversation.py',
             'test_history_keeps_latest_ten_completed_turns_in_order'),
            ('test_conversation.py',
             'test_sessions_are_isolated_by_user_and_new_session_is_empty'),
            (
                'test_conversation.py',
                'test_reset_starts_new_generation_'
                'without_old_short_term_context',
            ),
            ('test_conversation.py',
             'test_idle_expiry_is_exact_and_reads_do_not_extend_it'),
            ('test_conversation.py',
             'test_concurrent_reservation_allows_only_one_in_flight_turn'),
        ],
    },
    'SWM25-71': {
        'title': '사용자 컨텍스트 통합',
        'scope': 'history window, summary, memory isolation, injection bounds',
        'tests': [
            ('test_context_window.py',
             'test_latest_ten_raw_turns_and_summary_have_no_gap_or_overlap'),
            ('test_context_window.py',
             'test_hard_cap_and_content_free_metrics_cover_all_sources'),
            ('test_context_window.py',
             'test_untrusted_sources_remain_separate_json_data'),
            ('test_context_window.py',
             'test_malicious_history_cannot_authorize_benign_current_turn'),
            ('test_memory.py',
             'test_korean_memory_retrieval_and_user_isolation'),
            ('test_memory.py', 'test_expired_memory_is_not_returned'),
        ],
    },
    'SWM25-72': {
        'title': 'LLM provider 연결',
        'scope': 'strict payload/parser, credential redaction, retry/fallback',
        'tests': [
            ('test_openai_provider.py',
             'test_builds_strict_responses_payload_and_parses_tool_call'),
            ('test_openai_provider.py',
             'test_parses_structured_text_message'),
            ('test_openai_provider.py',
             'test_provider_repr_and_validation_never_expose_key'),
            ('test_openai_provider.py',
             'test_response_requires_explicit_completed_status'),
            ('test_reliable_provider.py',
             'test_retries_only_transient_failures_with_bounded_backoff'),
            (
                'test_reliable_provider.py',
                'test_authentication_failure_skips_retry_'
                'and_same_vendor_fallback',
            ),
            ('test_reliable_provider.py',
             'test_all_failures_return_safe_non_action_without_error_details'),
            ('test_runtime.py',
             'test_factory_builds_openai_primary_and_optional_model_fallback'),
        ],
    },
    'SWM25-73': {
        'title': 'Agent Tool Gateway',
        'scope': 'registry, idempotency, concurrency, blocking, simulation',
        'tests': [
            ('test_gateway.py',
             'test_registry_is_authoritative_and_rejects_unsafe_bindings'),
            ('test_gateway.py',
             'test_read_only_query_is_validated_and_idempotent'),
            ('test_gateway.py',
             'test_concurrent_duplicate_query_calls_adapter_once'),
            ('test_gateway.py',
             'test_side_effects_and_unknown_tools_fail_closed'),
            ('test_gateway.py',
             'test_simulation_is_explicit_and_has_no_real_sink'),
            ('test_gateway.py',
             'test_timeout_stale_state_and_bad_results_are_normalized'),
        ],
    },
    'SWM25-74': {
        'title': '실행 확인·결과 피드백 경계',
        'scope': 'negative evidence only: physical execution remains absent',
        'tests': [
            ('test_orchestrator.py',
             'test_policy_approval_is_not_gateway_execution_authority'),
            ('test_gateway.py',
             'test_side_effects_and_unknown_tools_fail_closed'),
            ('__stress__', '_swm25_74_rejects_fake_confirmation'),
        ],
        'negative_evidence_only': True,
    },
}


def _load_module(filename: str) -> ModuleType:
    """Load one existing pytest module without running pytest collection."""
    path = PACKAGE_ROOT / 'test' / filename
    module_name = f'_malbut_stress_{path.stem}'
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load test module: {filename}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _swm25_74_rejects_fake_confirmation() -> None:
    """Prove the current query contract cannot smuggle confirmation."""
    from malbut_agent_server.gateway import ToolQuery, production_registry
    from malbut_agent_server.schemas import ValidationError

    value = {
        'request_id': 'fake-confirmation',
        'user_id': 'stress-user',
        'tool_name': 'navigate',
        'arguments': {'location': '거실'},
        'confirmation': {'confirmed': True},
    }
    try:
        ToolQuery.from_dict(value)
    except ValidationError:
        pass
    else:
        raise AssertionError('fake confirmation unexpectedly crossed schema')

    snapshot = production_registry().to_dict()
    if any(item['executable'] for item in snapshot['capabilities']):
        raise AssertionError('production registry exposed an executable Tool')


def _resolve_tests() -> Dict[str, List[Tuple[str, Callable[[], None]]]]:
    """Resolve configured zero-fixture test functions once."""
    modules: Dict[str, ModuleType] = {}
    resolved: Dict[str, List[Tuple[str, Callable[[], None]]]] = {}
    for story_id, story in STORY_TESTS.items():
        story_tests = []
        for filename, function_name in story['tests']:
            if filename == '__stress__':
                function = globals()[function_name]
            else:
                if filename not in modules:
                    modules[filename] = _load_module(filename)
                function = getattr(modules[filename], function_name)
            story_tests.append(
                (f'{filename}::{function_name}', function)
            )
        resolved[story_id] = story_tests
    return resolved


def _git_value(*arguments: str) -> str:
    """Read one content-free Git identifier."""
    result = subprocess.run(
        ['git', *arguments],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _percentile(values: Sequence[float], quantile: float) -> float:
    """Return a nearest-rank percentile for non-empty values."""
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


def _run_story(
    story_id: str,
    tests: Sequence[Tuple[str, Callable[[], None]]],
    iterations: int,
    progress_every: int,
) -> Dict[str, Any]:
    """Run every representative subcheck once per story iteration."""
    records = []
    failures = []
    subchecks = {
        test_name: {
            'attempted': 0,
            'passed': 0,
            'failed': 0,
            '_durations_ms': [],
        }
        for test_name, _function in tests
    }
    story_started = time.perf_counter()
    for iteration in range(1, iterations + 1):
        iteration_started = time.perf_counter()
        passed = True
        for test_name, function in tests:
            check_started = time.perf_counter()
            subchecks[test_name]['attempted'] += 1
            try:
                function()
                subchecks[test_name]['passed'] += 1
            except BaseException as error:  # evidence must include any failure
                passed = False
                subchecks[test_name]['failed'] += 1
                formatted = ''.join(
                    traceback.format_exception(
                        type(error),
                        error,
                        error.__traceback__,
                    )
                )
                failures.append(
                    {
                        'iteration': iteration,
                        'test': test_name,
                        'error_type': type(error).__name__,
                        'message': str(error)[:500],
                        'traceback_sha256': hashlib.sha256(
                            formatted.encode('utf-8')
                        ).hexdigest(),
                    }
                )
            finally:
                subchecks[test_name]['_durations_ms'].append(
                    (time.perf_counter() - check_started) * 1000.0
                )
        duration_ms = (
            time.perf_counter() - iteration_started
        ) * 1000.0
        records.append(
            {
                'iteration': iteration,
                'passed': passed,
                'duration_ms': round(duration_ms, 3),
            }
        )
        if (
            iteration == 1
            or iteration == iterations
            or iteration % progress_every == 0
        ):
            print(
                f'{story_id}: {iteration}/{iterations} '
                f'iterations, failures={len(failures)}',
                flush=True,
            )

    durations = [record['duration_ms'] for record in records]
    passed_count = sum(record['passed'] for record in records)
    metadata = STORY_TESTS[story_id]
    subcheck_results = []
    for test_name, counters in subchecks.items():
        check_durations = counters.pop('_durations_ms')
        subcheck_results.append(
            {
                'test': test_name,
                **counters,
                'pass_rate': round(
                    counters['passed'] / counters['attempted'],
                    6,
                ),
                'duration_ms': {
                    'mean': round(statistics.mean(check_durations), 3),
                    'p95': round(
                        _percentile(check_durations, 0.95),
                        3,
                    ),
                    'max': round(max(check_durations), 3),
                },
            }
        )
    return {
        'title': metadata['title'],
        'scope': metadata['scope'],
        'negative_evidence_only': bool(
            metadata.get('negative_evidence_only', False)
        ),
        'selected_tests': [name for name, _function in tests],
        'subchecks': subcheck_results,
        'subchecks_per_iteration': len(tests),
        'subchecks_attempted': len(tests) * iterations,
        'iterations_attempted': iterations,
        'iterations_passed': passed_count,
        'iterations_failed': iterations - passed_count,
        'pass_rate': round(passed_count / iterations, 6),
        'duration_ms': {
            'total': round(
                (time.perf_counter() - story_started) * 1000.0,
                3,
            ),
            'mean': round(statistics.mean(durations), 3),
            'p50': round(_percentile(durations, 0.50), 3),
            'p95': round(_percentile(durations, 0.95), 3),
            'max': round(max(durations), 3),
        },
        'iterations': records,
        'failures': failures,
    }


def _write_private_json(path: Path, value: Dict[str, Any]) -> None:
    """Atomically write a mode-0600 JSON report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.',
        dir=str(path.parent),
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Run offline SWM25-69~74 representative stress checks.',
    )
    parser.add_argument('--iterations', type=int, default=300)
    parser.add_argument('--progress-every', type=int, default=25)
    parser.add_argument('--output', type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Run the bounded offline stress matrix and persist its evidence."""
    args = _parse_args()
    if args.iterations < 1 or args.iterations > 10000:
        raise SystemExit('--iterations must be between 1 and 10000')
    if args.progress_every < 1:
        raise SystemExit('--progress-every must be positive')

    sys.path.insert(0, str(PACKAGE_ROOT))
    resolved = _resolve_tests()
    started = time.perf_counter()
    stories = {
        story_id: _run_story(
            story_id,
            resolved[story_id],
            args.iterations,
            args.progress_every,
        )
        for story_id in STORY_TESTS
    }
    total_iterations = sum(
        story['iterations_attempted'] for story in stories.values()
    )
    total_passed = sum(
        story['iterations_passed'] for story in stories.values()
    )
    report = {
        'schema_version': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'offline_non_actuating',
        'source': {
            'head': _git_value('rev-parse', 'HEAD'),
            'origin_main': _git_value('rev-parse', 'origin/main'),
            'head_agent_tree': _git_value(
                'rev-parse', 'HEAD:malbut_agent_server'
            ),
            'origin_main_agent_tree': _git_value(
                'rev-parse', 'origin/main:malbut_agent_server'
            ),
            'harness_sha256': hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
            'python': platform.python_version(),
            'platform': platform.platform(),
        },
        'configuration': {
            'iterations_per_story': args.iterations,
            'story_count': len(stories),
            'live_openai_calls': False,
            'physical_ros_actions': False,
            'external_notifications': False,
            'application_files_or_images_created': False,
        },
        'summary': {
            'total_story_iterations': total_iterations,
            'passed_story_iterations': total_passed,
            'failed_story_iterations': total_iterations - total_passed,
            'total_subchecks': sum(
                story['subchecks_attempted']
                for story in stories.values()
            ),
            'wall_duration_ms': round(
                (time.perf_counter() - started) * 1000.0,
                3,
            ),
        },
        'interpretation': {
            'SWM25-74': (
                'Negative evidence only. Passing proves that the current '
                'runtime still blocks physical execution; it does not prove '
                'confirmation, execution, or feedback implementation.'
            ),
        },
        'stories': stories,
    }
    _write_private_json(args.output.resolve(), report)
    print(f'report: {args.output.resolve()}', flush=True)
    return 0 if total_iterations == total_passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
