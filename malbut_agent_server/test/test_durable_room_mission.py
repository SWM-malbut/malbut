"""Offline adversarial tests for the durable simulated room controller."""

import hashlib
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import replace

import pytest

import malbut_agent_server.durable_room_mission as durable_room_mission_module
from malbut_agent_server.durable_room_mission import (
    DurableMissionProposalHandle,
    DurableSimulationRoomMission,
    SimulationDeviceBinding,
)
from malbut_agent_server.orchestrator import OrchestrationResult
from malbut_agent_server.room_mission import (
    AdapterStepResult,
    SemanticRoomResolver,
    SimulationPhaseGate,
    SimulationRoomMissionAdapter,
    TrustedMissionState,
    monitor_room_arguments_digest,
    orchestration_authority_digest,
)
from malbut_agent_server.room_mission_ledger import (
    DurableMissionAuthority,
    DurableMissionConfirmation,
    SQLiteRoomMissionStore,
)
from malbut_agent_server.safety import SafetyResult
from malbut_agent_server.schemas import (
    AgentDecision,
    ProviderResult,
    ProviderUsage,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class _Clock:
    """Mutable deterministic wall clock."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _user_map(*, goal_x: float = 2.0) -> dict:
    return {
        'type': 'FeatureCollection',
        'map_id': 'home-a',
        'frame_id': 'map',
        'features': [{
            'type': 'Feature',
            'id': 'room-living',
            'properties': {
                'role': 'room',
                'room_id': 'room-living',
                'name': '거실',
                'category': 'living_room',
                'aliases': ['응접실'],
                'navigation_goal': {
                    'x': goal_x, 'y': 2.0, 'yaw': 0.0,
                },
                'coverage_viewpoints': [
                    {'x': 2.0, 'y': 2.0, 'yaw': 0.0},
                    {'x': 8.0, 'y': 8.0, 'yaw': 3.0},
                ],
            },
            'geometry': {
                'type': 'Polygon',
                'coordinates': [[
                    [0.0, 0.0], [10.0, 0.0], [10.0, 10.0],
                    [0.0, 10.0], [0.0, 0.0],
                ]],
            },
        }],
    }


def _result(clock: _Clock) -> OrchestrationResult:
    decision = AgentDecision(
        type='tool_call',
        message='proposal',
        tool_name='monitor_room',
        arguments={'location': '거실'},
        reason='',
        confidence=1.0,
        expires_in_ms=8000,
    )
    raw = AgentDecision(**decision.__dict__)
    return OrchestrationResult(
        request_id='request-1',
        conversation_id='conversation-1',
        turn_id='turn-1',
        conversation_generation=1,
        conversation_revision=1,
        conversation_ordinal=1,
        raw_decision=raw,
        decision=decision,
        safety=SafetyResult(True, 'allowed', 'fixed'),
        provider_result=ProviderResult(
            decision=raw,
            provider='mock',
            model='mock-v1',
            latency_ms=1.0,
            usage=ProviderUsage(1, 1, 2),
        ),
        memory_ids=[],
        decision_id='decision-1',
        issued_at=clock.value,
        expires_at=clock.value + 8.0,
        state_trusted=True,
        memory_revision=0,
    )


class _Trust:
    """Trusted server-side auth, confirmation, and state seams."""

    def __init__(self, result, clock, resolver) -> None:
        self.result = result
        self.clock = clock
        self.resolver = resolver
        self.authority = DurableMissionAuthority(
            subject_id='owner-1',
            auth_session_id='auth-session-1',
            conversation_id=result.conversation_id,
            conversation_session_instance_id='conversation-instance-1',
            proposal_turn_id=result.turn_id,
            request_id=result.request_id,
            conversation_generation=result.conversation_generation,
            conversation_revision=result.conversation_revision,
            conversation_ordinal=result.conversation_ordinal,
            authority_digest=orchestration_authority_digest(result),
        )
        self.active = True
        self.authority_calls = 0
        self.authority_hook = None
        self.confirmation_hook = None
        self.state_calls = 0
        self.state_hook = None
        self.state_validation_hook = None
        self.current_state = self.state()
        self.confirmation = DurableMissionConfirmation(
            confirmation_id='confirmation-1',
            authority=self.authority,
            decision_id=result.decision_id,
            arguments_digest=monitor_room_arguments_digest(
                {'location': '거실'}
            ),
            evidence_digest=_digest('trusted-person-evidence'),
            issuer_id='trusted-confirmation-service',
            person_subject_id='owner-1',
            issued_at=clock.value + 0.1,
            expires_at=clock.value + 7.0,
        )

    def state(self, **overrides) -> TrustedMissionState:
        values = {
            'observed_at': self.clock.value,
            'map_id': self.resolver.map_id,
            'map_revision': self.resolver.map_revision,
            'navigation_available': True,
            'localization_ok': True,
            'camera_available': True,
            'stream_available': True,
            'privacy_mode': False,
            'emergency_stop': False,
        }
        values.update(overrides)
        return TrustedMissionState(**values)

    def resolve_authority(self, result):
        assert result is self.result
        return self.authority

    def validate_authority(self, authority):
        self.authority_calls += 1
        if self.authority_hook is not None:
            self.authority_hook(self.authority_calls)
        return self.active and authority.binding_digest == (
            self.authority.binding_digest
        )

    def resolve_confirmation(self, confirmation_id):
        assert confirmation_id == 'confirmation-1'
        if self.confirmation_hook is not None:
            self.confirmation_hook()
        return self.confirmation

    def resolve_state(self, authority, plan):
        del authority, plan
        self.state_calls += 1
        if self.state_hook is not None:
            self.state_hook(self.state_calls)
        return self.current_state

    def validate_state(self, state, authority, plan):
        del authority, plan
        if self.state_validation_hook is not None:
            self.state_validation_hook()
        return state is self.current_state


class _Harness:
    """Compose one deterministic durable controller."""

    def __init__(
        self,
        *,
        database=None,
        adapter=None,
        clock=None,
        resolver=None,
        trust=None,
        lease_seconds=1.0,
        timeout=0.1,
    ) -> None:
        self._temporary_directory = None
        if database is None:
            self._temporary_directory = tempfile.TemporaryDirectory()
            database = os.path.join(
                self._temporary_directory.name, 'room-mission.sqlite3'
            )
        self.clock = clock or _Clock()
        self.result = (
            trust.result if trust is not None else _result(self.clock)
        )
        self.resolver = resolver or SemanticRoomResolver(
            _user_map(), expected_map_id='home-a'
        )
        self.trust = trust or _Trust(
            self.result, self.clock, self.resolver
        )
        self.adapter = adapter or SimulationRoomMissionAdapter()
        self.device_binding = SimulationDeviceBinding(
            'simulation-device-1', _digest('simulation-device-1')
        )
        self.store = SQLiteRoomMissionStore(
            str(database), clock=self.clock, lease_seconds=lease_seconds
        )
        self.controller = DurableSimulationRoomMission(
            self.store,
            self.resolver,
            self.adapter,
            self.device_binding,
            authority_resolver=self.trust.resolve_authority,
            authority_validator=self.trust.validate_authority,
            confirmation_resolver=self.trust.resolve_confirmation,
            state_resolver=self.trust.resolve_state,
            state_validator=self.trust.validate_state,
            clock=self.clock,
            worker_id_factory=lambda: 'worker-1',
            adapter_timeout_seconds=timeout,
            stream_timeout_seconds=timeout,
            cancellation_timeout_seconds=timeout,
        )

    def authorized(self):
        proposed = self.controller.propose(self.result)
        assert proposed.proposal is not None
        confirmed = self.controller.confirm(
            proposed.proposal, 'confirmation-1'
        )
        assert confirmed.tool_call_id is not None
        return proposed.proposal, confirmed.tool_call_id

    def close(self):
        self.store.close()
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()


def test_exact_success_phases_and_honest_durable_feedback() -> None:
    """Only the four exact fake phases yield simulated success."""
    harness = _Harness()
    try:
        handle, tool_call_id = harness.authorized()
        feedback = harness.controller.execute(tool_call_id, handle)
        assert feedback.status == 'succeeded'
        assert feedback.code == 'simulation_succeeded'
        assert feedback.terminal_source == 'simulation_adapter'
        assert feedback.durability == 'sqlite_local'
        assert feedback.lease_scope == 'database_device'
        assert feedback.simulated is True
        assert feedback.physical_effects is False
        assert feedback.viewer_live is False
        assert [
            phase for _tool, phase in harness.controller.simulation_calls
        ] == [
            'preflight', 'navigating', 'coverage', 'live_ready'
        ]
        assert harness.trust.state_calls >= 4
    finally:
        harness.close()


@pytest.mark.parametrize(
    ('adapter', 'code'),
    [
        (SimulationRoomMissionAdapter(fail_phase='navigating'),
         'navigating_failed'),
        (SimulationRoomMissionAdapter(timeout_phase='coverage'),
         'coverage_timeout'),
    ],
)
def test_typed_adapter_failure_and_timeout_are_recorded(adapter, code) -> None:
    """Only typed adapter outcomes become simulation observations."""
    harness = _Harness(adapter=adapter)
    try:
        handle, tool_call_id = harness.authorized()
        feedback = harness.controller.execute(tool_call_id, handle)
        assert feedback.code == code
        assert feedback.terminal_source == 'simulation_adapter'
    finally:
        harness.close()


def test_wrapper_hang_is_recovery_failure_not_adapter_timeout() -> None:
    """A local wait timeout never invents an adapter observation."""
    started = threading.Event()
    release = threading.Event()

    def blocked_step():
        started.set()
        release.wait()
        return AdapterStepResult('succeeded')

    harness = _Harness(timeout=0.02)
    try:
        handle, tool_call_id = harness.authorized()
        record = harness.controller._record_for_handle(handle)
        lease = harness.store.claim_execution(
            tool_call_id,
            harness.trust.authority,
            harness.controller._worker_id,
        )
        harness.store.prepare_phase(lease, 'preflight')
        outcome, hung, renewed = harness.controller._bounded_adapter_call(
            blocked_step, (), 0.02, lease
        )
        assert outcome is None
        assert hung is True
        feedback = harness.controller._fail_unresolved(record, renewed)
        assert started.is_set()
        assert feedback.status == 'failed'
        assert feedback.code == 'recovery_unavailable'
        assert feedback.terminal_source == 'recovery'
        events = harness.store.list_events(
            tool_call_id, harness.trust.authority
        )
        assert not any(
            event.source == 'simulation_adapter'
            and event.event_kind in {'observation', 'terminal'}
            for event in events
        )
    finally:
        release.set()
        harness.close()


def test_exact_adapter_type_and_forged_handles_are_rejected(
    tmp_path,
) -> None:
    """Subclass adapters and equal-looking handles grant no capability."""
    class _AdapterSubclass(SimulationRoomMissionAdapter):
        pass

    clock = _Clock()
    result = _result(clock)
    resolver = SemanticRoomResolver(_user_map(), expected_map_id='home-a')
    trust = _Trust(result, clock, resolver)
    memory_store = SQLiteRoomMissionStore(':memory:', clock=clock)
    try:
        with pytest.raises(ValueError):
            DurableSimulationRoomMission(
                memory_store,
                resolver,
                SimulationRoomMissionAdapter(),
                SimulationDeviceBinding('device-1', _digest('device-1')),
                authority_resolver=trust.resolve_authority,
                authority_validator=trust.validate_authority,
                confirmation_resolver=trust.resolve_confirmation,
                state_resolver=trust.resolve_state,
                state_validator=trust.validate_state,
                clock=clock,
            )
    finally:
        memory_store.close()

    store = SQLiteRoomMissionStore(
        str(tmp_path / 'exact-type.sqlite3'), clock=clock
    )
    try:
        with pytest.raises(ValueError):
            DurableSimulationRoomMission(
                store,
                resolver,
                _AdapterSubclass(),
                SimulationDeviceBinding('device-1', _digest('device-1')),
                authority_resolver=trust.resolve_authority,
                authority_validator=trust.validate_authority,
                confirmation_resolver=trust.resolve_confirmation,
                state_resolver=trust.resolve_state,
                state_validator=trust.validate_state,
                clock=clock,
            )
    finally:
        store.close()

    harness = _Harness()
    try:
        handle, tool_call_id = harness.authorized()
        forged = DurableMissionProposalHandle(handle.proposal_id)
        feedback = harness.controller.execute(tool_call_id, forged)
        assert feedback.code == 'authority_required'
        assert harness.controller.simulation_calls == ()
    finally:
        harness.close()


def test_store_identity_admission_ignores_shadow_and_precedes_callbacks(
    tmp_path,
) -> None:
    """The fixed identity verifier runs before any injected callback."""
    clock = _Clock()
    result = _result(clock)
    resolver = SemanticRoomResolver(_user_map(), expected_map_id='home-a')
    trust = _Trust(result, clock, resolver)
    binding = SimulationDeviceBinding('device-1', _digest('device-1'))
    reached = []

    def forbidden(*arguments, **keywords):
        del arguments, keywords
        reached.append(True)
        raise AssertionError('identity shadow ran')

    def build(store, worker_id_factory):
        return DurableSimulationRoomMission(
            store,
            resolver,
            SimulationRoomMissionAdapter(),
            binding,
            authority_resolver=trust.resolve_authority,
            authority_validator=trust.validate_authority,
            confirmation_resolver=trust.resolve_confirmation,
            state_resolver=trust.resolve_state,
            state_validator=trust.validate_state,
            clock=clock,
            worker_id_factory=worker_id_factory,
        )

    live_store = SQLiteRoomMissionStore(
        str(tmp_path / 'identity-shadow.sqlite3'), clock=clock
    )
    try:
        with pytest.raises(AttributeError):
            live_store.assert_durable_identity = forbidden
        live_store.__dict__['assert_durable_identity'] = forbidden
        live_store.__dict__['_lock'] = forbidden
        build(live_store, lambda: 'worker-identity')
        assert reached == []
    finally:
        live_store.close()

    closed_store = SQLiteRoomMissionStore(
        str(tmp_path / 'identity-closed.sqlite3'), clock=clock
    )
    closed_store.close()
    with pytest.raises(ValueError) as caught:
        build(closed_store, forbidden)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert reached == []


@pytest.mark.parametrize('operation', ['execute', 'cancel'])
def test_identity_loss_after_intent_never_dispatches(
    operation, monkeypatch,
) -> None:
    """An unlinked durable device after intent cannot reach the fake."""
    harness = _Harness()
    database = harness.store.database_path
    try:
        handle, tool_call_id = harness.authorized()
        if operation == 'execute':
            original = SQLiteRoomMissionStore.prepare_phase

            def unlink_after_intent(store, lease, phase):
                intent = original(store, lease, phase)
                if store is harness.store:
                    os.unlink(database)
                return intent

            monkeypatch.setattr(
                SQLiteRoomMissionStore,
                'prepare_phase',
                unlink_after_intent,
            )
        else:
            original = SQLiteRoomMissionStore.request_cancel

            def unlink_after_intent(store, *arguments, **keywords):
                request = original(store, *arguments, **keywords)
                if store is harness.store:
                    os.unlink(database)
                return request

            monkeypatch.setattr(
                SQLiteRoomMissionStore,
                'request_cancel',
                unlink_after_intent,
            )
        feedback = getattr(harness.controller, operation)(
            tool_call_id, handle
        )
        assert feedback.status == 'failed'
        assert feedback.code == 'ledger_unavailable'
        assert harness.controller.simulation_calls == ()
    finally:
        harness.close()


def test_terminal_receipt_is_unavailable_after_identity_loss(
    monkeypatch,
) -> None:
    """A stale successful row cannot escape after its DB disappears."""
    harness = _Harness()
    database = harness.store.database_path
    original = SQLiteRoomMissionStore.get_execution
    reads = {'count': 0}

    def unlink_after_second_read(store, tool_call_id, authority):
        execution = original(store, tool_call_id, authority)
        if store is harness.store:
            reads['count'] += 1
            if reads['count'] == 2:
                os.unlink(database)
        return execution

    try:
        handle, tool_call_id = harness.authorized()
        assert harness.controller.execute(
            tool_call_id, handle
        ).status == 'succeeded'
        monkeypatch.setattr(
            SQLiteRoomMissionStore,
            'get_execution',
            unlink_after_second_read,
        )
        feedback = harness.controller.feedback(tool_call_id, handle)
        assert feedback.status == 'failed'
        assert feedback.code == 'ledger_unavailable'
        assert feedback.terminal_source is None
        assert reads['count'] == 2
    finally:
        harness.close()


@pytest.mark.parametrize(
    ('mutation', 'expected_code'),
    [('revoke', 'authority_revoked'), ('privacy', 'privacy_blocked')],
)
def test_guard_failure_aborts_before_any_adapter_call(
    mutation, expected_code
) -> None:
    """Current authority and trusted state are fail-closed."""
    harness = _Harness()
    try:
        handle, tool_call_id = harness.authorized()
        if mutation == 'revoke':
            harness.trust.active = False
        else:
            harness.trust.current_state = harness.trust.state(
                privacy_mode=True
            )
        feedback = harness.controller.execute(tool_call_id, handle)
        assert feedback.status == 'failed'
        assert feedback.code == expected_code
        assert feedback.terminal_source == 'controller'
        assert harness.controller.simulation_calls == ()
    finally:
        harness.close()


def test_external_resolver_mutation_cannot_change_sealed_plan() -> None:
    """Post-construction resolver mutation cannot redirect execution."""
    harness = _Harness()
    try:
        handle, tool_call_id = harness.authorized()
        object.__setattr__(
            harness.resolver, '_map_revision', _digest('changed-map')
        )
        feedback = harness.controller.execute(tool_call_id, handle)
        assert feedback.code == 'simulation_succeeded'
        assert feedback.terminal_source == 'simulation_adapter'
    finally:
        harness.close()


def test_state_is_rechecked_between_phases_before_new_dispatch() -> None:
    """A later privacy block prevents the next phase from starting."""
    harness = _Harness()
    try:
        handle, tool_call_id = harness.authorized()

        def block_after_preflight(call_count):
            if call_count == 5:
                harness.trust.current_state = harness.trust.state(
                    privacy_mode=True
                )

        harness.trust.state_hook = block_after_preflight
        feedback = harness.controller.execute(tool_call_id, handle)
        assert feedback.code == 'privacy_blocked'
        assert [
            phase for _tool, phase in harness.controller.simulation_calls
        ] == [
            'preflight'
        ]
    finally:
        harness.close()


def test_state_validator_revocation_prevents_first_dispatch() -> None:
    """A validator-side revocation is caught after the callback."""
    harness = _Harness()
    try:
        handle, tool_call_id = harness.authorized()
        harness.trust.state_validation_hook = lambda: setattr(
            harness.trust, 'active', False
        )
        feedback = harness.controller.execute(tool_call_id, handle)
        assert feedback.code == 'authority_revoked'
        assert feedback.terminal_source == 'controller'
        assert harness.controller.simulation_calls == ()
    finally:
        harness.close()


@pytest.mark.parametrize('change', ['revoke', 'privacy'])
def test_post_live_guard_never_commits_success_after_change(change) -> None:
    """A change while live readiness blocks terminal success."""
    started = threading.Event()
    release = threading.Event()
    harness = _Harness(timeout=0.5)
    results = []
    try:
        handle, tool_call_id = harness.authorized()

        def block_post_live_state(call_count):
            if call_count == 13:
                started.set()
                release.wait()

        harness.trust.state_hook = block_post_live_state
        worker = threading.Thread(
            target=lambda: results.append(
                harness.controller.execute(tool_call_id, handle)
            )
        )
        worker.start()
        assert started.wait(0.4)
        if change == 'revoke':
            harness.trust.active = False
        else:
            harness.trust.current_state = harness.trust.state(
                privacy_mode=True
            )
        release.set()
        worker.join(1.0)
        assert len(results) == 1
        assert results[0].status == 'failed'
        assert results[0].code == 'recovery_unavailable'
        assert results[0].terminal_source == 'recovery'
        events = harness.store.list_events(
            tool_call_id, harness.trust.authority
        )
        assert not any(
            event.code == 'simulation_succeeded' for event in events
        )
    finally:
        release.set()
        harness.close()


def test_concurrent_execute_dispatches_one_phase_stream(monkeypatch) -> None:
    """The local guard and durable lease prevent duplicate dispatch."""
    started = threading.Event()
    release = threading.Event()
    harness = _Harness(timeout=0.5)
    results = []
    try:
        handle, tool_call_id = harness.authorized()
        original_prepare = SQLiteRoomMissionStore.prepare_phase

        def gated_prepare(store, lease, phase):
            intent = original_prepare(store, lease, phase)
            if store is harness.store and phase == 'preflight':
                started.set()
                release.wait()
            return intent

        monkeypatch.setattr(
            SQLiteRoomMissionStore, 'prepare_phase', gated_prepare
        )
        worker = threading.Thread(
            target=lambda: results.append(
                harness.controller.execute(tool_call_id, handle)
            )
        )
        worker.start()
        assert started.wait(0.3)
        concurrent = harness.controller.execute(tool_call_id, handle)
        assert concurrent.status == 'running'
        release.set()
        worker.join(1.0)
        assert len(results) == 1
        assert results[0].status == 'succeeded'
        phases = [
            phase
            for _tool, phase in harness.controller.simulation_calls
        ]
        assert phases == [
            'preflight', 'navigating', 'coverage', 'live_ready'
        ]
    finally:
        release.set()
        harness.close()


def test_confirmation_replay_and_reopen_keep_one_tool_id(tmp_path) -> None:
    """Exact evidence replay survives a store and controller restart."""
    database = tmp_path / 'durable-room.sqlite3'
    clock = _Clock()
    first = _Harness(database=database, clock=clock, lease_seconds=0.05)
    try:
        handle, tool_call_id = first.authorized()
        replay = first.controller.confirm(handle, 'confirmation-1')
        assert replay.tool_call_id == tool_call_id
        trust = first.trust
        result = first.result
        resolver = first.resolver
    finally:
        first.close()

    reopened = _Harness(
        database=database,
        clock=clock,
        resolver=resolver,
        trust=trust,
        lease_seconds=0.05,
    )
    try:
        restored = reopened.controller.rehydrate(result)
        assert restored.proposal is not None
        replay = reopened.controller.confirm(
            restored.proposal, 'confirmation-1'
        )
        assert replay.tool_call_id == tool_call_id
        assert reopened.controller.execute(
            tool_call_id, restored.proposal
        ).status == 'succeeded'
    finally:
        reopened.close()


def test_confirmed_restart_revocation_durably_aborts_pending_execution(
    tmp_path,
) -> None:
    """A confirmed proposal cannot revive after restart revocation."""
    database = tmp_path / 'confirmed-restart-revoked.sqlite3'
    clock = _Clock()
    first = _Harness(database=database, clock=clock)
    try:
        _handle, tool_call_id = first.authorized()
        result = first.result
        resolver = first.resolver
        trust = first.trust
    finally:
        first.close()

    trust.active = False
    revoked = _Harness(
        database=database,
        clock=clock,
        resolver=resolver,
        trust=trust,
    )
    try:
        rejected = revoked.controller.rehydrate(result)
        assert rejected.proposal is None
        assert rejected.feedback.status == 'failed'
        assert rejected.feedback.code == 'authority_revoked'
        assert revoked.controller.simulation_calls == ()
    finally:
        revoked.close()

    trust.active = True
    restored = _Harness(
        database=database,
        clock=clock,
        resolver=resolver,
        trust=trust,
    )
    try:
        replay = restored.controller.rehydrate(result)
        assert replay.proposal is not None
        feedback = restored.controller.execute(
            tool_call_id, replay.proposal
        )
        assert feedback.status == 'failed'
        assert feedback.code == 'authority_revoked'
        assert restored.controller.simulation_calls == ()
    finally:
        restored.close()


def test_restart_with_phase_intent_never_redispatches(tmp_path) -> None:
    """An unresolved operation is failed by recovery with zero calls."""
    database = tmp_path / 'intent-crash.sqlite3'
    clock = _Clock()
    first = _Harness(database=database, clock=clock, lease_seconds=0.05)
    try:
        _handle, tool_call_id = first.authorized()
        lease = first.store.claim_execution(
            tool_call_id, first.trust.authority, 'crashed-worker'
        )
        first.store.prepare_phase(lease, 'preflight')
        trust = first.trust
        result = first.result
        resolver = first.resolver
    finally:
        first.close()
    clock.value += 9.0

    reopened = _Harness(
        database=database,
        clock=clock,
        resolver=resolver,
        trust=trust,
        lease_seconds=0.05,
    )
    try:
        restored = reopened.controller.rehydrate(result)
        assert restored.proposal is not None
        feedback = reopened.controller.execute(
            tool_call_id, restored.proposal
        )
        assert feedback.code == 'recovery_unavailable'
        assert feedback.terminal_source == 'recovery'
        assert reopened.controller.simulation_calls == ()
    finally:
        reopened.close()


def test_expired_clean_restart_terminalizes_without_dispatch(tmp_path) -> None:
    """Expired clean work is released by the ledger after rehydrate."""
    database = tmp_path / 'expired-clean.sqlite3'
    clock = _Clock()
    first = _Harness(database=database, clock=clock, lease_seconds=0.05)
    try:
        _handle, tool_call_id = first.authorized()
        trust = first.trust
        result = first.result
        resolver = first.resolver
    finally:
        first.close()
    clock.value += 9.0

    reopened = _Harness(
        database=database,
        clock=clock,
        resolver=resolver,
        trust=trust,
        lease_seconds=0.05,
    )
    try:
        restored = reopened.controller.rehydrate(result)
        assert restored.proposal is not None
        feedback = reopened.controller.execute(
            tool_call_id, restored.proposal
        )
        assert feedback.status == 'timed_out'
        assert feedback.code == 'authorization_expired'
        assert feedback.terminal_source == 'controller'
        assert reopened.controller.simulation_calls == ()
    finally:
        reopened.close()


def test_expired_terminal_restart_replays_exact_success(tmp_path) -> None:
    """A terminal receipt remains owner-readable after its old TTL."""
    database = tmp_path / 'expired-terminal.sqlite3'
    clock = _Clock()
    first = _Harness(database=database, clock=clock)
    try:
        handle, tool_call_id = first.authorized()
        original = first.controller.execute(tool_call_id, handle)
        assert original.status == 'succeeded'
        trust = first.trust
        result = first.result
        resolver = first.resolver
    finally:
        first.close()
    clock.value += 9.0

    reopened = _Harness(
        database=database,
        clock=clock,
        resolver=resolver,
        trust=trust,
    )
    try:
        restored = reopened.controller.rehydrate(result)
        assert restored.proposal is not None
        replay = reopened.controller.feedback(
            tool_call_id, restored.proposal
        )
        assert replay.status == original.status
        assert replay.code == original.code
        assert replay.terminal_source == original.terminal_source
        assert reopened.controller.simulation_calls == ()
    finally:
        reopened.close()


def test_clean_between_phase_restart_resumes_only_next_phase(tmp_path) -> None:
    """A committed observation resumes without repeating its phase."""
    database = tmp_path / 'clean-restart.sqlite3'
    clock = _Clock()
    first = _Harness(database=database, clock=clock, lease_seconds=0.05)
    try:
        _handle, tool_call_id = first.authorized()
        lease = first.store.claim_execution(
            tool_call_id, first.trust.authority, 'crashed-worker'
        )
        intent = first.store.prepare_phase(lease, 'preflight')
        first.store.record_phase_result(lease, intent, 'succeeded')
        trust = first.trust
        result = first.result
        resolver = first.resolver
    finally:
        first.close()
    clock.value += 0.1

    reopened = _Harness(
        database=database,
        clock=clock,
        resolver=resolver,
        trust=trust,
        lease_seconds=0.05,
    )
    try:
        restored = reopened.controller.rehydrate(result)
        assert restored.proposal is not None
        feedback = reopened.controller.execute(
            tool_call_id, restored.proposal
        )
        assert feedback.status == 'succeeded'
        assert [
            phase
            for _tool, phase in reopened.controller.simulation_calls
        ] == [
            'navigating', 'coverage', 'live_ready'
        ]
    finally:
        reopened.close()


def test_cancel_before_run_is_durable_simulated_cancellation() -> None:
    """Fresh cancellation intent precedes the exact fake cancel call."""
    harness = _Harness()
    try:
        handle, tool_call_id = harness.authorized()
        feedback = harness.controller.cancel(tool_call_id, handle)
        assert feedback.status == 'cancelled'
        assert feedback.code == 'simulation_cancelled'
        assert feedback.terminal_source == 'simulation_adapter'
        assert [
            phase for _tool, phase in harness.controller.simulation_calls
        ] == [
            'cancel'
        ]
    finally:
        harness.close()


def test_duplicate_cancel_only_reads_inflight_transition(monkeypatch) -> None:
    """A duplicate cannot reconcile-fail another local cancel call."""
    started = threading.Event()
    release = threading.Event()
    harness = _Harness(timeout=0.5)
    results = []
    try:
        handle, tool_call_id = harness.authorized()
        original_record = SQLiteRoomMissionStore.record_cancel_result

        def gated_record(store, lease, intent, outcome):
            if store is harness.store:
                started.set()
                release.wait()
            return original_record(store, lease, intent, outcome)

        monkeypatch.setattr(
            SQLiteRoomMissionStore,
            'record_cancel_result',
            gated_record,
        )
        worker = threading.Thread(
            target=lambda: results.append(
                harness.controller.cancel(tool_call_id, handle)
            )
        )
        worker.start()
        assert started.wait(0.4)
        duplicate = harness.controller.cancel(tool_call_id, handle)
        assert duplicate.status == 'cancelling'
        release.set()
        worker.join(1.0)
        assert len(results) == 1
        assert results[0].status == 'cancelled'
        assert [
            phase for _tool, phase in harness.controller.simulation_calls
        ] == ['cancel']
    finally:
        release.set()
        harness.close()


def test_cancel_wins_over_late_phase_result_without_next_phase(
    monkeypatch,
) -> None:
    """Late simulated phase completion cannot overwrite cancellation."""
    started = threading.Event()
    release = threading.Event()
    harness = _Harness(timeout=0.5)
    execution_results = []
    try:
        handle, tool_call_id = harness.authorized()
        original_record = SQLiteRoomMissionStore.record_phase_result

        def gated_record(store, lease, intent, outcome):
            if store is harness.store and intent.phase == 'preflight':
                started.set()
                release.wait()
            return original_record(store, lease, intent, outcome)

        monkeypatch.setattr(
            SQLiteRoomMissionStore,
            'record_phase_result',
            gated_record,
        )
        worker = threading.Thread(
            target=lambda: execution_results.append(
                harness.controller.execute(tool_call_id, handle)
            )
        )
        worker.start()
        assert started.wait(0.4)
        cancelled = harness.controller.cancel(tool_call_id, handle)
        assert cancelled.status == 'cancelled'
        release.set()
        worker.join(1.0)
        assert len(execution_results) == 1
        assert execution_results[0].status == 'cancelled'
        assert [
            phase for _tool, phase in harness.controller.simulation_calls
        ] == [
            'preflight', 'cancel'
        ]
    finally:
        release.set()
        harness.close()


def test_cancel_wrapper_hang_is_recovery_failure() -> None:
    """A hung cancel wrapper cannot claim simulated cancellation."""
    started = threading.Event()
    release = threading.Event()

    def blocked_step():
        started.set()
        release.wait()
        return AdapterStepResult('succeeded')

    harness = _Harness(timeout=0.02)
    try:
        handle, tool_call_id = harness.authorized()
        record = harness.controller._record_for_handle(handle)
        lease = harness.store.claim_execution(
            tool_call_id,
            harness.trust.authority,
            harness.controller._worker_id,
        )
        request = harness.store.request_cancel(
            tool_call_id,
            harness.trust.authority,
            harness.controller._worker_id,
            current_lease=lease,
        )
        outcome, hung, renewed = harness.controller._bounded_adapter_call(
            blocked_step, (), 0.02, request.lease
        )
        assert outcome is None
        assert hung is True
        feedback = harness.controller._fail_cancel_intent(
            record, renewed, request.intent
        )
        assert started.is_set()
        assert feedback.status == 'failed'
        assert feedback.code == 'recovery_unavailable'
        assert feedback.terminal_source == 'recovery'
    finally:
        release.set()
        harness.close()


def test_revoked_preconfirm_proposal_is_durably_invalidated() -> None:
    """Revocation is durable without inventing a user denial."""
    harness = _Harness()
    try:
        proposed = harness.controller.propose(harness.result)
        assert proposed.proposal is not None
        harness.trust.active = False
        denied = harness.controller.confirm(
            proposed.proposal, 'confirmation-1'
        )
        assert denied.code == 'authority_revoked'
        harness.trust.active = True
        replay = harness.controller.confirm(
            proposed.proposal, 'confirmation-1'
        )
        assert replay.status == 'rejected'
        assert replay.tool_call_id is None
        assert harness.controller.simulation_calls == ()
        connection = sqlite3.connect(harness.store.database_path)
        try:
            status, terminal_code = connection.execute(
                'SELECT status, terminal_code '
                'FROM room_mission_proposals'
            ).fetchone()
        finally:
            connection.close()
        assert status == 'failed'
        assert terminal_code == 'authority_revoked'
        assert terminal_code != 'user_denied'
    finally:
        harness.close()


def test_only_explicit_deny_persists_user_denied() -> None:
    """The user-denied audit code requires the public denial path."""
    harness = _Harness()
    try:
        proposed = harness.controller.propose(harness.result)
        assert proposed.proposal is not None
        feedback = harness.controller.deny(proposed.proposal)
        assert feedback.code == 'confirmation_denied'
        connection = sqlite3.connect(harness.store.database_path)
        try:
            status, terminal_code = connection.execute(
                'SELECT status, terminal_code '
                'FROM room_mission_proposals'
            ).fetchone()
        finally:
            connection.close()
        assert status == 'denied'
        assert terminal_code == 'user_denied'
    finally:
        harness.close()


@pytest.mark.parametrize(
    ('mutation', 'terminal_code'),
    [
        ('source', 'source_changed'),
        ('map', 'map_changed'),
        ('device', 'device_changed'),
    ],
)
def test_system_invalidation_is_durable_across_restart(
    mutation, terminal_code, tmp_path
) -> None:
    """Typed invalidation survives restart without creating a Tool ID."""
    database = tmp_path / f'{mutation}-invalidated.sqlite3'
    harness = _Harness(database=database)
    database = harness.store.database_path
    clock = harness.clock
    try:
        proposed = harness.controller.propose(harness.result)
        assert proposed.proposal is not None
        if mutation == 'source':
            harness.result.decision.arguments['location'] = '응접실'
        elif mutation == 'map':
            with durable_room_mission_module._CONTROLLER_FIELDS_GUARD:
                durable_room_mission_module._CONTROLLER_FIELDS[
                    harness.controller
                ]['_resolver_map_revision'] = _digest(
                    'replacement-map'
                )
        else:
            object.__setattr__(
                harness.device_binding,
                'device_binding_digest',
                _digest('replacement-device'),
            )
        invalidated = harness.controller.confirm(
            proposed.proposal, 'confirmation-1'
        )
        assert invalidated.status == 'rejected'
        assert invalidated.code == terminal_code
        connection = sqlite3.connect(database)
        try:
            row = connection.execute(
                'SELECT status, terminal_code '
                'FROM room_mission_proposals'
            ).fetchone()
            execution_count = connection.execute(
                'SELECT COUNT(*) FROM room_mission_executions'
            ).fetchone()[0]
        finally:
            connection.close()
        assert row == ('failed', terminal_code)
        assert execution_count == 0
    finally:
        harness.close()

    reopened = _Harness(database=database, clock=clock)
    try:
        restored = reopened.controller.rehydrate(reopened.result)
        assert restored.proposal is None
        assert restored.feedback.status == 'failed'
        assert reopened.controller.simulation_calls == ()
    finally:
        reopened.close()


def test_confirmation_and_invalidation_race_has_no_active_execution(
    tmp_path,
) -> None:
    """Concurrent restart owners leave invalidation or an aborted Tool."""
    database = tmp_path / 'controller-confirm-invalidate.sqlite3'
    clock = _Clock()
    result = _result(clock)
    first_resolver = SemanticRoomResolver(
        _user_map(), expected_map_id='home-a'
    )
    first_trust = _Trust(result, clock, first_resolver)
    first = _Harness(
        database=database,
        clock=clock,
        resolver=first_resolver,
        trust=first_trust,
    )
    second_resolver = SemanticRoomResolver(
        _user_map(), expected_map_id='home-a'
    )
    second_trust = _Trust(result, clock, second_resolver)
    second = _Harness(
        database=database,
        clock=clock,
        resolver=second_resolver,
        trust=second_trust,
    )
    results = []
    barrier = threading.Barrier(2)
    try:
        first_proposed = first.controller.propose(result)
        second_proposed = second.controller.rehydrate(result)
        assert first_proposed.proposal is not None
        assert second_proposed.proposal is not None
        first_trust.active = False

        def confirm(controller, handle):
            barrier.wait()
            results.append(controller.confirm(handle, 'confirmation-1'))

        threads = [
            threading.Thread(
                target=confirm,
                args=(first.controller, first_proposed.proposal),
            ),
            threading.Thread(
                target=confirm,
                args=(second.controller, second_proposed.proposal),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 2

        connection = sqlite3.connect(database)
        try:
            proposal_row = connection.execute(
                'SELECT status, terminal_code '
                'FROM room_mission_proposals'
            ).fetchone()
            execution_rows = connection.execute(
                'SELECT status, code FROM room_mission_executions'
            ).fetchall()
        finally:
            connection.close()
        if proposal_row[0] == 'failed':
            assert proposal_row[1] == 'authority_revoked'
            assert execution_rows == []
        else:
            assert proposal_row == ('confirmed', None)
            assert execution_rows == [('failed', 'authority_revoked')]
        assert first.controller.simulation_calls == ()
        assert second.controller.simulation_calls == ()
    finally:
        first.close()
        second.close()


def test_adapter_and_resolver_shadows_cannot_replace_sealed_runtime() -> None:
    """Post-construction shadows never reach resolver or fake dispatch."""
    harness = _Harness()
    reached = []

    def forbidden(*arguments, **keywords):
        del arguments, keywords
        reached.append(True)
        raise AssertionError('shadow-called')

    try:
        for name in (
            'preflight', 'navigate', 'cover', 'wait_live_ready',
            'cancel', '_step',
        ):
            setattr(harness.adapter, name, forbidden)
        harness.adapter._calls_lock = 'invalid-lock'
        harness.adapter._phase_gates = 'invalid-gates'
        harness.adapter._fail_phase = 'preflight'
        harness.resolver.plan = forbidden
        harness.resolver.resolve = forbidden
        handle, tool_call_id = harness.authorized()
        feedback = harness.controller.execute(tool_call_id, handle)
        assert feedback.status == 'succeeded'
        assert reached == []
        assert [
            phase for _tool, phase in harness.controller.simulation_calls
        ] == ['preflight', 'navigating', 'coverage', 'live_ready']
    finally:
        harness.close()


def test_preconstruction_adapter_and_resolver_shadows_are_rejected(
    tmp_path,
) -> None:
    """Already-shadowed exact instances cannot seed a controller."""
    clock = _Clock()
    result = _result(clock)
    resolver = SemanticRoomResolver(_user_map(), expected_map_id='home-a')
    trust = _Trust(result, clock, resolver)
    store = SQLiteRoomMissionStore(
        str(tmp_path / 'shadow-reject.sqlite3'), clock=clock
    )
    binding = SimulationDeviceBinding('device-1', _digest('device-1'))

    def build(
        candidate_resolver,
        candidate_adapter,
        worker_id_factory=lambda: 'worker-1',
    ):
        return DurableSimulationRoomMission(
            store,
            candidate_resolver,
            candidate_adapter,
            binding,
            authority_resolver=trust.resolve_authority,
            authority_validator=trust.validate_authority,
            confirmation_resolver=trust.resolve_confirmation,
            state_resolver=trust.resolve_state,
            state_validator=trust.validate_state,
            clock=clock,
            worker_id_factory=worker_id_factory,
        )

    try:
        adapter = SimulationRoomMissionAdapter()
        adapter._step = lambda *arguments: None
        with pytest.raises(ValueError):
            build(resolver, adapter)
        gate_started = threading.Event()
        gate_release = threading.Event()
        gated_adapter = SimulationRoomMissionAdapter(phase_gates=(
            SimulationPhaseGate(
                'preflight', gate_started, gate_release
            ),
        ))
        worker_calls = []

        def forbidden_worker_factory():
            worker_calls.append(True)
            raise AssertionError('constructor callback ran')

        with pytest.raises(ValueError):
            build(
                resolver,
                gated_adapter,
                forbidden_worker_factory,
            )
        assert gate_started.is_set() is False
        assert worker_calls == []
        shadowed_resolver = SemanticRoomResolver(
            _user_map(), expected_map_id='home-a'
        )
        shadowed_resolver.resolve = lambda location: None
        with pytest.raises(ValueError):
            build(shadowed_resolver, SimulationRoomMissionAdapter())
    finally:
        store.close()


def test_worker_base_exception_is_sanitized_without_excepthook() -> None:
    """A child BaseException becomes fixed recovery with no secret leak."""
    class _SensitiveWorkerFailure(BaseException):
        pass

    def fail_step():
        raise _SensitiveWorkerFailure('worker-secret-value')

    harness = _Harness()
    observed = []
    original_hook = threading.excepthook
    threading.excepthook = lambda arguments: observed.append(
        repr(arguments.exc_value)
    )
    try:
        handle, tool_call_id = harness.authorized()
        record = harness.controller._record_for_handle(handle)
        lease = harness.store.claim_execution(
            tool_call_id,
            harness.trust.authority,
            harness.controller._worker_id,
        )
        harness.store.prepare_phase(lease, 'preflight')
        outcome, hung, renewed = harness.controller._bounded_adapter_call(
            fail_step, (), 0.1, lease
        )
        assert outcome is None
        assert hung is False
        feedback = harness.controller._fail_unresolved(record, renewed)
        assert feedback.code == 'recovery_unavailable'
        assert feedback.terminal_source == 'recovery'
        events = harness.store.list_events(
            tool_call_id, harness.trust.authority
        )
        public = repr(feedback.to_dict()) + repr(events)
        assert 'worker-secret-value' not in public
        assert observed == []
    finally:
        threading.excepthook = original_hook
        harness.close()


def test_crashed_cancel_intent_recovers_without_redispatch(tmp_path) -> None:
    """The recovering cancel owner does not defer itself forever."""
    database = tmp_path / 'cancel-intent-recovery.sqlite3'
    clock = _Clock()
    first = _Harness(database=database, clock=clock, lease_seconds=0.05)
    try:
        _handle, tool_call_id = first.authorized()
        lease = first.store.claim_execution(
            tool_call_id, first.trust.authority, 'crashed-cancel-worker'
        )
        first.store.request_cancel(
            tool_call_id,
            first.trust.authority,
            'crashed-cancel-worker',
            current_lease=lease,
        )
        result = first.result
        resolver = first.resolver
        trust = first.trust
    finally:
        first.close()
    clock.value += 0.1

    reopened = _Harness(
        database=database,
        clock=clock,
        resolver=resolver,
        trust=trust,
        lease_seconds=0.05,
    )
    try:
        restored = reopened.controller.rehydrate(result)
        assert restored.proposal is not None
        feedback = reopened.controller.cancel(
            tool_call_id, restored.proposal
        )
        assert feedback.status == 'failed'
        assert feedback.code == 'recovery_unavailable'
        assert feedback.terminal_source == 'recovery'
        assert reopened.controller.simulation_calls == ()
    finally:
        reopened.close()


def test_feedback_rejects_non_durable_markers() -> None:
    """Durable controller feedback cannot claim process-local scope."""
    harness = _Harness()
    try:
        proposed = harness.controller.propose(harness.result)
        with pytest.raises(ValueError):
            replace(proposed.feedback, durability='process_local')
        with pytest.raises(ValueError):
            replace(proposed.feedback, lease_scope='store_connection')
    finally:
        harness.close()


def test_minimum_lease_is_renewed_before_expiry(tmp_path) -> None:
    """A 50ms lease stays fenced throughout one bounded worker call."""
    database = tmp_path / 'minimum-lease-heartbeat.sqlite3'
    clock = _Clock()
    started = threading.Event()
    release = threading.Event()

    def blocked_step():
        started.set()
        release.wait()
        return AdapterStepResult('succeeded')

    first = _Harness(
        database=database,
        clock=clock,
        lease_seconds=0.05,
        timeout=0.5,
    )
    second_resolver = SemanticRoomResolver(
        _user_map(), expected_map_id='home-a'
    )
    second_trust = _Trust(first.result, clock, second_resolver)
    second = _Harness(
        database=database,
        clock=clock,
        resolver=second_resolver,
        trust=second_trust,
        lease_seconds=0.05,
        timeout=0.5,
    )
    results = []
    stop_clock = threading.Event()

    def advance_clock():
        while not stop_clock.wait(0.01):
            clock.value += 0.01

    try:
        first_handle, tool_call_id = first.authorized()
        second_proposed = second.controller.rehydrate(first.result)
        assert second_proposed.proposal is not None
        assert second.controller.confirm(
            second_proposed.proposal, 'confirmation-1'
        ).tool_call_id == tool_call_id
        lease = first.store.claim_execution(
            tool_call_id,
            first.trust.authority,
            first.controller._worker_id,
        )
        intent = first.store.prepare_phase(lease, 'preflight')
        worker = threading.Thread(
            target=lambda: results.append(
                first.controller._bounded_adapter_call(
                    blocked_step, (), 0.5, lease
                )
            )
        )
        worker.start()
        assert started.wait(0.4)
        clock_worker = threading.Thread(target=advance_clock)
        clock_worker.start()
        time.sleep(0.14)
        contender = second.controller.execute(
            tool_call_id, second_proposed.proposal
        )
        assert contender.status == 'running'
        assert second.controller.simulation_calls == ()
        stop_clock.set()
        clock_worker.join(0.5)
        release.set()
        worker.join(1.0)
        assert len(results) == 1
        outcome, hung, renewed = results[0]
        assert outcome == AdapterStepResult('succeeded')
        assert hung is False
        committed = first.store.record_phase_result(
            renewed, intent, outcome.status
        )
        assert committed.code == 'preflight_succeeded'
    finally:
        stop_clock.set()
        release.set()
        first.close()
        second.close()


def test_lost_lease_discards_late_result_and_recovery_fences(
    tmp_path, monkeypatch,
) -> None:
    """A second controller recovers while the stale worker cannot commit."""
    database = tmp_path / 'lost-lease-fencing.sqlite3'
    clock = _Clock()
    phase_started = threading.Event()
    phase_release = threading.Event()
    renewal_entered = threading.Event()
    renewal_release = threading.Event()

    def blocked_step():
        phase_started.set()
        phase_release.wait()
        return AdapterStepResult('succeeded')

    first = _Harness(
        database=database,
        clock=clock,
        lease_seconds=0.05,
        timeout=0.5,
    )
    second_resolver = SemanticRoomResolver(
        _user_map(), expected_map_id='home-a'
    )
    second_trust = _Trust(first.result, clock, second_resolver)
    second = _Harness(
        database=database,
        clock=clock,
        resolver=second_resolver,
        trust=second_trust,
        lease_seconds=0.05,
        timeout=0.5,
    )
    first_results = []
    original_renew = SQLiteRoomMissionStore.renew_lease

    def controlled_renew(store, lease):
        if store is first.store:
            renewal_entered.set()
            renewal_release.wait()
        return original_renew(store, lease)

    try:
        first_handle, tool_call_id = first.authorized()
        second_proposed = second.controller.rehydrate(first.result)
        assert second_proposed.proposal is not None
        assert second.controller.confirm(
            second_proposed.proposal, 'confirmation-1'
        ).tool_call_id == tool_call_id
        monkeypatch.setattr(
            SQLiteRoomMissionStore, 'renew_lease', controlled_renew
        )
        lease = first.store.claim_execution(
            tool_call_id,
            first.trust.authority,
            first.controller._worker_id,
        )
        first.store.prepare_phase(lease, 'preflight')
        worker = threading.Thread(
            target=lambda: first_results.append(
                first.controller._bounded_adapter_call(
                    blocked_step, (), 0.5, lease
                )
            )
        )
        worker.start()
        assert phase_started.wait(0.4)
        assert renewal_entered.wait(0.4)
        clock.value += 0.06
        recovered = second.controller.execute(
            tool_call_id, second_proposed.proposal
        )
        assert recovered.status == 'failed'
        assert recovered.code == 'recovery_unavailable'
        assert recovered.terminal_source == 'recovery'
        assert second.controller.simulation_calls == ()
        renewal_release.set()
        worker.join(1.0)
        assert len(first_results) == 1
        outcome, hung, stale_lease = first_results[0]
        assert outcome is None
        assert hung is False
        record = first.controller._record_for_handle(first_handle)
        stale_feedback = first.controller._fail_unresolved(
            record, stale_lease
        )
        assert stale_feedback.status == 'failed'
        assert stale_feedback.code == 'recovery_unavailable'
        phase_release.set()
        events = second.store.list_events(
            tool_call_id, second.trust.authority
        )
        assert not any(
            event.source == 'simulation_adapter'
            and event.event_kind in {'observation', 'terminal'}
            for event in events
        )
    finally:
        renewal_release.set()
        phase_release.set()
        first.close()
        second.close()


def test_configuration_collaborators_are_immutable() -> None:
    """Validated exact-type boundaries cannot be replaced at runtime."""
    with pytest.raises(TypeError):
        class _ControllerSubclass(DurableSimulationRoomMission):
            pass

    harness = _Harness()
    reached = []

    def forbidden(*arguments, **keywords):
        del arguments, keywords
        reached.append(True)
        raise AssertionError('shadow callback ran')

    try:
        replacements = {
            'adapter': SimulationRoomMissionAdapter(),
            '_store': harness.store,
            '_resolver_plans': {},
            '_adapter_instance': SimulationRoomMissionAdapter(),
            '_adapter_step': forbidden,
            '_authority_validator': forbidden,
            '_state_resolver': forbidden,
            '_durability': 'process_local',
            '_PUBLIC_CONFIGURATION_NAMES': frozenset(),
            '_PRIVATE_CONFIGURATION_NAMES': frozenset(),
            '__setattr__': forbidden,
            '__delattr__': forbidden,
            'execute': forbidden,
            'cancel': forbidden,
            'confirm': forbidden,
            '_guard': forbidden,
            '__dict__': {},
            '__class__': object,
        }
        for name, value in replacements.items():
            with pytest.raises(AttributeError):
                setattr(harness.controller, name, value)
            with pytest.raises((AttributeError, TypeError)):
                object.__setattr__(harness.controller, name, value)
            with pytest.raises(AttributeError):
                delattr(harness.controller, name)
            with pytest.raises((AttributeError, TypeError)):
                object.__delattr__(harness.controller, name)
        with pytest.raises(AttributeError):
            harness.controller.new_runtime_callback = forbidden
        with pytest.raises(AttributeError):
            getattr(harness.controller, '__dict__')
        assert reached == []
        handle, tool_call_id = harness.authorized()
        assert harness.controller.execute(
            tool_call_id, handle
        ).status == 'succeeded'
        assert reached == []
    finally:
        harness.close()


def test_direct_callback_replacement_never_bypasses_revocation() -> None:
    """Even object.__setattr__ cannot replace a trusted validator."""
    harness = _Harness()
    reached = []

    def forged_validator(authority):
        del authority
        reached.append(True)
        return True

    try:
        handle, tool_call_id = harness.authorized()
        harness.trust.active = False
        with pytest.raises(AttributeError):
            object.__setattr__(
                harness.controller,
                '_authority_validator',
                forged_validator,
            )
        feedback = harness.controller.execute(tool_call_id, handle)
        assert feedback.status == 'failed'
        assert feedback.code == 'authority_revoked'
        assert harness.controller.simulation_calls == ()
        assert reached == []
    finally:
        harness.close()


def test_public_feedback_and_repr_do_not_reflect_sensitive_content() -> None:
    """Public objects exclude transcript, room label, pose, and secrets."""
    harness = _Harness()
    try:
        proposed = harness.controller.propose(harness.result)
        assert proposed.proposal is not None
        public = repr(proposed) + repr(proposed.proposal)
        public += repr(proposed.feedback.to_dict())
        for forbidden in (
            '거실', '응접실', 'auth-session-1', 'owner-1',
            'trusted-person-evidence', 'navigation_goal',
            'coverage_viewpoints',
        ):
            assert forbidden not in public
        assert set(proposed.feedback.to_dict()) == {
            'status', 'phase', 'code', 'sequence', 'tool_call_id',
            'terminal_source', 'runtime_mode', 'simulated',
            'physical_effects', 'viewer_live', 'durability', 'lease_scope',
        }
    finally:
        harness.close()
