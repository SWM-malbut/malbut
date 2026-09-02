# SWM25-152 일반 대화와 로봇 계획에 서로 다른 모델을 적용

## 1. 결론

SWM25-151의 `RoutedAgentProvider`가 이미 제공하던 일반 대화와 로봇 계획
Provider 자리에 서로 다른 OpenAI 모델과 독립된 reliability/circuit 인스턴스를
연결했다.

```text
FrontRoute.GENERAL_CONVERSATION
  -> gpt-4.1-mini
  -> Tool 0개
  -> reasoning option 없음

FrontRoute.ROBOT_ACTION_REQUEST
  -> gpt-5.6-terra
  -> 기존 allowlisted Tool
  -> reasoning option 없음

Router abstain
  -> 기존 OPENAI_MODEL + OPENAI_FALLBACK_MODEL
```

Front Router가 명시적으로 주입되고 역할별 설정이 하나 이상 있을 때만 분리가
활성화된다. 기본 Router OFF, 기존 범용 Provider, HTTP API, SQLite, Safety,
confirmation, RobotAction과 ROS/Nav2 동작은 변경하지 않았다.

## 2. 목표

- 일반 대화의 응답 지연과 불필요한 Tool 노출을 줄인다.
- 로봇 행동 제안은 별도의 Planner 모델과 기존 Tool 계약을 사용한다.
- 한 역할의 timeout, retry와 circuit 상태가 다른 역할로 전파되지 않게 한다.
- 기존 설정과 Router OFF 경로를 하위 호환으로 유지한다.
- 실제 API payload와 반환 model metadata로 역할 선택을 검증한다.

## 3. 달성 조건

1. [x] 일반 대화는 설정된 Chat 역할만 호출하며 기본 retry 0에서는 실제 API를
   한 번 호출하고, request와 API payload의 Tool이 모두 0개다.
2. [x] 로봇 행동 요청은 설정된 Planner 모델만 호출하며 기존 strict Tool schema를
   그대로 전달한다.
3. [x] Chat, Planner와 abstain fallback은 서로 다른 `ReliableProvider`와 circuit을
   사용하며 한 역할의 장애가 다른 역할 호출로 이어지지 않는다.
4. [x] 역할 설정이 모두 비어 있거나 Router가 OFF이면 기존 `OPENAI_MODEL`,
   `OPENAI_FALLBACK_MODEL`과 public/persisted 동작을 유지한다.
5. [x] offline payload·호출 수·양방향 장애 격리 테스트와 실제 Chat/Planner API
   smoke에서 요청 모델과 역할별 결과를 확인한다.

## 4. 설정 계약

새 설정은 다음과 같다.

| 환경 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `OPENAI_GENERAL_MODEL` | 빈 값 | 일반 대화 전용 모델 |
| `OPENAI_ROBOT_PLANNER_MODEL` | 빈 값 | 로봇 행동 제안 전용 모델 |

두 값은 기존 `Settings` positional field 뒤에 추가했다. 둘 다 빈 값이면 factory가
기존 Provider 인스턴스 하나를 세 역할에 그대로 재사용한다. 하나라도 지정하면
Chat과 Planner는 서로 다른 `ReliableProvider`를 사용한다. 명시한 역할은 지정한
단일 model을 사용하고, 비어 있는 역할은 기존 `OPENAI_MODEL`과
`OPENAI_FALLBACK_MODEL` 체인을 독립적으로 복제한다.

명시한 역할 Provider는 전역 `OPENAI_FALLBACK_MODEL`을 상속하지 않는다. 선택한
역할이 실패했을 때 다른 성능·권한 특성의 모델로 암묵적으로 넘어가는 것을 막기
위해서다. 명시하지 않은 역할은 기존 fallback 의미를 유지하되 circuit을 공유하지
않는다. Router가 명시적으로 abstain한 요청도 기존 범용 fallback chain을 사용한다.

## 5. 일반 대화 payload 호환성

첫 live 진단에서 `gpt-4.1-mini`에 기존 Planner용 `reasoning` option을 보내면 API가
HTTP 400으로 거절하는 것을 확인했다. 같은 요청에서 해당 option만 제외하면 정상
`message` 결과를 반환했다.

이를 모델 이름 prefix로 분기하지 않고 Provider 생성자의 명시적
`include_reasoning` 계약으로 구현했다. 역할별 model ID는 임의의 유효한 값을 받을
수 있으므로 새 역할 Provider는 모두 모델마다 지원 여부가 다른 선택적 필드를
생략한다.

- 명시한 일반 대화 및 로봇 Planner 역할: `include_reasoning=False`
- 기존 범용 역할과 명시하지 않아 기존 체인을 복제한 역할: `True`
- 기존 `OpenAIResponsesProvider` 기본값: `True`

따라서 기존 Provider payload parity는 유지하면서 `gpt-4.1-mini` 같은 model도 어느
역할에든 명시적으로 설정할 수 있다. 2026-09-03 추가 smoke에서 Terra 역시 해당
선택적 필드 없이 strict `navigate` Tool proposal을 반환했다.

## 6. 장애 격리

역할별 circuit은 다음처럼 독립적이다.

```text
Chat timeout
  -> Chat circuit OPEN
  -> Planner circuit CLOSED
  -> abstain fallback circuit CLOSED
  -> 다음 Robot action은 Planner로 정상 전달
```

반대 방향도 동일하게 검증했다. 선택된 역할의 실패는 안전한 non-action 응답으로
끝나며 Planner, Chat 또는 범용 fallback을 추가 호출하지 않는다. 기본
`provider_max_retries=0`에서는 선택된 역할의 실제 HTTP attempt도 최대 한 번이다.

## 7. 실제 API smoke

2026-09-03에 기존 로컬 credential을 재사용하되 원문 응답과 credential을 출력하거나
저장하지 않는 content-free smoke를 실행했다.

| 역할 | 요청 model | API 반환 model | 호출 | Tool 수 | reasoning | 결과 | latency |
| --- | --- | --- | ---: | ---: | --- | --- | ---: |
| 일반 대화 | `gpt-4.1-mini` | `gpt-4.1-mini-2025-04-14` | 1 | 0 | 없음 | `message` | 1560.744 ms |
| 로봇 계획 | `gpt-5.6-terra` | `gpt-5.6-terra` | 1 | 1 | 없음 | `navigate` proposal | 2922.535 ms |

smoke는 모델·payload 역할 경계만 검증했다. Planner 입력의 RobotState는 실제 권한이
아닌 synthetic untrusted 참고 상태였으며, RobotAction 생성과 Nav2 호출은 모두
0회였다.

## 8. 확인된 후속 통합 조건

현재 `TextTurnService`는 모델 요청에 `RobotState()`를 넣는다. 이 값은 권한으로
사용되지 않지만 boolean 기본값이 모두 `false`여서 일부 Planner는 이를 명시적인
이동 불가 상태로 해석할 수 있다. 실제 진단에서 Luna와 Terra가 이 중립 상태만 보고
이동 제안을 보류했다.

이번 Story에서 prompt나 상태 의미를 바꾸면 역할별 모델 설정을 넘어 기존 Safety
의미까지 함께 변경하게 된다. 따라서 다음을 SWM25-154의 실제 Text Agent 인수 조건으로
남긴다.

- 모델용 unknown state를 명시적인 unknown projection으로 표현한다.
- Planner는 명확한 사용자 의도를 proposal로만 반환한다.
- 실제 가능성과 허가는 LLM 이후 fresh trusted RobotState와 Safety가 판단한다.
- 정확한 TextTurn 입력에서도 named-room proposal과 confirmation을 확인한다.

## 9. 검증 결과

구현 중 확인한 결과:

```text
변경 전 focused baseline: 30 passed
역할별 focused suite:     45 passed
전체 source suite:        578 passed
flake8:                   passed
pydocstyle:               passed
git diff --check:         passed
isolated colcon build:    passed
isolated colcon test:     578 tests, 0 failures
installed import:         passed
installed ROS CLI check:  passed
live Chat smoke:          passed, API call 1
live Planner smoke:       passed, API call 1
RobotAction/Nav2:         0
```

## 10. Jira 결론용 요약

> SWM25-151의 FrontRoute별 Provider 자리를 재사용해 일반 대화에는
> `gpt-4.1-mini`, 로봇 행동 제안에는 `gpt-5.6-terra`를 독립적으로 연결했다.
> 일반 대화는 Tool과 reasoning option을 보내지 않고, Planner는 기존 strict Tool을
> 유지하되 모델 호환성을 위해 선택적 reasoning option을 보내지 않는다.
> Chat·Planner·기존 abstain fallback의 retry/circuit을
> 분리해 한 역할 장애가 다른 역할 호출로 번지지 않게 했다. 역할 설정이 없거나
> Router가 OFF이면 기존 동작을 유지하며, 실제 API smoke에서 각 역할 1회 호출과
> model metadata를 확인했다. RobotAction과 Nav2 호출은 0회였다.
