"""Optional DSP checks; hardware and model quality are measured separately."""
import numpy as np
import pytest

pytest.importorskip('pywebrtc_audio')
pytest.importorskip('tflite_runtime')

from malbut_stt.echo import processor


def test_calibration_measures_internal_device_delay_instead_of_host_latency():
    ref = np.random.default_rng(5).normal(0, 0.05, 64000).astype(np.float32)
    mic = np.pad(ref * 2, (8320, 0))[:len(ref)]
    result = processor.calibrate(mic, ref, anchor_difference=0.007)
    assert result['lag_samples'] == 8320
    assert result['reference_delay_s'] == pytest.approx(0.487)
    assert result['confidence'] > 0.8


def test_silent_calibration_is_rejected():
    with pytest.raises(ValueError, match='unreliable'):
        processor.calibrate(np.zeros(64000), np.zeros(64000))


def test_low_correlation_requires_consistent_delay_in_both_halves():
    rng = np.random.default_rng(77)
    ref = rng.normal(0, 0.01, 128000).astype(np.float32)
    mic = np.pad(ref, (8320, 0))[:len(ref)] + rng.normal(0, 0.105, len(ref))
    result = processor.calibrate(mic, ref)
    assert 0.08 < result['confidence'] < 0.1
    assert result['window_lags'] == [8320, 8320]
    # A source whose delay changes between portions must not open duplex input.
    changed = np.pad(ref, (10000, 0))[:len(ref)]
    changed[:64000] = np.pad(ref, (8320, 0))[:64000]
    with pytest.raises(ValueError, match='unreliable'):
        processor.calibrate(changed, ref)


def test_no_reference_preserves_microphone_pcm_with_only_fixed_delay(monkeypatch):
    class UnusedModel:
        def __init__(self, *_):
            pass

        def process(self, *_):
            pytest.fail('Neural inference ran without a render reference')

    monkeypatch.setattr(processor, 'DTLN', UnusedModel)
    echo = processor.EchoProcessor(1, 'unused')
    mic = np.random.default_rng(3).normal(0, 0.05, 8192).astype(np.float32)
    out = echo.process_array(mic, np.zeros_like(mic))
    np.testing.assert_array_equal(out[:512], np.zeros(512))
    np.testing.assert_array_equal(out[512:], mic[:-512])


def test_bad_pcm_cannot_reach_vad(monkeypatch):
    monkeypatch.setattr(processor, 'DTLN', lambda *_: None)
    echo = processor.EchoProcessor(1, 'unused')
    for bad in [np.full(512, np.nan), np.zeros(511)]:
        with pytest.raises(ValueError):
            echo.process_array(bad, np.zeros(512))
