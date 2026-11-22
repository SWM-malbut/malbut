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


def test_actual_provider_prompt_allows_scene_grounded_motion_estimates():
    from malbut_agent_server.tools import SPEECH_MISSION_TOOLS, select_tool_specs

    scene = {
        'image': {'available': True},
        'pose': {'frame_id': 'map', 'x': 0.5, 'y': 0.5, 'yaw_deg': 30, 'age_s': 0.1},
        'map': {'revision': 'current-map', 'landmarks': [
            {'location': '거실', 'x': 1.0, 'y': 2.0}]},
    }
    original = replace(
        request(TurnImage(b'jpeg', lambda: True)), utterance='사진 보고 거실 쪽으로 조금 가줘',
        available_tools=SPEECH_MISSION_TOOLS, scene_context=scene, navigation_locations=(),
    )
    payload = OpenAIResponsesProvider('offline-key', 'offline-model').build_payload(
        original, [], [], select_tool_specs(SPEECH_MISSION_TOOLS))
    instructions = payload['instructions']
    assert '이동 방향·회전각·이동거리·지도 목표·경유점을 판단한다' in instructions
    assert '사용자가 수치를 지정하면 따르고, 지정하지 않았어도' in instructions
    assert '지도 목표는 제공된 현재 위치·방향과 장소 좌표를 기준으로 선택하거나 계산할 수 있다' in instructions
    assert '사소한 불확실성만으로 매번 수치를 되묻지 않는다' in instructions
    assert '후방이 안전하다고 단정하지 않는다' in instructions
    assert '별도 좌표·회전·후진 도구의 장면 기반 판단을 금지하지 않습니다' in instructions
    assert '사진만 보고 이동 목표를 만들지 않는다' not in instructions
    descriptions = {tool['name']: tool['description'] for tool in payload['tools']}
    assert 'otherwise choose an appropriate' in descriptions['request_rotation']
    assert 'otherwise choose a modest distance' in descriptions['request_backup']
    assert 'choose or calculate an approximate goal' in descriptions['request_navigation_point']
    assert 'chosen using the current pose' in descriptions['request_waypoints']
    data = json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])
    assert data['scene_context_untrusted'] == scene
    assert data['navigation_context']['locations'] == []


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
    from contextlib import nullcontext
    from threading import Event, RLock
    import time
    from malbut_agent_server.speech_scene import RosSpeechScene

    source = object.__new__(RosSpeechScene)
    source.node = SimpleNamespace(count_publishers=lambda _: 1)
    source.missions = None
    source._lock = RLock()
    source._sample_lock = RLock()
    source._closed = Event()
    source._sensor_window = nullcontext
    source._images = LatestScene(cloud_settings_required=True)
    source._system = (time.monotonic() - 60, {'system_state': 1})
    scene, attachment = source.snapshot()
    assert attachment is None
    assert scene['missions']['system_state'] == 1
    assert scene['missions']['observed_age_s'] >= 60
    assert scene['missions']['source'] == 'latched_transition'
    source.node.count_publishers = lambda _: 0
    assert source.snapshot()[0]['missions'] is None


def test_releasing_raw_pixels_preserves_frozen_image_until_revocation():
    cache = LatestScene(cloud_settings_required=False)
    cache.observe_image('raw', 100, 0)
    _, image = cache.capture(lambda _: b'jpeg')
    cache.clear_frame()
    assert cache.capture(lambda _: pytest.fail('raw image must be released'))[1] is None
    assert image.usable()
    cache.observe_settings(settings(cache.clock(), consent=False))
    assert not image.usable()


@pytest.mark.parametrize('utterance,expected', [
    ('안녕', False),
    ('안녕. 짧게 한 문장으로 인사해줘.', False),
    ('오늘 기분이 좋아', False),
    ('민지 이야기 기억해?', False),
    ('오늘 날씨 어때?', False),
    ('고마워', False),
    ('앞에 뭐가 보여?', True),
    ('저게 뭐야?', True),
    ('사진 보고 조금 움직여줘', True),
    ('왼쪽으로 돌아줘', True),
    ('뒤로 조금만 가줘', True),
    ('거실로 가줘', True),
    ('거실로 가자', True),
    ('지금 내가 입은 옷 색깔도 알 수 있어?', True),
    ('저 상자 색깔이 뭐야?', True),
    ('새 지도를 만들어줘', True),
    ('집을 가볍게 한 바퀴 순찰해줘', True),
    ('오늘 여기 기온이 몇도야?', False),
    ('기본 날씨 위치를 경기도 수원시 팔달구 우만1동으로 바꿔줘.', False),
    ('그럼, 내일 거기 최고 기온은 몇 도래?', False),
    ('홈캠 카메라랑 모니터링이 지금 켜져 있어?', False),
    ('최근에 홈캠에서 사람을 감지했는지도 조회해줘.', False),
    ('홈캠에 어제 움직임 있었어?', False),
    ('지난 순찰 사진에서 리모컨을 어디서 봤어?', False),
    ('지난 순찰에서 움직임 있었어?', False),
    ('그 순찰에서 안경도 봤어?', False),
    ('앞에 움직임이 있어?', True),
    ('홈캠 말고 지금 앞으로 좀 움직여줘', True),
    ('자, 오늘은 대화 여기까지만 하고 그만하자', False),
    ('좋아 다시 끝말잇기로 돌아가자 내가 아까 처음 제시한 단어가 뭐였지?', False),
    ('그럼, 책이 든 상자 색깔만 짧게 말해줘.', False),
    ('장난감 상자부터 책 상자 순서로 색깔 두 개만 말해줘.', False),
])
def test_needs_turn_image_gates_ordinary_vs_visual_and_navigation_turns(utterance, expected):
    from malbut_agent_server.speech_scene import needs_turn_image

    assert needs_turn_image(utterance, navigation_locations=('거실', '주방')) is expected


def test_prepare_text_turn_skips_camera_subscription_and_worker_routes_on_demand():
    from threading import Event, RLock
    from malbut_agent_server.speech_dialogue import DialogueWorker
    from malbut_agent_server.speech_scene import PendingTurnScene, RosSpeechScene
    from test_speech_dialogue import RuntimeFactory, collect

    source = object.__new__(RosSpeechScene)
    source.node = SimpleNamespace(count_publishers=lambda _: 0)
    source.missions = None
    source._lock = RLock()
    source._sample_lock = RLock()
    source._closed = Event()
    source._sensor_window = lambda: pytest.fail('text turn must not subscribe to camera')
    source._images = LatestScene(cloud_settings_required=False)
    source._system = None
    scene, attachment = source.prepare_text_turn()
    assert attachment is None
    assert scene['image'] == {'available': False, 'reason': 'not_requested'}

    calls = []
    pending = PendingTurnScene(lambda: ({'image': {'available': True}}, None))
    scene_source = SimpleNamespace(
        prepare_text_turn=lambda: (calls.append('text') or {'image': {'available': False, 'reason': 'not_requested'}}, None),
        prepare_turn=lambda: (calls.append('visual') or {'image': {'available': False, 'reason': 'pending_capture'}}, pending),
    )
    worker = DialogueWorker(RuntimeFactory(), 'owner', scene_source=scene_source)
    try:
        assert worker.submit('u1', '안녕. 짧게 한 문장으로 인사해줘.')
        assert worker.submit('u2', '앞에 뭐가 보여?')
        collect(worker, 2)
        assert calls == ['text', 'visual']
    finally:
        worker.close()
