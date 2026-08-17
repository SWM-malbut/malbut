"""Non-authorizing confirmation lifecycle for room-monitoring speech."""

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import MappingProxyType

import pytest

from malbut_agent_server.confirmation import (
    AuthenticatedUIActor,
    ToolConfirmationResponseEvent,
    ToolConfirmationUIResponseEvent,
    build_confirmation_resolution,
    classify_confirmation_response,
)
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gateway import production_registry
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.monitor_room_target import Effects, TargetBinding
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.robot_state import (
    RobotStateFieldEvidence,
    TrustedRobotStateEvidence,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import RobotState, ValidationError
from malbut_agent_server.speech import (
    SPEECH_SCHEMA_VERSION,
    MonitorRoomTargetRequest,
    SpeechActivityEvent,
    SpeechConversationCoordinator,
    SpeechTranscriptEvent,
    TrustedSpeechBinding,
)


class StaticTargetResolver:
    """Return a fixed trusted living-room target for lifecycle tests."""

    def resolve(self, request: MonitorRoomTargetRequest) -> TargetBinding:
        """Bind the exact proposal arguments to one immutable target."""
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
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        arguments_json = json.dumps(
            {'location': request.location},
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        return TargetBinding(
            device_id='confirmation-sim-device',
            device_binding_revision='confirmation-device-binding-1',
            source_revision='confirmation-source-revision-1',
            map_id='confirmation-map',
            map_revision='confirmation-map-revision-1',
            semantic_revision='b' * 64,
            frame_id='map',
            room_id='room-living',
            room_name='거실',
            room_category='living_room',
            source_arguments_digest=hashlib.sha256(
                arguments_json.encode('utf-8')
            ).hexdigest(),
            geometry_json=geometry_json,
            geometry_digest=hashlib.sha256(
                geometry_json.encode('utf-8')
            ).hexdigest(),
            representative_point=(2.0, 2.0),
            clearance_m=1.0,
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


class CountingMockProvider(MockProvider):
    """Count calls to prove confirmation text never reaches a model."""

    def __init__(self) -> None:
        """Initialize the deterministic counter."""
        super().__init__()
        self.calls = 0

    def complete(self, *args, **kwargs):
        """Count and delegate one offline completion."""
        self.calls += 1
        return super().complete(*args, **kwargs)


class MutableClock:
    """Controllable server clock for confirmation expiry tests."""

    def __init__(self) -> None:
        """Begin close to the same wall clock used by orchestration."""
        self.now = time.time()

    def __call__(self) -> float:
        """Return the current synthetic server time."""
        return self.now


class ReentrantCloseClock:
    """Close the speech session while a confirmation samples time."""

    def __init__(self) -> None:
        """Start disarmed so proposal construction can finish."""
        self.now = time.time()
        self.coordinator = None
        self.armed = False

    def __call__(self) -> float:
        """Reenter exactly once before returning server time."""
        if self.armed:
            self.armed = False
            self.coordinator.close_session(
                'confirmation-speech-session',
                'reentrant-clock-close',
            )
        return self.now


def _binding() -> TrustedSpeechBinding:
    return TrustedSpeechBinding.from_dict(
        {
            'user_id': 'confirmation-user',
            'speaker_id': 'confirmation-speaker',
            'speech_session_id': 'confirmation-speech-session',
            'conversation_id': 'confirmation-conversation',
            'source': 'trusted-scripted-stt',
        }
    )


def _event(
    *,
    utterance_id: str,
    sequence: int,
    capture_epoch: int,
    text: str,
    confidence: float = 0.98,
) -> SpeechTranscriptEvent:
    return SpeechTranscriptEvent.from_dict(
        {
            'schema_version': SPEECH_SCHEMA_VERSION,
            'utterance_id': utterance_id,
            'speech_session_id': 'confirmation-speech-session',
            'conversation_id': 'confirmation-conversation',
            'speaker_id': 'confirmation-speaker',
            'source': 'trusted-scripted-stt',
            'sequence': sequence,
            'capture_epoch': capture_epoch,
            'source_timestamp_ns': sequence * 1000000000,
            'text': text,
            'confidence': confidence,
            'is_final': True,
            'capture_origin': 'microphone',
            'audio_metadata': {
                'duration_ms': 500,
                'sample_rate_hz': 16000,
                'channel_count': 1,
            },
        }
    )


def _activity(capture_epoch: int) -> SpeechActivityEvent:
    return SpeechActivityEvent.from_dict(
        {
            'schema_version': SPEECH_SCHEMA_VERSION,
            'event_id': f'confirmation-activity-{capture_epoch}',
            'speech_session_id': 'confirmation-speech-session',
            'speaker_id': 'confirmation-speaker',
            'source': 'trusted-scripted-stt',
            'capture_epoch': capture_epoch,
            'source_timestamp_ns': 1500000000,
        }
    )


def _robot_state() -> RobotState:
    return RobotState.from_dict(
        {
            'battery_percent': 80,
            'navigation_available': True,
            'localization_ok': True,
            'camera_available': True,
            'privacy_mode': False,
        }
    )


def _boottime_ns() -> int:
    clock_id = getattr(time, 'CLOCK_BOOTTIME', None)
    if clock_id is not None:
        return time.clock_gettime_ns(clock_id)
    return time.monotonic_ns()


class StaticRobotStateSource:
    """Return one current snapshot bound to the lifecycle target."""

    def __init__(self) -> None:
        """Create stable request-scoped evidence for one test runtime."""
        assembled = _boottime_ns()
        receipt = RobotStateFieldEvidence(
            source='test_ros_topic',
            received_boottime_ns=assembled,
        )
        self.evidence = TrustedRobotStateEvidence(
            evidence_digest=hashlib.sha256(
                b'confirmation-lifecycle-state'
            ).hexdigest(),
            device_id='confirmation-sim-device',
            map_id='confirmation-map',
            map_revision='confirmation-map-revision-1',
            host_boot_id='11111111-1111-4111-8111-111111111111',
            instance_id='22222222-2222-4222-8222-222222222222',
            sequence=1,
            assembled_at='2026-08-15T00:00:00+00:00',
            assembled_boottime_ns=assembled,
            valid_until_boottime_ns=assembled + 4_000_000_000,
            battery_percent=80.0,
            navigation_available=True,
            localization_ok=True,
            emergency_stop=False,
            camera_available=True,
            privacy_mode=False,
            docked=False,
            forbidden_zones=(),
            field_evidence=MappingProxyType({
                name: receipt
                for name in (
                    'battery_percent',
                    'navigation_available',
                    'localization_ok',
                    'emergency_stop',
                    'camera_available',
                    'privacy_mode',
                    'docked',
                    'forbidden_zones',
                )
            }),
        )

    def read(self) -> TrustedRobotStateEvidence:
        """Return the immutable evidence snapshot."""
        return self.evidence


@contextmanager
def _runtime(
    clock=time.time,
    event_cache_size=256,
    database_path=':memory:',
):
    memory_store = SQLiteMemoryStore(database_path)
    conversation_store = SQLiteConversationStore(
        database_path,
        clock=clock,
    )
    provider = CountingMockProvider()
    orchestrator = AgentOrchestrator(
        provider=provider,
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(monitorable_locations=['거실']),
        trusted_robot_state_source=StaticRobotStateSource(),
        capability_registry=production_registry(),
    )
    coordinator = SpeechConversationCoordinator(
        orchestrator,
        clock=clock,
        event_cache_size=event_cache_size,
        monitor_room_target_resolver=StaticTargetResolver(),
    )
    coordinator.open_session(_binding())
    try:
        yield coordinator, provider
    finally:
        coordinator.close_session(
            'confirmation-speech-session',
            'confirmation-test-close',
        )
        conversation_store.close()
        memory_store.close()


def _propose(coordinator: SpeechConversationCoordinator):
    result = coordinator.handle_transcript(
        _event(
            utterance_id='confirmation-proposal-1',
            sequence=1,
            capture_epoch=1,
            text='거실 전체를 보여줘',
        ),
        robot_state=_robot_state(),
        available_tools=['monitor_room'],
    )
    assert result.status == 'awaiting_confirmation'
    assert result.confirmation_request is not None
    assert result.tts_request is not None
    return result


def _finish_prompt(coordinator, proposal):
    terminal = coordinator.mark_tts_terminal(
        'confirmation-speech-session',
        proposal.tts_request.request_id,
    )
    assert terminal.code == 'tts_terminal'
    return terminal.capture_epoch


def _response_event(proposal, capture_epoch, **overrides):
    pending = proposal.confirmation_request
    values = {
        'response_id': 'confirmation-response-direct-1',
        'speech_session_id': pending.speech_session_id,
        'confirmation_request_id': pending.confirmation_request_id,
        'disposition': 'approve',
    }
    values.update(overrides)
    return ToolConfirmationUIResponseEvent(**values)


def _actor(user_id='confirmation-user'):
    return AuthenticatedUIActor(
        user_id=user_id,
        auth_session_id=f'{user_id}-web-session',
        authentication_method='test-authenticated-ui',
    )


def _bound_response_event(proposal, capture_epoch, **overrides):
    pending = proposal.confirmation_request
    values = {
        'response_id': 'confirmation-response-bound-1',
        'speech_session_id': pending.speech_session_id,
        'conversation_id': pending.conversation_id,
        'confirmation_request_id': pending.confirmation_request_id,
        'decision_id': pending.decision_id,
        'proposal_fingerprint': pending.proposal_fingerprint,
        'capture_epoch': capture_epoch,
        'disposition': 'approve',
    }
    values.update(overrides)
    return ToolConfirmationResponseEvent(**values)


@pytest.mark.parametrize(
    ('text', 'disposition', 'code'),
    [
        ('네', 'approve', 'confirmation_approval_recorded_no_execution'),
        ('아니요', 'deny', 'confirmation_denial_recorded'),
        ('취소', 'cancel', 'confirmation_cancelled'),
    ],
)
def test_voice_confirmation_is_local_and_non_authorizing(
    text: str,
    disposition: str,
    code: str,
) -> None:
    """Record exact voice choices without another provider call."""
    with _runtime() as (coordinator, provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        event = _event(
            utterance_id=f'confirmation-{disposition}-1',
            sequence=2,
            capture_epoch=epoch,
            text=text,
        )
        result = coordinator.handle_transcript(event)

        assert result.status == 'recorded'
        assert result.code == code
        assert provider.calls == 1
        resolution = result.confirmation_resolution
        assert resolution is not None
        assert resolution.disposition == disposition
        assert resolution.execution_authorized is False
        assert resolution.consume_once is False
        assert resolution.tool_call_id is None
        assert resolution.mission_id is None
        assert result.agent_result is None
        assert coordinator.handle_transcript(event) is result


def test_unrecognized_response_keeps_pending_until_exact_answer() -> None:
    """Keep the original request pending after an ambiguous answer."""
    with _runtime() as (coordinator, provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        unclear = coordinator.handle_transcript(
            _event(
                utterance_id='confirmation-unclear-1',
                sequence=2,
                capture_epoch=epoch,
                text='글쎄, 다른 것도 말하고 싶은데',
            )
        )
        assert unclear.status == 'clarification'
        assert unclear.code == 'confirmation_response_unrecognized'
        assert unclear.confirmation_resolution is None

        approved = coordinator.handle_transcript(
            _event(
                utterance_id='confirmation-approve-after-unclear',
                sequence=3,
                capture_epoch=epoch,
                text='네',
            )
        )
        assert approved.code == (
            'confirmation_approval_recorded_no_execution'
        )
        assert provider.calls == 1


def test_barge_in_cancels_prompt_but_preserves_confirmation() -> None:
    """Treat barge-in as playback control rather than a denial."""
    with _runtime() as (coordinator, provider):
        proposal = _propose(coordinator)
        barge_in = coordinator.handle_barge_in(_activity(1))
        assert barge_in.code == 'tts_cancel_requested'
        assert barge_in.cancel_request is not None

        approved = coordinator.handle_transcript(
            _event(
                utterance_id='confirmation-barge-approve',
                sequence=2,
                capture_epoch=barge_in.capture_epoch,
                text='네',
            )
        )
        assert approved.code == (
            'confirmation_approval_recorded_no_execution'
        )
        assert approved.confirmation_request == (
            proposal.confirmation_request
        )
        assert provider.calls == 1


def test_final_before_vad_is_retryable_and_can_be_replayed() -> None:
    """Do not poison a confirmation when final arrives before VAD."""
    with _runtime() as (coordinator, provider):
        proposal = _propose(coordinator)
        early = _event(
            utterance_id='confirmation-final-before-vad',
            sequence=2,
            capture_epoch=1,
            text='네',
        )

        retryable = coordinator.handle_transcript(early)
        barge_in = coordinator.handle_barge_in(_activity(1))
        approved = coordinator.handle_transcript(
            replace(early, capture_epoch=barge_in.capture_epoch)
        )

        assert retryable.status == 'retryable'
        assert retryable.code == 'confirmation_prompt_active'
        assert barge_in.code == 'tts_cancel_requested'
        assert approved.code == (
            'confirmation_approval_recorded_no_execution'
        )
        assert approved.confirmation_request == (
            proposal.confirmation_request
        )
        assert provider.calls == 1


def test_direct_response_replay_conflict_and_terminal_fence() -> None:
    """Replay exact responses and fence mutated or later decisions."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        event = _response_event(proposal, epoch)

        first = coordinator.handle_ui_confirmation_response(
            event,
            _actor(),
        )
        replay = coordinator.handle_ui_confirmation_response(
            event,
            _actor(),
        )
        conflict = coordinator.handle_ui_confirmation_response(
            replace(event, disposition='deny'),
            _actor(),
        )
        second_id = coordinator.handle_ui_confirmation_response(
            replace(event, response_id='confirmation-response-direct-2'),
            _actor(),
        )

        assert replay is first
        assert conflict.code == 'confirmation_response_conflict'
        assert second_id.code == 'confirmation_already_resolved'


def test_wrong_binding_cannot_poison_later_valid_response() -> None:
    """Reject a foreign actor without reserving its response ID."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        event = _response_event(proposal, epoch)

        wrong = coordinator.handle_ui_confirmation_response(
            event,
            _actor('different-user'),
        )
        correct = coordinator.handle_ui_confirmation_response(
            event,
            _actor(),
        )

        assert wrong.code == 'confirmation_unavailable'
        assert correct.code == (
            'confirmation_approval_recorded_no_execution'
        )


def test_resolution_helper_rejects_a_mismatched_snapshot() -> None:
    """Make the public builder enforce the same exact proposal binding."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        event = _bound_response_event(proposal, epoch)

        with pytest.raises(
            ValidationError,
            match='confirmation response does not match request',
        ):
            build_confirmation_resolution(
                proposal.confirmation_request,
                replace(event, decision_id='different-decision'),
                time.time(),
            )
        with pytest.raises(
            ValidationError,
            match='confirmation response time is invalid',
        ):
            build_confirmation_resolution(
                proposal.confirmation_request,
                event,
                proposal.confirmation_request.issued_at - 0.001,
            )


def test_ui_approve_during_prompt_records_and_cancels_prompt() -> None:
    """Keep explicit UI confirmation independent of an audio epoch."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        event = _response_event(proposal, 1)

        approved = coordinator.handle_ui_confirmation_response(
            event,
            _actor(),
        )
        conflict = coordinator.handle_ui_confirmation_response(
            replace(event, disposition='deny'),
            _actor(),
        )

        assert approved.status == 'recorded'
        assert approved.code == (
            'confirmation_approval_recorded_no_execution'
        )
        assert approved.tts_cancel_request is not None
        assert approved.tts_cancel_request.reason == 'confirmation_resolved'
        assert conflict.code == 'confirmation_response_conflict'
        late_tts = coordinator.mark_tts_terminal(
            'confirmation-speech-session',
            proposal.tts_request.request_id,
        )
        assert late_tts.code == 'tts_already_terminal'
        assert late_tts.capture_epoch == approved.capture_epoch


def test_ui_schema_rejects_audio_or_authority_fields() -> None:
    """Keep UI clicks free of voice epochs and execution claims."""
    base = {
        'schema_version': 2,
        'response_id': 'strict-ui-response',
        'speech_session_id': 'strict-ui-session',
        'confirmation_request_id': 'strict-ui-confirmation',
        'disposition': 'approve',
    }
    for field, value in (
        ('capture_epoch', 1),
        ('user_id', 'client-asserted-user'),
        ('arguments', {'location': '거실'}),
        ('tool_call_id', 'forged-tool-call'),
    ):
        with pytest.raises(
            ValidationError,
            match='contains unknown fields',
        ):
            ToolConfirmationUIResponseEvent.from_dict(
                {**base, field: value}
            )


def test_server_time_expiry_dominates_late_approval() -> None:
    """Make a server-side deadline terminal even for an approval word."""
    clock = MutableClock()
    with _runtime(clock) as (coordinator, provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        clock.now = proposal.confirmation_request.expires_at

        expired = coordinator.handle_transcript(
            _event(
                utterance_id='confirmation-expired-approve',
                sequence=2,
                capture_epoch=epoch,
                text='네',
            )
        )

        assert expired.status == 'expired'
        assert expired.code == 'confirmation_expired'
        assert expired.confirmation_resolution.disposition == 'expired'
        assert expired.confirmation_resolution.execution_authorized is False
        assert provider.calls == 1


def test_deadline_is_sampled_after_conversation_context_lookup() -> None:
    """Do not approve with wall time captured before a slow dependency."""
    clock = MutableClock()
    with _runtime(clock) as (coordinator, _provider):
        proposal = _propose(coordinator)
        _finish_prompt(coordinator, proposal)
        store = coordinator.orchestrator.conversation_store
        original_context = store._confirmation_context_code_locked

        def reach_deadline_before_return(*args, **kwargs):
            code = original_context(*args, **kwargs)
            clock.now = proposal.confirmation_request.expires_at
            return code

        store._confirmation_context_code_locked = (
            reach_deadline_before_return
        )
        clock.now = proposal.confirmation_request.expires_at - 1.0

        result = coordinator.handle_ui_confirmation_response(
            _response_event(proposal, 2),
            _actor(),
        )

        assert result.status == 'expired'
        assert result.code == 'confirmation_expired'
        assert result.confirmation_resolution.disposition == 'expired'


def test_housekeeping_expires_an_unanswered_confirmation() -> None:
    """Terminalize deadline expiry without consuming a later utterance."""
    clock = MutableClock()
    with _runtime(clock) as (coordinator, provider):
        proposal = _propose(coordinator)
        assert coordinator.expire_due_confirmations() == ()
        clock.now = proposal.confirmation_request.expires_at

        outcomes = coordinator.expire_due_confirmations()

        assert len(outcomes) == 1
        expired = outcomes[0]
        assert expired.status == 'expired'
        assert expired.code == 'confirmation_expired'
        assert expired.tts_cancel_request is not None
        assert expired.confirmation_resolution.disposition == 'expired'
        next_turn = coordinator.handle_transcript(
            _event(
                utterance_id='after-autonomous-expiry',
                sequence=2,
                capture_epoch=expired.capture_epoch,
                text='안녕',
            )
        )
        assert next_turn.status == 'responded'
        assert provider.calls == 2


def test_housekeeping_expiry_cannot_be_blocked_by_response_id_claim() -> None:
    """Keep the server deadline outside the user response namespace."""
    clock = MutableClock()
    with _runtime(clock) as (coordinator, _provider):
        proposal = _propose(coordinator)
        expiry_id = coordinator._confirmation_expiry_response_id(
            proposal.confirmation_request.confirmation_request_id
        )
        clock.now = float('nan')
        retryable = coordinator.handle_ui_confirmation_response(
            _response_event(
                proposal,
                1,
                response_id=expiry_id,
            ),
            _actor(),
        )
        assert retryable.code == 'confirmation_time_unavailable'

        clock.now = proposal.confirmation_request.expires_at
        outcomes = coordinator.expire_due_confirmations()

        assert len(outcomes) == 1
        assert outcomes[0].status == 'expired'
        assert outcomes[0].code == 'confirmation_expired'


def test_close_fences_late_confirmation_response() -> None:
    """Prevent a closed speech session from accepting a late response."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        event = _response_event(proposal, 1)
        closed = coordinator.close_session(
            'confirmation-speech-session',
            'confirmation-explicit-close',
        )
        late = coordinator.handle_ui_confirmation_response(
            event,
            _actor(),
        )

        assert closed.status == 'closed'
        assert closed.cancel_request is not None
        assert late.code == 'speech_session_closed'


def test_exact_ui_replay_survives_later_session_close() -> None:
    """Return the prior intent record without accepting a new decision."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        event = _response_event(proposal, 1)
        first = coordinator.handle_ui_confirmation_response(
            event,
            _actor(),
        )
        coordinator.close_session(
            'confirmation-speech-session',
            'close-after-ui-intent',
        )
        replay = coordinator.handle_ui_confirmation_response(
            event,
            _actor(),
        )

        assert replay is first


def test_reset_invalidates_the_pending_confirmation() -> None:
    """Bind approval to the exact active conversation generation."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        coordinator.orchestrator.conversation_store.reset(
            'confirmation-user',
            'confirmation-conversation',
        )

        stale = coordinator.handle_ui_confirmation_response(
            _response_event(proposal, epoch),
            _actor(),
        )

        assert stale.status == 'rejected'
        assert stale.code == 'confirmation_conversation_changed'
        assert stale.confirmation_resolution is None


def test_external_close_invalidates_the_pending_confirmation() -> None:
    """A closed conversation cannot retain an approval prompt."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        coordinator.orchestrator.conversation_store.close_session(
            'confirmation-user',
            'confirmation-conversation',
        )

        stale = coordinator.handle_ui_confirmation_response(
            _response_event(proposal, epoch),
            _actor(),
        )

        assert stale.status == 'rejected'
        assert stale.code == 'confirmation_conversation_inactive'
        assert stale.confirmation_resolution is None


def test_delete_and_recreate_is_fenced_by_session_instance() -> None:
    """Generation one in a new lifecycle cannot revive an old prompt."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        store = coordinator.orchestrator.conversation_store
        assert store.delete(
            'confirmation-user',
            'confirmation-conversation',
        )
        recreated = store.create(
            'confirmation-user',
            'confirmation-conversation',
        )
        assert recreated.generation == 1
        assert (
            recreated.session_instance_id
            != proposal.confirmation_request
            .conversation_session_instance_id
        )

        stale = coordinator.handle_ui_confirmation_response(
            _response_event(proposal, epoch),
            _actor(),
        )

        assert stale.status == 'rejected'
        assert stale.code == 'confirmation_conversation_not_found'
        assert stale.confirmation_resolution is None


def test_reentrant_store_clock_fails_without_recording_approval() -> None:
    """Treat a non-pure injected Store clock as persistence failure."""
    clock = ReentrantCloseClock()
    with _runtime(clock) as (coordinator, _provider):
        clock.coordinator = coordinator
        proposal = _propose(coordinator)
        _finish_prompt(coordinator, proposal)
        clock.armed = True

        result = coordinator.handle_ui_confirmation_response(
            _response_event(proposal, 2),
            _actor(),
        )

        assert result.status == 'retryable'
        assert result.code == 'confirmation_persistence_unavailable'
        assert result.confirmation_resolution is None
        record = (
            coordinator.orchestrator.conversation_store
            .get_confirmation_intent(
                'confirmation-user',
                proposal.confirmation_request.confirmation_request_id,
            )
        )
        assert record.state == 'pending'


def test_clock_failure_reserves_response_id_until_exact_retry() -> None:
    """Fail closed without allowing a retryable response ID mutation."""
    clock = MutableClock()
    with _runtime(clock) as (coordinator, _provider):
        proposal = _propose(coordinator)
        event = _response_event(proposal, 1)
        clock.now = float('nan')

        retryable = coordinator.handle_ui_confirmation_response(
            event,
            _actor(),
        )
        conflict = coordinator.handle_ui_confirmation_response(
            replace(event, disposition='deny'),
            _actor(),
        )
        clock.now = time.time()
        approved = coordinator.handle_ui_confirmation_response(
            event,
            _actor(),
        )

        assert retryable.status == 'retryable'
        assert retryable.code == 'confirmation_time_unavailable'
        assert conflict.code == 'confirmation_response_conflict'
        assert approved.code == (
            'confirmation_approval_recorded_no_execution'
        )


def test_active_response_claim_is_not_evicted_by_small_replay_cache() -> None:
    """Keep first-payload binding while one confirmation is unresolved."""
    clock = MutableClock()
    with _runtime(clock, event_cache_size=1) as (coordinator, _provider):
        proposal = _propose(coordinator)
        first = _response_event(proposal, 1)
        second = replace(first, response_id='confirmation-response-other')
        clock.now = float('nan')

        assert coordinator.handle_ui_confirmation_response(
            first,
            _actor(),
        ).code == 'confirmation_time_unavailable'
        assert coordinator.handle_ui_confirmation_response(
            second,
            _actor(),
        ).code == 'confirmation_time_unavailable'
        mutated = coordinator.handle_ui_confirmation_response(
            replace(first, disposition='deny'),
            _actor(),
        )

        assert mutated.code == 'confirmation_response_conflict'
        clock.now = time.time()
        exact = coordinator.handle_ui_confirmation_response(
            first,
            _actor(),
        )
        assert exact.code == 'confirmation_approval_recorded_no_execution'


def test_approve_and_deny_race_has_one_terminal_winner() -> None:
    """Linearize competing responses to one terminal outcome."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        approve = _response_event(proposal, epoch)
        deny = replace(
            approve,
            response_id='confirmation-race-deny',
            disposition='deny',
        )
        barrier = threading.Barrier(3)
        results = []

        def invoke(event) -> None:
            barrier.wait(timeout=2.0)
            results.append(
                    coordinator.handle_ui_confirmation_response(
                        event,
                        _actor(),
                    )
            )

        threads = [
            threading.Thread(target=invoke, args=(approve,)),
            threading.Thread(target=invoke, args=(deny,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2.0)
        for thread in threads:
            thread.join(timeout=2.0)

        recorded = [
            result for result in results if result.status == 'recorded'
        ]
        fenced = [
            result
            for result in results
            if result.code == 'confirmation_already_resolved'
        ]
        assert len(recorded) == 1
        assert len(fenced) == 1
        assert recorded[0].confirmation_resolution.tool_call_id is None


def test_voice_resolution_linearizes_before_barge_in(monkeypatch) -> None:
    """Keep durable and speech terminal state consistent when approval wins."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        store = coordinator.orchestrator.conversation_store
        original = store.resolve_confirmation_intent
        persistence_entered = threading.Event()
        release_persistence = threading.Event()
        barge_started = threading.Event()
        voice_results = []
        barge_results = []

        def blocked_resolution(**kwargs):
            persistence_entered.set()
            assert release_persistence.wait(timeout=2.0)
            return original(**kwargs)

        def approve() -> None:
            voice_results.append(
                coordinator.handle_transcript(
                    _event(
                        utterance_id='confirmation-race-voice',
                        sequence=2,
                        capture_epoch=epoch,
                        text='네',
                    )
                )
            )

        def barge() -> None:
            barge_started.set()
            barge_results.append(
                coordinator.handle_barge_in(_activity(epoch))
            )

        monkeypatch.setattr(
            store,
            'resolve_confirmation_intent',
            blocked_resolution,
        )
        voice_thread = threading.Thread(target=approve)
        voice_thread.start()
        assert persistence_entered.wait(timeout=2.0)
        barge_thread = threading.Thread(target=barge)
        barge_thread.start()
        assert barge_started.wait(timeout=2.0)
        assert barge_thread.is_alive()

        release_persistence.set()
        voice_thread.join(timeout=2.0)
        barge_thread.join(timeout=2.0)
        assert not voice_thread.is_alive()
        assert not barge_thread.is_alive()

        assert voice_results[0].code == (
            'confirmation_approval_recorded_no_execution'
        )
        assert barge_results[0].status == 'ready'
        durable = store.get_confirmation_intent(
            proposal.confirmation_request.user_id,
            proposal.confirmation_request.confirmation_request_id,
        )
        assert durable.state == 'resolved'
        assert durable.disposition == 'approve'
        state = coordinator._session_state(
            'confirmation-speech-session'
        )
        assert state.pending_confirmation is None


def test_external_terminal_writer_converges_local_pending_state(
    tmp_path,
) -> None:
    """Mirror a durable winner written by another server connection."""
    database = str(tmp_path / 'external-confirmation.sqlite3')
    with _runtime(database_path=database) as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        pending = proposal.confirmation_request
        external = SQLiteConversationStore(database)
        try:
            winner = external.resolve_confirmation_intent(
                user_id=pending.user_id,
                confirmation_request_id=(
                    pending.confirmation_request_id
                ),
                proposal_fingerprint=pending.proposal_fingerprint,
                response_id='external-denial-response',
                response_fingerprint='a' * 64,
                requested_disposition='deny',
                response_channel='ui_in_process',
                assurance_level='unverified_in_process_ui',
                provenance_ref='b' * 64,
            )
            assert winner.disposition == 'deny'

            local = coordinator.handle_ui_confirmation_response(
                _response_event(
                    proposal,
                    epoch,
                    response_id='local-losing-approval',
                    disposition='approve',
                ),
                _actor(),
            )
            assert local.status == 'recorded'
            assert local.code == 'confirmation_denial_recorded'
            assert local.confirmation_resolution is not None
            assert local.confirmation_resolution.disposition == 'deny'
            assert local.confirmation_resolution.response_id == (
                'external-denial-response'
            )
            state = coordinator._session_state(
                'confirmation-speech-session'
            )
            assert state.pending_confirmation is None
        finally:
            external.close()


def test_barge_in_before_voice_resolution_leaves_durable_pending() -> None:
    """Do not record an old-epoch approval after barge-in wins."""
    with _runtime() as (coordinator, _provider):
        proposal = _propose(coordinator)
        epoch = _finish_prompt(coordinator, proposal)
        barge = coordinator.handle_barge_in(_activity(epoch))
        stale = coordinator.handle_transcript(
            _event(
                utterance_id='confirmation-race-stale-voice',
                sequence=2,
                capture_epoch=epoch,
                text='네',
            )
        )

        assert barge.status == 'ready'
        assert stale.status == 'rejected'
        store = coordinator.orchestrator.conversation_store
        durable = store.get_confirmation_intent(
            proposal.confirmation_request.user_id,
            proposal.confirmation_request.confirmation_request_id,
        )
        assert durable.state == 'pending'


@pytest.mark.parametrize(
    'text',
    [
        '응 아니',
        '네, 침실로 바꿔',
        '"yes"는 예시야',
        'approve and move',
        '',
    ],
)
def test_confirmation_classifier_rejects_compound_or_meta_text(
    text: str,
) -> None:
    """Reject compound, quoted, or empty confirmation text."""
    assert classify_confirmation_response(text) is None
