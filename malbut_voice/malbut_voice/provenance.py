"""Instance-private capabilities for microphone transcript provenance."""

import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass

from malbut_voice.errors import TranscriptSecurityError


_PRIVATE_CONSTRUCTOR = object()


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode('ascii')


@dataclass(frozen=True)
class DeviceAttestation:
    """Content-free identity of the statically verified capture path."""

    binding_digest: str
    binary_device: int
    binary_inode: int
    rdev_major: int
    rdev_minor: int


@dataclass(frozen=True)
class ModelAttestation:
    """Content-free identity of one verified local model snapshot."""

    model_digest: str
    model_id: str
    snapshot_revision: str


@dataclass(frozen=True)
class PublicTranscriptAudit:
    """Content-free claims that remain safe for local diagnostic output."""

    schema_version: int
    utterance_id: str
    sequence: int
    source: str
    capture_origin: str
    duration_ms: int
    sample_rate_hz: int
    channel_count: int
    confidence: float
    text_chars: int
    physical_audio_capture: bool
    microphone_provenance_verified: bool
    speaker_identity_verified: bool
    execution_authority: bool

    def to_dict(self):
        """Return the bounded content-free audit representation."""
        return {
            'schema_version': self.schema_version,
            'utterance_id': self.utterance_id,
            'sequence': self.sequence,
            'source': self.source,
            'capture_origin': self.capture_origin,
            'duration_ms': self.duration_ms,
            'sample_rate_hz': self.sample_rate_hz,
            'channel_count': self.channel_count,
            'confidence': self.confidence,
            'text_chars': self.text_chars,
            'physical_audio_capture': self.physical_audio_capture,
            'microphone_provenance_verified': (
                self.microphone_provenance_verified
            ),
            'speaker_identity_verified': self.speaker_identity_verified,
            'execution_authority': self.execution_authority,
        }


@dataclass(frozen=True, slots=True)
class _CaptureReceipt:
    boot_id: str
    issuer_instance_id: str
    sequence: int
    device_binding_digest: str
    started_boottime_ns: int
    ended_boottime_ns: int
    frame_count: int
    sample_rate_hz: int
    channels: int
    pcm_sha256: str
    mac: str

    def unsigned_dict(self):
        return {
            'boot_id': self.boot_id,
            'issuer_instance_id': self.issuer_instance_id,
            'sequence': self.sequence,
            'device_binding_digest': self.device_binding_digest,
            'started_boottime_ns': self.started_boottime_ns,
            'ended_boottime_ns': self.ended_boottime_ns,
            'frame_count': self.frame_count,
            'sample_rate_hz': self.sample_rate_hz,
            'channels': self.channels,
            'pcm_sha256': self.pcm_sha256,
        }


class _CapturedMicrophonePCM:
    __slots__ = ('_authority', '_consumed', '_pcm', '_receipt')

    def __init__(self, seal, authority, pcm, receipt):
        if seal is not _PRIVATE_CONSTRUCTOR:
            raise TypeError('private microphone capture capability')
        self._authority = authority
        self._pcm = pcm
        self._receipt = receipt
        self._consumed = False


@dataclass(frozen=True, slots=True)
class _TranscriptReceipt:
    capture_mac: str
    model_digest: str
    transcript_sha256: str
    confidence_micros: int
    mac: str

    def unsigned_dict(self):
        return {
            'capture_mac': self.capture_mac,
            'model_digest': self.model_digest,
            'transcript_sha256': self.transcript_sha256,
            'confidence_micros': self.confidence_micros,
        }


class _TranscribedMicrophoneCapture:
    __slots__ = (
        '_authority', '_capture_receipt', '_consumed', '_confidence',
        '_receipt', '_text',
    )

    def __init__(
        self,
        seal,
        authority,
        capture_receipt,
        text,
        confidence,
        receipt,
    ):
        if seal is not _PRIVATE_CONSTRUCTOR:
            raise TypeError('private microphone transcript capability')
        self._authority = authority
        self._capture_receipt = capture_receipt
        self._text = text
        self._confidence = confidence
        self._receipt = receipt
        self._consumed = False


class _ProvenanceAuthority:
    __slots__ = ('_instance_id', '_lock', '_secret', '_sequence')

    def __init__(self):
        self._secret = secrets.token_bytes(32)
        self._instance_id = secrets.token_hex(16)
        self._sequence = 0
        self._lock = threading.Lock()

    @property
    def instance_id(self):
        return self._instance_id

    def _mac(self, label, value):
        return hmac.new(
            self._secret,
            label.encode('ascii') + b'\0' + _canonical(value),
            hashlib.sha256,
        ).hexdigest()

    def issue_capture(
        self,
        pcm,
        *,
        boot_id,
        device_binding_digest,
        started_boottime_ns,
        ended_boottime_ns,
        sample_rate_hz,
        channels,
    ):
        if type(pcm) is not bytearray or not pcm or len(pcm) % 2:
            raise TranscriptSecurityError('capture_capability_invalid')
        with self._lock:
            if self._sequence >= (1 << 63) - 1:
                raise TranscriptSecurityError('capture_sequence_exhausted')
            self._sequence += 1
            unsigned = {
                'boot_id': boot_id,
                'issuer_instance_id': self._instance_id,
                'sequence': self._sequence,
                'device_binding_digest': device_binding_digest,
                'started_boottime_ns': started_boottime_ns,
                'ended_boottime_ns': ended_boottime_ns,
                'frame_count': len(pcm) // (2 * channels),
                'sample_rate_hz': sample_rate_hz,
                'channels': channels,
                'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
            }
            receipt = _CaptureReceipt(
                **unsigned,
                mac=self._mac('capture-v1', unsigned),
            )
        return _CapturedMicrophonePCM(
            _PRIVATE_CONSTRUCTOR,
            self,
            pcm,
            receipt,
        )

    def consume_capture(self, capture):
        with self._lock:
            if (
                type(capture) is not _CapturedMicrophonePCM
                or capture._authority is not self
                or capture._consumed
            ):
                raise TranscriptSecurityError('capture_capability_rejected')
            receipt = capture._receipt
            expected_mac = self._mac('capture-v1', receipt.unsigned_dict())
            pcm_sha256 = hashlib.sha256(capture._pcm).hexdigest()
            capture._consumed = True
            if (
                not hmac.compare_digest(receipt.mac, expected_mac)
                or not hmac.compare_digest(receipt.pcm_sha256, pcm_sha256)
            ):
                zeroize(capture._pcm)
                raise TranscriptSecurityError('capture_integrity_rejected')
            return capture._pcm, receipt

    def issue_transcript(
        self,
        capture_receipt,
        *,
        model_digest,
        text,
        confidence,
    ):
        if not isinstance(text, str) or not text:
            raise TranscriptSecurityError('transcript_capability_rejected')
        if isinstance(confidence, bool) or not isinstance(
            confidence,
            (int, float),
        ):
            raise TranscriptSecurityError('transcript_capability_rejected')
        confidence_micros = int(round(confidence * 1000000))
        if confidence_micros < 0 or confidence_micros > 1000000:
            raise TranscriptSecurityError('transcript_capability_rejected')
        unsigned = {
            'capture_mac': capture_receipt.mac,
            'model_digest': model_digest,
            'transcript_sha256': hashlib.sha256(
                text.encode('utf-8')
            ).hexdigest(),
            'confidence_micros': confidence_micros,
        }
        receipt = _TranscriptReceipt(
            **unsigned,
            mac=self._mac('transcript-v1', unsigned),
        )
        return _TranscribedMicrophoneCapture(
            _PRIVATE_CONSTRUCTOR,
            self,
            capture_receipt,
            text,
            confidence_micros / 1000000.0,
            receipt,
        )

    def consume_transcript(self, transcript, model_digest):
        with self._lock:
            if (
                type(transcript) is not _TranscribedMicrophoneCapture
                or transcript._authority is not self
                or transcript._consumed
            ):
                raise TranscriptSecurityError('transcript_capability_rejected')
            receipt = transcript._receipt
            expected_mac = self._mac('transcript-v1', receipt.unsigned_dict())
            text_sha256 = hashlib.sha256(
                transcript._text.encode('utf-8')
            ).hexdigest()
            transcript._consumed = True
            if (
                not hmac.compare_digest(receipt.mac, expected_mac)
                or not hmac.compare_digest(
                    receipt.transcript_sha256,
                    text_sha256,
                )
                or not hmac.compare_digest(receipt.model_digest, model_digest)
            ):
                raise TranscriptSecurityError('transcript_integrity_rejected')
            return (
                transcript._text,
                transcript._confidence,
                transcript._capture_receipt,
                transcript._receipt,
            )

    def sign_final(self, value):
        return self._mac('final-v1', value)

    def verify_final(self, value, proof):
        expected = self._mac('final-v1', value)
        return hmac.compare_digest(expected, proof)


def zeroize(buffer):
    """Overwrite one mutable byte buffer in place."""
    if isinstance(buffer, bytearray):
        buffer[:] = b'\0' * len(buffer)
