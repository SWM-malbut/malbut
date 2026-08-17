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

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parent

Check = Tuple[str, Callable[[], None]]


class SourceIntegrityError(RuntimeError):
    """Raised without disclosing which source changed during a run."""


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


def _monkeypatch_check(
    function: Callable[[pytest.MonkeyPatch], None],
    label: str,
) -> Check:
    def invoke() -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            function(monkeypatch)

    return label, invoke


def _resolve_checks(scratch_root: Path) -> Dict[str, List[Check]]:
    memory = _load_module('test_memory.py')
    memory_service = _load_module('test_memory_service.py')
    orchestrator = _load_module('test_orchestrator.py')
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
            'real_version_two_migration',
            'test_real_version_two_tables_are_migrated_with_legacy_rows',
        ),
        (
            'idempotency_provenance_mismatch',
            'test_idempotency_provenance_column_mismatch_fails_closed',
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
    memory_checks.append(
        _temporary_check(
            memory.test_writer_gate_blocks_unmanaged_and_legacy_connections,
            'writer_gate_unmanaged_legacy',
            scratch_root,
        )
    )
    for name in (
        'test_low_level_confirmed_compatibility_marks_unknown_provenance',
        'test_provenance_is_part_of_durable_idempotency_fingerprint',
    ):
        memory_checks.append(
            (name.removeprefix('test_'), getattr(memory, name))
        )
    for name in (
        'test_completed_same_user_turn_allows_confirmed_lifecycle',
        'test_reset_reused_turn_identity_has_distinct_provenance',
        'test_deleted_and_recreated_conversation_has_distinct_instance',
        'test_pending_missing_and_other_user_turns_fail_closed',
        'test_other_users_completed_turn_is_not_valid_evidence',
        'test_update_and_delete_retries_survive_evidence_reset',
        'test_closed_or_expired_evidence_only_allows_exact_retry',
    ):
        memory_checks.append(
            (name.removeprefix('test_'), getattr(memory_service, name))
        )
    for label, name in (
        (
            'evidence_deleted_exact_replay_conflict',
            'test_exact_retry_survives_deleted_evidence_but_conflict_does_not',
        ),
        (
            'shared_conversation_memory_database',
            'test_memory_gate_coexists_with_shared_conversation_database',
        ),
        (
            'cross_connection_service_idempotency',
            'test_cross_connection_service_idempotency_is_atomic',
        ),
        (
            'cross_connection_service_conflict',
            'test_cross_connection_request_conflict_is_atomic',
        ),
    ):
        memory_checks.append(
            _temporary_check(
                getattr(memory_service, name),
                label,
                scratch_root,
            )
        )
    for name in (
        'test_memory_change_during_inference_discards_result',
        'test_memory_expiring_during_inference_discards_result',
        'test_provider_cannot_mutate_memory_snapshot_to_bypass_fence',
        'test_independent_conversations_run_provider_calls_in_parallel',
        'test_reset_during_inference_discards_late_answer',
    ):
        memory_checks.append(
            (name.removeprefix('test_'), getattr(orchestrator, name))
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
    for name in (
        'test_barge_in_does_not_wait_for_provider_and_discards_late_tts',
        'test_close_does_not_wait_for_provider_and_discards_late_tts',
        'test_completion_guard_linearizes_commit_before_barge_in',
        'test_slow_commit_does_not_block_unrelated_session_barge_in',
        'test_in_flight_duplicate_and_conflict_do_not_call_provider_twice',
        'test_external_delete_during_inference_returns_typed_discard',
        'test_provider_failure_releases_the_in_flight_reservation',
        'test_cancellation_without_supersession_returns_fallback_result',
        'test_barge_in_discards_a_concurrent_provider_failure',
        'test_supersession_wins_a_concurrent_conversation_error',
        'test_blank_agent_message_fails_before_durable_speech_commit',
        'test_transient_conversation_conflict_is_typed_and_retryable',
    ):
        speech_checks.append(
            (name.removeprefix('test_'), getattr(speech, name))
        )
    for mutation, expected_code in (
        ('close', 'conversation_inactive'),
        ('expire', 'conversation_inactive'),
        ('delete', 'conversation_not_found'),
    ):
        speech_checks.append(
            _case(
                getattr(
                    speech,
                    'test_external_conversation_loss_is_a_'
                    'typed_fail_closed_result',
                ),
                f'external_conversation_{mutation}',
                mutation,
                expected_code,
            )
        )
    for name in (
        'test_completion_guard_cancels_before_durable_commit',
        'test_completion_guard_holds_through_conversation_commit',
    ):
        speech_checks.append(
            (name.removeprefix('test_'), getattr(orchestrator, name))
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
    for name in (
        'test_thread_start_failure_completes_and_caches_reservation',
        'test_newer_emergency_wins_submit_precheck_reservation_race',
        'test_token_cancelled_before_worker_start_skips_expression',
    ):
        expression_checks.append(
            _monkeypatch_check(
                getattr(expression, name),
                name.removeprefix('test_'),
            )
        )
    for name in (
        'test_blocking_renderer_cannot_delay_emergency_neutral',
        'test_late_renderer_completion_cannot_restore_superseded_state',
        'test_receiver_generation_fence_rejects_late_visual_effect',
        'test_explicit_neutral_supersedes_pending_expression',
        'test_slow_concurrent_duplicates_share_one_dispatch',
        'test_control_neutral_makes_normal_submission_renderer_busy',
        'test_pending_conflict_and_unrelated_request_are_rejected',
        'test_neutral_on_empty_lane_is_coalesced_without_rendering',
        'test_unavailable_renderer_clears_active_without_neutral_dispatch',
        'test_expiry_neutral_failure_disables_renderer',
        'test_emergency_cancels_renderer_failure_neutral_fallback',
    ):
        expression_checks.append(
            (name.removeprefix('test_'), getattr(expression, name))
        )
    resolved = {
        'SWM25-75': memory_checks,
        'SWM25-76': speech_checks,
        'SWM25-77': expression_checks,
    }
    for story_id, checks in resolved.items():
        labels = [label for label, _function in checks]
        if len(labels) != len(set(labels)):
            raise RuntimeError(f'{story_id} contains duplicate check labels')
    return resolved


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


def _source_manifest_paths() -> Tuple[Path, ...]:
    """Return every local behavior source in stable manifest order."""
    selected_tests = [
        PACKAGE_ROOT / 'test' / 'test_memory.py',
        PACKAGE_ROOT / 'test' / 'test_memory_service.py',
        PACKAGE_ROOT / 'test' / 'test_orchestrator.py',
        PACKAGE_ROOT / 'test' / 'test_speech_pipeline.py',
        PACKAGE_ROOT / 'test' / 'test_expression.py',
    ]
    paths = [
        Path(__file__).resolve(),
        *selected_tests,
        *(PACKAGE_ROOT / 'malbut_agent_server').rglob('*.py'),
    ]
    return tuple(sorted(
        paths,
        key=lambda path: path.relative_to(PACKAGE_ROOT).as_posix(),
    ))


def _source_hashes() -> Dict[str, str]:
    """Capture one sorted content hash manifest for the offline run."""
    return {
        path.relative_to(PACKAGE_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in _source_manifest_paths()
    }


def _require_unchanged_sources(
    start_hashes: Dict[str, str],
    end_hashes: Dict[str, str],
) -> None:
    """Fail closed without persisting source names or mismatch details."""
    if start_hashes != end_hashes:
        raise SourceIntegrityError(
            'source integrity changed during stress run; report not written'
        )


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
    if not __debug__:
        raise SystemExit(
            'optimized Python disables assertions; report not written'
        )
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
        try:
            start_source_hashes = _source_hashes()
        except OSError:
            raise SystemExit(
                'source integrity manifest could not be captured'
            ) from None
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
                'explicit_source_sha256': start_source_hashes,
                'source_unchanged_during_run': True,
                'assertions_enabled': __debug__,
                'python_optimize': sys.flags.optimize,
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
        try:
            end_source_hashes = _source_hashes()
            _require_unchanged_sources(
                start_source_hashes,
                end_source_hashes,
            )
        except OSError:
            raise SystemExit(
                'source integrity check failed; report not written'
            ) from None
        except SourceIntegrityError as error:
            raise SystemExit(str(error)) from None
        _write_private_json(output, report)
    print(f'report: {output}', flush=True)
    return 0 if total_passed == total_iterations else 1


if __name__ == '__main__':
    raise SystemExit(main())
