"""Offline end-to-end tests for wake-to-room-mission handoff."""

import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest

from malbut_agent_server.continuous_voice import (
    AWAITING_CONFIRMATION,
    AWAITING_WAKE,
    MISSION_WAIT,
    ContinuousVoiceSession,
    SpeechOutputResult,
    WakeWordEvent,
)
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gateway import simulation_registry
from malbut_agent_server.local_stt import LocalSTTResult, WavMetadata
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.room_live_scenario import (
    RoomLiveScenarioCoordinator,
    RoomLiveScenarioValidationError,
)
from malbut_agent_server.room_mission import (
    MissionAuthority,
    RoomMonitoringMission,
    SemanticRoomResolver,
    SimulationPhaseGate,
    SimulationRoomMissionAdapter,
    TrustedConfirmation,
    TrustedMissionState,
    orchestration_authority_digest,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import RobotState
from malbut_agent_server.speech import (
    SpeechConversationCoordinator,
    TrustedSpeechBinding,
)


class _WakeSource:
    """Return a finite sequence of synthetic wake detections."""

    def __init__(self, events) -> None:
        self.events = deque(events)
        self.calls = 0

    def wait_for_wake(self, stop_event: threading.Event):
        """Return one event without touching a microphone."""
        self.calls += 1
        if stop_event.is_set() or not self.events:
            return None
        return self.events.popleft()


class _WallClock:
    """Use wall time until a test pins one deterministic value."""

    def __init__(self) -> None:
        self.value = None

    def __call__(self) -> float:
        if self.value is None:
            return time.time()
        return self.value


class _TranscriptSource:
    """Return scripted final STT results without audio capture."""

    def __init__(self, transcripts) -> None:
        self.transcripts = deque(transcripts)

    def capture_final(self, wake_event, stop_event):
        """Return one transcript for an accepted wake event."""
        del wake_event
        if stop_event.is_set() or not self.transcripts:
            raise RuntimeError('transcript source unavailable')
        return self.transcripts.popleft()


class _SpeechOutput:
    """Acknowledge TTS without speakers or external I/O."""

    def __init__(self) -> None:
        self.requests = []
        self.cancellations = []

    def play(self, request, stop_event):
        """Record one safe response as terminal."""
        if stop_event.is_set():
            return SpeechOutputResult(
                request_id=request.request_id,
                status='cancelled',
            )
        self.requests.append(request)
        return SpeechOutputResult(
            request_id=request.request_id,
            status='completed',
        )

    def cancel(self, request) -> None:
        """Record an idempotent cancellation request."""
        self.cancellations.append(request)


class _TrustStore:
    """Trusted in-memory authority and affirmative-event test seam."""

    def __init__(self) -> None:
        self.authorities = {}
        self.confirmations = {}
        self.active = set()
        self.on_resolve = None

    def resolve_authority(self, result):
        """Resolve only a previously committed authority snapshot."""
        callback = self.on_resolve
        self.on_resolve = None
        if callback is not None:
            callback()
        return self.authorities[result.request_id]

    def register(self, result):
        """Commit the exact orchestration digest before proposal use."""
        authority = MissionAuthority(
            subject_id='voice-user',
            session_id='voice-session',
            request_id=result.request_id,
            conversation_id=result.conversation_id,
            turn_id=result.turn_id,
            conversation_generation=result.conversation_generation,
            conversation_revision=result.conversation_revision,
            conversation_ordinal=result.conversation_ordinal,
            decision_digest=orchestration_authority_digest(result),
        )
        self.authorities[result.request_id] = authority
        self.active.add(id(authority))
        return authority

    def validate_authority(self, authority) -> bool:
        """Accept only an object issued by this test seam."""
        return id(authority) in self.active

    def revoke(self, result) -> None:
        """Revoke one committed authority snapshot."""
        authority = self.authorities[result.request_id]
        self.active.discard(id(authority))

    def resolve_confirmation(self, confirmation_id):
        """Resolve only a separately stored affirmative event."""
        return self.confirmations[confirmation_id]

    def issue(self, request, result, confirmation_id='confirm-1') -> str:
        """Issue exact evidence after an explicit simulated user yes."""
        now = time.time()
        self.confirmations[confirmation_id] = TrustedConfirmation(
            confirmation_id=confirmation_id,
            authority=self.authorities[result.request_id],
            decision_id=request.decision_id,
            arguments_digest=request.arguments_digest,
            issued_at=now,
            expires_at=min(now + 1.0, request.expires_at),
            decision_expires_at=request.expires_at,
        )
        return confirmation_id


class _Runtime:
    """Own the resources used by one fully local scenario."""

    def __init__(self, adapter) -> None:
        self.registry = simulation_registry()
        self.memory = SQLiteMemoryStore(':memory:')
        self.conversations = SQLiteConversationStore(':memory:')
        orchestrator = AgentOrchestrator(
            provider=MockProvider(),
            memory_store=self.memory,
            conversation_store=self.conversations,
            safety_policy=SafetyPolicy(
                monitorable_locations=['거실']
            ),
            trusted_robot_state=True,
            capability_registry=self.registry,
        )
        binding = TrustedSpeechBinding(
            user_id='voice-user',
            speaker_id='voice-speaker',
            speech_session_id='voice-session',
            conversation_id='voice-conversation',
            source='scripted-stt',
        )
        self.output = _SpeechOutput()
        self.wake_source = _WakeSource([
            _wake('wake-1', 1),
            _wake('wake-2', 2),
        ])
        self.voice = ContinuousVoiceSession(
            SpeechConversationCoordinator(orchestrator),
            binding,
            self.wake_source,
            _TranscriptSource([
                _transcript(
                    '거실로 가서 방 전체를 라이브로 보여줘'
                ),
                _transcript('안녕'),
            ]),
            self.output,
            robot_state=RobotState.from_dict({
                'battery_percent': 80,
                'navigation_available': True,
                'localization_ok': True,
                'camera_available': True,
                'privacy_mode': False,
            }),
            available_tools=('monitor_room',),
        )
        self.resolver = SemanticRoomResolver(
            _user_map(),
            expected_map_id='home-a',
        )
        self.adapter = adapter
        self.trust = _TrustStore()
        self.wall_clock = _WallClock()
        self.current_state = self._new_mission_state()
        self.mission = RoomMonitoringMission(
            self.resolver,
            self.adapter,
            authority_resolver=self.trust.resolve_authority,
            authority_validator=self.trust.validate_authority,
            confirmation_resolver=self.trust.resolve_confirmation,
            state_resolver=self.resolve_state,
            state_validator=self.validate_state,
            clock=self.wall_clock,
            adapter_timeout_seconds=0.1,
            stream_timeout_seconds=0.1,
            cancellation_timeout_seconds=0.1,
        )
        self.scenario = RoomLiveScenarioCoordinator(
            self.voice,
            self.mission,
        )

    def _new_mission_state(self, **changes):
        """Build a fresh server-owned local state snapshot."""
        values = {
            'observed_at': self.wall_clock(),
            'map_id': self.resolver.map_id,
            'map_revision': self.resolver.map_revision,
            'navigation_available': True,
            'localization_ok': True,
            'camera_available': True,
            'stream_available': True,
            'privacy_mode': False,
            'emergency_stop': False,
        }
        values.update(changes)
        return TrustedMissionState(**values)

    def set_mission_state(self, **changes) -> None:
        """Replace the state returned by the trusted test resolver."""
        self.current_state = self._new_mission_state(**changes)

    def resolve_state(self, authority, plan):
        """Return state from the trusted runtime seam."""
        del authority, plan
        return self.current_state

    def validate_state(self, state, authority, plan) -> bool:
        """Accept only the exact runtime-owned state object."""
        del authority, plan
        return state is self.current_state

    def close(self) -> None:
        """Close in reverse ownership order."""
        self.voice.close()
        self.conversations.close()
        self.memory.close()


@contextmanager
def _runtime(adapter=None):
    runtime = _Runtime(adapter or SimulationRoomMissionAdapter())
    try:
        yield runtime
    finally:
        runtime.close()


def _wake(event_id: str, sequence: int) -> WakeWordEvent:
    return WakeWordEvent(
        event_id=event_id,
        source='scripted-wake',
        source_sequence=sequence,
        source_timestamp_ns=1000 + sequence,
        confidence=0.99,
    )


def _transcript(text: str) -> LocalSTTResult:
    return LocalSTTResult(
        text=text,
        confidence=0.99,
        language='ko',
        audio_metadata=WavMetadata(
            duration_ms=1000,
            sample_rate_hz=16000,
            channel_count=1,
            sample_width_bytes=2,
            frame_count=16000,
            file_size_bytes=32044,
        ),
        backend='scripted-stt',
        model='scripted-stt-v1',
    )


def _user_map():
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
                    'x': 2.0,
                    'y': 2.0,
                    'yaw': 0.0,
                },
                'coverage_viewpoints': [
                    {'x': 2.0, 'y': 2.0, 'yaw': 0.0},
                    {'x': 8.0, 'y': 8.0, 'yaw': 3.0},
                ],
            },
            'geometry': {
                'type': 'Polygon',
                'coordinates': [[
                    [0.0, 0.0],
                    [10.0, 0.0],
                    [10.0, 10.0],
                    [0.0, 10.0],
                    [0.0, 0.0],
                ]],
            },
        }],
    }


def _propose(runtime):
    cycle = runtime.voice.run_once()
    assert cycle.state == AWAITING_CONFIRMATION
    assert cycle.confirmation_request is not None
    assert (
        runtime.voice.pending_confirmation_request
        is cycle.confirmation_request
    )
    result = cycle.pipeline_result.agent_result
    runtime.trust.register(result)
    proposed = runtime.scenario.propose_from_voice(cycle)
    assert proposed.proposal is not None
    return cycle, result, proposed.proposal


def test_voice_confirmation_executes_fake_once_then_rearms_wake() -> None:
    """The full safe path executes once and accepts the next wake."""
    with _runtime() as runtime:
        cycle, result, proposal = _propose(runtime)
        request = cycle.confirmation_request
        agent_adapter = runtime.registry.get('monitor_room').adapter

        assert request.to_audit_dict()['authorized'] is False
        assert request.to_audit_dict()['tool_call_id'] is None
        assert runtime.adapter.calls == ()
        assert agent_adapter.calls == 0
        assert runtime.wake_source.calls == 1

        pending = runtime.voice.run_once()
        assert pending.status == 'busy'
        assert pending.code == 'confirmation_pending'
        assert pending.state == AWAITING_CONFIRMATION
        assert runtime.wake_source.calls == 1

        invalid_id = runtime.trust.issue(
            request,
            result,
            confirmation_id='confirm-mutated',
        )
        runtime.trust.confirmations[invalid_id] = replace(
            runtime.trust.confirmations[invalid_id],
            arguments_digest='0' * 64,
        )
        rejected = runtime.scenario.confirm_and_execute(
            proposal,
            invalid_id,
        )
        assert rejected.code == 'confirmation_invalid'
        assert runtime.adapter.calls == ()
        assert runtime.voice.state == AWAITING_CONFIRMATION

        confirmation_id = runtime.trust.issue(request, result)
        confirmed = runtime.scenario.confirm(
            proposal,
            confirmation_id,
        )
        assert confirmed.status == 'confirmed'
        assert runtime.voice.state == MISSION_WAIT
        in_progress = runtime.voice.run_once()
        assert in_progress.status == 'busy'
        assert in_progress.code == 'mission_in_progress'
        assert in_progress.state == MISSION_WAIT
        assert runtime.wake_source.calls == 1

        terminal = runtime.scenario.execute(
            confirmed.tool_call_id,
            proposal,
        )
        calls = runtime.adapter.calls

        assert terminal.status == 'succeeded'
        assert terminal.code == 'simulation_succeeded'
        assert terminal.viewer_live is False
        assert terminal.physical_effects is False
        assert [phase for _, phase in calls] == [
            'preflight',
            'navigating',
            'coverage',
            'live_ready',
        ]
        with pytest.raises(RoomLiveScenarioValidationError):
            runtime.scenario.execute(
                confirmed.tool_call_id,
                proposal,
            )
        assert runtime.adapter.calls == calls
        assert agent_adapter.calls == 0
        assert runtime.voice.state == AWAITING_WAKE
        assert runtime.output.requests == []

        serialized = json.dumps(
            terminal.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )
        for forbidden in (
            '거실',
            'location',
            'room-living',
            'navigation_goal',
            'coverage_viewpoints',
        ):
            assert forbidden not in serialized

        next_cycle = runtime.voice.run_once()
        assert next_cycle.status == 'responded'
        assert next_cycle.code == 'message'
        assert next_cycle.state == AWAITING_WAKE
        assert runtime.wake_source.calls == 2
        assert len(runtime.output.requests) == 1


def test_denial_is_terminal_and_never_reaches_fake_adapter() -> None:
    """An explicit no cannot be replaced by a later affirmative event."""
    with _runtime() as runtime:
        cycle, result, proposal = _propose(runtime)
        denied = runtime.scenario.deny(proposal)
        confirmation_id = runtime.trust.issue(
            cycle.confirmation_request,
            result,
        )

        assert denied.status == 'cancelled'
        assert denied.code == 'confirmation_denied'
        assert denied.viewer_live is False
        assert runtime.voice.state == AWAITING_WAKE
        with pytest.raises(RoomLiveScenarioValidationError):
            runtime.scenario.confirm_and_execute(
                proposal,
                confirmation_id,
            )
        assert runtime.adapter.calls == ()


def test_confirmed_cancellation_never_reaches_fake_adapter() -> None:
    """Cancellation can terminally consume a Tool ID before execution."""
    with _runtime() as runtime:
        cycle, result, proposal = _propose(runtime)
        confirmation_id = runtime.trust.issue(
            cycle.confirmation_request,
            result,
        )
        confirmed = runtime.scenario.confirm(
            proposal,
            confirmation_id,
        )
        assert runtime.voice.state == MISSION_WAIT
        blocked = runtime.voice.run_once()
        assert blocked.code == 'mission_in_progress'
        assert runtime.wake_source.calls == 1
        cancelled = runtime.scenario.cancel(
            confirmed.tool_call_id,
            proposal,
        )

        assert confirmed.status == 'confirmed'
        assert cancelled.status == 'cancelled'
        assert cancelled.code == 'mission_cancelled'
        assert cancelled.viewer_live is False
        assert runtime.voice.state == AWAITING_WAKE
        with pytest.raises(RoomLiveScenarioValidationError):
            runtime.scenario.execute(
                confirmed.tool_call_id,
                proposal,
            )
        assert runtime.adapter.calls == ()


def test_expired_confirmation_clears_both_pending_states() -> None:
    """Mission-side expiry cannot strand the voice confirmation state."""
    with _runtime() as runtime:
        cycle, result, proposal = _propose(runtime)
        confirmation_id = runtime.trust.issue(
            cycle.confirmation_request,
            result,
        )
        runtime.wall_clock.value = cycle.confirmation_request.expires_at

        expired = runtime.scenario.confirm(
            proposal,
            confirmation_id,
        )

        assert expired.status == 'timed_out'
        assert expired.code == 'confirmation_expired'
        assert runtime.voice.state == AWAITING_WAKE
        assert runtime.adapter.calls == ()


@pytest.mark.parametrize('operation', ['confirm', 'deny'])
def test_voice_side_expiry_clears_scenario_pending(operation) -> None:
    """Voice expiry cannot leave an already-active scenario tombstone."""
    with _runtime() as runtime:
        cycle, _, proposal = _propose(runtime)
        request = cycle.confirmation_request
        with patch(
            'malbut_agent_server.continuous_voice.time.time',
            return_value=request.expires_at,
        ):
            expired = runtime.voice.run_once()
        assert expired.code == 'confirmation_expired'
        assert runtime.voice.state == AWAITING_WAKE

        with pytest.raises(RoomLiveScenarioValidationError):
            if operation == 'confirm':
                runtime.scenario.confirm(
                    proposal,
                    'expired-confirmation',
                )
            else:
                runtime.scenario.deny(proposal)

        with pytest.raises(
            RoomLiveScenarioValidationError,
            match='voice proposal binding is invalid',
        ):
            runtime.scenario.propose_from_voice(cycle)


@pytest.mark.parametrize('operation', ['confirm', 'deny'])
def test_revoked_authority_clears_pending_voice_state(operation) -> None:
    """Revocation is terminal for both pending state machines."""
    with _runtime() as runtime:
        _, result, proposal = _propose(runtime)
        runtime.trust.revoke(result)

        if operation == 'confirm':
            feedback = runtime.scenario.confirm(
                proposal,
                'revoked-confirmation',
            )
        else:
            feedback = runtime.scenario.deny(proposal)

        assert feedback.code == 'authority_revoked'
        assert runtime.voice.state == AWAITING_WAKE
        assert runtime.adapter.calls == ()


def test_navigation_failure_never_claims_live_readiness() -> None:
    """A failed fake movement stops before coverage or live readiness."""
    adapter = SimulationRoomMissionAdapter(fail_phase='navigating')
    with _runtime(adapter) as runtime:
        cycle, result, proposal = _propose(runtime)
        confirmation_id = runtime.trust.issue(
            cycle.confirmation_request,
            result,
        )
        terminal = runtime.scenario.confirm_and_execute(
            proposal,
            confirmation_id,
        )

        assert terminal.status == 'failed'
        assert terminal.code == 'navigation_failed'
        assert terminal.viewer_live is False
        assert terminal.physical_effects is False
        assert [phase for _, phase in adapter.calls] == [
            'preflight',
            'navigating',
        ]
        assert runtime.voice.state == AWAITING_WAKE


def test_server_resolved_privacy_state_blocks_every_adapter_phase() -> None:
    """Execution state comes from the injected trusted resolver only."""
    with _runtime() as runtime:
        cycle, result, proposal = _propose(runtime)
        confirmation_id = runtime.trust.issue(
            cycle.confirmation_request,
            result,
        )
        runtime.set_mission_state(privacy_mode=True)

        terminal = runtime.scenario.confirm_and_execute(
            proposal,
            confirmation_id,
        )

        assert terminal.status == 'failed'
        assert terminal.code == 'privacy_mode'
        assert terminal.viewer_live is False
        assert runtime.adapter.calls == ()
        assert runtime.voice.state == AWAITING_WAKE


def test_running_cancellation_rearms_wake_only_after_terminal() -> None:
    """A running fake mission keeps wake fenced until cancellation ends."""
    navigation_started = threading.Event()
    navigation_release = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate(
            'navigating',
            navigation_started,
            navigation_release,
        ),
    ))
    with _runtime(adapter) as runtime:
        cycle, result, proposal = _propose(runtime)
        confirmation_id = runtime.trust.issue(
            cycle.confirmation_request,
            result,
        )
        confirmed = runtime.scenario.confirm(
            proposal,
            confirmation_id,
        )
        execution = []
        worker = threading.Thread(
            target=lambda: execution.append(runtime.scenario.execute(
                confirmed.tool_call_id,
                proposal,
            ))
        )
        worker.start()
        assert navigation_started.wait(timeout=1)

        blocked = runtime.voice.run_once()
        assert blocked.code == 'mission_in_progress'
        assert runtime.wake_source.calls == 1

        cancelled = runtime.scenario.cancel(
            confirmed.tool_call_id,
            proposal,
        )
        assert cancelled.status == 'cancelled'
        assert runtime.voice.state == AWAITING_WAKE
        navigation_release.set()
        worker.join(timeout=1)

        assert not worker.is_alive()
        assert execution[0].status == 'cancelled'
        next_cycle = runtime.voice.run_once()
        assert next_cycle.code == 'message'
        assert runtime.wake_source.calls == 2


def test_concurrent_execute_replay_cannot_rearm_wake_early() -> None:
    """A duplicate execute stays rejected while the first worker runs."""
    navigation_started = threading.Event()
    navigation_release = threading.Event()
    adapter = SimulationRoomMissionAdapter(phase_gates=(
        SimulationPhaseGate(
            'navigating',
            navigation_started,
            navigation_release,
        ),
    ))
    with _runtime(adapter) as runtime:
        cycle, result, proposal = _propose(runtime)
        confirmation_id = runtime.trust.issue(
            cycle.confirmation_request,
            result,
        )
        confirmed = runtime.scenario.confirm(
            proposal,
            confirmation_id,
        )
        first = []
        worker = threading.Thread(
            target=lambda: first.append(runtime.scenario.execute(
                confirmed.tool_call_id,
                proposal,
            ))
        )
        worker.start()
        assert navigation_started.wait(timeout=1)

        replay = runtime.scenario.execute(
            confirmed.tool_call_id,
            proposal,
        )
        assert replay.status == 'rejected'
        assert replay.code == 'execution_replay'
        assert runtime.voice.state == MISSION_WAIT
        blocked = runtime.voice.run_once()
        assert blocked.code == 'mission_in_progress'
        assert runtime.wake_source.calls == 1

        navigation_release.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert first[0].status == 'succeeded'
        assert runtime.voice.state == AWAITING_WAKE


def test_execute_exception_fails_voice_closed_and_clears_active() -> None:
    """Unexpected mission failure cannot strand voice in mission wait."""
    with _runtime() as runtime:
        cycle, result, proposal = _propose(runtime)
        confirmation_id = runtime.trust.issue(
            cycle.confirmation_request,
            result,
        )
        confirmed = runtime.scenario.confirm(
            proposal,
            confirmation_id,
        )

        with patch.object(
            RoomMonitoringMission,
            'execute',
            side_effect=RuntimeError('private adapter detail'),
        ):
            with pytest.raises(
                RoomLiveScenarioValidationError,
                match='room mission execution failed',
            ) as caught:
                runtime.scenario.execute(
                    confirmed.tool_call_id,
                    proposal,
                )

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert 'private adapter detail' not in repr(caught.value)
        assert runtime.voice.state == AWAITING_WAKE
        assert runtime.adapter.calls == ()
        with pytest.raises(RoomLiveScenarioValidationError):
            runtime.scenario.execute(
                confirmed.tool_call_id,
                proposal,
            )


def test_active_authority_revocation_terminally_rearms_voice() -> None:
    """Revoked confirmed work cannot wedge or later revive a voice cycle."""
    with _runtime() as runtime:
        cycle, result, proposal = _propose(runtime)
        authority = runtime.trust.authorities[result.request_id]
        confirmation_id = runtime.trust.issue(
            cycle.confirmation_request,
            result,
        )
        confirmed = runtime.scenario.confirm(
            proposal,
            confirmation_id,
        )
        runtime.trust.active.remove(id(authority))

        revoked = runtime.scenario.execute(
            confirmed.tool_call_id,
            proposal,
        )

        assert revoked.code == 'authority_revoked'
        assert runtime.voice.state == AWAITING_WAKE
        assert runtime.adapter.calls == ()
        runtime.trust.active.add(id(authority))
        with pytest.raises(RoomLiveScenarioValidationError):
            runtime.scenario.execute(
                confirmed.tool_call_id,
                proposal,
            )
        assert runtime.adapter.calls == ()


def test_voice_side_confirmation_expiry_discards_stale_proposal() -> None:
    """A later valid voice cycle replaces an expired scenario binding."""
    with _runtime() as runtime:
        cycle, _, _ = _propose(runtime)
        request = cycle.confirmation_request
        with patch(
            'malbut_agent_server.continuous_voice.time.time',
            return_value=request.expires_at + 1.0,
        ):
            expired = runtime.voice.run_once()
        assert expired.code == 'confirmation_expired'
        assert runtime.voice.state == AWAITING_WAKE

        runtime.voice.transcript_source.transcripts.clear()
        runtime.voice.transcript_source.transcripts.append(_transcript(
            '거실로 가서 방 전체를 라이브로 보여줘'
        ))
        next_cycle = runtime.voice.run_once()
        assert next_cycle.state == AWAITING_CONFIRMATION
        next_result = next_cycle.pipeline_result.agent_result
        runtime.trust.register(next_result)

        proposed = runtime.scenario.propose_from_voice(next_cycle)

        assert proposed.proposal is not None
        assert runtime.voice.state == AWAITING_CONFIRMATION
        assert runtime.adapter.calls == ()


def test_reentrant_authority_resolver_cannot_split_scenario_state() -> None:
    """A resolver callback cannot nest a second proposal transition."""
    with _runtime() as runtime:
        cycle = runtime.voice.run_once()
        result = cycle.pipeline_result.agent_result
        runtime.trust.register(result)

        def reenter() -> None:
            with pytest.raises(
                RoomLiveScenarioValidationError,
                match='already active',
            ):
                runtime.scenario.propose_from_voice(cycle)

        runtime.trust.on_resolve = reenter
        proposed = runtime.scenario.propose_from_voice(cycle)

        assert proposed.proposal is not None
        assert runtime.voice.state == AWAITING_CONFIRMATION
        assert runtime.adapter.calls == ()
        denied = runtime.scenario.deny(proposed.proposal)
        assert denied.code == 'confirmation_denied'
        assert runtime.voice.state == AWAITING_WAKE


def test_proposal_exception_is_sanitized_and_rearms_voice() -> None:
    """A collaborator exception cannot leak or strand pending voice."""
    with _runtime() as runtime:
        cycle = runtime.voice.run_once()
        result = cycle.pipeline_result.agent_result
        runtime.trust.register(result)

        with patch.object(
            RoomMonitoringMission,
            'propose',
            side_effect=RuntimeError('private-proposal-sentinel'),
        ):
            with pytest.raises(
                RoomLiveScenarioValidationError,
                match='room mission proposal failed',
            ) as caught:
                runtime.scenario.propose_from_voice(cycle)

        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert 'private-proposal-sentinel' not in repr(caught.value)
        assert runtime.voice.state == AWAITING_WAKE
        assert runtime.adapter.calls == ()


def test_bridge_rejects_non_confirmation_voice_cycles() -> None:
    """Ordinary chat output cannot be reinterpreted as a room proposal."""
    with _runtime() as runtime:
        ordinary = runtime.voice.run_once()
        forged = replace(
            ordinary,
            status='responded',
            code='message',
            confirmation_request=None,
        )

        with pytest.raises(RoomLiveScenarioValidationError):
            runtime.scenario.propose_from_voice(forged)
        assert runtime.adapter.calls == ()


def test_bridge_rejects_displayed_argument_mutation() -> None:
    """Prompt arguments cannot diverge from the bound Agent decision."""
    with _runtime() as runtime:
        cycle = runtime.voice.run_once()
        mutated_request = replace(
            cycle.confirmation_request,
            arguments={'location': '침실'},
        )
        mutated_cycle = replace(
            cycle,
            confirmation_request=mutated_request,
        )

        with pytest.raises(RoomLiveScenarioValidationError):
            runtime.scenario.propose_from_voice(mutated_cycle)
        assert runtime.adapter.calls == ()
