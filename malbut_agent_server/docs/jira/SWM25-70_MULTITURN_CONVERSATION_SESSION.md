# SWM25-70 멀티턴 대화 세션

## 문서 정보

| 항목 | 값 |
| --- | --- |
| Jira 스토리 | SWM25-70 멀티턴 대화 세션 |
| 대상 패키지 | `malbut_agent_server` |
| 기준일 | 2026-08-02 |
| 현재 상태 | 인수 조건 10개 구현 및 관련 자동화 테스트 통과 |
| 운영 판정 | 단일 프로세스 MVP 사용 가능, 실 LLM·다중 프로세스 부하 검증 필요 |
| 저장 방식 | SQLite 기반 사용자·세션 격리 |

이 문서는 SWM25-70의 현재 구현을 코드와 자동화 테스트에 연결한 인수
기록이다. 세션 저장·순서·격리·중복 제거는 provider와 독립적으로 동작한다.
다만 `아까 말한 것`, `그 사람`, `그거`의 의미를 자연스럽게 해석하는 품질은
선택한 LLM에도 영향을 받으므로, 현재의 Mock 기반 회귀 테스트와 별도로 최종
provider 실측이 필요하다.

## 1. 목표

사용자가 로봇과 여러 차례 대화하면서 이전 대화의 문맥을 이어갈 수 있도록
한다. 동시에 사용자·세션 간 문맥 혼합, 동일 요청의 중복 처리, 동시 요청의
순서 뒤섞임, 종료·초기화·만료 이후의 늦은 응답 저장을 방지한다.

## 2. 범위

### 포함

- `conversation_id`, `turn_id`, `request_id`를 포함하는 대화 요청 계약
- 사용자별 SQLite 세션과 사용자·로봇 발화 저장
- 현재 세션에서 완료된 최근 N턴의 순서 보장
- 세션 생성·조회·초기화·종료·삭제
- 유휴 시간 기준 세션 만료
- 동일 요청의 내구성 있는 idempotency
- 같은 프로세스에서 들어온 동시 요청 직렬화
- reset·close·delete·만료와 추론 완료 사이의 경쟁 상태 차단
- 최근 원문과 오래된 대화 요약의 generation 경계

### 제외

- STT·TTS·VAD 등 음성 입출력
- 사람을 식별하는 얼굴 인식과 `person_id` 생성
- 장기 기억의 추출·동의·보존 정책
- LLM provider 자체의 한국어 대명사 해석 품질 보장
- 다중 서버·다중 프로세스 분산 세션 잠금
- ROS 2 Tool의 실제 실행

관련 기능 경계는
[`SWM25-69 대화·에이전트 계약`](SWM25-69_CONVERSATION_AGENT_CONTRACT.md)을
따른다.

## 3. 구성과 처리 흐름

```text
HTTP 클라이언트
  -> AgentRequest 스키마 검증
  -> AgentOrchestrator.handle()의 프로세스 내 직렬화
  -> SQLiteConversationStore.begin_turn()
       - 활성 세션 확인
       - 동일 request_id 재전송 확인
       - turn_id·pending 충돌 확인
       - 다음 ordinal 예약
       - 최근 N턴과 오래된 대화 요약 조회
  -> Provider 호출
  -> 로컬 SafetyPolicy 검증
  -> SQLiteConversationStore.complete_turn()
       - session_instance_id·generation·revision 재확인
       - 사용자·로봇 답변과 공개 응답 원자적 저장
       - revision·expires_at 갱신
  -> HTTP 응답
```

주요 구현 위치:

- 요청 검증: [`schemas.py`](../../malbut_agent_server/schemas.py#L168)
- 세션 저장소: [`conversation.py`](../../malbut_agent_server/conversation.py#L211)
- 대화 오케스트레이션:
  [`orchestrator.py`](../../malbut_agent_server/orchestrator.py#L207)
- HTTP 세션 API:
  [`http_server.py`](../../malbut_agent_server/http_server.py#L357)
- 런타임 제한:
  [`config.py`](../../malbut_agent_server/config.py#L99)
- 사용자 실행 설명: [`README.md`](../../README.md#L184)

## 4. 데이터 모델

### 4.1 `conversation_sessions`

세션의 논리 키는 `(user_id, conversation_id)`다.

| 필드 | 역할 |
| --- | --- |
| `user_id` | 사용자 범위와 데이터 격리 키 |
| `conversation_id` | 클라이언트가 이어서 사용할 대화 ID |
| `session_instance_id` | 삭제 후 같은 ID로 재생성해도 이전 세션과 구별하는 UUID |
| `status` | `active`, `closed`, `expired` 중 하나 |
| `generation` | reset·만료 시 증가하는 단기 문맥 경계 |
| `revision` | 완료 턴 및 생명주기 변경을 감지하는 비교·교환 버전 |
| `created_at`, `updated_at` | 생성·최근 완료 시각 |
| `expires_at` | 유휴 만료 시각 |

스키마와 제약은
[`conversation.py`](../../malbut_agent_server/conversation.py#L285)에 있다.

### 4.2 `conversation_turns`

한 행은 사용자 발화와 이에 대한 최종 로봇 답변 한 쌍이다.

| 필드 | 역할 |
| --- | --- |
| `turn_id` | 세션 generation 안의 클라이언트 턴 식별자 |
| `request_id` | 사용자 범위의 idempotency 식별자 |
| `request_fingerprint` | 전체 요청의 SHA-256 지문 |
| `ordinal` | 1부터 증가하는 대화 순서 |
| `status` | 추론 중 `pending`, 저장 완료 후 `completed` |
| `user_content` | 사용자 발화 |
| `assistant_content` | 최종 안전 검사를 반영한 로봇 답변 |
| `response_json` | 재전송 시 provider를 다시 호출하지 않고 반환할 공개 응답 |

DB는 다음 제약으로 중복과 순서 충돌을 차단한다.

- `(user_id, conversation_id, generation, turn_id)` 기본 키
- `(user_id, request_id)` 유일 제약
- `(user_id, conversation_id, generation, ordinal)` 유일 제약
- 세션당 `pending` 턴 하나만 허용하는 부분 유일 인덱스
- 세션 삭제 시 턴을 함께 삭제하는 foreign key cascade

구현 근거는
[`conversation.py`](../../malbut_agent_server/conversation.py#L308)와
[`conversation.py`](../../malbut_agent_server/conversation.py#L383)다.

### 4.3 `conversation_summaries`

최근 원문 N턴보다 오래된 완료 턴은 현재 `session_instance_id`와 generation에
결속된 별도 요약으로 관리한다. `summary_id`, revision, 원본 ordinal 범위,
원본 턴 수, digest, summarizer 이름과 시각을 저장한다. reset·삭제·만료 후
이전 요약을 현재 컨텍스트로 읽지 않는다.

구현 근거는
[`conversation.py`](../../malbut_agent_server/conversation.py#L349)다.

## 5. HTTP API

모든 POST 요청은 `application/json`을 사용하며 운영 시 Bearer 인증을
적용한다. MVP HTTP 서버는 `MALBUT_AGENT_USER_ID`로 지정한 한 사용자만
수락하지만, 내부 SQLite 저장소는 `user_id`별 격리를 지원한다.

| 메서드 | 경로 | 입력 핵심 필드 | 정상 결과 |
| --- | --- | --- | --- |
| `POST` | `/v1/conversations` | `user_id`, 선택 `conversation_id` | `201`, 활성 세션 생성·반환 |
| `POST` | `/v1/conversations/get` | `user_id`, `conversation_id`, 선택 `limit` | `200`, 세션·turns·messages·summary |
| `POST` | `/v1/conversations/reset` | `user_id`, `conversation_id` | `200`, 증가한 generation과 빈 문맥 |
| `POST` | `/v1/conversations/close` | `user_id`, `conversation_id` | `200`, `closed` 세션 |
| `POST` | `/v1/conversations/delete` | `user_id`, `conversation_id` | `200`, cascade 삭제 결과 |
| `POST` | `/v1/agent/respond` | 아래 대화 요청 계약 | `200`, 저장된 대화 결정 |

`/v1/agent/respond`의 세션 관련 필드는 모두 필수다.

```json
{
  "request_id": "req-001",
  "user_id": "local-user",
  "conversation_id": "conversation-001",
  "turn_id": "turn-001",
  "utterance": "내 이름은 신이야",
  "robot_state": {},
  "available_tools": []
}
```

세션 조회 응답의 `turns`는 사용자·로봇 한 쌍이고, `messages`는 각 발화를
`sequence` 순서로 펼친 목록이다. HTTP 구현은
[`http_server.py`](../../malbut_agent_server/http_server.py#L363)에서 확인할
수 있다.

관련 오류:

| HTTP 상태 | 오류 코드 | 의미 |
| ---: | --- | --- |
| `400` | `validation_error` | 필수 ID 누락, 형식 오류, 알 수 없는 필드 |
| `404` | `conversation_not_found` | 사용자 범위 안에서 세션을 찾지 못함 |
| `409` | `conversation_conflict` | ID 재사용, 다른 입력 재전송, 동시 pending 충돌 |
| `409` | `conversation_state` | closed·expired 세션에 새 턴 요청 |
| `409` | `conversation_changed` | 추론 중 세션 revision·generation·instance 변경 |

## 6. 인수 조건 점검

### 6.1 요청에 `conversation_id`와 `turn_id`를 추가한다

- [x] **완료**
- `AgentRequest.from_dict()`가 `request_id`, `conversation_id`, `turn_id`를
  필수로 검증하고 알 수 없는 요청 필드를 거절한다.
- 코드 근거:
  [`schemas.py`](../../malbut_agent_server/schemas.py#L168),
  [`http_server.py`](../../malbut_agent_server/http_server.py#L357)
- 통합 테스트 근거:
  [`test_conversation_lifecycle_and_follow_up_round_trip`](../../test/test_http_server.py#L201)

### 6.2 사용자와 로봇의 발화를 순서대로 저장한다

- [x] **완료**
- `complete_turn()`이 사용자 발화, 최종 로봇 답변, 공개 응답을 한 턴으로
  저장한다. `ordinal`과 `to_messages()`의 `sequence`가 발화 순서를 보장한다.
- 코드 근거:
  [`conversation.py`](../../malbut_agent_server/conversation.py#L80),
  [`conversation.py`](../../malbut_agent_server/conversation.py#L848)
- 테스트 근거:
  [`test_create_get_close_delete_lifecycle_and_ordered_messages`](../../test/test_conversation.py#L84),
  [`test_conversation_lifecycle_and_follow_up_round_trip`](../../test/test_http_server.py#L201)

### 6.3 같은 세션에서 최소 10턴의 대화 문맥을 유지한다

- [x] **완료**
- 기본 최근 원문 범위는 10턴이고 허용 설정 범위는 10~50턴이다. 10턴보다
  오래된 완료 대화는 별도 rolling summary로 이어진다.
- 코드 근거:
  [`conversation.py`](../../malbut_agent_server/conversation.py#L214),
  [`config.py`](../../malbut_agent_server/config.py#L275)
- 테스트 근거:
  [`test_history_keeps_latest_ten_completed_turns_in_order`](../../test/test_conversation.py#L141),
  [`test_provider_receives_latest_ten_turns_in_order`](../../test/test_orchestrator.py#L511)

### 6.4 “아까 말한 것”, “그 사람”, “그거” 같은 후속 표현을 처리한다

- [x] **MVP 완료**
- provider에 현재 세션의 순서화된 최근 대화를 넘긴다. Mock provider 회귀
  기준에서 `아까`, 사람 지시어, 결과 지시어가 현재 세션 발화로 해석된다.
- 코드 근거:
  [`orchestrator.py`](../../malbut_agent_server/orchestrator.py#L279)
- 테스트 근거:
  [`test_conversation_lifecycle_and_follow_up_round_trip`](../../test/test_http_server.py#L201),
  [`test_mock_resolves_person_and_result_follow_ups`](../../test/test_orchestrator.py#L555)
- 제한: 이 조건의 저장·전달 경로는 구현됐지만, 최종 실 LLM에서도 동일한
  한국어 후속 표현 평가를 반복해야 한다.

### 6.5 다른 사용자의 대화가 현재 세션에 섞이지 않는다

- [x] **완료**
- 모든 세션·턴·요약 조회 조건에 `user_id`가 포함된다. 동일한
  `conversation_id`와 `request_id`를 서로 다른 사용자가 사용해도 이력이
  섞이지 않는다.
- 코드 근거:
  [`conversation.py`](../../malbut_agent_server/conversation.py#L293),
  [`conversation.py`](../../malbut_agent_server/conversation.py#L1555)
- 테스트 근거:
  [`test_sessions_are_isolated_by_user_and_new_session_is_empty`](../../test/test_conversation.py#L177)

### 6.6 새 세션에서는 이전 단기 대화를 사용하지 않는다

- [x] **완료**
- 새 `conversation_id`는 빈 이력으로 시작한다. reset은 generation을
  증가시키고, 삭제 후 재생성은 새 `session_instance_id`를 사용한다.
  provider에는 현재 instance와 generation에 정확히 맞는 이력만 전달한다.
- 코드 근거:
  [`conversation.py`](../../malbut_agent_server/conversation.py#L1003),
  [`conversation.py`](../../malbut_agent_server/conversation.py#L1555)
- 테스트 근거:
  [`test_sessions_are_isolated_by_user_and_new_session_is_empty`](../../test/test_conversation.py#L177),
  [`test_reset_starts_new_generation_without_old_short_term_context`](../../test/test_conversation.py#L231)
- 주의: reset은 과거 완료 턴을 즉시 물리 삭제하는 기능이 아니라 현재
  generation에서 논리적으로 제외하는 기능이다. 물리 삭제는 delete API를
  사용한다.

### 6.7 세션 생성, 조회, 초기화, 종료, 삭제 기능을 제공한다

- [x] **완료**
- 다섯 생명주기 기능이 각각 HTTP endpoint와 저장소 메서드로 제공된다.
- 코드 근거:
  [`http_server.py`](../../malbut_agent_server/http_server.py#L363),
  [`conversation.py`](../../malbut_agent_server/conversation.py#L485)
- 테스트 근거:
  [`test_create_get_close_delete_lifecycle_and_ordered_messages`](../../test/test_conversation.py#L84),
  [`test_conversation_lifecycle_and_follow_up_round_trip`](../../test/test_http_server.py#L201)

### 6.8 일정 시간 사용하지 않은 세션을 자동 만료한다

- [x] **완료, lazy expiration 방식**
- 기본 TTL은 1,800초다. 조회는 만료 시각을 연장하지 않고, 완료된 새 턴만
  `expires_at`을 갱신한다. 저장소 접근 또는 `purge_expired()` 시 기한이 지난
  활성 세션을 `expired`로 전환한다.
- 코드 근거:
  [`conversation.py`](../../malbut_agent_server/conversation.py#L848),
  [`conversation.py`](../../malbut_agent_server/conversation.py#L1588)
- 테스트 근거:
  [`test_idle_expiry_is_exact_and_reads_do_not_extend_it`](../../test/test_conversation.py#L296),
  [`test_expiry_invalidates_in_flight_turn`](../../test/test_conversation.py#L328)
- 제한: 현재는 주기적인 background sweeper가 아니라 접근 시 만료를 확정하는
  방식이다. 유휴 상태에서도 DB status를 정시에 바꿔야 한다면 운영 scheduler가
  `purge_expired()`를 호출해야 한다.

### 6.9 동일 요청 재전송으로 답변이 중복 생성되지 않는다

- [x] **완료**
- 전체 요청 SHA-256 fingerprint와 `request_id`를 함께 저장한다. 동일 ID와
  동일 입력은 저장된 응답을 반환하여 provider를 다시 호출하지 않는다.
  동일 ID의 입력이 다르면 `409 conversation_conflict`로 거절한다. 이 기록은
  SQLite 재시작 후에도 유지된다.
- 코드 근거:
  [`orchestrator.py`](../../malbut_agent_server/orchestrator.py#L230),
  [`conversation.py`](../../malbut_agent_server/conversation.py#L1160)
- 테스트 근거:
  [`test_exact_retry_is_durable_and_changed_retry_conflicts`](../../test/test_conversation.py#L360),
  [`test_request_id_is_idempotent_and_cannot_change_input`](../../test/test_orchestrator.py#L309),
  [`test_concurrent_requests_are_ordered_and_exact_retry_runs_once`](../../test/test_orchestrator.py#L599)

### 6.10 동시에 들어온 요청의 대화 순서가 뒤섞이지 않는다

- [x] **단일 프로세스 MVP 완료**
- `AgentOrchestrator.handle()`은 프로세스 내 `RLock`으로 턴 처리를 직렬화한다.
  DB의 ordinal 유일 제약과 세션당 pending 한 개 제약이 중복 순서를 추가로
  차단한다. 추론 중 reset·close·delete·만료가 발생하면 instance,
  generation, revision 비교에 실패한 늦은 답변을 저장하지 않는다.
- 코드 근거:
  [`orchestrator.py`](../../malbut_agent_server/orchestrator.py#L207),
  [`conversation.py`](../../malbut_agent_server/conversation.py#L663),
  [`conversation.py`](../../malbut_agent_server/conversation.py#L848)
- 테스트 근거:
  [`test_concurrent_reservation_allows_only_one_in_flight_turn`](../../test/test_conversation.py#L507),
  [`test_concurrent_requests_are_ordered_and_exact_retry_runs_once`](../../test/test_orchestrator.py#L599),
  [`test_reset_during_inference_discards_late_answer`](../../test/test_orchestrator.py#L741)
- 제한: 여러 worker 프로세스·여러 서버 사이에서 도착 순서대로 대기시키는
  분산 큐는 아직 없다. 현재 DB 제약은 충돌을 안전하게 거절하지만 분산 요청을
  모두 순서대로 처리해 주지는 않는다.

## 7. 설정과 제한

| 환경 변수 | 기본값 | 허용 범위 | 의미 |
| --- | ---: | ---: | --- |
| `MALBUT_AGENT_CONVERSATION_TTL_SECONDS` | 1,800 | 60~2,592,000 | 완료 턴 이후 유휴 만료 시간 |
| `MALBUT_AGENT_CONVERSATION_HISTORY_LIMIT` | 10 | 10~50 | provider에 원문으로 전달할 최근 완료 턴 수 |
| `MALBUT_AGENT_CONVERSATION_SUMMARY_MAX_CHARS` | 2,000 | 256~8,000 | 저장할 rolling summary 문자 상한 |
| `MALBUT_AGENT_MAX_MODEL_INPUT_CHARS` | 20,000 | 4,096~1,000,000 | 모델 입력 컨텍스트 문자 예산 |
| `MALBUT_AGENT_MAX_CONVERSATION_SESSIONS` | 100 | 1~1,000 | 사용자당 저장할 세션 수 |
| `MALBUT_AGENT_MAX_CONVERSATION_TURNS` | 1,000 | 10~10,000 | generation당 완료 턴 수 |
| `MALBUT_AGENT_MAX_CONCURRENT_REQUESTS` | 8 | 1~64 | HTTP 동시 연결 상한 |
| `MALBUT_AGENT_REQUESTS_PER_MINUTE` | 60 | 1~10,000 | 전체 POST 요청 속도 상한 |

세션 조회의 `limit` 기본값은 100이고 저장소가 허용하는 범위는 1~500이다.
상세 기본값과 범위는
[`config.py`](../../malbut_agent_server/config.py#L99)와
[`config.py`](../../malbut_agent_server/config.py#L255)를 기준으로 한다.

## 8. 남은 위험과 다음 작업

1. **실 provider 후속 표현 평가**

   Mock에서 통과한 `아까`, `그 사람`, `그거`와 모호한 선행사가 최종 주력·
   fallback LLM에서도 안정적인지 동일 데이터로 최소 3회 반복한다.

2. **다중 worker 정책 결정**

   현재 오케스트레이터 lock은 프로세스 전역이라 서로 다른 세션도 직렬화한다.
   처리량이 필요하면 사용자·세션별 lock으로 좁히고, 여러 worker를 사용할
   경우 Redis queue, DB advisory lock 또는 단일 session owner를 설계한다.

3. **만료 sweeper 운영화**

   접근이 전혀 없는 세션도 정해진 시각에 DB 상태를 변경하고 보존 정책에 따라
   정리하려면 주기적인 `purge_expired()` 호출과 모니터링을 추가한다.

4. **보존·삭제 정책 확정**

   reset·만료는 과거 완료 턴을 현재 문맥에서 제외하지만 즉시 물리 삭제하지
   않는다. 개인정보 보존 기간, 백업 삭제, 사용자 삭제 요청 SLA를 정하고
   검증해야 한다.

5. **HTTP 다중 사용자 인증 경계**

   저장소는 사용자 격리를 지원하지만 현재 HTTP 프로세스는 한
   `MALBUT_AGENT_USER_ID`에 바인딩된다. 다중 사용자 운영 전에는 인증 주체와
   `user_id`를 서버가 결속하고 클라이언트가 임의 지정하지 못하게 해야 한다.

6. **ROS 2 대화 bridge 통합**

   STT 결과와 사용자 identity를 대화 요청에 결속하고, 네트워크 재시도에서도
   동일 `request_id`·`turn_id`를 재사용하도록 ROS bridge 계약을 추가한다.

## 9. 검증 기록과 재현 명령

아래 명령으로 SWM25-69 안전 계약과 SWM25-70 세션 회귀를 함께 실행한다.

```bash
cd ~/ros2_ws/src/malbut/malbut_agent_server

PYTHONPATH=. python3 -m pytest -q \
  test/test_conversation.py \
  test/test_http_server.py \
  test/test_orchestrator.py
```

최신 실행 결과는 PR의 검증 기록과 GitHub Actions를 기준으로 한다. 이
검증은 로컬 단위·HTTP 통합 테스트다. 실제 LLM API,
ROS 2 bridge, 다중 프로세스 부하 시험을 통과했다는 의미는 아니다.

2026-08-04 로컬 검증에서는 SWM25-69 안전 계약을 포함하여 총 57개 테스트가
통과했고 오류·실패·skip은 없었다. ROS 설치 결과의
`malbut-agent-server --provider mock --database :memory: --check`도 정상
종료했다.

전체 패키지 회귀는 저장소의 [`README.md`](../../README.md#L635)에 적힌 다음
명령으로 재현한다.

```bash
cd ~/ros2_ws/src/malbut

PYTHONPATH=malbut_agent_server \
python3 -m pytest -q malbut_agent_server/test
```
