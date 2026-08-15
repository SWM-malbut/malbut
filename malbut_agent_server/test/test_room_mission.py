"""Adversarial offline tests for the simulation-only room mission."""

import threading
import time
from dataclasses import replace

import pytest

from malbut_agent_server.orchestrator import OrchestrationResult
from malbut_agent_server.room_mission import (
    AdapterStepResult,
    MissionAuthority,
    MissionProposalHandle,
    RoomMissionValidationError,
    RoomMonitoringMission,
    SemanticRoomResolver,
    SimulationPhaseGate,
    SimulationRoomMissionAdapter,
    TrustedConfirmation,
    TrustedMissionState,
    monitor_room_arguments_digest,
    orchestration_authority_digest,
)
from malbut_agent_server.safety import SafetyResult
from malbut_agent_server.schemas import (
    AgentDecision,
    ProviderResult,
    ProviderUsage,
)


class _Clock:
    """Mutable deterministic clock."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _IDs:
    """Return deterministic server identifiers."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f'id_{self.calls}'


def _room(
    room_id: str = 'room-living',
    name: str = '거실',
    category: str = 'living_room',
    aliases=None,
    minimum_x: float = 0.0,
) -> dict:
    aliases = ['응접실'] if aliases is None else aliases
    return {
        'type': 'Feature',
        'id': room_id,
        'properties': {
            'role': 'room',
            'room_id': room_id,
            'name': name,
            'category': category,
            'aliases': aliases,
            'centroid': [minimum_x + 5.0, 5.0],
            'navigation_goal': {
                'x': minimum_x + 2.0,
                'y': 2.0,
                'yaw': 0.0,
            },
            'coverage_viewpoints': [
                {'x': minimum_x + 2.0, 'y': 2.0, 'yaw': 0.0},
                {'x': minimum_x + 8.0, 'y': 8.0, 'yaw': 3.0},
            ],
        },
        'geometry': {
            'type': 'Polygon',
            'coordinates': [[
                [minimum_x, 0.0],
                [minimum_x + 10.0, 0.0],
                [minimum_x + 10.0, 10.0],
                [minimum_x, 10.0],
                [minimum_x, 0.0],
            ]],
        },
    }


def _user_map(*rooms: dict) -> dict:
    return {
        'type': 'FeatureCollection',
        'map_id': 'home-a',
        'frame_id': 'map',
        'features': list(rooms or (_room(),)),
    }


def _resolver(user_map=None) -> SemanticRoomResolver:
    return SemanticRoomResolver(
        _user_map() if user_map is None else user_map,
        expected_map_id='home-a',
    )


def _decision(
    wall: _Clock,
    *,
    request_id: str = 'request-1',
    decision_id: str = 'decision-1',
    turn_id: str = 'turn-1',
    revision: int = 1,
    ordinal: int = 1,
    tool_name: str = 'monitor_room',
    safety_allowed: bool = True,
    state_trusted: bool = True,
    issued_offset: float = 0.0,
    ttl: float = 5.0,
) -> OrchestrationResult:
    decision = AgentDecision(
        type='tool_call',
        message='proposal',
        tool_name=tool_name,
        arguments={'location': '거실'},
        reason='',
        confidence=1.0,
        expires_in_ms=int(ttl * 1000),
    )
    raw = AgentDecision(
        type=decision.type,
        message=decision.message,
        tool_name=decision.tool_name,
        arguments=dict(decision.arguments),
        reason=decision.reason,
        confidence=decision.confidence,
        expires_in_ms=decision.expires_in_ms,
    )
    issued_at = wall.value + issued_offset
    return OrchestrationResult(
        request_id=request_id,
        conversation_id='conversation-1',
        turn_id=turn_id,
        conversation_generation=1,
        conversation_revision=revision,
        conversation_ordinal=ordinal,
        raw_decision=raw,
        decision=decision,
        safety=SafetyResult(
            safety_allowed,
            'allowed' if safety_allowed else 'privacy_mode',
            'fixed',
        ),
        provider_result=ProviderResult(
            decision=raw,
            provider='mock',
            model='mock-v1',
            latency_ms=1.0,
            usage=ProviderUsage(1, 1, 2),
        ),
        memory_ids=[],
        decision_id=decision_id,
        issued_at=issued_at,
        expires_at=issued_at + ttl,
        state_trusted=state_trusted,
        memory_revision=0,
    )


class _Trust:
    """In-memory stand-in for server auth and confirmation stores."""

    def __init__(self) -> None:
        self.authorities = {}
        self.active = {}
        self.confirmations = {}
        self.mutate_on_resolve = False
        self.validate_calls = 0
        self.on_validate = None

    def register(self, result, *, subject_id='owner-1'):
        authority = MissionAuthority(
            subject_id=subject_id,
            session_id=f'session-{subject_id}',
            request_id=result.request_id,
            conversation_id=result.conversation_id,
            turn_id=result.turn_id,
            conversation_generation=result.conversation_generation,
            conversation_revision=result.conversation_revision,
            conversation_ordinal=result.conversation_ordinal,
            decision_digest=orchestration_authority_digest(result),
        )
        self.authorities[result.request_id] = authority
        self.active[id(authority)] = True
        return authority

    def resolve_authority(self, result):
        if self.mutate_on_resolve:
            result.decision.arguments['location'] = '응접실'
            result.raw_decision.arguments['location'] = '응접실'
        return self.authorities[result.request_id]

    def validate_authority(self, authority):
        self.validate_calls += 1
        if self.on_validate is not None:
            self.on_validate(self.validate_calls)
        return self.active.get(id(authority), False)

    def revoke(self, authority):
        self.active[id(authority)] = False

    def issue(
        self,
        result,
        wall,
        *,
        confirmation_id='confirmation-1',
        authority=None,
    ):
        authority = authority or self.authorities[result.request_id]
        confirmation = TrustedConfirmation(
            confirmation_id=confirmation_id,
            authority=authority,
            decision_id=result.decision_id,
            arguments_digest=monitor_room_arguments_digest(
                {'location': '거실'}
            ),
            issued_at=wall.value,
            expires_at=min(wall.value + 2.0, result.expires_at),
            decision_expires_at=result.expires_at,
        )
        self.confirmations[confirmation_id] = confirmation
        return confirmation

    def resolve_confirmation(self, confirmation_id):
        return self.confirmations[confirmation_id]


class _Harness:
    """Compose one deterministic controller with trusted test seams."""

    def __init__(
        self,
        *,
        adapter=None,
        id_factory=None,
        timeout=0.1,
        max_records=256,
        clock_callable=None,
    ):
        self.wall = _Clock(1000.0)
        self.monotonic = _Clock(50.0)
        self.resolver = _resolver()
        self.adapter = adapter or SimulationRoomMissionAdapter()
        self.ids = id_factory or _IDs()
        self.trust = _Trust()
        self.current_state = self._new_state()
        self.mission = RoomMonitoringMission(
            self.resolver,
            self.adapter,
            authority_resolver=self.trust.resolve_authority,
            authority_validator=self.trust.validate_authority,
            confirmation_resolver=self.trust.resolve_confirmation,
            state_resolver=self.resolve_state,
            state_validator=self.validate_state,
            clock=clock_callable or self.wall,
            monotonic_clock=self.monotonic,
            id_factory=self.ids,
            adapter_timeout_seconds=timeout,
            stream_timeout_seconds=timeout,
            cancellation_timeout_seconds=timeout,
            max_mission_records=max_records,
        )

    def propose(self, result=None, *, subject_id='owner-1'):
        result = result or _decision(self.wall)
        authority = self.trust.register(
            result, subject_id=subject_id
        )
        response = self.mission.propose(result)
        assert response.proposal is not None
        return result, authority, response.proposal

    def confirmed(self, result=None, *, subject_id='owner-1'):
        result, authority, proposal = self.propose(
            result, subject_id=subject_id
        )
        confirmation_id = f'confirmation-{result.request_id}'
        self.trust.issue(
            result,
            self.wall,
            confirmation_id=confirmation_id,
        )
        feedback = self.mission.confirm(proposal, confirmation_id)
        assert feedback.tool_call_id is not None
        return result, authority, proposal, feedback

    def _new_state(self, **overrides):
        values = {
            'observed_at': self.wall.value,
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

    def state(self, **overrides):
        self.current_state = self._new_state(**overrides)
        return self.current_state

    def resolve_state(self, authority, plan):
        del authority, plan
        return self.current_state

    def validate_state(self, state, authority, plan):
        del authority, plan
        return state is self.current_state

    def second_result(self):
        result = _decision(
            self.wall,
            request_id='request-2',
            decision_id='decision-2',
            turn_id='turn-2',
            revision=2,
            ordinal=2,
        )
        self.trust.register(result, subject_id='owner-2')
        return result


def test_resolver_explicit_goal_snapshot_and_unique_aliases() -> None:
    """Explicit poses win over centroid and input mutation."""
    source = _user_map()
    resolver = _resolver(source)
    plans = [
        resolver.plan('거실'),
        resolver.plan('LIVING_ROOM'),
        resolver.plan('응접실'),
    ]
    assert plans[0] == plans[1] == plans[2]
    assert plans[0].navigation_goal.x == 2.0
    source['features'][0]['properties']['navigation_goal']['x'] = 9.0
    assert resolver.plan('거실').navigation_goal.x == 2.0


@pytest.mark.parametrize(
    ('field', 'value'),
    [('frame_id', 'odom'), ('map_id', 'other')],
)
def test_resolver_rejects_wrong_frame_or_map(field, value) -> None:
    """Map identity is fixed at construction."""
    source = _user_map()
    source[field] = value
    with pytest.raises(RoomMissionValidationError):
        _resolver(source)


def test_resolver_never_promotes_centroid() -> None:
    """A centroid cannot become a navigation goal."""
    source = _user_map()
    del source['features'][0]['properties']['navigation_goal']
    with pytest.raises(RoomMissionValidationError):
        _resolver(source)


@pytest.mark.parametrize(
    'viewpoints',
    [
        [],
        [
            {'x': 2.0, 'y': 2.0, 'yaw': 0.0},
            {'x': 2.0, 'y': 2.0, 'yaw': 0.0},
        ],
        [{'x': 0.0, 'y': 2.0, 'yaw': 0.0}],
        [{'x': 12.0, 'y': 2.0, 'yaw': 0.0}],
    ],
)
def test_resolver_rejects_invalid_viewpoints(viewpoints) -> None:
    """Coverage poses must be nonempty, unique, and strictly inside."""
    source = _user_map()
    source['features'][0]['properties'][
        'coverage_viewpoints'
    ] = viewpoints
    with pytest.raises(RoomMissionValidationError):
        _resolver(source)


@pytest.mark.parametrize('kind', ['bow_tie', 'hole'])
def test_resolver_rejects_bow_tie_and_holes(kind) -> None:
    """Invalid topology fails closed before point containment."""
    source = _user_map()
    if kind == 'bow_tie':
        coordinates = [[
            [0.0, 0.0],
            [10.0, 10.0],
            [0.0, 10.0],
            [10.0, 0.0],
            [0.0, 0.0],
        ]]
    else:
        coordinates = source['features'][0]['geometry']['coordinates'] + [[
            [4.0, 4.0],
            [6.0, 4.0],
            [6.0, 6.0],
            [4.0, 6.0],
            [4.0, 4.0],
        ]]
    source['features'][0]['geometry']['coordinates'] = coordinates
    with pytest.raises(RoomMissionValidationError):
        _resolver(source)


def test_resolver_rejects_all_ambiguous_labels() -> None:
    """Labels are unique both within and across rooms."""
    with pytest.raises(RoomMissionValidationError):
        _resolver(_user_map(_room(aliases=['거실'])))
    second = _room(
        room_id='room-study',
        name='서재',
        category='study',
        aliases=['응접실'],
        minimum_x=20.0,
    )
    with pytest.raises(RoomMissionValidationError):
        _resolver(_user_map(_room(), second))


def test_resolver_identity_and_indexes_are_immutable() -> None:
    """Map identity cannot be rewritten after validation."""
    resolver = _resolver()
    for name, value in (
        ('map_id', 'other'),
        ('map_revision', '0' * 64),
        ('_map_id', 'other'),
        ('_map_revision', '0' * 64),
        ('_rooms', {}),
        ('_aliases', {}),
    ):
        with pytest.raises(AttributeError, match='immutable'):
            setattr(resolver, name, value)
    assert resolver.map_id == 'home-a'
    assert resolver.plan('거실').map_id == 'home-a'


def test_proposal_uses_server_authority_and_is_opaque() -> None:
    """No caller-provided owner or plan appears at the public boundary."""
    harness = _Harness()
    result = _decision(harness.wall)
    missing = harness.mission.propose(result)
    assert missing.feedback.code == 'authority_unavailable'
    harness.trust.register(result)
    accepted = harness.mission.propose(result)
    handle = accepted.proposal
    assert handle is not None
    assert not hasattr(handle, 'plan')
    assert not hasattr(handle, 'authority')
    assert '거실' not in repr(accepted)
    assert set(handle.to_dict()) == {'proposal_id'}
    with pytest.raises(TypeError):
        harness.mission.propose(result, user_id='owner-2')


def test_authority_must_match_full_conversation_version() -> None:
    """Conversation reset/recreate metadata is bound into authority."""
    harness = _Harness()
    result = _decision(harness.wall)
    authority = harness.trust.register(result)
    harness.trust.authorities[result.request_id] = replace(
        authority,
        conversation_revision=2,
    )
    rejected = harness.mission.propose(result)
    assert rejected.feedback.code == 'authority_unavailable'


def test_committed_authority_rejects_decision_mutation() -> None:
    """Stale Safety approval cannot authorize a changed raw/final pair."""
    harness = _Harness()
    result = _decision(harness.wall)
    harness.trust.register(result)
    result.decision.arguments['location'] = '응접실'
    result.raw_decision.arguments['location'] = '응접실'

    rejected = harness.mission.propose(result)

    assert rejected.feedback.code == 'authority_unavailable'
    assert rejected.proposal is None
    assert harness.adapter.calls == ()


def test_authority_callback_cannot_mutate_live_decision() -> None:
    """Proposal validation and planning use the entry snapshot only."""
    harness = _Harness()
    result = _decision(harness.wall)
    harness.trust.register(result)
    harness.trust.mutate_on_resolve = True

    rejected = harness.mission.propose(result)

    assert rejected.feedback.code == 'authority_unavailable'
    assert rejected.proposal is None
    assert harness.adapter.calls == ()


def test_only_fresh_trusted_monitor_room_can_be_proposed() -> None:
    """Wrong tool, safety, trust, freshness, and TTL are rejected."""
    cases = [
        {'tool_name': 'navigate'},
        {'safety_allowed': False},
        {'state_trusted': False},
        {'issued_offset': -6.0},
        {'ttl': 11.0},
    ]
    for index, changes in enumerate(cases):
        harness = _Harness()
        result = _decision(
            harness.wall,
            request_id=f'request-{index}',
            decision_id=f'decision-{index}',
            **changes,
        )
        harness.trust.register(result)
        response = harness.mission.propose(result)
        assert response.proposal is None
        assert response.feedback.code == 'untrusted_proposal'


def test_trusted_confirmation_exact_binding_and_replay() -> None:
    """Issuer evidence binds authority, decision, arguments, and TTL."""
    harness = _Harness()
    result, _, proposal = harness.propose()
    valid = harness.trust.issue(result, harness.wall)
    other = harness.trust.register(
        harness.second_result(), subject_id='owner-2'
    )
    invalid = [
        replace(valid, authority=other),
        replace(valid, decision_id='other-decision'),
        replace(valid, arguments_digest='0' * 64),
        replace(valid, decision_expires_at=valid.decision_expires_at + 1),
    ]
    for index, evidence in enumerate(invalid):
        key = f'invalid-{index}'
        harness.trust.confirmations[key] = replace(
            evidence, confirmation_id=key
        )
        response = harness.mission.confirm(proposal, key)
        assert response.code == 'confirmation_invalid'
        assert response.tool_call_id is None
    accepted = harness.mission.confirm(
        proposal, valid.confirmation_id
    )
    replay = harness.mission.confirm(
        proposal, valid.confirmation_id
    )
    assert accepted.status == 'confirmed'
    assert replay.code == 'confirmation_replay'
    assert replay.tool_call_id == accepted.tool_call_id


def test_confirmation_lookup_key_must_equal_envelope_id() -> None:
    """A resolver cannot return evidence stored under another nonce."""
    harness = _Harness()
    result, _, proposal = harness.propose()
    evidence = harness.trust.issue(
        result,
        harness.wall,
        confirmation_id='actual-confirmation',
    )
    harness.trust.confirmations['lookup-alias'] = evidence

    rejected = harness.mission.confirm(proposal, 'lookup-alias')

    assert rejected.code == 'confirmation_invalid'
    assert rejected.tool_call_id is None


def test_plain_boolean_is_not_confirmation_and_deny_is_terminal() -> None:
    """Caller booleans have no authority; denial cannot be reversed."""
    first = _Harness()
    _, _, proposal = first.propose()
    invalid = first.mission.confirm(proposal, True)
    assert invalid.code == 'confirmation_invalid'
    assert invalid.tool_call_id is None

    second = _Harness()
    result, _, proposal = second.propose()
    denied = second.mission.deny(proposal)
    second.trust.issue(result, second.wall)
    replay = second.mission.confirm(proposal, 'confirmation-1')
    assert denied.code == 'confirmation_denied'
    assert replay.code == 'confirmation_replay'
    assert replay.tool_call_id is None


def test_revoked_and_cross_owner_replay_never_discloses_tool_id() -> None:
    """Authority is validated before replay or Tool ID lookup."""
    harness = _Harness()
    _, authority, victim, confirmed = harness.confirmed()
    attacker_result = harness.second_result()
    attacker = harness.mission.propose(attacker_result).proposal
    assert attacker is not None
    forged = MissionProposalHandle(victim.proposal_id)
    for handle in (attacker, forged):
        response = harness.mission.feedback(
            confirmed.tool_call_id, handle
        )
        assert response.code == 'authority_required'
        assert response.tool_call_id is None
    harness.trust.revoke(authority)
    for response in (
        harness.mission.confirm(
            victim, 'confirmation-request-1'
        ),
        harness.mission.feedback(confirmed.tool_call_id, victim),
        harness.mission.cancel(confirmed.tool_call_id, victim),
        harness.mission.execute(
            confirmed.tool_call_id, victim
        ),
    ):
        assert response.code == 'authority_revoked'
        assert response.tool_call_id is None


def test_success_is_exactly_once_and_honestly_simulated() -> None:
    """Success cannot claim a physical effect or real live viewer."""
    harness = _Harness()
    _, _, proposal, confirmed = harness.confirmed()
    result = harness.mission.execute(
        confirmed.tool_call_id, proposal
    )
    replay = harness.mission.execute(
        confirmed.tool_call_id, proposal
    )
    assert result.code == 'simulation_succeeded'
    assert result.runtime_mode == 'simulation'
    assert result.simulated is True
    assert result.physical_effects is False
    assert result.viewer_live is False
    assert result.durability == 'process_local'
    assert result.lease_scope == 'controller_instance'
    assert replay.code == 'execution_replay'
    assert [phase for _, phase in harness.adapter.calls] == [
        'preflight',
        'navigating',
        'coverage',
        'live_ready',
    ]
    assert {
        tool_id for tool_id, _ in harness.adapter.calls
    } == {confirmed.tool_call_id}


def test_execute_rejects_caller_state_and_bad_provenance() -> None:
    """A caller cannot inject a self-described trusted state object."""
    first = _Harness()
    _, _, proposal, confirmed = first.confirmed()
    with pytest.raises(TypeError):
        first.mission.execute(
            confirmed.tool_call_id,
            proposal,
            first.state(),
        )

    second = _Harness()
    _, _, proposal, confirmed = second.confirmed()
    second.current_state = object()
    rejected = second.mission.execute(
        confirmed.tool_call_id, proposal
    )
    assert rejected.code == 'state_unavailable'
    assert second.adapter.calls == ()


@pytest.mark.parametrize(
    ('change', 'code'),
    [
        ({'observed_at': 997.0}, 'stale_state'),
        ({'map_id': 'home-b'}, 'map_changed'),
        ({'map_revision': 'changed'}, 'map_changed'),
        ({'privacy_mode': True}, 'privacy_mode'),
        ({'emergency_stop': True}, 'emergency_stop'),
        ({'navigation_available': False}, 'navigation_unavailable'),
        ({'localization_ok': False}, 'localization_unavailable'),
        ({'camera_available': False}, 'camera_unavailable'),
        ({'stream_available': False}, 'stream_unavailable'),
    ],
)
def test_bad_runtime_state_blocks_all_adapter_calls(change, code) -> None:
    """Stale, changed, private, and unavailable states fail closed."""
    harness = _Harness()
    _, _, proposal, confirmed = harness.confirmed()
    harness.state(**change)
    result = harness.mission.execute(
        confirmed.tool_call_id,
        proposal,
    )
    assert result.code == code
    assert harness.adapter.calls == ()


def test_navigation_failure_and_stream_timeout_are_terminal() -> None:
    """Failed movement or fake stream readiness cannot become success."""
    cases = [
        (
            SimulationRoomMissionAdapter(fail_phase='navigating'),
            'navigation_failed',
        ),
        (
            SimulationRoomMissionAdapter(timeout_phase='live_ready'),
            'stream_timeout',
        ),
    ]
    for adapter, code in cases:
        harness = _Harness(adapter=adapter)
        _, _, proposal, confirmed = harness.confirmed()
        result = harness.mission.execute(
            confirmed.tool_call_id, proposal
        )
        assert result.code == code


def test_expiry_after_preflight_blocks_navigation() -> None:
    """Authorization is checked after every completed adapter phase."""
    phase_started = threading.Event()
    phase_release = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate(
            'preflight', phase_started, phase_release
        ),
    ))
    harness = _Harness(adapter=adapter, timeout=1.0)
    _, _, proposal, confirmed = harness.confirmed()
    results = []
    worker = threading.Thread(
        target=lambda: results.append(harness.mission.execute(
            confirmed.tool_call_id, proposal
        ))
    )
    worker.start()
    assert phase_started.wait(timeout=1)
    harness.wall.value += 3.0
    harness.monotonic.value += 3.0
    phase_release.set()
    worker.join(timeout=1)

    assert results[0].code == 'authorization_expired'
    assert [phase for _, phase in adapter.calls] == ['preflight']


def test_authority_revoke_after_live_phase_blocks_success() -> None:
    """Final simulation success rechecks the current owner outside lock."""
    phase_started = threading.Event()
    phase_release = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate(
            'live_ready', phase_started, phase_release
        ),
    ))
    harness = _Harness(adapter=adapter, timeout=1.0)
    _, authority, proposal, confirmed = harness.confirmed()
    results = []
    worker = threading.Thread(
        target=lambda: results.append(harness.mission.execute(
            confirmed.tool_call_id, proposal
        ))
    )
    worker.start()
    assert phase_started.wait(timeout=1)
    harness.trust.revoke(authority)
    phase_release.set()
    worker.join(timeout=1)

    assert results[0].code == 'authority_revoked'
    assert results[0].status == 'failed'


def test_cancel_during_final_guard_cannot_be_overwritten_by_success() -> None:
    """A final success check cannot erase an in-flight cancellation."""
    final_guard_started = threading.Event()
    final_guard_release = threading.Event()
    cancel_started = threading.Event()
    cancel_release = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate('cancel', cancel_started, cancel_release),
    ))
    harness = _Harness(adapter=adapter, timeout=1.0)
    _, _, proposal, confirmed = harness.confirmed()
    harness.trust.validate_calls = 0

    def hold_final_guard(call_number):
        if call_number == 11:
            final_guard_started.set()
            assert final_guard_release.wait(timeout=1)

    harness.trust.on_validate = hold_final_guard
    execution = []
    cancellation = []
    execute_worker = threading.Thread(
        target=lambda: execution.append(harness.mission.execute(
            confirmed.tool_call_id, proposal
        ))
    )
    execute_worker.start()
    assert final_guard_started.wait(timeout=1)
    cancel_worker = threading.Thread(
        target=lambda: cancellation.append(harness.mission.cancel(
            confirmed.tool_call_id, proposal
        ))
    )
    cancel_worker.start()
    assert cancel_started.wait(timeout=1)

    final_guard_release.set()
    execute_worker.join(timeout=1)
    assert not execute_worker.is_alive()
    assert execution[0].code == 'cancellation_started'
    assert execution[0].status == 'running'

    cancel_release.set()
    cancel_worker.join(timeout=1)
    assert not cancel_worker.is_alive()
    assert cancellation[0].code == 'mission_cancelled'
    assert cancellation[0].status == 'cancelled'
    assert all(
        result.code != 'simulation_succeeded'
        for result in execution + cancellation
    )


def test_clock_failure_after_phase_is_terminal_and_releases_lease() -> None:
    """A runtime clock fault cannot strand a running mission lease."""

    class FailingClock:
        def __init__(self):
            self.calls = 0
            self.fail_on = None

        def __call__(self):
            self.calls += 1
            if self.calls == self.fail_on:
                raise RuntimeError('private-clock-detail')
            return 1000.0

    clock = FailingClock()
    harness = _Harness(clock_callable=clock)
    _, _, proposal, confirmed = harness.confirmed()
    clock.fail_on = clock.calls + 3

    failed = harness.mission.execute(
        confirmed.tool_call_id, proposal
    )

    assert failed.status == 'timed_out'
    assert failed.code == 'clock_invalid'
    assert [phase for _, phase in harness.adapter.calls] == ['preflight']

    clock.fail_on = None
    second_result = harness.second_result()
    second_proposal = harness.mission.propose(second_result).proposal
    assert second_proposal is not None
    harness.trust.issue(
        second_result,
        harness.wall,
        confirmation_id='confirmation-second-after-clock',
    )
    second = harness.mission.confirm(
        second_proposal,
        'confirmation-second-after-clock',
    )
    succeeded = harness.mission.execute(
        second.tool_call_id, second_proposal
    )
    assert succeeded.code == 'simulation_succeeded'


def test_adapter_worker_start_failure_is_terminal_and_releases_lease(
    monkeypatch,
) -> None:
    """A local thread-start failure cannot leave an active record behind."""
    harness = _Harness()
    _, _, proposal, confirmed = harness.confirmed()
    original_start = threading.Thread.start

    def fail_start(_thread):
        raise RuntimeError('private-thread-detail')

    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    failed = harness.mission.execute(
        confirmed.tool_call_id, proposal
    )

    assert failed.status == 'failed'
    assert failed.code == 'preflight_failed'
    assert 'private-thread-detail' not in str(failed.to_dict())

    monkeypatch.setattr(threading.Thread, 'start', original_start)
    second_result = harness.second_result()
    second_proposal = harness.mission.propose(second_result).proposal
    assert second_proposal is not None
    harness.trust.issue(
        second_result,
        harness.wall,
        confirmation_id='confirmation-second-after-thread',
    )
    second = harness.mission.confirm(
        second_proposal,
        'confirmation-second-after-thread',
    )
    succeeded = harness.mission.execute(
        second.tool_call_id, second_proposal
    )
    assert succeeded.code == 'simulation_succeeded'


def test_phase_wait_is_clamped_to_remaining_authority_ttl() -> None:
    """A long adapter timeout cannot outlive a shorter authorization."""
    phase_started = threading.Event()
    phase_release = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate(
            'preflight', phase_started, phase_release
        ),
    ))
    harness = _Harness(adapter=adapter, timeout=1.0)
    result, _, proposal = harness.propose()
    evidence = harness.trust.issue(result, harness.wall)
    harness.trust.confirmations[evidence.confirmation_id] = replace(
        evidence,
        expires_at=harness.wall.value + 0.03,
    )
    confirmed = harness.mission.confirm(
        proposal, evidence.confirmation_id
    )
    started = time.monotonic()

    expired = harness.mission.execute(
        confirmed.tool_call_id, proposal
    )

    assert phase_started.is_set()
    assert time.monotonic() - started < 0.3
    assert expired.code == 'authorization_expired'
    phase_release.set()


def test_backward_wall_clock_and_monotonic_expiry_reject() -> None:
    """Clock manipulation cannot extend a proposal."""
    first = _Harness()
    result, _, proposal = first.propose()
    first.trust.issue(result, first.wall)
    first.wall.value = 990.0
    rolled_back = first.mission.confirm(
        proposal, 'confirmation-1'
    )
    assert rolled_back.status != 'confirmed'
    assert rolled_back.tool_call_id is None

    second = _Harness()
    result, _, proposal = second.propose()
    second.trust.issue(result, second.wall)
    second.monotonic.value += 6.0
    expired = second.mission.confirm(proposal, 'confirmation-1')
    assert expired.code == 'confirmation_expired'


def test_confirmation_commit_refreshes_time_after_authority_check() -> None:
    """A validator-side expiry cannot race Tool ID creation."""
    harness = _Harness()
    result, _, proposal = harness.propose()
    harness.trust.issue(result, harness.wall)
    harness.trust.validate_calls = 0

    def expire_on_final_validation(call_number):
        if call_number == 2:
            harness.wall.value += 3.0
            harness.monotonic.value += 3.0

    harness.trust.on_validate = expire_on_final_validation

    expired = harness.mission.confirm(proposal, 'confirmation-1')

    assert expired.code == 'confirmation_expired'
    assert expired.tool_call_id is None


def test_physical_adapter_is_rejected_at_construction() -> None:
    """No arbitrary or marker-flipping adapter can enter the controller."""

    class PhysicalAdapter(SimulationRoomMissionAdapter):
        physical_calls = 0

        def preflight(self, context, plan, state):
            del context, plan, state
            self.physical_calls += 1
            return AdapterStepResult('succeeded')

    with pytest.raises(
        RoomMissionValidationError, match='built-in simulation'
    ):
        _Harness(adapter=PhysicalAdapter())


def test_validated_configuration_cannot_be_publicly_replaced() -> None:
    """Post-construction mutation cannot bypass simulation constraints."""
    harness = _Harness()
    replacements = {
        'adapter': object(),
        '_adapter': object(),
        'resolver': object(),
        '_resolver': object(),
        'max_state_age_seconds': -1.0,
        '_max_state_age_seconds': -1.0,
        'adapter_timeout_seconds': -1.0,
        '_adapter_timeout_seconds': -1.0,
        'stream_timeout_seconds': 9999.0,
        'cancellation_timeout_seconds': -1.0,
        '_adapter_preflight': lambda: None,
        '_authority_resolver': lambda value: value,
    }
    for name, value in replacements.items():
        with pytest.raises(AttributeError, match='immutable'):
            setattr(harness.mission, name, value)


def test_hung_phase_is_daemon_bounded() -> None:
    """A hung adapter cannot hold the API indefinitely."""
    started_event = threading.Event()
    release_event = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate(
            'preflight', started_event, release_event
        ),
    ))
    harness = _Harness(adapter=adapter, timeout=0.03)
    _, _, proposal, confirmed = harness.confirmed()
    started = time.monotonic()
    result = harness.mission.execute(
        confirmed.tool_call_id, proposal
    )
    assert time.monotonic() - started < 0.3
    assert result.code == 'preflight_timeout'
    assert started_event.is_set()
    release_event.set()


def test_hung_cancel_never_holds_feedback_lock() -> None:
    """Feedback remains responsive while cancellation is bounded."""
    nav_started = threading.Event()
    nav_release = threading.Event()
    cancel_started = threading.Event()
    cancel_release = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate('navigating', nav_started, nav_release),
        SimulationPhaseGate('cancel', cancel_started, cancel_release),
    ))
    harness = _Harness(adapter=adapter, timeout=0.08)
    _, _, proposal, confirmed = harness.confirmed()
    execution = []
    cancellation = []
    worker = threading.Thread(
        target=lambda: execution.append(harness.mission.execute(
            confirmed.tool_call_id, proposal
        ))
    )
    worker.start()
    assert nav_started.wait(timeout=1)
    cancel_worker = threading.Thread(
        target=lambda: cancellation.append(harness.mission.cancel(
            confirmed.tool_call_id, proposal
        ))
    )
    cancel_worker.start()
    assert cancel_started.wait(timeout=1)
    started = time.monotonic()
    feedback = harness.mission.feedback(
        confirmed.tool_call_id, proposal
    )
    assert time.monotonic() - started < 0.04
    assert feedback.code == 'cancellation_started'
    cancel_worker.join(timeout=1)
    assert cancellation[0].code == 'cancellation_timeout'
    nav_release.set()
    cancel_release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()


def test_distinct_missions_have_one_atomic_active_lease() -> None:
    """A second mission fails busy without an adapter call."""
    started_event = threading.Event()
    release_event = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate(
            'preflight', started_event, release_event
        ),
    ))
    harness = _Harness(adapter=adapter, timeout=0.5)
    _, _, first_proposal, first = harness.confirmed()
    second_result = harness.second_result()
    second_proposal = harness.mission.propose(second_result).proposal
    assert second_proposal is not None
    harness.trust.issue(
        second_result,
        harness.wall,
        confirmation_id='confirmation-second',
    )
    second = harness.mission.confirm(
        second_proposal, 'confirmation-second'
    )
    first_results = []
    worker = threading.Thread(
        target=lambda: first_results.append(harness.mission.execute(
            first.tool_call_id, first_proposal
        ))
    )
    worker.start()
    assert started_event.wait(timeout=1)
    busy = harness.mission.execute(
        second.tool_call_id, second_proposal
    )
    assert busy.code == 'mission_busy'
    assert all(
        tool_id == first.tool_call_id
        for tool_id, _ in adapter.calls
    )
    release_event.set()
    worker.join(timeout=1)
    assert first_results[0].code == 'simulation_succeeded'


def test_cancel_before_run_invokes_no_adapter() -> None:
    """Idle cancellation is terminal without a simulated phase."""
    first = _Harness()
    _, _, proposal, confirmed = first.confirmed()
    cancelled = first.mission.cancel(
        confirmed.tool_call_id, proposal
    )
    assert cancelled.status == 'cancelled'
    assert first.adapter.calls == ()


def test_server_id_collision_never_overwrites_ledger() -> None:
    """Three duplicate ID attempts fail closed."""
    harness = _Harness(id_factory=lambda: 'same')
    harness.propose()
    second = harness.second_result()
    with pytest.raises(RoomMissionValidationError, match='collision'):
        harness.mission.propose(second)


def test_reentrant_proposal_id_factory_cannot_duplicate_decision() -> None:
    """ID generation occurs outside the atomic decision commit."""

    class ReentrantIDs:
        def __init__(self):
            self.calls = 0
            self.mission = None
            self.result = None
            self.nested = []

        def __call__(self):
            self.calls += 1
            if self.calls == 1:
                self.nested.append(self.mission.propose(self.result))
            return f'reentrant_{self.calls}'

    ids = ReentrantIDs()
    harness = _Harness(id_factory=ids)
    result = _decision(harness.wall)
    harness.trust.register(result)
    ids.mission = harness.mission
    ids.result = result

    outer = harness.mission.propose(result)
    candidates = [outer, ids.nested[0]]

    assert sum(item.proposal is not None for item in candidates) == 1
    assert sorted(item.feedback.code for item in candidates) == [
        'decision_replay',
        'mission_proposed',
    ]


def test_reentrant_tool_id_factory_cannot_duplicate_confirmation() -> None:
    """One confirmation nonce can commit only one Tool identifier."""

    class ReentrantIDs:
        def __init__(self):
            self.calls = 0
            self.enabled = False
            self.triggered = False
            self.mission = None
            self.proposal = None
            self.confirmation_id = None
            self.nested = []

        def __call__(self):
            self.calls += 1
            if self.enabled and not self.triggered:
                self.triggered = True
                self.nested.append(self.mission.confirm(
                    self.proposal,
                    self.confirmation_id,
                ))
            return f'reentrant_{self.calls}'

    ids = ReentrantIDs()
    harness = _Harness(id_factory=ids)
    result, _, proposal = harness.propose()
    evidence = harness.trust.issue(result, harness.wall)
    ids.mission = harness.mission
    ids.proposal = proposal
    ids.confirmation_id = evidence.confirmation_id
    ids.enabled = True

    outer = harness.mission.confirm(
        proposal,
        evidence.confirmation_id,
    )
    candidates = [outer, ids.nested[0]]

    assert sum(item.status == 'confirmed' for item in candidates) == 1
    assert {item.tool_call_id for item in candidates} == {
        ids.nested[0].tool_call_id,
    }
    terminal = harness.mission.execute(
        ids.nested[0].tool_call_id,
        proposal,
    )
    replay = harness.mission.execute(
        ids.nested[0].tool_call_id,
        proposal,
    )
    assert terminal.code == 'simulation_succeeded'
    assert replay.code == 'execution_replay'
    assert len(harness.adapter.calls) == 4


def test_revoked_proposal_cannot_revive_after_authority_returns() -> None:
    """Authority loss permanently tombstones pending and confirmed work."""
    pending = _Harness()
    pending_result, pending_authority, pending_proposal = pending.propose()
    pending_evidence = pending.trust.issue(
        pending_result,
        pending.wall,
    )
    pending.trust.revoke(pending_authority)

    rejected = pending.mission.confirm(
        pending_proposal,
        pending_evidence.confirmation_id,
    )
    pending.trust.active[id(pending_authority)] = True
    replay = pending.mission.confirm(
        pending_proposal,
        pending_evidence.confirmation_id,
    )

    assert rejected.code == 'authority_revoked'
    assert replay.code == 'confirmation_replay'
    assert replay.tool_call_id is None

    active = _Harness()
    _, authority, proposal, confirmed = active.confirmed()
    active.trust.revoke(authority)
    revoked = active.mission.execute(
        confirmed.tool_call_id,
        proposal,
    )
    active.trust.active[id(authority)] = True
    late = active.mission.execute(
        confirmed.tool_call_id,
        proposal,
    )

    assert revoked.code == 'authority_revoked'
    assert late.code == 'execution_replay'
    assert active.adapter.calls == ()


def test_mission_record_capacity_retains_replay_tombstones() -> None:
    """The hard cap rejects new work without evicting replay history."""
    harness = _Harness(max_records=1)
    first_result, _, _ = harness.propose()
    second_result = harness.second_result()

    full = harness.mission.propose(second_result)
    replay = harness.mission.propose(first_result)

    assert full.proposal is None
    assert full.feedback.code == 'mission_capacity_reached'
    assert replay.proposal is None
    assert replay.feedback.code == 'decision_replay'


def test_exception_boundaries_remove_secret_causes_and_contexts() -> None:
    """Collaborator, clock, and JSON failures expose fixed errors only."""

    def fail_with_secret():
        raise RuntimeError('private-sentinel')

    id_harness = _Harness(id_factory=fail_with_secret)
    with pytest.raises(RoomMissionValidationError) as id_error:
        id_harness.propose()

    clock_harness = _Harness(clock_callable=fail_with_secret)
    clock_result = _decision(clock_harness.wall)
    clock_harness.trust.register(clock_result)
    with pytest.raises(RoomMissionValidationError) as clock_error:
        clock_harness.mission.propose(clock_result)

    bad_map = _user_map()
    bad_map['private-sentinel'] = object()
    with pytest.raises(RoomMissionValidationError) as json_error:
        _resolver(bad_map)

    for captured in (id_error, clock_error, json_error):
        error = captured.value
        assert error.__cause__ is None
        assert error.__context__ is None
        assert 'private-sentinel' not in str(error)


def test_active_lease_is_explicitly_controller_instance_local() -> None:
    """Two controllers may overlap only because this adapter is simulated."""
    phase_started = threading.Event()
    phase_release = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate(
            'preflight', phase_started, phase_release
        ),
    ))
    first = _Harness(adapter=adapter, timeout=0.5)
    second = _Harness(adapter=adapter, timeout=0.5)
    _, _, first_proposal, first_call = first.confirmed()
    _, _, second_proposal, second_call = second.confirmed()
    results = []
    workers = [
        threading.Thread(
            target=lambda: results.append(first.mission.execute(
                first_call.tool_call_id, first_proposal
            ))
        ),
        threading.Thread(
            target=lambda: results.append(second.mission.execute(
                second_call.tool_call_id, second_proposal
            ))
        ),
    ]
    for worker in workers:
        worker.start()
    assert phase_started.wait(timeout=1)
    deadline = time.monotonic() + 0.2
    while len(adapter.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(adapter.calls) == 2
    phase_release.set()
    for worker in workers:
        worker.join(timeout=1)

    assert len(results) == 2
    assert all(result.code == 'simulation_succeeded' for result in results)
    assert all(
        result.lease_scope == 'controller_instance'
        for result in results
    )
