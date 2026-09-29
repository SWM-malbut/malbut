"""Reject brief VAD noise before it can reach dialogue, ASR, or playback control."""

from queue import Empty
from types import SimpleNamespace

import pytest

from malbut_stt.dialogue_pipeline import DialoguePipeline


VOICE = b'\x01\x00' * 320  # One 20 ms frame at 16 kHz.
QUIET = bytes(640)


@pytest.fixture
def dialogue():
    """Run captured PCM and queued fake inference on one deterministic owner."""
    pipelines = []

    def create(*, awake=True, text='감사합니다.', aec=True):
        run = SimpleNamespace(
            now=0.0, transcripts=[], statuses=[], controls=[], interruptions=[],
            calls=[], reports=[], endpoint_chimes=[], wake_chimes=[],
        )

        def transcribe(kind, pcm, rate):
            assert rate == 16000 and isinstance(pcm, bytes)
            run.calls.append((kind, pcm))
            return '제이크야' if kind == 'wake' else text

        def recorder_factory():
            raise AssertionError('fake-audio tests must not open a microphone')

        pipeline = DialoguePipeline(
            recorder_factory=recorder_factory,
            wake=SimpleNamespace(transcribe=lambda pcm, rate: transcribe('wake', pcm, rate)),
            transcriber=SimpleNamespace(
                transcribe=lambda pcm, rate: transcribe('command', pcm, rate)),
            is_speech=lambda frame, rate: frame == VOICE,
            publish_transcript=lambda uid, value: run.transcripts.append((uid, value)),
            publish_control=lambda pid, command: run.controls.append((pid, command)),
            publish_interruption=lambda *args: run.interruptions.append(args),
            publish_input_status=lambda *args: run.statuses.append(args),
            on_endpoint=lambda: run.endpoint_chimes.append('endpoint'),
            on_wake=lambda: run.wake_chimes.append('wake'),
            report=run.reports.append, clock=lambda: run.now, input_has_aec=aec,
        )
        pipelines.append(pipeline)
        if awake:
            pipeline.session.activate()

        def settle():
            # Preserve the real worker's job/result boundary without background
            # scheduling: only the local inference engines are replaced.
            pipeline.poll()
            for _ in range(10):
                try:
                    kind, generation, uid, pcm = pipeline.jobs.get_nowait()
                except Empty:
                    return
                engine = pipeline.wake if kind == 'wake' else pipeline.transcriber
                value = engine.transcribe(pcm, 16000)
                pipeline.results.put_nowait((kind, generation, uid, value, None, 0.0))
                pipeline.poll()
            raise AssertionError('inference did not settle after bounded fake results')

        def feed(pcm, *, busy_at_capture=False):
            run.now += len(pcm) / 32000
            pipeline.feed(pcm, busy_at_capture=busy_at_capture)
            settle()

        run.pipeline, run.feed = pipeline, feed
        return run

    yield create
    for pipeline in pipelines:
        pipeline.close()


@pytest.mark.parametrize('awake', [False, True], ids=['wake-listening', 'dialogue'])
@pytest.mark.parametrize('text', ['감사합니다.', ''], ids=['plausible-asr', 'empty-asr'])
def test_isolated_vad_blips_never_reach_asr_or_create_dialogue_events(dialogue, awake, text):
    """Five brief false positives cannot become five answers or retry notices."""
    run = dialogue(awake=awake, text=text)

    for _ in range(5):
        run.feed(VOICE)
        run.feed(QUIET * 100)

    assert run.calls == []
    assert run.transcripts == run.statuses == run.controls == run.interruptions == []
    assert run.endpoint_chimes == run.wake_chimes == []
    assert run.pipeline.session.active is awake
    assert run.pipeline.session.utterance_id is None
    assert run.pipeline.jobs.empty() and run.pipeline.results.empty()


def test_short_confirmation_survives_the_default_onset_guard(dialogue):
    """Four voiced frames preserve a real short answer such as '네'."""
    run = dialogue(text='네')
    assert run.pipeline.start_session('fall-confirmation')
    run.feed(VOICE * 3)
    assert run.statuses == [] and run.pipeline.session.utterance_id is None

    run.feed(VOICE)
    uid = run.pipeline.session.utterance_id
    assert uid
    assert run.statuses == [('fall-confirmation', uid, 'started')]
    run.feed(QUIET * 100)

    assert run.transcripts == [(uid, '네')]
    assert [kind for kind, _ in run.calls] == ['command']
    assert VOICE * 4 in run.calls[0][1]
    assert run.statuses == [('fall-confirmation', uid, 'started')]
    assert run.endpoint_chimes == ['endpoint']


def test_separate_real_identical_utterances_are_each_accepted(dialogue):
    """The guard checks audio onset, never a blacklist or repeated text."""
    run = dialogue(text='감사합니다.')
    for _ in range(2):
        run.feed(QUIET * 16)  # Let the previous endpoint chime drain.
        run.feed(VOICE * 4)
        run.feed(QUIET * 100)

    assert [text for _, text in run.transcripts] == ['감사합니다.', '감사합니다.']
    ids = [uid for uid, _ in run.transcripts]
    assert len(set(ids)) == 2
    assert run.statuses == [('', uid, 'started') for uid in ids]
    assert [kind for kind, _ in run.calls] == ['command', 'command']
    assert run.endpoint_chimes == ['endpoint', 'endpoint']


def test_quiet_gap_requires_a_new_consecutive_onset(dialogue):
    """Separate 60 ms bursts cannot pool their voiced frames across silence."""
    run = dialogue()
    run.feed(VOICE * 3)
    run.feed(QUIET)
    run.feed(VOICE * 3)
    assert run.statuses == run.calls == []
    assert run.pipeline.session.utterance_id is None

    run.feed(VOICE)
    uid = run.pipeline.session.utterance_id
    assert uid and run.statuses == [('', uid, 'started')]
    run.feed(QUIET * 100)
    assert run.transcripts == [(uid, '감사합니다.')]
    assert len(run.calls) == 1


def test_raw_tts_playback_and_tail_cannot_satisfy_the_onset_guard(dialogue):
    """Without AEC, playback gating also drops any earlier onset candidate."""
    run = dialogue(aec=False)
    run.feed(VOICE * 3)
    run.pipeline.on_playback_status('reply', 'playing')
    run.feed(VOICE * 4 + QUIET * 100)
    assert run.calls == run.statuses == run.controls == []

    run.pipeline.on_playback_status('reply', 'finished')
    run.feed(VOICE * 4)  # The 300 ms playback tail is still excluded.
    run.feed(QUIET * 16)
    run.feed(VOICE * 3)
    assert run.calls == run.statuses == []
    assert run.pipeline.session.utterance_id is None

    run.feed(VOICE)
    uid = run.pipeline.session.utterance_id
    assert uid and run.statuses == [('', uid, 'started')]
    run.feed(QUIET * 100)
    assert run.transcripts == [(uid, '감사합니다.')]
    assert run.controls == []
    assert run.endpoint_chimes == ['endpoint']


def test_brief_aec_input_does_not_pause_an_existing_answer(dialogue):
    """Even with AEC, an isolated VAD hit is not a valid interruption."""
    run = dialogue(aec=True)
    run.pipeline.on_playback_status('reply', 'playing')
    for _ in range(5):
        run.feed(VOICE)
        run.feed(QUIET * 100)
    assert run.calls == run.statuses == run.controls == run.interruptions == []
    assert run.transcripts == run.endpoint_chimes == []

    run.feed(VOICE * 4)
    assert run.controls == [('reply', 'pause')]
    assert run.statuses == [('', run.pipeline.session.utterance_id, 'started')]


@pytest.mark.parametrize('busy_source', ['capture', 'inference'])
def test_busy_onset_cannot_become_a_new_turn_when_inference_finishes(dialogue, busy_source):
    """Eligibility belongs to the whole onset, not just its fourth frame."""
    run = dialogue()
    if busy_source == 'inference':
        run.pipeline._busy = True
    run.feed(VOICE, busy_at_capture=busy_source == 'capture')
    run.pipeline._busy = False
    run.feed(VOICE * 3)
    run.feed(QUIET * 100)

    assert run.calls == run.transcripts == run.statuses == []
    assert run.pipeline.session.utterance_id is None
    assert run.endpoint_chimes == []

    run.feed(VOICE * 4)
    uid = run.pipeline.session.utterance_id
    assert uid and run.statuses == [('', uid, 'started')]
    run.feed(QUIET * 100)
    assert run.transcripts == [(uid, '감사합니다.')]
    assert len(run.calls) == 1


def test_quiet_resets_a_busy_candidate_before_a_fresh_valid_onset(dialogue):
    """A rejected noisy candidate must not poison the next real utterance."""
    run = dialogue()
    run.feed(VOICE, busy_at_capture=True)
    run.feed(QUIET)
    assert run.calls == run.statuses == []

    run.feed(VOICE * 4)
    uid = run.pipeline.session.utterance_id
    assert uid and run.statuses == [('', uid, 'started')]
    run.feed(QUIET * 100)
    assert run.transcripts == [(uid, '감사합니다.')]
    assert len(run.calls) == 1
