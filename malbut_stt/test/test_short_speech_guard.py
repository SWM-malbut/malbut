"""Exercise recorded VAD patterns through capture, inference and publication.

These fixtures reproduce decisions/sample counts, not the unavailable robot WAV.
The model is deterministic; no microphone, native decoder or ROS is required.
"""

import json
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from malbut_stt.audio import CaptureSettings, UtteranceCollector
from malbut_stt.dialogue_pipeline import DialoguePipeline
from malbut_stt.incident import IncidentRecorder


VOICE = b'\x01\x00' * 320
QUIET = bytes(640)
LONG_TEXT = '구독과 좋아요 부탁드려요!'


def pump(pipeline, condition):
    deadline = monotonic() + 3
    while not condition():
        pipeline.poll()
        assert monotonic() < deadline
        sleep(.001)


@pytest.fixture
def create(tmp_path):
    states = []
    recorders = []

    def factory(*, streaming=True, text=LONG_TEXT, **options):
        # Test the experimental guard explicitly; production defaults leave it off.
        options.setdefault('short_speech_guard', True)
        state = SimpleNamespace(now=0., transcripts=[], partials=[], calls=[], reports=[])
        recorder = IncidentRecorder(tmp_path / str(len(states)), save_audio=False, post_seconds=60)
        recorders.append(recorder)

        def decode(pcm, rate, **kwargs):
            state.calls.append((pcm, kwargs))
            return text

        transcriber = (SimpleNamespace(create_stream=lambda: SimpleNamespace(transcribe=decode))
                       if streaming else SimpleNamespace(transcribe=decode))
        p = DialoguePipeline(
            recorder_factory=lambda: None, wake=None, transcriber=transcriber,
            is_speech=lambda frame, _: frame == VOICE,
            publish_transcript=lambda *args: state.transcripts.append(args),
            publish_control=lambda *_: None, publish_interruption=lambda *_: None,
            on_partial=lambda *args: state.partials.append(args),
            report=state.reports.append, clock=lambda: state.now,
            on_wake=lambda: None, on_failure=lambda: None,
            diagnostics=recorder, endpoint_predecode_s=.8, **options,
        )
        p.session.activate()
        p.asr_thread = Thread(target=p._infer, daemon=True)
        p.asr_thread.start()
        state.pipeline, state.recorder = p, recorder
        states.append(state)
        return state

    yield factory
    for state in states:
        state.pipeline.close()
    for recorder in recorders:
        recorder.close()


@pytest.mark.parametrize('streaming', [False, True])
def test_recorded_short_burst_cannot_publish_a_long_transcript(create, streaming):
    state = create(streaming=streaming)
    p = state.pipeline
    # 2026-10-12 incident: 14 voiced frames, then 40 quiet frames (1.08 s input).
    p.feed(VOICE * 14 + QUIET * 40)
    pump(p, lambda: 'partial_failed' in state.reports)
    assert not state.transcripts and not state.partials
    assert p._endpoint_result[2] == 'InsufficientSpeechEvidence'
    # More endpoint silence cannot count as additional speech evidence.
    p.feed(QUIET * 60)
    pump(p, lambda: 'transcription_failed:InsufficientSpeechEvidence' in state.reports)
    assert not state.transcripts and not state.partials and not p.session.active
    assert len(state.calls) == 2  # Final retry must pass the same guard.
    state.recorder.close()
    path = state.recorder.last_incident
    manifest = json.loads((path / 'incident.json').read_text())
    assert manifest['triggers'][0]['event'] == 'recognition_rejected'
    events = [json.loads(line) for line in (path / 'events.jsonl').read_text().splitlines()]
    rejected = [e for e in events if e['event'] == 'recognition_rejected']
    assert len(rejected) == 2 and all(e['voiced_s'] == .28 for e in rejected)
    assert not any(e['event'] == 'transcript_published' for e in events)
    trace = json.loads((path / 'transactions.json').read_text())
    assert trace['transactions'][0]['observed']['stt_published'] is False
    assert not list(path.rglob('*.wav'))


def test_sparse_voiced_frames_do_not_become_two_seconds_of_speech(create):
    state = create(text='구독과 좋아요는 저에게 아주 큰 힘이 됩니다.')
    p = state.pipeline
    # Second occurrence: a 300 ms burst, quiet gap, then a 120 ms burst.
    p.feed(VOICE * 15 + QUIET * 83 + VOICE * 6)
    pump(p, lambda: 'partial_failed' in state.reports)
    assert not state.partials and not state.transcripts
    assert state.calls[0][1]['final'] is False


@pytest.mark.parametrize('text', ['네', '아니요', '감사합니다.'])
@pytest.mark.parametrize('streaming', [False, True])
def test_short_real_replies_still_reach_final_publication(create, text, streaming):
    state = create(text=text, streaming=streaming)
    state.pipeline.feed(VOICE * 4 + QUIET * 100)
    pump(state.pipeline, lambda: bool(state.transcripts))
    assert state.transcripts[0][1] == text


def test_matching_phrase_with_sufficient_speech_is_not_blacklisted(create):
    state = create()
    state.pipeline.feed(VOICE * 40 + QUIET * 100)
    pump(state.pipeline, lambda: bool(state.transcripts))
    assert state.transcripts[0][1] == LONG_TEXT


def test_guard_can_be_disabled_for_controlled_comparison(create):
    state = create(short_speech_guard=False)
    state.pipeline.feed(VOICE * 14 + QUIET * 100)
    pump(state.pipeline, lambda: bool(state.transcripts))
    assert state.transcripts[0][1] == LONG_TEXT


@pytest.mark.parametrize('aec', [False, True])
def test_configured_tail_discards_frames_only_inside_its_window(create, aec):
    state = create(text='네', input_has_aec=aec, chime_tail_s=.8)
    p = state.pipeline
    p._accept_result('wake', p._generation, None, '제이크', None)
    # Synthetic timing checks the parameter, not the cause of the robot incident.
    for index in range(14):
        state.now = .344 + index * .02
        p.feed(VOICE)
    assert not p.command_stream.collector.started
    assert not state.calls and not state.transcripts
    state.now = .801
    p.feed(VOICE * 4 + QUIET * 100)
    pump(p, lambda: bool(state.transcripts))
    assert state.transcripts[0][1] == '네'
    assert VOICE * 4 in state.calls[0][0]
    assert VOICE * 5 not in state.calls[0][0]


def test_voiced_duration_excludes_preroll_and_silence_and_survives_audio_release():
    c = UtteranceCollector(16000, lambda frame, _: frame == VOICE, CaptureSettings())
    c.feed(QUIET * 15 + VOICE * 4 + QUIET * 10 + VOICE * 3)
    assert c.snapshot('endpoint_check').voiced_s == pytest.approx(.14)
    c.discard_before(.3)
    c.feed(QUIET * 5 + VOICE * 2)
    assert c.snapshot('partial_check').voiced_s == pytest.approx(.18)


@pytest.mark.parametrize('kind', ['wake', 'endpoint', 'failure'])
def test_default_tail_applies_to_each_local_chime_and_preserves_wake_wait(create, kind):
    state = create()
    p = state.pipeline
    def sound():
        state.now += .18
    if kind == 'wake':
        p.on_wake = sound
        p._accept_result('wake', p._generation, None, '제이크', None)
        assert p._command_start_deadline == pytest.approx(5.48)
    elif kind == 'endpoint':
        p.on_endpoint = sound
        p._play_endpoint_chime()
    else:
        p.on_failure = sound
        p._fail_command('empty_transcript')
    assert p._chime_gate_until == pytest.approx(.48)
    assert p._input_blocked(.479)
    assert not p._input_blocked(.481)


@pytest.mark.parametrize('value', [-1, 3.1, True, float('nan'), float('inf'), '0.8'])
def test_invalid_chime_tail_fails_before_capture(create, value):
    with pytest.raises(ValueError, match='chime_tail_s'):
        create(chime_tail_s=value)
