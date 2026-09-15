"""Join continuous PCM boundaries to dialogue events, without hardware or AEC."""

from types import SimpleNamespace

from malbut_stt.conversation import ConversationSession
from malbut_stt.streaming import StreamingUtteranceCollector


QUIET = bytes(640)
VOICE = b'\x01\x00' * 320


def runtime():
    run = SimpleNamespace(now=0.0, controls=[], transcripts=[], completed=[])
    run.session = ConversationSession(
        clock=lambda: run.now,
        publish_transcript=lambda uid, text: run.transcripts.append((uid, text)),
        publish_control=lambda pid, command: run.controls.append((pid, command)),
    )
    # Synthetic input already represents user speech. This is not an AEC test.
    stream = StreamingUtteranceCollector(lambda frame, _: any(frame))

    def feed(frame, count=1):
        for _ in range(count):
            run.now += 0.02
            for event in stream.feed(frame):
                if event.status == 'speech_started':
                    run.session.user_speech_started()
                elif event.status == 'complete':
                    run.completed.append((run.session.utterance_id, event.pcm))
            run.session.tick()

    run.feed = feed
    run.session.activate()
    return run


def test_speech_before_deadline_can_finish_after_deadline_without_a_new_wake():
    run = runtime()
    run.session.on_playback_status('first', 'playing')
    run.session.on_playback_status('first', 'finished')
    run.feed(QUIET, 249)
    assert run.session.active
    # Onset at 4.99 s cancels the wait; audio completes after the 5-second deadline.
    run.now = 4.97
    run.feed(VOICE)
    run.feed(QUIET, 99)
    assert run.completed == []
    assert run.session.deadline is None
    run.feed(QUIET)
    uid, pcm = run.completed.pop()
    assert pcm.endswith(VOICE + QUIET * 100)
    run.now = 10.0  # A delayed local transcription result is still in this turn.
    run.session.finish_utterance(uid, '계속 이야기하자', addressed=True)
    assert run.transcripts == [(uid, '계속 이야기하자')]
    run.session.on_playback_status('second', 'playing')
    run.session.on_playback_status('second', 'finished')
    run.now = 15.0
    assert run.session.tick()
    assert not run.session.active


def test_barge_in_pauses_before_transcription_and_resumes_after_acknowledgement():
    run = runtime()
    run.session.on_playback_status('reply', 'playing')
    run.feed(VOICE)
    assert run.controls == [('reply', 'pause')]
    assert run.completed == run.transcripts == []
    run.feed(QUIET, 100)
    uid, _ = run.completed.pop()
    # The decision is injected; these components do not classify the addressee.
    run.session.finish_utterance(uid, '친구에게 한 말', addressed=False)
    assert run.controls == [('reply', 'pause')]
    run.session.on_playback_status('reply', 'paused')
    assert run.controls == [('reply', 'pause'), ('reply', 'resume')]
    assert run.transcripts == []
    run.session.on_playback_status('reply', 'playing')
    run.session.on_playback_status('reply', 'finished')
    assert run.session.deadline == run.now + 5.0
