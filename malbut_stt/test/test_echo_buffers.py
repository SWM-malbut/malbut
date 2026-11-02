"""PCM clocks and stream ownership without optional native DSP dependencies."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest


@pytest.fixture
def echo_class(monkeypatch):
    class Aec:
        def __init__(self, **_):
            pass

        def process(self, near, far):
            assert near.shape == far.shape == (160,)
            return near.copy()

    class Neural:
        def __init__(self, *_):
            pass

        def reset(self):
            pass

        def process(self, near, far):
            # Suppress VAD while leaving the ASR waveform intact.
            return np.zeros_like(near)

    monkeypatch.setitem(sys.modules, 'pywebrtc_audio', SimpleNamespace(AudioProcessor=Aec))
    monkeypatch.setitem(sys.modules, 'malbut_stt.echo.neural', SimpleNamespace(DTLN=Neural))
    path = Path(__file__).parents[1] / 'malbut_stt/echo/processor.py'
    spec = importlib.util.spec_from_file_location('malbut_stt.echo._buffer_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EchoProcessor


@pytest.mark.parametrize('active', [False, True])
@pytest.mark.parametrize('recovery', [False, True])
def test_reframing_preserves_32ms_clock_and_ungated_syllables(echo_class, active, recovery):
    processor = echo_class(1, 'unused', retain_recovery=recovery)
    mic = np.random.default_rng(9).normal(0, .05, 128 * 97).astype(np.float32)
    far = np.full_like(mic, .1 if active else 0.)
    chunks, recognized, recovered = [], [], []
    for start in range(0, len(mic), 512):
        chunks.append(processor.process_array(mic[start:start+512], far[start:start+512]))
        recognized.append(processor.asr_output)
        recovered.append(processor.recovery_output)
    delayed = np.concatenate((np.zeros(512, np.float32), mic[:-512]))
    np.testing.assert_array_equal(np.concatenate(recognized), delayed)
    np.testing.assert_array_equal(np.concatenate(chunks), np.zeros_like(mic) if active else delayed)
    if recovery:
        np.testing.assert_array_equal(np.concatenate(recovered), delayed)
    else:
        assert all(frame.size == 0 for frame in recovered)


def test_chunk_processing_does_not_mutate_previous_blocks_or_inputs(echo_class):
    mic = np.arange(128 * 600, dtype=np.float32) / 100000
    far = np.zeros_like(mic)
    far[2048:4096] = .1
    far[25088:27008] = .2
    original = mic.copy(), far.copy()
    whole = echo_class(2, 'unused', retain_recovery=True)
    expected = whole.process_array(mic, far), whole.asr_output, whole.recovery_output
    split = echo_class(2, 'unused', retain_recovery=True)
    streams = [], [], []
    for start in range(0, len(mic), 128):
        streams[0].append(split.process(mic[start:start+128], far[start:start+128]))
        streams[1].append(split.asr_output)
        streams[2].append(split.recovery_output)
    for parts, wanted in zip(streams, expected):
        np.testing.assert_array_equal(np.concatenate(parts), wanted)
    np.testing.assert_array_equal(mic, original[0])
    np.testing.assert_array_equal(far, original[1])
