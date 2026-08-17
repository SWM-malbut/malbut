"""Explicit opt-in hardware smoke tests; skipped in ordinary test runs."""

import os
from pathlib import Path

import pytest

from malbut_voice.config import load_protected_config
from malbut_voice.transcript_source import MicrophoneTranscriptSource


MICROPHONE_OPT_IN = 'I_UNDERSTAND_THIS_OPENS_THE_MICROPHONE'


@pytest.mark.skipif(
    os.environ.get('MALBUT_RUN_MIC_SMOKE') != MICROPHONE_OPT_IN,
    reason='real microphone smoke requires an explicit operator opt-in',
)
def test_explicit_one_second_real_microphone_final():
    """Capture one real second only under the exact operator opt-in phrase."""
    config_path = os.environ.get('MALBUT_MIC_STT_CONFIG')
    if not config_path:
        pytest.fail('MALBUT_MIC_STT_CONFIG is required for hardware smoke')
    config = load_protected_config(Path(config_path))
    source = MicrophoneTranscriptSource(config)

    result = source.capture_final(duration_seconds=1)

    assert source.verify_final(result)
    assert result.event.capture_origin == 'microphone'
    assert result.event.is_final is True
