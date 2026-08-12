# 합성 대화·컨텍스트 전체 흐름 기록

> SYNTHETIC / OFFLINE / NON-ACTUATING / NOT PRODUCTION DATA

기존 300회 stress JSON에는 발화·응답·prompt가 저장되지 않았다. 이 문서는 과거 로그를 복구한 것이 아니라, 현재 코드를 Mock으로 새로 실행해 사람이 읽을 수 있도록 남긴 별도 증거다.

- 생성 시각: `2026-08-12T17:44:51.027914+00:00`
- 시나리오: `4`개
- 통과: `4/4`
- 실제 OpenAI 호출: `false`
- 실제 ROS·파일·카메라·알림 부작용: `false`
- JSON 원본 권한: `0600`

## 읽는 순서

`요청 → 이전 대화/요약/기억 → 모델 입력 JSON → Mock 원결정 → SafetyPolicy → 최종 응답 → DB 저장` 순서로 보면 된다.

## 1. 같은 세션의 이전 사용자 발화 참조 — 통과

두 번째 요청에 첫 번째 턴이 어떤 형태로 들어가고 응답에 사용되는지 보여준다.

- 관련 story: `SWM25-69, SWM25-70, SWM25-71`

### 턴 1: `turn-1`

- 사용자: **내 이름은 신이야**
- Provider: `mock / malbut-korean-rules-v1`
- 외부 API·ROS 호출: 없음

선택된 최근 대화:

```json
[]
```

선택된 이전 요약:

```json
null
```

검색된 장기 기억:

```json
[]
```

모델에 전달된 정확한 컨텍스트 JSON:

```json
{
  "context_policy": {
    "recent_turn_limit": 50,
    "recent_turn_hard_limit": 50,
    "conversation_chars": 6000,
    "summary_chars": 2000,
    "memory_chars": 3000,
    "model_input_chars": 20000
  },
  "robot_state_untrusted": {
    "battery_percent": null,
    "navigation_available": false,
    "localization_ok": false,
    "emergency_stop": false,
    "camera_available": false,
    "privacy_mode": false,
    "docked": false,
    "forbidden_zones": []
  },
  "available_tools": [],
  "conversation_history_untrusted": [],
  "conversation_summary_untrusted": null,
  "memory_context_untrusted": [],
  "current_user_utterance": "내 이름은 신이야",
  "context_truncated": false
}
```

MockProvider 원결정:

```json
{
  "type": "clarification",
  "message": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
  "tool_name": null,
  "arguments": {},
  "reason": "intent_unclear",
  "confidence": 0.9,
  "expires_in_ms": 5000
}
```

로컬 SafetyPolicy 결과:

```json
{
  "allowed": true,
  "code": "not_an_action",
  "reason": "행동 요청이 아닙니다."
}
```

최종 응답과 실행 경계:

```json
{
  "decision": {
    "type": "clarification",
    "message": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
    "tool_name": null,
    "arguments": {},
    "reason": "intent_unclear",
    "confidence": 0.9,
    "expires_in_ms": 5000
  },
  "execution": {
    "decision_id": "754680c7-428e-4078-a82c-e29fb01cac3f",
    "issued_at": 1786556690.9866664,
    "expires_at": 1786556695.9866664,
    "authorized": false,
    "proposal_authorized": false,
    "state_trusted": false,
    "fresh": true,
    "consume_once": false,
    "tool_call_id": null
  }
}
```

영속 저장 확인:

- 저장된 턴 수: `1`
- `raw_decision` 저장 여부: `false`

### 턴 2: `turn-2`

- 사용자: **아까 내가 뭐라고 했지?**
- Provider: `mock / malbut-korean-rules-v1`
- 외부 API·ROS 호출: 없음

선택된 최근 대화:

```json
[
  {
    "turn_id": "turn-1",
    "ordinal": 1,
    "user": "내 이름은 신이야",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  }
]
```

선택된 이전 요약:

```json
null
```

검색된 장기 기억:

```json
[]
```

모델에 전달된 정확한 컨텍스트 JSON:

```json
{
  "context_policy": {
    "recent_turn_limit": 50,
    "recent_turn_hard_limit": 50,
    "conversation_chars": 6000,
    "summary_chars": 2000,
    "memory_chars": 3000,
    "model_input_chars": 20000
  },
  "robot_state_untrusted": {
    "battery_percent": null,
    "navigation_available": false,
    "localization_ok": false,
    "emergency_stop": false,
    "camera_available": false,
    "privacy_mode": false,
    "docked": false,
    "forbidden_zones": []
  },
  "available_tools": [],
  "conversation_history_untrusted": [
    {
      "turn_id": "turn-1",
      "ordinal": 1,
      "user": "내 이름은 신이야",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    }
  ],
  "conversation_summary_untrusted": null,
  "memory_context_untrusted": [],
  "current_user_utterance": "아까 내가 뭐라고 했지?",
  "context_truncated": false
}
```

MockProvider 원결정:

```json
{
  "type": "message",
  "message": "아까 “내 이름은 신이야”라고 말했어.",
  "tool_name": null,
  "arguments": {},
  "reason": "conversation_history_user_reference",
  "confidence": 1.0,
  "expires_in_ms": 5000
}
```

로컬 SafetyPolicy 결과:

```json
{
  "allowed": true,
  "code": "not_an_action",
  "reason": "행동 요청이 아닙니다."
}
```

최종 응답과 실행 경계:

```json
{
  "decision": {
    "type": "message",
    "message": "아까 “내 이름은 신이야”라고 말했어.",
    "tool_name": null,
    "arguments": {},
    "reason": "conversation_history_user_reference",
    "confidence": 1.0,
    "expires_in_ms": 5000
  },
  "execution": {
    "decision_id": "3f53fd40-e5d7-4332-ab5b-b6ec3886b51d",
    "issued_at": 1786556690.9892223,
    "expires_at": 1786556695.9892223,
    "authorized": false,
    "proposal_authorized": false,
    "state_trusted": false,
    "fresh": true,
    "consume_once": false,
    "tool_call_id": null
  }
}
```

영속 저장 확인:

- 저장된 턴 수: `2`
- `raw_decision` 저장 여부: `false`

검증 결과:

- [x] second turn received one prior completed turn
- [x] prior user utterance reached untrusted history
- [x] MockProvider resolved the follow-up from history
- [x] non-action response passed SafetyPolicy

## 2. 최근 10턴 + 이전 요약 + 장기 기억 결합 — 통과

모델에 전달되는 세 컨텍스트 영역과 최종 기억 답변을 한 흐름으로 보여준다.

- 관련 story: `SWM25-70, SWM25-71`

### 턴 1: `turn-13`

- 사용자: **강아지 이름이 뭐였지?**
- Provider: `mock / malbut-korean-rules-v1`
- 외부 API·ROS 호출: 없음

선택된 최근 대화:

```json
[
  {
    "turn_id": "seed-turn-03",
    "ordinal": 3,
    "user": "합성 대화 03: 오늘 기록 03",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  },
  {
    "turn_id": "seed-turn-04",
    "ordinal": 4,
    "user": "합성 대화 04: 오늘 기록 04",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  },
  {
    "turn_id": "seed-turn-05",
    "ordinal": 5,
    "user": "합성 대화 05: 오늘 기록 05",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  },
  {
    "turn_id": "seed-turn-06",
    "ordinal": 6,
    "user": "합성 대화 06: 오늘 기록 06",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  },
  {
    "turn_id": "seed-turn-07",
    "ordinal": 7,
    "user": "합성 대화 07: 오늘 기록 07",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  },
  {
    "turn_id": "seed-turn-08",
    "ordinal": 8,
    "user": "합성 대화 08: 오늘 기록 08",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  },
  {
    "turn_id": "seed-turn-09",
    "ordinal": 9,
    "user": "합성 대화 09: 오늘 기록 09",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  },
  {
    "turn_id": "seed-turn-10",
    "ordinal": 10,
    "user": "합성 대화 10: 오늘 기록 10",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  },
  {
    "turn_id": "seed-turn-11",
    "ordinal": 11,
    "user": "합성 대화 11: 오늘 기록 11",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  },
  {
    "turn_id": "seed-turn-12",
    "ordinal": 12,
    "user": "합성 대화 12: 오늘 기록 12",
    "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘."
  }
]
```

선택된 이전 요약:

```json
{
  "summary_id": "b3246c40-6036-46ca-ab44-0f6e5395895f",
  "user_id": "synthetic-user",
  "conversation_id": "synthetic-summary-memory",
  "session_instance_id": "9e860bfd-4e21-46d9-aec2-130419a6e384",
  "generation": 1,
  "summary_revision": 2,
  "content": "[UNTRUSTED_CONVERSATION_SUMMARY_DATA source_start_ordinal=1 source_end_ordinal=2 source_turn_count=2 algorithm=local-extractive-rolling-v1]\n{\"assistant_data\":\"요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.\",\"source_ordinal\":1,\"turn_id\":\"seed-turn-01\",\"user_data\":\"합성 대화 01: 오늘 기록 01\"}\n{\"assistant_data\":\"요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.\",\"source_ordinal\":2,\"turn_id\":\"seed-turn-02\",\"user_data\":\"합성 대화 02: 오늘 기록 02\"}",
  "source_start_ordinal": 1,
  "source_end_ordinal": 2,
  "source_turn_count": 2,
  "source_digest": "b0fbe7e443755a32882fbbe5c27aaca880cd65c2d4478778af37aecf15817cc2",
  "summarizer": "local-extractive-rolling-v1",
  "fallback_used": false,
  "created_at": 1786556691.0144215,
  "updated_at": 1786556691.0189905
}
```

검색된 장기 기억:

```json
[
  {
    "id": "synthetic-memory-pet-name",
    "user_id": "synthetic-user",
    "kind": "fact",
    "content": "강아지 이름은 초코야",
    "source": "user_verified",
    "confidence": 1.0,
    "created_at": 1786556690.9910963,
    "expires_at": null,
    "metadata": {
      "synthetic": true
    },
    "score": 13.0
  }
]
```

모델에 전달된 정확한 컨텍스트 JSON:

```json
{
  "context_policy": {
    "recent_turn_limit": 50,
    "recent_turn_hard_limit": 50,
    "conversation_chars": 6000,
    "summary_chars": 2000,
    "memory_chars": 3000,
    "model_input_chars": 20000
  },
  "robot_state_untrusted": {
    "battery_percent": null,
    "navigation_available": false,
    "localization_ok": false,
    "emergency_stop": false,
    "camera_available": false,
    "privacy_mode": false,
    "docked": false,
    "forbidden_zones": []
  },
  "available_tools": [],
  "conversation_history_untrusted": [
    {
      "turn_id": "seed-turn-03",
      "ordinal": 3,
      "user": "합성 대화 03: 오늘 기록 03",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    },
    {
      "turn_id": "seed-turn-04",
      "ordinal": 4,
      "user": "합성 대화 04: 오늘 기록 04",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    },
    {
      "turn_id": "seed-turn-05",
      "ordinal": 5,
      "user": "합성 대화 05: 오늘 기록 05",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    },
    {
      "turn_id": "seed-turn-06",
      "ordinal": 6,
      "user": "합성 대화 06: 오늘 기록 06",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    },
    {
      "turn_id": "seed-turn-07",
      "ordinal": 7,
      "user": "합성 대화 07: 오늘 기록 07",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    },
    {
      "turn_id": "seed-turn-08",
      "ordinal": 8,
      "user": "합성 대화 08: 오늘 기록 08",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    },
    {
      "turn_id": "seed-turn-09",
      "ordinal": 9,
      "user": "합성 대화 09: 오늘 기록 09",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    },
    {
      "turn_id": "seed-turn-10",
      "ordinal": 10,
      "user": "합성 대화 10: 오늘 기록 10",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    },
    {
      "turn_id": "seed-turn-11",
      "ordinal": 11,
      "user": "합성 대화 11: 오늘 기록 11",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    },
    {
      "turn_id": "seed-turn-12",
      "ordinal": 12,
      "user": "합성 대화 12: 오늘 기록 12",
      "assistant": "요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.",
      "user_truncated": false,
      "assistant_truncated": false
    }
  ],
  "conversation_summary_untrusted": {
    "summary_id": "b3246c40-6036-46ca-ab44-0f6e5395895f",
    "generation": 1,
    "summary_revision": 2,
    "source_start_ordinal": 1,
    "source_end_ordinal": 2,
    "source_turn_count": 2,
    "source_digest": "b0fbe7e443755a32882fbbe5c27aaca880cd65c2d4478778af37aecf15817cc2",
    "summarizer": "local-extractive-rolling-v1",
    "created_at": 1786556691.0144215,
    "updated_at": 1786556691.0189905,
    "content": "[UNTRUSTED_CONVERSATION_SUMMARY_DATA source_start_ordinal=1 source_end_ordinal=2 source_turn_count=2 algorithm=local-extractive-rolling-v1]\n{\"assistant_data\":\"요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.\",\"source_ordinal\":1,\"turn_id\":\"seed-turn-01\",\"user_data\":\"합성 대화 01: 오늘 기록 01\"}\n{\"assistant_data\":\"요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.\",\"source_ordinal\":2,\"turn_id\":\"seed-turn-02\",\"user_data\":\"합성 대화 02: 오늘 기록 02\"}",
    "truncated": false
  },
  "memory_context_untrusted": [
    {
      "id": "synthetic-memory-pet-name",
      "kind": "fact",
      "content": "강아지 이름은 초코야",
      "source": "user_verified",
      "confidence": 1.0,
      "truncated": false
    }
  ],
  "current_user_utterance": "강아지 이름이 뭐였지?",
  "context_truncated": false
}
```

MockProvider 원결정:

```json
{
  "type": "message",
  "message": "기억해 둔 내용은 “강아지 이름은 초코야”이야.",
  "tool_name": null,
  "arguments": {},
  "reason": "retrieved_verified_memory",
  "confidence": 1.0,
  "expires_in_ms": 5000
}
```

로컬 SafetyPolicy 결과:

```json
{
  "allowed": true,
  "code": "not_an_action",
  "reason": "행동 요청이 아닙니다."
}
```

최종 응답과 실행 경계:

```json
{
  "decision": {
    "type": "message",
    "message": "기억해 둔 내용은 “강아지 이름은 초코야”이야.",
    "tool_name": null,
    "arguments": {},
    "reason": "retrieved_verified_memory",
    "confidence": 1.0,
    "expires_in_ms": 5000
  },
  "execution": {
    "decision_id": "302d5459-cde2-437c-a81c-5ea939b7bc53",
    "issued_at": 1786556691.0225194,
    "expires_at": 1786556696.0225194,
    "authorized": false,
    "proposal_authorized": false,
    "state_trusted": false,
    "fresh": true,
    "consume_once": false,
    "tool_call_id": null
  }
}
```

영속 저장 확인:

- 저장된 턴 수: `13`
- `raw_decision` 저장 여부: `false`

검증 결과:

- [x] provider received latest ten raw turns
- [x] summary covered exactly the two older turns
- [x] one user-isolated memory was retrieved
- [x] retrieved fact was used in the final answer

## 3. navigate 제안과 Safety 이후 최종 거절 비교 — 통과

모델의 Tool 제안이 곧 실행이 아니며 로컬 SafetyPolicy가 최종 응답을 바꾸는 것을 보여준다.

- 관련 story: `SWM25-69, SWM25-72, SWM25-74`

### 턴 1: `turn-1`

- 사용자: **거실로 가줘**
- Provider: `mock / malbut-korean-rules-v1`
- 외부 API·ROS 호출: 없음

선택된 최근 대화:

```json
[]
```

선택된 이전 요약:

```json
null
```

검색된 장기 기억:

```json
[]
```

모델에 전달된 정확한 컨텍스트 JSON:

```json
{
  "context_policy": {
    "recent_turn_limit": 50,
    "recent_turn_hard_limit": 50,
    "conversation_chars": 6000,
    "summary_chars": 2000,
    "memory_chars": 3000,
    "model_input_chars": 20000
  },
  "robot_state_untrusted": {
    "battery_percent": 80.0,
    "navigation_available": true,
    "localization_ok": true,
    "emergency_stop": false,
    "camera_available": false,
    "privacy_mode": false,
    "docked": false,
    "forbidden_zones": []
  },
  "available_tools": [
    "navigate"
  ],
  "conversation_history_untrusted": [],
  "conversation_summary_untrusted": null,
  "memory_context_untrusted": [],
  "current_user_utterance": "거실로 가줘",
  "context_truncated": false
}
```

MockProvider 원결정:

```json
{
  "type": "tool_call",
  "message": "거실 이동을 요청할게.",
  "tool_name": "navigate",
  "arguments": {
    "location": "거실"
  },
  "reason": "named_navigation_request",
  "confidence": 0.98,
  "expires_in_ms": 5000
}
```

로컬 SafetyPolicy 결과:

```json
{
  "allowed": false,
  "code": "untrusted_robot_state",
  "reason": "신뢰된 로컬 ROS 상태가 없어 행동을 실행하지 않습니다."
}
```

최종 응답과 실행 경계:

```json
{
  "decision": {
    "type": "refusal",
    "message": "신뢰된 로컬 ROS 상태가 없어 행동을 실행하지 않습니다.",
    "tool_name": null,
    "arguments": {},
    "reason": "safety:untrusted_robot_state",
    "confidence": 1.0,
    "expires_in_ms": 5000
  },
  "execution": {
    "decision_id": "259b0e3f-6f98-4430-b081-495ba9d1e491",
    "issued_at": 1786556691.02644,
    "expires_at": 1786556696.02644,
    "authorized": false,
    "proposal_authorized": false,
    "state_trusted": false,
    "fresh": true,
    "consume_once": false,
    "tool_call_id": null
  }
}
```

영속 저장 확인:

- 저장된 턴 수: `1`
- `raw_decision` 저장 여부: `false`

검증 결과:

- [x] provider proposed the high-level navigate Tool
- [x] provider bound the named destination
- [x] local SafetyPolicy rejected untrusted request state
- [x] final decision became refusal
- [x] physical execution stayed unauthorized
- [x] no tool_call_id was created

## 4. Production Gateway의 실제 실행 차단 경계 — 통과

현재 Gateway는 confirmation·tool_call_id·실제 ROS 실행을 구현한 것처럼 보이지 않도록 negative evidence를 남긴다.

- 관련 story: `SWM25-73, SWM25-74`

### `production_registry`

```json
{
  "snapshot": {
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
        "timeout_ms": 2000
      },
      {
        "name": "detect_pet",
        "risk_level": "L1",
        "mode": "read_only",
        "available_for_proposal": true,
        "executable": false,
        "blocked_by": "executor_unavailable",
        "timeout_ms": 3000
      },
      {
        "name": "capture_photo",
        "risk_level": "L2",
        "mode": "proposal_only",
        "available_for_proposal": true,
        "executable": false,
        "blocked_by": "confirmation_required",
        "timeout_ms": 5000
      },
      {
        "name": "send_notification",
        "risk_level": "L2",
        "mode": "proposal_only",
        "available_for_proposal": true,
        "executable": false,
        "blocked_by": "confirmation_required",
        "timeout_ms": 5000
      },
      {
        "name": "get_robot_status",
        "risk_level": "L0",
        "mode": "read_only",
        "available_for_proposal": true,
        "executable": false,
        "blocked_by": "executor_unavailable",
        "timeout_ms": 1000
      }
    ]
  },
  "executable_count": 0
}
```

### `tool_query_validated`

```json
{
  "query": {
    "request_id": "synthetic-gateway-request-1",
    "user_id": "synthetic-user",
    "tool_name": "navigate",
    "arguments": {
      "location": "거실"
    }
  }
}
```

### `gateway_result`

```json
{
  "result": {
    "result_id": "d10c34f6-d7f5-46a0-95e5-66452fab67e2",
    "request_id": "synthetic-gateway-request-1",
    "tool_name": "navigate",
    "mode": "proposal_only",
    "status": "rejected",
    "started_at": "2026-08-12T17:44:51.027781Z",
    "completed_at": "2026-08-12T17:44:51.027846Z",
    "result": null,
    "error": {
      "code": "confirmation_required",
      "message": "SWM25-74 confirmation is required for this Tool."
    },
    "cached": false
  }
}
```

### `fake_confirmation_rejected`

```json
{
  "request": {
    "request_id": "synthetic-fake-confirmation",
    "user_id": "synthetic-user",
    "tool_name": "navigate",
    "arguments": {
      "location": "거실"
    },
    "confirmation": {
      "confirmed": true
    }
  },
  "validation_error": "unknown Tool query fields: confirmation"
}
```

검증 결과:

- [x] production registry exposes zero executable Tools
- [x] navigate query is blocked pending SWM25-74 confirmation
- [x] fake confirmation field is rejected by strict schema

## 해석할 때 주의할 점

- 이 결과는 규칙 기반 `MockProvider`의 코드 흐름 증거이며 실제 OpenAI 모델 품질 증거가 아니다.
- exact prompt와 raw decision은 이 합성 trace에만 기록했다. 운영 대화에서는 개인정보 때문에 기본적으로 저장하지 않는다.
- `execution.authorized=false`는 의도된 현재 경계다. SWM25-74의 confirmation·영속 1회 소비·ROS 실행·feedback/cancel은 아직 없다.
- 전체 system instructions와 모든 중간 필드는 같은 이름의 JSON 원본에서 확인할 수 있다.
