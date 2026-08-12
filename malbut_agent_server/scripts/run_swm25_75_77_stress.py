#!/usr/bin/env python3
"""Repeat representative SWM25-75~77 checks and save private evidence."""

import argparse
import functools
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

Check = Tuple[str, Callable[[], None]]


def _load_module(filename: str) -> ModuleType:
    path = PACKAGE_ROOT / 'test' / filename
    spec = importlib.util.spec_from_file_location(
        f'_malbut_75_77_stress_{path.stem}',
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load test module: {filename}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _temporary_check(
    function: Callable[[Path], None],
    label: str,
    scratch_root: Path,
) -> Check:
    def invoke() -> None:
        with tempfile.TemporaryDirectory(
            prefix='swm25-75-',
            dir=str(scratch_root),
        ) as directory:
            function(Path(directory))

    return label, invoke


def _case(
    function: Callable[..., None],
    label: str,
    *arguments: Any,
) -> Check:
    return label, functools.partial(function, *arguments)


def _resolve_checks(scratch_root: Path) -> Dict[str, List[Check]]:
    memory = _load_module('test_memory.py')
    speech = _load_module('test_speech_pipeline.py')
    expression = _load_module('test_expression.py')

    memory_checks = [
        (
            'search_user_isolation',
            memory.test_korean_memory_retrieval_and_user_isolation,
        ),
        (
            'confirmed_lifecycle_cas_evidence',
            memory.test_confirmed_mutation_lifecycle_uses_cas_and_evidence,
        ),
        (
            'confirmed_replay_after_expiry',
            getattr(
                memory,
                'test_confirmed_replay_survives_expiry_'
                'but_updates_stay_blocked',
            ),
        ),
        (
            'owner_snapshot_expiry',
            memory.test_owner_snapshot_ignores_other_users_and_detects_expiry,
        ),
        (
            'cross_user_mutation_isolation',
            memory.test_confirmed_mutations_do_not_cross_user_scope,
        ),
        (
            'expiry_purge_revision_audit',
            memory.test_expiry_boundary_and_purge_advance_durable_revision,
        ),
    ]
    for label, name in (
        (
            'file_permissions_scoped_delete',
            'test_file_permissions_and_scoped_delete',
        ),
        (
            'durable_create_replay',
            'test_confirmed_create_is_idempotent_across_reopen',
        ),
        (
            'durable_update_delete_replay',
            'test_update_and_delete_retries_do_not_repeat_mutations',
        ),
        (
            'two_connection_revision_cas',
            'test_two_connections_share_revisions_and_enforce_cas',
        ),
        (
            'version_one_migration',
            'test_version_one_database_is_migrated_without_data_loss',
        ),
        (
            'concurrent_migration',
            'test_concurrent_version_one_open_migrates_once',
        ),
        (
            'migration_rollback',
            'test_failed_migration_rolls_back_writer_lock',
        ),
        (
            'content_free_operational_tables',
            'test_audit_and_idempotency_tables_do_not_duplicate_content',
        ),
    ):
        memory_checks.append(
            _temporary_check(getattr(memory, name), label, scratch_root)
        )

    speech_checks = [
        (name.removeprefix('test_'), getattr(speech, name))
        for name in (
            'test_partial_can_be_replaced_by_final_without_provider_call',
            'test_one_conversation_cannot_have_two_live_speech_sessions',
            'test_source_control_characters_are_rejected',
            'test_untrusted_transcripts_never_reach_the_provider',
            'test_accepted_sequence_rejects_an_older_later_epoch_event',
            'test_active_tts_requires_barge_in_before_new_user_turn',
            'test_rejected_high_sequence_does_not_block_next_capture_epoch',
            'test_final_safe_response_becomes_text_only_interruptible_tts',
            'test_duplicate_final_is_idempotent_and_mutation_conflicts',
            'test_barge_in_cancels_once_and_opens_a_new_capture_epoch',
            'test_evicted_activity_replay_cannot_cancel_a_later_tts',
            'test_tts_terminal_fences_late_feedback_and_self_echo',
            'test_session_close_cancels_tts_and_rejects_late_transcript',
            'test_audit_projection_excludes_text_speaker_and_audio_content',
        )
    ]
    for field_name, field_value in (
        ('audio', 'synthetic-data'),
        ('pcm', [1, 2, 3]),
        ('path', '/synthetic/voice.wav'),
        ('uri', 'memory://synthetic'),
        ('waveform', [0.1]),
        ('user_id', 'synthetic-untrusted-user'),
    ):
        speech_checks.append(
            _case(
                getattr(
                    speech,
                    'test_transcript_schema_rejects_'
                    'audio_and_untrusted_identity_fields',
                ),
                f'transcript_rejects_{field_name}',
                field_name,
                field_value,
            )
        )
    for field_name, field_value in (
        ('audio', 'synthetic-bytes'),
        ('path', '/synthetic/voice.wav'),
        ('uri', 'memory://synthetic'),
    ):
        speech_checks.append(
            _case(
                speech.test_audio_metadata_rejects_content_and_locations,
                f'audio_metadata_rejects_{field_name}',
                field_name,
                field_value,
            )
        )
    for label, field_name, field_value in (
        ('confidence_nan', 'confidence', float('nan')),
        ('confidence_infinity', 'confidence', float('inf')),
        ('confidence_negative', 'confidence', -0.01),
        ('sequence_zero', 'sequence', 0),
        ('capture_epoch_zero', 'capture_epoch', 0),
    ):
        speech_checks.append(
            _case(
                speech.test_transcript_rejects_invalid_numeric_metadata,
                f'transcript_rejects_{label}',
                field_name,
                field_value,
            )
        )

    expression_checks = [
        (name.removeprefix('test_'), getattr(expression, name))
        for name in (
            'test_cue_round_trip_is_strict_and_content_free',
            'test_neutral_and_non_neutral_intensities_are_not_ambiguous',
            'test_trusted_state_rejects_truthy_non_booleans',
            'test_mapper_uses_final_refusal_without_reading_diagnostic_text',
            'test_mapper_rejects_malformed_safety_metadata',
            'test_policy_priority_is_emergency_then_privacy_then_availability',
            'test_stale_cue_is_rejected_at_the_exact_deadline',
            'test_arbiter_renders_then_returns_to_neutral_at_ttl',
            'test_retry_is_idempotent_without_extending_expression_ttl',
            'test_same_request_id_with_changed_cue_is_a_conflict',
            'test_trusted_override_clears_active_before_reporting_conflict',
            'test_concurrent_duplicate_is_rendered_once',
            'test_emergency_and_privacy_clear_the_assistant_lane',
            'test_trusted_override_rechecks_an_idempotent_retry',
            'test_tick_rechecks_trusted_state_without_a_new_cue',
            'test_unavailable_renderer_and_stale_cue_do_not_render',
            'test_rate_limits_minimum_interval_and_window',
            'test_neutral_cue_bypasses_rate_limit_to_reduce_expression',
            'test_renderer_failure_attempts_neutral_exactly_once',
            'test_neutral_failure_disables_renderer_without_a_fallback_loop',
            'test_explicit_neutral_failure_is_not_retried',
            'test_noop_boundary_is_non_actuating_and_not_a_model_tool',
        )
    ]
    for field_name, value in (
        ('emotion', 'angry'),
        ('emotion', []),
        ('modality', 'audio'),
        ('source', 'model_selected'),
        ('intensity', True),
        ('intensity', float('nan')),
        ('intensity', 0.8),
        ('duration_ms', True),
        ('duration_ms', 249),
        ('duration_ms', 5001),
        ('ttl_ms', True),
        ('ttl_ms', 0),
        ('ttl_ms', 1001),
        ('issued_at', float('inf')),
    ):
        expression_checks.append(
            _case(
                expression.test_cue_rejects_unsupported_or_unbounded_values,
                f'cue_rejects_{field_name}_{len(expression_checks)}',
                field_name,
                value,
            )
        )
    for reason, emotion in (
        ('greeting', 'happy'),
        ('thanks', 'happy'),
        ('apology', 'apologetic'),
        ('celebration', 'excited'),
        ('synthetic_unknown', 'neutral'),
    ):
        expression_checks.append(
            _case(
                expression.test_final_decision_mapper_is_deterministic,
                f'mapper_{reason}',
                reason,
                emotion,
            )
        )
    return {
        'SWM25-75': memory_checks,
        'SWM25-76': speech_checks,
        'SWM25-77': expression_checks,
    }


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def _run_story(
    story_id: str,
    checks: Sequence[Check],
    iterations: int,
    progress_every: int,
) -> Dict[str, Any]:
    subchecks = {
        label: {'attempted': 0, 'passed': 0, 'failed': 0, 'durations': []}
        for label, _function in checks
    }
    iteration_records = []
    failure_samples = []
    total_failures = 0
    story_started = time.perf_counter()
    for iteration in range(1, iterations + 1):
        iteration_started = time.perf_counter()
        passed = True
        for label, function in checks:
            started = time.perf_counter()
            counters = subchecks[label]
            counters['attempted'] += 1
            try:
                function()
                counters['passed'] += 1
            except Exception as error:
                passed = False
                total_failures += 1
                counters['failed'] += 1
                if len(failure_samples) < 20:
                    formatted = ''.join(
                        traceback.format_exception(
                            type(error), error, error.__traceback__
                        )
                    )
                    failure_samples.append(
                        {
                            'iteration': iteration,
                            'check': label,
                            'error_type': type(error).__name__,
                            'error_message_sha256': hashlib.sha256(
                                str(error).encode('utf-8')
                            ).hexdigest(),
                            'traceback_sha256': hashlib.sha256(
                                formatted.encode('utf-8')
                            ).hexdigest(),
                        }
                    )
            finally:
                counters['durations'].append(
                    (time.perf_counter() - started) * 1000.0
                )
        duration = (time.perf_counter() - iteration_started) * 1000.0
        iteration_records.append(
            {
                'iteration': iteration,
                'passed': passed,
                'duration_ms': round(duration, 3),
            }
        )
        if iteration in {1, iterations} or iteration % progress_every == 0:
            print(
                f'{story_id}: {iteration}/{iterations}, '
                f'failures={total_failures}',
                flush=True,
            )
    durations = [item['duration_ms'] for item in iteration_records]
    passed_count = sum(item['passed'] for item in iteration_records)
    subcheck_results = []
    for label, counters in subchecks.items():
        check_durations = counters.pop('durations')
        subcheck_results.append(
            {
                'check': label,
                **counters,
                'duration_ms': {
                    'mean': round(statistics.mean(check_durations), 3),
                    'p95': round(_percentile(check_durations, 0.95), 3),
                    'max': round(max(check_durations), 3),
                },
            }
        )
    return {
        'selected_checks': [label for label, _function in checks],
        'checks_per_iteration': len(checks),
        'subchecks_attempted': len(checks) * iterations,
        'iterations_attempted': iterations,
        'iterations_passed': passed_count,
        'iterations_failed': iterations - passed_count,
        'pass_rate': round(passed_count / iterations, 6),
        'duration_ms': {
            'total': round((time.perf_counter() - story_started) * 1000.0, 3),
            'mean': round(statistics.mean(durations), 3),
            'p95': round(_percentile(durations, 0.95), 3),
            'max': round(max(durations), 3),
        },
        'subchecks': subcheck_results,
        'iterations': iteration_records,
        'failure_count': total_failures,
        'failure_samples': failure_samples,
        'failure_records_omitted': max(0, total_failures - 20),
    }


def _source_hashes() -> Dict[str, str]:
    paths = [
        Path(__file__),
        PACKAGE_ROOT / 'malbut_agent_server' / 'memory.py',
        PACKAGE_ROOT / 'malbut_agent_server' / 'orchestrator.py',
        PACKAGE_ROOT / 'malbut_agent_server' / 'speech.py',
        PACKAGE_ROOT / 'malbut_agent_server' / 'expression.py',
        PACKAGE_ROOT / 'test' / 'test_memory.py',
        PACKAGE_ROOT / 'test' / 'test_orchestrator.py',
        PACKAGE_ROOT / 'test' / 'test_speech_pipeline.py',
        PACKAGE_ROOT / 'test' / 'test_expression.py',
    ]
    return {
        str(path.relative_to(PACKAGE_ROOT)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


def _git_value(*arguments: str) -> str:
    result = subprocess.run(
        ['git', *arguments],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_private_json(path: Path, value: Dict[str, Any]) -> None:
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
        description='Run offline SWM25-75~77 representative stress checks.'
    )
    parser.add_argument('--iterations', type=int, default=300)
    parser.add_argument('--progress-every', type=int, default=25)
    parser.add_argument('--output', type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Run the deterministic offline matrix and persist private evidence."""
    args = _parse_args()
    if not 1 <= args.iterations <= 10000:
        raise SystemExit('--iterations must be between 1 and 10000')
    if args.progress_every < 1:
        raise SystemExit('--progress-every must be positive')
    output = args.output.resolve()
    try:
        output.relative_to(PACKAGE_ROOT)
    except ValueError as error:
        raise SystemExit('--output must stay inside the package') from error
    output.parent.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(PACKAGE_ROOT))
    with tempfile.TemporaryDirectory(
        prefix='.swm25-75-77-stress-',
        dir=str(output.parent),
    ) as scratch:
        checks = _resolve_checks(Path(scratch))
        started = time.perf_counter()
        stories = {
            story_id: _run_story(
                story_id,
                story_checks,
                args.iterations,
                args.progress_every,
            )
            for story_id, story_checks in checks.items()
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
                'git_head': _git_value('rev-parse', 'HEAD'),
                'branch': _git_value('branch', '--show-current'),
                'explicit_source_sha256': _source_hashes(),
                'python': platform.python_version(),
                'platform': platform.platform(),
            },
            'configuration': {
                'iterations_per_story': args.iterations,
                'story_count': len(stories),
                'selected_check_count': sum(
                    len(value) for value in checks.values()
                ),
                'live_api_calls': False,
                'physical_ros_actions': False,
                'camera_or_microphone_access': False,
                'external_notifications': False,
                'actual_renderer_calls': False,
            },
            'privacy': {
                'synthetic_inputs_only': True,
                'raw_memory_content_persisted': False,
                'transcript_text_persisted': False,
                'speaker_id_persisted': False,
                'raw_exception_text_persisted': False,
                'artifact_mode': '0600',
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
                'scope': (
                    'Repeated deterministic synthetic checks; not 300 '
                    'independent users, real voice sessions, renderer '
                    'sessions, or a production SLA.'
                ),
                'SWM25-75': (
                    'Does not prove trusted person binding, public adapter, '
                    'retention policy, or derived-data erasure.'
                ),
                'SWM25-76': (
                    'Does not prove STT/TTS quality, audio I/O, ROS, echo '
                    'cancellation, or latency.'
                ),
                'SWM25-77': (
                    'Does not prove a real frontend, display, LED, or ROS '
                    'renderer.'
                ),
            },
            'stories': stories,
        }
        _write_private_json(output, report)
    print(f'report: {output}', flush=True)
    return 0 if total_passed == total_iterations else 1


if __name__ == '__main__':
    raise SystemExit(main())
