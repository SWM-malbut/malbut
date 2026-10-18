"""Brief outcomes retain uncertainty without reading internal failure reasons."""

import ast
import copy
import json
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
        assert CATALOG[TEXT_IDS[FUNCTION_STARTS[tool]]] == FUNCTION_STARTS[tool]
    assert CATALOG['cancel.requested'] == '취소를 요청했어요.'
    assert CATALOG['cancel.unknown'] == '취소 여부를 확인하지 못했어요.'
    assert CATALOG['operation.canceled'] == '작업이 취소됐어요.'
    assert not any('.failed.' in key for key in CATALOG)


def test_existing_detailed_replies_use_short_recordings():
    text = '전면 작업의 취소를 요청했지만 접수 여부를 확인하지 못했어요. 종료된 것으로 판단하지 않을게요.'
    assert CATALOG[TEXT_IDS[text]] == '취소 여부를 확인하지 못했어요.'
    for text, audio_id in TEXT_IDS.items():
        assert audio_id in CATALOG


def test_fixed_input_errors_and_response_constants_have_recordings():
    """Catch a new literal error response that would silently call live TTS."""
    from malbut_agent_server import fall_check_watch, speech_dialogue

    source = Path(__file__).resolve().parents[1] / 'malbut_agent_server/ros_communication.py'
    literals = [node.args[0].value for node in ast.walk(ast.parse(source.read_text()))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'say' and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)]
    assert literals
    for module, names in (
        (speech_dialogue, ('ERROR_RESPONSE', 'SESSION_ERROR_RESPONSE',
                           'MEMORY_CHANGED_RESPONSE', 'NAVIGATION_CHANGED_RESPONSE')),
        (fall_check_watch, ('FALL_CHECK_START', 'FALL_CHECK_BUSY', 'FALL_CHECK_HELP',
                            'FALL_CHECK_STOPPED', 'FALL_CHECK_RESPONSE')),
    ):
        literals.extend(getattr(module, name) for name in names)
    for text in literals:
        assert text in TEXT_IDS, text


def test_fixed_navigation_guard_replies_have_recordings():
    """Map refusals returned just before dispatch, including expiration races."""
    source = Path(__file__).resolve().parents[1] / 'malbut_agent_server/speech_dialogue.py'
    guard = next(node for node in ast.walk(ast.parse(source.read_text()))
                 if isinstance(node, ast.FunctionDef) and node.name == 'guard')
    replies = [node.value.value for node in ast.walk(guard)
               if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
               and isinstance(node.value.value, str)]
    assert replies
    for text in replies:
        assert text in TEXT_IDS, text


def test_packaged_catalogs_match_the_agent_and_ship_every_selected_recording():
    root = Path(__file__).resolve().parents[2]
    for relative in ('malbut_tts', 'malbut_test/malbut_tts'):
        directory = root / relative / 'malbut_tts/audio'
        assert json.loads((directory / 'catalog.json').read_text()) == CATALOG
        assert all((directory / (audio_id + '.wav')).is_file() for audio_id in CATALOG)


@pytest.mark.parametrize('text,audio_id', [
    ('지도나 방 정보가 변경되어 이동하지 않았어요. 다시 말씀해 주세요.', 'navigation.changed'),
    ('사용 중인 저장 지도와 등록된 목적지를 확인할 수 없어요.', 'navigation.map_required'),
    ('등록된 목적지 이름을 확인해 다시 말씀해 주세요.', 'navigation.target_missing'),
])
def test_equivalent_fixed_replies_reuse_the_matching_recording(text, audio_id):
    assert TEXT_IDS[text] == audio_id


@pytest.mark.parametrize('text,audio_id', [
    ('실행 요청의 처리 여부를 확인하지 못했어요. 자동으로 다시 보내지 않을게요.',
     'operation.request_unknown'),
    ('이 음성 실행 기능이 연결되지 않았어요.', 'operation.voice_unavailable'),
    ('날씨 조회 기능을 사용할 수 없습니다.', 'weather.unavailable'),
    ('홈캠 조회가 연결되지 않았어요.', 'homecam.unavailable'),
    ('날씨 조회 인자가 올바르지 않습니다.', 'weather.invalid_arguments'),
    ('홈캠 조회 인자가 올바르지 않아요.', 'homecam.invalid_arguments'),
    ('홈캠 조회 결과로 답변을 만들지 못했어요.', 'homecam.answer_failed'),
    ('날씨 조회 결과로 답변을 만들지 못했어요.', 'weather.answer_failed'),
])
def test_tool_error_recordings_preserve_the_full_message(text, audio_id):
    assert TEXT_IDS[text] == audio_id
    assert CATALOG[audio_id] == text


def test_expired_confirmation_does_not_claim_that_the_map_changed():
    text = '확인 중 지도나 목적지가 변경되었거나 시간이 지났어요. 다시 요청해 주세요.'
    assert CATALOG[TEXT_IDS[text]] == text
    text = '목적지 확인 시간이 지났어요. 다시 요청해 주세요.'
    assert CATALOG[TEXT_IDS[text]] == text


@pytest.mark.parametrize('answers,audio_id', [
    ([], 'situation.no_response'),
    (['잘 모르겠어요'] * 3, 'situation.unclear'),
    (['도와주세요'], 'situation.help_needed'),
    (['도움은 필요 없어요'], 'situation.help_not_needed'),
    (['그냥 누워 있었어'], 'situation.resolved'),
])
def test_each_situation_outcome_keeps_its_exact_recorded_closing(answers, audio_id):
    from malbut_agent_server.situation_dialogue import MockSituationProvider, SituationDialogue

    dialogue = SituationDialogue(MockSituationProvider())
    dialogue.start('fixed-closing', 'fall', '낙상 여부 확인')
    if not answers:
        turn = dialogue.no_response()
    else:
        for answer in answers:
            turn = dialogue.answer(answer)
    assert turn.result is not None
    assert TEXT_IDS[turn.text] == audio_id
    assert CATALOG[audio_id] == turn.text


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
