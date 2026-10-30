"""Verify hybrid onset, causal semantic guards and unchanged fallback behavior."""
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from malbut_stt.audio import CaptureSettings, UtteranceCollector
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.streaming import StreamingUtteranceCollector
from malbut_stt.turn_detection import ReachyStartFilter, SmartTurnGuard, load_guards

VOICE = b'\x01\x00' * 320
QUIET = bytes(640)


class Model:
    def __init__(self, probability=.9):
        self.value = probability
        self.resets = 0

    def reset(self):
        self.resets += 1

    def probability(self, pcm):
        assert len(pcm) == 1024
        return self.value


def test_reachy_qualifies_after_160_ms_and_preserves_onset_pcm():
    gate = ReachyStartFilter(model=Model())
    stream = StreamingUtteranceCollector(lambda *_: True, start_qualifier=gate)
    for _ in range(7):
        assert stream.feed(VOICE) == []
    event, = stream.feed(VOICE)
    assert event.status == 'speech_started'
    assert bytes(stream.collector.audio) == VOICE*8
    assert stream.collector.voiced_frames == 8


def test_reset_cannot_reuse_previous_onset_evidence():
    gate = ReachyStartFilter(model=Model())
    stream = StreamingUtteranceCollector(lambda *_: True, start_qualifier=gate)
    stream.feed(VOICE*8)
    stream.reset()
    assert stream.feed(VOICE*7) == []
    assert not stream.collector.started


def test_reachy_state_releases_after_400_ms_without_changing_endpoint_timeout():
    model = Model()
    gate = ReachyStartFilter(model=model)
    for _ in range(8):
        gate(VOICE,16000)
    model.value = .1
    for _ in range(20):
        gate(QUIET,16000)
    assert gate.confirmed  # Twelve complete 32 ms low frames, pending 16 ms.
    assert not gate(QUIET,16000)


def test_continuous_webrtc_noise_has_bounded_candidate_and_idle_timeout():
    gate = ReachyStartFilter(model=Model(.1))
    collector = UtteranceCollector(16000,lambda *_: True,CaptureSettings(),
                                  start_qualifier=gate)
    for _ in range(300):
        result = collector.feed(VOICE)
        assert len(collector.candidate_audio) <= 640*30
        if result is not None:
            break
    assert result.status == 'no_speech'
    assert not collector.started


def test_legacy_does_not_need_models(tmp_path):
    assert load_guards('legacy',tmp_path)==(None,None)
    with pytest.raises(ValueError,match='legacy or hybrid'):
        load_guards('mistyped',tmp_path)


def test_hybrid_loads_smart_without_a_second_onset_filter(monkeypatch, tmp_path):
    from malbut_stt import turn_detection
    smart = object()
    monkeypatch.setattr(turn_detection, 'SmartTurnGuard', lambda _: smart)
    def forbidden(*_):
        raise AssertionError('capture Silero already owns onset detection')
    monkeypatch.setattr(turn_detection, 'ReachyStartFilter', forbidden)
    assert load_guards('hybrid', tmp_path) == (None, smart)


def test_reachy_start_loads_only_silero_and_never_smart(monkeypatch, tmp_path):
    from malbut_stt import turn_detection
    onset = object()
    paths = []
    def load(path):
        paths.append(path)
        return onset
    def forbidden(*_):
        raise AssertionError('reachy_start must not load Smart Turn')
    monkeypatch.setattr(turn_detection, 'ReachyStartFilter', load)
    monkeypatch.setattr(turn_detection, 'SmartTurnGuard', forbidden)
    assert load_guards('reachy_start', tmp_path) == (onset, None)
    assert paths == [tmp_path/'silero-v6.2.3.onnx']


def test_smart_model_checksum_is_checked_before_runtime_import(tmp_path):
    path=tmp_path/'fake.onnx';path.write_bytes(b'invalid')
    with pytest.raises(ValueError,match='checksum'):
        SmartTurnGuard(path)


def test_smart_rejects_empty_and_partial_samples_without_running_model():
    guard=SmartTurnGuard.__new__(SmartTurnGuard)
    for pcm in (b'',b'\0'):
        with pytest.raises(ValueError):
            guard.check(pcm)


def pipeline(guard):
    state=SimpleNamespace(now=0.,captured=[],logs=[])
    p=DialoguePipeline(recorder_factory=lambda:None,wake=None,
        transcriber=SimpleNamespace(transcribe=lambda *_:'알겠습니다.'),
        is_speech=lambda frame,_:any(frame),publish_transcript=lambda *_:None,
        publish_control=lambda *_:None,publish_interruption=lambda *_:None,
        report=state.logs.append,clock=lambda:state.now,input_has_aec=True,
        settings=CaptureSettings(silence_timeout_s=2.,min_speech_s=.02),
        endpoint_predecode_s=.8,smart_turn=guard)
    p.session.activate()
    p._complete_capture=lambda _,event:state.captured.append(event)
    return p,state


@pytest.mark.parametrize('complete',[True,False,None])
def test_guard_controls_only_early_endpoint_and_never_two_second_fallback(complete):
    p,state=pipeline(object())
    p.feed(VOICE);p.feed(QUIET*40)
    kind,generation,token,payload=p.jobs.get_nowait()
    assert kind=='endpoint'
    # Smart Turn sees observed PCM, while the legacy non-streaming ASR still
    # receives its original 2 s zero-padding for compatible transcription.
    assert payload.endpoint_pcm==VOICE+QUIET*40
    assert payload==VOICE+QUIET*100
    p.results.put_nowait((kind,generation,token,'알겠습니다.',None,0.,complete))
    p.poll();assert not state.captured
    p.feed(QUIET*10);p.poll()
    if complete:
        assert state.captured[0].silence_s==1.
    else:
        assert not state.captured
        p.feed(QUIET*50);p.poll()
        assert state.captured[0].silence_s==2.


def test_old_smart_result_cannot_finish_resumed_voice():
    p,state=pipeline(object())
    p.feed(VOICE);p.feed(QUIET*40)
    kind,generation,token,_=p.jobs.get_nowait()
    p.feed(VOICE)
    p.results.put_nowait((kind,generation,token,'알겠습니다.',None,0.,True))
    p.poll();p.feed(QUIET*50);p.poll()
    assert not state.captured


def test_smart_complete_cannot_override_incomplete_korean_text():
    p,state=pipeline(object())
    p.feed(VOICE);p.feed(QUIET*40)
    kind,generation,token,_=p.jobs.get_nowait()
    p.results.put_nowait((kind,generation,token,'비가 오면',None,0.,True))
    p.poll();p.feed(QUIET*10);p.poll()
    assert not state.captured


@pytest.mark.parametrize('fail',[False,True])
def test_worker_uses_observed_audio_and_guard_failure_preserves_asr(fail):
    seen=[]
    def check(pcm):
        seen.append(pcm)
        if fail:
            raise RuntimeError('model unavailable')
        return SimpleNamespace(complete=False,probability=.2,inference_ms=10.)
    p,state=pipeline(SimpleNamespace(check=check))
    p.feed(VOICE);p.feed(QUIET*40)
    worker=Thread(target=p._infer,daemon=True);worker.start()
    try:
        result=p.results.get(timeout=2.)
        assert result[3:5]==('알겠습니다.',None)
        assert result[-1] is (None if fail else False)
        assert seen==[VOICE+QUIET*40]
    finally:
        p.stopping.set();worker.join(timeout=2.)
        assert not worker.is_alive()
