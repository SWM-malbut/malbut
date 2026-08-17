"""Trusted conversation results derived from simulation receipts."""

import sqlite3
from dataclasses import replace

import pytest

import malbut_agent_server.conversation as conversation_module
import malbut_agent_server.execution_ledger as execution_ledger
import malbut_agent_server.trusted_results as trusted_results_module
from malbut_agent_server.conversation import ConversationChangedError
from malbut_agent_server.monitor_room_coverage import (
    DEFAULT_COVERAGE_PROFILE,
    PLANNER_REVISION,
)
from malbut_agent_server.trusted_results import (
    TrustedResultSchemaError,
    TrustedToolResult,
)
from test_monitor_room_simulation_execution import (
    MutableClock,
    _scenario,
    _simulation_store,
    _trusted_variant,
)


def test_planned_receipt_atomically_appends_structured_result(
    tmp_path,
) -> None:
    """Append no fake turn and expose only the closed current result."""
    database = tmp_path / 'trusted-result.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-result')
    before = store.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    before_turns = store.list_turns(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )

    receipt = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    after = store.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    results = store.list_trusted_tool_results(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    snapshot = store.snapshot(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    next_turn = store.begin_turn(
        user_id=scenario.draft.user_id,
        conversation_id=scenario.draft.conversation_id,
        turn_id='trusted-result-next-turn',
        request_id='trusted-result-next-request',
        request_fingerprint='trusted-result-next-fingerprint',
        user_content='결과를 알려줘',
    )

    assert len(results) == 1
    result = results[0]
    assert result.record_kind == 'planned'
    assert result.result_digest == receipt.result_digest
    assert result.sample_count == 49
    assert after.revision == before.revision + 1
    assert after.expires_at == before.expires_at
    assert store.list_turns(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    ) == before_turns
    assert snapshot.trusted_results == results
    assert next_turn.trusted_results == results
    assert next_turn.token is not None
    store.fail_turn(next_turn.token)
    assert result.to_prompt_dict() == {
        'schema_version': 1,
        'source': 'monitor_room_simulation',
        'tool_name': 'monitor_room',
        'record_kind': 'planned',
        'state': 'succeeded',
        'code': 'semantic_sample_plan_created',
        'completed_at': receipt.completed_at,
        'simulation': True,
        'physical_authorized': False,
        'physical_effects': False,
        'viewer_live': False,
        'nav2_validated': False,
        'camera_coverage_validated': False,
        'coverage_achieved': False,
        'execution_authorized': False,
        'coverage_plan': {
            'planner_revision': PLANNER_REVISION,
            'sample_count': 49,
            'component_count': 1,
        },
    }
    rendered = str(result.to_prompt_dict())
    for private in (
        result.trusted_result_id,
        result.trusted_result_fingerprint,
        scenario.draft.confirmation_request_id,
        scenario.target.room_id,
        scenario.target.device_id,
        scenario.target.map_id,
        receipt.receipt_digest,
        receipt.plan_digest,
        receipt.result_digest,
    ):
        assert private not in rendered
    store.close()


def test_exact_replay_verifies_one_result_without_duplicate(
    tmp_path,
) -> None:
    """Replay the receipt without creating or revising another result."""
    database = tmp_path / 'trusted-result-replay.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-replay')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    first_session = store.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    first = store.list_trusted_tool_results(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )[0]
    store.close()

    reopened = _simulation_store(str(database), clock=clock)
    replay = reopened.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    results = reopened.list_trusted_tool_results(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    replay_session = reopened.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )

    assert replay.replayed is True
    assert results == (first,)
    assert replay_session.revision == first_session.revision
    reopened.close()


def test_planning_failure_is_typed_without_raw_exception(
    tmp_path,
    monkeypatch,
) -> None:
    """Append a closed failure code without exception text."""
    database = tmp_path / 'trusted-result-failed.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-failed')

    def fail(_target):
        raise RuntimeError('private planner detail')

    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        fail,
    )
    receipt = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    result = store.list_trusted_tool_results(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )[0]

    assert receipt.record_kind == 'planning_failed'
    assert result.record_kind == 'planning_failed'
    assert result.state == 'failed'
    assert result.sample_count == result.component_count == 0
    assert result.plan_digest is None
    assert 'private planner detail' not in str(result.to_prompt_dict())
    store.close()


def test_invalidated_receipt_never_creates_trusted_result(
    tmp_path,
) -> None:
    """Keep target-drift tombstones outside the trusted result lane."""
    database = tmp_path / 'trusted-result-invalidated.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-invalidated')
    before = store.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    changed = replace(
        scenario.target,
        source_revision='changed-source-revision',
    )
    approval, request = _trusted_variant(
        scenario,
        current_target=changed,
    )
    receipt = store.consume_approved_monitor_room_simulation(
        approval=approval,
        request=request,
    )

    assert receipt.record_kind == 'invalidated'
    assert store.list_trusted_tool_results(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    ) == ()
    assert store.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    ).revision == before.revision
    store.close()


def test_result_insert_failure_rolls_back_terminal_and_revision(
    tmp_path,
    monkeypatch,
) -> None:
    """Leave neither receipt nor revision when the result insert fails."""
    database = tmp_path / 'trusted-result-atomic-rollback.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-atomic-rollback')
    before = store.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    original = conversation_module.record_or_verify_trusted_result_locked

    def fail_result(*_args, **_kwargs):
        raise RuntimeError('injected trusted result failure')

    monkeypatch.setattr(
        conversation_module,
        'record_or_verify_trusted_result_locked',
        fail_result,
    )
    with pytest.raises(RuntimeError, match='injected trusted result failure'):
        store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )

    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
    ).fetchone()[0] == 0
    assert store._connection.execute(
        'SELECT COUNT(*) FROM conversation_trusted_tool_results'
    ).fetchone()[0] == 0
    assert store.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    ).revision == before.revision
    monkeypatch.setattr(
        conversation_module,
        'record_or_verify_trusted_result_locked',
        original,
    )
    retry = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert retry.replayed is False
    assert store._connection.execute(
        'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
    ).fetchone()[0] == 1
    assert len(store.list_trusted_tool_results(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )) == 1
    assert store.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    ).revision == before.revision + 1
    store.close()


def test_result_revision_fences_concurrent_provider_completion(
    tmp_path,
) -> None:
    """Bump revision without extending TTL so a stale provider CAS loses."""
    database = tmp_path / 'trusted-result-provider-race.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-provider-race')
    pending = store.begin_turn(
        user_id=scenario.draft.user_id,
        conversation_id=scenario.draft.conversation_id,
        turn_id='provider-race-turn',
        request_id='provider-race-request',
        request_fingerprint='provider-race-fingerprint',
        user_content='다음 질문',
    )
    assert pending.token is not None
    expires_at = pending.session.expires_at

    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    current = store.get(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )

    assert current.revision == pending.session.revision + 1
    assert current.expires_at == expires_at
    with pytest.raises(ConversationChangedError):
        store.complete_turn(
            pending.token,
            assistant_content='stale provider response',
            response={'schema_version': 3},
        )
    store.close()


def test_reset_filters_old_result_and_delete_cascades_it(
    tmp_path,
) -> None:
    """Keep reset generation-local and honor conversation deletion."""
    database = tmp_path / 'trusted-result-lifecycle.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-lifecycle')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert store.reset(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    ).generation == scenario.draft.generation + 1
    assert store.list_trusted_tool_results(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    ) == ()
    assert store._connection.execute(
        'SELECT COUNT(*) FROM conversation_trusted_tool_results'
    ).fetchone()[0] == 1
    assert store.delete(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    assert store._connection.execute(
        'SELECT COUNT(*) FROM conversation_trusted_tool_results'
    ).fetchone()[0] == 0
    store.close()


def test_delete_then_exact_receipt_replay_does_not_resurrect_result(
    tmp_path,
) -> None:
    """Retain receipt replay while deleted conversation content stays gone."""
    database = tmp_path / 'trusted-result-delete-replay.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-delete-replay')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert store.delete(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    replay = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )

    assert replay.replayed is True
    assert store._connection.execute(
        'SELECT COUNT(*) FROM conversation_trusted_tool_results'
    ).fetchone()[0] == 0
    store.close()


def test_existing_receipt_at_schema_activation_is_not_backfilled(
    tmp_path,
) -> None:
    """Fence pre-bridge receipts and leave exact replay result-free."""
    database = tmp_path / 'trusted-result-activation.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-activation')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store._connection.executescript(
        '''
        DROP TRIGGER conversation_trusted_tool_result_insert_guard;
        DROP TRIGGER conversation_trusted_tool_result_no_update;
        DROP TRIGGER conversation_trusted_tool_result_no_replace;
        DROP INDEX conversation_trusted_tool_results_owner_idx;
        DROP TABLE conversation_trusted_tool_results;
        DROP TRIGGER monitor_room_trusted_result_metadata_no_update;
        DROP TRIGGER monitor_room_trusted_result_metadata_no_delete;
        DROP TRIGGER monitor_room_trusted_result_metadata_no_replace;
        DROP TABLE monitor_room_trusted_result_schema_metadata;
        '''
    )
    store._connection.execute(
        'DROP TRIGGER monitor_room_simulation_preactivation_no_delete'
    )
    store._connection.execute(
        '''
        DELETE FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (trusted_results_module.TRUSTED_RESULT_ACTIVATION_SENTINEL,),
    )
    store._connection.execute(
        execution_ledger.SIMULATION_PREACTIVATION_NO_DELETE_TRIGGER_SQL
    )
    store._connection.commit()
    store.close()

    reopened = _simulation_store(str(database), clock=clock)
    replay = reopened.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    metadata = reopened._connection.execute(
        'SELECT * FROM monitor_room_trusted_result_schema_metadata'
    ).fetchone()

    assert replay.replayed is True
    assert metadata['terminal_rowid_cutoff'] == 1
    assert reopened._connection.execute(
        'SELECT COUNT(*) FROM conversation_trusted_tool_results'
    ).fetchone()[0] == 0
    reopened.close()


def test_full_schema_drop_after_activation_fails_closed(tmp_path) -> None:
    """Keep an independent immutable anchor outside the result objects."""
    database = tmp_path / 'trusted-result-schema-drop.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-schema-drop')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store._connection.executescript(
        '''
        DROP TRIGGER conversation_trusted_tool_result_insert_guard;
        DROP TRIGGER conversation_trusted_tool_result_no_update;
        DROP TRIGGER conversation_trusted_tool_result_no_replace;
        DROP INDEX conversation_trusted_tool_results_owner_idx;
        DROP TABLE conversation_trusted_tool_results;
        DROP TRIGGER monitor_room_trusted_result_metadata_no_update;
        DROP TRIGGER monitor_room_trusted_result_metadata_no_delete;
        DROP TRIGGER monitor_room_trusted_result_metadata_no_replace;
        DROP TABLE monitor_room_trusted_result_schema_metadata;
        '''
    )
    store._connection.commit()
    store.close()

    with pytest.raises(TrustedResultSchemaError):
        _simulation_store(str(database), clock=clock)


def test_activation_cutoff_tamper_breaks_independent_anchor(
    tmp_path,
) -> None:
    """Bind the activation cutoff to the separately protected sentinel."""
    database = tmp_path / 'trusted-result-cutoff-tamper.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    store._connection.execute(
        'DROP TRIGGER monitor_room_trusted_result_metadata_no_update'
    )
    store._connection.execute(
        '''
        UPDATE monitor_room_trusted_result_schema_metadata
        SET terminal_rowid_cutoff = terminal_rowid_cutoff + 1
        '''
    )
    store._connection.execute(
        trusted_results_module.TRUSTED_RESULT_METADATA_NO_UPDATE_SQL
    )
    store._connection.commit()
    store.close()

    with pytest.raises(TrustedResultSchemaError):
        _simulation_store(str(database), clock=clock)


def test_schema_and_row_tamper_fail_closed(tmp_path) -> None:
    """Reject missing guards and coherent-looking raw row mutation."""
    database = tmp_path / 'trusted-result-tamper.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-tamper')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store._connection.execute(
        'DROP TRIGGER conversation_trusted_tool_result_no_update'
    )
    store._connection.execute(
        '''
        UPDATE conversation_trusted_tool_results
        SET sample_count = sample_count + 1
        '''
    )
    store._connection.commit()
    store.close()

    with pytest.raises(TrustedResultSchemaError):
        _simulation_store(str(database), clock=clock)


@pytest.mark.parametrize(
    'assignment',
    (
        'sample_count = sample_count + 1',
        'sample_count = 1.5',
        "trusted_result_fingerprint = '" + ('c' * 64) + "'",
        'physical_effects = 1',
    ),
)
def test_restored_guard_row_tamper_fails_closed(
    tmp_path,
    assignment,
) -> None:
    """Recompute and type-check rows after a restored SQL guard."""
    database = tmp_path / (
        'trusted-result-row-tamper-'
        + str(abs(hash(assignment)))
        + '.sqlite3'
    )
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(
        store,
        clock,
        suffix='trusted-row-' + str(abs(hash(assignment))),
    )
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store._connection.execute(
        'DROP TRIGGER conversation_trusted_tool_result_no_update'
    )
    store._connection.execute('PRAGMA ignore_check_constraints=ON')
    store._connection.execute(
        'UPDATE conversation_trusted_tool_results SET ' + assignment
    )
    store._connection.execute('PRAGMA ignore_check_constraints=OFF')
    store._connection.execute(
        trusted_results_module.TRUSTED_RESULT_NO_UPDATE_SQL
    )
    store._connection.commit()
    store.close()

    with pytest.raises(TrustedResultSchemaError):
        _simulation_store(str(database), clock=clock)


def test_replace_and_live_owner_delete_tamper_are_detected(
    tmp_path,
) -> None:
    """Block replacement and detect a missing post-activation result."""
    database = tmp_path / 'trusted-result-delete-tamper.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='trusted-delete-tamper')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            '''
            INSERT OR REPLACE INTO conversation_trusted_tool_results
            SELECT * FROM conversation_trusted_tool_results
            '''
        )
    store._connection.rollback()
    store._connection.execute(
        'DELETE FROM conversation_trusted_tool_results'
    )
    store._connection.commit()
    store.close()

    with pytest.raises(TrustedResultSchemaError):
        _simulation_store(str(database), clock=clock)


def test_unicode_identifiers_are_valid_for_frozen_result() -> None:
    """Use shared identifier rules rather than an ASCII-only subset."""
    digest = 'a' * 64
    result = TrustedToolResult(
        trusted_result_id='trusted-tool-result-' + ('b' * 40),
        trusted_result_fingerprint=digest,
        user_id='사용자 이름',
        conversation_id='대화 세션',
        session_instance_id='session-instance',
        generation=1,
        source_revision=1,
        source_turn_id='발화 차례',
        source_ordinal=1,
        record_kind='planned',
        state='succeeded',
        result_code='semantic_sample_plan_created',
        planner_revision=PLANNER_REVISION,
        profile_digest=DEFAULT_COVERAGE_PROFILE.digest,
        plan_digest=digest,
        result_digest=digest,
        sample_count=1,
        component_count=1,
        completed_at=0.0,
    )

    assert result.user_id == '사용자 이름'
    assert result.conversation_id == '대화 세션'
