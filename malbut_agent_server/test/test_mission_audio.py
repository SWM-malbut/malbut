"""Verify source-backed failure notices and conservative outcome handling."""

import pytest
import yaml

from malbut_agent_server.mission_audio import CATALOG, notice_for_event
from malbut_agent_server.mission_audio_cases import LABELS
from malbut_agent_server.mission_speech import MissionAnnouncer


@pytest.mark.parametrize('capability,payload,suffix,phrase', [
    ('patrol', {'message': 'RGB camera is stale'}, 'camera_stale', '영상이 제때'),
    ('patrol', {'message': 'Saved /map is not available'}, 'map_missing', '지도를 받지'),
    ('autoslam', {'message': 'Nav2 map saver failed'}, 'save_failed', '저장하지'),
    ('get_weather', {'error_code': 'KMA_RATE_LIMITED'}, 'rate_limited', '조회 한도'),
    ('set_weather_location', {'message': 'LOCATION_SAVE_FAILED'}, 'save_failed', '저장하지'),
    ('enroll_person', {'message': 'At most five people can be registered'}, 'capacity', '다섯 명'),
    ('relocalize', {'message': 'no recent LiDAR scan'}, 'scan_stale', '라이다'),
])
def test_known_failure_selects_its_own_recording(capability, payload, suffix, phrase):
    audio_id, text = notice_for_event(dict(
        capability_id=capability, kind='failed', result_yaml=yaml.safe_dump(payload)))
    assert audio_id == f'{capability}.failed.{suffix}'
    assert phrase in text and CATALOG[audio_id] == text


def test_every_registered_capability_has_a_recorded_outcome():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'malbut_interfaces/capabilities'
    capabilities = {yaml.safe_load(path.read_text())['capability']['id']
                    for path in root.glob('*.yaml')}
    for capability in capabilities | set(LABELS):
        # New capabilities can use shared outcome clips until their cases are added.
        prefix = capability if capability in LABELS else 'operation'
        for kind in ('succeeded', 'failed', 'canceled', 'unsupported'):
            assert notice_for_event(dict(capability_id=capability, kind=kind)) == (
                f'{prefix}.{kind}', CATALOG[f'{prefix}.{kind}'])
    for capability in LABELS:
        assert notice_for_event(dict(capability_id=capability, kind='unknown')) == (
            f'{capability}.unknown', CATALOG[f'{capability}.unknown'])
    assert CATALOG['operation.unsupported'] == '현재 지원하지 않아요.'


@pytest.mark.parametrize('reason,expected', [
    ('mission preempted by a replacement request', 'patrol.failed.manager_preempted'),
    ('unrecognized failure', 'patrol.failed'),
])
def test_manager_reason_overrides_child_payload(reason, expected):
    audio_id, _ = notice_for_event(dict(capability_id='patrol', kind='failed',
                                       reason=reason, result_yaml='message: RGB camera is stale'))
    assert audio_id == expected


@pytest.mark.parametrize('payload', [
    'message: RGB camera is stale', '!!python/object:malicious {}',
    '[unrelated, data]', 'message: [list]', 'message: "quoted RGB camera is stale"',
    'x' * 16385,
])
def test_foreign_or_invalid_result_does_not_invent_a_cause(payload):
    audio_id, _ = notice_for_event(dict(capability_id='follow_person', kind='failed',
                                       result_yaml=payload))
    assert audio_id == 'follow_person.failed'


def test_manager_service_error_does_not_become_a_weather_provider_failure():
    audio_id, text = notice_for_event(dict(capability_id='get_weather', kind='failed',
                                          reason='Downstream Service is unavailable'))
    assert audio_id == 'get_weather.failed.manager_service_unavailable'
    assert '기상청' not in text


def test_announcer_suppresses_routine_events_and_deduplicates_results():
    spoken = []
    def unexpected(text):
        pytest.fail('known notices must not call text speech')
    announcer = MissionAnnouncer(unexpected, speak_audio=lambda text, audio_id:
                                 spoken.append((text, audio_id)) or True)
    event = dict(request_id='request', capability_id='patrol', kind='failed',
                 result_yaml='message: RGB camera is stale')
    assert announcer.handle(dict(event, kind='accepted')) is None
    assert announcer.handle(dict(event, kind='unknown'))
    assert spoken[-1][1] == 'patrol.unknown'
    assert announcer.handle(event)
    assert announcer.handle(event) is None
    assert spoken[-1][1] == 'patrol.failed.camera_stale'
    assert len(spoken) == 2


def test_unsent_audio_can_be_announced_again():
    announcer = MissionAnnouncer(lambda _: True, speak_audio=lambda *_: False)
    event = dict(request_id='request', capability_id='patrol', kind='succeeded')
    assert announcer.handle(event) is None
    announcer._speak_audio = lambda *_: True
    assert announcer.handle(event)
