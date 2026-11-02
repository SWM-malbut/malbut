"""Late camera capture across memory retrieval, request copies and API retries."""

import copy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.schemas import RobotState, SpeechAgentRequest, copy_request
from malbut_agent_server.speech_scene import LatestScene, PendingTurnScene
from malbut_agent_server.story_memory_provider import StoryMemoryProvider


def make_request(image):
    return SpeechAgentRequest(
        'request', 'speaker', 'conversation', 'turn', '앞에 뭐가 보여?', RobotState(), (),
        scene_context={'image': {'available': False, 'reason': 'pending_capture'},
                       'map': {'revision': 'map-1'}, 'conversation_opener': {'source': 'current_encounter'}},
        turn_image=image,
    )


def image_context(payload):
    content = payload['input']
    text = content[0]['content'][0]['text'] if isinstance(content, list) else content
    return json.loads(text.split('\n', 1)[1])['scene_context_untrusted']


def scene_fixture():
    now = [100.0]
    scene = LatestScene(cloud_settings_required=False, clock=lambda: now[0])
    captures = []

    def capture():
        captures.append(now[0])
        scene.observe_image('pixels', now[0], 0)
        metadata, image = scene.capture(lambda _: b'private-jpeg')
        return {'image': metadata, 'map': {'revision': 'map-2'},
                'pose': {'x': 1.5, 'y': 2.0, 'age_s': 0}}, image

    return now, scene, captures, PendingTurnScene(capture)


def test_slow_memory_retrieval_finishes_before_camera_capture_and_transport():
    now, _, captures, pending = scene_fixture()
    payloads = []

    class Memory:
        def context(self, *args):
            assert captures == []
            now[0] += 3
            return {'enabled': False}

        def record_context_reply(self, *args):
            assert captures == []
            now[0] += 1
            return True

    def transport(url, headers, payload, timeout):
        assert captures == [104.0]
        assert payload['input'][0]['content'][1]['type'] == 'input_image'
        payloads.append(payload)
        return {'id': 'offline', 'status': 'completed', 'model': 'offline', 'output': [{
            'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                'type': 'message', 'message': '상자가 보여요.', 'reason': 'visible',
                'confidence': 1.0,
            })}],
        }]}

    provider = StoryMemoryProvider(
        OpenAIResponsesProvider('offline-key', 'offline', transport=transport), Memory())
    result = provider.complete(make_request(pending), [], [], [], memory_context={'mode': 'answer_only'})
    assert result.decision.message == '상자가 보여요.'
    context = image_context(payloads[0])
    assert context['image']['available']
    assert context['image']['source_stamp_s'] == 104.0
    assert context['map']['revision'] == 'map-2'
    assert context['pose']['x'] == 1.5
    assert context['conversation_opener']['source'] == 'current_encounter'


@pytest.mark.parametrize('change', ['expire', 'revoke'])
def test_retry_rechecks_permission_and_age_without_recapturing(change):
    now, scene, captures, pending = scene_fixture()
    request = make_request(pending)
    provider = OpenAIResponsesProvider('offline-key', 'offline')
    first = provider.build_payload(request, [], [], [])
    assert isinstance(first['input'], list)
    if change == 'expire':
        now[0] += 2.1
    else:
        scene.observe_settings(SimpleNamespace(
            check_state='confirmed', server_checked_at=now[0],
            camera_enabled=False, cloud_consent=True))
    second = provider.build_payload(copy.deepcopy(request), [], [], [])
    assert isinstance(second['input'], str)
    assert image_context(second)['image']['reason'] == 'stale_or_permission_changed'
    assert captures == [100.0]


def test_revocation_during_memory_work_prevents_encoding():
    now, scene, _, pending = scene_fixture()
    scene.observe_settings(SimpleNamespace(
        check_state='confirmed', server_checked_at=now[0],
        camera_enabled=True, cloud_consent=False))
    payload = OpenAIResponsesProvider('offline-key', 'offline').build_payload(
        make_request(pending), [], [], [])
    assert isinstance(payload['input'], str)
    assert image_context(payload)['image']['reason'] == 'camera_or_consent_unavailable'


@pytest.mark.parametrize('mode', ['automatic_extraction', 'source_review'])
def test_background_memory_never_captures_a_picture(mode):
    pending = PendingTurnScene(lambda: pytest.fail('background camera access'))
    request = replace(make_request(pending), available_tools=())
    payload = OpenAIResponsesProvider('offline-key', 'offline').build_payload(
        request, [], [], [], memory_context={'mode': mode})
    assert isinstance(payload['input'], str)
    assert 'scene_context_untrusted' not in json.loads(payload['input'].split('\n', 1)[1])


def test_failed_capture_is_not_retried_and_keeps_text_dialogue_available():
    attempts = []

    def fail():
        attempts.append(1)
        raise RuntimeError('camera unavailable')

    request = make_request(PendingTurnScene(fail))
    provider = OpenAIResponsesProvider('offline-key', 'offline')
    for _ in range(2):
        payload = provider.build_payload(request, [], [], [])
        assert image_context(payload)['image']['reason'] == 'capture_failed'
    assert attempts == [1]


def test_pending_capture_survives_internal_copies_but_never_serialization():
    _, _, captures, pending = scene_fixture()
    request = make_request(pending)
    clone = copy_request(request, request.to_dict())
    assert clone.turn_image is pending
    assert copy.deepcopy(request).turn_image is pending
    assert SpeechAgentRequest.from_dict(request.to_dict()).turn_image is None
    pending.resolve()
    assert 'private-jpeg' not in repr(request)
    assert 'private-jpeg' not in json.dumps(request.to_dict())
    clone.turn_image.resolve()
    assert len(captures) == 1


@pytest.mark.parametrize('change', ['expire', 'revoke'])
def test_slow_payload_preparation_cannot_send_expired_or_revoked_image(change):
    now, scene, captures, pending = scene_fixture()

    def counter(payload):
        if change == 'expire':
            now[0] += 3
        else:
            scene.observe_settings(SimpleNamespace(
                check_state='confirmed', server_checked_at=now[0],
                camera_enabled=False, cloud_consent=True))
        return 100

    provider = OpenAIResponsesProvider('offline', 'offline', semantic_context=True, token_counter=counter)
    payload = provider.build_payload(make_request(pending), [], [], [])
    assert isinstance(payload['input'], str)
    assert image_context(payload)['image']['available'] is False
    assert 'private-jpeg' not in json.dumps(payload)
    assert captures == [100.0]
