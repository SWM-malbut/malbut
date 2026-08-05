# Malbut Agent Server

`malbut_agent_server`는 LLM과 로봇 실행 계층 사이의 안전 계약, 사용자별
멀티턴 세션, 제한된 대화·기억 컨텍스트를 제공하는 ROS 2 Python 패키지다.

SWM25-71까지의 실행 범위는 외부 네트워크를 사용하지 않는 `mock`
provider로 제한한다. 다음 기능을 로컬에서 검증할 수 있다.

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

실제 LLM provider, 공개 장기 기억 CRUD API, ROS Tool 실행기는 각각 후속
스토리에서 연결한다. 모델이 추론한 내용을 자동 저장하는 경로는 없으며,
현재 서버가 반환하는 Tool 제안은 물리 실행 명령이 아니다.

## 테스트

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server
PYTHONPATH=. python3 -m pytest -q test
```

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

## 문서

- [SWM25-69 대화·에이전트 계약](docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md)
- [SWM25-70 멀티턴 대화 세션](docs/jira/SWM25-70_MULTITURN_CONVERSATION_SESSION.md)
- [SWM25-71 사용자 컨텍스트 통합](docs/jira/SWM25-71_USER_CONTEXT_INTEGRATION.md)

실제 LLM의 한국어 후속 표현 품질, 다중 프로세스 분산 잠금, 주기적 만료
sweeper와 ROS 2 대화 bridge는 이 MVP의 운영 완료 범위가 아니다.
