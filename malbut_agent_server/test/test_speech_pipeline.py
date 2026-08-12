"""Tests for the offline SWM25-76 speech conversation boundary."""

import json
from contextlib import contextmanager

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import RobotState, ValidationError
from malbut_agent_server.speech import (
    SPEECH_SCHEMA_VERSION,
    SpeechActivityEvent,
    SpeechConversationCoordinator,
    SpeechTranscriptEvent,
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
def _runtime(event_cache_size: int = 256):
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    provider = CountingMockProvider()
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
                utterance_id='echo',
                sequence=4,
                capture_origin='self_echo',
            )
        ).code == 'self_echo'
        assert coordinator.handle_transcript(
            _transcript(
                utterance_id='wrong-session',
                speech_session_id='missing-session',
                sequence=5,
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
