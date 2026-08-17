"""Pinned local-only faster-whisper adapter for in-memory PCM."""

import math
import os

from malbut_voice.config import ModelBinding, _model_binding_fingerprint
from malbut_voice.errors import (
    ModelSecurityError,
    TranscriptSecurityError,
    chain_free_boundary,
)
from malbut_voice.model_manifest import verify_model_binding
from malbut_voice.provenance import _ProvenanceAuthority, zeroize


MAX_TRANSCRIPT_CHARACTERS = 2000
MAX_SEGMENTS = 128
MAX_WORDS = 512
MIN_WORD_WEIGHT_SECONDS = 0.01
MIN_LOG_PROBABILITY = 1e-9
REQUIRED_OFFLINE_ENVIRONMENT = {
    'HF_HUB_DISABLE_TELEMETRY': '1',
    'HF_HUB_OFFLINE': '1',
    'TRANSFORMERS_OFFLINE': '1',
}


def _default_model_factory(snapshot_path, **kwargs):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ModelSecurityError('faster_whisper_unavailable')
    return WhisperModel(snapshot_path, **kwargs)


def _default_numpy():
    try:
        import numpy
    except ImportError:
        raise ModelSecurityError('numpy_unavailable')
    return numpy


def _finite_number(value, code):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptSecurityError(code)
    result = float(value)
    if not math.isfinite(result):
        raise TranscriptSecurityError(code)
    return result


class FasterWhisperLocalBackend:
    """Issue a model-bound transcript capability from private PCM."""

    def __init__(
        self,
        binding,
        authority,
        *,
        model_factory=None,
        numpy_loader=None,
        model_verifier=None,
        version_lookup=None,
        environment=None,
    ):
        """Configure lazy local dependencies without loading a model."""
        if not isinstance(binding, ModelBinding):
            raise TypeError('binding must be ModelBinding')
        if not isinstance(authority, _ProvenanceAuthority):
            raise TypeError('authority must be private provenance authority')
        self._binding = binding
        self._binding_fingerprint = _model_binding_fingerprint(binding)
        self._authority = authority
        self._model_factory = (
            _default_model_factory if model_factory is None else model_factory
        )
        self._numpy_loader = (
            _default_numpy if numpy_loader is None else numpy_loader
        )
        self._model_verifier = (
            verify_model_binding if model_verifier is None else model_verifier
        )
        self._version_lookup = version_lookup
        self._environment = os.environ if environment is None else environment
        self._verified_model = None
        self._model = None

    @chain_free_boundary
    def prepare(self):
        """Verify and load a pinned local snapshot with network lookup off."""
        if (
            _model_binding_fingerprint(self._binding)
            != self._binding_fingerprint
        ):
            raise ModelSecurityError('model_binding_changed')
        if any(
            self._environment.get(name) != expected
            for name, expected in REQUIRED_OFFLINE_ENVIRONMENT.items()
        ):
            raise ModelSecurityError('offline_environment_required')
        verified = self._model_verifier(
            self._binding,
            version_lookup=self._version_lookup,
        )
        if self._verified_model is not None:
            if verified.attestation != self._verified_model.attestation:
                raise ModelSecurityError('model_attestation_changed')
            return verified.attestation
        try:
            model = self._model_factory(
                str(verified.snapshot_path),
                device='cpu',
                compute_type='int8',
                local_files_only=True,
            )
        except ModelSecurityError:
            raise
        except Exception:
            raise ModelSecurityError('local_model_load_failed')
        verified_after_load = self._model_verifier(
            self._binding,
            version_lookup=self._version_lookup,
        )
        if (
            verified_after_load.attestation != verified.attestation
            or verified_after_load.snapshot_path != verified.snapshot_path
        ):
            del model
            raise ModelSecurityError('model_attestation_changed')
        self._verified_model = verified_after_load
        self._model = model
        return verified_after_load.attestation

    @staticmethod
    def _materialize_segments(segments, maximum_duration_seconds=None):
        text_parts = []
        weighted_log_probability = 0.0
        total_weight = 0.0
        segment_count = 0
        word_count = 0
        try:
            iterator = iter(segments)
        except TypeError:
            raise TranscriptSecurityError('stt_segments_invalid')
        for segment in iterator:
            segment_count += 1
            if segment_count > MAX_SEGMENTS:
                raise TranscriptSecurityError('stt_segments_excessive')
            text = getattr(segment, 'text', None)
            if not isinstance(text, str):
                raise TranscriptSecurityError('stt_segment_text_invalid')
            text_parts.append(text)
            text_characters = sum(len(part) for part in text_parts)
            if text_characters > MAX_TRANSCRIPT_CHARACTERS:
                raise TranscriptSecurityError('stt_transcript_excessive')
            words = getattr(segment, 'words', None)
            if not isinstance(words, (list, tuple)) or not words:
                raise TranscriptSecurityError('stt_word_evidence_missing')
            for word in words:
                probability = _finite_number(
                    getattr(word, 'probability', None),
                    'stt_word_probability_invalid',
                )
                start = _finite_number(
                    getattr(word, 'start', None),
                    'stt_word_timestamp_invalid',
                )
                end = _finite_number(
                    getattr(word, 'end', None),
                    'stt_word_timestamp_invalid',
                )
                if probability < 0.0 or probability > 1.0 or end < start:
                    raise TranscriptSecurityError('stt_word_evidence_invalid')
                if (
                    start < 0.0
                    or (
                        maximum_duration_seconds is not None
                        and end > maximum_duration_seconds + 0.5
                    )
                ):
                    raise TranscriptSecurityError(
                        'stt_word_timestamp_out_of_bounds'
                    )
                weight = max(end - start, MIN_WORD_WEIGHT_SECONDS)
                weighted_log_probability += weight * math.log(
                    max(probability, MIN_LOG_PROBABILITY)
                )
                total_weight += weight
                word_count += 1
                if word_count > MAX_WORDS:
                    raise TranscriptSecurityError('stt_words_excessive')
        text = ''.join(text_parts).strip()
        if (
            not text
            or len(text) > MAX_TRANSCRIPT_CHARACTERS
            or not word_count
            or total_weight <= 0.0
        ):
            raise TranscriptSecurityError('stt_final_transcript_empty')
        confidence = math.exp(weighted_log_probability / total_weight)
        if not math.isfinite(confidence):
            raise TranscriptSecurityError('stt_confidence_invalid')
        return text, min(1.0, max(0.0, confidence))

    @chain_free_boundary
    def transcribe(self, capture, expected_attestation=None):
        """Transcribe one in-memory capture and zero all audio buffers."""
        pcm, capture_receipt = self._authority.consume_capture(capture)
        waveform = None
        try:
            model_attestation = self.prepare()
            if (
                expected_attestation is not None
                and model_attestation != expected_attestation
            ):
                raise ModelSecurityError('model_attestation_changed')
            numpy = self._numpy_loader()
            try:
                waveform = numpy.frombuffer(pcm, dtype='<i2').astype(
                    numpy.float32
                )
                waveform /= 32768.0
            except Exception:
                raise TranscriptSecurityError('pcm_conversion_failed')
            try:
                segments, _info = self._model.transcribe(
                    waveform,
                    language='ko',
                    task='transcribe',
                    beam_size=5,
                    temperature=0.0,
                    vad_filter=True,
                    vad_parameters={'min_silence_duration_ms': 500},
                    condition_on_previous_text=False,
                    word_timestamps=True,
                )
                duration_seconds = (
                    capture_receipt.frame_count
                    / capture_receipt.sample_rate_hz
                )
                text, confidence = self._materialize_segments(
                    segments,
                    maximum_duration_seconds=duration_seconds,
                )
            except TranscriptSecurityError:
                raise
            except Exception:
                raise TranscriptSecurityError('stt_inference_failed')
            return self._authority.issue_transcript(
                capture_receipt,
                model_digest=model_attestation.model_digest,
                text=text,
                confidence=confidence,
            )
        finally:
            if waveform is not None:
                try:
                    waveform.fill(0.0)
                except Exception:
                    pass
            zeroize(pcm)
