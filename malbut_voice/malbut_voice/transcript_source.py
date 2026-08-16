"""One-shot source that binds private microphone proof to a final event."""

import hashlib
import threading

from malbut_voice.audio_capture import BoundedArecordCapture
from malbut_voice.config import FIXED_SOURCE, VoiceConfig
from malbut_voice.errors import (
    TranscriptSecurityError,
    chain_free_boundary,
)
from malbut_voice.faster_whisper_stt import FasterWhisperLocalBackend
from malbut_voice.provenance import (
    PublicTranscriptAudit,
    _ProvenanceAuthority,
)


_FINAL_CONSTRUCTOR = object()
_SOURCE_TEST_CONSTRUCTOR = object()


def _default_binding_factory(config_binding):
    from malbut_agent_server.speech import TrustedSpeechBinding

    return TrustedSpeechBinding(
        user_id=config_binding.user_id,
        speaker_id=config_binding.speaker_id,
        speech_session_id=config_binding.speech_session_id,
        conversation_id=config_binding.conversation_id,
        source=FIXED_SOURCE,
    )


def _default_event_factory(**fields):
    from malbut_agent_server.speech import AudioMetadata, SpeechTranscriptEvent

    metadata = fields.pop('audio_metadata')
    return SpeechTranscriptEvent(
        **fields,
        audio_metadata=AudioMetadata(**metadata),
    )


def _event_proof_value(
    event_fields,
    capture_mac,
    transcript_mac,
    model_digest,
):
    return {
        'event': event_fields,
        'capture_mac': capture_mac,
        'transcript_mac': transcript_mac,
        'model_digest': model_digest,
    }


def _event_fields(event):
    metadata = event.audio_metadata
    return {
        'schema_version': event.schema_version,
        'utterance_id': event.utterance_id,
        'speech_session_id': event.speech_session_id,
        'conversation_id': event.conversation_id,
        'speaker_id': event.speaker_id,
        'source': event.source,
        'sequence': event.sequence,
        'capture_epoch': event.capture_epoch,
        'source_timestamp_ns': event.source_timestamp_ns,
        'text': event.text,
        'confidence': event.confidence,
        'is_final': event.is_final,
        'capture_origin': event.capture_origin,
        'audio_metadata': {
            'duration_ms': metadata.duration_ms,
            'sample_rate_hz': metadata.sample_rate_hz,
            'channel_count': metadata.channel_count,
        },
    }


class VerifiedMicrophoneFinal:
    """Opaque wrapper whose proof is valid only for its issuing source."""

    __slots__ = (
        '_audit', '_authority', '_capture_mac', '_event', '_model_digest',
        '_proof', '_transcript_mac',
    )

    def __init__(
        self,
        seal,
        authority,
        event,
        audit,
        capture_mac,
        transcript_mac,
        model_digest,
        proof,
    ):
        """Reject direct construction outside the issuing source."""
        if seal is not _FINAL_CONSTRUCTOR:
            raise TypeError('private verified microphone final capability')
        object.__setattr__(self, '_authority', authority)
        object.__setattr__(self, '_event', event)
        object.__setattr__(self, '_audit', audit)
        object.__setattr__(self, '_capture_mac', capture_mac)
        object.__setattr__(self, '_transcript_mac', transcript_mac)
        object.__setattr__(self, '_model_digest', model_digest)
        object.__setattr__(self, '_proof', proof)

    def __setattr__(self, name, value):
        """Reject normal mutation of the sealed wrapper."""
        raise AttributeError('verified microphone final is immutable')

    @property
    def event(self):
        """Return the final event; the bare event alone is not proof."""
        return self._event

    @property
    def audit(self):
        """Return the content-free public audit projection."""
        return self._audit


class MicrophoneTranscriptSource:
    """Own one unforgeable capture-to-final provenance authority."""

    def __init__(
        self,
        config,
        *,
        capture_factory=None,
        backend_factory=None,
        binding_factory=None,
        event_factory=None,
        capture_options=None,
        backend_options=None,
        _test_seal=None,
    ):
        """Construct an idle source; no hardware or model is opened here."""
        if not isinstance(config, VoiceConfig):
            raise TypeError('config must be VoiceConfig')
        if not config.is_protected:
            raise TypeError('config must come from protected loader')
        test_values = (
            capture_factory,
            backend_factory,
            binding_factory,
            event_factory,
            capture_options,
            backend_options,
        )
        if any(value is not None for value in test_values):
            if _test_seal is not _SOURCE_TEST_CONSTRUCTOR:
                raise TypeError('dependency injection is private to tests')
        self._config = config
        self._authority = _ProvenanceAuthority()
        self._operation_lock = threading.Lock()
        self._final_lock = threading.Lock()
        self._consumed_proofs = set()
        capture_type = (
            BoundedArecordCapture
            if capture_factory is None
            else capture_factory
        )
        backend_type = (
            FasterWhisperLocalBackend
            if backend_factory is None
            else backend_factory
        )
        self._capture = capture_type(
            config.audio,
            config.capture,
            self._authority,
            **({} if capture_options is None else capture_options),
        )
        self._backend = backend_type(
            config.model,
            self._authority,
            **({} if backend_options is None else backend_options),
        )
        self._binding_factory = (
            _default_binding_factory
            if binding_factory is None
            else binding_factory
        )
        self._event_factory = (
            _default_event_factory if event_factory is None else event_factory
        )
        self._trusted_binding = None
        self._device_attestation = None
        self._model_attestation = None

    @classmethod
    def _for_test(cls, config, **dependencies):
        """Construct with deterministic fakes for package tests only."""
        return cls(
            config,
            _test_seal=_SOURCE_TEST_CONSTRUCTOR,
            **dependencies,
        )

    def _prepare_locked(self):
        if not self._config.is_protected:
            raise TranscriptSecurityError('config_integrity_rejected')
        device = self._capture.prepare()
        try:
            binding = self._binding_factory(self._config.binding)
        except Exception:
            raise TranscriptSecurityError('speech_binding_invalid')
        model = self._backend.prepare()
        self._trusted_binding = binding
        self._device_attestation = device
        self._model_attestation = model
        return {
            'device_attested': True,
            'model_attested': True,
            'microphone_opened': False,
            'speaker_identity_verified': False,
            'execution_authority': False,
        }

    @chain_free_boundary
    def prepare(self):
        """Check device, binding, runtime, and model without microphone I/O."""
        if not self._operation_lock.acquire(blocking=False):
            raise TranscriptSecurityError('source_busy')
        try:
            return self._prepare_locked()
        finally:
            self._operation_lock.release()

    @chain_free_boundary
    def capture_final(self, duration_seconds=None):
        """Explicitly capture, transcribe, and issue one final capability."""
        if not self._operation_lock.acquire(blocking=False):
            raise TranscriptSecurityError('source_busy')
        try:
            duration = (
                self._config.capture.default_duration_seconds
                if duration_seconds is None
                else duration_seconds
            )
            self._prepare_locked()
            capture = self._capture.capture(
                duration,
                expected_attestation=self._device_attestation,
            )
            transcript = self._backend.transcribe(
                capture,
                expected_attestation=self._model_attestation,
            )
            text, confidence, capture_receipt, transcript_receipt = (
                self._authority.consume_transcript(
                    transcript,
                    self._model_attestation.model_digest,
                )
            )
            duration_ms = (
                capture_receipt.frame_count * 1000
                // capture_receipt.sample_rate_hz
            )
            utterance_seed = (
                'malbut-microphone-utterance-v1\0'
                f'{self._authority.instance_id}\0{capture_receipt.sequence}'
            ).encode('ascii')
            utterance_id = (
                'mic-' + hashlib.sha256(utterance_seed).hexdigest()[:40]
            )
            event_fields = {
                'schema_version': 1,
                'utterance_id': utterance_id,
                'speech_session_id': self._trusted_binding.speech_session_id,
                'conversation_id': self._trusted_binding.conversation_id,
                'speaker_id': self._trusted_binding.speaker_id,
                'source': FIXED_SOURCE,
                'sequence': capture_receipt.sequence,
                'capture_epoch': self._config.binding.capture_epoch,
                'source_timestamp_ns': capture_receipt.ended_boottime_ns,
                'text': text,
                'confidence': confidence,
                'is_final': True,
                'capture_origin': 'microphone',
                'audio_metadata': {
                    'duration_ms': duration_ms,
                    'sample_rate_hz': capture_receipt.sample_rate_hz,
                    'channel_count': capture_receipt.channels,
                },
            }
            try:
                event = self._event_factory(**dict(event_fields))
            except Exception:
                raise TranscriptSecurityError('speech_event_invalid')
            audit = PublicTranscriptAudit(
                schema_version=1,
                utterance_id=utterance_id,
                sequence=capture_receipt.sequence,
                source=FIXED_SOURCE,
                capture_origin='microphone',
                duration_ms=duration_ms,
                sample_rate_hz=capture_receipt.sample_rate_hz,
                channel_count=capture_receipt.channels,
                confidence=confidence,
                text_chars=len(text),
                physical_audio_capture=True,
                microphone_provenance_verified=True,
                speaker_identity_verified=False,
                execution_authority=False,
            )
            proof_value = _event_proof_value(
                event_fields,
                capture_receipt.mac,
                transcript_receipt.mac,
                self._model_attestation.model_digest,
            )
            proof_value['audit'] = audit.to_dict()
            proof = self._authority.sign_final(proof_value)
            return VerifiedMicrophoneFinal(
                _FINAL_CONSTRUCTOR,
                self._authority,
                event,
                audit,
                capture_receipt.mac,
                transcript_receipt.mac,
                self._model_attestation.model_digest,
                proof,
            )
        finally:
            self._operation_lock.release()

    def _verify_final_unlocked(self, result):
        if (
            type(result) is not VerifiedMicrophoneFinal
            or result._authority is not self._authority
            or result._proof in self._consumed_proofs
            or type(result._audit) is not PublicTranscriptAudit
        ):
            return False
        try:
            proof_value = _event_proof_value(
                _event_fields(result._event),
                result._capture_mac,
                result._transcript_mac,
                result._model_digest,
            )
            proof_value['audit'] = result._audit.to_dict()
            return self._authority.verify_final(
                proof_value,
                result._proof,
            )
        except Exception:
            return False

    def verify_final(self, result):
        """Return whether an unused final came from this exact source."""
        with self._final_lock:
            return self._verify_final_unlocked(result)

    @chain_free_boundary
    def consume_final(self, result):
        """Consume one verified final exactly once for a future local sink."""
        with self._final_lock:
            if not self._verify_final_unlocked(result):
                raise TranscriptSecurityError('final_capability_rejected')
            self._consumed_proofs.add(result._proof)
            return result._event
