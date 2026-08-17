"""Tests for preflight ordering and final provenance sealing."""

import threading
from types import SimpleNamespace

import pytest

from conftest import voice_config_dict
from malbut_voice.config import VoiceConfig, load_protected_config
from malbut_voice.errors import ModelSecurityError, TranscriptSecurityError
from malbut_voice.provenance import (
    DeviceAttestation,
    ModelAttestation,
    zeroize,
)
from malbut_voice.transcript_source import (
    MicrophoneTranscriptSource,
    VerifiedMicrophoneFinal,
)


DEVICE_ATTESTATION = DeviceAttestation(
    binding_digest='a' * 64,
    binary_device=1,
    binary_inode=2,
    rdev_major=116,
    rdev_minor=9,
)
MODEL_ATTESTATION = ModelAttestation(
    model_digest='b' * 64,
    model_id='test-small',
    snapshot_revision='1' * 40,
)


class _FakeEvent:
    def __init__(self, **fields):
        metadata = fields.pop('audio_metadata')
        self.__dict__.update(fields)
        self.audio_metadata = SimpleNamespace(**metadata)


def _binding_factory(config):
    return SimpleNamespace(
        user_id=config.user_id,
        speaker_id=config.speaker_id,
        speech_session_id=config.speech_session_id,
        conversation_id=config.conversation_id,
        source='local-hardware-faster-whisper-v1',
    )


def _build_source(
    config,
    calls,
    *,
    gate=None,
    model_failure=False,
    real_speech_contract=False,
):
    class FakeCapture:
        def __init__(self, _binding, policy, authority, **_options):
            self.policy = policy
            self.authority = authority

        def prepare(self):
            calls.append('device_prepare')
            return DEVICE_ATTESTATION

        def capture(self, duration, expected_attestation=None):
            calls.append('capture')
            assert expected_attestation == DEVICE_ATTESTATION
            if gate is not None:
                gate[0].set()
                assert gate[1].wait(timeout=2.0)
            pcm = bytearray(self.policy.expected_bytes(duration))
            return self.authority.issue_capture(
                pcm,
                boot_id='00000000-0000-0000-0000-000000000001',
                device_binding_digest=DEVICE_ATTESTATION.binding_digest,
                started_boottime_ns=1000000000,
                ended_boottime_ns=2000000000,
                sample_rate_hz=self.policy.sample_rate_hz,
                channels=self.policy.channels,
            )

    class FakeBackend:
        def __init__(self, _binding, authority, **_options):
            self.authority = authority

        def prepare(self):
            calls.append('model_prepare')
            if model_failure:
                raise ModelSecurityError('model_runtime_unavailable')
            return MODEL_ATTESTATION

        def transcribe(self, capture, expected_attestation=None):
            calls.append('transcribe')
            assert expected_attestation == MODEL_ATTESTATION
            pcm, receipt = self.authority.consume_capture(capture)
            try:
                return self.authority.issue_transcript(
                    receipt,
                    model_digest=MODEL_ATTESTATION.model_digest,
                    text='안녕하세요',
                    confidence=0.9,
                )
            finally:
                zeroize(pcm)

    def binding_with_call(value):
        calls.append('binding_prepare')
        return _binding_factory(value)

    dependencies = {
        'capture_factory': FakeCapture,
        'backend_factory': FakeBackend,
    }
    if not real_speech_contract:
        dependencies.update(
            {
                'binding_factory': binding_with_call,
                'event_factory': _FakeEvent,
            }
        )
    return MicrophoneTranscriptSource._for_test(config, **dependencies)


def test_model_and_device_preflight_finish_before_capture(config_file):
    """Never record audio before the local model is verified and loaded."""
    calls = []
    source = _build_source(load_protected_config(config_file), calls)

    result = source.capture_final()

    assert calls[:4] == [
        'device_prepare',
        'binding_prepare',
        'model_prepare',
        'capture',
    ]
    assert calls[4:] == ['transcribe']
    assert source.verify_final(result)


def test_model_preflight_failure_never_reaches_capture(config_file):
    """Fail before recording if model hash, runtime, or load is unavailable."""
    calls = []
    source = _build_source(
        load_protected_config(config_file),
        calls,
        model_failure=True,
    )

    with pytest.raises(ModelSecurityError, match='runtime_unavailable'):
        source.capture_final()

    assert calls == ['device_prepare', 'binding_prepare', 'model_prepare']


def test_final_is_fixed_microphone_event_without_identity_or_authority_claim(
    config_file,
):
    """Derive final event claims internally and keep the audit content-free."""
    source = _build_source(load_protected_config(config_file), [])
    result = source.capture_final()

    assert result.event.source == 'local-hardware-faster-whisper-v1'
    assert result.event.capture_origin == 'microphone'
    assert result.event.is_final is True
    assert result.event.audio_metadata.duration_ms == 1000
    assert result.audit.physical_audio_capture is True
    assert result.audit.microphone_provenance_verified is True
    assert result.audit.speaker_identity_verified is False
    assert result.audit.execution_authority is False
    public_audit = result.audit.to_dict()
    assert 'text' not in public_audit
    assert 'speaker_id' not in public_audit
    assert 'model_digest' not in public_audit
    assert 'device_binding_digest' not in public_audit


def test_final_normal_and_object_level_mutation_are_detected(config_file):
    """Seal current event and audit values, not saved metadata."""
    source = _build_source(load_protected_config(config_file), [])
    result = source.capture_final()

    with pytest.raises(AttributeError, match='immutable'):
        result.event = object()
    object.__setattr__(result.event, 'text', 'forged')
    assert source.verify_final(result) is False

    audit_result = source.capture_final()
    object.__setattr__(audit_result.audit, 'text_chars', 999)
    assert source.verify_final(audit_result) is False


def test_final_is_bound_to_source_and_consumed_once(config_file):
    """Reject a foreign issuer and manipulated wrapper replay."""
    config = load_protected_config(config_file)
    source = _build_source(config, [])
    foreign = _build_source(config, [])
    result = source.capture_final()

    assert foreign.verify_final(result) is False
    assert source.consume_final(result) is result.event
    assert source.verify_final(result) is False
    with pytest.raises(TranscriptSecurityError, match='rejected'):
        source.consume_final(result)


def test_direct_final_constructor_is_private():
    """Reject manufacture of a verified final without an issuer proof."""
    with pytest.raises(TypeError, match='private'):
        VerifiedMicrophoneFinal(
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )


def test_source_rejects_simultaneous_capture(config_file):
    """Allow at most one in-flight capture per source instance."""
    entered = threading.Event()
    release = threading.Event()
    source = _build_source(
        load_protected_config(config_file),
        [],
        gate=(entered, release),
    )
    outcomes = []

    def run_capture():
        try:
            outcomes.append(source.capture_final())
        except Exception as error:
            outcomes.append(error)

    worker = threading.Thread(target=run_capture)
    worker.start()
    assert entered.wait(timeout=2.0)
    with pytest.raises(TranscriptSecurityError, match='source_busy'):
        source.capture_final()
    release.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], VerifiedMicrophoneFinal)


def test_default_factory_builds_current_speech_transcript_event(config_file):
    """Bind private proof to the agent server's typed final event contract."""
    from malbut_agent_server.speech import SpeechTranscriptEvent

    source = _build_source(
        load_protected_config(config_file),
        [],
        real_speech_contract=True,
    )

    result = source.capture_final()

    assert isinstance(result.event, SpeechTranscriptEvent)
    assert source.verify_final(result)
    assert source.verify_final(result.event) is False


def test_source_rejects_config_not_issued_by_protected_loader(tmp_path):
    """Do not turn an ordinary caller dictionary into provenance authority."""
    config = VoiceConfig.from_dict(voice_config_dict(tmp_path))

    with pytest.raises(TypeError, match='protected loader'):
        MicrophoneTranscriptSource(config)


def test_source_rechecks_object_level_config_mutation_before_preflight(
    config_file,
):
    """Reject nested mutation before device, model, or subprocess use."""
    config = load_protected_config(config_file)
    calls = []
    source = _build_source(config, calls)
    object.__setattr__(config.capture, 'sample_rate_hz', 8000)

    with pytest.raises(TranscriptSecurityError, match='config_integrity'):
        source.capture_final()

    assert calls == []
