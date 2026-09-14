"""Exercise the STT/Agent decision round trip with synthetic audio and model output."""

import json
import time

import pytest

from malbut_stt.dialogue_pipeline import DialoguePipeline

speech_dialogue = pytest.importorskip('malbut_agent_server.speech_dialogue')
from malbut_agent_server.config import Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.speech_addressee import SpeechAddresseeClassifier


def drain_one(worker):
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        replies = worker.drain()
        if replies:
            assert len(replies) == 1
            return replies[0]
        time.sleep(0.002)
    pytest.fail('Agent worker did not return a result')


@pytest.mark.parametrize('decision', ['addressed', 'not_addressed', 'unknown'])
def test_candidate_is_classified_before_normal_dialogue_and_correlated_back_to_stt(decision):
    requests, controls, transcripts, runtimes, handled = [], [], [], [], []

    def transport(url, headers, payload, timeout):
        requests.append(payload)
        return {'status': 'completed', 'output': [{
            'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text',
                         'text': json.dumps({'decision': decision})}],
        }]}

    def factory():
        runtime = build_orchestrator(Settings(database_path=':memory:'))
        runtime.speech_addressee = SpeechAddresseeClassifier(OpenAIResponsesProvider(
            api_key='test-key-not-for-network', model='test-model', transport=transport,
        ))
        handle = runtime.handle

        def track(request):
            handled.append(request.utterance)
            return handle(request)

        runtime.handle = track
        runtimes.append(runtime)
        return runtime

    worker = speech_dialogue.DialogueWorker(factory, 'speech-test')

    def publish_transcript(uid, text):
        transcripts.append((uid, text))
        assert worker.submit(uid, text)

    pipeline = DialoguePipeline(
        recorder_factory=None, wake=None, transcriber=None,
        is_speech=lambda frame, _: any(frame), clock=lambda: 0.0,
        publish_transcript=publish_transcript,
        publish_control=lambda pid, command: controls.append((pid, command)),
        publish_interruption=lambda uid, pid, text: worker.submit_interruption(uid, pid, text),
        report=lambda event: None,
        # Input is synthetic near-end speech; this does not test real microphone AEC.
        input_has_aec=True,
    )

    def say(text):
        pipeline.feed(b'\x01\x00' * 320)
        pipeline.feed(bytes(640) * 150)
        kind, generation, uid, pcm = pipeline.jobs.get_nowait()
        assert kind == 'command' and pcm
        pipeline.results.put_nowait((kind, generation, uid, text, None))
        pipeline.poll()
        return uid

    try:
        pipeline.session.activate()
        say('안녕')
        first = drain_one(worker)
        assert first['kind'] == 'answer'
        pipeline.on_playback_status('reply-1', 'playing')
        interrupted_text = '지금 그 이야기는 잠깐 멈춰'
        uid = say(interrupted_text)
        assert controls == [('reply-1', 'pause')]
        pipeline.on_playback_status('reply-1', 'paused')
        verdict = drain_one(worker)
        assert verdict == {
            'kind': 'addressee', 'utterance_id': uid,
            'playback_id': 'reply-1', 'decision': decision,
        }
        assert handled == ['안녕']
        assert len(transcripts) == len(requests) == 1
        context = json.loads(requests[0]['input'])
        assert context['current_utterance_untrusted'] == interrupted_text
        assert context['recent_turns_untrusted'][-1]['user'] == '안녕'
        for _ in range(2):
            pipeline.on_addressee(uid, 'reply-1', decision)
        if decision == 'addressed':
            assert drain_one(worker)['kind'] == 'answer'
            assert controls == [('reply-1', 'pause'), ('reply-1', 'stop')]
            assert handled == ['안녕', interrupted_text]
            assert len(transcripts) == 2
        elif decision == 'not_addressed':
            assert controls == [('reply-1', 'pause'), ('reply-1', 'resume')]
            assert handled == ['안녕']
        else:
            assert controls == [('reply-1', 'pause')]
            assert not pipeline.session.active
        stored = runtimes[0].conversation_store.snapshot(
            'speech-test', first['conversation_id'],
        )
        assert [turn.user_content for turn in stored.turns] == handled
    finally:
        pipeline.close()
        worker.close()
