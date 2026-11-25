"""Verify front-end filtering, adaptive near-field VAD gating, and venue calibration."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
import wave

import numpy as np
import pytest
import yaml

from malbut_stt.audio import CapturePCM, CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.noise_guard import (
    HighPassFilter,
    NoiseAwareVad,
    NoiseFloorTracker,
    PreprocessedRecorder,
    frame_rms_dbfs,
    recommend_venue_settings,
)
from malbut_stt.silero_gate import SileroGate, from_environment as silero_gate_from_env
from malbut_stt.silero_stream import SileroStreamVad
from malbut_stt.streaming import StreamingUtteranceCollector


def _tone_pcm(freq_hz: float, dbfs: float, duration_s: float, *, rate: int = 16000) -> bytes:
    """Generate a mono PCM16 sine wave whose RMS is ``dbfs``."""
    count = round(duration_s * rate)
    t = np.arange(count, dtype=np.float64) / rate
    # Sine wave peak = sqrt(2) * rms.
    peak = (10.0 ** (dbfs / 20.0)) * math.sqrt(2.0) * 32768.0
    samples = np.rint(np.clip(peak * np.sin(2.0 * math.pi * freq_hz * t), -32768, 32767))
    return samples.astype('<i2').tobytes()


def _noise_pcm(dbfs: float, duration_s: float, *, seed: int = 0, rate: int = 16000) -> bytes:
    rng = np.random.default_rng(seed)
    count = round(duration_s * rate)
    raw = rng.standard_normal(count)
    raw = raw / max(1e-12, float(np.sqrt(np.mean(raw * raw))))
    amp = (10.0 ** (dbfs / 20.0)) * 32768.0
    samples = np.rint(np.clip(raw * amp, -32768, 32767)).astype('<i2')
    return samples.tobytes()


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)


class _FakeSileroModel:
    def __init__(self, score: float = 0.9):
        self.score = score
        self.resets = 0
        self.calls = 0

    def new_stream(self):
        return _FakeSileroModel(score=self.score)

    def reset(self):
        self.resets += 1

    def probability(self, pcm: bytes) -> float:
        assert len(pcm) == 1024
        self.calls += 1
        return self.score


def test_frame_rms_dbfs_measures_known_levels_and_rejects_bad_pcm():
    assert frame_rms_dbfs(bytes(640)) == -120.0
    tone_20 = _tone_pcm(500.0, -20.0, 0.02)
    assert frame_rms_dbfs(tone_20) == pytest.approx(-20.0, abs=0.25)
    tone_40 = _tone_pcm(500.0, -40.0, 0.02)
    assert frame_rms_dbfs(tone_40) == pytest.approx(-40.0, abs=0.25)
    for bad in (b'', b'\x00', b'\x01\x02\x03'):
        with pytest.raises(ValueError):
            frame_rms_dbfs(bad)


def test_high_pass_filter_attenuates_rumble_preserves_speech_and_matches_scalar_iir():
    # Compare vectorized block recurrence against exact scalar recurrence across chunk boundaries.
    rng = np.random.default_rng(11)
    signal = rng.uniform(-0.5, 0.5, size=2000).astype(np.float64)
    hpf_whole = HighPassFilter(100.0)
    out_whole = hpf_whole.process_samples(signal)

    hpf_chunked = HighPassFilter(100.0)
    chunks = [hpf_chunked.process_samples(signal[i:i + 320]) for i in range(0, 2000, 320)]
    out_chunked = np.concatenate(chunks)
    np.testing.assert_allclose(out_chunked, out_whole, atol=1e-6)

    # Exact scalar reference.
    alpha = hpf_whole.alpha
    ref = np.empty_like(signal)
    px = py = 0.0
    for idx, sample in enumerate(signal):
        py = alpha * (py + sample - px)
        px = sample
        ref[idx] = py
    np.testing.assert_allclose(out_whole, ref.astype(np.float32), atol=1e-6)

    # Verify spectral response: 30 Hz rumble strongly attenuated, 1 kHz speech preserved.
    hpf = HighPassFilter(100.0)
    rumble = _tone_pcm(30.0, -20.0, 0.5)
    rumble_out = hpf.process_pcm(rumble)
    # Ignore initial 50 ms transient.
    assert frame_rms_dbfs(rumble_out[1600:]) < -29.0

    hpf.reset()
    speech = _tone_pcm(1000.0, -20.0, 0.5)
    speech_out = hpf.process_pcm(speech)
    assert frame_rms_dbfs(speech_out[1600:]) == pytest.approx(-20.0, abs=0.5)


def test_noise_floor_tracker_adapts_quickly_at_startup_and_ignores_active_speech():
    tracker = NoiseFloorTracker(warmup_frames=10, window_frames=20)
    # Noisy hall ambient at -35 dBFS.
    for _ in range(20):
        tracker.observe(-35.0, is_active_speech=False)
    assert tracker.floor_dbfs == pytest.approx(-35.0, abs=0.1)

    # Active presenter speech at -15 dBFS must never pull the noise floor up.
    for _ in range(100):
        tracker.observe(-15.0, is_active_speech=True)
    assert tracker.floor_dbfs == pytest.approx(-35.0, abs=0.1)

    # Digital silence frames (-120 dBFS) must not collapse the room estimate.
    for _ in range(50):
        tracker.observe(-120.0, is_active_speech=False)
    assert tracker.floor_dbfs == pytest.approx(-35.0, abs=0.1)

    # Spiky babble with occasional brief valleys must stay near upper ambient, not valley.
    spiky = NoiseFloorTracker(warmup_frames=10, window_frames=40, quantile=0.75)
    for idx in range(40):
        spiky.observe(-52.0 if idx % 4 == 0 else -34.0, is_active_speech=False)
    assert spiky.floor_dbfs == pytest.approx(-34.0, abs=1.0)


def test_noise_aware_vad_ignores_single_frame_babble_spike_during_endpoint_silence():
    silero = SileroStreamVad(model=_FakeSileroModel(score=0.95), threshold=0.65)
    tracker = NoiseFloorTracker(warmup_frames=10)
    vad = NoiseAwareVad(
        silero, min_rms_dbfs=-35.0, snr_margin_db=6.0, sustain_margin_db=3.0,
        hangover_frames=2, onset_confirm_frames=2, high_pass_hz=100.0, floor_tracker=tracker,
    )
    collector = StreamingUtteranceCollector(
        vad.is_speech,
        settings=CaptureSettings(min_speech_s=0.16, silence_timeout_s=0.6, max_utterance_s=10.0),
    )
    # Warm up ambient at -40 dBFS.
    collector.feed(_noise_pcm(-40.0, 0.4, seed=3))
    # Speak loudly for 0.4 s, then drop to -40 dBFS with an isolated 20 ms -20 dBFS spike mid-silence.
    speech = _tone_pcm(500.0, -18.0, 0.4)
    quiet_a = _noise_pcm(-40.0, 0.3, seed=4)
    spike = _tone_pcm(500.0, -20.0, 0.02)
    quiet_b = _noise_pcm(-40.0, 0.5, seed=5)
    events = collector.feed(speech + quiet_a + spike + quiet_b)
    assert any(ev.status == 'complete' for ev in events)


def test_noise_aware_vad_blocks_distant_crowd_keeps_soft_tail_and_finalizes_endpoint():
    # Even if Silero flags BOTH distant crowd chatter and near-field speech as 0.95,
    # the energy + adaptive SNR gate must separate them cleanly and finalize endpoints.
    model = _FakeSileroModel(score=0.95)
    silero = SileroStreamVad(model=model, threshold=0.65)
    tracker = NoiseFloorTracker(warmup_frames=10)
    vad = NoiseAwareVad(
        silero, min_rms_dbfs=-38.0, snr_margin_db=8.0, sustain_margin_db=4.0,
        hangover_frames=4, high_pass_hz=100.0, floor_tracker=tracker,
    )
    collector = StreamingUtteranceCollector(
        vad.is_speech,
        settings=CaptureSettings(min_speech_s=0.16, silence_timeout_s=0.6, max_utterance_s=10.0),
    )

    # 1. Distant crowd chatter at -35 dBFS (1.0 s): never triggers speech_started.
    crowd_1s = _noise_pcm(-35.0, 1.0, seed=1)
    events = collector.feed(crowd_1s)
    assert events == []
    assert tracker.floor_dbfs == pytest.approx(-35.0, abs=2.0)

    # 2. Presenter speaks near mic at -18 dBFS (0.5 s), followed by soft ending at -29 dBFS (0.2 s)
    #    (above sustain threshold floor+4 ≈ -31 dBFS, below onset floor+8 ≈ -27 dBFS).
    near_speech = _tone_pcm(400.0, -18.0, 0.5)
    soft_ending = _tone_pcm(400.0, -29.0, 0.2)
    # 3. Presenter stops; hall crowd chatter at -35 dBFS continues for 1.0 s.
    crowd_tail = _noise_pcm(-35.0, 1.0, seed=2)

    all_events = collector.feed(near_speech + soft_ending + crowd_tail)
    statuses = [ev.status for ev in all_events]
    assert 'speech_started' in statuses
    assert 'complete' in statuses
    complete = next(ev for ev in all_events if ev.status == 'complete')
    # Captured utterance should cover speech + soft tail + silence timeout (not hang forever).
    assert complete.voiced_s >= 0.6


def test_noise_aware_vad_reset_preserves_room_floor_and_shares_across_wake_and_command():
    silero = SileroStreamVad(model=_FakeSileroModel(score=0.95), threshold=0.65)
    tracker = NoiseFloorTracker(warmup_frames=10)
    cmd_vad = NoiseAwareVad(
        silero, min_rms_dbfs=-38.0, snr_margin_db=8.0, floor_tracker=tracker)
    wake_vad = cmd_vad.new_stream()
    assert wake_vad.floor_tracker is cmd_vad.floor_tracker
    assert wake_vad.vad is not cmd_vad.vad

    # Learn -34 dBFS hall noise on wake_vad.
    frame = _noise_pcm(-34.0, 0.02, seed=7)
    for _ in range(15):
        assert not wake_vad.is_speech(frame, 16000)
    learned_floor = tracker.floor_dbfs
    assert learned_floor == pytest.approx(-34.0, abs=2.0)

    # Resetting between utterances preserves the learned room noise floor.
    wake_vad.reset()
    cmd_vad.reset()
    assert cmd_vad.floor_tracker.floor_dbfs == pytest.approx(learned_floor, abs=1e-6)
    # Command VAD immediately rejects -34 dBFS crowd noise without re-warming.
    assert not cmd_vad.is_speech(frame, 16000)


def test_noise_aware_vad_rejects_strong_low_frequency_rumble_via_hpf():
    silero = SileroStreamVad(model=_FakeSileroModel(score=0.95), threshold=0.6)
    vad = NoiseAwareVad(silero, min_rms_dbfs=-26.0, snr_margin_db=0.0, high_pass_hz=120.0)
    # 25 Hz HVAC/vibration rumble at -20 dBFS drops well below -26 dBFS after 120 Hz HPF.
    rumble = _tone_pcm(25.0, -20.0, 0.4)
    decisions = [vad.is_speech(rumble[i:i + 640], 16000) for i in range(0, len(rumble), 640)]
    # After initial 2-frame transient, low-frequency rumble stays rejected.
    assert not any(decisions[3:])


def test_preprocessed_recorder_aligns_asr_and_vad_streams_to_exact_160_sample_clock():
    class _FakeRecorder:
        sample_rate = 16000
        selected_device = 'fake-mic'

        def __init__(self, chunks):
            self._chunks = iter(chunks)
            self.events = []

        def start(self):
            self.events.append('start')

        def read(self):
            return next(self._chunks)

        def cancel_read(self):
            self.events.append('cancel_read')

        def stop(self):
            self.events.append('stop')

        def delete(self):
            self.events.append('delete')

    class _FakeWebRtcNs:
        def __init__(self, level):
            self.level = level
            self.calls = 0

        def process(self, block):
            assert block.shape == (160,)
            self.calls += 1
            return block * 0.5

    # Build a deterministic impulse/ramp across 4 chunks of 512 samples.
    total = np.arange(2048, dtype=np.int16) * 4
    chunks = [total[i:i + 512].tolist() for i in range(0, 2048, 512)]
    inner = _FakeRecorder(chunks)
    created = []
    rec = PreprocessedRecorder(
        inner, high_pass_hz=0.0, ns_level=2, ns_mode='vad_only',
        processor_factory=lambda lvl: created.append(lvl) or _FakeWebRtcNs(lvl),
    )
    assert created == [2]
    rec.start()
    asr_parts, vad_parts = [], []
    for _ in range(4):
        cap = rec.read()
        assert isinstance(cap, CapturePCM)
        assert len(cap) == 512 * 2 and len(cap.vad_pcm) == 512 * 2
        asr_parts.append(np.frombuffer(bytes(cap), dtype='<i2'))
        vad_parts.append(np.frombuffer(cap.vad_pcm, dtype='<i2'))
    rec.cancel_read()
    rec.stop()
    rec.delete()
    assert inner.events == ['start', 'cancel_read', 'stop', 'delete']

    asr_all = np.concatenate(asr_parts)
    vad_all = np.concatenate(vad_parts)
    # Both streams share the exact 160-sample alignment delay; vad is scaled by 0.5.
    np.testing.assert_array_equal(asr_all[:160], np.zeros(160, dtype=np.int16))
    np.testing.assert_array_equal(vad_all[:160], np.zeros(160, dtype=np.int16))
    np.testing.assert_array_equal(asr_all[160:], total[:-160])
    np.testing.assert_allclose(vad_all[160:], total[:-160] * 0.5, atol=1.0)


def test_preprocessed_recorder_vad_and_asr_mode_and_hpf_only_mode():
    class _OneShot:
        sample_rate = 16000

        def __init__(self, samples):
            self._samples = samples

        def start(self):
            pass

        def read(self):
            return self._samples

        def stop(self):
            pass

        def delete(self):
            pass

    class _ZeroNs:
        def process(self, block):
            return np.zeros_like(block)

    samples = [2000] * 512
    rec_both = PreprocessedRecorder(
        _OneShot(samples), high_pass_hz=0.0, ns_level=3, ns_mode='vad_and_asr',
        processor_factory=lambda _: _ZeroNs(),
    )
    cap = rec_both.read()
    assert bytes(cap) == bytes(512 * 2) and cap.vad_pcm == bytes(512 * 2)

    rec_hpf = PreprocessedRecorder(_OneShot(samples), high_pass_hz=100.0, ns_mode='off')
    cap_hpf = rec_hpf.read()
    assert bytes(cap_hpf) == cap_hpf.vad_pcm
    # DC constant should decay strongly toward 0 by the end of 512 samples.
    tail = np.frombuffer(bytes(cap_hpf), dtype='<i2')[-64:]
    assert np.max(np.abs(tail)) < 200


def test_silero_stream_custom_threshold_propagates_to_new_stream():
    model = _FakeSileroModel(score=0.6)
    vad = SileroStreamVad(model=model, threshold=0.65)
    wake = vad.new_stream()
    assert wake.threshold == 0.65
    frame = b'\x10\x00' * 320
    assert not vad.is_speech(frame * 2, 16000)
    assert not wake.is_speech(frame * 2, 16000)
    vad.threshold = 0.55
    assert vad.is_speech(frame * 2, 16000)
    for bad in (0.0, 1.0, -0.1, float('nan'), True):
        with pytest.raises(ValueError):
            SileroStreamVad(model=model, threshold=bad)


def test_silero_gate_enforces_custom_threshold_and_min_rms(monkeypatch, tmp_path):
    scores = _FakeSileroModel(score=0.62)
    monkeypatch.setattr('malbut_stt.silero_gate.load_pinned_model', lambda _: scores)
    gate = SileroGate('local.onnx', threshold=0.65, min_rms_dbfs=-35.0)
    loud = _tone_pcm(500.0, -20.0, 0.2)
    quiet = _tone_pcm(500.0, -45.0, 0.2)
    # Rejected because probability 0.62 < 0.65.
    assert not gate.check(loud)['allowed']
    scores.score = 0.85
    # Rejected because RMS -45 dBFS < -35 dBFS even with high Silero score.
    assert not gate.check(quiet)['allowed']
    # Accepted when both probability and RMS exceed thresholds.
    res = gate.check(loud)
    assert res['allowed'] and res['threshold'] == 0.65 and res['min_rms_dbfs'] == -35.0

    # from_environment passes threshold & min_rms_dbfs when non-default.
    model = tmp_path / 'malbut_speech/models/silero-v6.2.3.onnx'
    model.parent.mkdir(parents=True)
    model.touch()
    loaded = silero_gate_from_env({'XDG_CACHE_HOME': str(tmp_path)},
                                  threshold=0.7, min_rms_dbfs=-36.0)
    assert loaded.threshold == 0.7 and loaded.min_rms_dbfs == -36.0


def test_dialogue_pipeline_wake_min_speech_blocks_short_noise_bursts():
    voice = b'\x01\x00' * 320
    quiet = bytes(640)
    wake_calls = []

    def wake_transcribe(pcm, rate):
        wake_calls.append(len(pcm))
        return '제이크야'

    reports = []
    p = DialoguePipeline(
        recorder_factory=lambda: None,
        wake=SimpleNamespace(transcribe=wake_transcribe),
        transcriber=SimpleNamespace(transcribe=lambda *_: ''),
        is_speech=lambda frame, _: frame == voice,
        publish_transcript=lambda *_: None,
        publish_control=lambda *_: None,
        publish_interruption=lambda *_: None,
        report=reports.append,
        on_wake=lambda: None,
        wake_min_speech_s=0.18,
    )
    p.asr_thread = Thread(target=p._infer, daemon=True)
    p.asr_thread.start()
    try:
        # 120 ms burst (6 frames) is below 180 ms wake_min_speech_s -> no Whisper call!
        p.feed(voice * 6 + quiet * 25)
        p.poll()
        assert wake_calls == []
        assert not p.session.active

        # 200 ms speech (10 frames) >= 180 ms -> accepted as wake phrase!
        p.feed(voice * 10 + quiet * 25)
        deadline = monotonic() + 3.0
        while not p.session.active:
            p.poll()
            assert monotonic() < deadline
            sleep(0.002)
        assert len(wake_calls) == 1
    finally:
        p.close()


def test_noisy_venue_yaml_boots_cleanly_in_ros_node_and_supports_live_tuning(monkeypatch, tmp_path):
    from malbut_stt.node import main

    venue_yaml = Path(__file__).parents[1] / 'config/jetson_noisy_venue.yaml'
    loaded = yaml.safe_load(venue_yaml.read_text(encoding='utf-8'))
    venue_params = loaded['malbut_stt']['ros__parameters']

    # Build a minimal ROS mock environment matching test_node.py.
    model = tmp_path / 'local-model'
    model.mkdir()
    state = SimpleNamespace(
        ok=False, logs=[], calls={}, params={'wake_model_path': str(model), **venue_params},
        set_parameters=None, pipeline=None, pipeline_args=None,
    )

    class _Node:
        def __init__(self, _name):
            pass

        def declare_parameter(self, name, default, *, ignore_override=False):
            state.calls.setdefault('defaults', {})[name] = default
            if ignore_override:
                state.params[name] = default
            return SimpleNamespace(value=state.params.get(name, default))

        def get_parameter(self, name):
            return SimpleNamespace(value=state.params.get(name, state.calls['defaults'][name]))

        def add_on_set_parameters_callback(self, cb):
            state.set_parameters = cb

        def get_logger(self):
            return SimpleNamespace(
                info=lambda m: state.logs.append(('info', m)),
                warning=lambda m: state.logs.append(('warning', m)),
                error=lambda m: state.logs.append(('error', m)),
            )

        def create_publisher(self, *_):
            return SimpleNamespace(publish=lambda *_: None)

        def create_subscription(self, *_):
            pass

        def create_client(self, *_):
            return SimpleNamespace(service_is_ready=lambda: False)

        def create_service(self, *_):
            pass

        def destroy_node(self):
            pass

    class _FakeVad:
        def __init__(self, path, *, threshold=0.5):
            self.path = path
            self.threshold = threshold
            state.calls.setdefault('vads', []).append(self)

        def new_stream(self):
            return _FakeVad(self.path, threshold=self.threshold)

        def is_speech(self, pcm, rate):
            return False

        def reset(self):
            pass

    class _FakeNs:
        def __init__(self, **kwargs):
            state.calls['webrtc_ns'] = kwargs

        def process(self, block):
            return block

    class _FakeRecorder:
        sample_rate = 16000

        def __init__(self, **kwargs):
            state.calls['raw_recorder'] = kwargs

        def start(self):
            pass

        def stop(self):
            pass

        def delete(self):
            pass

    spins = 0

    def spin_once(_node, timeout_sec):
        nonlocal spins
        spins += 1
        if spins == 1:
            res = state.set_parameters([
                SimpleNamespace(name='vad_threshold', value=0.72),
                SimpleNamespace(name='vad_min_rms_dbfs', value=-34.0),
                SimpleNamespace(name='vad_snr_margin_db', value=10.0),
            ])
            assert res.successful
            state.params.update(vad_threshold=0.72, vad_min_rms_dbfs=-34.0,
                                vad_snr_margin_db=10.0)
            bad = state.set_parameters([SimpleNamespace(name='vad_threshold', value=1.2)])
            assert not bad.successful
        else:
            raise KeyboardInterrupt

    class _Pipeline:
        def __init__(self, **kwargs):
            state.pipeline_args = kwargs
            state.pipeline = self
            self.command_stream = SimpleNamespace(
                settings=kwargs['settings'], set_next_turn_timing=lambda *_: None)
            self.phase = 'running'
            self.pending_addressee = None
            self.session = SimpleNamespace(playback_id='', playback_state='', session_id='')
            self.capture_ready = SimpleNamespace(is_set=lambda: False)

        def control_web_talk(self, *_a, **_kw):
            return True

        def start(self):
            state.calls['recorder_obj'] = state.pipeline_args['recorder_factory']()

        def poll(self):
            pass

        def close(self):
            pass

    monkeypatch.delenv('MALBUT_STT_DIAGNOSTIC_DIR', raising=False)
    monkeypatch.setenv('MALBUT_STT_INCIDENT_CONFIG', '{"enabled":false}')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setitem(sys.modules, 'pywebrtc_audio', SimpleNamespace(AudioProcessor=_FakeNs))
    monkeypatch.setitem(sys.modules, 'rclpy', SimpleNamespace(
        init=lambda *_, **__: setattr(state, 'ok', True),
        ok=lambda: state.ok,
        shutdown=lambda: setattr(state, 'ok', False),
        spin_once=spin_once,
    ))
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=_Node))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        QoSProfile=SimpleNamespace,
        HistoryPolicy=SimpleNamespace(KEEP_LAST='keep_last'),
        ReliabilityPolicy=SimpleNamespace(RELIABLE='reliable'),
        DurabilityPolicy=SimpleNamespace(VOLATILE='volatile', TRANSIENT_LOCAL='transient_local'),
    ))
    monkeypatch.setitem(sys.modules, 'std_msgs.msg', SimpleNamespace(String=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'rcl_interfaces.msg', SimpleNamespace(
        Log=SimpleNamespace, SetParametersResult=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.msg', SimpleNamespace(
        SpeechInputStatus=SimpleNamespace, SpeechTranscript=SimpleNamespace,
        SpeechRequest=SimpleNamespace,
        SpeechPlaybackStatus=SimpleNamespace(
            ACCEPTED='accepted', PLAYING='playing', PAUSED='paused',
            FINISHED='finished', FAILED='failed', STOPPED='stopped'),
    ))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.srv', SimpleNamespace(
        ControlWebTalk=SimpleNamespace(Request=SimpleNamespace, Response=SimpleNamespace),
        ControlSpeechSession=SimpleNamespace(Request=SimpleNamespace, Response=SimpleNamespace),
        ClassifySpeechAddressee=SimpleNamespace(
            Request=SimpleNamespace,
            Response=SimpleNamespace(ADDRESSED='a', NOT_ADDRESSED='n', UNKNOWN='u')),
        ControlSpeechPlayback=SimpleNamespace(
            Request=SimpleNamespace(PAUSE='p', RESUME='r', STOP='s', STOP_ALL='sa'),
            Response=SimpleNamespace),
    ))
    monkeypatch.setattr('malbut_stt.silero_stream.SileroStreamVad', _FakeVad)
    monkeypatch.setattr('malbut_stt.wake.LocalWakeRecognizer', SimpleNamespace(
        from_transcriber=lambda t: SimpleNamespace(model=t.model)))
    monkeypatch.setattr('malbut_stt.node.LocalWhisperTranscriber',
                        lambda *a, **kw: SimpleNamespace(model=object()))
    monkeypatch.setattr('malbut_stt.node.SoundDeviceRecorder', _FakeRecorder)
    monkeypatch.setattr('malbut_stt.node.DialoguePipeline', _Pipeline)

    assert main([]) == 0, state.logs
    assert isinstance(state.calls['recorder_obj'], PreprocessedRecorder)
    assert state.calls['recorder_obj'].ns_mode == 'vad_only'
    assert state.calls['recorder_obj'].ns_level == 2
    assert state.calls['webrtc_ns']['ns_level'] == 2
    assert state.calls['webrtc_ns']['echo_cancellation'] is False
    assert state.pipeline_args['wake_min_speech_s'] == 0.18
    assert isinstance(state.pipeline_args['is_speech'].__self__, NoiseAwareVad)
    assert isinstance(state.pipeline_args['wake_is_speech'].__self__, NoiseAwareVad)
    # Live parameter update took effect on both VADs and NoiseAwareVad guards.
    assert state.calls['vads'][0].threshold == 0.72
    assert state.calls['vads'][1].threshold == 0.72
    assert state.pipeline_args['is_speech'].__self__.min_rms_dbfs == -34.0
    assert state.pipeline_args['is_speech'].__self__.snr_margin_db == 10.0
    assert state.pipeline_args['wake_is_speech'].__self__.min_rms_dbfs == -34.0


@pytest.mark.parametrize('param,bad_value', [
    ('vad_threshold', 0.0), ('vad_threshold', 1.0), ('vad_threshold', True),
    ('vad_min_rms_dbfs', -121.0), ('vad_min_rms_dbfs', 1.0), ('vad_min_rms_dbfs', False),
    ('vad_snr_margin_db', -1.0), ('vad_snr_margin_db', 41.0),
    ('vad_sustain_margin_db', -1.0), ('vad_sustain_margin_db', 21.0),
    ('vad_hangover_ms', -1), ('vad_hangover_ms', 1001), ('vad_hangover_ms', 1.5),
    ('high_pass_hz', -1.0), ('high_pass_hz', 501.0),
    ('noise_suppression_mode', 'invalid'),
    ('noise_suppression_level', -1), ('noise_suppression_level', 4),
    ('noise_suppression_level', True),
    ('wake_min_speech_s', 0.0), ('wake_min_speech_s', 0.31),
])
def test_invalid_noise_guard_parameters_fail_before_loading_models(monkeypatch, tmp_path,
                                                                   param, bad_value):
    from malbut_stt.node import main

    model = tmp_path / 'm'
    model.mkdir()
    logs = []
    params = {'wake_model_path': str(model), param: bad_value}

    class _Node:
        def __init__(self, _):
            pass

        def declare_parameter(self, name, default, **_):
            return SimpleNamespace(value=params.get(name, default))

        def get_logger(self):
            return SimpleNamespace(info=lambda m: logs.append(('info', m)),
                                   warning=lambda m: logs.append(('warning', m)),
                                   error=lambda m: logs.append(('error', m)))

        def destroy_node(self):
            pass

    monkeypatch.setitem(sys.modules, 'rclpy', SimpleNamespace(
        init=lambda *_, **__: None, ok=lambda: True, shutdown=lambda: None))
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=_Node))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        QoSProfile=SimpleNamespace, HistoryPolicy=SimpleNamespace,
        ReliabilityPolicy=SimpleNamespace, DurabilityPolicy=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'std_msgs.msg', SimpleNamespace(String=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'rcl_interfaces.msg', SimpleNamespace(
        Log=SimpleNamespace, SetParametersResult=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.msg', SimpleNamespace(
        SpeechInputStatus=SimpleNamespace, SpeechTranscript=SimpleNamespace,
        SpeechRequest=SimpleNamespace, SpeechPlaybackStatus=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.srv', SimpleNamespace(
        ControlWebTalk=SimpleNamespace, ControlSpeechSession=SimpleNamespace,
        ClassifySpeechAddressee=SimpleNamespace, ControlSpeechPlayback=SimpleNamespace))
    monkeypatch.setattr('malbut_stt.node.LocalWhisperTranscriber',
                        lambda *a, **kw: pytest.fail('must not load Whisper'))
    assert main([]) == 1
    assert any('validating_configuration' in m for _, m in logs)


def test_calibrate_venue_noise_cli_verdicts_and_yaml_round_trip(tmp_path, capsys):
    sys.path.insert(0, str(Path(__file__).parents[1] / 'tools'))
    import calibrate_venue_noise

    # 1. Good SNR (~18 dB): verdict == 'ok'.
    amb_ok = tmp_path / 'amb_ok.wav'
    sp_ok = tmp_path / 'sp_ok.wav'
    _write_wav(amb_ok, _noise_pcm(-42.0, 2.0, seed=1))
    _write_wav(sp_ok, _tone_pcm(440.0, -20.0, 2.0))
    out_yaml = tmp_path / 'stt.yaml'
    rc = calibrate_venue_noise.main([
        '--ambient-wav', str(amb_ok), '--speech-wav', str(sp_ok),
        '--write-config', str(out_yaml),
    ])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report['verdict'] == 'ok'
    parsed = yaml.safe_load(out_yaml.read_text(encoding='utf-8'))
    assert parsed['malbut_stt']['ros__parameters']['followup_timeout_s'] == 0.0
    assert parsed['malbut_stt']['ros__parameters']['vad_threshold'] == 0.65

    # 2. Marginal SNR (~8 dB): verdict == 'caution'.
    rec_caution = recommend_venue_settings([-36.0] * 30, [-28.0] * 30)
    assert rec_caution['verdict'] == 'caution'
    assert rec_caution['ros_parameters']['vad_threshold'] == 0.7

    # 3. Terrible SNR (~2 dB): verdict == 'critical' -> recommends close-talk / lavalier mic.
    rec_crit = recommend_venue_settings([-26.0] * 30, [-24.0] * 30)
    assert rec_crit['verdict'] == 'critical'
    assert 'lavalier' in rec_crit['verdict_detail']


def test_pulse_reference_rejects_conflicting_frontend_preprocessing(monkeypatch, tmp_path):
    from malbut_stt.node import main

    model = tmp_path / 'm'
    model.mkdir()
    logs = []
    params = {
        'wake_model_path': str(model),
        'aec_backend': 'pulse_reference',
        'aec_source': 'mic', 'aec_sink': 'spk',
        'aec_model_directory': '/m', 'aec_calibration_audio': '/c.wav',
        'noise_suppression_mode': 'vad_only',
    }
    monkeypatch.setenv('MALBUT_SHARED_MICROPHONE', 'mic')
    monkeypatch.setenv('PULSE_SOURCE', 'mic')
    monkeypatch.setenv('PULSE_SINK', 'spk')

    class _Node:
        def __init__(self, _):
            pass

        def declare_parameter(self, name, default, **_):
            return SimpleNamespace(value=params.get(name, default))

        def get_logger(self):
            return SimpleNamespace(info=lambda m: logs.append(('info', m)),
                                   warning=lambda m: logs.append(('warning', m)),
                                   error=lambda m: logs.append(('error', m)))

        def destroy_node(self):
            pass

    monkeypatch.setitem(sys.modules, 'rclpy', SimpleNamespace(
        init=lambda *_, **__: None, ok=lambda: True, shutdown=lambda: None))
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=_Node))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        QoSProfile=SimpleNamespace, HistoryPolicy=SimpleNamespace,
        ReliabilityPolicy=SimpleNamespace, DurabilityPolicy=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'std_msgs.msg', SimpleNamespace(String=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'rcl_interfaces.msg', SimpleNamespace(
        Log=SimpleNamespace, SetParametersResult=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.msg', SimpleNamespace(
        SpeechInputStatus=SimpleNamespace, SpeechTranscript=SimpleNamespace,
        SpeechRequest=SimpleNamespace, SpeechPlaybackStatus=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.srv', SimpleNamespace(
        ControlWebTalk=SimpleNamespace, ControlSpeechSession=SimpleNamespace,
        ClassifySpeechAddressee=SimpleNamespace, ControlSpeechPlayback=SimpleNamespace))
    assert main([]) == 1
    assert any('validating_configuration' in m for _, m in logs)


def test_missing_pywebrtc_audio_fails_fast_when_ns_enabled(monkeypatch, tmp_path):
    from malbut_stt.node import main

    model = tmp_path / 'm'
    model.mkdir()
    logs = []
    params = {'wake_model_path': str(model), 'noise_suppression_mode': 'vad_only'}

    class _Node:
        def __init__(self, _):
            pass

        def declare_parameter(self, name, default, **_):
            return SimpleNamespace(value=params.get(name, default))

        def get_logger(self):
            return SimpleNamespace(info=lambda m: logs.append(('info', m)),
                                   warning=lambda m: logs.append(('warning', m)),
                                   error=lambda m: logs.append(('error', m)))

        def destroy_node(self):
            pass

    monkeypatch.setitem(sys.modules, 'pywebrtc_audio', None)
    monkeypatch.setitem(sys.modules, 'rclpy', SimpleNamespace(
        init=lambda *_, **__: None, ok=lambda: True, shutdown=lambda: None))
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=_Node))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        QoSProfile=SimpleNamespace, HistoryPolicy=SimpleNamespace,
        ReliabilityPolicy=SimpleNamespace, DurabilityPolicy=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'std_msgs.msg', SimpleNamespace(String=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'rcl_interfaces.msg', SimpleNamespace(
        Log=SimpleNamespace, SetParametersResult=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.msg', SimpleNamespace(
        SpeechInputStatus=SimpleNamespace, SpeechTranscript=SimpleNamespace,
        SpeechRequest=SimpleNamespace, SpeechPlaybackStatus=SimpleNamespace))
    monkeypatch.setitem(sys.modules, 'malbut_interfaces.srv', SimpleNamespace(
        ControlWebTalk=SimpleNamespace, ControlSpeechSession=SimpleNamespace,
        ClassifySpeechAddressee=SimpleNamespace, ControlSpeechPlayback=SimpleNamespace))
    monkeypatch.setattr('malbut_stt.node.LocalWhisperTranscriber',
                        lambda *a, **kw: pytest.fail('must fail before loading Whisper'))
    assert main([]) == 1
    assert any('loading_runtime_dependencies' in m for _, m in logs)


def test_sounddevice_recorder_uses_pulse_for_resolved_pin_mic(monkeypatch):
    from malbut_stt.audio import SoundDeviceRecorder

    opened = {}

    class _FakeSd:
        @staticmethod
        def check_input_settings(**kwargs):
            opened['checked'] = kwargs

        @staticmethod
        def RawInputStream(**kwargs):
            opened['stream'] = kwargs
            return SimpleNamespace(start=lambda: None, stop=lambda: None, close=lambda: None)

    dji = 'alsa_input.usb-DJI_Mic_Mini-00.mono-fallback'
    monkeypatch.setitem(sys.modules, 'sounddevice', _FakeSd)
    monkeypatch.setenv('MALBUT_STT_RESOLVED_PULSE_SOURCE', dji)
    monkeypatch.setenv('PULSE_SOURCE', dji)
    monkeypatch.setenv('MALBUT_SHARED_MICROPHONE', '')
    rec = SoundDeviceRecorder(frame_length=512, device_index=0)
    assert opened['checked']['device'] == 'pulse'
    assert opened['checked']['samplerate'] == 16000
    assert opened['checked']['channels'] == 1
    assert rec.sample_rate == 16000

    # Mismatched PULSE_SOURCE fails closed.
    monkeypatch.setenv('PULSE_SOURCE', 'wrong-source')
    with pytest.raises(RuntimeError, match='does not match PULSE_SOURCE'):
        SoundDeviceRecorder(frame_length=512, device_index=0)


def test_setup_pin_mic_cli_detects_dji_warns_speaker_hijack_and_writes_yaml(
        monkeypatch, tmp_path, capsys):
    sys.path.insert(0, str(Path(__file__).parents[1] / 'tools'))
    import setup_pin_mic

    dji_source = 'alsa_input.usb-DJI_Mic_Mini-00.mono-fallback'
    sources = [
        {'name': 'alsa_input.xfm', 'properties': {'alsa.card_name': 'XFM-DP-V0.0.18'}},
        {'name': dji_source, 'properties': {'device.product.name': 'DJI Mic Mini'}},
    ]
    sinks = [
        {'name': 'alsa_output.usb-GeneralPlus_Speaker.analog-stereo'},
        {'name': 'alsa_output.usb-DJI_Mic_Mini.analog-stereo'},
    ]
    monkeypatch.setattr(setup_pin_mic, '_list_pulse',
                        lambda kind, _env: sources if kind == 'sources' else sinks)
    monkeypatch.setattr(setup_pin_mic, '_get_default_sink',
                        lambda _env: 'alsa_output.usb-DJI_Mic_Mini.analog-stereo')

    cfg = tmp_path / 'stt.yaml'
    rc = setup_pin_mic.main([
        '--match', 'DJI', '--skip-probe', '--apply', '--config-path', str(cfg),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'ok'
    assert out['resolved_source'] == dji_source
    assert out['speaker_warning'] is not None
    assert 'GeneralPlus_Speaker' in out['speaker_warning']['fix_command']
    parsed = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    assert parsed['/**']['ros__parameters']['input_pulse_source'] == 'match:DJI'
    assert parsed['/**']['ros__parameters']['aec_backend'] == 'none'
    assert parsed['/**']['ros__parameters']['wake_input_gain'] == 1.0

    # Revert removes the override cleanly when there was no pre-existing config.
    rc_rev = setup_pin_mic.main(['--revert', '--config-path', str(cfg)])
    assert rc_rev == 0
    capsys.readouterr()
    assert not cfg.exists()

    # Existing robot stt.yaml is backed up, tuned params are preserved, and --revert restores it.
    original = (
        '/**:\n  ros__parameters:\n'
        '    aec_backend: pulse_reference\n'
        '    aec_source: xfm\n'
        '    aec_sink: spk\n'
        '    turn_detection: adaptive_fused\n'
        '    wake_input_gain: 4.0\n'
    )
    cfg.write_text(original, encoding='utf-8')
    muted_sources = [
        {'name': dji_source, 'mute': True, 'properties': {'device.product.name': 'DJI Mic Mini'}},
    ]
    unmute_calls = []
    monkeypatch.setattr(setup_pin_mic, '_list_pulse',
                        lambda kind, _env: muted_sources if kind == 'sources' else [{'name': 'auto_null'}])
    monkeypatch.setattr(setup_pin_mic, '_get_default_sink', lambda _env: 'auto_null')
    monkeypatch.setattr(setup_pin_mic, '_unmute_source',
                        lambda name, _env: unmute_calls.append(name) or True)
    rc2 = setup_pin_mic.main(['--match', 'DJI', '--skip-probe', '--apply', '--config-path', str(cfg)])
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2['unmuted'] is True
    assert unmute_calls == [dji_source]
    assert 'auto_null' in out2['speaker_warning']['message']
    merged = yaml.safe_load(cfg.read_text(encoding='utf-8'))['/**']['ros__parameters']
    assert merged['turn_detection'] == 'adaptive_fused'
    assert merged['input_pulse_source'] == 'match:DJI'
    assert merged['aec_backend'] == 'none'
    assert merged['wake_input_gain'] == 1.0
    assert 'aec_source' not in merged
    assert setup_pin_mic.main(['--revert', '--config-path', str(cfg)]) == 0
    assert cfg.read_text(encoding='utf-8') == original
