from malbut_agent_server.speech_delivery import SpeechDelivery
from malbut_agent_server.schemas import SpeechAgentRequest,RobotState
from malbut_agent_server.speech_dialogue import DialogueWorker
from test_speech_dialogue import FixedProvider,RuntimeFactory,collect

def test_old_playback_status_cannot_overwrite_current_answer():
    delivery=SpeechDelivery()
    delivery.requested('old',interim=False);delivery.observe('old','playing')
    assert delivery.input_started()
    delivery.observe('old','stopped');assert delivery.interrupted
    delivery.requested('new',interim=False);delivery.observe('new','playing')
    delivery.observe('old','finished');delivery.observe('new','finished')
    assert not delivery.input_started()
    delivery.requested('progress',interim=True);delivery.observe('progress','stopped')
    assert not delivery.interrupted

def test_worker_preserves_interruption_as_metadata_not_user_text():
    provider=FixedProvider();worker=DialogueWorker(RuntimeFactory(provider),'speaker')
    try:
        assert worker.submit('interrupt','다른 이야기 해줘',previous_reply_interrupted=True)
        collect(worker,1)
        request=provider.calls[0][0]
        assert request.utterance=='다른 이야기 해줘'
        assert request.previous_reply_interrupted
        assert SpeechAgentRequest.from_dict(request.to_dict()).previous_reply_interrupted
        assert worker.submit('normal','계속 이야기해줘')
        collect(worker,1)
        assert not provider.calls[1][0].previous_reply_interrupted
    finally:worker.close()
