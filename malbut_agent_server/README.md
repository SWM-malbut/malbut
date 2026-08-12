# Malbut Agent Server

`malbut_agent_server`는 LLM과 로봇 실행 계층 사이의 안전 계약, 사용자별
멀티턴 세션, 제한된 대화·기억 컨텍스트, LLM provider 연결과
서버 소유 Tool capability 경계를
제공하는 ROS 2 Python 패키지다.

SWM25-72에서 오프라인 `mock`과 OpenAI Responses API를 같은
요청·응답 규격으로 연결했다. 다음 기능을 검증할 수 있다.

- `(user_id, conversation_id)` 단위 SQLite 세션 격리
- `request_id`, `turn_id` 기반 내구성 있는 중복 요청 방지
- 사용자·로봇 발화의 순서 저장과 최근 10턴 전달
- 세션 생성·조회·초기화·종료·삭제
- 유휴 만료와 reset·delete 중 늦게 도착한 응답 차단
- 같은 프로세스에서 동시에 들어온 요청의 직렬 처리
- `아까 말한 것`, `그 사람`, `그거`의 Mock 기반 후속 표현 회귀
- 최근 N턴 원문과 그 이전 대화의 결정론적 rolling summary 분리
- 사용자별 장기 기억의 별도 검색과 만료 항목 제외
- 전체 모델 입력 문자 제한, overflow fallback과 내용 없는 크기 메트릭
- 과거 대화·요약·기억을 `_untrusted` JSON 데이터로 직렬화
- OpenAI 구조화 응답·엄격한 Tool schema·사용량 메타데이터 정규화
- 유한 retry, backoff, circuit breaker와 옵션 모델 fallback
- API 오류 시 로봇 행동이 아닌 안전 응답으로 fail-closed
- 30개 한국어 고정 테스트셋·반복 실행·비용 추정 평가 CLI
- 서버 소유 capability registry와 요청 Tool 부분집합 계산
- 읽기 전용·명시적 시뮬레이션·제안 전용 Tool 모드 분리
- 인증된 capability 조회와 비부작용 Tool query API
- Tool 입력 schema, timeout, 결과 크기·상태 freshness 검증
- 프로세스 내 Tool query 중복 억제와 오류 원문 비공개
- 확인 근거·CAS·영속 멱등성·내용 없는 audit를 갖춘 장기 기억 core
- final transcript만 받는 비실행 음성 대화 경계와 TTS 취소 계약
- 최종 안전 응답을 제한된 visual cue로 바꾸는 비실행 감정 표현 정책

장기 기억 변경 core는 구현했지만 신뢰된 person identity와 확인 token이 필요한
공개 HTTP/ROS CRUD adapter는 열지 않았다. 실제 ROS 부작용 Tool 실행기도 후속
스토리에서 연결한다. 모델이 추론한 내용을 자동 저장하는 경로는 없다. 현재 서버는
`trusted_robot_state=False`, `MALBUT_AGENT_TOOL_MODE=proposal`이 기본이라
OpenAI 또는 Mock이 반환한 Tool 제안을 물리 실행하지 않는다.

## 테스트

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server
PYTHONPATH=. python3 -m pytest -q test
```

전체 계약은
[`docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md`](docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md)에
정리되어 있다. 여섯 연관 스토리의 책임 경계는 관리자 승인을 받았지만,
세부 ROS 타입·안전 임계값·Mock 시험은 SWM25-73~77에서 구현하고 검증하기
전까지 실행 가능한 물리 기능으로 취급하지 않는다.

승인 증거와 후속 구현 전 확인할 항목은
[`SWM25-69 인터페이스 승인 가이드`](docs/jira/SWM25-69_INTERFACE_APPROVAL_GUIDE.md)에
정리되어 있다. CI 통과나 PR 병합은 구현 근거이며 책임 경계 승인만으로
후속 물리 기능 구현이 완료되지는 않는다.

## Mock 서버 실행

먼저 설정과 DB 초기화를 검사한다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --provider mock \
  --database /tmp/malbut-agent-demo.sqlite3 \
  --check
```

서버를 실행한다. 기본 주소는 `http://127.0.0.1:8765`다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --provider mock \
  --database /tmp/malbut-agent-demo.sqlite3
```

세션을 만든다.

```bash
curl -X POST http://127.0.0.1:8765/v1/conversations \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "local-user",
    "conversation_id": "demo-conversation"
  }'
```

첫 번째 발화를 보낸다.

```bash
curl -X POST http://127.0.0.1:8765/v1/agent/respond \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "request-001",
    "user_id": "local-user",
    "conversation_id": "demo-conversation",
    "turn_id": "turn-001",
    "utterance": "내 이름은 신이야",
    "robot_state": {},
    "available_tools": []
  }'
```

같은 `request_id`와 동일한 입력을 재전송하면 저장된 응답을 반환하며 Mock을
다시 호출하지 않는다. 같은 ID로 다른 입력을 보내면 `409`로 거절한다.

## SWM25-73 Tool Gateway

`available_tools`는 클라이언트가 capability를 선언하는 필드가 아니다. 서버
registry가 허용한 목록을 이번 요청에서 더 좁히는 selector다. 모델과 safety
policy에는 다음 교집합만 전달된다.

```text
정적 Tool schema ∩ 서버 capability registry ∩ 요청 available_tools
```

현재 capability와 실행 가능 여부를 확인한다. 인증을 사용하는 서버라면 같은
Bearer 헤더를 추가해야 한다.

```bash
curl http://127.0.0.1:8765/v1/tools/capabilities
```

기본 `proposal` 모드에서 이동을 query해도 실제 Nav2 goal은 발행되지 않고
`confirmation_required`로 차단된다.

```bash
curl -X POST http://127.0.0.1:8765/v1/tools/query \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "tool-query-001",
    "user_id": "local-user",
    "tool_name": "navigate",
    "arguments": {"location": "거실"}
  }'
```

로컬 연결 시험에서만 시뮬레이션을 명시적으로 켤 수 있다. 이 모드는 LLM
provider 선택과 독립적이며 Mock provider를 선택했다고 자동으로 켜지지 않는다.

```bash
MALBUT_AGENT_TOOL_MODE=simulation \
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --provider mock \
  --database /tmp/malbut-agent-simulation.sqlite3
```

시뮬레이션 adapter는 결과에 `simulated=true`를 남기며 Nav2 goal, 사진 파일,
외부 알림을 만들지 않는다. `/v1/tools/query`는 읽기 전용 또는 이 Mock
시뮬레이션만 처리한다. confirmation, 실제 행동, `tool_call_id`, 재시작 뒤에도
유지되는 1회 소비와 취소·feedback은 SWM25-74 범위다.

현재 query cache는 프로세스 내 최대 256건으로 제한된다. adapter 응답
deadline이 지나도 이미 시작된 Python thread를 강제로 중단하지 못하므로,
73에서는 자체 I/O timeout이 있고 부작용이 없는 adapter만 연결한다.

`/v1/agent/respond`의 `execution.proposal_authorized`는 로컬 정책을 통과한
제안이라는 뜻일 뿐이다. `execution.authorized`와 `consume_once`는
SWM25-74 전까지 항상 `false`이고 `tool_call_id`는 `null`이다.

## SWM25-75~77 오프라인 계약

세 후속 스토리는 외부 장치나 유료 API를 호출하지 않는 범위에서 구현했다.

- SWM25-75: 확인된 장기 기억 create/update/delete, record CAS, 사용자별 영속
  revision, 재시작 후 멱등 replay와 내용 없는 audit
- SWM25-76: 원시 오디오를 받지 않는 final transcript 계약, 신뢰된
  사용자·세션 binding, self-echo 차단과 TTS barge-in/cancel 상태기계
- SWM25-77: 최종 Safety 응답의 결정적 visual cue, 긴급·privacy 우선 억제,
  TTL·빈도 제한·bounded process-local idempotency와 neutral fallback

각 기능의 대표 검사를 300회씩 반복하는 명령은 다음과 같다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
python3 scripts/run_swm25_75_77_stress.py \
  --iterations 300 \
  --output \
  docs/evaluations/artifacts/SWM25-75_77_300X_OFFLINE_2026-08-13.json
```

이 검증은 실제 사람 인식, STT/TTS, ROS, frontend renderer 또는 운영 성능
시험을 대신하지 않는다. 현재 완료 범위와 blocker는 각 스토리 문서와
300회 반복 보고서에 분리해 두었다.

## OpenAI 서버 실행

`.env.example`을 Git에서 제외되는 로컬 파일로 복사한 뒤 권한을 제한한다.

```bash
cp .env.example .env.local
chmod 600 .env.local
```

`.env.local`에서 `MALBUT_AGENT_PROVIDER=openai`, `OPENAI_API_KEY`,
`MALBUT_AGENT_AUTH_TOKEN`을 설정한다. API key는 코드·Git·명령행 인자에
넣지 않는다. 실측 기준 운영 후보는 `gpt-5.6-terra`, 저비용
fallback 후보는 `gpt-5.6-luna`다.

먼저 유료 API 호출 없이 설정을 검사한다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --env-file .env.local \
  --check
```

검사가 통과하면 서버를 실행한다. OpenAI 모드는 loopback bind와 HTTP
Bearer 인증을 모두 강제한다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.cli \
  --env-file .env.local
```

`/healthz`를 제외한 요청에는
`Authorization: Bearer <MALBUT_AGENT_AUTH_TOKEN>` 헤더가 필요하다.

## Provider 평가

오프라인 Mock 계약을 먼저 확인한다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.eval_runner \
  --provider mock \
  --repetitions 3 \
  --output /tmp/malbut-agent-mock-eval.json
```

실제 비교는 동일한 30개 테스트를 모델별 최소 3회 반복한다. 원문
발화·응답·API key는 보고서에 저장하지 않으며, 출력 JSON은
`0600` 권한으로 저장된다.

```bash
PYTHONPATH=. python3 -m malbut_agent_server.eval_runner \
  --provider openai \
  --model gpt-5.6-luna \
  --model gpt-5.6-terra \
  --repetitions 3 \
  --timeout-seconds 5 \
  --request-delay-seconds 0.1 \
  --env-file .env.local \
  --output /tmp/malbut-agent-openai-eval.json \
  --progress
```

## 합성 대화 흐름 기록

일반 평가 JSON은 개인정보 보호를 위해 발화·응답·원문 prompt를 저장하지
않는다. 요청부터 컨텍스트 선택, Mock 원결정, SafetyPolicy, 최종 응답과
DB 저장까지 사람이 읽어야 할 때는 합성 데이터 전용 trace를 실행한다.

```bash
PYTHONPATH=. python3 scripts/run_synthetic_conversation_trace.py
```

이 명령은 인메모리 SQLite와 결정론적 MockProvider만 사용하며 OpenAI,
ROS, 카메라, 파일 생성 Tool, 알림 전송을 호출하지 않는다. 전체 JSON은
`0600`으로, 사람이 읽기 쉬운 Markdown은 `0644`로 기록한다. 실제 사용자
대화나 운영 자격 증명을 이 trace에 넣으면 안 된다.

## 사용자 컨텍스트

모델 입력은 다음 영역을 서로 다른 데이터로 구성한다.

- `conversation_history_untrusted`: 현재 세션의 최근 완료 N턴 원문
- `conversation_summary_untrusted`: 최근 N턴 이전 구간의 rolling summary
- `memory_context_untrusted`: 현재 사용자에게 속한 활성 장기 기억
- `current_user_utterance`: 현재 요청의 사용자 발화

과거 세 영역 안의 `SYSTEM`, `developer`, Tool 호출 문장은 현재 명령으로
승격하지 않는다. 전체 입력은 기본 20,000자로 제한하며, 초과하면 선택
문맥을 줄인 뒤 현재 발화의 가능한 prefix를 보존한다. 응답의
`provider.context`에는 원문 대신 각 영역의 원본·포함 개수와 문자 수,
잘린 영역과 overflow 여부만 들어간다.

주요 설정은 다음과 같다.

| 환경 변수 | 기본값 | 허용 범위 |
| --- | ---: | ---: |
| `MALBUT_AGENT_MEMORY_LIMIT` | 5 | 1~10 |
| `MALBUT_AGENT_CONVERSATION_HISTORY_LIMIT` | 10 | 10~50 |
| `MALBUT_AGENT_CONVERSATION_SUMMARY_MAX_CHARS` | 2,000 | 256~8,000 |
| `MALBUT_AGENT_MAX_MODEL_INPUT_CHARS` | 20,000 | 4,096~1,000,000 |
| `MALBUT_AGENT_TIMEOUT_SECONDS` | 5 | 1~120 |
| `MALBUT_AGENT_PROVIDER_TOTAL_TIMEOUT_SECONDS` | 11 | 1~300 |
| `MALBUT_AGENT_PROVIDER_MAX_RETRIES` | 0 | 0~3 |
| `MALBUT_AGENT_TOOL_MODE` | `proposal` | `proposal`, `simulation` |
| `OPENAI_MODEL` | `gpt-5.6-terra` | 출력 가능한 공식 model ID |
| `OPENAI_FALLBACK_MODEL` | 빈 값 | 선택, 주력과 다른 model ID |
| `OPENAI_REASONING_EFFORT` | `none` | 지원 effort 값 |
| `OPENAI_MAX_OUTPUT_TOKENS` | 500 | 64~4,096 |

## 문서

- [SWM25-69 대화·에이전트 계약](docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md)
- [SWM25-69 인터페이스 승인 가이드](docs/jira/SWM25-69_INTERFACE_APPROVAL_GUIDE.md)
- [SWM25-70 멀티턴 대화 세션](docs/jira/SWM25-70_MULTITURN_CONVERSATION_SESSION.md)
- [SWM25-71 사용자 컨텍스트 통합](docs/jira/SWM25-71_USER_CONTEXT_INTEGRATION.md)
- [SWM25-72 LLM provider 연결](docs/jira/SWM25-72_LLM_PROVIDER_INTEGRATION.md)
- [SWM25-73 Agent Tool Gateway](docs/jira/SWM25-73_AGENT_TOOL_GATEWAY.md)
- [SWM25-75 장기 기억 오프라인 core](docs/jira/SWM25-75_LONG_TERM_MEMORY_INTEGRATION.md)
- [SWM25-76 음성 대화 오프라인 계약](docs/jira/SWM25-76_VOICE_CONVERSATION_PIPELINE.md)
- [SWM25-77 감정 표현 오프라인 계약](docs/jira/SWM25-77_EMOTION_EXPRESSION_INTEGRATION.md)
- [SWM25-72 OpenAI baseline 평가](docs/evaluations/SWM25-72_OPENAI_EVALUATION_2026-08-05.md)
- [SWM25-72 OpenAI post-fix parity 평가](docs/evaluations/SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md)
- [SWM25-69~74 구현 재검증·300회 반복 보고서](docs/evaluations/SWM25-69_74_REVALIDATION_2026-08-12.html)
- [SWM25-75~77 기능별 300회 반복 보고서](docs/evaluations/SWM25-75_77_300X_OFFLINE_2026-08-13.md)
- [합성 대화·컨텍스트 전체 흐름 기록](docs/evaluations/SYNTHETIC_CONVERSATION_TRACE_2026-08-13.md)
- [Malbut LLM Agent 구현·출시 승인 기준](docs/LLM_AGENT_IMPLEMENTATION_ACCEPTANCE_CRITERIA.md)

다중 프로세스 분산 잠금, Tool query cache의 재시작 후 보존, 주기적 만료
sweeper, 독립 provider 장애 fallback과 ROS 2 대화 bridge는 이 MVP의 운영
완료 범위가 아니다.
