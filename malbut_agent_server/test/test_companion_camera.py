"""Requested live views without owner context, persistent pixels or API calls."""

import base64
import json
from types import SimpleNamespace

import pytest

from malbut_agent_server.companion_dialogue import CompanionDialogue, requests_current_view
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.speech_dialogue import DialogueWorker
from malbut_agent_server.speech_scene import PendingTurnScene, TurnImage
from test_speech_dialogue import RuntimeFactory, collect


@pytest.mark.parametrize('text', [
    '앞에 뭐가 보여?', '지금 카메라 화면 확인해줘', '어? 저 뭐니?',
    '저게 뭐야?', '이거 무엇인지 알려줘', '주변에 무엇이 있어?', '이것 좀 봐줘',
])
def test_explicit_view_questions(text):
    assert requests_current_view(text)


@pytest.mark.parametrize('text', [
    '오늘 산책했어', '아, 진짜?', '지금 뭐해?', '저는 뭐 먹을까 고민 중이에요',
    '내 기억 지워줘', '식물 좋아해요', '어제 본 영화가 재미있었어',
])
def test_small_talk_does_not_request_a_picture(text):
    assert not requests_current_view(text)


class Camera:
    def __init__(self, permitted=True):
        self.prepared = 0
        self.captured = 0
        self.permitted = permitted

    def prepare_image(self):
        self.prepared += 1
        return PendingTurnScene(self.capture)

    def capture(self):
        self.captured += 1
        if not self.permitted:
            return {'image': {'available': False, 'reason': 'camera_or_consent_unavailable'}}, None
        return {'image': {'available': True}}, TurnImage(b'private-jpeg', lambda: self.permitted)


def test_worker_guest_view_reaches_api_payload_but_not_owner_history_or_disk():
    camera, payloads = Camera(), []

    def transport(url, headers, payload, timeout):
        payloads.append(payload)
        content = payload['input'][0]['content']
        assert [part['type'] for part in content] == ['input_text', 'input_image']
        assert base64.b64decode(content[1]['image_url'].split(',')[1]) == b'private-jpeg'
        context = json.loads(content[0]['text'].split('\n', 1)[1])
        assert context['scene_context_untrusted'] == {
            'companion_guest': True, 'image': {'available': True},
        }
        assert 'tools' not in payload and payload['store'] is False
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
            'type': 'message', 'message': '상자가 보여요.', 'reason': 'visible', 'confidence': 1.0,
        })}]}]}

    factory = RuntimeFactory(OpenAIResponsesProvider('offline-key', 'offline', transport=transport))
    def runtime():
        value = factory()
        value.companion_provider = factory.provider
        return value

    worker = DialogueWorker(runtime, 'owner', scene_source=camera)
    try:
        assert worker.submit('view', '앞에 뭐가 보여?', companion_session='visit')
        assert collect(worker, 1)[0]['text'] == '상자가 보여요.'
        assert camera.prepared == camera.captured == len(payloads) == 1
        assert factory.handled == []  # No owner orchestrator/history access.
        assert 'private-jpeg' not in repr(worker._companion.snapshot())
        assert 'image_url' not in repr(worker._companion.snapshot())
    finally:
        worker.close()


@pytest.mark.parametrize('available', [True, False])
def test_next_small_talk_turn_does_not_reuse_pixels_or_access_camera(available):
    camera, calls = Camera(available), []
    builder = OpenAIResponsesProvider('offline-key', 'offline')

    def complete(request, memories, turns, tools):
        calls.append((request, builder.build_payload(request, memories, turns, tools)))
        return SimpleNamespace(decision=SimpleNamespace(type='message', message='알겠어요.'))

    dialogue = CompanionDialogue()
    dialogue.begin('visit')
    provider = SimpleNamespace(complete=complete)
    dialogue.answer(provider, 'visit', 'one', '저게 뭐야?', None, scene_source=camera)
    assert isinstance(calls[0][1]['input'], list) is available
    dialogue.answer(provider, 'visit', 'two', '오늘 산책했어', None, scene_source=camera)
    assert calls[1][0].turn_image is None
    assert isinstance(calls[1][1]['input'], str)
    assert camera.prepared == camera.captured == 1


def test_camera_failure_keeps_guest_text_reply_available():
    camera = SimpleNamespace(prepare_image=lambda: (_ for _ in ()).throw(RuntimeError('offline')))
    dialogue = CompanionDialogue()
    dialogue.begin('visit')

    def complete(request, memories, turns, tools):
        assert request.turn_image is None and not request.robot_state.camera_available
        assert request.scene_context['image']['reason'] == 'camera_unavailable'
        return SimpleNamespace(decision=SimpleNamespace(type='message', message='이번에는 영상을 받지 못했어요.'))

    assert dialogue.answer(SimpleNamespace(complete=complete), 'visit', 'one',
                           '카메라 화면 보여?', None, scene_source=camera)


def test_ordinary_worker_defers_capture_across_orchestrator_request_copies():
    camera = Camera()
    pending = camera.prepare_image()
    source = SimpleNamespace(
        prepare_turn=lambda: ({'image': {'available': False, 'reason': 'pending_capture'}}, pending),
        snapshot=lambda: pytest.fail('must not capture before orchestration'),
    )
    factory = RuntimeFactory()
    worker = DialogueWorker(factory, 'owner', scene_source=source)
    try:
        assert worker.submit('view', '앞에 뭐가 보여?')
        collect(worker, 1)
        assert factory.provider.calls[0][0].turn_image is pending
        assert camera.captured == 0
    finally:
        worker.close()
