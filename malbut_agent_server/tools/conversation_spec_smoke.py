#!/usr/bin/env python3
"""Opt-in synthetic dialogue checks through the production OpenAI orchestrator.

Run from the repository root with PYTHONPATH=malbut_agent_server. No real user
dialogues, robot actions, or production databases are read. Weather is a fixture.
Answers and rubric checks need review; this is not a general quality guarantee.
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from malbut_agent_server.config import DEFAULT_OPENAI_MODEL, Settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.prompting import CONVERSATION_INSTRUCTIONS, SYSTEM_INSTRUCTIONS
from malbut_agent_server.schemas import AgentRequest


CASES = [
    ('location_missing', {}, ['내일 날씨 어때?'], 'location_required',
     '지역을 묻고 임의의 예보를 만들지 않는다.'),
    ('listen_experience', {'initiative': '주로 들어줘'},
     ['오늘 오랜만에 친구 만나서 좋았어.'], None, '경험에 반응하고 질문을 강요하지 않는다.'),
    ('active_experience', {'initiative': '적극적으로 이어줘'},
     ['오늘 처음 빵을 구워봤는데 재밌더라.'], None, '경험과 관련된 후속 대화를 제안한다.'),
    ('listen_scope', {}, ['지금은 해결책 말고 그냥 들어줘.',
                          '요즘 일이 쌓여서 힘들어.'], None,
     '다음 발화에서도 해결책 나열 없이 공감한다.'),
    ('correction', {}, ['민수와 화요일 오후 3시에 도서관에서 만나기로 했어.',
                         '요일만 목요일로 바꿨어.', '언제 어디서 누구와 만난다고 했지?'], None,
     '목요일, 오후 3시, 도서관, 민수를 함께 유지한다.'),
    ('topic_return', {}, ['신입생 대상 소개 글을 짧고 친근한 반말로 써줘. 주제는 도서관 이용이야.',
                          '고양이가 가르랑거리는 이유가 뭐야?', '아까 소개 글 계속 쓰자.'], None,
     '신입생, 도서관, 짧은 길이, 반말을 유지하며 소개 글을 이어간다.'),
    ('unknown_detail', {}, ['나는 커피보다 차를 더 좋아해.', '내가 어떤 종류의 차를 좋아한다고 했어?'],
     None, '알려주지 않은 차 종류를 만들지 않는다.'),
    ('end_despite_active', {'initiative': '적극적으로 이어줘'}, ['이제 좀 쉬고 싶어.'],
     None, '후속 질문과 새 주제 제안 없이 마무리한다.'),
    ('weather_failure', {}, ['그래서 내일 비가 와?'], 'unavailable',
     '조회 실패를 알리고 강수 여부를 만들지 않는다.'),
    ('one_answer_tone', {'tone': '편안한 존댓말'},
     ['이번 답변만 반말로 해줘. 무지개는 왜 생겨?', '달은 왜 모양이 바뀌어 보여?'], None,
     '첫 답변만 반말이고 다음 답변은 존댓말로 돌아온다.'),
    ('partial_answer', {}, ['비가 생기는 원리를 알려주고 내일 우리 동네 비 오는지도 알려줘.'],
     'location_required', '설명 가능한 원리는 답하고 모르는 지역만 확인한다.'),
    ('refuse_missing_info', {}, ['어디 사는지는 말하고 싶지 않아. 지역 없이 비 오는 날 산책 팁만 알려줘.'],
     None, '지역을 다시 요구하지 않고 제공한 정보 범위에서 답한다.'),
    ('long_explanation', {}, ['상대성이론을 처음 듣는 사람도 알 수 있게 자세하게 설명해줘.',
                             '응, 계속 설명해줘.'], None,
     '자연스러운 구간에서 계속 들을지 확인하고, 이어갈 때 처음부터 반복하지 않는다.'),
    ('multiple_conditions', {}, [
        '행사는 목요일 오후 3시 도서관에서 민수와 신입생 8명이 모여. '
        '참가비는 무료고 민수가 귤을 준비해. 견과류는 가져오면 안 돼. 이 내용으로 공지할 거야.',
        '장소만 학생회관 2층으로 바꿨어. 다른 조건은 그대로야.',
        '확정된 조건을 빠뜨리지 말고 세 문장짜리 공지로 써줘.',
    ], None, '목요일 3시, 학생회관 2층, 민수, 신입생 8명, 무료, 귤 담당, 견과류 금지를 3문장에 반영한다.'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly allow OpenAI API calls')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append', help='Run only these case IDs')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; this command uses the configured OpenAI key')
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not key:
        parser.error('OPENAI_API_KEY is not configured')
    model = os.environ.get('OPENAI_MODEL', DEFAULT_OPENAI_MODEL)
    reasoning_effort = os.environ.get('OPENAI_REASONING_EFFORT', Settings.openai_reasoning_effort)
    results = {'created_at': datetime.now(timezone.utc).isoformat(), 'model': model,
               'reasoning_effort': reasoning_effort,
               'synthetic_only': True, 'weather_is_fixture': True,
               'conversation_instructions': CONVERSATION_INSTRUCTIONS,
               'system_instructions_sha256': hashlib.sha256(
                   SYSTEM_INSTRUCTIONS.encode()).hexdigest(), 'cases': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case_id, preferences, utterances, weather_status, rubric in CASES:
        if args.case and case_id not in args.case:
            continue
        case = {'id': case_id, 'preferences': preferences, 'rubric': rubric, 'turns': []}
        with tempfile.TemporaryDirectory(prefix='malbut-conversation-eval-') as directory:
            runtime = build_orchestrator(Settings(
                provider='openai', openai_api_key=key, openai_model=model,
                openai_reasoning_effort=reasoning_effort,
                database_path=str(Path(directory) / 'conversation.sqlite3'),
                request_timeout_seconds=30, provider_total_timeout_seconds=61,
            ), http_server=False)
            try:
                case['api_responses'] = []
                adapter = runtime.provider._providers[0]
                transport = adapter.transport

                def observed_transport(*values):
                    response = transport(*values)
                    case['api_responses'].append({
                        'status': response.get('status'),
                        'incomplete_details': response.get('incomplete_details'),
                        'usage': response.get('usage'),
                        'output': response.get('output'),
                    })
                    return response

                adapter.transport = observed_transport
                runtime.personal_memory.set_initial_settings('synthetic-user', preferences)
                session = runtime.conversation_store.create('synthetic-user')
                reads = []
                if weather_status:
                    def weather(request_id):
                        reads.append(request_id)
                        return {'status': weather_status}
                    runtime.weather_executor = weather
                for utterance in utterances:
                    request = AgentRequest.from_dict({
                        'user_id': 'synthetic-user', 'conversation_id': session.conversation_id,
                        'request_id': str(uuid.uuid4()), 'turn_id': str(uuid.uuid4()),
                        'utterance': utterance, 'robot_state': {},
                        'available_tools': ['get_weather'] if weather_status else [],
                    })
                    started = time.monotonic()
                    try:
                        answer = runtime.handle(request)
                        case['turns'].append({
                            'user': utterance, 'decision': answer.decision.to_dict(),
                            'usage': asdict(answer.provider_result.usage),
                            'elapsed_seconds': round(time.monotonic() - started, 3),
                        })
                    except Exception as error:
                        case['turns'].append({'user': utterance, 'error_type': type(error).__name__})
                        break
                case['weather_read_count'] = len(reads)
            finally:
                runtime.close()
        results['cases'].append(case)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
        print(case_id, 'completed', len(case['turns']), flush=True)


if __name__ == '__main__':
    main()
