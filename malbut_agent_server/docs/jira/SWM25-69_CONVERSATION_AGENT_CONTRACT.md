# SWM25-69 대화·에이전트 계약 정의

## 문서 정보

| 항목 | 값 |
| --- | --- |
| Jira 스토리 | SWM25-69 대화&에이전트 계약 정의 |
| 계약 버전 | 0.3-draft |
| 대상 시스템 | Malbut 반려로봇, ROS 2 Humble, Jetson Orin NX 8GB |
| 계약 소유 | SWM25-69 |
| 상태 | 연관 스토리 담당자 검토 대기 |
| 최종 승인 조건 | SWM25-27·34·36·38·40·41 인터페이스 담당자 승인 |

이 문서는 SWM25-69가 소유하는 대화·추론·오케스트레이션 경계와 다른
로봇 스토리가 소유하는 감지·실행 경계를 정의한다. 문서에 `제안`으로
표시된 인터페이스는 현재 저장소에 구현되어 있지 않으며, 담당자 합의 없이
확정 계약으로 취급하지 않는다.

## 1. 목표

사용자가 Malbut과 자연스럽게 대화하고 로봇 기능을 요청할 수 있도록 하되,
LLM은 고수준 행동만 제안하고 실제 실행 권한은 신뢰 가능한 로컬 ROS
상태와 각 기능의 실행 노드가 갖도록 한다.

SWM25-69는 다음을 소유한다.

- 대화 세션과 단기 문맥 관리
- LLM provider 호출과 응답 정규화
- 사용자 발화의 대화·질문·행동 요청 분류
- 등록된 고수준 Tool 선택과 인자 생성
- 모호한 요청에 대한 재질문
- 사용자 확인 흐름
- 안전 정책의 1차 검증
- Tool 실행 상태를 자연어 응답으로 변환
- 장기 기억 검색 결과를 신뢰되지 않은 문맥으로 사용하는 작업

SWM25-69는 다음을 소유하지 않는다.

- STT·TTS·VAD의 음성 신호 처리 구현
- 얼굴 인식과 `person_id` 생성
- 리마인더 저장·스케줄링·전달
- 표정·소리·몸짓 렌더링
- 사람 추적·주행 제어
- 긴급 호출의 감지·판정·연락 실행
- 모터, 조향, 속도, 비상 정지 장치의 저수준 제어

## 2. 지원 대화 유형

### 2.1 일상 자유 대화

- 일반적인 인사, 안부, 가벼운 질의에 자연어로 답한다.
- 로봇 행동이 필요하지 않으면 Tool을 호출하지 않는다.
- 알 수 없는 사실을 기억이나 센서로 확인하지 못하면 추측하지 않는다.
- 한국어 사용자에게는 기본적으로 간결한 한국어로 답한다.

완료 예시:

- 사용자: `안녕, 오늘 기분 어때?`
- 결과: Tool 호출 없이 자연어 메시지를 반환한다.

### 2.2 로봇 상태 질문

- 배터리, 도킹, 카메라 프라이버시, 위치 추정 등 현재 로봇 상태를 묻는
  질문은 `get_robot_status`를 사용한다.
- 모델의 사전 지식이나 HTTP 클라이언트가 주장한 상태로 답하지 않는다.
- 상태가 오래됐거나 확인되지 않으면 `모름`으로 답한다.

완료 예시:

- 사용자: `배터리 얼마나 남았어?`
- 결과: 최신 ROS 상태 조회 결과를 근거로 답한다.

### 2.3 기능 실행 요청

- 자연어 요청을 등록된 고수준 Tool과 엄격한 인자로 변환한다.
- Tool이 없거나 현재 사용할 수 없으면 실행을 가장하지 않는다.
- 버전 1에서는 대화 한 턴당 최대 한 개의 Tool만 제안한다.
- 여러 단계가 필요한 요청은 단계를 설명하고 각 단계마다 다시 승인한다.

완료 예시:

- 사용자: `거실로 가줘.`
- 결과: 허용된 목적지와 최신 ROS 상태를 확인한 뒤 `navigate`를 제안한다.

### 2.4 기억 기반 대화

- 단기 대화와 사람별 장기 기억을 구분한다.
- 장기 기억은 사용자별로 격리하고 신뢰되지 않은 참고 문맥으로 취급한다.
- 모델이 추론한 사실을 자동으로 장기 기억에 저장하지 않는다.
- 장기 기억의 생성·수정·삭제는 사용자의 명시적 확인을 필요로 한다.

완료 예시:

- 사용자: `우리 강아지 이름이 뭐였지?`
- 결과: 현재 사용자에게 속한 확인된 기억이 있을 때만 그 내용을 답한다.

### 2.5 모호한 요청에 대한 재질문

- 목적지, 대상, 시간, 연락 대상 등 필수 인자가 모호하면 Tool을 호출하지
  않고 `clarification`을 반환한다.
- 사용자가 확인하기 전에는 모델이 임의의 기본값을 선택하지 않는다.

완료 예시:

- 사용자: `거기로 가줘.`
- 결과: `어느 장소로 갈까?`라고 질문하고 이동하지 않는다.

### 2.6 멀티턴 단기 문맥 — SWM25-70 후속 계약

- 요청은 `request_id`, `conversation_id`, `turn_id`를 모두 포함한다.
- 서버가 `(user_id, conversation_id)`로 세션을 소유하며 클라이언트가 보낸
  대화 이력 배열은 받지 않는다.
- 사용자 발화와 최종 안전 응답을 한 쌍으로 SQLite에 순서대로 저장한다.
- provider에는 현재 generation에서 완료된 최근 N턴 원문과 그보다 오래된
  턴의 요약을 서로 다른 데이터로 전달한다. 기본 N은 10이며 10~50 사이로
  설정할 수 있다.
- `아까 말한 것`, `그 사람`, `그거` 같은 후속 표현은 현재 세션의 문맥만
  사용하고 근거가 여러 개면 재질문한다.
- 다른 사용자나 다른 세션의 단기 문맥을 합치지 않는다.
- reset은 generation을 증가시키고 이전 단기 문맥을 즉시 숨긴다.
- close·delete·유휴 만료 뒤에는 새 턴을 받지 않는다.
- 동일한 `request_id`와 동일 입력의 재전송은 저장된 답변을 반환한다.
  ID는 같지만 입력이 다르면 충돌로 거절한다.
- 같은 세션의 동시 턴은 직렬화한다. 추론 중 reset·close·delete·만료가
  발생하면 늦게 도착한 답변을 폐기한다.

SWM25-70에서 구현·검증할 목표 HTTP 세션 경계:

| 메서드 | 경로 | 결과 |
| --- | --- | --- |
| `POST` | `/v1/conversations` | 생성 또는 활성 세션 반환 |
| `POST` | `/v1/conversations/get` | 세션, turns, 순서화된 messages 조회 |
| `POST` | `/v1/conversations/reset` | 새 generation과 빈 단기 문맥 |
| `POST` | `/v1/conversations/close` | 세션 종료 |
| `POST` | `/v1/conversations/delete` | 세션과 턴 삭제 |

목표 기본 유휴 만료는 1,800초이며 단순 조회는 활동 시간을 연장하지 않고,
완료된 새 턴만 만료 시각을 갱신한다. 이 절의 저장·동시성 동작은
SWM25-70 브랜치의 구현과 테스트를 통과하기 전까지 제공 기능으로 간주하지
않는다.

### 2.7 장기 대화 컨텍스트 윈도우 — SWM25-71 후속 계약

최근 대화 원문, 오래된 대화의 요약, 사람별 장기 기억은 서로 다른
데이터로 저장하고 모델 입력에서도 분리한다.

| 영역 | 저장·선택 정책 | 모델 입력 키 |
| --- | --- | --- |
| 최근 대화 | 현재 세션의 완료된 최근 N턴 원문, 기본 N=10 | `conversation_history_untrusted` |
| 대화 요약 | 최근 N턴보다 오래된 완료 턴의 rolling summary | `conversation_summary_untrusted` |
| 장기 기억 | 현재 사용자 범위에서 별도 검색한 확인된 기억 | `memory_context_untrusted` |
| 현재 요청 | 현재 HTTP 요청의 사용자 발화 한 개 | `current_user_utterance` |

대화 요약은 외부 LLM을 추가 호출하지 않는 결정론적 로컬 extractive
summarizer가 생성한다. 요약과 함께 다음 출처를 저장한다.

- `summary_id`
- `session_instance_id`와 generation
- summary revision
- 원본 시작·끝 turn ordinal과 원본 턴 수
- 원본 연쇄 digest
- summarizer 이름과 fallback 여부
- 생성 시각과 마지막 갱신 시각

요약 범위는 최근 원문 범위와 겹치거나 비어서는 안 된다. 예를 들어 완료된
턴이 13개이고 N=10이면 요약은 1~3번 턴, 최근 원문은 4~13번 턴을
담당한다. 재시작 시 N이 달라지면 저장된 완료 턴에서 요약 경계를 새 N에
맞게 재구성한 뒤 provider를 호출한다.

기본 대화 컨텍스트 문자 예산은 system instruction과 직렬화된 컨텍스트
데이터를 합쳐 20,000자다. 상한 초과 시 선택 문맥을 결정론적으로 줄이고,
JSON escaping까지 계산해 현재 발화의 가장 긴 안전한 prefix를 보존하므로
문맥 크기만으로 요청이 실패하지 않는다. Responses Tool schema와
structured-output schema는 이 예산에 포함되지 않는 고정 오버헤드이며 실제
token 사용량은 provider usage로 별도 측정한다. 환경 변수
`MALBUT_AGENT_MAX_MODEL_INPUT_CHARS`로 4,096~1,000,000자 사이에서
설정한다. 저장할 요약 상한은
`MALBUT_AGENT_CONVERSATION_SUMMARY_MAX_CHARS`로 256~8,000자 사이에서
설정하며 기본값은 2,000자다. 현재 provider에 전달하는 요약 내용의 내부
상한도 2,000자다.

최근 대화, 대화 요약, 장기 기억은 모두 신뢰되지 않은 데이터다. 그 안의
명령문, 역할 변경, system·developer 메시지 표시, Tool 호출 요청과 안전
규칙 우회 문구를 실행 지시로 취급하지 않는다. Tool 결정에는 prompt와
별개인 로컬 current-turn intent gate를 적용한다. 현재 사용자 발화에 해당
행동과 대상의 명시적 근거가 없으면 `current_turn_intent_missing`으로
차단한다. 부정·금지 표현은 fail-closed로 처리하고, 알림 문구의 모든
내용은 현재 발화에 결속한다. 신뢰된 미디어 레지스트리가 없는 동안
알림의 이미지 첨부는 허용하지 않는다.

응답의 `provider.context`는 내용을 포함하지 않고 다음 측정값만 제공한다.

- 최근 대화의 원본·포함 턴 수와 문자 수
- 요약 ID, 원본 턴 수, 저장된·포함 요약 문자 수
- 장기 기억의 원본·포함 항목 수와 문자 수
- 현재 발화의 원본·포함 문자 수
- 전체 모델 입력 문자 수와 상한
- 잘린 영역과 overflow fallback 사용 여부

reset은 generation을 바꾸고 기존 요약을 제거한다. delete와 유휴 만료도
요약을 제거하며, 읽을 때는 현재 `session_instance_id`와 generation에
정확히 일치하는 문맥만 사용한다. 따라서 삭제·만료된 대화나 삭제 후 같은
ID로 재생성한 과거 세션의 대화가 다시 모델 입력에 포함되지 않는다. 이
절은 SWM25-71의 목표 계약이며 해당 브랜치의 구현과 테스트를 통과하기
전까지 현재 제공 기능으로 간주하지 않는다.

## 3. 책임 경계

| 기능 | SWM25-69 책임 | 연관 스토리 책임 |
| --- | --- | --- |
| 음성 명령 | 확정된 텍스트를 대화 요청으로 처리 | SWM25-34가 STT·TTS와 음성 세션 제공 |
| 긴급 호출 | 긴급 이벤트를 대화 문맥에 반영하고 안내 | SWM25-27이 독립적으로 감지·판정·연락 |
| 사람별 기억 | `person_id` 범위에서 검색하고 확인 흐름 제공 | SWM25-36이 기억 정책·저장 계약 제공 |
| 리마인더 | 내용·시간을 명확히 하고 Tool 호출 | SWM25-38이 저장·스케줄·알림 실행 |
| 감정 표현 | 제한된 감정 태그 제안 | SWM25-40이 표정·소리·몸짓으로 렌더링 |
| 따라다니기 | 명시적 대상과 사용자 확인을 받아 Tool 호출 | SWM25-41이 추적·주행·중단·안전 실행 |

SWM25-31의 소리 감지와 SWM25-32의 사람 구분 감지는 각각 음성 세션과
`person_id`의 상위 입력 의존성이다. SWM25-69는 이 감지 결과를 직접
생성하지 않는다.

## 4. 공통 대화 결정 계약

모델이 반환할 수 있는 결정은 다음 네 종류로 제한한다.

| 결정 | 의미 | 실행 가능 여부 |
| --- | --- | --- |
| `message` | 일반 자연어 답변 | 실행 없음 |
| `clarification` | 필수 정보 재질문 | 실행 없음 |
| `refusal` | 안전·권한·정책에 따른 거절 | 실행 없음 |
| `tool_call` | 등록된 고수준 Tool 제안 | 로컬 안전 검사 후에만 가능 |

현재 공통 결정 구조:

```json
{
  "type": "tool_call",
  "message": "거실로 이동할게.",
  "tool_name": "navigate",
  "arguments": {
    "location": "거실"
  },
  "reason": "",
  "confidence": 0.95,
  "expires_in_ms": 5000
}
```

규칙:

- `tool_name`은 등록된 Tool allowlist에 있어야 한다.
- Tool 인자는 Tool별 스키마와 정확히 일치해야 한다.
- 추가 필드는 허용하지 않는다.
- 기본 결정 유효 시간은 5초이며 최대 10초를 넘을 수 없다.
- 모델의 `tool_call`은 제안일 뿐 실행 승인이 아니다.
- 일반 HTTP 요청의 `robot_state`는 신뢰하지 않는다.

## 5. 공통 Tool 실행 결과 계약

연관 스토리의 실행 노드는 다음 공통 결과 형태를 제공하는 것을 제안한다.

```json
{
  "tool_call_id": "uuid",
  "tool_name": "navigate",
  "status": "succeeded",
  "started_at": "2026-07-31T12:00:00+09:00",
  "completed_at": "2026-07-31T12:00:15+09:00",
  "result": {},
  "error": null
}
```

허용 상태:

- `pending`
- `running`
- `succeeded`
- `failed`
- `cancelled`
- `timed_out`

공통 오류 구조:

```json
{
  "code": "localization_unavailable",
  "message": "로봇 위치를 확인할 수 없습니다.",
  "retryable": true
}
```

실행기는 `tool_call_id`를 원자적으로 한 번만 소비해야 한다. 같은 ID의
재전송은 새 행동을 시작하지 않고 기존 상태를 반환해야 한다.

## 6. Tool 목록

### 6.1 구현된 모델 Tool

| Tool | 역할 | 구현 상태 |
| --- | --- | --- |
| `get_robot_status` | 로봇 상태 조회 | 스키마·안전 검증 구현, ROS 실행기 미연결 |
| `navigate` | 이름이 확인된 목적지로 이동 | 스키마·안전 검증 구현, Nav2 실행기 미연결 |
| `detect_pet` | 현재 카메라에서 반려동물 확인 | 스키마·안전 검증 구현, 감지 노드 미연결 |
| `capture_photo` | 프라이버시 검사 후 사진 촬영 | 스키마·안전 검증 구현, 촬영 실행기 미연결 |
| `send_notification` | 등록된 보호자에게 문자 알림 | 텍스트 검증 구현, 알림 실행기 미연결 |

### 6.2 연관 스토리와 합의할 제안 Tool

| Tool | 담당 스토리 | 상태 |
| --- | --- | --- |
| `create_reminder` | SWM25-38 | 제안, 합의 필요 |
| `start_follow` | SWM25-41 | 제안, 합의 필요 |
| `stop_follow` | SWM25-41 | 제안, 합의 필요 |
| `express_emotion` | SWM25-40 | 제안, 합의 필요 |

긴급 호출은 일반 LLM Tool 목록에 넣지 않는다. SWM25-27의 독립 경로가
긴급 처리를 시작하고, 에이전트는 그 결과 이벤트를 받아 사용자에게
설명하는 역할만 맡는다.

## 7. Tool별 상세 계약

### 7.1 `get_robot_status`

목적: 신뢰 가능한 최신 ROS 상태를 읽는다.

입력:

```json
{}
```

제안 출력:

```json
{
  "observed_at": "2026-07-31T12:00:00+09:00",
  "battery_percent": 74.0,
  "docked": false,
  "navigation_available": true,
  "localization_ok": true,
  "camera_available": true,
  "privacy_mode": false,
  "emergency_stop": false
}
```

| 항목 | 계약 |
| --- | --- |
| 사용자 확인 | 필요 없음 |
| 응답 timeout | 1초 |
| 주요 오류 | `state_unavailable`, `stale_state`, `executor_unavailable`, `timed_out` |
| 안전 규칙 | 상태 생성 시각을 포함하며 오래된 상태를 현재 상태처럼 답하지 않음 |

### 7.2 `navigate`

목적: 검증된 Nav2 경로를 통해 이름이 확인된 목적지로 이동한다.

입력:

```json
{
  "location": "거실"
}
```

제안 출력:

```json
{
  "goal_id": "uuid",
  "location": "거실",
  "status": "running"
}
```

| 항목 | 계약 |
| --- | --- |
| 사용자 확인 | 물리 이동이므로 항상 확인 필수 |
| 접수 timeout | 2초 |
| 실행 timeout | 기본 120초, 실행 노드에서 설정 가능 |
| 주요 오류 | `location_not_allowed`, `forbidden_zone`, `navigation_unavailable`, `localization_unavailable`, `battery_unknown`, `battery_low`, `emergency_stop`, `timed_out` |
| 안전 규칙 | 최신 ROS 상태, 목적지 allowlist, 금지구역, 배터리, e-stop을 실행 직전에 재검증 |

`navigate`는 `/cmd_vel`을 발행하지 않는다. SWM25-69는 Nav2 action을
직접 소유하지 않고 별도 실행 어댑터에 고수준 목적지를 전달한다.

### 7.3 `detect_pet`

목적: 로봇을 움직이지 않고 현재 카메라 프레임에서 반려동물을 확인한다.

입력:

```json
{}
```

제안 출력:

```json
{
  "detected": true,
  "confidence": 0.91,
  "observed_at": "2026-07-31T12:00:00+09:00"
}
```

| 항목 | 계약 |
| --- | --- |
| 사용자 확인 | 명시적 요청은 추가 확인 없음 |
| 응답 timeout | 3초 |
| 주요 오류 | `privacy_mode`, `camera_unavailable`, `stale_frame`, `detector_unavailable`, `timed_out` |
| 안전 규칙 | 프라이버시 모드에서는 실행하지 않음 |

### 7.4 `capture_photo`

목적: 프라이버시 검사를 통과한 현재 카메라 프레임을 한 장 저장한다.

입력:

```json
{}
```

제안 출력:

```json
{
  "image_id": "user-scoped-uuid",
  "captured_at": "2026-07-31T12:00:00+09:00"
}
```

| 항목 | 계약 |
| --- | --- |
| 사용자 확인 | 명시적 촬영 요청은 추가 확인 없음. 능동·추론 촬영은 확인 필수 |
| 응답 timeout | 5초 |
| 주요 오류 | `privacy_mode`, `camera_unavailable`, `stale_frame`, `storage_unavailable`, `timed_out` |
| 안전 규칙 | 이미지 ID는 사용자 범위 미디어 저장소에서만 유효해야 함 |

현재는 사용자 범위 미디어 저장소가 없으므로 촬영 이미지의 외부 전송을
허용하지 않는다.

### 7.5 `send_notification`

목적: 등록된 보호자에게 짧은 텍스트 알림을 보낸다.

입력:

```json
{
  "message": "초코가 거실에서 감지됐어요.",
  "image_id": null
}
```

제안 출력:

```json
{
  "notification_id": "uuid",
  "delivered": true
}
```

| 항목 | 계약 |
| --- | --- |
| 사용자 확인 | 외부 전송이므로 메시지와 수신자를 보여주고 항상 확인 |
| 응답 timeout | 5초 |
| 주요 오류 | `recipient_unavailable`, `sensitive_notification`, `image_attachment_unverified`, `privacy_mode`, `timed_out` |
| 안전 규칙 | 비밀·인증정보 전송 차단, 메시지 최대 500자 |

현재 버전에서는 `image_id`가 `null`이 아닌 요청을 차단한다.

### 7.6 `create_reminder` — 제안

목적: 사용자가 확인한 내용과 시각으로 리마인더를 생성한다.

입력:

```json
{
  "content": "약 먹기",
  "trigger_at": "2026-08-01T08:00:00+09:00"
}
```

제안 출력:

```json
{
  "reminder_id": "uuid",
  "content": "약 먹기",
  "trigger_at": "2026-08-01T08:00:00+09:00",
  "status": "scheduled"
}
```

| 항목 | 계약 |
| --- | --- |
| 사용자 확인 | 내용·날짜·시각·시간대를 다시 읽고 항상 확인 |
| 응답 timeout | 3초 |
| 주요 오류 | `ambiguous_time`, `invalid_time`, `time_in_past`, `scheduler_unavailable`, `timed_out` |
| 안전 규칙 | 확인 전 저장 금지, 서버 시간대가 아닌 사용자 시간대 포함 |

SWM25-38 담당자와 생성·수정·취소 인터페이스 및 전달 실패 정책을 합의해야
한다.

### 7.7 `start_follow` — 제안

목적: 확인된 한 사람을 따라가는 세션을 시작한다.

입력:

```json
{
  "person_id": "person-scoped-id"
}
```

제안 출력:

```json
{
  "follow_session_id": "uuid",
  "person_id": "person-scoped-id",
  "status": "running"
}
```

| 항목 | 계약 |
| --- | --- |
| 사용자 확인 | 항상 필요 |
| 접수 timeout | 2초 |
| 대상 획득 timeout | 10초 |
| 주요 오류 | `person_not_identified`, `person_confidence_low`, `tracking_unavailable`, `navigation_unavailable`, `battery_low`, `emergency_stop`, `target_lost`, `timed_out` |
| 안전 규칙 | 최신 사람 인식·ROS 안전 상태 필요, 대상 상실·e-stop·낙하 위험 시 즉시 중단 |

SWM25-69는 카메라 좌표나 속도를 생성하지 않는다. SWM25-41이 추적과
주행 제어 및 정지 조건을 소유한다.

### 7.8 `stop_follow` — 제안

목적: 현재 사람 따라가기 세션을 중단한다.

입력:

```json
{
  "follow_session_id": null
}
```

`follow_session_id`가 `null`이면 현재 사용자의 활성 세션을 대상으로 한다.

제안 출력:

```json
{
  "follow_session_id": "uuid",
  "status": "cancelled"
}
```

| 항목 | 계약 |
| --- | --- |
| 사용자 확인 | 필요 없음 |
| 응답 timeout | 2초 |
| 주요 오류 | `follow_session_not_found`, `executor_unavailable`, `timed_out` |
| 안전 규칙 | 중단 요청은 시작 요청보다 우선하며 LLM 재판단 없이 전달 |

### 7.9 `express_emotion` — 제안

목적: SWM25-40에 제한된 감정 표현을 요청한다.

입력:

```json
{
  "emotion": "happy",
  "duration_ms": 3000
}
```

허용 감정:

- `neutral`
- `happy`
- `concerned`
- `excited`
- `apologetic`

제안 출력:

```json
{
  "emotion": "happy",
  "status": "succeeded"
}
```

| 항목 | 계약 |
| --- | --- |
| 사용자 확인 | 필요 없음 |
| 응답 timeout | 1초 |
| 주요 오류 | `emotion_not_supported`, `renderer_unavailable`, `timed_out` |
| 안전 규칙 | 이동 베이스를 제어하지 않음, 표현 시간 상한 5초 |

## 8. 사용자 확인 정책

### 8.1 행동 위험 등급

| 등급 | 의미 | 기본 정책 |
| --- | --- | --- |
| L0 | 조회 또는 위험을 줄이는 즉시 중단 | 확인 없이 실행 |
| L1 | 되돌릴 수 있는 낮은 위험 행동 | 명시적 요청이면 실행하고 결과 안내 |
| L2 | 개인정보, 외부 전송, 영구 저장·삭제 | 최종 내용을 보여주고 명시적 확인 |
| L3 | 이동·추적처럼 물리적 위험이 있는 행동 | 명시적 확인 후 최신 ROS 안전 상태 재검사 |
| L4 | 긴급 호출처럼 안전과 외부 연락에 영향을 주는 행동 | LLM 외부의 전용 안전 정책으로 처리 |

| Tool 또는 기능 | 위험 등급 | 확인 정책 |
| --- | ---: | --- |
| `get_robot_status` | L0 | 불필요 |
| `detect_pet` | L1 | 명시적 요청이면 추가 확인 없음 |
| `capture_photo` | L2 | 명시적 촬영 요청을 확인으로 인정하며 능동 촬영은 별도 확인 |
| `send_notification` | L2 | 메시지와 등록 수신자를 보여주고 항상 확인 |
| `navigate` | L3 | 목적지를 보여주고 항상 확인 |
| `create_reminder` | L2 | 내용·날짜·시각·시간대를 보여주고 항상 확인 |
| `start_follow` | L3 | 대상을 보여주고 항상 확인 |
| `stop_follow` | L0 | 확인 없이 즉시 처리 |
| `express_emotion` | L0 | 불필요 |
| 긴급 호출 | L4 | SWM25-27 전용 정책 적용 |

### 8.2 행동별 확인 매트릭스

| 행동 | 명시적 사용자 요청 | 에이전트 추론·능동 실행 | 모호한 요청 |
| --- | --- | --- | --- |
| 상태 조회 | 즉시 | 즉시 | 재질문 |
| 반려동물 감지 | 즉시 | 프라이버시 정책 확인 | 재질문 |
| 사진 촬영 | 즉시 | 확인 필수 | 재질문 |
| 텍스트 알림 | 확인 필수 | 확인 필수 | 재질문 |
| 이미지 알림 | 현재 차단 | 현재 차단 | 차단 |
| 허용된 목적지 이동 | 확인 필수 | 확인 필수 | 재질문 |
| 리마인더 생성 | 확인 필수 | 확인 필수 | 재질문 |
| 사람 따라가기 시작 | 확인 필수 | 확인 필수 | 재질문 |
| 사람 따라가기 중단 | 즉시 | 안전상 필요하면 즉시 | 즉시 |
| 감정 표현 | 즉시 | 정책 범위에서 즉시 | 기본 `neutral` |
| 긴급 호출 | SWM25-27 전용 정책 | SWM25-27 전용 정책 | LLM이 지연시키지 않음 |

명시적 요청은 현재 대화 턴에서 사용자가 행동과 대상을 직접 지정한 경우를
뜻한다. 과거 대화, 장기 기억, 감지 이벤트만으로 행동을 추론한 경우에는
명시적 요청으로 보지 않는다.

### 8.3 확인 토큰 규칙

사용자 확인은 다음을 포함해야 한다.

- 확인한 사용자와 `person_id`
- `conversation_id`와 확인 발화의 `turn_id`
- 실행할 행동
- 최종 Tool 이름과 정규화된 전체 인자
- 대상·목적지·시간·등록 연락 대상
- 개인정보·카메라 사용 여부
- 취소 방법
- 확인 만료 시간

추가 규칙:

- `네` 같은 짧은 응답은 동일 세션에 대기 중인 행동이 정확히 하나일 때만
  확인으로 인정한다.
- 인자가 하나라도 변경되면 기존 확인을 무효화하고 다시 확인한다.
- 확인 토큰은 한 번만 원자적으로 소비할 수 있다.
- 확인 후에도 실행 직전 최신 ROS 안전 상태를 다시 검사한다.
- 만료·취소된 확인은 행동에 재사용하지 않는다.
- `stop_follow`, 비상 정지 등 위험을 줄이는 행동에는 확인을 요구하지 않는다.
- 화면과 음성에는 내부 Tool 이름 대신 사용자가 이해할 수 있는 실제 행동을
  설명한다.

확인 응답은 짧은 유효 시간을 가지며 다른 행동에 재사용할 수 없다.

## 9. 오류와 timeout 정책

### 9.1 공통 오류 코드

| 코드 | 의미 | 기본 처리 |
| --- | --- | --- |
| `invalid_arguments` | Tool 인자가 스키마와 다름 | 실행 없이 재질문 |
| `unknown_tool` | 등록되지 않은 Tool | 실행 차단 |
| `tool_unavailable` | 현재 제공되지 않는 Tool | 대안 안내 |
| `confirmation_required` | 사용자 확인 필요 | 확인 질문 |
| `confirmation_expired` | 확인 유효 시간 만료 | 다시 확인 |
| `untrusted_robot_state` | 신뢰된 ROS 상태 없음 | 실행 차단 |
| `stale_state` | ROS 상태가 너무 오래됨 | 상태 갱신 후 재시도 |
| `emergency_stop` | 비상 정지 상태 | 모든 행동 차단 |
| `privacy_mode` | 카메라·마이크 프라이버시 모드 | 해당 센서 행동 차단 |
| `timed_out` | 정해진 시간 안에 완료되지 않음 | 작업 취소 후 안내 |
| `cancelled` | 사용자 또는 안전 계층이 취소 | 취소 결과 안내 |
| `executor_unavailable` | 담당 실행 노드 미동작 | 실행하지 않고 안내 |
| `provider_error` | LLM provider 오류 | Tool 실행 없이 fallback |

### 9.2 timeout 기준

| 구간 | 기준 |
| --- | --- |
| LLM provider 전체 응답 | 기본 30초 |
| 행동 결정 승인 TTL | 기본 5초, 최대 10초 |
| Tool 접수 | 1~5초, Tool별 계약 적용 |
| Nav2 이동 | 기본 120초 |
| 따라가기 대상 획득 | 10초 |
| 사용자 확인 | 제품 UX 담당자와 합의 필요 |

timeout이 발생하면 실행 상태를 `timed_out`으로 확정하고, 늦게 도착한 성공
응답으로 새 행동을 시작하지 않는다.

### 9.3 HTTP·안전·Tool 오류 구분

- 요청 형식, 인증, rate limit, 모델 provider 장애는 HTTP 오류로 반환한다.
- 모델이 제안한 행동이 로컬 정책에 막힌 경우에는 정상 HTTP 응답 안에
  `decision.type=refusal`과 `safety.code`를 반환한다.
- Tool 실행기가 작업을 접수한 뒤 발생한 실패는 공통 Tool 실행 결과의
  `status`와 `error`로 반환한다.
- 사용자에게는 내부 예외나 비밀값을 노출하지 않고 복구 가능한 다음 행동만
  설명한다.

### 9.4 현재 구현과 목표 계약의 차이

| 항목 | 현재 구현 | 목표 계약 |
| --- | --- | --- |
| Tool 실행 | 실제 실행기 없음 | ROS adapter가 공통 상태·결과·취소 제공 |
| HTTP 행동 승인 | 안전 정책이 외부 상태를 기본 불신하며, 이 PR에는 HTTP endpoint 없음 | ROS-owned 상태 공급자만 승인 계산에 사용 |
| 상태 freshness | `observed_at`, sequence, source 없음 | 생성 시각·순번·출처와 최대 age 검증 |
| capability 목록 | HTTP 요청의 `available_tools` 사용 | ROS-owned capability registry가 제공 |
| 사용자 확인 | 확인 토큰·endpoint 없음 | 사용자·세션·Tool·인자·만료에 묶인 1회성 확인 |
| 중복 실행 방지 | `consume_once` 선언만 존재 | 실행기가 `tool_call_id`를 영속·원자적으로 소비 |
| 실행 timeout·취소 | 없음 | Tool별 timeout, 취소와 terminal status 제공 |
| 읽기 전용 상태 조회 | 전역 신뢰 상태·e-stop 검사에 함께 차단 | L0 조회·중단은 안전한 범위에서 허용 |
| 사람별 사용자 | 요청의 `user_id` 형식만 검증, 인증·바인딩은 미구현 | 신뢰된 `person_id` 기반 격리 |
| 단기 대화 세션 | SWM25-70·71 목표 계약만 정의, 이 PR에는 저장소·HTTP 구현 없음 | SQLite lifecycle·TTL·retry와 음성 `speaker_id` 결합 |

목표 계약을 코드에 반영하기 전에는 공개 HTTP 경로에서 물리 행동을
승인하지 않는다.

## 10. 긴급 호출 독립 경로

긴급 호출은 다음 경로를 사용해야 한다.

```text
긴급 버튼·키워드·센서
        │
        ▼
SWM25-27 로컬 긴급 판정기
        │
        ├── 로컬 경보·연락 실행
        ├── 취소·오탐 처리
        └── 긴급 상태 이벤트 발행
                    │
                    ▼
        SWM25-69가 사용자에게 상태 설명
```

필수 규칙:

- LLM provider 장애나 인터넷 단절이 긴급 호출을 막아서는 안 된다.
- LLM은 긴급 이벤트를 무시·취소·지연할 수 없다.
- LLM은 `emergency_stop`을 해제할 수 없다.
- 물리 SOS 버튼과 확정된 로컬 긴급 키워드는 LLM을 거치지 않는다.
- 일반 대화 중 명백한 긴급 표현이 들어오면 전용 로컬 감지 경로에도
  전달하되, 실제 판정과 연락 정책은 SWM25-27이 소유한다.
- 긴급 처리를 시작하면 진행 중인 이동과 따라가기를 우선 중단한다.
- 연락 대상은 사전에 등록된 대상만 사용하며 LLM이 전화번호나 수신자를
  생성·변경하지 않는다.
- 초기 프로토타입에서는 검증되지 않은 112·119 자동 호출보다 등록된
  보호자 알림을 기본값으로 사용한다.
- 긴급 상태에서는 일반 이동·촬영·따라가기 행동을 시작하지 않는다.
- 긴급 호출 성공·실패 결과는 SWM25-27이 제공한 사실만 설명한다.
- 생성·전달·성공·실패·취소와 중복 억제 결과를 감사 가능한 이벤트로 남긴다.
- 긴급 취소 권한, 허용 시간과 네트워크 장애 시 로컬 경고 정책은
  SWM25-27 담당자가 확정한다.

## 11. 금지된 저수준 제어

다음 인터페이스는 LLM Tool로 등록하지 않는다.

- `/cmd_vel`
- `geometry_msgs/Twist`
- 모터 PWM
- 휠별 속도
- 조향각 직접 명령
- 비상 정지 해제
- 안전 센서 비활성화
- 금지구역 삭제
- 카메라·마이크 프라이버시 강제 해제
- Nav2 safety controller 우회
- 임의 셸 명령·프로세스 실행

LLM이 위 내용을 자연어 또는 Tool 인자로 생성하더라도 로컬 allowlist와
실행 노드가 차단해야 한다.

## 12. 연관 스토리 인터페이스 합의표

현재 저장소에는 아래 연관 기능의 확정 ROS service/action/message가 없다.
따라서 다음 표는 담당자 검토를 위한 제안 handoff다.

| 스토리 | SWM25-69가 받는 입력 | SWM25-69가 보내는 요청 | 필요한 결과 | 합의 상태 |
| --- | --- | --- | --- | --- |
| SWM25-27 긴급 호출 | 긴급 상태·원인·처리 상태 | 없음. 일반 LLM Tool로 호출하지 않음 | 성공·실패·취소 사실 | 검토 필요 |
| SWM25-34 음성 명령 | transcript·confidence·speech_session_id | assistant text·TTS 취소 | 발화 완료·취소 | 검토 필요 |
| SWM25-36 장기 기억 | person_id 범위 검색 결과 | 확인된 기억 생성·수정·삭제 | memory_id·revision | 검토 필요 |
| SWM25-38 리마인더 | 리마인더 상태 이벤트 | 생성·수정·취소 | reminder_id·정규화 시각·상태 | 검토 필요 |
| SWM25-40 감정 표현 | 지원 감정·renderer 상태 | emotion·duration_ms | 완료·실패 | 검토 필요 |
| SWM25-41 따라다니기 | 추적 가능 상태·대상 상태 | 시작·중단 | session_id·진행·종료 원인 | 검토 필요 |

### 12.1 제안 ROS adapter

현재 저장소에는 아래 기능의 확정 커스텀 `.msg`, `.srv`, `.action`이 없다.
새 `malbut_interfaces` 패키지에서 타입을 공동 소유하는 방안을 제안한다.
장시간 실행·feedback·취소가 필요한 기능은 Action, 짧은 조회·영속 변경은
Service, 비동기 감지 결과는 Topic으로 구분한다.

| 스토리 | 제안 ROS 인터페이스 | 핵심 필드 | SLA 초안 |
| --- | --- | --- | --- |
| SWM25-27 | `/malbut/emergency/events` Topic | incident_id, source, reason, state, stamp | 로컬 이벤트 즉시 전달 |
| SWM25-34 | `/malbut/speech/transcript` Topic | utterance_id, conversation_id, speaker_id, text, confidence, is_final, stamp | 최종 transcript만 실행 문맥으로 사용 |
| SWM25-34 | `/malbut/speech/speak` Action | request_id, text, voice, style, interruptible | goal 0.5초, 재생 시작 1.5초, 취소 0.3초 |
| SWM25-36 | `/malbut/memory/query` Service | request_id, person_id, query, limit | 로컬 검색 0.3초 |
| SWM25-36 | `/malbut/memory/commit·update·delete` Services | request_id, person_id, memory_id, content, evidence_turn_id, user_confirmed | 변경 0.5초 |
| SWM25-38 | `/malbut/reminders/create·list·update·cancel` Services | request_id, owner_person_id, message, trigger_at, timezone, user_confirmed | CRUD 1초 |
| SWM25-38 | `/malbut/reminders/events` Topic | reminder_id, owner_person_id, state, fired_at | 영속 scheduler 이벤트 |
| SWM25-40 | `/malbut/expression/play` Action | request_id, emotion, intensity, duration_ms | goal 0.3초 |
| SWM25-41 | `/malbut/perception/tracked_people` Topic | person_id, identity_confidence, tracking_confidence, pose, last_seen | freshness 기준 합의 필요 |
| SWM25-41 | `/malbut/follow_person` Action | request_id, target_person_id, max_duration_sec, user_confirmed | goal 1초, 취소·정지 0.3초 |

인터페이스 공통 요구:

- 모든 변경·Action 요청은 idempotent `request_id`를 포함한다.
- 모든 sensor·상태 이벤트는 ROS timestamp와 source를 포함한다.
- 부분 STT 결과로 Tool을 실행하지 않는다.
- `person_id`는 LLM이 생성하지 않고 신뢰된 인식 adapter가 공급한다.
- 기억 변경, 리마인더 변경, 따라가기 시작 요청은 확인 증거를 포함한다.
- 리마인더 시각은 simulation time이 아닌 UTC wall clock과 IANA timezone으로
  보존한다.
- Action 서버가 최종 실행 권한과 취소·안전 정지를 소유한다.
- LLM에는 전화번호, RGB 값, 파일 경로, 속도·가속도 같은 저수준 값을
  노출하지 않는다.

### 12.2 담당자별 필수 검토

#### SWM25-27 긴급 호출

- [ ] LLM 비의존 긴급 경로와 로컬 입력을 확정한다.
- [ ] 긴급 발생·취소·중복 이벤트 정책을 확정한다.
- [ ] 등록 연락 대상과 네트워크 장애 fallback을 확정한다.
- [ ] 긴급 처리 중 이동·추적 중단 정책을 확정한다.

#### SWM25-34 음성 명령 수행

- [ ] transcript에 session, final 여부, confidence, timestamp를 포함한다.
- [ ] 부분 transcript로 Tool을 실행하지 않도록 합의한다.
- [ ] 끼어들기, 발화 취소, TTS 중단 인터페이스를 확정한다.
- [ ] 로봇 자신의 TTS를 사용자 명령으로 재인식하지 않도록 한다.

#### SWM25-36 사람별 장기 기억

- [ ] `person_id` 발급과 최소 인식 신뢰도를 확정한다.
- [ ] 기억 조회·저장·수정·삭제·만료 인터페이스를 확정한다.
- [ ] 사용자 동의 증거와 사람 간 격리 정책을 확정한다.
- [ ] 미인식·저신뢰 사용자에게 개인 기억을 제공하지 않도록 한다.

#### SWM25-38 리마인더

- [ ] 날짜, timezone, 반복, 내용 스키마를 확정한다.
- [ ] 생성·변경·취소 전 확인 정책을 확정한다.
- [ ] 재부팅 복구와 전달 성공·실패 이벤트를 확정한다.
- [ ] 같은 `request_id`의 중복 리마인더 생성을 방지한다.

#### SWM25-40 감정 표현

- [ ] 감정 enum, intensity, duration과 modality를 확정한다.
- [ ] 긴급 상태에서 일반 감정 표현을 억제한다.
- [ ] 표현 실패 시 `neutral` fallback과 빈도 제한을 확정한다.
- [ ] 표현 adapter가 이동 베이스를 제어하지 않도록 한다.

#### SWM25-41 사람 따라다니기

- [ ] 시작·중단·상태·feedback 인터페이스를 확정한다.
- [ ] 대상 식별과 identity·tracking 최소 신뢰도를 확정한다.
- [ ] 대상 상실, 센서 장애, 배터리 부족 시 정지 정책을 확정한다.
- [ ] `stop_follow`가 LLM 장애와 무관하게 우선 실행되도록 한다.
- [ ] 속도와 안전거리의 최종 결정은 추적·주행 노드가 담당한다.

### 12.3 공통 승인 항목

각 담당자는 다음 항목을 승인하거나 수정해야 한다.

- ROS interface 종류: topic, service 또는 action
- package와 interface 이름
- 요청·응답 필드
- 상태 freshness 기준
- timeout과 취소 동작
- idempotency 키
- 담당 안전 검증
- 오류 코드
- 개인정보와 로그 보존 정책
- Mock 또는 시뮬레이션 adapter
- 계약 버전과 변경 통보 방법
- 정상·오류·timeout·취소 공동 테스트

## 13. 달성 조건 추적

### 13.1 자유 대화·에이전트 기능 계약

- [x] 지원할 대화 유형을 정의했다.
- [x] 현재 및 제안 Tool 목록을 문서화했다.
- [x] Tool별 입력, 출력, 오류, timeout을 정의했다.
- [x] 실행 전 사용자 확인이 필요한 행동을 정의했다.
- [x] 긴급 호출을 LLM에만 의존하지 않는 별도 경로로 정의했다.
- [x] `/cmd_vel`, 모터 PWM, 비상 정지 해제 등의 직접 제어를 금지했다.
- [ ] SWM25-27 담당자가 긴급 호출 경계를 승인했다.
- [ ] SWM25-34 담당자가 음성 인터페이스를 승인했다.
- [ ] SWM25-36 담당자가 장기 기억 인터페이스를 승인했다.
- [ ] SWM25-38 담당자가 리마인더 인터페이스를 승인했다.
- [ ] SWM25-40 담당자가 감정 표현 인터페이스를 승인했다.
- [ ] SWM25-41 담당자가 따라다니기 인터페이스를 승인했다.

## 14. 승인 기록

| 스토리 | 담당자 | 결정 | 날짜 | 비고 |
| --- | --- | --- | --- | --- |
| SWM25-27 |  | 대기 |  |  |
| SWM25-34 |  | 대기 |  |  |
| SWM25-36 |  | 대기 |  |  |
| SWM25-38 |  | 대기 |  |  |
| SWM25-40 |  | 대기 |  |  |
| SWM25-41 |  | 대기 |  |  |
