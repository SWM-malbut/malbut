"""Six fixed historical-context cases; actual provider only, no robot adapters."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time

from malbut_agent_server.config import Settings
from malbut_agent_server.conversation import ConversationTurn
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.schemas import SpeechAgentRequest
from malbut_agent_server.tools import SPEECH_DELEGATED_TOOLS, select_tool_specs


STOPPED = {
    'runtime': {'state': 'STOPPED', 'ready': False}, 'voice': {'ready': True},
    'system': {'active_missions': []}, 'battery': None,
}
CASES = [
    ('same_status_question', '로봇 상태 알려 줘', '로봇 상태 알려 줘',
     '현재 로봇은 정지(STOPPED) 상태이고, 실행 중인 작업은 없어요. 배터리는 확인할 수 없어요.',
     'get_robot_status', STOPPED, 'get_robot_status'),
    ('still_patrolling', '아직 순찰 중이야?', '지금 뭐 하고 있어?',
     '로봇 기능은 켜져 있고, 순찰 작업 하나가 실행 중이에요.', 'get_robot_status',
     {'runtime': {'state': 'RUNNING'}, 'system': {'active_missions': [
         {'mission_id': 'synthetic-patrol', 'capability_id': 'patrol'}]}}, 'get_robot_status'),
    ('current_observations', '지금도 사람이 보여?', '지금 사람이 보여?',
     '최근 관측에 사람 한 명이 있었어요. 신원은 확인할 수 없어요.', 'get_robot_observations',
     {'observations': {'person_count': 1, 'current': True}}, 'get_robot_observations'),
    ('repeat_saved_maps', '저장된 지도 목록 보여 줘', '저장된 지도 목록 보여 줘',
     '저장된 지도는 home 하나예요.', 'list_saved_maps',
     {'maps': [{'id': 'home.yaml', 'name': 'home'}], 'last_selected_map': 'home.yaml'},
     'list_saved_maps'),
    ('repeat_homecam_status', '홈캠 카메라랑 마이크 설정 상태 알려 줘',
     '홈캠 카메라랑 마이크 설정 상태 알려 줘',
     '저장된 설정은 카메라 켜짐, 마이크 꺼짐이에요. 현재 정상 동작 여부는 별도 확인이 필요해요.',
     'get_homecam_status', {'cameraEnabled': True, 'microphoneEnabled': False,
                            'runtimeVerified': False}, 'get_homecam_status'),
    ('recall_previous_result', '새로 확인하지 말고 아까 조회한 결과만 다시 말해 줘',
     '로봇 상태 알려 줘', '당시 로봇 기능은 꺼져 있고, 실행 중인 작업은 없었어요.',
     'get_robot_status', STOPPED, 'message'),
]


def main():
    directory = Path(__file__).parent
    settings = Settings.from_env()
    provider = OpenAIResponsesProvider(
        api_key=settings.openai_api_key, model=settings.openai_model,
        base_url=settings.openai_base_url, timeout_seconds=60,
        max_output_tokens=settings.openai_max_output_tokens,
        reasoning_effort=settings.openai_reasoning_effort)
    names = [name for name in SPEECH_DELEGATED_TOOLS
             if name not in ('cancel_voice_mission', 'request_navigation')]
    names += ['get_weather', 'set_weather_location']
    specs = select_tool_specs(names)
    cases = []
    for case_id, utterance, prior_user, prior_answer, prior_tool, prior_result, expected in CASES:
        cases.append(dict(
            id=case_id, utterance=utterance, expected=expected,
            prior_user=prior_user, prior_answer=prior_answer,
            memory_context={'mode': 'answer_only', 'robot_operation_results': [{
                'tool': prior_tool, 'state': 'succeeded', 'message': prior_answer,
                'result': prior_result, 'publication': {},
            }]},
        ))
    (directory / 'repeat-query-cases.json').write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + '\n')

    def run(case):
        request = SpeechAgentRequest.from_dict(dict(
            request_id='synthetic-repeat-' + case['id'], user_id='synthetic-user',
            conversation_id='synthetic-' + case['id'], turn_id='two',
            utterance=case['utterance'], robot_state={}, available_tools=names))
        history = [ConversationTurn(
            conversation_id=request.conversation_id, user_id=request.user_id,
            session_instance_id='synthetic-session', turn_id='one',
            request_id='synthetic-prior-' + case['id'], request_fingerprint=case['id'],
            generation=0, ordinal=1, user_content=case['prior_user'],
            assistant_content=case['prior_answer'], response={},
            created_at=1.0, completed_at=2.0,
        )]
        start = time.monotonic()
        try:
            result = provider.complete(request, [], history, specs,
                                       memory_context=case['memory_context'])
            decision = result.decision
            actual = decision.tool_name if decision.type == 'tool_call' else decision.type
            return dict(id=case['id'], expected=case['expected'], actual=decision.to_dict(),
                        passed=actual == case['expected'], usage=result.usage.to_dict(),
                        response_model=result.model, latency_seconds=round(time.monotonic() - start, 3))
        except Exception as error:
            return dict(id=case['id'], expected=case['expected'], passed=False,
                        error_type=type(error).__name__,
                        latency_seconds=round(time.monotonic() - start, 3))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, cases))
    prompt_path = directory.parents[2] / 'malbut_agent_server' / 'prompting.py'
    payload = dict(
        model=provider.model, reasoning_effort=provider.reasoning_effort,
        prompt_sha256=hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        execution='provider-only; synthetic history; no database, ROS, cloud, settings or motion adapters',
        count=len(results), passed=sum(item['passed'] for item in results),
        usage={key: sum(item.get('usage', {}).get(key) or 0 for item in results)
               for key in ('input_tokens', 'output_tokens', 'total_tokens')},
        cost='API response does not report billed monetary cost', results=results)
    (directory / 'repeat-query-results.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in payload.items() if key != 'results'}))
    print(json.dumps(results, ensure_ascii=False))


if __name__ == '__main__':
    main()
