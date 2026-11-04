"""Delayed startup must process every sample but announce only a fresh stream."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from malbut_stt.echo import scheduling, timing, worker


def test_priming_counts_initialization_only_and_never_discards_after_ready():
    gate = worker.StartupPriming(10.0)
    assert not gate.accept(9.9, 10.1)
    assert not gate.accept(10.01, 10.5)
    assert gate.accept(10.6, 10.75)
    assert (gate.historical_frames, gate.backlog_frames) == (1, 1)
    assert gate.accept(10.61, 20.)  # Normal stale PCM reaches the existing parent guard.
    gate.check_deadline(100.)
    assert (gate.historical_frames, gate.backlog_frames) == (1, 1)


def test_priming_timeout_does_not_wait_indefinitely_or_mark_ready():
    gate = worker.StartupPriming(10.)
    assert not gate.accept(10.1, 14.999)
    with pytest.raises(TimeoutError, match='startup catch-up'):
        gate.accept(15., 15.)
    assert not gate.ready


@pytest.mark.parametrize('timestamp', [float('nan'), float('inf'), 11.])
def test_invalid_startup_clock_cannot_advertise_ready(timestamp):
    gate = worker.StartupPriming(10.)
    with pytest.raises(RuntimeError, match='startup capture timestamp'):
        gate.accept(timestamp, 10.1)
    assert not gate.ready


@pytest.mark.parametrize('model_delay,history_delay,step_s,recover,calibration_errors,ref_lead', [
    (0., 0., .001, True, (), 0),
    (1., 2., .01, True, (), 0),
    (1., 2., .04, False, (), 0),
    (0., 0., .001, True, (ValueError,), 0),
    (1., 2., .01, True, (ValueError,), 2),
    (1., 2., .04, False, (ValueError,), 0),
    (0., 0., .001, False, (ValueError, ValueError), 0),
    (0., 0., .001, False, (RuntimeError,), 0),
    (0., 0., .001, False, (ValueError, RuntimeError), 0),
])
def test_real_worker_defers_ready_and_preserves_ordered_dsp(
        monkeypatch, tmp_path, model_delay, history_delay, step_s, recover,
        calibration_errors, ref_lead):
    """Run the real worker loop with deterministic capture/native boundaries.

    Capture continues on its 32 ms clock while model initialization/training
    consumes wall time. The live processor records every near-end sample index.
    """
    monkeypatch.delenv('MALBUT_AEC_TRACE_PATH', raising=False)
    monkeypatch.setattr(scheduling, 'configure_audio_worker_priority', lambda: None)
    now = [100.]
    monkeypatch.setattr(worker, 'monotonic', lambda: now[0])
    processed = []
    messages = []
    traces = []
    stopped = [False]
    delayed_live = [False]
    calibration_calls = []
    stream_events = []

    class Trace:
        enabled = False
        def __init__(self, role):
            pass
        def record(self, event, metrics, **kwargs):
            traces.append((event, metrics))
        def close(self):
            pass

    monkeypatch.setattr(timing, 'TimingTrace', Trace)

    class Processor:
        retain_recovery = False
        def __init__(self, **kwargs):
            now[0] += model_delay
        def process_array(self, near, far):
            assert near.shape == far.shape
            indices = [int(near[i]) for i in range(0, len(near), 512)]
            processed.extend(indices)
            if len(near) > 512:
                now[0] += history_delay
            else:
                now[0] += step_s
                # Deliberately delay the first ordinary frame after readiness.
                # The startup gate must not silently discard or restamp it.
                if any(m[0] == 'ready' for _, m in messages) and not delayed_live[0]:
                    now[0] += .5
                    delayed_live[0] = True
            self.asr_output = np.zeros_like(near)
            return self.asr_output

    class Events:
        cursor = 0
        def __init__(self, **kwargs):
            pass
        def get(self, **kwargs):
            if self.cursor < ref_lead:
                name, index = 'ref', self.cursor
            else:
                index, source = divmod(self.cursor - ref_lead, 2)
                name = 'ref' if source == 0 else 'mic'
                if name == 'ref':
                    index += ref_lead
            self.cursor += 1
            timestamp = 100. + index * .032
            now[0] = max(now[0], timestamp)
            return (name, index,
                    np.full(512, index, dtype=np.float32), timestamp)

    class Output:
        def put(self, message, **kwargs):
            messages.append((now[0], message))
            if (message[0] == 'error'
                    or sum(m[0] == 'audio' for _, m in messages) == 2):
                stopped[0] = True

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            if self.target.__name__ == 'play_calibration':
                self.target()
        def join(self, **kwargs):
            pass

    class Stream:
        def __init__(self, **kwargs):
            self.kind = 'warm' if 'callback' in kwargs else 'stimulus'
            stream_events.append(('open', self.kind))
        def start(self):
            stream_events.append(('start', self.kind))
        def write(self, samples):
            now[0] += len(samples) / 16000
        def abort(self):
            stream_events.append(('abort', self.kind))
        def close(self):
            stream_events.append(('close', self.kind))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()

    calibration = dict(lag_samples=1024, reference_lead_s=.04,
                       reference_delay_s=.064, microphone_gain=1.)
    def calibrate(mic, ref, anchor_difference):
        # Uneven event delivery must not give the retry two different origins.
        mic_indices = [int(mic[i]) for i in range(0, len(mic), 512)]
        ref_indices = [int(ref[i]) for i in range(0, len(ref), 512)]
        assert mic_indices[0] == ref_indices[0]
        assert mic_indices == list(range(mic_indices[0], mic_indices[-1] + 1))
        assert ref_indices == list(range(ref_indices[0], ref_indices[-1] + 1))
        assert anchor_difference == pytest.approx(0.)
        calibration_calls.append((mic_indices, ref_indices))
        if len(calibration_calls) <= len(calibration_errors):
            raise calibration_errors[len(calibration_calls)-1]('calibration test failure')
        return dict(calibration)
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(OutputStream=Stream))
    monkeypatch.setitem(sys.modules, 'malbut_stt.echo.processor', SimpleNamespace(
        EchoProcessor=Processor, calibrate=calibrate))
    monkeypatch.setattr(worker, 'Queue', Events)
    monkeypatch.setattr(worker, 'Thread', Thread)
    monkeypatch.setattr(worker.subprocess, 'check_output', lambda args, **kw:
        json.dumps({'sources': [{'name': 'mic'}], 'sinks': [{'name': 'speaker', 'index': 1}],
                    'sink-inputs': []}[args[-1]]))
    # Model integrity is a separate boundary: provide the manifest's expected
    # digests as fake bytes, with a deterministic test-only hash adapter.
    manifest = json.loads(Path(worker.__file__).with_name('models.json').read_text())
    for name, digest in manifest['sha256'].items():
        (tmp_path / name).write_text(digest)
    monkeypatch.setattr(worker.hashlib, 'sha256',
                        lambda data: SimpleNamespace(hexdigest=lambda: data.decode()))
    audio = tmp_path / 'calibration.wav'
    with wave.open(str(audio), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(bytes(16000 * 2 * 2))
    worker.run(dict(source='mic', sink='speaker', models=str(tmp_path),
                    calibration_audio=str(audio)), Output(),
               SimpleNamespace(is_set=lambda: stopped[0]))
    retried = bool(calibration_errors and calibration_errors[0] is ValueError)
    assert len(calibration_calls) == (2 if retried else 1)
    assert stream_events.count(('open', 'warm')) == 1
    assert stream_events.count(('start', 'warm')) == 1
    assert stream_events.count(('abort', 'warm')) == 1
    assert stream_events.count(('close', 'warm')) == 1
    assert stream_events.count(('open', 'stimulus')) == len(calibration_calls)
    assert stream_events.count(('close', 'stimulus')) == len(calibration_calls)
    if retried:
        first_mic, first_ref = calibration_calls[0]
        assert calibration_calls[1][0][0] == max(first_mic[-1], first_ref[-1]) + 1
    calibration_failed = len(calibration_errors) >= len(calibration_calls)
    if calibration_failed:
        assert processed == []
        assert [m[0] for _, m in messages] == ['error']
        assert 'calibration test failure' in messages[0][1][1]
        return
    training_start = calibration_calls[-1][0][0]
    assert processed == list(range(training_start, processed[-1] + 1))
    if recover:
        assert [m[0] for _, m in messages] == ['ready', 'audio', 'audio']
        ready_at, ready = messages[0]
        first_at, first = messages[1]
        assert ready_at == first_at
        assert 0 <= first_at - first[2] <= worker.StartupPriming.MAX_AGE_S
        assert messages[2][0] - messages[2][1][2] > .4
        assert messages[2][1][2] - first[2] == pytest.approx(.032)
        ready_metrics = next(metrics for event, metrics in traces if event == 'worker_ready')
        assert ready_metrics['first_audio_age_s'] <= worker.StartupPriming.MAX_AGE_S
        if model_delay:
            assert ready_metrics['priming_historical_frames'] > 0
            assert ready_metrics['priming_backlog_frames'] > 0
    else:
        assert [m[0] for _, m in messages] == ['error']
        assert 'startup catch-up timed out' in messages[0][1][1]
        failure = next(metrics for event, metrics in traces if event == 'worker_failure')
        assert failure['priming_elapsed_s'] >= worker.StartupPriming.TIMEOUT_S
        assert failure['priming_historical_frames'] > 0
