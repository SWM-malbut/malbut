"""Fixed synthetic Korean intent checks; provider only, no runtime or adapters.

Run explicitly with PYTHONPATH=malbut_agent_server and OPENAI_API_KEY already
present. This script is never imported by normal tests or robot startup.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

from malbut_agent_server.config import Settings
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.schemas import AgentRequest
from malbut_agent_server.tools import SPEECH_DELEGATED_TOOLS, select_tool_specs


CASES = [
    ('status', '지금 로봇 상태가 어때?', ['get_robot_status'], {}),
    ('observation', '지금 사람이나 추적 상태가 보이는지 확인해 줘', ['get_robot_observations'], {}),
    ('maps', '저장된 지도 목록 보여 줘', ['list_saved_maps'], {}),
    ('zones', '설정된 이동 구역을 알려 줘', ['get_map_zones'], {}),
    ('follow', '나 좀 따라와 줄래?', ['request_follow_person'], {}),
    ('patrol', '집을 꼼꼼하게 순찰해 줘', ['request_patrol'], {'thoroughness': 'thorough'}),
    ('mapping', '새 지도를 office라는 이름으로 만들어 저장해 줘', ['request_mapping'], {'map_name': 'office'}),
    ('relocalize', '선택된 지도에서 내 위치를 다시 맞춰 줘', ['request_relocalization'], {'method': 'auto'}),
    ('global_search', '현재 지도에서 전역 탐색으로 로봇 위치를 찾아 줘', ['request_relocalization'], {'method': 'global_search'}),
    ('manual', '웹으로 수동 조종할 수 있게 켜 줘', ['request_manual_control'], {}),
    ('recovery', '고장 난 로봇 기능을 복구해 줘', ['request_recovery'], {}),
    ('wake', '로봇을 깨워 줘', ['wake_robot'], {'map': None}),
    ('standby', '음성 대화는 두고 로봇 기능만 대기시켜 줘', ['standby_robot'], {}),
    ('stop', '움직임 전부 멈춰', ['stop_robot_movement'], {}),
    ('stop_web', '웹에서 움직이던 것도 모두 멈춰 줘', ['stop_robot_movement'], {}),
    ('stop_dialect', '고마 움직이고 가만히 있어라', ['stop_robot_movement'], {}),
    ('negative', '지금은 나 따라오지 마', ['message', 'clarification', 'stop_robot_movement'], {}),
    ('quoted', "'나를 따라와'라는 말을 영어로 번역해 줘", ['message'], {}),
    ('hypothetical', '만약 순찰하라고 하면 어떻게 해?', ['message'], {}),
    ('future', '내일 세 시에 순찰해 줘', ['clarification', 'message', 'refusal'], {}),
    ('corrected_target', '거실로, 아니 주방으로 가 줘', ['request_navigation'], {'location': '주방'}),
    ('speaker_location', '내가 있는 여기로 와 줘', ['clarification'], {}),
    ('ambiguous_map', '저장된 지도 중 하나 골라서 써 줘', ['clarification', 'list_saved_maps', 'wake_robot'], {}),
    ('multiple', '나 따라오면서 새 지도도 만들어 줘', ['clarification'], {}),
    ('homecam_status', '홈캠 카메라랑 마이크 설정 상태 알려 줘', ['get_homecam_status'], {}),
    ('homecam_events', '최근 사람 감지 이벤트 다섯 개 보여 줘', ['get_homecam_events'], {'limit': 5, 'event_type': 'person'}),
    ('homecam_recordings', '최근 녹화 세 개를 확인해 줘', ['get_homecam_recordings'], {'limit': 3}),
    ('homecam_falls', '기록된 낙상 알림 두 개 보여 줘', ['get_homecam_falls'], {'limit': 2}),
    ('settings', '홈캠 마이크만 꺼 줘', ['update_homecam_settings'], {'microphoneEnabled': False,
        'cameraEnabled': None, 'monitoringEnabled': None, 'fallEnabled': None}),
    ('no_authority', '웹 소유자 동의 없이 홈캠 위임을 켜 줘', ['refusal', 'message'], {}),
    ('weather', '오늘 날씨가 어때?', ['get_weather'], {}),
    ('weather_location', '날씨 위치를 대전광역시 유성구 덕명동으로 바꿔 줘', ['set_weather_location'], {'location': '대전광역시 유성구 덕명동'}),
]


def main(result_name='results.json'):
    directory = Path(__file__).parent
    settings = Settings.from_env()
    provider = OpenAIResponsesProvider(
        api_key=settings.openai_api_key, model=settings.openai_model,
        base_url=settings.openai_base_url, timeout_seconds=60,
        max_output_tokens=settings.openai_max_output_tokens,
        reasoning_effort=settings.openai_reasoning_effort)
    names = [name for name in SPEECH_DELEGATED_TOOLS if name != 'cancel_voice_mission']
    names += ['get_weather', 'set_weather_location']
    specs = select_tool_specs(names)
    cases = [dict(id=case[0], utterance=case[1], expected=case[2], arguments=case[3]) for case in CASES]
    (directory / 'cases.json').write_text(json.dumps(cases, ensure_ascii=False, indent=2) + '\n')

    def run(case):
        request = AgentRequest.from_dict(dict(
            request_id='synthetic-' + case['id'], user_id='synthetic-user',
            conversation_id='synthetic-' + case['id'], turn_id='one',
            utterance=case['utterance'], robot_state={}, available_tools=names))
        start = time.monotonic()
        try:
            result = provider.complete(request, [], [], specs)
            decision = result.decision
            actual = decision.tool_name if decision.type == 'tool_call' else decision.type
            passed = (actual in case['expected'] and
                      all(decision.arguments.get(key) == value for key, value in case['arguments'].items()))
            return dict(case, actual=decision.to_dict(), passed=passed, usage=result.usage.to_dict(),
                        response_model=result.model, latency_seconds=round(time.monotonic() - start, 3))
        except Exception as error:
            # No transport headers, environment values or raw exception data.
            return dict(case, passed=False, error_type=type(error).__name__,
                        latency_seconds=round(time.monotonic() - start, 3))

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(run, cases))
    payload = dict(model=provider.model, reasoning_effort=provider.reasoning_effort,
                   execution='provider-only; no ROS, cloud, settings, or motion adapters',
                   count=len(results), passed=sum(item['passed'] for item in results),
                   usage={key: sum(item.get('usage', {}).get(key) or 0 for item in results)
                          for key in ('input_tokens', 'output_tokens', 'total_tokens')},
                   cost='API response does not report billed monetary cost', results=results)
    (directory / result_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in payload.items() if key != 'results'}))
    print(json.dumps([{'id': item['id'], 'actual': item.get('actual', {}).get('tool_name') or
                      item.get('actual', {}).get('type'), 'error_type': item.get('error_type')}
                     for item in results if not item['passed']], ensure_ascii=False))


if __name__ == '__main__':
    main()
