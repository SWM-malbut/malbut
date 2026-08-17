"""Trusted transcript to non-authorizing room confirmation request."""

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from types import MappingProxyType

import pytest

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
from malbut_agent_server.schemas import RobotState
from malbut_agent_server.speech import (
    SPEECH_SCHEMA_VERSION,
    MonitorRoomTargetRequest,
    SpeechActivityEvent,
    SpeechConversationCoordinator,
    SpeechTranscriptEvent,
    TrustedSpeechBinding,
)


def _target(location: str) -> TargetBinding:
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
        {'location': location},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return TargetBinding(
        device_id='monitor-sim-device',
        device_binding_revision='monitor-device-binding-1',
        source_revision='monitor-source-revision-1',
        map_id='monitor-map',
        map_revision='monitor-map-revision-1',
        semantic_revision='a' * 64,
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


class StaticTargetResolver:
    """Return one deterministic trusted target for speech tests."""

    def __init__(self) -> None:
        """Retain calls for exact binding assertions."""
        self.calls = []

    def resolve(self, request: MonitorRoomTargetRequest) -> TargetBinding:
        """Resolve the exact model location without external I/O."""
        self.calls.append(request)
        return _target(request.location)


class FailingTargetResolver:
    """Simulate an unavailable trusted semantic repository."""

    def resolve(self, request: MonitorRoomTargetRequest) -> TargetBinding:
        """Fail without exposing repository details to the speech result."""
        del request
        raise RuntimeError('private semantic repository failure')


class BlockingTargetResolver(StaticTargetResolver):
    """Pause resolution to exercise control-event races."""

    def __init__(self) -> None:
        """Create deterministic resolution barriers."""
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def resolve(self, request: MonitorRoomTargetRequest) -> TargetBinding:
        """Block until the test advances the speech capture epoch."""
        self.started.set()
        if not self.release.wait(timeout=3.0):
            raise RuntimeError('target resolver test timed out')
        return super().resolve(request)


def _binding() -> TrustedSpeechBinding:
    return TrustedSpeechBinding.from_dict(
        {
            'user_id': 'monitor-voice-user',
            'speaker_id': 'monitor-trusted-speaker',
            'speech_session_id': 'monitor-speech-session',
            'conversation_id': 'monitor-voice-conversation',
            'source': 'trusted-scripted-stt',
        }
    )


def _event(
    *,
    utterance_id: str = 'monitor-utterance-1',
    sequence: int = 1,
    capture_epoch: int = 1,
    text: str = '거실 전체를 보여줘',
) -> SpeechTranscriptEvent:
    return SpeechTranscriptEvent.from_dict(
        {
            'schema_version': SPEECH_SCHEMA_VERSION,
            'utterance_id': utterance_id,
            'speech_session_id': 'monitor-speech-session',
            'conversation_id': 'monitor-voice-conversation',
            'speaker_id': 'monitor-trusted-speaker',
            'source': 'trusted-scripted-stt',
            'sequence': sequence,
            'capture_epoch': capture_epoch,
            'source_timestamp_ns': sequence * 1000000000,
            'text': text,
            'confidence': 0.98,
            'is_final': True,
            'capture_origin': 'microphone',
            'audio_metadata': {
                'duration_ms': 900,
                'sample_rate_hz': 16000,
                'channel_count': 1,
            },
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
    """Return a fresh state snapshot bound to the semantic test map."""

    def __init__(self, **overrides) -> None:
        """Retain deterministic metadata overrides and read count."""
        self.overrides = overrides
        self.calls = 0
        self.evidence = None

    def read(self) -> TrustedRobotStateEvidence:
        """Build one complete current snapshot per proposal/replay."""
        self.calls += 1
        if self.evidence is not None:
            return self.evidence
        assembled = _boottime_ns()
        receipt = RobotStateFieldEvidence(
            source='test_ros_topic',
            received_boottime_ns=assembled,
        )
        values = {
            'evidence_digest': hashlib.sha256(
                f'test-evidence-{self.calls}'.encode('ascii')
            ).hexdigest(),
            'device_id': 'monitor-sim-device',
            'map_id': 'monitor-map',
            'map_revision': 'monitor-map-revision-1',
            'host_boot_id': '11111111-1111-4111-8111-111111111111',
            'instance_id': '22222222-2222-4222-8222-222222222222',
            'sequence': self.calls,
            'assembled_at': '2026-08-15T00:00:00+00:00',
            'assembled_boottime_ns': assembled,
            'valid_until_boottime_ns': assembled + 4_000_000_000,
            'battery_percent': 80.0,
            'navigation_available': True,
            'localization_ok': True,
            'emergency_stop': False,
            'camera_available': True,
            'privacy_mode': False,
            'docked': False,
            'forbidden_zones': (),
            'field_evidence': {
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
            },
        }
        values.update(self.overrides)
        self.evidence = TrustedRobotStateEvidence(**values)
        return self.evidence


def _activity(capture_epoch: int = 1) -> SpeechActivityEvent:
    return SpeechActivityEvent.from_dict(
        {
            'schema_version': SPEECH_SCHEMA_VERSION,
            'event_id': 'monitor-barge-in-1',
            'speech_session_id': 'monitor-speech-session',
            'speaker_id': 'monitor-trusted-speaker',
            'source': 'trusted-scripted-stt',
            'capture_epoch': capture_epoch,
            'source_timestamp_ns': 1500000000,
        }
    )


@contextmanager
def _runtime(target_resolver=None, state_source=None):
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=MockProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(
            monitorable_locations=['거실'],
        ),
        trusted_robot_state_source=(
            state_source or StaticRobotStateSource()
        ),
        capability_registry=production_registry(),
    )
    coordinator = SpeechConversationCoordinator(
        orchestrator,
        monitor_room_target_resolver=target_resolver,
    )
    coordinator.open_session(_binding())
    try:
        yield coordinator, conversation_store
    finally:
        coordinator.close_session(
            'monitor-speech-session',
            'monitor-close-1',
        )
        conversation_store.close()
        memory_store.close()


def _restartable_runtime(
    database_path,
    target_resolver,
    state_source=None,
):
    memory_store = SQLiteMemoryStore(str(database_path))
    conversation_store = SQLiteConversationStore(str(database_path))
    orchestrator = AgentOrchestrator(
        provider=MockProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(monitorable_locations=['거실']),
        trusted_robot_state_source=(
            state_source or StaticRobotStateSource()
        ),
        capability_registry=production_registry(),
    )
    coordinator = SpeechConversationCoordinator(
        orchestrator,
        monitor_room_target_resolver=target_resolver,
    )
    coordinator.open_session(_binding())
    return coordinator, conversation_store, memory_store


def test_trusted_transcript_stops_at_immutable_confirmation() -> None:
    """No Tool ID, adapter call, ROS action, or camera call is issued."""
    resolver = StaticTargetResolver()
    with _runtime(resolver) as (coordinator, _store):
        result = coordinator.handle_transcript(
            _event(),
            robot_state=_robot_state(),
            available_tools=['monitor_room'],
        )
        assert result.status == 'awaiting_confirmation'
        assert result.code == 'tool_confirmation_required'
        assert result.agent_result is not None
        assert result.agent_result.decision.tool_name == 'monitor_room'
        assert result.agent_result.to_dict()['execution'] == {
            'authorized': False,
            'consume_once': False,
            'tool_call_id': None,
            'proposal_authorized': True,
            'decision_id': result.agent_result.decision_id,
            'issued_at': result.agent_result.issued_at,
            'expires_at': result.agent_result.expires_at,
            'state_trusted': True,
            'state_evidence_scope': 'monitor_room',
            'state_evidence': {
                'scope': 'monitor_room',
                'evidence_digest': (
                    result.agent_result.state_evidence.evidence_digest
                ),
                'current': True,
            },
            'fresh': True,
        }

        confirmation = result.confirmation_request
        assert confirmation is not None
        assert isinstance(confirmation.arguments, MappingProxyType)
        assert confirmation.arguments_dict() == {'location': '거실'}
        assert confirmation.execution_authorized is False
        assert confirmation.decision_id == result.agent_result.decision_id
        assert confirmation.speech_session_id == 'monitor-speech-session'
        assert confirmation.source_utterance_id == 'monitor-utterance-1'
        assert confirmation.conversation_generation == 1
        assert len(confirmation.proposal_fingerprint) == 64
        assert len(resolver.calls) == 1
        assert resolver.calls[0].user_id == 'monitor-voice-user'
        assert resolver.calls[0].location == '거실'
        public = confirmation.to_dict()
        assert public['risk_level'] == 'L3'
        assert public['execution_authorized'] is False
        assert public['target']['room_name'] == '거실'
        assert result.tts_request is not None
        assert result.tts_request.text == confirmation.message
        assert result.tts_request.text == (
            '거실에서 로봇이 이동하고 방 전체를 확인하며 카메라 영상을 '
            '실시간 전송할까요? 최대 300초, 녹화 사용 안 함, 마이크 '
            '사용 안 함, 말하기 사용 안 함입니다.'
        )
        with pytest.raises(TypeError):
            confirmation.arguments['location'] = '침실'

        repeated = coordinator.handle_transcript(
            _event(),
            robot_state=_robot_state(),
            available_tools=['monitor_room'],
        )
        assert repeated == result

        terminal = coordinator.mark_tts_terminal(
            'monitor-speech-session',
            result.tts_request.request_id,
        )
        assert terminal.code == 'tts_terminal'
        blocked = coordinator.handle_transcript(
            _event(
                utterance_id='monitor-utterance-2',
                sequence=2,
                capture_epoch=terminal.capture_epoch,
            ),
            robot_state=_robot_state(),
            available_tools=['monitor_room'],
        )
        assert blocked.status == 'clarification'
        assert blocked.code == 'confirmation_response_unrecognized'
        assert blocked.confirmation_request is confirmation


def test_default_runtime_fails_closed_without_a_target_resolver() -> None:
    """A model proposal alone cannot create confirmation or TTS output."""
    with _runtime() as (coordinator, store):
        result = coordinator.handle_transcript(
            _event(),
            robot_state=_robot_state(),
            available_tools=['monitor_room'],
        )

        assert result.status == 'rejected'
        assert result.code == 'monitor_room_target_resolver_unavailable'
        assert result.agent_result is None
        assert result.confirmation_request is None
        assert result.tts_request is None
        assert store.list_turns(
            'monitor-voice-user',
            'monitor-voice-conversation',
        ) == []


def test_target_resolver_error_is_typed_and_does_not_poison_dialogue() -> None:
    """A private resolver failure emits no prompt and allows a later turn."""
    with _runtime(FailingTargetResolver()) as (coordinator, store):
        failed = coordinator.handle_transcript(
            _event(),
            robot_state=_robot_state(),
            available_tools=['monitor_room'],
        )

        assert failed.status == 'rejected'
        assert failed.code == 'monitor_room_target_resolution_failed'
        assert failed.agent_result is None
        assert failed.confirmation_request is None
        assert failed.tts_request is None
        assert store.list_turns(
            'monitor-voice-user',
            'monitor-voice-conversation',
        ) == []

        next_turn = coordinator.handle_transcript(
            _event(
                utterance_id='monitor-utterance-2',
                sequence=2,
                text='안녕',
            ),
        )
        assert next_turn.status == 'responded'
        assert next_turn.code == 'final_transcript_processed'
        assert next_turn.confirmation_request is None
        assert next_turn.tts_request is not None


@pytest.mark.parametrize(
    ('field_name', 'field_value'),
    [
        ('device_id', 'other-device'),
        ('map_id', 'other-map'),
        ('map_revision', 'other-map-revision'),
    ],
)
def test_semantic_target_must_match_current_robot_state_binding(
    field_name: str,
    field_value: str,
) -> None:
    """A cross-device or cross-map target creates no durable prompt/TTS."""
    source = StaticRobotStateSource(
        **{field_name: field_value},
    )
    with _runtime(
        StaticTargetResolver(),
        state_source=source,
    ) as (coordinator, store):
        result = coordinator.handle_transcript(
            _event(),
            # This matching client claim is deliberately irrelevant.
            robot_state=_robot_state(),
            available_tools=['monitor_room'],
        )

        assert result.status == 'rejected'
        assert result.code == 'monitor_room_state_target_mismatch'
        assert result.agent_result is None
        assert result.confirmation_request is None
        assert result.tts_request is None
        assert store.list_turns(
            'monitor-voice-user',
            'monitor-voice-conversation',
        ) == []


def test_target_resolution_releases_lock_and_rechecks_capture_epoch() -> None:
    """Barge-in wins while semantic I/O is blocked and suppresses output."""
    resolver = BlockingTargetResolver()
    with _runtime(resolver) as (coordinator, store):
        outcome = {}

        def invoke() -> None:
            outcome['result'] = coordinator.handle_transcript(
                _event(),
                robot_state=_robot_state(),
                available_tools=['monitor_room'],
            )

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        assert resolver.started.wait(timeout=1.0)

        barge_in = coordinator.handle_barge_in(_activity())
        assert barge_in.status == 'ready'
        assert barge_in.code == 'capture_epoch_advanced'
        resolver.release.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()

        result = outcome['result']
        assert result.status == 'discarded'
        assert result.code == 'capture_epoch_changed_during_inference'
        assert result.confirmation_request is None
        assert result.tts_request is None
        assert store.list_turns(
            'monitor-voice-user',
            'monitor-voice-conversation',
        ) == []


def test_restart_replays_durable_pending_target_without_network(
    tmp_path,
) -> None:
    """An exact cached request restores its stored prompt offline."""
    database_path = tmp_path / 'monitor-room-replay.sqlite3'
    first_resolver = StaticTargetResolver()
    state_source = StaticRobotStateSource()
    first, first_store, first_memory = _restartable_runtime(
        database_path,
        first_resolver,
        state_source,
    )
    initial = first.handle_transcript(
        _event(),
        robot_state=_robot_state(),
        available_tools=['monitor_room'],
    )
    assert initial.status == 'awaiting_confirmation'
    assert len(first_resolver.calls) == 1
    first_store.close()
    first_memory.close()

    failing_resolver = FailingTargetResolver()
    second, second_store, second_memory = _restartable_runtime(
        database_path,
        failing_resolver,
        state_source,
    )
    try:
        replay = second.handle_transcript(
            _event(),
            robot_state=_robot_state(),
            available_tools=['monitor_room'],
        )
        assert replay.status == 'awaiting_confirmation'
        assert replay.code == 'tool_confirmation_required'
        assert replay.confirmation_request is not None
        assert (
            replay.confirmation_request.proposal_fingerprint
            == initial.confirmation_request.proposal_fingerprint
        )
        assert replay.tts_request.text == initial.tts_request.text
        assert len(second_store.list_turns(
            'monitor-voice-user',
            'monitor-voice-conversation',
        )) == 1
    finally:
        second_store.close()
        second_memory.close()
