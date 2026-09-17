# SWM25-73 Agent Tool Gateway

## 문서 정보

| 항목 | 내용 |
| --- | --- |
| Jira | SWM25-73 |
| 작성 기준일 | 2026-08-06 |
| 선행 계약 | SWM25-69~72 |
| 구현 범위 | 서버 소유 capability registry, read-only·Mock simulation Gateway |
| 상태 | 구현 및 자동화 검증 중 |

> SWM25-73은 실제 이동, 촬영 파일 저장, 외부 알림과 영구 데이터 변경을
> 실행하지 않는다. confirmation, `tool_call_id`, 영속적인 1회 소비, 실제
> Action feedback·취소는 SWM25-74가 소유한다.

## 1. 목표

LLM과 HTTP 클라이언트가 로봇의 실행 능력을 직접 선언하지 못하게 한다.
서버가 소유한 capability registry를 단일 진실 공급원으로 사용하고, 모델에
보이는 Tool 제안과 신뢰된 adapter 호출을 서로 다른 단계로 분리한다.

다음 세 모드를 사용한다.

| 코드 값 | 의미 | SWM25-73 실행 여부 |
| --- | --- | --- |
| `proposal_only` | 고수준 Tool 제안만 가능 | 실행 불가 |
| `read_only` | 신뢰된 상태를 변경 없이 조회 | adapter가 주입된 경우만 가능 |
| `simulation_only` | 부작용 없는 Mock simulation | 명시적 simulation profile에서만 가능 |

LLM provider와 Tool profile은 독립적이다. `mock` provider를 사용해도 Tool
simulation이 자동으로 활성화되지 않는다.

## 2. 신뢰 경계

```text
HTTP available_tools (신뢰하지 않는 selector)
                 │
                 ▼
정적 TOOL_SPECS ∩ 서버 CapabilityRegistry
                 │
        ┌────────┴────────┐
        ▼                 ▼
모델·Safety에 전달      /v1/tools/query
Tool 제안만 생성        schema·mode·adapter 재검증
                              │
               ┌──────────────┼───────────────┐
               ▼              ▼               ▼
           read_only   simulation_only   proposal_only
           신뢰 조회      Mock만 호출       호출 0회
```

필수 규칙:

- registry의 이름, mode, availability, timeout과 adapter는 서버 코드만 정한다.
- `available_tools`는 서버 목록을 좁힐 수만 있고 넓힐 수 없다.
- provider와 SafetyPolicy에는 같은 effective Tool 목록을 전달한다.
- provider에는 registry schema의 deep copy를 전달한다.
- 일반 HTTP의 `robot_state`를 adapter 실행 권한으로 사용하지 않는다.
- `/cmd_vel`, 모터 PWM, e-stop 해제, shell 실행은 등록할 수 없다.
- production 기본 profile에는 실행 adapter가 없다. L0·L1 조회는
  `read_only`지만 `executor_unavailable`, 부작용 Tool은 `proposal_only`다.
- `execution.proposal_authorized=true`도 실제 실행 권한이 아니다.

## 3. 현재 capability 분류

| Tool | 위험 | production 기본 | 명시적 simulation | 실제 부작용 |
| --- | --- | --- | --- | --- |
| `get_robot_status` | L0 | read-only, adapter 없음 | fresh Mock 상태 조회 | 없음 |
| `detect_pet` | L1 | read-only, adapter 없음 | privacy·freshness Mock | 없음 |
| `capture_photo` | L2 | 제안 전용 | 파일을 만들지 않는 Mock | 없음 |
| `send_notification` | L2 | 제안 전용 | 외부 전송하지 않는 Mock | 없음 |
| `navigate` | L3 | 제안 전용 | Nav2 goal을 발행하지 않는 Mock | 없음 |

`create_reminder`, `start_follow`, `stop_follow`, `express_emotion`은 현재
`TOOL_SPECS`에 등록되지 않았다. 이름을 직접 주입해도 `unknown_tool`로
차단된다.

실제 `get_robot_status` 또는 `detect_pet` read-only adapter는 다음 조건을
만족한 뒤 의존성 주입해야 한다.

- ROS topic·service를 핵심 LLM 서버에 하드코딩하지 않는다.
- 상태 변경, topic publish, 파일 생성, 외부 요청을 하지 않는다.
- 상태 출처와 timezone이 있는 `observed_at`을 반환한다.
- 상태와 감지 결과는 capability별 최대 age보다 오래되면 `stale_state`다.
- `detect_pet`은 카메라 접근 전 로컬 privacy 상태를 검사한다.
- 실제 adapter는 `ReadOnlyToolAdapter` marker를 명시적으로 구현해야 한다.

현재 factory는 실제 ROS adapter를 주입하지 않는다.

## 4. Registry 계약

`ToolCapability`의 현재 필드는 다음과 같다.

| 필드 | 검증 |
| --- | --- |
| `name` | 정적 `TOOL_SPECS`에 존재해야 하며 registry 안에서 유일 |
| `mode` | 세 mode 중 하나 |
| `available` | server-owned proposal availability |
| `adapter` | `proposal_only`에서는 연결 불가 |
| `timeout_seconds` | 0초 초과 10초 이하 |
| `max_result_bytes` | 128~65,536 bytes |
| `max_state_age_seconds` | 0초 초과 60초 이하 |

`navigate`, `capture_photo`, `send_notification`을 `read_only`로 위장하면 서버
시작 시 거절한다. 같은 이름에 adapter를 두 번 등록하거나 알려지지 않은
Tool을 등록해도 시작하지 않는다. simulation adapter는
`SimulationToolAdapter`, 조회 adapter는 `ReadOnlyToolAdapter` marker가 없으면
호출 전 등록 단계에서 거절한다.

adapter 출력은 Tool별 strict allowlist와 타입으로 재검증한다. 상태의 배터리
범위·boolean 필드, 감지의 privacy evidence·confidence, Mock 이동의
`nav2_goal_published=false`, 촬영·전송의 false evidence가 맞지 않으면
`adapter_failed`다.

Timeout은 SWM25-69 상세 계약과 동일하게 상태 1초, 이동 접수 2초, 감지 3초,
촬영·알림 5초를 registry에 표시한다.

Capability 공개 응답에는 adapter 객체, ROS endpoint, credential과 내부 예외가
들어가지 않는다.

## 5. 대화 제안 계약

`POST /v1/agent/respond` 요청 구조는 유지한다. 다음 식으로 effective 목록을
계산한다.

```text
effective = 요청 순서를 유지한
            request.available_tools ∩ server registry의 available names
```

- 빈 요청 목록은 빈 provider Tool 목록이 된다.
- 알 수 없거나 비활성인 요청 이름은 effective 목록에 추가되지 않는다.
- 알 수 없는 이름을 보냈다는 이유로 서버 capability가 생성되지 않는다.
- 모델이 effective 목록 밖 Tool을 반환하면 기존 SafetyPolicy가
  `unknown_tool` 또는 `tool_unavailable`로 refusal한다.
- Mock의 “무슨 기능이 있어?” 응답도 effective 목록만 설명한다.

응답의 실행 메타데이터는 다음 의미로 고정한다.

```json
{
  "execution": {
    "decision_id": "proposal UUID",
    "authorized": false,
    "proposal_authorized": true,
    "consume_once": false,
    "tool_call_id": null
  }
}
```

`proposal_authorized`는 신뢰된 상태와 로컬 policy를 통과한 모델 제안이라는
뜻이다. `/respond`는 Gateway를 자동 호출하지 않는다.

저장 응답 schema는 이 실행 의미 변경과 함께 v2로 기록한다. 기존 v1 응답은
대화 재시도를 위해 읽을 수 있지만, 캐시에서 복원한 `decision_id`도 실행
권한으로 사용하지 않는다.

## 6. HTTP API

### 6.1 `GET /v1/tools/capabilities`

`/healthz`와 달리 기존 Bearer 인증과 rate limit을 적용한다.

```json
{
  "source": "server_owned_registry",
  "revision": "swm25-73-v1",
  "runtime_mode": "production",
  "capabilities": [
    {
      "name": "navigate",
      "risk_level": "L3",
      "mode": "proposal_only",
      "available_for_proposal": true,
      "executable": false,
      "blocked_by": "confirmation_required",
      "timeout_ms": 1000
    }
  ]
}
```

### 6.2 `POST /v1/tools/query`

이 endpoint는 read-only 또는 명시적 side-effect-free simulation 전용이다.
실제 행동 실행 endpoint가 아니다.

이 query는 대화의 `decision_id`, 현재 턴 intent 또는 confirmation과 결합하지
않는다. 따라서 서버가 신뢰한 비부작용 adapter에만 사용할 수 있다. 대화
제안을 실제 행동으로 전환하는 경로는 SWM25-74 전까지 존재하지 않는다.

```json
{
  "request_id": "tool-query-001",
  "user_id": "local-user",
  "tool_name": "get_robot_status",
  "arguments": {}
}
```

성공 예시:

```json
{
  "result_id": "UUID",
  "request_id": "tool-query-001",
  "tool_name": "get_robot_status",
  "mode": "simulation_only",
  "status": "succeeded",
  "result": {
    "simulated": true,
    "source": "malbut_mock_adapter",
    "observed_at": "2026-08-06T00:00:00Z",
    "battery_percent": 80.0,
    "emergency_stop": false,
    "subsystems_ok": true
  },
  "error": null,
  "cached": false
}
```

SWM25-73 결과에는 `tool_call_id`가 없다. confirmation 필드를 추가하면 알 수
없는 요청 필드로 거절한다.

### 6.3 오류 코드

잘 구성된 query의 정책·adapter 실패는 HTTP 200 안의 terminal 결과로
정규화한다. envelope 형식·인증·ID 재사용 충돌은 HTTP 오류다.

| 코드 | 의미 | adapter 호출 |
| --- | --- | ---: |
| `unknown_tool` | 정적 Tool 목록에도 없음 | 0회 |
| `tool_unavailable` | 알려진 Tool이지만 현재 registry에 없거나 비활성 | 0회 |
| `confirmation_required` | proposal 또는 production의 simulation Tool | 0회 |
| `invalid_arguments` | strict Tool schema 불일치 | 0회 |
| `executor_unavailable` | 허용 mode지만 신뢰 adapter 없음 | 0회 |
| `stale_state` | 상태 timestamp가 freshness 제한 초과 | 1회 조회 후 폐기 |
| `timed_out` | adapter 응답 deadline 초과 | 늦은 결과는 응답·cache를 덮어쓰지 않음 |
| `adapter_failed` | 예외, 비 JSON, 크기·결과 검증 실패 | 성공 처리 안 함 |
| `request_conflict` | 같은 ID를 다른 사용자·Tool·인자로 재사용 | HTTP 409, 두 번째 호출 0회 |

내부 예외 메시지와 traceback은 응답에 넣지 않는다.

## 7. 중복 요청과 동시성

Gateway는 `request_id`와 `(user_id, tool_name, arguments)` fingerprint를
비교한다.

- 같은 ID와 같은 입력의 순차·동시 재전송은 같은 `result_id`를 반환한다.
- cache에 보존된 동안 adapter는 프로세스 안에서 1회만 호출한다.
- 같은 ID와 다른 입력은 `409 request_conflict`다.
- 차단, timeout과 실패 결과도 cache해 자동 재호출하지 않는다.
- cache는 최대 256건의 메모리 LRU이며 재시작하면 사라진다.
- LRU에서 제거된 오래된 ID는 다시 호출될 수 있으므로 실제 부작용의
  idempotency 근거로 사용할 수 없다.
- cache된 상태는 과거 동일 요청의 결과다. 현재 상태가 필요하면 새 request
  ID를 사용하고 `observed_at`을 다시 검증한다.

이는 조회·Mock simulation을 위한 제한된 중복 억제다. 실제 부작용의
영속적·원자적 1회 소비는 SWM25-74에서 DB에 구현한다.

## 8. Profile 설정

기본값:

```dotenv
MALBUT_AGENT_TOOL_MODE=proposal
```

로컬 simulation을 명시적으로 선택할 때만 다음 값을 사용한다.

```dotenv
MALBUT_AGENT_TOOL_MODE=simulation
```

`simulation` profile의 Mock adapter는 다음을 보장한다.

- `navigate`: `nav2_goal_published=false`
- `capture_photo`: `image_created=false`
- `send_notification`: `delivered=false`
- 모든 결과: `simulated=true`

Gateway deadline은 HTTP terminal 결과를 고정하지만 Python에서 이미 실행 중인
adapter thread를 강제로 종료하지는 못한다. 따라서 SWM25-73 adapter는 반드시
비부작용이어야 하고 자체 I/O timeout을 가져야 한다. 실제 ROS Action의
cooperative cancel과 late-result 처리는 SWM25-74에서 구현한다.

`MALBUT_AGENT_TOOL_MODE=physical` 같은 값은 provider가 Mock이어도 서버 검증
단계에서 거절한다.

## 9. SWM25-74 이관 범위

- 사용자·세션·제안·인자·만료에 묶인 confirmation 증거
- 확인된 제안의 별도 `tool_call_id` 발급
- 재시작 뒤에도 유지되는 원자적 1회 소비
- 실제 ROS Service·Action adapter와 최신 safety 상태 재검증
- 물리 이동, 파일 저장, 외부 전송과 영구 변경
- pending/running/terminal 상태, feedback, timeout과 cancel
- 늦은 실제 성공 결과 폐기와 운영 감사 로그

이 항목이 완료되기 전에는 `proposal_only`를 production 부작용 실행으로
바꾸지 않는다.

## 10. 달성 조건

- [x] 서버 소유 capability registry를 구현했다.
- [x] 클라이언트 Tool 목록이 registry를 넓히지 못한다.
- [x] provider와 SafetyPolicy가 같은 effective 목록을 사용한다.
- [x] 위험 Tool을 read-only로 위장하거나 proposal adapter를 연결할 수 없다.
- [x] Mock의 capability 설명이 실제 effective 목록만 사용한다.
- [x] strict 인자 검증 뒤에만 adapter를 호출한다.
- [x] Tool별 strict 출력 타입·크기와 조회 freshness를 검증한다.
- [x] marker 없는 임의 adapter를 등록 단계에서 차단한다.
- [x] production에서 simulation adapter 호출이 0회다.
- [x] simulation이 실제 Nav2·파일·외부 전송을 만들지 않는다.
- [x] 응답 timeout·stale·예외·잘못된 결과를 fail-closed로 정규화한다.
- [x] cache에 보존된 동일 요청의 순차·동시 adapter 호출이 1회다.
- [x] 서로 다른 요청은 bounded worker에서 병렬 처리한다.
- [x] capability API와 query API에 기존 인증·rate limit을 적용했다.
- [x] `/respond`가 Gateway를 자동 호출하지 않는다.
- [x] `execution.authorized=false`, `consume_once=false`,
      `tool_call_id=null`로 73과 74의 경계를 명시했다.
- [x] 전체 pytest·flake8·pep257 회귀 검증이 통과했다.
- [ ] CI가 통과했다.

## 11. 구현 파일

- `malbut_agent_server/gateway.py`: registry, modes, query, Mock adapter
- `malbut_agent_server/tools.py`: strict Tool argument validation
- `malbut_agent_server/orchestrator.py`: effective Tool 교집합과 proposal metadata
- `malbut_agent_server/http_server.py`: capability·query endpoint
- `malbut_agent_server/config.py`, `factory.py`: 독립 Tool profile
- `test/test_gateway.py`: registry·mode·idempotency·failure 검증

SWM25-73 완료는 “모델이 Tool 이름을 잘 골랐다”가 아니라, 서버 policy 밖의
adapter 호출과 실제 부작용이 자동화 검증에서 0회임을 의미한다.
