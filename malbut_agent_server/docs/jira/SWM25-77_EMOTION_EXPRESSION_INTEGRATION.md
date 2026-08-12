# SWM25-77 감정 표현 연동 — 비실행 시각 표현 계약 MVP

- 상태: 오프라인 계약 및 순수 Python 정책 구현
- 범위: `malbut_agent_server.expression`
- 비범위: 실제 ROS Action, 웹 frontend, 화면·LED·스피커·모터 제어
- 기준: SWM25-69의 제한된 감정 태그와 SWM25-40 renderer 책임 경계

## 1. 결론

이번 MVP는 LLM이 장치나 화면을 직접 제어하는 기능이 아니다. 최종 안전
응답의 제한된 메타데이터를 시각 표현 cue로 바꾸고, 로컬의 신뢰된 상태가
그 cue를 억제할 수 있는 독립 계약이다. 저장소에 포함된 renderer는 아무
작업도 하지 않는 `NoopVisualExpressionRenderer`와 메모리에 호출만 기록하는
`RecordingVisualExpressionRenderer`뿐이다.

따라서 다음 흐름에서 마지막 실제 장치 구간은 아직 존재하지 않는다.

```text
최종 AgentDecision + SafetyResult
                 │
                 ▼
   결정적 final-decision mapper
                 │
                 ▼
       visual ExpressionCue
                 │
                 ▼
 ExpressionPolicy / ExpressionArbiter
     ▲           │
     │           ▼
긴급·privacy   visual-only renderer protocol
신뢰 상태       │
                 └─ 현재 제공: Noop / Recording만
```

`express_emotion`을 모델 Tool로 등록하지 않았다. 현 대화 계약은 terminal
text와 function call을 동시에 허용하지 않으므로, 일반 Tool로 추가하면
사용자 답변과 보조 표정을 함께 전달하기 어렵다. 또한 현재 Tool Gateway에는
실제 side-effect adapter를 승인하는 SWM25-74 실행 권한이 없다.

## 2. 안전 원칙

1. **로봇 표현만 나타낸다.** cue는 Malbut의 표현 방식이며 사용자나
   반려동물의 감정, 심리, 질환 또는 의도를 나타내는 판정이 아니다.
2. **텍스트를 분석하지 않는다.** mapper는 사용자 발화, 답변 본문, 얼굴,
   음성, 기억, 과거 대화를 읽지 않는다. 최종 decision type·고정 reason과
   최종 `SafetyResult`만 사용한다.
3. **시각 표현만 허용한다.** modality는 항상 `visual`이다. 소리와 몸짓은
   별도 동의·안전 계약 없이는 확장하지 않는다.
4. **모델은 우선순위를 정하지 않는다.** cue에 priority 필드가 없다.
   신뢰된 로컬 긴급 상태가 privacy보다 우선하며, 둘 모두 일반 표현보다
   우선한다.
5. **표현은 만료된다.** 일반 표정은 최대 5초이며, 만료하면 한 번만
   `neutral`을 요청한다.
6. **실패는 표현을 키우지 않는다.** 지원하지 않는 값은 보정하지 않고
   거절한다. renderer 실패 시 neutral을 한 번만 시도하고, neutral도
   실패하면 해당 arbiter 인스턴스에서 renderer를 비활성화한다.
7. **표현 adapter에는 이동 권한이 없다.** protocol은
   `render_visual(request_id, emotion, intensity, duration_ms)` 하나뿐이며
   속도, pose, topic, shell, URL, 파일 경로 또는 오디오 인자를 받지 않는다.

## 3. ExpressionCue 계약

`ExpressionCue.from_dict()`는 아래 모든 필드를 요구하며 추가 필드를
거절한다.

| 필드 | 계약 |
| --- | --- |
| `request_id` | 1~128자, 제어문자 금지, process-local idempotency key |
| `cue_id` | 결정 내용에서 결정적으로 생성한 1~128자 식별자 |
| `emotion` | 아래 5개 allowlist 중 하나 |
| `intensity` | 유한 실수 `0.0~0.7`; neutral은 정확히 0, 나머지는 0보다 큼 |
| `duration_ms` | 정수 `250~5000` |
| `issued_at` | 같은 프로세스의 monotonic clock 값 |
| `ttl_ms` | dispatch 유효 시간, 정수 `1~1000` |
| `modality` | `visual` 고정 |
| `source` | `deterministic_final_decision` 고정 |

허용 emotion은 기존 SWM25-69 제안을 유지한다.

- `neutral`
- `happy`
- `concerned`
- `excited`
- `apologetic`

`angry`, `sad`처럼 합의되지 않은 이름, `audio`·`gesture`, boolean numeric,
NaN·Infinity, 범위 밖 intensity·duration·TTL, 임의 source·priority는 모두
실패한다. 조용히 clamp하거나 비슷한 감정으로 바꾸지 않는다.

`issued_at`은 wall clock이 아니므로 이 cue를 그대로 프로세스 밖에 전달하는
공개 wire contract로 사용하면 안 된다. 향후 ROS/frontend 연결에서는
수신자가 자체 monotonic clock으로 TTL을 시작하고, 별도의 transport
timestamp·freshness 계약을 확정해야 한다.

`from_dict()`는 strict schema 역직렬화 검사용이지만 그 결과는 local mapper의
authority를 갖지 않는다. `ExpressionArbiter.submit()`은
`map_final_decision_to_expression()`이 현재 프로세스 안에서 만든 cue만 받는다.
따라서 caller가 `source=deterministic_final_decision` 문자열만 복사해 원하는
emotion을 고르는 것은 renderer 권한이 되지 않는다.

## 4. 결정적 mapper

`map_final_decision_to_expression()`은 다음 고정표만 사용한다.

| 최종 조건 | emotion | intensity | duration |
| --- | --- | ---: | ---: |
| Safety가 차단했거나 최종 type이 `refusal` | `concerned` | 0.35 | 1500ms |
| reason `greeting` 또는 `thanks` | `happy` | 0.50 | 1500ms |
| reason `apology` | `apologetic` | 0.35 | 1500ms |
| reason `celebration` | `excited` | 0.65 | 1500ms |
| 그 밖의 message·clarification·tool proposal | `neutral` | 0.00 | 1000ms |

본문에 “우울”, “불안”, “화남” 같은 단어가 있어도 mapper 결과는 달라지지
않는다. 모델이 임의 reason을 만들더라도 위 정확한 allowlist와 일치하지
않으면 neutral이다. 이 정책은 심리 진단기가 아니며 그런 용도로 사용하면
안 된다.

`cue_id`는 request ID, 최종 decision metadata, safety code와 선택된 emotion의
정규화된 JSON에서 SHA-256으로 결정적으로 생성한다. 재시도 시 monotonic
`issued_at`이 달라도 같은 최종 결정은 같은 cue ID를 얻는다.

## 5. 로컬 정책 우선순위

`TrustedExpressionState`는 모델이나 일반 HTTP payload가 아니라 신뢰된 로컬
호출자가 생성해야 한다. 필드는 exact boolean만 허용한다.

우선순위는 다음과 같이 고정된다.

1. `emergency_active=true` → `emergency_override`
2. `privacy_mode=true` → `privacy_override`
3. `renderer_available=false` → `renderer_unavailable`
4. dispatch TTL 만료 → `stale_cue`
5. monotonic `issued_at`이 현재보다 미래 → `future_cue`
6. 위 조건이 없을 때만 일반 cue 허용

긴급과 privacy가 동시에 참이면 긴급이 우선한다. override가 발생했을 때
현재 assistant 표현이 활성 상태라면 그 lane을 지우고 neutral을 한 번
요청한다. 활성 표현이 없으면 불필요한 neutral 호출을 반복하지 않는다.
외부 안전 화면이나 경고 표시가 있다면 그것은 별도 상위 lane의 책임이며,
이 모듈의 neutral은 그 안전 표시를 덮어쓸 권한이 없다.

`ExpressionArbiter.tick(state)`도 매번 신뢰 상태를 요구한다. 새 cue가 없어도
긴급·privacy 변화가 활성 표현을 즉시 중단할 수 있다. cached retry 역시
현재 신뢰 상태를 다시 검사하므로 이전 성공 결과가 긴급 override를
우회하지 못한다.

## 6. TTL과 neutral 복귀

서로 다른 두 시간 한도를 구분한다.

- **dispatch TTL:** cue 발급 뒤 renderer에 넘길 수 있는 시간. 최대 1000ms이며
  정확한 deadline부터 stale이다.
- **display duration:** renderer가 일반 표현을 유지할 수 있는 시간.
  250~5000ms이다.

arbiter는 process-local monotonic clock만 사용한다. 일반 표현을 성공적으로
받으면 `expires_at = started_at + duration`을 저장한다. `tick()` 또는 다음
`submit()`이 만료를 관찰하면 neutral을 정확히 한 번 요청하고 활성 상태를
제거한다. 재시도된 request는 최초 `expires_at`을 연장하지 않는다.

이 모듈은 background timer thread를 만들지 않는다. 향후 실제 소비자는
신뢰 상태와 함께 `tick()`을 충분한 주기로 호출하거나 자체 watchdog을
가져야 한다. 프로세스가 재시작되면 arbiter는 활성 표현이 없는 neutral
논리 상태로 시작한다.

같은 request ID의 payload conflict가 있더라도 emergency/privacy active lane
clear가 먼저 수행된다. 충돌 예외가 안전 neutral 전환을 지연할 수 없다.

## 7. 빈도 제한

기본값은 보수적인 MVP 초안이다.

- non-neutral 표현 사이 최소 간격: 2초
- 60초 동안 성공 시도 가능한 non-neutral cue: 최대 6개
- neutral cue와 안전상 neutral 복귀: 제한하지 않음

일반 caller가 서로 다른 ID로 neutral cue를 연속 제출하는 경우 이미 active
assistant 표현이 없다면 `already_neutral`로 coalesce하고 renderer를 다시
호출하지 않는다. 안전 override·만료·오류 fallback의 내부 neutral은 필요한
경우 한 번 직접 전달된다.

rate-limit은 성공 결과뿐 아니라 renderer에 전달한 non-neutral 시도도
계수한다. 반복 실패가 renderer를 빠르게 두드리는 것을 막기 위해서다.
제한된 cue는 `rate_limited`로 끝나며 현재 활성 표현을 연장하지 않는다.

이 숫자는 제품 UX 승인값이 아니다. 실제 화면의 광량, 애니메이션 특성,
접근성 검토 후 설정값을 확정해야 한다.

## 8. idempotency와 conflict

arbiter는 bounded LRU cache를 사용한다. 기본 보존 개수는 256이다.

- cache에 남아 있는 같은 `request_id`와 같은 cue payload → 동일한 `result_id`와 최초
  `expires_at` 반환, `cached=true`, renderer 재호출 없음
- 같은 `request_id`와 다른 cue payload → `ExpressionConflictError`
- 비교 fingerprint에서 `issued_at`만 제외 → retry가 TTL을 갱신해도 최초
  결과를 재생하며 표현 시간을 늘리지 못함
- 동시 duplicate → lock 안에서 하나만 renderer에 전달

다만 이것은 **bounded cache 안의 process-local at-most-once**다. cache에서
축출된 ID와 재시작 뒤 요청은 exactly-once를 보장하지 않는다. 실제 장치 연동
전에는 SWM25-74 실행 기록 또는 renderer 측 영속 request ledger가 필요하다.

신뢰 상태는 cache보다 우선한다. 과거에 성공한 같은 request를 재시도해도
현재 emergency/privacy가 활성화되어 있으면 표현을 neutral로 지우고 override
결과를 반환한다.

## 9. renderer 오류와 fallback

일반 표현 호출에서 예외가 발생하면 다음 순서만 허용한다.

1. 활성 표현을 제거한다.
2. 다른 request ID로 neutral을 한 번 요청한다.
3. neutral 성공 → `renderer_failed_neutral_fallback`
4. neutral 실패 → `renderer_unavailable`, 내부 renderer disabled
5. disabled 상태의 이후 cue → renderer를 호출하지 않고 suppress

neutral cue 자체가 실패하면 neutral을 다시 재귀 요청하지 않고 즉시 renderer를
비활성화한다. 예외 문자열은 `ExpressionResult`에 넣지 않는다. 로컬 결과에는
bounded status·code와 boolean `renderer_error`만 남긴다. 일반 표현 호출에서
예외가 한 번이라도 발생하면 neutral fallback이 성공해도 `renderer_error=true`다.

## 10. 상태와 결과 코드

| code | 의미 |
| --- | --- |
| `rendered` | bounded cue를 renderer protocol에 한 번 전달 |
| `emergency_override` | trusted emergency가 일반 표현보다 우선 |
| `privacy_override` | trusted privacy가 일반 표현보다 우선 |
| `renderer_unavailable` | 신뢰 상태 또는 neutral 실패로 renderer 사용 불가 |
| `stale_cue` | dispatch TTL 만료 |
| `future_cue` | 신뢰할 수 없는 미래 monotonic 발급 시각 |
| `rate_limited` | 최소 간격 또는 window 한도 초과 |
| `already_neutral` | active 표현이 없어 일반 neutral renderer 호출 생략 |
| `expired_to_neutral` | display duration 만료 뒤 neutral 복귀 |
| `renderer_failed_neutral_fallback` | 일반 표현 실패, neutral 복귀 성공 |

결과에는 사용자 발화, assistant 본문, 기억, prompt, 진단 label을 넣지 않는다.

## 11. 오프라인 검증

집중 검증 명령:

```bash
cd /home/shin/ros2_ws/src/malbut/malbut_agent_server
python3 -m pytest -q test/test_expression.py
python3 -m flake8 malbut_agent_server/expression.py test/test_expression.py
python3 -m pydocstyle malbut_agent_server/expression.py test/test_expression.py
```

2026-08-13 최종 결과: **41 passed**. 전체 통합 워크트리는
**237 passed in 8.25s**다.

테스트 범위:

- strict round-trip, 추가·누락 필드, allowlist와 numeric 경계
- 고정 mapper, 본문 속 심리·의료 표현 비참조
- emergency > privacy > availability 우선순위
- exact TTL deadline, display expiry와 neutral 단회 복귀
- 순차·동시 duplicate at-most-once, TTL 미연장, payload conflict
- 최소 간격과 window rate-limit, neutral 우선 통과
- renderer 실패 시 neutral 1회, neutral 실패 뒤 fail-closed
- cached retry의 trusted state 재검사
- Noop·Recording 이외 실제 renderer 및 모델 Tool이 없다는 경계

## 12. 남은 실제 연동 blocker

아래가 확정되기 전에는 SWM25-77을 실제 표정 출력 완료로 표시하면 안 된다.

1. SWM25-40 소유 renderer 또는 frontend 저장소와 담당자
2. `/malbut/expression/play`의 실제 `.action` package·type 또는 웹 wire schema
3. receiver-side freshness와 watchdog
4. intensity, rate, 화면 접근성, 밝기·애니메이션 UX 승인값
5. 긴급·privacy 안전 overlay와 assistant lane의 합성 규칙
6. 재시작 뒤 idempotency를 보장할 영속 실행 ledger
7. timeout·취소·renderer health feedback 계약
8. 실제 장치가 이동 베이스, 스피커, 카메라, 파일, 네트워크 권한을 갖지
   않는다는 integration test
9. renderer 호출 timeout과 비동기 dispatch. 현재 offline arbiter는 lock 안에서
   renderer protocol을 호출하므로 renderer가 hang하면 `tick(emergency)`도
   기다린다. blocking renderer가 있는 실제 integration에는 사용할 수 없다.

현재 완료 범위는 “안전한 비실행 visual cue 계약과 오프라인 arbiter”이다.
실제 표정 표시, ROS publish, 웹 UI 변경, LLM 출력 schema 변경은 수행하지
않았다.
