# Malbut Agent Server

`malbut_agent_server`는 LLM과 로봇 실행 계층 사이의 안전 계약과 사용자별
멀티턴 대화 세션을 제공하는 ROS 2 Python 패키지다.

SWM25-70의 실행 범위는 외부 네트워크를 사용하지 않는 `mock` provider로
제한한다. 다음 기능을 로컬에서 검증할 수 있다.

- `(user_id, conversation_id)` 단위 SQLite 세션 격리
- `request_id`, `turn_id` 기반 내구성 있는 중복 요청 방지
- 사용자·로봇 발화의 순서 저장과 최근 10턴 전달
- 세션 생성·조회·초기화·종료·삭제
- 유휴 만료와 reset·delete 중 늦게 도착한 응답 차단
- 같은 프로세스에서 동시에 들어온 요청의 직렬 처리
- `아까 말한 것`, `그 사람`, `그거`의 Mock 기반 후속 표현 회귀

실제 LLM provider, 장기 기억 API, ROS Tool 실행기는 각각 후속 스토리에서
연결한다. 현재 서버가 반환하는 Tool 제안은 물리 실행 명령이 아니다.

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

## 문서

- [SWM25-69 대화·에이전트 계약](docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md)
- [SWM25-70 멀티턴 대화 세션](docs/jira/SWM25-70_MULTITURN_CONVERSATION_SESSION.md)

실제 LLM의 한국어 후속 표현 품질, 다중 프로세스 분산 잠금, 주기적 만료
sweeper와 ROS 2 대화 bridge는 이 MVP의 운영 완료 범위가 아니다.
