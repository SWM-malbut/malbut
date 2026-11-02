"""Bound preview work without delaying endpoints or losing unconfirmed speech."""

from queue import Empty
from types import SimpleNamespace

import pytest

from malbut_stt.audio import CaptureSettings
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.transcription import LocalWhisperTranscriber
from test_rolling_dialogue import PcmTokenModel, QUIET, token_pcm, token_text, retained_pcm_sizes
from test_partial_dialogue import pump, state as threaded_state  # noqa: F401


class Simulation:
    """Run the real capture/incremental state machine with measured-cost fixtures."""

    def __init__(self, *, adaptive=True, max_buffer_s=60., interval=2.):
        self.now = 0.
        self.final, self.controls, self.candidates, self.reports, self.kinds = [], [], [], [], []
        self.model = PcmTokenModel()
        transcriber = LocalWhisperTranscriber.__new__(LocalWhisperTranscriber)
        transcriber.model = self.model
        self.pipeline = DialoguePipeline(
            recorder_factory=lambda: None, wake=None, transcriber=transcriber,
            is_speech=lambda pcm, _: any(pcm), input_has_aec=True,
            publish_transcript=lambda *args: self.final.append(args),
            publish_control=lambda *args: self.controls.append(args),
            publish_interruption=lambda *args: self.candidates.append(args),
            report=self.reports.append, clock=lambda: self.now,
            settings=CaptureSettings(max_utterance_s=None, max_buffer_s=max_buffer_s,
                                     silence_timeout_s=2.),
            endpoint_predecode_s=.8, partial_interval_s=interval, adaptive_partials=adaptive)
        self.pipeline.session.activate()

    def feed(self, pcm):
        self.now += len(pcm) / 32000
        self.pipeline.feed(pcm)

    def resolve(self, job, seconds=2.):
        kind, generation, token, payload = job
        self.kinds.append(kind)
        text = payload.stream.transcribe(
            payload.pcm, 16000, final=payload.final, speech_end_s=payload.speech_end_s,
            audio_start_s=payload.audio_start_s)
        result = (kind, generation, token, text, None, payload.stream.retained_start_s)
        if kind in ('partial', 'endpoint'):
            result += (None, seconds)
        self.pipeline.results.put_nowait(result)
        self.pipeline.poll()

    def settle(self, seconds=2.):
        for _ in range(20):
            try:
                job = self.pipeline.jobs.get_nowait()
            except Empty:
                return
            self.resolve(job, seconds)
        raise AssertionError('preview work did not settle')


def test_slow_preview_expands_interval_and_fast_decodes_restore_it_gradually():
    sim = Simulation()
    p = sim.pipeline
    sim.feed(token_pcm(1) + token_pcm(2))
    sim.settle(2.5)
    assert p.command_stream.partial_interval_s == 4.
    sim.feed(token_pcm(3) + token_pcm(4))
    assert p.jobs.empty()
    sim.feed(token_pcm(5) + token_pcm(6))
    sim.settle(.1)
    assert 2 < p.command_stream.partial_interval_s < 4
    for value in range(7, 31):
        sim.feed(token_pcm(value))
        sim.settle(.1)
        assert 2 <= p.command_stream.partial_interval_s <= 4
    assert p.command_stream.partial_interval_s == 2.


def test_worker_reports_decode_duration_without_ros_poll_or_queue_delay(threaded_state, monkeypatch):
    from malbut_stt import dialogue_pipeline

    ticks = iter([100., 102.5])
    monkeypatch.setattr(dialogue_pipeline, 'perf_counter', lambda: next(ticks))
    p = threaded_state.pipeline
    p.feed(token_pcm(1) + token_pcm(2))
    pump(p, lambda: bool(threaded_state.partial))
    assert p._partial_cost_s == 2.5
    assert p.command_stream.partial_interval_s == 4.


@pytest.mark.parametrize('adaptive,seconds,expected', [(True, .05, 2.), (False, 3., 2.)])
def test_fast_or_explicitly_fixed_previews_keep_the_existing_cadence(adaptive, seconds, expected):
    sim = Simulation(adaptive=adaptive)
    for value in range(1, 13):
        sim.feed(token_pcm(value))
        sim.settle(seconds)
    assert sim.pipeline.command_stream.partial_interval_s == expected
    assert sim.kinds == ['partial'] * 6


def test_pending_partial_waits_for_new_interval_but_endpoint_bypasses_it():
    sim = Simulation()
    p = sim.pipeline
    sim.feed(token_pcm(1) + token_pcm(2))
    held = p.jobs.get_nowait()
    sim.feed(token_pcm(3) + token_pcm(4))
    assert p._endpoint_candidate is not None and p.jobs.empty()
    sim.resolve(held, 3.)
    assert p.jobs.empty()  # The older 2-second candidate must not defeat adaptation.
    sim.feed(QUIET * 40)
    endpoint = p.jobs.get_nowait()
    assert endpoint[0] == 'endpoint'
    sim.resolve(endpoint, .1)
    sim.feed(QUIET * 60)
    sim.settle()
    assert len(sim.final) == 1
    assert sim.final[0][1] == ' '.join(token_text(i) for i in range(1, 5))
    assert sim.kinds == ['partial', 'endpoint']  # Completed endpoint is reused.


def test_final_audio_has_priority_over_waiting_partial_and_keeps_the_tail():
    sim = Simulation()
    p = sim.pipeline
    sim.feed(token_pcm(1) + token_pcm(2))
    held = p.jobs.get_nowait()
    sim.feed(token_pcm(3) + token_pcm(4) + QUIET * 100)
    assert p.jobs.queue[0][0] == 'command'
    sim.resolve(held, 3.)
    sim.settle()
    assert sim.kinds == ['partial', 'command']
    assert len(sim.final) == 1
    assert sim.final[0][1] == ' '.join(token_text(i) for i in range(1, 5))


@pytest.mark.parametrize('adaptive', [False, True])
def test_two_minutes_keep_every_word_and_bound_pcm_under_slow_inference(adaptive):
    sim = Simulation(adaptive=adaptive)
    peak = 0
    for value in range(1, 121):
        sim.feed(token_pcm(value))
        peak = max(peak, *retained_pcm_sizes(sim.pipeline))
        sim.settle(2.1)
        assert not sim.final
    sim.feed(QUIET * 100)
    sim.settle()
    assert len(sim.final) == 1
    assert sim.final[0][1] == ' '.join(token_text(i) for i in range(1, 121))
    assert peak <= 60 * 32000
    assert sim.kinds.count('partial') == (30 if adaptive else 60)
    assert not any('buffer_overflow' in event for event in sim.reports)


def test_buffer_pressure_restores_prompt_preview_without_discarding_audio():
    sim = Simulation(max_buffer_s=10.)
    p = sim.pipeline
    for value in range(1, 7):
        sim.feed(token_pcm(value))
        sim.settle(2.)
    before = bytes(p.command_stream.collector.audio)
    p._refresh_partial_interval()
    assert p.command_stream.partial_interval_s == 2.
    assert bytes(p.command_stream.collector.audio) == before
    assert p.command_stream.collector.audio_start_s == 0.


def test_canceled_session_resets_cadence_and_rejects_late_cost_and_text():
    sim = Simulation()
    p = sim.pipeline
    sim.feed(token_pcm(1) + token_pcm(2))
    sim.settle(3.)
    sim.feed(token_pcm(3) + token_pcm(4) + token_pcm(5) + token_pcm(6))
    held = p.jobs.get_nowait()
    p._terminate('test_session_change')
    p.session.activate()
    assert p.command_stream.partial_interval_s == 2.
    sim.resolve(held, 30.)
    assert p._partial_cost_s is None and p.command_stream.partial_interval_s == 2.
    sim.feed(token_pcm(101) + token_pcm(102) + QUIET * 100)
    sim.settle(.1)
    assert len(sim.final) == 1
    assert sim.final[0][1] == ' '.join(token_text(i) for i in (101, 102))


def test_interruption_waits_for_final_text_before_addressee_decision():
    sim = Simulation()
    p = sim.pipeline
    p.on_playback_status('answer', 'playing')
    sim.feed(token_pcm(1) + token_pcm(2))
    sim.settle(3.)
    assert sim.controls == [('answer', 'pause')]
    assert not sim.candidates and not sim.final
    p.on_playback_status('answer', 'paused')
    sim.feed(token_pcm(3) + QUIET * 100)
    sim.settle()
    uid, playback, text = sim.candidates[0]
    p.on_addressee(uid, playback, 'addressed')
    assert sim.final == [(uid, text)]
    assert text == ' '.join(token_text(i) for i in range(1, 4))
    assert sim.controls[-1] == ('answer', 'stop')


@pytest.mark.parametrize('interval', [None, .04, 8.])
def test_explicit_custom_or_disabled_preview_interval_is_preserved(interval):
    sim = Simulation(interval=interval)
    sim.pipeline._observe_partial_cost(30.)
    assert sim.pipeline.command_stream.partial_interval_s == interval
