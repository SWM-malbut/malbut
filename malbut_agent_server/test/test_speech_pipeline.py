"""Tests for the offline SWM25-76 speech conversation boundary."""

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest

from malbut_agent_server.conversation import (
    ConversationConflictError,
    ConversationNotFoundError,
    SQLiteConversationStore,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import (
    AgentOrchestrator,
    OrchestrationCancelledError,
)
from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import (
    MAX_ID_LENGTH,
    AgentDecision,
    ProviderResult,
    RobotState,
    ValidationError,
)
from malbut_agent_server.speech import (
    SPEECH_SCHEMA_VERSION,
    SpeechActivityEvent,
    SpeechConversationCoordinator,
    SpeechControlResult,
    SpeechPipelineResult,
    SpeechTranscriptEvent,
    TTSCancelRequest,
    TTSRequest,
    TrustedSpeechBinding,
)


class CountingMockProvider(MockProvider):
    """Count deterministic provider calls without network access."""

    def __init__(self) -> None:
        """Initialize the provider call counter."""
        super().__init__()
        self.calls = 0

    def complete(self, *args, **kwargs):
        """Count and delegate one deterministic completion."""
        self.calls += 1
        return super().complete(*args, **kwargs)


class BlockingMockProvider(CountingMockProvider):
    """Hold one offline completion until a concurrency test releases it."""

    def __init__(self) -> None:
        """Create deterministic start and release barriers."""
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, *args, **kwargs):
        """Signal entry, wait for the test, and then complete locally."""
        self.started.set()
        if not self.release.wait(timeout=3.0):
            raise RuntimeError('blocking test provider timed out')
        return super().complete(*args, **kwargs)


class BlockingFailureProvider(CountingMockProvider):
    """Hold one offline completion and then raise a provider failure."""

    def __init__(self) -> None:
        """Create deterministic start and release barriers."""
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, *args, **kwargs):
        """Wait for a concurrent control event before failing locally."""
        del args, kwargs
        self.started.set()
        if not self.release.wait(timeout=3.0):
            raise RuntimeError('blocking test provider timed out')
        self.calls += 1
        raise RuntimeError('synthetic delayed provider failure')


class FailOnceMockProvider(CountingMockProvider):
    """Raise once to prove a failed reservation does not remain stuck."""

    def __init__(self) -> None:
        """Initialize the single injected failure."""
        super().__init__()
        self.fail_next = True

    def complete(self, *args, **kwargs):
        """Fail the first call and delegate every later call."""
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError('synthetic provider failure')
        return super().complete(*args, **kwargs)


class BlankMessageProvider(CountingMockProvider):
    """Return an invalid blank decision for commit-order regression tests."""

    def complete(self, *args, **kwargs):
        """Return one locally constructed invalid provider result."""
        del args, kwargs
        self.calls += 1
        return ProviderResult(
            decision=AgentDecision(type='message', message='   '),
            provider='blank-fixture',
            model='fixture',
            latency_ms=0,
        )


class MutableClock:
    """Controllable wall clock for a conversation expiry boundary."""

    def __init__(self, now: float = 1000.0) -> None:
        """Start at one finite timestamp."""
        self.now = now

    def __call__(self) -> float:
        """Return the current synthetic timestamp."""
        return self.now


def _binding(**overrides) -> TrustedSpeechBinding:
    value = {
        'user_id': 'voice-user',
        'speaker_id': 'trusted-speaker',
        'speech_session_id': 'speech-session-1',
        'conversation_id': 'voice-conversation-1',
        'source': 'local-stt',
    }
    value.update(overrides)
    return TrustedSpeechBinding.from_dict(value)


def _transcript(**overrides) -> SpeechTranscriptEvent:
    value = {
        'schema_version': SPEECH_SCHEMA_VERSION,
        'utterance_id': 'utterance-1',
        'speech_session_id': 'speech-session-1',
        'conversation_id': 'voice-conversation-1',
        'speaker_id': 'trusted-speaker',
        'source': 'local-stt',
        'sequence': 1,
        'capture_epoch': 1,
        'source_timestamp_ns': 1000000000,
        'text': '안녕',
        'confidence': 0.98,
        'is_final': True,
        'capture_origin': 'microphone',
        'audio_metadata': {
            'duration_ms': 600,
            'sample_rate_hz': 16000,
            'channel_count': 1,
        },
    }
    value.update(overrides)
    return SpeechTranscriptEvent.from_dict(value)


def _activity(**overrides) -> SpeechActivityEvent:
    value = {
        'schema_version': SPEECH_SCHEMA_VERSION,
        'event_id': 'activity-1',
        'speech_session_id': 'speech-session-1',
        'speaker_id': 'trusted-speaker',
        'source': 'local-stt',
        'capture_epoch': 1,
        'source_timestamp_ns': 2000000000,
    }
    value.update(overrides)
    return SpeechActivityEvent.from_dict(value)


@contextmanager
def _runtime(
    event_cache_size: int = 256,
    provider=None,
    clock=time.time,
):
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(
        ':memory:',
        clock=clock,
    )
    provider = provider or CountingMockProvider()
    orchestrator = AgentOrchestrator(
        provider=provider,
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
        trusted_robot_state=True,
    )
    coordinator = SpeechConversationCoordinator(
        orchestrator,
        event_cache_size=event_cache_size,
    )
    coordinator.open_session(_binding())
    try:
        yield (
            coordinator,
            provider,
            conversation_store,
        )
    finally:
        conversation_store.close()
        memory_store.close()


def _run_transcript_in_thread(coordinator, event):
    """Start one transcript call and retain its result or exception."""
    outcome = {}

    def invoke() -> None:
        try:
            outcome['result'] = coordinator.handle_transcript(event)
        except Exception as error:  # pragma: no cover - assertion aid
            outcome['error'] = error

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    return thread, outcome


@pytest.mark.parametrize(
    ('field_name', 'field_value'),
    [
        ('audio', 'base64-data'),
        ('pcm', [1, 2, 3]),
        ('path', '/tmp/voice.wav'),
        ('uri', 'file:///tmp/voice.wav'),
        ('waveform', [0.1]),
        ('user_id', 'untrusted-user'),
    ],
)
def test_transcript_schema_rejects_audio_and_untrusted_identity_fields(
    field_name: str,
    field_value: object,
) -> None:
    """Raw audio, locations, and client user identities never cross."""
    value = _transcript().to_dict()
    value[field_name] = field_value
    with pytest.raises(ValidationError, match='unknown fields'):
        SpeechTranscriptEvent.from_dict(value)


@pytest.mark.parametrize(
    ('field_name', 'field_value'),
    [
        ('audio', 'bytes'),
        ('path', '/tmp/voice.wav'),
        ('uri', 'memory://voice'),
    ],
)
def test_audio_metadata_rejects_content_and_locations(
    field_name: str,
    field_value: object,
) -> None:
    """Only duration, sample rate, and channel count are accepted."""
    value = _transcript().to_dict()
    value['audio_metadata'][field_name] = field_value
    with pytest.raises(ValidationError, match='unknown fields'):
        SpeechTranscriptEvent.from_dict(value)


@pytest.mark.parametrize(
    ('field_name', 'field_value'),
    [
        ('confidence', float('nan')),
        ('confidence', float('inf')),
        ('confidence', -0.01),
        ('sequence', 0),
        ('capture_epoch', 0),
    ],
)
def test_transcript_rejects_invalid_numeric_metadata(
    field_name: str,
    field_value: object,
) -> None:
    """Non-finite, out-of-range, and non-positive metadata fail closed."""
    with pytest.raises(ValidationError):
        _transcript(**{field_name: field_value})


@pytest.mark.parametrize(
    ('mutation', 'expected_message'),
    [
        (lambda _value: None, 'must be an object'),
        (
            lambda value: {**value, 'utterance_id': 7},
            'must be a string',
        ),
        (
            lambda value: {**value, 'utterance_id': '   '},
            'must not be empty',
        ),
        (
            lambda value: {
                **value,
                'utterance_id': 'x' * (MAX_ID_LENGTH + 1),
            },
            'must be at most',
        ),
        (
            lambda value: {**value, 'utterance_id': 'line\nbreak'},
            'control characters',
        ),
        (
            lambda value: {**value, 'sequence': True},
            'must be an integer',
        ),
        (
            lambda value: {**value, 'confidence': True},
            'must be a number',
        ),
    ],
)
def test_strict_transcript_primitives_reject_ambiguous_values(
    mutation,
    expected_message: str,
) -> None:
    """Primitive coercion, oversized IDs, and non-objects fail closed."""
    value = mutation(_transcript().to_dict())
    with pytest.raises(ValidationError, match=expected_message):
        SpeechTranscriptEvent.from_dict(value)


@pytest.mark.parametrize(
    ('changes', 'expected_message'),
    [
        ({'is_final': 1}, 'is_final must be a boolean'),
        ({'capture_origin': 'bluetooth'}, 'capture_origin is unsupported'),
        ({'audio_metadata': {}}, 'must be an AudioMetadata'),
    ],
)
def test_direct_transcript_construction_enforces_typed_invariants(
    changes: dict,
    expected_message: str,
) -> None:
    """Direct dataclass construction cannot bypass envelope validation."""
    with pytest.raises(ValidationError, match=expected_message):
        replace(_transcript(), **changes)


@pytest.mark.parametrize(
    ('field_name', 'field_value', 'expected_message'),
    [
        ('is_final', 1, 'is_final must be a boolean'),
        ('capture_origin', 'bluetooth', 'capture_origin is unsupported'),
    ],
)
def test_dict_transcript_rejects_closed_set_violations_early(
    field_name: str,
    field_value: object,
    expected_message: str,
) -> None:
    """Envelope booleans and capture-origin enums are strictly typed."""
    value = _transcript().to_dict()
    value[field_name] = field_value
    with pytest.raises(ValidationError, match=expected_message):
        SpeechTranscriptEvent.from_dict(value)


@pytest.mark.parametrize(
    ('field_name', 'field_value', 'expected_message'),
    [
        ('voice', 'custom', 'voice is unsupported'),
        ('style', 'cheerful', 'style is unsupported'),
        ('interruptible', False, 'must be interruptible'),
    ],
)
def test_tts_request_has_a_closed_safe_output_contract(
    field_name: str,
    field_value: object,
    expected_message: str,
) -> None:
    """Adapters cannot select unapproved voices, styles, or blocking TTS."""
    request = TTSRequest(
        schema_version=SPEECH_SCHEMA_VERSION,
        request_id='tts-request',
        speech_session_id='speech-session-1',
        conversation_id='voice-conversation-1',
        turn_id='turn-1',
        source_utterance_id='utterance-1',
        text='안녕하세요',
    )
    with pytest.raises(ValidationError, match=expected_message):
        replace(request, **{field_name: field_value})


def test_tts_cancel_reason_is_a_closed_set() -> None:
    """A downstream cancel cannot carry an unaudited free-form reason."""
    with pytest.raises(ValidationError, match='reason is unsupported'):
        TTSCancelRequest(
            request_id='cancel-request',
            speech_session_id='speech-session-1',
            tts_request_id='tts-request',
            reason='operator_override',
        )


def test_result_serialization_preserves_optional_contracts() -> None:
    """Public result JSON distinguishes absent and present output objects."""
    empty_pipeline = SpeechPipelineResult(
        status='rejected',
        code='fixture',
        capture_epoch=1,
    )
    assert empty_pipeline.to_dict()['agent'] is None
    assert empty_pipeline.to_dict()['tts_request'] is None

    cancel = TTSCancelRequest(
        request_id='cancel-request',
        speech_session_id='speech-session-1',
        tts_request_id='tts-request',
        reason='barge_in',
    )
    empty_control = SpeechControlResult(
        status='ready',
        code='fixture',
        capture_epoch=1,
    )
    control = SpeechControlResult(
        status='ready',
        code='fixture-cancel',
        capture_epoch=2,
        cancel_request=cancel,
    )
    assert empty_control.to_dict()['cancel_request'] is None
    assert control.to_dict()['cancel_request'] == cancel.to_dict()

    with _runtime() as (coordinator, _provider, _store):
        responded = coordinator.handle_transcript(_transcript()).to_dict()
        assert responded['agent'] is not None
        assert responded['tts_request'] is not None


def test_public_coordinator_methods_reject_wrong_types_and_unknown_sessions(
) -> None:
    """Programmer errors raise while unknown session identities stay typed."""
    with pytest.raises(TypeError, match='orchestrator'):
        SpeechConversationCoordinator(object())

    with _runtime() as (coordinator, provider, _store):
        with pytest.raises(TypeError, match='binding'):
            coordinator.open_session({})
        with pytest.raises(TypeError, match='SpeechTranscriptEvent'):
            coordinator.handle_transcript({})
        with pytest.raises(TypeError, match='RobotState'):
            coordinator.handle_transcript(_transcript(), robot_state={})
        with pytest.raises(TypeError, match='SpeechActivityEvent'):
            coordinator.handle_barge_in({})

        unknown_activity = coordinator.handle_barge_in(
            _activity(speech_session_id='unknown-session')
        )
        assert unknown_activity.code == 'unknown_speech_session'
        assert coordinator.mark_tts_terminal(
            'unknown-session',
            'unknown-tts',
        ).code == 'unknown_speech_session'
        assert coordinator.close_session(
            'unknown-session',
            'unknown-close',
        ).code == 'unknown_speech_session'
        assert provider.calls == 0


@pytest.mark.parametrize('value', [True, 0, 4097])
def test_session_state_capacity_configuration_is_strict(value) -> None:
    """Session-state memory cannot be configured as unbounded or ambiguous."""
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=CountingMockProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
    )
    try:
        with pytest.raises(ValidationError, match='max_session_states'):
            SpeechConversationCoordinator(
                orchestrator,
                max_session_states=value,
            )
    finally:
        conversation_store.close()
        memory_store.close()


def test_closed_session_states_are_bounded_without_eviction() -> None:
    """Capacity fails closed while preserving terminal replay semantics."""
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=CountingMockProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
    )
    coordinator = SpeechConversationCoordinator(
        orchestrator,
        max_session_states=2,
    )
    first = _binding()
    second = _binding(
        speech_session_id='speech-session-2',
        conversation_id='voice-conversation-2',
    )
    third = _binding(
        speech_session_id='speech-session-3',
        conversation_id='voice-conversation-3',
    )
    try:
        coordinator.open_session(first)
        closed = coordinator.close_session(first.speech_session_id, 'close-1')
        coordinator.open_session(second)

        with pytest.raises(
            ValidationError,
            match='speech session capacity reached',
        ):
            coordinator.open_session(third)

        replay = coordinator.close_session(first.speech_session_id, 'close-1')
        assert replay == closed
        with pytest.raises(ConversationNotFoundError):
            conversation_store.get(
                third.user_id,
                third.conversation_id,
            )
        assert len(coordinator._sessions) == 2
    finally:
        conversation_store.close()
        memory_store.close()


def test_open_session_is_idempotent_but_cannot_rebind_or_reopen() -> None:
    """A speech-session lease is immutable and terminal once closed."""
    with _runtime() as (coordinator, _provider, _store):
        duplicate = coordinator.open_session(_binding())
        assert duplicate.code == 'session_already_open'

        with pytest.raises(ValidationError, match='bound differently'):
            coordinator.open_session(_binding(speaker_id='other-speaker'))

        coordinator.close_session('speech-session-1', 'close-lease')
        with pytest.raises(ValidationError, match='session is closed'):
            coordinator.open_session(_binding())


def test_activity_binding_and_replay_fingerprint_fail_closed() -> None:
    """VAD identity mismatch is cached but mutated replay is rejected."""
    with _runtime() as (coordinator, provider, _store):
        event = _activity(speaker_id='other-speaker')
        mismatch = coordinator.handle_barge_in(event)
        assert mismatch.code == 'speech_binding_mismatch'

        conflict = coordinator.handle_barge_in(
            _activity(
                speaker_id='other-speaker',
                source_timestamp_ns=event.source_timestamp_ns + 1,
            )
        )
        assert conflict.code == 'activity_conflict'
        assert provider.calls == 0


def test_partial_can_be_replaced_by_final_without_provider_call() -> None:
    """A partial event is ignored and the matching final event is accepted."""
    with _runtime() as (coordinator, provider, _store):
        partial = coordinator.handle_transcript(
            _transcript(is_final=False)
        )
        assert partial.status == 'ignored'
        assert partial.code == 'partial_transcript'
        assert provider.calls == 0

        final = coordinator.handle_transcript(_transcript())
        assert final.status == 'responded'
        assert provider.calls == 1


def test_one_conversation_cannot_have_two_live_speech_sessions() -> None:
    """Closing one voice binding cannot corrupt another shared session."""
    with _runtime() as (coordinator, _provider, _store):
        with pytest.raises(ValidationError, match='active speech session'):
            coordinator.open_session(
                _binding(speech_session_id='speech-session-2')
            )


def test_source_control_characters_are_rejected() -> None:
    """Source labels cannot inject lines into audit output."""
    with pytest.raises(ValidationError, match='control characters'):
        _binding(source='local-stt\nforged')
    with pytest.raises(ValidationError, match='control characters'):
        _transcript(source='local-stt\tforged')


def test_untrusted_transcripts_never_reach_the_provider() -> None:
    """Confidence, binding, echo, and ordering gates precede inference."""
    with _runtime() as (coordinator, provider, _store):
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='low-confidence',
                sequence=2,
                confidence=0.2,
            )
        ).code == 'low_confidence'
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='wrong-speaker',
                sequence=3,
                speaker_id='unknown-speaker',
            )
        ).code == 'speaker_mismatch'
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='wrong-conversation',
                sequence=4,
                conversation_id='other-conversation',
            )
        ).code == 'conversation_mismatch'
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='wrong-source',
                sequence=5,
                source='other-stt',
            )
        ).code == 'source_mismatch'
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='echo',
                sequence=6,
                capture_origin='self_echo',
            )
        ).code == 'self_echo'
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='unknown-origin',
                sequence=7,
                capture_origin='unknown',
            )
        ).code == 'unknown_capture_origin'
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='wrong-session',
                speech_session_id='missing-session',
                sequence=8,
            )
        ).code == 'unknown_speech_session'
        assert provider.calls == 0


def test_accepted_sequence_rejects_an_older_later_epoch_event() -> None:
    """Only an accepted final establishes the transcript high-water mark."""
    with _runtime() as (coordinator, provider, _store):
        accepted = coordinator.handle_transcript(
            _transcript(sequence=5)
        )
        terminal = coordinator.mark_tts_terminal(
            'speech-session-1',
            accepted.tts_request.request_id,
        )
        assert terminal.capture_epoch == 2

        stale = coordinator.handle_transcript(
            _transcript(
                utterance_id='stale',
                sequence=4,
                capture_epoch=2,
            )
        )
        assert stale.code == 'stale_transcript'
        assert provider.calls == 1


def test_active_tts_requires_barge_in_before_new_user_turn() -> None:
    """Playback cannot be mistaken for a second user turn."""
    with _runtime() as (coordinator, provider, _store):
        first = coordinator.handle_transcript(_transcript())
        assert first.status == 'responded'
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='while-speaking',
                sequence=2,
            )
        ).code == 'tts_playback_active'
        assert provider.calls == 1


def test_rejected_high_sequence_does_not_block_next_capture_epoch() -> None:
    """Playback rejection cannot poison ordering for a later user turn."""
    with _runtime() as (coordinator, provider, _store):
        coordinator.handle_transcript(_transcript())
        rejected = coordinator.handle_transcript(
            _transcript(
                utterance_id='playback-echo-high-sequence',
                sequence=999,
            )
        )
        assert rejected.code == 'tts_playback_active'

        barge_in = coordinator.handle_barge_in(_activity())
        assert barge_in.capture_epoch == 2
        accepted = coordinator.handle_transcript(
            _transcript(
                utterance_id='next-real-turn',
                sequence=2,
                capture_epoch=2,
                text='다음 질문',
            )
        )
        assert accepted.status == 'responded'
        assert provider.calls == 2


def test_final_safe_response_becomes_text_only_interruptible_tts() -> None:
    """TTS uses the locally checked refusal, never the raw Tool proposal."""
    with _runtime() as (coordinator, provider, store):
        state = RobotState.from_dict(
            {
                'camera_available': True,
                'privacy_mode': True,
            }
        )
        result = coordinator.handle_transcript(
            _transcript(text='사진 찍어줘'),
            robot_state=state,
            available_tools=('capture_photo',),
        )
        assert provider.calls == 1
        assert result.agent_result is not None
        assert result.agent_result.raw_decision.type == 'tool_call'
        assert result.agent_result.decision.type == 'refusal'
        assert result.agent_result.safety.code == 'privacy_mode'
        assert result.tts_request is not None
        assert (
            result.tts_request.text
            == result.agent_result.decision.message
        )
        assert (
            result.tts_request.text
            != result.agent_result.raw_decision.message
        )
        assert result.tts_request.interruptible is True

        serialized = json.dumps(
            result.tts_request.to_dict(),
            ensure_ascii=False,
        )
        assert 'audio_metadata' not in serialized
        assert 'base64' not in serialized
        snapshot = store.snapshot(
            'voice-user',
            'voice-conversation-1',
        )
        assert snapshot.turns[0].user_content == '사진 찍어줘'
        persisted = json.dumps(
            snapshot.turns[0].response,
            ensure_ascii=False,
        )
        assert 'audio_metadata' not in persisted
        assert 'sample_rate_hz' not in persisted


def test_duplicate_final_is_idempotent_and_mutation_conflicts() -> None:
    """One utterance produces one provider call and stable correlation IDs."""
    with _runtime() as (coordinator, provider, _store):
        event = _transcript()
        first = coordinator.handle_transcript(event)
        duplicate = coordinator.handle_transcript(event)
        assert provider.calls == 1
        assert duplicate is first
        assert duplicate.request_id == first.request_id
        assert duplicate.turn_id == first.turn_id
        assert (
            duplicate.tts_request.request_id
            == first.tts_request.request_id
        )

        conflict = coordinator.handle_transcript(
            _transcript(text='변조된 발화')
        )
        assert conflict.status == 'rejected'
        assert conflict.code == 'utterance_conflict'
        assert provider.calls == 1


def test_barge_in_cancels_once_and_opens_a_new_capture_epoch() -> None:
    """Trusted VAD cancellation precedes the next final transcript."""
    with _runtime() as (coordinator, provider, _store):
        first = coordinator.handle_transcript(_transcript())
        assert first.tts_request is not None

        event = _activity()
        barge_in = coordinator.handle_barge_in(event)
        duplicate = coordinator.handle_barge_in(event)
        assert barge_in.code == 'tts_cancel_requested'
        assert barge_in.capture_epoch == 2
        assert barge_in.cancel_request is not None
        assert (
            barge_in.cancel_request.tts_request_id
            == first.tts_request.request_id
        )
        assert duplicate == barge_in

        stale = coordinator.handle_transcript(
            _transcript(
                utterance_id='old-epoch',
                sequence=2,
                capture_epoch=1,
            )
        )
        assert stale.code == 'stale_capture_epoch'
        assert provider.calls == 1

        next_turn = coordinator.handle_transcript(
            _transcript(
                utterance_id='utterance-2',
                sequence=3,
                capture_epoch=2,
                text='고마워',
            )
        )
        assert next_turn.status == 'responded'
        assert provider.calls == 2


def test_evicted_activity_replay_cannot_cancel_a_later_tts() -> None:
    """Capture epochs retain safety after the small replay cache evicts."""
    with _runtime(event_cache_size=1) as (
        coordinator,
        provider,
        _store,
    ):
        coordinator.handle_transcript(_transcript())
        first_activity = _activity(event_id='activity-old')
        first = coordinator.handle_barge_in(first_activity)
        assert first.capture_epoch == 2

        second_turn = coordinator.handle_transcript(
            _transcript(
                utterance_id='utterance-2',
                sequence=2,
                capture_epoch=2,
                text='두 번째 질문',
            )
        )
        assert second_turn.status == 'responded'
        second = coordinator.handle_barge_in(
            _activity(
                event_id='activity-new',
                capture_epoch=2,
                source_timestamp_ns=3000000000,
            )
        )
        assert second.capture_epoch == 3

        replay = coordinator.handle_barge_in(first_activity)
        assert replay.code == 'stale_activity_epoch'
        assert replay.capture_epoch == 3
        assert replay.cancel_request is None
        assert provider.calls == 2


def test_tts_terminal_fences_late_feedback_and_self_echo() -> None:
    """Only the active TTS can finish and echo never enters inference."""
    with _runtime() as (coordinator, provider, _store):
        first = coordinator.handle_transcript(_transcript())
        request_id = first.tts_request.request_id
        terminal = coordinator.mark_tts_terminal(
            'speech-session-1',
            request_id,
        )
        assert terminal.code == 'tts_terminal'
        assert terminal.capture_epoch == 2
        assert coordinator.mark_tts_terminal(
            'speech-session-1',
            request_id,
        ).code == 'tts_already_terminal'
        assert coordinator.mark_tts_terminal(
            'speech-session-1',
            'unrelated-tts-request',
        ).code == 'stale_tts_result'

        echo = coordinator.handle_transcript(
            _transcript(
                utterance_id='tts-echo',
                sequence=2,
                capture_epoch=2,
                capture_origin='self_echo',
            )
        )
        assert echo.code == 'self_echo'
        assert provider.calls == 1


def test_session_close_cancels_tts_and_rejects_late_transcript() -> None:
    """Closing a voice session is idempotent and terminal for input."""
    with _runtime() as (coordinator, provider, store):
        first = coordinator.handle_transcript(_transcript())
        closed = coordinator.close_session(
            'speech-session-1',
            'close-control-1',
        )
        duplicate = coordinator.close_session(
            'speech-session-1',
            'close-control-1',
        )
        assert closed.status == 'closed'
        assert closed.code == 'session_closed_tts_cancel_requested'
        assert closed.cancel_request is not None
        assert (
            closed.cancel_request.tts_request_id
            == first.tts_request.request_id
        )
        assert duplicate == closed
        assert store.get(
            'voice-user',
            'voice-conversation-1',
        ).status == 'closed'

        conflict = coordinator.close_session(
            'speech-session-1',
            'different-close-control',
        )
        assert conflict.code == 'close_conflict'
        late = coordinator.handle_transcript(
            _transcript(
                utterance_id='late-utterance',
                sequence=2,
                capture_epoch=closed.capture_epoch,
            )
        )
        assert late.code == 'speech_session_closed'
        replay = coordinator.handle_transcript(_transcript())
        assert replay.code == 'speech_session_closed'
        assert replay.tts_request is None
        assert coordinator.handle_barge_in(
            _activity()
        ).code == 'speech_session_closed'
        assert provider.calls == 1


def test_barge_in_does_not_wait_for_provider_and_discards_late_tts() -> None:
    """An epoch fence remains responsive during slow model inference."""
    provider = BlockingMockProvider()
    with _runtime(provider=provider) as (coordinator, _, store):
        thread, outcome = _run_transcript_in_thread(
            coordinator,
            _transcript(),
        )
        assert provider.started.wait(timeout=1.0)

        started = time.monotonic()
        barge_in = coordinator.handle_barge_in(_activity())
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert barge_in.code == 'capture_epoch_advanced'
        assert barge_in.capture_epoch == 2

        provider.release.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert 'error' not in outcome
        result = outcome['result']
        assert result.status == 'discarded'
        assert result.code == 'capture_epoch_changed_during_inference'
        assert result.capture_epoch == 2
        assert result.agent_result is None
        assert result.tts_request is None
        assert provider.calls == 1
        assert store.list_turns(
            'voice-user',
            'voice-conversation-1',
        ) == []


def test_barge_in_discards_a_concurrent_provider_failure() -> None:
    """A superseding VAD fence wins even when the provider then fails."""
    provider = BlockingFailureProvider()
    with _runtime(provider=provider) as (coordinator, _, store):
        thread, outcome = _run_transcript_in_thread(
            coordinator,
            _transcript(),
        )
        assert provider.started.wait(timeout=1.0)

        barge_in = coordinator.handle_barge_in(_activity())
        assert barge_in.code == 'capture_epoch_advanced'
        assert barge_in.capture_epoch == 2

        provider.release.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert 'error' not in outcome
        result = outcome['result']
        assert result.status == 'discarded'
        assert result.code == 'capture_epoch_changed_during_inference'
        assert result.capture_epoch == 2
        assert result.agent_result is None
        assert result.tts_request is None
        assert provider.calls == 1
        assert store.list_turns(
            'voice-user',
            'voice-conversation-1',
        ) == []


def test_close_does_not_wait_for_provider_and_discards_late_tts() -> None:
    """Session close wins a race with an already-running inference."""
    provider = BlockingMockProvider()
    with _runtime(provider=provider) as (coordinator, _, store):
        thread, outcome = _run_transcript_in_thread(
            coordinator,
            _transcript(),
        )
        assert provider.started.wait(timeout=1.0)

        started = time.monotonic()
        closed = coordinator.close_session(
            'speech-session-1',
            'close-during-inference',
        )
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert closed.status == 'closed'
        assert closed.code == 'session_closed'
        assert closed.cancel_request is None

        provider.release.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert 'error' not in outcome
        result = outcome['result']
        assert result.status == 'discarded'
        assert result.code == 'speech_session_closed_during_inference'
        assert result.agent_result is None
        assert result.tts_request is None
        assert store.list_turns(
            'voice-user',
            'voice-conversation-1',
        ) == []
        assert store.get(
            'voice-user',
            'voice-conversation-1',
        ).status == 'closed'


def test_completion_guard_linearizes_commit_before_barge_in() -> None:
    """A control event cannot split the guarded durable commit section."""
    with _runtime() as (coordinator, _provider, store):
        original_complete = store.complete_turn
        commit_entered = threading.Event()
        release_commit = threading.Event()
        barge_started = threading.Event()
        barge_finished = threading.Event()
        barge_outcome = {}

        def blocking_complete(*args, **kwargs):
            commit_entered.set()
            if not release_commit.wait(timeout=3.0):
                raise RuntimeError('blocking conversation commit timed out')
            return original_complete(*args, **kwargs)

        def invoke_barge_in() -> None:
            barge_started.set()
            barge_outcome['result'] = coordinator.handle_barge_in(
                _activity()
            )
            barge_finished.set()

        store.complete_turn = blocking_complete
        transcript_thread, transcript_outcome = (
            _run_transcript_in_thread(coordinator, _transcript())
        )
        assert commit_entered.wait(timeout=1.0)
        barge_thread = threading.Thread(
            target=invoke_barge_in,
            daemon=True,
        )
        barge_thread.start()
        assert barge_started.wait(timeout=1.0)
        assert not barge_finished.wait(timeout=0.05)

        release_commit.set()
        transcript_thread.join(timeout=2.0)
        barge_thread.join(timeout=2.0)
        assert not transcript_thread.is_alive()
        assert not barge_thread.is_alive()
        assert 'error' not in transcript_outcome
        assert len(
            store.list_turns(
                'voice-user',
                'voice-conversation-1',
            )
        ) == 1
        assert barge_outcome['result'].capture_epoch == 2
        transcript_result = transcript_outcome['result']
        assert transcript_result.code == 'final_transcript_processed'
        assert transcript_result.tts_request is not None
        assert barge_outcome['result'].cancel_request is not None
        assert (
            barge_outcome['result'].cancel_request.tts_request_id
            == transcript_result.tts_request.request_id
        )


def test_slow_commit_does_not_block_unrelated_session_barge_in() -> None:
    """One session commit never creates a coordinator-wide convoy."""
    with _runtime() as (coordinator, _provider, store):
        coordinator.open_session(
            _binding(
                user_id='voice-user-2',
                speaker_id='trusted-speaker-2',
                speech_session_id='speech-session-2',
                conversation_id='voice-conversation-2',
                source='local-stt-2',
            )
        )
        original_complete = store.complete_turn
        commit_entered = threading.Event()
        release_commit = threading.Event()

        def blocking_complete(*args, **kwargs):
            commit_entered.set()
            if not release_commit.wait(timeout=3.0):
                raise RuntimeError('blocking conversation commit timed out')
            return original_complete(*args, **kwargs)

        store.complete_turn = blocking_complete
        thread, outcome = _run_transcript_in_thread(
            coordinator,
            _transcript(),
        )
        try:
            assert commit_entered.wait(timeout=1.0)
            started = time.monotonic()
            second_barge = coordinator.handle_barge_in(
                _activity(
                    event_id='activity-session-2',
                    speech_session_id='speech-session-2',
                    speaker_id='trusted-speaker-2',
                    source='local-stt-2',
                )
            )
            elapsed = time.monotonic() - started
            assert elapsed < 0.5
            assert second_barge.code == 'capture_epoch_advanced'
            assert second_barge.capture_epoch == 2
        finally:
            release_commit.set()
            thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert 'error' not in outcome
        assert outcome['result'].status == 'responded'


def test_in_flight_duplicate_and_conflict_do_not_call_provider_twice() -> None:
    """One reservation owns inference and remains idempotent afterward."""
    provider = BlockingMockProvider()
    with _runtime(provider=provider) as (coordinator, _, _store):
        event = _transcript()
        thread, outcome = _run_transcript_in_thread(coordinator, event)
        assert provider.started.wait(timeout=1.0)

        duplicate = coordinator.handle_transcript(event)
        assert duplicate.status == 'processing'
        assert duplicate.code == 'transcript_in_progress'
        assert coordinator.handle_transcript(
            _transcript(text='변조된 동시 발화')
        ).code == 'utterance_conflict'
        busy = coordinator.handle_transcript(
            _transcript(
                utterance_id='utterance-2',
                sequence=2,
                text='두 번째 발화',
            )
        )
        assert busy.status == 'retryable'
        assert busy.code == 'inference_in_progress'
        assert provider.calls == 0

        provider.release.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert 'error' not in outcome
        first = outcome['result']
        assert first.status == 'responded'
        assert coordinator.handle_transcript(event) is first
        assert provider.calls == 1

        coordinator.mark_tts_terminal(
            'speech-session-1',
            first.tts_request.request_id,
        )
        retried = coordinator.handle_transcript(
            _transcript(
                utterance_id='utterance-2',
                sequence=2,
                capture_epoch=2,
                text='두 번째 발화',
            )
        )
        assert retried.status == 'responded'
        assert provider.calls == 2


@pytest.mark.parametrize(
    ('mutation', 'expected_code'),
    [
        ('close', 'conversation_inactive'),
        ('expire', 'conversation_inactive'),
        ('delete', 'conversation_not_found'),
    ],
)
def test_external_conversation_loss_is_a_typed_fail_closed_result(
    mutation: str,
    expected_code: str,
) -> None:
    """External close, expiry, and deletion never escape as exceptions."""
    clock = MutableClock()
    with _runtime(clock=clock) as (coordinator, provider, store):
        if mutation == 'close':
            store.close_session(
                'voice-user',
                'voice-conversation-1',
            )
        elif mutation == 'expire':
            clock.now += 1801.0
        else:
            assert store.delete(
                'voice-user',
                'voice-conversation-1',
            ) is True

        result = coordinator.handle_transcript(_transcript())
        assert result.status == 'rejected'
        assert result.code == expected_code
        assert result.tts_request is None
        assert result.capture_epoch == 2
        assert provider.calls == 0
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='late-after-loss',
                sequence=2,
                capture_epoch=2,
            )
        ).code == 'speech_session_closed'
        local_close = coordinator.close_session(
            'speech-session-1',
            'acknowledge-external-close',
        )
        assert local_close.status == 'closed'
        assert local_close.code == 'session_already_closed_external'
        assert coordinator.close_session(
            'speech-session-1',
            'acknowledge-external-close',
        ) == local_close


def test_external_delete_during_inference_returns_typed_discard() -> None:
    """A deleted pending conversation cannot produce a late TTS request."""
    provider = BlockingMockProvider()
    with _runtime(provider=provider) as (coordinator, _, store):
        thread, outcome = _run_transcript_in_thread(
            coordinator,
            _transcript(),
        )
        assert provider.started.wait(timeout=1.0)
        assert store.delete(
            'voice-user',
            'voice-conversation-1',
        ) is True

        provider.release.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert 'error' not in outcome
        result = outcome['result']
        assert result.status == 'rejected'
        assert result.code == 'conversation_changed_during_inference'
        assert result.tts_request is None
        assert result.capture_epoch == 2


def test_supersession_wins_a_concurrent_conversation_error() -> None:
    """A lifecycle error cannot replace an already-earned epoch discard."""
    with _runtime() as (coordinator, provider, _store):
        error_started = threading.Event()
        release_error = threading.Event()

        def delayed_conversation_error(*_args, **_kwargs):
            error_started.set()
            if not release_error.wait(timeout=3.0):
                raise RuntimeError('conversation error fixture timed out')
            raise ConversationConflictError('synthetic lifecycle race')

        coordinator.orchestrator.handle = delayed_conversation_error
        thread, outcome = _run_transcript_in_thread(
            coordinator,
            _transcript(),
        )
        assert error_started.wait(timeout=1.0)

        barge_in = coordinator.handle_barge_in(_activity())
        assert barge_in.code == 'capture_epoch_advanced'

        release_error.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert 'error' not in outcome
        result = outcome['result']
        assert result.status == 'discarded'
        assert result.code == 'capture_epoch_changed_during_inference'
        assert result.capture_epoch == 2
        assert result.tts_request is None
        assert provider.calls == 0


def test_external_delete_makes_close_a_typed_local_terminal_result() -> None:
    """Voice close remains fail-closed if its conversation disappeared."""
    with _runtime() as (coordinator, _provider, store):
        assert store.delete(
            'voice-user',
            'voice-conversation-1',
        ) is True
        result = coordinator.close_session(
            'speech-session-1',
            'close-after-delete',
        )
        assert result.status == 'closed'
        assert result.code == 'session_closed_conversation_unavailable'
        assert result.cancel_request is None
        assert coordinator.handle_transcript(
            _transcript(capture_epoch=2)
        ).code == 'speech_session_closed'


def test_external_delete_still_returns_an_active_tts_cancel() -> None:
    """Conversation loss cannot suppress a required playback cancel."""
    with _runtime() as (coordinator, _provider, store):
        first = coordinator.handle_transcript(_transcript())
        assert first.tts_request is not None
        assert store.delete(
            'voice-user',
            'voice-conversation-1',
        ) is True

        closed = coordinator.close_session(
            'speech-session-1',
            'close-after-delete-with-tts',
        )
        assert closed.status == 'closed'
        assert (
            closed.code
            == 'session_closed_conversation_unavailable_'
            'tts_cancel_requested'
        )
        assert closed.cancel_request is not None
        assert (
            closed.cancel_request.tts_request_id
            == first.tts_request.request_id
        )


def test_provider_failure_releases_the_in_flight_reservation() -> None:
    """An unexpected provider error cannot permanently wedge the session."""
    provider = FailOnceMockProvider()
    with _runtime(provider=provider) as (coordinator, _, _store):
        event = _transcript()
        with pytest.raises(RuntimeError, match='synthetic provider failure'):
            coordinator.handle_transcript(event)
        result = coordinator.handle_transcript(event)
        assert result.status == 'responded'
        assert provider.calls == 1


def test_cancellation_without_supersession_returns_fallback_result() -> None:
    """A bare orchestrator cancellation releases its speech reservation."""
    with _runtime() as (coordinator, provider, _store):
        original_handle = coordinator.orchestrator.handle

        def cancel_before_commit(*_args, **_kwargs):
            raise OrchestrationCancelledError('synthetic cancellation')

        coordinator.orchestrator.handle = cancel_before_commit
        event = _transcript()
        cancelled = coordinator.handle_transcript(event)
        assert cancelled.status == 'discarded'
        assert cancelled.code == 'inference_cancelled_before_commit'
        assert cancelled.capture_epoch == 1
        assert cancelled.agent_result is None
        assert cancelled.tts_request is None
        assert provider.calls == 0

        coordinator.orchestrator.handle = original_handle
        retried = coordinator.handle_transcript(event)
        assert retried.status == 'responded'
        assert provider.calls == 1


def test_blank_agent_message_fails_before_durable_speech_commit() -> None:
    """TTS text validation cannot fail after an assistant turn is stored."""
    provider = BlankMessageProvider()
    with _runtime(provider=provider) as (coordinator, _, store):
        event = _transcript()
        with pytest.raises(ProviderError, match='invalid metadata'):
            coordinator.handle_transcript(event)
        assert provider.calls == 1
        assert store.list_turns(
            'voice-user',
            'voice-conversation-1',
        ) == []


def test_transient_conversation_conflict_is_typed_and_retryable() -> None:
    """A lifecycle conflict is not cached as a permanent utterance result."""
    with _runtime() as (coordinator, provider, _store):
        original_handle = coordinator.orchestrator.handle
        calls = 0

        def conflict_once(request, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConversationConflictError('synthetic conflict')
            return original_handle(request, **kwargs)

        coordinator.orchestrator.handle = conflict_once
        event = _transcript()
        first = coordinator.handle_transcript(event)
        assert first.status == 'retryable'
        assert first.code == 'conversation_conflict'
        assert first.tts_request is None

        retried = coordinator.handle_transcript(event)
        assert retried.status == 'responded'
        assert calls == 2
        assert provider.calls == 1


def test_audit_projection_excludes_text_speaker_and_audio_content() -> None:
    """The explicit audit view contains metrics but no transcript content."""
    event = _transcript(text='개인적인 대화 내용')
    audit = event.to_audit_dict()
    rendered = json.dumps(audit, ensure_ascii=False)
    assert event.text not in rendered
    assert event.speaker_id not in rendered
    assert audit['text_chars'] == len(event.text)
    assert audit['audio_metadata']['duration_ms'] == 600
    assert 'path' not in rendered
    assert 'uri' not in rendered
