"""Brief outcomes retain uncertainty without reading internal failure reasons."""

import copy
from pathlib import Path

import pytest
import yaml

from malbut_agent_server.mission_audio import CATALOG, TEXT_IDS, notice_for_event
from malbut_agent_server.mission_audio_cases import EVENT_AUDIO_IDS
from malbut_agent_server.mission_speech import MissionAnnouncer
from malbut_agent_server.function_speech import FUNCTION_STARTS
from malbut_agent_server.tools import HOMECAM_QUERY_TOOLS, SPEECH_MISSION_TOOLS


@pytest.mark.parametrize('capability,reason,payload', [
    ('patrol', '', 'message: RGB camera is stale'),
    ('autoslam', '', 'message: Nav2 map saver failed'),
    ('get_weather', '', 'error_code: KMA_RATE_LIMITED'),
    ('enroll_person', '', 'message: At most five people can be registered'),
    ('relocalize', '', 'message: no recent LiDAR scan'),
    ('patrol', 'mission preempted by a replacement request', 'message: RGB camera is stale'),
    ('future_capability', 'unrecognized failure', '!!python/object:malicious {}'),
    ('follow_person', '', 'x' * 16385),
])
def test_failures_share_a_short_clip_and_preserve_diagnostics(capability, reason, payload):
    event = dict(capability_id=capability, kind='failed', reason=reason, result_yaml=payload)
    original = copy.deepcopy(event)
    assert notice_for_event(event) == ('operation.failed', '작업을 완료하지 못했어요.')
    assert event == original


def test_every_registered_capability_has_all_recorded_outcomes():
    root = Path(__file__).resolve().parents[2] / 'malbut_interfaces/capabilities'
    capabilities = {yaml.safe_load(path.read_text())['capability']['id']
                    for path in root.glob('*.yaml')}
    for capability in capabilities | {'future_capability'}:
        for kind, expected in EVENT_AUDIO_IDS.items():
            if capability == 'set_weather_location' and kind == 'succeeded':
                expected = 'set_weather_location.succeeded'
            assert notice_for_event(dict(capability_id=capability, kind=kind)) == (
                expected, CATALOG[expected])
    assert CATALOG['operation.unsupported'] == '현재 지원하지 않아요.'


def test_voice_functions_keep_one_start_and_cancellation_receipt():
    tools = (set(SPEECH_MISSION_TOOLS) | set(HOMECAM_QUERY_TOOLS)
             | {'get_weather', 'set_weather_location'}) - {'cancel_voice_mission'}
    assert set(FUNCTION_STARTS) == tools
    for tool in tools:
        assert CATALOG[f'function.{tool}.starting'] == FUNCTION_STARTS[tool]
    assert CATALOG['cancel.requested'] == '취소를 요청했어요.'
    assert CATALOG['cancel.unknown'] == '취소 여부를 확인하지 못했어요.'
    assert CATALOG['operation.canceled'] == '작업이 취소됐어요.'
    assert not any('.failed.' in key for key in CATALOG)


def test_existing_detailed_replies_use_short_recordings():
    text = '전면 작업의 취소를 요청했지만 접수 여부를 확인하지 못했어요. 종료된 것으로 판단하지 않을게요.'
    assert CATALOG[TEXT_IDS[text]] == '취소 여부를 확인하지 못했어요.'
    for text, audio_id in TEXT_IDS.items():
        assert audio_id in CATALOG


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
    assert spoken[-1][1] == 'operation.unknown'
    assert announcer.handle(event)
    assert announcer.handle(event) is None
    assert spoken[-1][1] == 'operation.failed'
    assert len(spoken) == 2


def test_unsent_audio_can_be_announced_again():
    announcer = MissionAnnouncer(lambda _: True, speak_audio=lambda *_: False)
    event = dict(request_id='request', capability_id='patrol', kind='succeeded')
    assert announcer.handle(event) is None
    announcer._speak_audio = lambda *_: True
    assert announcer.handle(event)
