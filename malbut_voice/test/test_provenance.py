"""Tests for instance-private capture and transcript capabilities."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from malbut_voice.errors import TranscriptSecurityError
from malbut_voice.provenance import (
    _CapturedMicrophonePCM,
    _ProvenanceAuthority,
)


def _issue(authority, pcm=None):
    return authority.issue_capture(
        bytearray(b'\x01\x00\x02\x00' if pcm is None else pcm),
        boot_id='00000000-0000-0000-0000-000000000001',
        device_binding_digest='a' * 64,
        started_boottime_ns=100,
        ended_boottime_ns=200,
        sample_rate_hz=16000,
        channels=1,
    )


def test_private_capture_constructor_rejects_callers():
    """Do not let a caller manufacture a capture capability directly."""
    with pytest.raises(TypeError, match='private'):
        _CapturedMicrophonePCM(None, None, bytearray(b'\0\0'), None)


def test_capture_is_bound_to_exact_issuer_and_single_consumption():
    """Reject foreign issuers and replay after the first STT handoff."""
    issuer = _ProvenanceAuthority()
    foreign = _ProvenanceAuthority()
    capture = _issue(issuer)

    with pytest.raises(TranscriptSecurityError, match='rejected'):
        foreign.consume_capture(capture)
    _pcm, receipt = issuer.consume_capture(capture)
    assert receipt.sequence == 1
    with pytest.raises(TranscriptSecurityError, match='rejected'):
        issuer.consume_capture(capture)


def test_capture_mac_binds_the_actual_pcm_bytes():
    """Reject mutation of the audio buffer after receipt issuance."""
    issuer = _ProvenanceAuthority()
    capture = _issue(issuer)
    capture._pcm[0] ^= 0xFF

    with pytest.raises(TranscriptSecurityError, match='integrity'):
        issuer.consume_capture(capture)


def test_transcript_receipt_binds_capture_model_text_and_confidence():
    """Reject model substitution and replay of a transcript capability."""
    issuer = _ProvenanceAuthority()
    pcm, capture_receipt = issuer.consume_capture(_issue(issuer))
    transcript = issuer.issue_transcript(
        capture_receipt,
        model_digest='b' * 64,
        text='안녕하세요',
        confidence=0.8123456,
    )

    with pytest.raises(TranscriptSecurityError, match='integrity'):
        issuer.consume_transcript(transcript, 'c' * 64)
    with pytest.raises(TranscriptSecurityError, match='rejected'):
        issuer.consume_transcript(transcript, 'b' * 64)
    transcript = issuer.issue_transcript(
        capture_receipt,
        model_digest='b' * 64,
        text='안녕하세요',
        confidence=0.8123456,
    )
    values = issuer.consume_transcript(transcript, 'b' * 64)
    assert values[0] == '안녕하세요'
    assert values[1] == 0.812346
    with pytest.raises(TranscriptSecurityError, match='rejected'):
        issuer.consume_transcript(transcript, 'b' * 64)
    pcm[:] = b'\0' * len(pcm)


def test_capture_sequences_are_unique_under_concurrent_issue():
    """Serialize monotonically issued receipt sequence values per instance."""
    issuer = _ProvenanceAuthority()

    def issue_and_consume(_index):
        pcm, receipt = issuer.consume_capture(_issue(issuer))
        pcm[:] = b'\0' * len(pcm)
        return receipt.sequence

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(issue_and_consume, range(32)))

    assert sorted(sequences) == list(range(1, 33))
