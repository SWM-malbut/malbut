"""Transient per-turn RGB and consent checks without ROS, hardware or API calls."""

import base64
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.schemas import RobotState, SpeechAgentRequest, copy_request
from malbut_agent_server.speech_scene import LatestScene, TurnImage


def settings(now, *, camera=True, consent=True, state='confirmed'):
    return SimpleNamespace(server_checked_at=now, camera_enabled=camera,
                           cloud_consent=consent, check_state=state, enabled=False)


def request(image=None):
    return SpeechAgentRequest(
        'request', 'speaker', 'conversation', 'turn', '앞에 뭐가 보여?', RobotState(), (),
        scene_context={'image': {'available': image is not None}, 'map': None, 'pose': None},
        turn_image=image,
    )


def test_latest_frame_only_and_fall_enabled_is_not_camera_consent():
    now = [100.0]
    cache = LatestScene(cloud_settings_required=True, clock=lambda: now[0])
    cache.observe_image('before-permission', 99.9, 0.1)
    assert cache.capture(lambda _: b'jpeg')[1] is None
    cache.observe_settings(settings(now[0]))
    cache.observe_image('old', 99.9, 0.1)
    cache.observe_image('latest', 100.0, 0)
    encoded = []
    context, image = cache.capture(lambda raw: encoded.append(raw) or b'jpeg')
    assert encoded == ['latest'] and image.usable()
    assert context['source_stamp_s'] == 100.0
    cache.observe_settings(settings(now[0], camera=False))
    assert not image.usable()
    assert cache.capture(lambda _: b'jpeg')[1] is None


@pytest.mark.parametrize('source_age', [-1.0, 2.1, float('nan'), float('inf')])
def test_stale_future_invalid_frames_never_reach_encoder(source_age):
    cache = LatestScene(cloud_settings_required=False)
    cache.observe_image('raw', 1, source_age)
    assert cache.capture(lambda _: pytest.fail('must not encode'))[1] is None


def test_retry_does_not_recapture_and_expires_from_original_source_time():
    now = [100.0]
    cache = LatestScene(cloud_settings_required=False, clock=lambda: now[0])
    cache.observe_image('original', 99.0, 1.0)
    _, image = cache.capture(lambda _: b'first-jpeg')
    cache.observe_image('newer', 100.0, 0.0)
    assert image.jpeg == b'first-jpeg'
    now[0] += 1.1
    assert not image.usable()
    assert cache.capture(lambda _: b'new-jpeg')[1] is not None


def test_settings_expire_and_standalone_also_honors_observed_off_setting():
    now = [100.0]
    cache = LatestScene(cloud_settings_required=False, clock=lambda: now[0])
    cache.observe_settings(settings(100.0, consent=False))
    cache.observe_image('raw', 100, 0)
    assert cache.capture(lambda _: b'jpeg')[1] is None
    now[0] = 100.1
    cache.observe_settings(settings(now[0]))
    cache.observe_image('raw', 100, 0)
    _, image = cache.capture(lambda _: b'jpeg')
    now[0] = 131
    assert not image.usable()


def test_old_confirmed_snapshot_cannot_restore_revoked_camera_consent():
    now = [100.0]
    cache = LatestScene(cloud_settings_required=True, clock=lambda: now[0])
    cache.observe_settings(settings(100.0))
    now[0] = 101.0
    cache.observe_settings(settings(101.0, consent=False))
    for checked in (100.0, 101.0):
        cache.observe_settings(settings(checked))
        cache.observe_image('raw', 101, 0)
        assert cache.capture(lambda _: b'jpeg')[1] is None


def test_bytes_never_enter_json_or_repr_but_survive_internal_orchestrator_copies():
    image = TurnImage(b'private-camera-bytes', lambda: True)
    original = request(image)
    value = original.to_dict()
    assert 'private-camera' not in json.dumps(value) and 'private-camera' not in repr(original)
    assert SpeechAgentRequest.from_dict(value).turn_image is None
    assert copy_request(original, value).turn_image is image


def test_provider_attaches_exactly_one_image_without_counting_base64_as_text():
    image = TurnImage(b'private-camera-bytes', lambda: True)
    counted = []
    provider = OpenAIResponsesProvider(
        'offline-key', 'offline-model', semantic_context=True,
        token_counter=lambda payload: counted.append(json.dumps(payload)) or 100,
    )
    payload = provider.build_payload(request(image), [], [], [])
    content = payload['input'][0]['content']
    assert [part['type'] for part in content] == ['input_text', 'input_image']
    assert content[1]['detail'] == 'low'
    assert base64.b64decode(content[1]['image_url'].split(',')[1]) == image.jpeg
    assert 'image_url' not in counted[0]
    assert json.loads(content[0]['text'].split('\n', 1)[1])['scene_context_untrusted']['image']['available']


def test_no_image_after_revocation_or_in_background_memory_requests():
    permission = [True]
    original = request(TurnImage(b'jpeg', lambda: permission[0]))
    provider = OpenAIResponsesProvider('offline-key', 'offline-model')
    permission[0] = False
    payload = provider.build_payload(original, [], [], [])
    assert isinstance(payload['input'], str)
    assert not json.loads(payload['input'].split('\n', 1)[1])['scene_context_untrusted']['image']['available']
    permission[0] = True
    for mode in ('automatic_extraction', 'source_review'):
        payload = provider.build_payload(original, [], [], [], memory_context={'mode': mode})
        assert isinstance(payload['input'], str)
        assert 'scene_context_untrusted' not in json.loads(payload['input'].split('\n', 1)[1])


def test_explicit_disabled_and_encoding_failure_do_not_block_text_dialogue():
    cache = LatestScene(cloud_settings_required=False, enabled=False)
    cache.observe_image('raw', 100, 0)
    assert cache.capture(lambda _: pytest.fail('disabled'))[1] is None
    cache.enabled = True
    cache.observe_image('raw', 100, 0)
    def fail(_):
        raise ValueError('invalid RGB')
    context, image = cache.capture(fail)
    assert image is None and context['reason'] == 'encoding_failed'
    assert replace(request(), scene_context={'image': context}).utterance


def test_transition_state_does_not_expire_like_a_sensor_heartbeat():
    from threading import RLock
    import time
    from malbut_agent_server.speech_scene import RosSpeechScene

    source = object.__new__(RosSpeechScene)
    source.node = SimpleNamespace(count_publishers=lambda _: 1)
    source.missions = None
    source._lock = RLock()
    source._images = LatestScene(cloud_settings_required=True)
    source._system = (time.monotonic() - 60, {'system_state': 1})
    scene, attachment = source.snapshot()
    assert attachment is None
    assert scene['missions']['system_state'] == 1
    assert scene['missions']['observed_age_s'] >= 60
    assert scene['missions']['source'] == 'latched_transition'
    source.node.count_publishers = lambda _: 0
    assert source.snapshot()[0]['missions'] is None
