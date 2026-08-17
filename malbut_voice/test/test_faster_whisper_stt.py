"""Tests for the optional, local-only faster-whisper adapter."""

import math
from pathlib import Path
from types import SimpleNamespace

import numpy
import pytest

from conftest import create_model_tree
from malbut_voice.config import ModelBinding
from malbut_voice.errors import (
    ConfigSecurityError,
    ModelSecurityError,
    TranscriptSecurityError,
)
from malbut_voice.faster_whisper_stt import FasterWhisperLocalBackend
from malbut_voice.model_manifest import VerifiedModel
from malbut_voice.provenance import (
    ModelAttestation,
    _ProvenanceAuthority,
)


OFFLINE_ENVIRONMENT = {
    'HF_HUB_DISABLE_TELEMETRY': '1',
    'HF_HUB_OFFLINE': '1',
    'TRANSFORMERS_OFFLINE': '1',
}
MODEL_ATTESTATION = ModelAttestation(
    model_digest='b' * 64,
    model_id='test-small',
    snapshot_revision='1' * 40,
)


def _binding():
    return ModelBinding(
        root=Path('/protected/model'),
        manifest=Path('/protected/manifest.json'),
    )


def _verified():
    return VerifiedModel(
        snapshot_path=Path('/protected/model/snapshots') / ('1' * 40),
        runtime_versions={
            'av': '17.1.0',
            'faster-whisper': '1.2.1',
            'ctranslate2': '4.8.1',
            'numpy': '2.2.6',
            'onnxruntime': '1.23.2',
            'tokenizers': '0.23.1',
        },
        attestation=MODEL_ATTESTATION,
    )


def _capture(authority, pcm):
    return authority.issue_capture(
        pcm,
        boot_id='00000000-0000-0000-0000-000000000001',
        device_binding_digest='a' * 64,
        started_boottime_ns=100,
        ended_boottime_ns=200,
        sample_rate_hz=16000,
        channels=1,
    )


class _FakeModel:
    def __init__(self, segments):
        self.segments = segments
        self.calls = []
        self.waveform = None

    def transcribe(self, waveform, **kwargs):
        self.calls.append(kwargs)
        self.waveform = waveform
        return iter(self.segments), SimpleNamespace(language='ko')


def _segment(text=' 안녕하세요', probability=0.81):
    word = SimpleNamespace(
        start=0.0,
        end=0.5,
        probability=probability,
    )
    return SimpleNamespace(text=text, words=[word])


def test_backend_is_local_only_and_uses_in_memory_float32(monkeypatch):
    """Load only a local path and pass an ndarray, never a file or URI."""
    authority = _ProvenanceAuthority()
    raw_pcm = bytearray(b'\x00\x80\x00\x00\xff\x7f')
    model = _FakeModel([_segment()])
    construction = []
    verification_calls = []

    def verifier(binding, version_lookup=None):
        verification_calls.append((binding, version_lookup))
        return _verified()

    def factory(path, **kwargs):
        construction.append((path, kwargs))
        return model

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError('network access attempted')

    monkeypatch.setattr('socket.socket', network_forbidden)
    backend = FasterWhisperLocalBackend(
        _binding(),
        authority,
        model_factory=factory,
        numpy_loader=lambda: numpy,
        model_verifier=verifier,
        version_lookup=lambda _package: 'pinned',
        environment=OFFLINE_ENVIRONMENT,
    )

    transcript = backend.transcribe(
        _capture(authority, raw_pcm),
        expected_attestation=MODEL_ATTESTATION,
    )
    text, confidence, _capture_receipt, _receipt = (
        authority.consume_transcript(
            transcript,
            MODEL_ATTESTATION.model_digest,
        )
    )

    assert construction == [
        (
            '/protected/model/snapshots/' + ('1' * 40),
            {
                'device': 'cpu',
                'compute_type': 'int8',
                'local_files_only': True,
            },
        )
    ]
    assert len(verification_calls) == 2
    assert isinstance(model.waveform, numpy.ndarray)
    assert model.waveform.dtype == numpy.float32
    assert numpy.count_nonzero(model.waveform) == 0
    assert raw_pcm == bytearray(len(raw_pcm))
    assert text == '안녕하세요'
    assert confidence == pytest.approx(0.81, abs=1e-6)
    assert model.calls == [
        {
            'language': 'ko',
            'task': 'transcribe',
            'beam_size': 5,
            'temperature': 0.0,
            'vad_filter': True,
            'vad_parameters': {'min_silence_duration_ms': 500},
            'condition_on_previous_text': False,
            'word_timestamps': True,
        }
    ]


def test_confidence_is_duration_weighted_geometric_mean():
    """Compute a deterministic final confidence from word probabilities."""
    first = SimpleNamespace(start=0.0, end=0.5, probability=0.81)
    second = SimpleNamespace(start=0.5, end=1.5, probability=0.64)
    segment = SimpleNamespace(text=' 확인', words=[first, second])

    text, confidence = FasterWhisperLocalBackend._materialize_segments(
        [segment]
    )

    expected = math.exp((0.5 * math.log(0.81) + math.log(0.64)) / 1.5)
    assert text == '확인'
    assert confidence == pytest.approx(expected)


def test_backend_reverifies_model_before_each_captured_inference():
    """Detect manifest drift between preflight and PCM consumption."""
    authority = _ProvenanceAuthority()
    model = _FakeModel([_segment()])
    attestations = [_verified(), _verified(), _verified()]

    def verifier(_binding, version_lookup=None):
        return attestations.pop(0)

    backend = FasterWhisperLocalBackend(
        _binding(),
        authority,
        model_factory=lambda _path, **_kwargs: model,
        numpy_loader=lambda: numpy,
        model_verifier=verifier,
        environment=OFFLINE_ENVIRONMENT,
    )
    assert backend.prepare() == MODEL_ATTESTATION
    raw_pcm = bytearray(b'\0\0')
    backend.transcribe(
        _capture(authority, raw_pcm),
        expected_attestation=MODEL_ATTESTATION,
    )

    assert attestations == []
    assert raw_pcm == bytearray(len(raw_pcm))


def test_backend_rejects_model_changed_during_first_load(
    tmp_path,
    expected_versions,
):
    """Rehash after construction before publishing the loaded model."""
    root, snapshot, manifest_path, _manifest = create_model_tree(tmp_path)

    def mutate_during_load(_path, **_kwargs):
        (snapshot / 'model.bin').write_bytes(b'tampered-after-verify')
        (snapshot / 'model.bin').chmod(0o600)
        return _FakeModel([_segment()])

    backend = FasterWhisperLocalBackend(
        ModelBinding(root=root, manifest=manifest_path),
        _ProvenanceAuthority(),
        model_factory=mutate_during_load,
        version_lookup=expected_versions.__getitem__,
        environment=OFFLINE_ENVIRONMENT,
    )

    with pytest.raises(ModelSecurityError, match='hash_mismatch'):
        backend.prepare()

    assert backend._model is None


def test_backend_requires_supervisor_offline_environment_before_verifier():
    """Fail early unless offline and telemetry flags are fixed."""
    calls = []
    backend = FasterWhisperLocalBackend(
        _binding(),
        _ProvenanceAuthority(),
        model_factory=lambda *_args, **_kwargs: calls.append('model'),
        model_verifier=lambda *_args, **_kwargs: calls.append('verify'),
        environment={},
    )

    with pytest.raises(
        ModelSecurityError,
        match='offline_environment_required',
    ):
        backend.prepare()

    assert calls == []


def test_backend_zeroizes_pcm_when_model_preparation_fails():
    """Clear captured bytes even when a pinned model cannot be loaded."""
    authority = _ProvenanceAuthority()
    raw_pcm = bytearray(b'\x01\x00\x02\x00')

    def fail_verifier(_binding, version_lookup=None):
        raise ModelSecurityError('model_runtime_unavailable')

    backend = FasterWhisperLocalBackend(
        _binding(),
        authority,
        model_verifier=fail_verifier,
        environment=OFFLINE_ENVIRONMENT,
    )

    with pytest.raises(ModelSecurityError, match='runtime_unavailable'):
        backend.transcribe(_capture(authority, raw_pcm))

    assert raw_pcm == bytearray(len(raw_pcm))


def test_backend_rejects_missing_word_evidence_and_zeroizes():
    """Do not call a text-only segment a confidence-backed final transcript."""
    authority = _ProvenanceAuthority()
    raw_pcm = bytearray(b'\x01\x00\x02\x00')
    model = _FakeModel([SimpleNamespace(text=' 텍스트', words=None)])
    backend = FasterWhisperLocalBackend(
        _binding(),
        authority,
        model_factory=lambda _path, **_kwargs: model,
        numpy_loader=lambda: numpy,
        model_verifier=lambda *_args, **_kwargs: _verified(),
        environment=OFFLINE_ENVIRONMENT,
    )

    with pytest.raises(TranscriptSecurityError, match='word_evidence_missing'):
        backend.transcribe(_capture(authority, raw_pcm))

    assert raw_pcm == bytearray(len(raw_pcm))


def test_unexpected_model_error_is_chain_free_and_content_free():
    """Do not expose model paths or runtime exception chains."""
    backend = FasterWhisperLocalBackend(
        _binding(),
        _ProvenanceAuthority(),
        model_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('/private/model/path')
        ),
        model_verifier=lambda *_args, **_kwargs: _verified(),
        environment=OFFLINE_ENVIRONMENT,
    )

    with pytest.raises(ModelSecurityError) as raised:
        backend.prepare()

    assert str(raised.value) == 'local_model_load_failed'
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__traceback__ is None


def test_backend_rechecks_model_binding_object_before_verification():
    """Reject object-level model path mutation before manifest access."""
    binding = _binding()
    calls = []
    backend = FasterWhisperLocalBackend(
        binding,
        _ProvenanceAuthority(),
        model_verifier=lambda *_args, **_kwargs: calls.append(True),
        environment=OFFLINE_ENVIRONMENT,
    )
    object.__setattr__(binding, 'root', Path('relative-attacker-root'))

    with pytest.raises(ConfigSecurityError, match='invalid_path'):
        backend.prepare()

    assert calls == []
