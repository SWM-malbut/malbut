"""Durable, simulation-only consumption of room confirmations."""

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, replace

import pytest

import malbut_agent_server.execution_ledger as execution_ledger
from malbut_agent_server.conversation import (
    ConfirmationIntentDraft,
    ConfirmationIntentConflictError,
    SQLiteConversationStore,
)
from malbut_agent_server.execution_ledger import (
    SimulationAssuranceError,
    SimulationConsumeConflictError,
    SimulationConsumeRequest,
    SimulationExecutionAlreadyConsumedError,
    SimulationExecutionContractUpgradeRequiredError,
    SimulationExecutionNotFoundError,
    SimulationExecutionSchemaError,
    VerifiedSimulationApproval,
)
from malbut_agent_server.monitor_room_coverage import (
    CoveragePlanningResult,
    DEFAULT_COVERAGE_PROFILE,
    PLANNER_REVISION,
)
from malbut_agent_server.monitor_room_target import Effects, TargetBinding
from malbut_agent_server.schemas import ValidationError


_TEST_TRUST = execution_ledger._SimulationTestTrustHarness(
    hashlib.sha256(b'malbut-explicit-test-only-simulation-trust').digest(),
)


def _simulation_store(*args, **kwargs) -> SQLiteConversationStore:
    """Open a store with the explicit test-only HMAC verifier."""
    kwargs.setdefault('simulation_execution_verifier', _TEST_TRUST)
    return SQLiteConversationStore(*args, **kwargs)


class MutableClock:
    """Deterministic server clock for execution-boundary tests."""

    def __init__(self, now: float = 100.0) -> None:
        """Start at one finite timestamp."""
        self.now = now

    def __call__(self) -> float:
        """Return the current timestamp."""
        return self.now


class QueuedClock(MutableClock):
    """Return explicit samples, then fall back to the mutable value."""

    def __init__(self, now: float = 100.0) -> None:
        """Start with no queued observations."""
        super().__init__(now)
        self.samples = []

    def __call__(self) -> float:
        """Consume one queued sample when present."""
        if self.samples:
            return self.samples.pop(0)
        return self.now


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _target(arguments_json: str, suffix: str) -> TargetBinding:
    geometry = {
        'type': 'Polygon',
        'coordinates': [[
            [0.0, 0.0],
            [4.0, 0.0],
            [4.0, 4.0],
            [0.0, 4.0],
            [0.0, 0.0],
        ]],
    }
    geometry_json = json.dumps(
        geometry,
        sort_keys=True,
        separators=(',', ':'),
    )
    return TargetBinding(
        device_id=f'simulation-device-{suffix}',
        device_binding_revision=f'simulation-membership-{suffix}',
        source_revision=f'simulation-source-{suffix}',
        map_id='simulation-map-home',
        map_revision=f'simulation-map-revision-{suffix}',
        semantic_revision=_digest(f'simulation-semantics-{suffix}'),
        frame_id='map',
        room_id=f'simulation-room-{suffix}',
        room_name='거실',
        room_category='living_room',
        source_arguments_digest=_digest(arguments_json),
        geometry_json=geometry_json,
        geometry_digest=_digest(geometry_json),
        representative_point=(2.0, 2.0),
        clearance_m=2.0,
        area_m2=16.0,
        effects=Effects(
            physical_navigation=True,
            camera_capture=True,
            external_video_stream=True,
            video_recording=False,
            audio_capture=False,
            max_duration_seconds=300,
            coverage_mode='whole_room',
            viewer_scope='requesting_user',
            talkback_allowed=False,
        ),
    )


@dataclass(frozen=True)
class ApprovedScenario:
    """All server-owned inputs for one approved simulation."""

    draft: ConfirmationIntentDraft
    target: TargetBinding
    approval: VerifiedSimulationApproval
    request: SimulationConsumeRequest


def _commit_confirmation(
    store: SQLiteConversationStore,
    clock: MutableClock,
    *,
    suffix: str = 'one',
    expires_in: float = 60.0,
) -> tuple[ConfirmationIntentDraft, TargetBinding]:
    user_id = 'simulation-user'
    conversation_id = f'simulation-conversation-{suffix}'
    store.create(user_id, conversation_id)
    begin = store.begin_turn(
        user_id=user_id,
        conversation_id=conversation_id,
        turn_id=f'simulation-turn-{suffix}',
        request_id=f'simulation-agent-request-{suffix}',
        request_fingerprint=_digest(f'agent-request-{suffix}'),
        user_content='거실 전체를 보여줘',
    )
    token = begin.token
    assert token is not None
    arguments = {'location': '거실'}
    arguments_json = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    target = _target(arguments_json, suffix)
    issued_at = clock.now
    expires_at = issued_at + expires_in
    message = (
        '거실에서 이동해 방 전체를 확인하고 실시간 영상을 '
        '전송할까요? 최대 300초, 녹화·음성·말하기는 '
        '사용하지 않습니다.'
    )
    fingerprint_body = {
        'schema_version': 3,
        'agent_request_id': token.request_id,
        'user_id': token.user_id,
        'speech_session_id': 'simulation-speech-session',
        'source_utterance_id': f'simulation-utterance-{suffix}',
        'conversation_id': token.conversation_id,
        'conversation_session_instance_id': token.session_instance_id,
        'conversation_generation': token.generation,
        'conversation_revision': token.revision + 1,
        'conversation_ordinal': token.ordinal,
        'turn_id': token.turn_id,
        'decision_id': f'simulation-decision-{suffix}',
        'tool_name': 'monitor_room',
        'arguments': arguments,
        'issued_at': issued_at,
        'expires_at': expires_at,
        'risk_level': 'L3',
        'message': message,
        'target': target.to_private_dict(),
    }
    proposal_fingerprint = _digest(
        json.dumps(
            fingerprint_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
    )
    draft = ConfirmationIntentDraft(
        schema_version=3,
        confirmation_request_id=f'simulation-confirmation-{suffix}',
        agent_request_id=token.request_id,
        user_id=token.user_id,
        speech_session_id='simulation-speech-session',
        source_utterance_id=f'simulation-utterance-{suffix}',
        conversation_id=token.conversation_id,
        session_instance_id=token.session_instance_id,
        generation=token.generation,
        revision=token.revision + 1,
        ordinal=token.ordinal,
        turn_id=token.turn_id,
        decision_id=f'simulation-decision-{suffix}',
        tool_name='monitor_room',
        arguments_digest=_digest(arguments_json),
        proposal_fingerprint=proposal_fingerprint,
        issued_at=issued_at,
        expires_at=expires_at,
        risk_level='L3',
        confirmation_message=message,
        target_binding_schema_version=target.schema_version,
        target_device_id=target.device_id,
        target_device_binding_revision=target.device_binding_revision,
        target_source_revision=target.source_revision,
        target_map_id=target.map_id,
        target_map_revision=target.map_revision,
        target_semantic_revision=target.semantic_revision,
        target_frame_id=target.frame_id,
        target_room_id=target.room_id,
        target_room_name=target.room_name,
        target_room_category=target.room_category,
        target_geometry_json=target.geometry_json,
        target_geometry_digest=target.geometry_digest,
        target_representative_x=target.representative_point[0],
        target_representative_y=target.representative_point[1],
        target_clearance_m=target.clearance_m,
        target_area_m2=target.area_m2,
        target_source_arguments_digest=target.source_arguments_digest,
        target_binding_digest=target.binding_digest,
        effects_schema_version=target.effects.schema_version,
        effect_physical_navigation=target.effects.physical_navigation,
        effect_camera_capture=target.effects.camera_capture,
        effect_external_video_stream=(
            target.effects.external_video_stream
        ),
        effect_video_recording=target.effects.video_recording,
        effect_audio_capture=target.effects.audio_capture,
        effect_coverage_mode=target.effects.coverage_mode,
        effect_viewer_scope=target.effects.viewer_scope,
        effect_talkback_allowed=target.effects.talkback_allowed,
        effect_max_duration_seconds=target.effects.max_duration_seconds,
        effects_digest=target.effects_digest,
    )
    response = {
        'schema_version': 3,
        'public': {
            'request_id': token.request_id,
            'conversation': {
                'conversation_id': token.conversation_id,
                'session_instance_id': token.session_instance_id,
                'turn_id': token.turn_id,
                'generation': token.generation,
                'revision': token.revision + 1,
                'ordinal': token.ordinal,
            },
            'decision': {
                'type': 'tool_call',
                'tool_name': 'monitor_room',
                'arguments': arguments,
            },
            'safety': {'allowed': True},
            'execution': {
                'decision_id': draft.decision_id,
                'issued_at': issued_at,
                'expires_at': expires_at,
                'proposal_authorized': True,
                'state_trusted': True,
                'authorized': False,
                'consume_once': False,
                'tool_call_id': None,
            },
        },
    }
    store.complete_turn(
        token,
        assistant_content='거실 모니터링을 시작할까요?',
        response=response,
        confirmation_intent=draft,
    )
    return draft, target


def _approve(
    store: SQLiteConversationStore,
    clock: MutableClock,
    draft: ConfirmationIntentDraft,
    target: TargetBinding,
    *,
    consume_request_id: str = 'simulation-consume-one',
) -> ApprovedScenario:
    terminal = store.resolve_confirmation_intent(
        user_id=draft.user_id,
        confirmation_request_id=draft.confirmation_request_id,
        proposal_fingerprint=draft.proposal_fingerprint,
        response_id=f'simulation-response-{draft.turn_id}',
        response_fingerprint=_digest(f'response-{draft.turn_id}'),
        requested_disposition='approve',
        response_channel='ui_in_process',
        assurance_level='unverified_in_process_ui',
        provenance_ref=_digest(f'provenance-{draft.turn_id}'),
    )
    assert terminal.confirmation_result_id is not None
    assert terminal.resolved_at is not None
    approval, request = _TEST_TRUST.issue(
        user_id=draft.user_id,
        principal_binding_digest=_digest(f'principal-{draft.user_id}'),
        confirmation_request_id=draft.confirmation_request_id,
        confirmation_result_id=terminal.confirmation_result_id,
        proposal_fingerprint=draft.proposal_fingerprint,
        verified_at=terminal.resolved_at,
        approval_expires_at=draft.expires_at + 600.0,
        consume_request_id=consume_request_id,
        current_target=target,
        target_observed_at=clock.now,
    )
    return ApprovedScenario(draft, target, approval, request)


def _scenario(
    store: SQLiteConversationStore,
    clock: MutableClock,
    *,
    suffix: str = 'one',
    expires_in: float = 60.0,
) -> ApprovedScenario:
    draft, target = _commit_confirmation(
        store,
        clock,
        suffix=suffix,
        expires_in=expires_in,
    )
    return _approve(store, clock, draft, target)


def _trusted_variant(
    scenario: ApprovedScenario,
    *,
    current_target: TargetBinding | None = None,
    consume_request_id: str | None = None,
    user_id: str | None = None,
    proposal_fingerprint: str | None = None,
    target_observed_at: float | None = None,
) -> tuple[VerifiedSimulationApproval, SimulationConsumeRequest]:
    """Re-issue signed test evidence after an intentional mutation."""
    approval = scenario.approval
    proposal = proposal_fingerprint or approval.proposal_fingerprint
    return _TEST_TRUST.issue(
        user_id=user_id or approval.user_id,
        principal_binding_digest=approval.principal_binding_digest,
        confirmation_request_id=approval.confirmation_request_id,
        confirmation_result_id=approval.confirmation_result_id,
        proposal_fingerprint=proposal,
        verified_at=approval.verified_at,
        approval_expires_at=approval.expires_at,
        consume_request_id=(
            consume_request_id or scenario.request.consume_request_id
        ),
        current_target=current_target or scenario.target,
        target_observed_at=(
            approval.verified_at
            if target_observed_at is None
            else target_observed_at
        ),
    )


def _rewrite_as_exact_v3(database) -> None:
    """Build an authentic legacy fixture from current confirmation rows."""
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        terminal_rows = [
            dict(row)
            for row in connection.execute(
                'SELECT * FROM monitor_room_simulation_ledger'
            ).fetchall()
        ]
        eligible_rows = [
            dict(row)
            for row in connection.execute(
                '''
                SELECT confirmation.rowid AS confirmation_rowid,
                       confirmation.confirmation_request_id,
                       confirmation.proposal_fingerprint,
                       confirmation.target_binding_digest,
                       confirmation.effects_digest,
                       confirmation.created_at
                FROM confirmation_intents AS confirmation
                '''
            ).fetchall()
        ]
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

        connection.execute(
            execution_ledger._V3_SIMULATION_SCHEMA_METADATA_TABLE_SQL
        )
        connection.execute(
            execution_ledger.SIMULATION_PREACTIVATION_PROPOSALS_TABLE_SQL
        )
        connection.execute(
            execution_ledger.SIMULATION_WRITE_FENCE_TABLE_SQL
        )
        connection.execute(
            execution_ledger._V3_SIMULATION_ELIGIBILITY_TABLE_SQL
        )
        connection.execute(
            execution_ledger._V3_SIMULATION_LEDGER_TABLE_SQL
        )
        activation_epoch = _digest('exact-v3-activation-epoch')
        connection.execute(
            '''
            INSERT INTO monitor_room_simulation_schema_metadata
            VALUES (1, 3, 75.0, ?)
            ''',
            (activation_epoch,),
        )
        connection.execute(
            '''
            INSERT INTO monitor_room_simulation_write_fence
            VALUES (1, 0)
            '''
        )
        for row in eligible_rows:
            connection.execute(
                '''
                INSERT INTO monitor_room_simulation_eligibility (
                    confirmation_request_id, contract_version,
                    activation_epoch, confirmation_rowid,
                    proposal_fingerprint, target_binding_digest,
                    effects_digest, created_at
                ) VALUES (?, 3, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    row['confirmation_request_id'],
                    activation_epoch,
                    row['confirmation_rowid'],
                    row['proposal_fingerprint'],
                    row['target_binding_digest'],
                    row['effects_digest'],
                    row['created_at'],
                ),
            )
        legacy_columns = tuple(
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(monitor_room_simulation_ledger)'
            ).fetchall()
        )
        for row in terminal_rows:
            legacy = dict(row)
            legacy.update(
                {
                    'schema_version': 3,
                    'simulation_profile_revision': (
                        execution_ledger
                        ._LEGACY_SIMULATION_PROFILE_REVISION
                    ),
                    'result_code': (
                        'simulation_succeeded'
                        if row['state'] == 'succeeded'
                        else 'simulation_failed'
                        if row['state'] == 'failed'
                        else row['result_code']
                    ),
                }
            )
            connection.execute(
                'INSERT INTO monitor_room_simulation_ledger ('
                + ', '.join(legacy_columns)
                + ') VALUES ('
                + ', '.join('?' for _column in legacy_columns)
                + ')',
                tuple(legacy[column] for column in legacy_columns),
            )
        for sql in (
            execution_ledger.SIMULATION_APPROVAL_CONSUME_INDEX_SQL,
            execution_ledger.SIMULATION_NO_UPDATE_TRIGGER_SQL,
            execution_ledger.SIMULATION_NO_DELETE_TRIGGER_SQL,
            execution_ledger.SIMULATION_NO_REPLACE_TRIGGER_SQL,
            execution_ledger
            ._V3_SIMULATION_ELIGIBILITY_GUARD_TRIGGER_SQL,
            execution_ledger.SIMULATION_ELIGIBILITY_NO_UPDATE_TRIGGER_SQL,
            execution_ledger.SIMULATION_METADATA_NO_UPDATE_TRIGGER_SQL,
            execution_ledger.SIMULATION_METADATA_NO_DELETE_TRIGGER_SQL,
            execution_ledger.SIMULATION_METADATA_NO_REPLACE_TRIGGER_SQL,
            execution_ledger
            .SIMULATION_PREACTIVATION_NO_UPDATE_TRIGGER_SQL,
            execution_ledger
            .SIMULATION_PREACTIVATION_NO_DELETE_TRIGGER_SQL,
            execution_ledger
            .SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL,
        ):
            connection.execute(sql)
        connection.commit()
    finally:
        connection.close()


def test_schema_initializes_and_new_confirmation_gets_marker(
    tmp_path,
) -> None:
    """Initialize exact ledger objects and mark only a new proposal."""
    database = tmp_path / 'simulation-schema.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    try:
        draft, target = _commit_confirmation(store, clock)
        objects = {
            row['name']: row['type']
            for row in store._connection.execute(
                '''
                SELECT name, type FROM sqlite_master
                WHERE name LIKE 'monitor_room_simulation_%'
                '''
            ).fetchall()
        }
        assert objects == {
            'monitor_room_simulation_schema_metadata': 'table',
            'monitor_room_simulation_preactivation_proposals': 'table',
            'monitor_room_simulation_write_fence': 'table',
            'monitor_room_simulation_eligibility': 'table',
            'monitor_room_simulation_ledger': 'table',
            'monitor_room_simulation_approval_consume_idx': 'index',
            'monitor_room_simulation_no_update': 'trigger',
            'monitor_room_simulation_no_delete': 'trigger',
            'monitor_room_simulation_no_replace': 'trigger',
            'monitor_room_simulation_eligibility_guard': 'trigger',
            'monitor_room_simulation_eligibility_no_update': 'trigger',
            'monitor_room_simulation_metadata_no_update': 'trigger',
            'monitor_room_simulation_metadata_no_delete': 'trigger',
            'monitor_room_simulation_metadata_no_replace': 'trigger',
            'monitor_room_simulation_preactivation_no_update': 'trigger',
            'monitor_room_simulation_preactivation_no_delete': 'trigger',
            'monitor_room_simulation_preactivation_no_insert': 'trigger',
        }
        marker = store._connection.execute(
            '''
            SELECT * FROM monitor_room_simulation_eligibility
            WHERE confirmation_request_id = ?
            ''',
            (draft.confirmation_request_id,),
        ).fetchone()
        assert marker['contract_version'] == 4
        assert marker['confirmation_rowid'] > 0
        assert len(marker['activation_epoch']) == 64
        assert marker['proposal_fingerprint'] == draft.proposal_fingerprint
        assert marker['target_binding_digest'] == target.binding_digest
        assert marker['effects_digest'] == target.effects_digest
    finally:
        store.close()

    reopened = _simulation_store(str(database), clock=clock)
    reopened.close()


def test_production_default_refuses_even_valid_test_hmac_evidence() -> None:
    """Keep simulation consumption disabled without configured verifier."""
    clock = MutableClock()
    store = SQLiteConversationStore(':memory:', clock=clock)
    try:
        scenario = _scenario(store, clock)
        with pytest.raises(SimulationAssuranceError):
            store.consume_approved_monitor_room_simulation(
                approval=scenario.approval,
                request=scenario.request,
            )
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_store_requires_both_execution_and_receipt_verification() -> None:
    """Reject a partial verifier at configuration time."""

    class FreshOnlyVerifier:
        """Deliberately incomplete verifier fixture."""

        @staticmethod
        def verify(approval, request, now):
            """Return a target without authenticating a receipt."""
            return request.current_target

    with pytest.raises(TypeError):
        SQLiteConversationStore(
            ':memory:',
            simulation_execution_verifier=FreshOnlyVerifier(),
        )


def test_new_proposal_survives_wall_clock_rollback_after_activation() -> None:
    """Use the proposal snapshot, not wall time, as the activation gate."""
    clock = MutableClock(100.0)
    store = _simulation_store(':memory:', clock=clock)
    try:
        clock.now = 50.0
        scenario = _scenario(store, clock, suffix='clock-rollback')
        result = store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        assert result.state == 'succeeded'
        assert result.simulation_authority_issued is True
    finally:
        store.close()


def test_public_objects_without_hmac_proof_cannot_consume() -> None:
    """Reject structurally valid actor and target objects made by a caller."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    try:
        scenario = _scenario(store, clock)
        forged_approval = VerifiedSimulationApproval(
            user_id=scenario.approval.user_id,
            principal_binding_digest=(
                scenario.approval.principal_binding_digest
            ),
            confirmation_request_id=(
                scenario.approval.confirmation_request_id
            ),
            confirmation_result_id=(
                scenario.approval.confirmation_result_id
            ),
            proposal_fingerprint=(
                scenario.approval.proposal_fingerprint
            ),
            verified_at=scenario.approval.verified_at,
            expires_at=scenario.approval.expires_at,
        )
        forged_request = SimulationConsumeRequest(
            consume_request_id=scenario.request.consume_request_id,
            confirmation_request_id=(
                scenario.request.confirmation_request_id
            ),
            confirmation_result_id=(
                scenario.request.confirmation_result_id
            ),
            proposal_fingerprint=(
                scenario.request.proposal_fingerprint
            ),
            current_target=scenario.target,
            target_observed_at=clock.now,
            target_evidence_expires_at=clock.now + 5.0,
        )
        with pytest.raises(SimulationAssuranceError):
            store.consume_approved_monitor_room_simulation(
                approval=forged_approval,
                request=forged_request,
            )
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    'mutation',
    ['verified_at', 'expires_at', 'target', 'observed_at'],
)
def test_hmac_binds_approval_times_and_fresh_target(
    mutation,
) -> None:
    """Reject any post-issuance mutation of signed trust evidence."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    try:
        scenario = _scenario(store, clock)
        approval = scenario.approval
        request = scenario.request
        if mutation == 'verified_at':
            approval = replace(
                approval,
                verified_at=approval.verified_at + 0.25,
            )
        elif mutation == 'expires_at':
            approval = replace(
                approval,
                expires_at=approval.expires_at + 1.0,
            )
        elif mutation == 'target':
            request = replace(
                request,
                current_target=replace(
                    scenario.target,
                    source_revision='forged-target-revision',
                ),
            )
        else:
            request = replace(
                request,
                target_observed_at=request.target_observed_at + 0.25,
            )
        with pytest.raises(SimulationAssuranceError):
            store.consume_approved_monitor_room_simulation(
                approval=approval,
                request=request,
            )
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_signed_target_evidence_must_still_be_fresh() -> None:
    """Do not treat a valid signature as an unlimited freshness claim."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    try:
        scenario = _scenario(store, clock)
        clock.now = scenario.request.target_evidence_expires_at
        with pytest.raises(SimulationAssuranceError):
            store.consume_approved_monitor_room_simulation(
                approval=scenario.approval,
                request=scenario.request,
            )
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_pre_activation_approval_is_permanently_tombstoned(
    monkeypatch,
) -> None:
    """Refuse to upgrade an approval created outside this contract."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda target: calls.append(target),
    )
    try:
        scenario = _scenario(store, clock)
        store._connection.execute(
            '''
            DELETE FROM monitor_room_simulation_eligibility
            WHERE confirmation_request_id = ?
            ''',
            (scenario.draft.confirmation_request_id,),
        )
        store._connection.commit()

        first = store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        replay = store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )

        assert first.state == 'invalidated'
        assert first.result_code == 'simulation_binding_upgrade_required'
        assert first.tool_call_id is None
        assert first.simulation_authority_issued is False
        assert replay.result_code == first.result_code
        assert replay.replayed is True
        assert calls == []
    finally:
        store.close()


def test_preactivation_denylist_allows_safe_rowid_reuse(
    tmp_path,
) -> None:
    """Deny old proposals without denying a new proposal on reused rowid."""
    database = tmp_path / 'simulation-preactivation-denylist.sqlite3'
    clock = MutableClock()
    first = _simulation_store(str(database), clock=clock)
    draft, target = _commit_confirmation(first, clock)
    first.close()

    connection = sqlite3.connect(str(database))
    objects = connection.execute(
        '''
        SELECT type, name FROM sqlite_master
        WHERE name LIKE 'monitor_room_simulation_%'
          AND type = 'trigger'
        '''
    ).fetchall()
    for object_type, name in objects:
        connection.execute(f'DROP {object_type.upper()} "{name}"')
    connection.execute(
        'DROP INDEX monitor_room_simulation_approval_consume_idx'
    )
    connection.execute('DROP TABLE monitor_room_simulation_ledger')
    connection.execute('DROP TABLE monitor_room_simulation_eligibility')
    connection.execute('DROP TABLE monitor_room_simulation_write_fence')
    connection.execute(
        'DROP TABLE monitor_room_simulation_preactivation_proposals'
    )
    connection.execute(
        'DROP TABLE monitor_room_simulation_schema_metadata'
    )
    connection.commit()
    connection.close()

    store = _simulation_store(str(database), clock=clock)
    try:
        metadata = store._connection.execute(
            'SELECT * FROM monitor_room_simulation_schema_metadata'
        ).fetchone()
        confirmation = store._connection.execute(
            '''
            SELECT rowid AS confirmation_rowid, *
            FROM confirmation_intents
            WHERE confirmation_request_id = ?
            ''',
            (draft.confirmation_request_id,),
        ).fetchone()
        denied = store._connection.execute(
            '''
            SELECT * FROM monitor_room_simulation_preactivation_proposals
            WHERE proposal_fingerprint = ?
            ''',
            (draft.proposal_fingerprint,),
        ).fetchone()
        assert denied['snapshot_rowid'] == confirmation['confirmation_rowid']
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                '''
                INSERT INTO monitor_room_simulation_preactivation_proposals (
                    proposal_fingerprint,
                    activation_epoch,
                    snapshot_rowid,
                    snapshotted_at
                ) VALUES (?, ?, 999, ?)
                ''',
                (
                    _digest('post-activation-denial-forgery'),
                    metadata['activation_epoch'],
                    metadata['activated_at'],
                ),
            )
        store._connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                '''
                DELETE FROM monitor_room_simulation_preactivation_proposals
                WHERE proposal_fingerprint = ?
                ''',
                (draft.proposal_fingerprint,),
            )
        store._connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
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
                ) VALUES (?, 3, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    draft.confirmation_request_id,
                    metadata['activation_epoch'],
                    confirmation['confirmation_rowid'],
                    draft.proposal_fingerprint,
                    target.binding_digest,
                    target.effects_digest,
                    confirmation['created_at'],
                ),
            )
        store._connection.rollback()

        scenario = _approve(store, clock, draft, target)
        result = store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        assert result.state == 'invalidated'
        assert result.result_code == 'simulation_binding_upgrade_required'
        assert result.simulation_authority_issued is False

        old_rowid = confirmation['confirmation_rowid']
        assert store.delete(draft.user_id, draft.conversation_id)
        new_draft, new_target = _commit_confirmation(
            store,
            clock,
            suffix='new-after-activation',
        )
        new_row = store._connection.execute(
            '''
            SELECT rowid AS confirmation_rowid
            FROM confirmation_intents
            WHERE confirmation_request_id = ?
            ''',
            (new_draft.confirmation_request_id,),
        ).fetchone()
        assert new_row['confirmation_rowid'] == old_rowid
        new_marker = store._connection.execute(
            '''
            SELECT proposal_fingerprint
            FROM monitor_room_simulation_eligibility
            WHERE confirmation_request_id = ?
            ''',
            (new_draft.confirmation_request_id,),
        ).fetchone()
        assert new_marker['proposal_fingerprint'] == (
            new_draft.proposal_fingerprint
        )
        new_scenario = _approve(store, clock, new_draft, new_target)
        succeeded = store.consume_approved_monitor_room_simulation(
            approval=new_scenario.approval,
            request=new_scenario.request,
        )
        assert succeeded.state == 'succeeded'
        assert succeeded.simulation_authority_issued is True
    finally:
        store.close()


def test_happy_execution_is_private_and_exact_replay_survives_restart(
    tmp_path,
    monkeypatch,
) -> None:
    """Persist one private receipt and replay it without rerunning work."""
    database = tmp_path / 'simulation-replay.sqlite3'
    clock = MutableClock()
    calls = []
    original = execution_ledger.build_monitor_room_coverage_plan

    def observed(target):
        calls.append(target.binding_digest)
        return original(target)

    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        observed,
    )
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock)
    before = store.get_confirmation_intent(
        scenario.draft.user_id,
        scenario.draft.confirmation_request_id,
    ).to_public_dict()['authority']
    first = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    after = store.get_confirmation_intent(
        scenario.draft.user_id,
        scenario.draft.confirmation_request_id,
    ).to_public_dict()['authority']
    store.close()

    reopened = _simulation_store(str(database), clock=clock)
    try:
        replay = reopened.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        public = first.to_public_dict()
        encoded = json.dumps(public, sort_keys=True)

        assert first.state == 'succeeded'
        assert first.result_code == 'semantic_sample_plan_created'
        assert first.record_kind == 'planned'
        assert first.schema_version == 4
        assert first.planner_revision == PLANNER_REVISION
        assert first.profile_digest == DEFAULT_COVERAGE_PROFILE.digest
        assert first.plan_digest is not None
        assert first.result_digest is not None
        assert first.receipt_digest is not None
        assert first.sample_count == 49
        assert first.component_count == 1
        assert first.tool_call_id.startswith('simulation-tool-')
        assert first.mission_id.startswith('simulation-mission-')
        assert first.operation_id.startswith('simulation-operation-')
        assert first.simulation_authority_issued is True
        assert first.replayed is False
        assert replay.replayed is True
        assert replay.tool_call_id == first.tool_call_id
        assert replay.mission_id == first.mission_id
        assert replay.operation_id == first.operation_id
        assert replay.plan_digest == first.plan_digest
        assert replay.result_digest == first.result_digest
        assert replay.receipt_digest == first.receipt_digest
        assert replay.sample_count == first.sample_count
        assert replay.component_count == first.component_count
        assert calls == [scenario.target.binding_digest]
        assert before == after == {
            'kind': 'none',
            'eligible_for_execution': False,
            'execution_authorized': False,
            'consume_once': False,
            'tool_call_id': None,
            'mission_id': None,
        }
        assert public['simulation'] is True
        assert public['physical_effects'] is False
        assert public['viewer_live'] is False
        assert public['nav2_validated'] is False
        assert public['camera_coverage_validated'] is False
        assert public['coverage_achieved'] is False
        assert public['execution_authorized'] is False
        assert public['coverage_plan'] == {
            'planner_revision': PLANNER_REVISION,
            'profile_digest': DEFAULT_COVERAGE_PROFILE.digest,
            'plan_digest': first.plan_digest,
            'result_digest': first.result_digest,
            'sample_count': 49,
            'component_count': 1,
        }
        assert 'x_mm' not in encoded
        assert 'y_mm' not in encoded
        assert 'samples' not in encoded
        for private in (
            scenario.draft.confirmation_request_id,
            scenario.draft.proposal_fingerprint,
            scenario.target.device_id,
            scenario.target.map_id,
            scenario.target.room_id,
            scenario.target.binding_digest,
            scenario.target.effects_digest,
            scenario.approval.principal_binding_digest,
        ):
            assert private not in encoded
    finally:
        reopened.close()


def test_negative_zero_time_is_canonical_across_sqlite_restart(
    tmp_path,
) -> None:
    """Canonicalize signed zero before hashing a durable receipt."""
    database = tmp_path / 'simulation-negative-zero.sqlite3'
    clock = MutableClock(-0.0)
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='negative-zero')
    first = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store.close()

    reopened = _simulation_store(str(database), clock=clock)
    try:
        replay = reopened.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )

        assert first.state == 'succeeded'
        assert first.completed_at == 0.0
        assert replay.replayed is True
        assert replay.completed_at == 0.0
        assert replay.receipt_digest == first.receipt_digest
    finally:
        reopened.close()


def test_v3_terminal_migrates_to_audit_only_without_plan_authority(
    tmp_path,
    monkeypatch,
) -> None:
    """Preserve a v3 receipt without inventing v4 coverage evidence."""
    database = tmp_path / 'simulation-v3-terminal.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='legacy-terminal')
    original = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    assert store.delete(
        scenario.draft.user_id,
        scenario.draft.conversation_id,
    )
    store.close()
    _rewrite_as_exact_v3(database)

    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda target: calls.append(target),
    )
    migrated = _simulation_store(str(database), clock=clock)
    try:
        metadata = migrated._connection.execute(
            'SELECT * FROM monitor_room_simulation_schema_metadata'
        ).fetchone()
        row = migrated._connection.execute(
            'SELECT * FROM monitor_room_simulation_ledger'
        ).fetchone()
        denied = migrated._connection.execute(
            '''
            SELECT proposal_fingerprint
            FROM monitor_room_simulation_preactivation_proposals
            WHERE proposal_fingerprint = ?
            ''',
            (scenario.draft.proposal_fingerprint,),
        ).fetchone()

        assert metadata['schema_version'] == 4
        assert row['schema_version'] == 3
        assert row['record_kind'] == 'legacy_unplanned'
        assert row['result_code'] == 'simulation_succeeded'
        assert row['tool_call_id'] == original.tool_call_id
        assert row['mission_id'] == original.mission_id
        assert row['operation_id'] == original.operation_id
        assert row['planner_revision'] is None
        assert row['profile_digest'] is None
        assert row['plan_digest'] is None
        assert row['result_digest'] is None
        assert row['sample_count'] is None
        assert row['component_count'] is None
        assert row['receipt_digest'] is None
        assert row['nav2_validated'] == 0
        assert row['camera_coverage_validated'] == 0
        assert row['coverage_achieved'] == 0
        assert denied is not None

        with pytest.raises(
            SimulationExecutionContractUpgradeRequiredError
        ):
            migrated.consume_approved_monitor_room_simulation(
                approval=scenario.approval,
                request=scenario.request,
            )
        assert calls == []
        assert migrated._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 1
    finally:
        migrated.close()


def test_pending_v3_confirmation_is_denylisted_during_v4_migration(
    tmp_path,
    monkeypatch,
) -> None:
    """Require a new confirmation rather than upgrading pending v3 scope."""
    database = tmp_path / 'simulation-v3-pending.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    draft, target = _commit_confirmation(
        store,
        clock,
        suffix='legacy-pending',
    )
    store.close()
    _rewrite_as_exact_v3(database)

    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda current: calls.append(current),
    )
    migrated = _simulation_store(str(database), clock=clock)
    try:
        assert migrated._connection.execute(
            '''
            SELECT COUNT(*) FROM monitor_room_simulation_eligibility
            WHERE confirmation_request_id = ?
            ''',
            (draft.confirmation_request_id,),
        ).fetchone()[0] == 0
        assert migrated._connection.execute(
            '''
            SELECT COUNT(*)
            FROM monitor_room_simulation_preactivation_proposals
            WHERE proposal_fingerprint = ?
            ''',
            (draft.proposal_fingerprint,),
        ).fetchone()[0] == 1

        scenario = _approve(migrated, clock, draft, target)
        result = migrated.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        assert result.state == 'invalidated'
        assert result.record_kind == 'invalidated'
        assert result.result_code == 'simulation_binding_upgrade_required'
        assert result.simulation_authority_issued is False
        assert result.planner_revision is None
        assert result.plan_digest is None
        assert result.result_digest is None
        assert calls == []
    finally:
        migrated.close()


def test_v3_migration_fault_rolls_back_and_releases_writer_lock(
    tmp_path,
    monkeypatch,
) -> None:
    """Restore exact v3 state after a mid-DDL migration exception."""
    database = tmp_path / 'simulation-v3-fault.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    _commit_confirmation(store, clock, suffix='legacy-fault')
    store.close()
    _rewrite_as_exact_v3(database)

    create_v4 = execution_ledger._create_v4_schema_locked

    def fault_after_create(connection, **kwargs):
        create_v4(connection, **kwargs)
        raise RuntimeError('injected migration fault')

    monkeypatch.setattr(
        execution_ledger,
        '_create_v4_schema_locked',
        fault_after_create,
    )
    with pytest.raises(RuntimeError, match='injected migration fault'):
        _simulation_store(str(database), clock=clock)

    probe = sqlite3.connect(str(database), timeout=0.1)
    try:
        assert probe.execute(
            'SELECT schema_version '
            'FROM monitor_room_simulation_schema_metadata'
        ).fetchone()[0] == 3
        probe.execute('BEGIN IMMEDIATE')
        probe.rollback()
    finally:
        probe.close()

    monkeypatch.setattr(
        execution_ledger,
        '_create_v4_schema_locked',
        create_v4,
    )
    migrated = _simulation_store(str(database), clock=clock)
    try:
        assert migrated._connection.execute(
            'SELECT schema_version '
            'FROM monitor_room_simulation_schema_metadata'
        ).fetchone()[0] == 4
    finally:
        migrated.close()


def test_v3_migration_rejects_malformed_terminal_time(
    tmp_path,
) -> None:
    """Authenticate weak legacy REAL affinity before copying any receipt."""
    database = tmp_path / 'simulation-v3-time-tamper.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='legacy-time-tamper')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store.close()
    _rewrite_as_exact_v3(database)

    connection = sqlite3.connect(str(database))
    connection.execute('DROP TRIGGER monitor_room_simulation_no_update')
    connection.execute(
        'UPDATE monitor_room_simulation_ledger SET completed_at = -99.5'
    )
    connection.execute(
        execution_ledger.SIMULATION_NO_UPDATE_TRIGGER_SQL
    )
    connection.commit()
    connection.close()

    with pytest.raises(SimulationExecutionSchemaError):
        _simulation_store(str(database), clock=clock)


def test_exact_receipt_replay_does_not_require_fresh_target_evidence(
    monkeypatch,
) -> None:
    """Replay an immutable receipt without granting another execution."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    calls = []
    original = execution_ledger.build_monitor_room_coverage_plan

    def observed(target):
        calls.append(target.binding_digest)
        return original(target)

    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        observed,
    )
    try:
        scenario = _scenario(store, clock)
        first = store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        assert calls == [scenario.target.binding_digest]

        clock.now = scenario.request.target_evidence_expires_at
        replay = store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )

        assert replay.replayed is True
        assert replay.tool_call_id == first.tool_call_id
        assert replay.mission_id == first.mission_id
        assert replay.operation_id == first.operation_id
        assert calls == [scenario.target.binding_digest]
    finally:
        store.close()


def test_changed_consume_conflicts_and_wrong_selectors_are_not_found(
) -> None:
    """Hide wrong owners and proposals while fencing changed retries."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    try:
        scenario = _scenario(store, clock)
        store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        changed_target = replace(
            scenario.target,
            source_revision='simulation-source-changed',
        )
        changed_approval, changed = _trusted_variant(
            scenario,
            current_target=changed_target,
        )
        with pytest.raises(SimulationConsumeConflictError):
            store.consume_approved_monitor_room_simulation(
                approval=changed_approval,
                request=changed,
            )

        wrong_actor, wrong_actor_request = _trusted_variant(
            scenario,
            user_id='wrong-simulation-user',
        )
        with pytest.raises(SimulationExecutionNotFoundError):
            store.consume_approved_monitor_room_simulation(
                approval=wrong_actor,
                request=wrong_actor_request,
            )
        wrong_proposal = _digest('wrong-proposal')
        wrong_approval, wrong_request = _trusted_variant(
            scenario,
            proposal_fingerprint=wrong_proposal,
            consume_request_id='simulation-consume-wrong-proposal',
        )
        with pytest.raises(SimulationExecutionNotFoundError):
            store.consume_approved_monitor_room_simulation(
                approval=wrong_approval,
                request=wrong_request,
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    ('planner_result', 'expected_code'),
    [
        (RuntimeError('private geometry detail'),
         'semantic_sample_planning_failed'),
        (object(), 'semantic_sample_result_invalid'),
    ],
)
def test_planner_failure_is_typed_terminal_and_exactly_replayed(
    tmp_path,
    monkeypatch,
    planner_result,
    expected_code,
) -> None:
    """Persist no raw failure or partial plan and never rerun a replay."""
    database = tmp_path / f'{expected_code}.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix=expected_code)
    calls = []

    def fail_or_return(target):
        calls.append(target.binding_digest)
        if isinstance(planner_result, Exception):
            raise planner_result
        return planner_result

    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        fail_or_return,
    )
    first = store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store.close()
    reopened = _simulation_store(str(database), clock=clock)
    try:
        replay = reopened.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        encoded = json.dumps(first.to_public_dict(), sort_keys=True)
        assert first.state == 'failed'
        assert first.record_kind == 'planning_failed'
        assert first.result_code == expected_code
        assert first.simulation_authority_issued is True
        assert first.planner_revision == PLANNER_REVISION
        assert first.profile_digest == DEFAULT_COVERAGE_PROFILE.digest
        assert first.plan_digest is None
        assert first.result_digest is not None
        assert first.sample_count == 0
        assert first.component_count == 0
        assert replay.replayed is True
        assert replay.result_digest == first.result_digest
        assert calls == [scenario.target.binding_digest]
        assert 'private geometry detail' not in encoded
        assert 'x_mm' not in encoded
        assert first.to_public_dict()['coverage_achieved'] is False
    finally:
        reopened.close()


def test_planner_profile_is_bound_before_receipt_or_planner_use(
    monkeypatch,
) -> None:
    """Fail closed if a frozen request is bypass-mutated after signing."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda target: calls.append(target),
    )
    try:
        scenario = _scenario(store, clock, suffix='profile-drift')
        with pytest.raises(ValidationError):
            replace(
                scenario.request,
                profile_digest=_digest('changed-profile'),
            )
        object.__setattr__(
            scenario.request,
            'profile_revision',
            'changed-planner-revision',
        )
        with pytest.raises(SimulationAssuranceError):
            store.consume_approved_monitor_room_simulation(
                approval=scenario.approval,
                request=scenario.request,
            )
        assert calls == []
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_planner_cached_plan_digest_mutation_is_typed_invalid_result(
    monkeypatch,
) -> None:
    """Rebuild core values instead of trusting a frozen cached digest."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    scenario = _scenario(store, clock, suffix='cached-plan-digest')
    planner = execution_ledger.build_monitor_room_coverage_plan

    def forged(target):
        honest = planner(target)
        object.__setattr__(
            honest.plan,
            '_plan_digest',
            _digest('forged-plan-digest'),
        )
        return CoveragePlanningResult(plan=honest.plan)

    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        forged,
    )
    try:
        result = store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        assert result.state == 'failed'
        assert result.record_kind == 'planning_failed'
        assert result.result_code == 'semantic_sample_result_invalid'
        assert result.plan_digest is None
        assert result.sample_count == 0
        assert result.component_count == 0
    finally:
        store.close()


def test_terminal_insert_failure_rolls_back_planner_transaction(
    monkeypatch,
) -> None:
    """Leave no partial receipt when persistence fails after pure planning."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    scenario = _scenario(store, clock, suffix='planner-rollback')
    planner = execution_ledger.build_monitor_room_coverage_plan
    insert_terminal = execution_ledger._insert_terminal_locked
    calls = []

    def observed(target):
        calls.append(target.binding_digest)
        return planner(target)

    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        observed,
    )
    monkeypatch.setattr(
        execution_ledger,
        '_insert_terminal_locked',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError('terminal persistence failed')
        ),
    )
    try:
        with pytest.raises(RuntimeError, match='terminal persistence failed'):
            store.consume_approved_monitor_room_simulation(
                approval=scenario.approval,
                request=scenario.request,
            )
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 0

        monkeypatch.setattr(
            execution_ledger,
            '_insert_terminal_locked',
            insert_terminal,
        )
        succeeded = store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )
        assert succeeded.state == 'succeeded'
        assert calls == [
            scenario.target.binding_digest,
            scenario.target.binding_digest,
        ]
    finally:
        store.close()


def test_exact_confirmation_deadline_is_an_immutable_tombstone(
    monkeypatch,
) -> None:
    """Make the exact durable deadline non-executable forever."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda target: calls.append(target),
    )
    try:
        scenario = _scenario(store, clock, expires_in=10.0)
        clock.now = scenario.draft.expires_at
        deadline_approval, deadline_request = _trusted_variant(
            scenario,
            target_observed_at=clock.now,
        )
        result = store.consume_approved_monitor_room_simulation(
            approval=deadline_approval,
            request=deadline_request,
        )
        assert result.state == 'invalidated'
        assert result.result_code == 'simulation_confirmation_expired'
        assert result.tool_call_id is None
        clock.now = deadline_request.target_evidence_expires_at
        replay = store.consume_approved_monitor_room_simulation(
            approval=deadline_approval,
            request=deadline_request,
        )
        assert replay.replayed is True
        assert replay.receipt_digest == result.receipt_digest
        assert calls == []
    finally:
        store.close()


@pytest.mark.parametrize('change', ['target', 'effects'])
def test_observed_change_is_permanent_after_state_returns_to_original(
    change,
    monkeypatch,
) -> None:
    """Keep an observed target or effects mismatch permanently terminal."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda target: calls.append(target),
    )
    try:
        scenario = _scenario(store, clock)
        if change == 'target':
            changed_target = replace(
                scenario.target,
                source_revision='simulation-source-b',
            )
            expected = 'simulation_target_changed'
        else:
            changed_effects = replace(
                scenario.target.effects,
                video_recording=True,
            )
            changed_target = replace(
                scenario.target,
                effects=changed_effects,
            )
            expected = 'simulation_effects_changed'
        changed_approval, changed_request = _trusted_variant(
            scenario,
            current_target=changed_target,
        )
        tombstone = store.consume_approved_monitor_room_simulation(
            approval=changed_approval,
            request=changed_request,
        )
        assert tombstone.state == 'invalidated'
        assert tombstone.result_code == expected
        exact_replay = store.consume_approved_monitor_room_simulation(
            approval=changed_approval,
            request=changed_request,
        )
        assert exact_replay.replayed is True
        assert exact_replay.receipt_digest == tombstone.receipt_digest

        restored_approval, restored_request = _trusted_variant(
            scenario,
            consume_request_id='simulation-consume-restored-a',
            current_target=scenario.target,
        )
        with pytest.raises(SimulationExecutionAlreadyConsumedError):
            store.consume_approved_monitor_room_simulation(
                approval=restored_approval,
                request=restored_request,
            )
        persisted = store._connection.execute(
            '''
            SELECT state, result_code
            FROM monitor_room_simulation_ledger
            WHERE confirmation_request_id = ?
            ''',
            (scenario.draft.confirmation_request_id,),
        ).fetchone()
        assert tuple(persisted) == ('invalidated', expected)
        assert calls == []
    finally:
        store.close()


@pytest.mark.parametrize(
    ('mutation', 'expected'),
    [
        ('reset', 'simulation_conversation_changed'),
        ('close_session', 'simulation_conversation_inactive'),
    ],
)
def test_lifecycle_change_before_consume_never_runs_simulation(
    mutation,
    expected,
    monkeypatch,
) -> None:
    """Fence reset and close before the simulation boundary."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda target: calls.append(target),
    )
    try:
        scenario = _scenario(store, clock)
        lifecycle = getattr(store, mutation)
        lifecycle(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
        )

        result = store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )

        assert result.state == 'invalidated'
        assert result.result_code == expected
        assert result.simulation_authority_issued is False
        assert result.tool_call_id is None
        assert calls == []
    finally:
        store.close()


def test_session_expiry_at_fresh_sample_prevents_simulation(
    monkeypatch,
) -> None:
    """Sweep session TTL again at the final execution-boundary sample."""
    clock = QueuedClock()
    store = _simulation_store(
        ':memory:',
        clock=clock,
        ttl_seconds=60,
    )
    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda target: calls.append(target),
    )
    try:
        scenario = _scenario(store, clock, expires_in=300.0)
        approval, request = _trusted_variant(
            scenario,
            target_observed_at=159.0,
        )
        clock.samples = [159.0, 160.0]
        result = store.consume_approved_monitor_room_simulation(
            approval=approval,
            request=request,
        )
        assert result.state == 'invalidated'
        assert result.result_code == 'simulation_conversation_inactive'
        assert result.simulation_authority_issued is False
        assert calls == []
    finally:
        store.close()


def test_target_evidence_expiring_at_fresh_sample_rolls_back(
    monkeypatch,
) -> None:
    """Recheck signed semantic evidence immediately before simulation."""
    clock = QueuedClock()
    store = _simulation_store(':memory:', clock=clock)
    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda target: calls.append(target),
    )
    try:
        scenario = _scenario(store, clock, expires_in=300.0)
        clock.samples = [104.9, 105.0]
        with pytest.raises(SimulationAssuranceError):
            store.consume_approved_monitor_room_simulation(
                approval=scenario.approval,
                request=scenario.request,
            )
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 0
        assert calls == []
    finally:
        store.close()


def test_delete_before_consume_is_not_found_and_creates_no_receipt(
    monkeypatch,
) -> None:
    """Refuse a deleted confirmation without manufacturing authority."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    calls = []
    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        lambda target: calls.append(target),
    )
    try:
        scenario = _scenario(store, clock)
        assert store.delete(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
        )

        with pytest.raises(SimulationExecutionNotFoundError):
            store.consume_approved_monitor_room_simulation(
                approval=scenario.approval,
                request=scenario.request,
            )
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 0
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_eligibility'
        ).fetchone()[0] == 0
        assert calls == []
    finally:
        store.close()


def test_terminal_ledger_reserves_confirmation_and_decision_ids() -> None:
    """Prevent deleted content rows from reopening a spent identity."""
    clock = MutableClock()
    store = _simulation_store(':memory:', clock=clock)
    try:
        scenario = _scenario(store, clock)
        store.consume_approved_monitor_room_simulation(
            approval=scenario.approval,
            request=scenario.request,
        )

        with store._lock:
            store._begin()
            replay = store._insert_confirmation_intent_locked(
                scenario.draft,
                clock.now,
            )
            store._connection.commit()
        assert replay.state == 'resolved'
        assert replay.confirmation_result_id == (
            scenario.approval.confirmation_result_id
        )

        assert store.delete(
            scenario.draft.user_id,
            scenario.draft.conversation_id,
        )
        with pytest.raises(ConfirmationIntentConflictError):
            _commit_confirmation(store, clock, suffix='one')
        assert store._connection.execute(
            'SELECT COUNT(*) FROM confirmation_intents'
        ).fetchone()[0] == 0
        assert store._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_sixteen_stores_linearize_to_one_receipt_and_one_simulation(
    tmp_path,
    monkeypatch,
) -> None:
    """Serialize separate connections to one execution and stable IDs."""
    database = tmp_path / 'simulation-concurrency.sqlite3'
    clock = MutableClock()
    creator = _simulation_store(str(database), clock=clock)
    scenario = _scenario(creator, clock)
    creator.close()
    stores = [
        _simulation_store(str(database), clock=clock)
        for _index in range(16)
    ]
    barrier = threading.Barrier(len(stores) + 1)
    result_lock = threading.Lock()
    simulation_lock = threading.Lock()
    results = []
    errors = []
    calls = []
    original = execution_ledger.build_monitor_room_coverage_plan

    def observed(target):
        with simulation_lock:
            calls.append(target.binding_digest)
        return original(target)

    def consume(store) -> None:
        try:
            barrier.wait(timeout=5.0)
            result = store.consume_approved_monitor_room_simulation(
                approval=scenario.approval,
                request=scenario.request,
            )
            with result_lock:
                results.append(result)
        except Exception as error:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(error)

    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        observed,
    )
    threads = [
        threading.Thread(target=consume, args=(store,))
        for store in stores
    ]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5.0)
        for thread in threads:
            thread.join(timeout=10.0)
            assert not thread.is_alive()

        assert errors == []
        assert len(results) == 16
        assert sum(not result.replayed for result in results) == 1
        assert sum(result.replayed for result in results) == 15
        assert {result.state for result in results} == {'succeeded'}
        assert len({result.tool_call_id for result in results}) == 1
        assert len({result.mission_id for result in results}) == 1
        assert len({result.operation_id for result in results}) == 1
        assert calls == [scenario.target.binding_digest]
        assert stores[0]._connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 1
    finally:
        for store in stores:
            store.close()


def test_private_seam_requires_transaction_and_serializes_deferred_callers(
    tmp_path,
    monkeypatch,
) -> None:
    """Fence accidental raw callers before the pure simulator boundary."""
    database = tmp_path / 'simulation-private-seam.sqlite3'
    clock = MutableClock()
    creator = _simulation_store(str(database), clock=clock)
    scenario = _scenario(creator, clock)
    creator.close()

    probe = sqlite3.connect(str(database))
    probe.row_factory = sqlite3.Row
    try:
        with pytest.raises(SimulationAssuranceError):
            execution_ledger._consume_approved_monitor_room_simulation_locked(
                probe,
                approval=scenario.approval,
                request=scenario.request,
                verifier=_TEST_TRUST,
                now=clock.now,
                fresh_clock=clock,
                context_classifier=lambda row, observed_at: None,
            )
    finally:
        probe.close()

    barrier = threading.Barrier(3)
    result_lock = threading.Lock()
    results = []
    errors = []
    calls = []
    original = execution_ledger.build_monitor_room_coverage_plan

    def observed(target):
        with result_lock:
            calls.append(target.binding_digest)
        return original(target)

    def consume_raw() -> None:
        connection = sqlite3.connect(str(database), timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('BEGIN')
            barrier.wait(timeout=5.0)
            result = (
                execution_ledger
                ._consume_approved_monitor_room_simulation_locked(
                    connection,
                    approval=scenario.approval,
                    request=scenario.request,
                    verifier=_TEST_TRUST,
                    now=clock.now,
                    fresh_clock=clock,
                    context_classifier=lambda row, observed_at: None,
                )
            )
            connection.commit()
            with result_lock:
                results.append(result)
        except Exception as error:  # pragma: no cover - asserted below
            connection.rollback()
            with result_lock:
                errors.append(error)
        finally:
            connection.close()

    monkeypatch.setattr(
        execution_ledger,
        'build_monitor_room_coverage_plan',
        observed,
    )
    threads = [threading.Thread(target=consume_raw) for _index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5.0)
    for thread in threads:
        thread.join(timeout=10.0)
        assert not thread.is_alive()

    assert errors == []
    assert len(results) == 2
    assert sum(result.replayed for result in results) == 1
    assert calls == [scenario.target.binding_digest]


def _clone_invalid_ledger_row(
    connection: sqlite3.Connection,
    *,
    changed_column: str,
    changed_value,
    suffix: str,
    replace_existing: bool = False,
    preserve_approval_consume: bool = False,
) -> None:
    columns = tuple(
        row[1]
        for row in connection.execute(
            'PRAGMA table_info(monitor_room_simulation_ledger)'
        ).fetchall()
    )
    source = connection.execute(
        'SELECT * FROM monitor_room_simulation_ledger'
    ).fetchone()
    values = dict(zip(columns, source))
    values.update(
        {
            'confirmation_request_id': f'raw-confirmation-{suffix}',
            'confirmation_result_id': f'raw-result-{suffix}',
            'decision_id': f'raw-decision-{suffix}',
            'consume_request_id': f'raw-consume-{suffix}',
            'tool_call_id': f'raw-tool-{suffix}',
            'mission_id': f'raw-mission-{suffix}',
            'operation_id': f'raw-operation-{suffix}',
            changed_column: changed_value,
        }
    )
    if preserve_approval_consume:
        values['consume_request_id'] = source[
            columns.index('consume_request_id')
        ]
    insert = 'INSERT OR REPLACE' if replace_existing else 'INSERT'
    connection.execute(
        insert + ' INTO monitor_room_simulation_ledger ('
        + ', '.join(columns)
        + ') VALUES ('
        + ', '.join('?' for _column in columns)
        + ')',
        tuple(values[column] for column in columns),
    )


def test_raw_sql_cannot_escalate_mutate_or_delete_terminal_receipt(
    tmp_path,
) -> None:
    """Enforce immutable and simulation-only flags at the SQL layer."""
    database = tmp_path / 'simulation-immutable.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock)
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store.close()

    connection = sqlite3.connect(str(database))
    try:
        columns = tuple(
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(monitor_room_simulation_ledger)'
            ).fetchall()
        )
        original = connection.execute(
            'SELECT * FROM monitor_room_simulation_ledger'
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT OR REPLACE INTO monitor_room_simulation_ledger ('
                + ', '.join(columns)
                + ') VALUES ('
                + ', '.join('?' for _column in columns)
                + ')',
                tuple(original),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            _clone_invalid_ledger_row(
                connection,
                changed_column='completed_at',
                changed_value=101.0,
                suffix='replace-owner-index',
                replace_existing=True,
                preserve_approval_consume=True,
            )
        connection.rollback()
        metadata = connection.execute(
            'SELECT * FROM monitor_room_simulation_schema_metadata'
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                UPDATE monitor_room_simulation_schema_metadata
                SET activation_epoch = activation_epoch
                '''
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'DELETE FROM monitor_room_simulation_schema_metadata'
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                INSERT OR REPLACE INTO
                    monitor_room_simulation_schema_metadata
                VALUES (?, ?, ?, ?)
                ''',
                tuple(metadata),
            )
        connection.rollback()
        denied = connection.execute(
            'SELECT * FROM monitor_room_simulation_preactivation_proposals'
        ).fetchone()
        if denied is not None:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    '''
                    UPDATE monitor_room_simulation_preactivation_proposals
                    SET snapshot_rowid = snapshot_rowid
                    '''
                )
            connection.rollback()
        fence = connection.execute(
            '''
            UPDATE monitor_room_simulation_write_fence
            SET fence = fence WHERE singleton = 1
            '''
        )
        assert fence.rowcount == 1
        connection.rollback()
        for column, value in (
            ('authority_kind', 'physical'),
            ('simulation_authority_issued', 0),
            ('physical_authorized', 1),
            ('physical_effects', 1),
            ('viewer_live', 1),
            ('nav2_validated', 1),
            ('camera_coverage_validated', 1),
            ('coverage_achieved', 1),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f'''
                    UPDATE monitor_room_simulation_ledger
                    SET {column} = ?
                    ''',
                    (value,),
                )
            connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'DELETE FROM monitor_room_simulation_ledger'
            )
        connection.rollback()

        for index, (column, value) in enumerate(
            (
                ('authority_kind', 'physical'),
                ('physical_authorized', 1),
                ('physical_effects', 1),
                ('viewer_live', 1),
                ('nav2_validated', 1),
                ('camera_coverage_validated', 1),
                ('coverage_achieved', 1),
                ('record_kind', 'invalidated'),
                ('planner_revision', 'changed-planner'),
                ('profile_digest', _digest('changed-profile')),
                ('plan_digest', None),
                ('result_digest', None),
                ('sample_count', 0),
                ('component_count', 0),
            )
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _clone_invalid_ledger_row(
                    connection,
                    changed_column=column,
                    changed_value=value,
                    suffix=str(index),
                )
            connection.rollback()
        assert connection.execute(
            'SELECT COUNT(*) FROM monitor_room_simulation_ledger'
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_schema_tamper_fails_closed_without_stranding_writer_lock(
    tmp_path,
) -> None:
    """Reject a missing immutable trigger and release the writer lock."""
    database = tmp_path / 'simulation-schema-tamper.sqlite3'
    store = _simulation_store(str(database))
    store.close()
    connection = sqlite3.connect(str(database))
    connection.execute(
        'DROP TRIGGER monitor_room_simulation_no_update'
    )
    connection.commit()
    connection.close()

    with pytest.raises(SimulationExecutionSchemaError):
        _simulation_store(str(database))

    probe = sqlite3.connect(str(database), timeout=0.1)
    try:
        probe.execute('BEGIN IMMEDIATE')
        probe.rollback()
    finally:
        probe.close()


@pytest.mark.parametrize(
    ('column', 'changed_value'),
    [
        ('result_digest', _digest('tampered-result-digest')),
        ('receipt_digest', _digest('tampered-receipt-digest')),
        ('sample_count', 1),
        ('component_count', 2),
        ('target_binding_digest', _digest('tampered-target')),
        ('effects_digest', _digest('tampered-effects')),
        ('arguments_digest', _digest('tampered-arguments')),
        ('owner_binding_digest', _digest('tampered-owner')),
        ('confirmation_issued_at', 99.5),
        ('confirmation_expires_at', 161.0),
        ('completed_at', 100.5),
        ('tool_call_id', 'simulation-tool-tampered'),
        ('mission_id', 'simulation-mission-tampered'),
        ('operation_id', 'simulation-operation-tampered'),
    ],
)
def test_receipt_tamper_fails_even_after_exact_trigger_restore(
    tmp_path,
    column,
    changed_value,
) -> None:
    """Recompute the complete content-free receipt during every reopen."""
    database = tmp_path / f'simulation-receipt-tamper-{column}.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix='digest-tamper')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store.close()

    connection = sqlite3.connect(str(database))
    connection.execute('DROP TRIGGER monitor_room_simulation_no_update')
    connection.execute(
        f'''
        UPDATE monitor_room_simulation_ledger
        SET {column} = ?
        ''',
        (changed_value,),
    )
    connection.execute(
        execution_ledger.SIMULATION_NO_UPDATE_TRIGGER_SQL
    )
    connection.commit()
    connection.close()

    with pytest.raises(SimulationExecutionSchemaError):
        _simulation_store(str(database), clock=clock)


@pytest.mark.parametrize(
    ('column', 'changed_value'),
    [
        ('sample_count', 1.5),
        ('component_count', 1.5),
        ('confirmation_issued_at', 'a'),
        ('confirmation_expires_at', 'z'),
        ('completed_at', -99.5),
    ],
)
def test_sql_shape_rejects_real_counts_and_invalid_times_without_trigger(
    tmp_path,
    column,
    changed_value,
) -> None:
    """Keep exact numeric storage types and bounded terminal chronology."""
    database = tmp_path / f'simulation-shape-{column}.sqlite3'
    clock = MutableClock()
    store = _simulation_store(str(database), clock=clock)
    scenario = _scenario(store, clock, suffix=f'shape-{column}')
    store.consume_approved_monitor_room_simulation(
        approval=scenario.approval,
        request=scenario.request,
    )
    store.close()

    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            'DROP TRIGGER monitor_room_simulation_no_update'
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f'''
                UPDATE monitor_room_simulation_ledger
                SET {column} = ?
                ''',
                (changed_value,),
            )
        connection.rollback()
        connection.execute(
            execution_ledger.SIMULATION_NO_UPDATE_TRIGGER_SQL
        )
        connection.commit()
    finally:
        connection.close()

    reopened = _simulation_store(str(database), clock=clock)
    reopened.close()


def test_missing_write_fence_or_replace_trigger_fails_schema_validation(
    tmp_path,
) -> None:
    """Reject loss of serialization or INSERT replacement protection."""
    for suffix, statement in (
        (
            'fence',
            'DELETE FROM monitor_room_simulation_write_fence',
        ),
        (
            'replace-trigger',
            'DROP TRIGGER monitor_room_simulation_no_replace',
        ),
        (
            'unexpected-fence-trigger',
            '''
            CREATE TRIGGER unexpected_fence_trigger
            AFTER UPDATE ON monitor_room_simulation_write_fence
            BEGIN
                SELECT 1;
            END
            ''',
        ),
    ):
        database = tmp_path / f'simulation-schema-{suffix}.sqlite3'
        store = _simulation_store(str(database))
        store.close()
        connection = sqlite3.connect(str(database))
        connection.execute(statement)
        connection.commit()
        connection.close()
        with pytest.raises(SimulationExecutionSchemaError):
            _simulation_store(str(database))
