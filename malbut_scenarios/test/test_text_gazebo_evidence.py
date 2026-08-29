"""Contracts for content-free SWM25-133 acceptance evidence."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import inspect
import json
import os
import stat
from threading import Barrier, Thread

import pytest

from malbut_scenarios import text_gazebo_evidence
from malbut_scenarios.text_gazebo_evidence import (
    CleanupEvidence,
    ConfirmationState,
    DispatchState,
    EvidenceCounts,
    EvidenceDurations,
    NavigationState,
    ReadinessState,
    RobotActionState,
    StableStates,
    TextGazeboEvidenceManifest,
    TextGazeboEvidenceReceipt,
    write_evidence_manifest,
)
from malbut_scenarios.text_gazebo_scenario import (
    TextGazeboScenarioProfile,
)


def _states() -> StableStates:
    return StableStates(
        readiness=ReadinessState.READY,
        confirmation=ConfirmationState.APPROVED,
        robot_action=RobotActionState.SUCCEEDED,
        dispatch=DispatchState.TERMINAL,
        navigation=NavigationState.SUCCEEDED,
    )


def _counts() -> EvidenceCounts:
    return EvidenceCounts(
        agent_proposal_count=1,
        confirmation_count=1,
        approved_confirmation_count=1,
        robot_action_count=1,
        dispatch_intent_count=1,
        robot_web_start_count=1,
        robot_web_verified_target_count=1,
        nav2_goal_count=1,
        preapproval_nav2_goal_count=0,
        terminal_result_count=1,
        replay_additional_effect_count=0,
    )


def _durations() -> EvidenceDurations:
    return EvidenceDurations(
        readiness_seconds=1,
        execution_seconds=2.5,
        cleanup_seconds=0.5,
        total_seconds=4,
    )


def _cleanup() -> CleanupEvidence:
    return CleanupEvidence(
        completed=True,
        owned_processes_remaining=0,
        ros_nodes_remaining=0,
        owned_sockets_remaining=0,
        forced_termination_count=0,
    )


def _receipt(**changes) -> TextGazeboEvidenceReceipt:
    values = {
        'run_id': 'run-' + '1' * 32,
        'commit': '2' * 40,
        'source_tree_digest': '6' * 64,
        'installed_digest': '3' * 64,
        'goal_set_digest': '4' * 64,
        'runtime_binding_digest': '5' * 64,
        'target_binding_digest': '7' * 64,
        'scenario_profile': TextGazeboScenarioProfile.HAPPY_PATH,
        'states': _states(),
        'counts': _counts(),
        'durations': _durations(),
        'cleanup': _cleanup(),
    }
    values.update(changes)
    return TextGazeboEvidenceReceipt(**values)


def test_receipt_schema_is_fixed_canonical_and_digest_bound() -> None:
    receipt = _receipt()
    manifest = TextGazeboEvidenceManifest(receipt)
    receipt_value = json.loads(receipt.canonical_json())
    manifest_value = json.loads(manifest.canonical_json())

    assert set(receipt_value) == {
        'cleanup',
        'commit',
        'counts',
        'durations',
        'goal_set_digest',
        'installed_digest',
        'physical_authorized',
        'run_id',
        'runtime_binding_digest',
        'scenario_profile',
        'simulation',
        'source_tree_digest',
        'states',
        'target_binding_digest',
    }
    assert receipt_value['simulation'] is True
    assert receipt_value['physical_authorized'] is False
    assert receipt_value['states']['navigation'] == 'succeeded'
    assert receipt_value['counts']['nav2_goal_count'] == 1
    assert manifest_value == {
        'format': 'malbut.text-gazebo-e2e-evidence.v3',
        'receipt': receipt_value,
        'receipt_digest': receipt.digest(),
    }
    assert receipt.digest() == hashlib.sha256(
        receipt.canonical_json().encode('utf-8')
    ).hexdigest()
    assert manifest.digest() == hashlib.sha256(
        manifest.canonical_json().encode('utf-8')
    ).hexdigest()
    assert ' ' not in receipt.canonical_json()
    assert '\n' not in receipt.canonical_json()


def test_receipt_and_manifest_are_immutable_and_slot_only() -> None:
    receipt = _receipt()
    manifest = TextGazeboEvidenceManifest(receipt)

    with pytest.raises(FrozenInstanceError):
        receipt.run_id = 'run-' + '9' * 32
    with pytest.raises(FrozenInstanceError):
        manifest.receipt_digest = '0' * 64
    assert not hasattr(receipt, '__dict__')
    assert not hasattr(manifest, '__dict__')


def test_public_api_has_no_private_content_fields() -> None:
    constructors = (
        TextGazeboEvidenceReceipt,
        StableStates,
        EvidenceCounts,
        EvidenceDurations,
        CleanupEvidence,
    )
    parameters = {
        name
        for constructor in constructors
        for name in inspect.signature(constructor).parameters
    }
    forbidden = {
        'text',
        'utterance',
        'token',
        'cookies',
        'csrf',
        'session_id',
        'action_id',
        'goal_id',
        'coordinates',
        'path',
        'environment',
        'payload',
    }

    assert parameters.isdisjoint(forbidden)
    assert 'physical_authorized' not in inspect.signature(
        TextGazeboEvidenceReceipt
    ).parameters
    assert 'simulation' not in inspect.signature(
        TextGazeboEvidenceReceipt
    ).parameters
    assert 'secret-value' not in repr(_receipt())
    assert 'secret-value' not in repr(
        TextGazeboEvidenceManifest(_receipt())
    )


@pytest.mark.parametrize(
    'field,value',
    (
        ('run_id', 'request-private-id'),
        ('commit', 'A' * 40),
        ('commit', 'a' * 39),
        ('installed_digest', 'g' * 64),
        ('source_tree_digest', 'g' * 64),
        ('goal_set_digest', '4' * 63),
        ('runtime_binding_digest', '5' * 65),
        ('target_binding_digest', '7' * 63),
    ),
)
def test_receipt_rejects_unbounded_or_non_digest_identity(
    field,
    value,
) -> None:
    with pytest.raises(ValueError):
        _receipt(**{field: value})


@pytest.mark.parametrize('value', (True, -1, 1.5, float('nan'), 1_000_001))
def test_counts_reject_bool_nan_fraction_negative_and_excess(value) -> None:
    with pytest.raises(ValueError, match='bounded non-negative integer'):
        replace(_counts(), nav2_goal_count=value)


@pytest.mark.parametrize(
    'value',
    (True, -0.1, float('nan'), float('inf'), 86_400.1),
)
def test_durations_reject_non_finite_negative_and_excess(value) -> None:
    with pytest.raises(ValueError, match='finite'):
        replace(_durations(), total_seconds=value)


def test_typed_states_reject_arbitrary_strings() -> None:
    with pytest.raises(TypeError, match='ReadinessState'):
        replace(_states(), readiness='ready')

    with pytest.raises(TypeError, match='TextGazeboScenarioProfile'):
        _receipt(scenario_profile='happy_path')


@pytest.mark.parametrize(
    'changes',
    (
        {
            'states': StableStates(
                readiness=ReadinessState.READY,
                confirmation=ConfirmationState.APPROVED,
                robot_action=RobotActionState.UNKNOWN,
                dispatch=DispatchState.TERMINAL,
                navigation=NavigationState.SUCCEEDED,
            ),
        },
        {'counts': replace(_counts(), nav2_goal_count=2)},
        {'counts': replace(_counts(), preapproval_nav2_goal_count=1)},
        {'counts': replace(_counts(), replay_additional_effect_count=1)},
        {
            'cleanup': replace(
                _cleanup(),
                owned_processes_remaining=1,
            ),
        },
        {'cleanup': replace(_cleanup(), forced_termination_count=1)},
    ),
)
def test_success_receipt_rejects_non_success_evidence(changes) -> None:
    with pytest.raises(ValueError, match='success receipt'):
        _receipt(**changes)


def test_writer_creates_private_parent_and_atomic_owner_only_file(
    tmp_path,
) -> None:
    private_parent = tmp_path / 'private-evidence'
    destination = private_parent / 'receipt.json'
    manifest = TextGazeboEvidenceManifest(_receipt())

    digest = write_evidence_manifest(destination, manifest)

    assert digest == manifest.digest()
    assert destination.read_text(encoding='utf-8') == (
        manifest.canonical_json() + '\n'
    )
    assert stat.S_IMODE(private_parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.stat().st_uid == os.getuid()
    assert list(private_parent.glob('.*.tmp')) == []


def test_writer_refuses_overwrite_and_preserves_original(tmp_path) -> None:
    parent = tmp_path / 'private'
    parent.mkdir(mode=0o700)
    destination = parent / 'receipt.json'
    first = TextGazeboEvidenceManifest(_receipt())
    second = TextGazeboEvidenceManifest(
        _receipt(run_id='run-' + '6' * 32)
    )
    write_evidence_manifest(destination, first)

    with pytest.raises(FileExistsError, match='already exists'):
        write_evidence_manifest(destination, second)

    assert destination.read_text(encoding='utf-8') == (
        first.canonical_json() + '\n'
    )


def test_writer_rejects_destination_and_parent_symlinks(tmp_path) -> None:
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    target = private / 'target.json'
    target.write_text('unchanged', encoding='utf-8')
    alias = private / 'receipt.json'
    alias.symlink_to(target.name)
    manifest = TextGazeboEvidenceManifest(_receipt())

    with pytest.raises(ValueError, match='symbolic link'):
        write_evidence_manifest(alias, manifest)
    assert target.read_text(encoding='utf-8') == 'unchanged'

    real_parent = tmp_path / 'real-parent'
    real_parent.mkdir(mode=0o700)
    parent_alias = tmp_path / 'parent-alias'
    parent_alias.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match='symbolic link'):
        write_evidence_manifest(parent_alias / 'receipt.json', manifest)


def test_writer_rejects_non_private_or_foreign_parent(
    monkeypatch,
    tmp_path,
) -> None:
    parent = tmp_path / 'not-private'
    parent.mkdir(mode=0o755)
    manifest = TextGazeboEvidenceManifest(_receipt())
    with pytest.raises(PermissionError, match='0700'):
        write_evidence_manifest(parent / 'receipt.json', manifest)

    parent.chmod(0o700)
    actual_uid = os.getuid()
    monkeypatch.setattr(
        text_gazebo_evidence.os,
        'getuid',
        lambda: actual_uid + 1,
    )
    with pytest.raises(PermissionError, match='owned'):
        write_evidence_manifest(parent / 'receipt.json', manifest)


def test_concurrent_writers_publish_exactly_one_complete_manifest(
    tmp_path,
) -> None:
    parent = tmp_path / 'private'
    parent.mkdir(mode=0o700)
    destination = parent / 'receipt.json'
    manifests = (
        TextGazeboEvidenceManifest(_receipt()),
        TextGazeboEvidenceManifest(
            _receipt(run_id='run-' + '7' * 32)
        ),
    )
    barrier = Barrier(2)
    outcomes = []

    def publish(manifest):
        barrier.wait()
        try:
            write_evidence_manifest(destination, manifest)
        except FileExistsError:
            outcomes.append('exists')
        else:
            outcomes.append('published')

    threads = [Thread(target=publish, args=(item,)) for item in manifests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ['exists', 'published']
    saved = destination.read_text(encoding='utf-8').rstrip('\n')
    assert saved in {item.canonical_json() for item in manifests}
    assert list(parent.glob('.*.tmp')) == []
