# SWM25-151 필요한 요청만 전문 Agent 처리기로 연결한다

## 1. 결론

SWM25-150에서 만든 다섯 가지 `FrontRoute` 계약을 기존
`AgentProvider.complete()` 위치에 연결했다. 캐시되지 않은 새 turn만 Router를 한 번
통과하며, 결과에 따라 원격 Provider 하나 또는 로컬 처리기 하나만 선택한다.

```text
TextTurn confirmation/replay
  -> Orchestrator.begin_turn()
  -> cached response 확인
  -> RoutedAgentProvider
     |- general_conversation   -> Tool 없는 Chat Provider
     |- clarification_required -> 서버의 고정 재질문
     |- robot_status_query     -> 현재는 조회 미연결 안내
     |- current_action_query   -> 현재는 조회 미연결 안내
     |- robot_action_request   -> 기존 Robot Planner
     `- None                   -> 기존 범용 Provider
```

Front route는 처리기를 선택할 뿐이다. Tool 검증, fresh RobotState, Safety,
confirmation, RobotAction과 Nav2 권한은 기존 서버 경계를 그대로 사용한다.

SWM25-150의 OpenAI candidate Inspector는 production Router로 승격하지 않았다. 실제
분류기와 threshold가 없는 현재 factory 기본값은 Router OFF이며 기존 Provider를 그대로
사용한다.

## 2. 변경 이유

Router를 `TextTurnService` 앞에서 실행하면 pending confirmation, 동일 request replay와
SWM25-149의 서버 clarification도 다시 분류하게 된다. 이 경우 이미 저장된 답을
재생하는 요청까지 분류 비용을 지불하고, 승인 응답 `네`가 일반 대화나 행동 요청으로
잘못 넘어갈 수 있다.

기존 Orchestrator는 다음 순서를 이미 보장한다.

```text
begin_turn
  -> cached response면 즉시 반환
  -> effective utterance 확정
  -> memory/history snapshot
  -> provider.complete
```

따라서 `RoutedAgentProvider`를 기존 Provider의 decorator로 주입했다. 기존 TextTurn,
Orchestrator, conversation transaction과 HTTP handler에는 Router hook을 추가하지 않았다.

## 3. 목표

- uncached turn만 다섯 route 중 하나의 처리기로 보낸다.
- 한 번의 처리 시도에서 Router와 선택된 Provider의 호출 수를 제한한다.
- 일반 대화와 로봇 행동 Provider의 Tool 권한을 분리한다.
- 준비되지 않은 조회 route가 LLM 추측이나 DB 직접 조회로 이어지지 않게 한다.
- 기존 replay, Safety, confirmation, API와 SQLite 의미를 유지한다.

## 4. 달성 조건 5개

1. [x] 동일 요청의 durable cached replay, pending confirmation 응답과 기존 서버
   clarification은 Router와 전문 Provider를 추가 호출하지 않는다.
2. [x] uncached handle attempt는 Router를 최대 한 번 호출하고, match된 처리기 하나만
   선택하며 비선택 처리기는 호출하지 않는다.
3. [x] 일반 대화는 복제한 `AgentRequest.available_tools=()`와 `ToolSpec=[]`를 모두
   적용하고, Provider가 `tool_call`을 반환해도 confirmation 전에 로컬 refusal로
   교체한다.
4. [x] `None`만 기존 범용 Provider로 보내며, Router 오류·invalid result와 미구현
   status/current-action route는 다른 원격 Provider 없이 bounded local 결과로
   종료한다.
5. [x] action route는 기존 Planner 결과를 TextDecisionPolicy, fresh RobotState,
   deterministic Safety와 confirmation에 그대로 전달하며 Router 자체는 Action·dispatch·
   Nav2 권한을 만들지 않는다.

## 5. 구현 구조

### 5.1 RoutedAgentProvider

`providers/routed.py`의 `RoutedAgentProvider`는 기존 `AgentProvider`를 구현한다.

```text
RoutedAgentProvider(
  FrontRoutingService,
  general_provider,
  robot_planner_provider,
  fallback_provider,
)
```

세 Provider 자리는 명시적으로 분리했지만 SWM25-151 factory에서는 기존 Provider
인스턴스 하나를 세 역할에 재사용한다. Chat 모델과 Planner 모델을 실제로 분리하는
설정은 SWM25-152 범위다.

선택된 Provider가 실패하면 다른 Provider로 cascading fallback하지 않는다. 첫 호출이
외부 시스템에 도달했는지 불명확한 상태에서 다른 처리기를 호출하면 호출 수와 route
권한이 달라질 수 있기 때문이다.

### 5.2 일반 대화의 이중 Tool 제거

일반 대화에서는 다음 두 입력을 모두 비운다.

```text
AgentRequest.available_tools = ()
Provider tools argument       = []
```

둘 중 하나만 비우면 prompt 또는 Responses Tool schema의 다른 경로로 `navigate`가
노출될 수 있다. 또한 일반 Provider가 계약을 무시하고 `tool_call`을 반환하면 wrapper가
이를 로컬 refusal로 교체한다. 이때 실제 원격 호출의 provider, model, latency, usage,
response ID와 context metrics는 보존해 관측값을 0으로 위장하지 않는다.

### 5.3 로컬 route

다음 route는 원격 Provider를 호출하지 않는다.

| route | 현재 결과 | 이유 |
|---|---|---|
| clarification | 구체적으로 말해 달라는 고정 질문 | 전역 모호성은 Tool 제안 전 해소 |
| robot status | 조회 경로 미연결 안내 | trusted status handler는 SWM25-143 범위 |
| current action | 조회 경로 미연결 안내 | active-action read model은 SWM25-144 범위 |
| Router error/invalid | 분류 기능 unavailable 안내 | invalid를 정상 abstain으로 위장하지 않음 |

로컬 결과는 `provider=malbut-front-policy`, 고정 policy revision, `latency_ms=0`으로
기록한다. 실제 Provider를 호출한 뒤 차단한 결과에는 이 로컬 metadata를 사용하지 않는다.

### 5.4 bounded history projection

Router에는 Orchestrator가 `begin_turn()`에서 얻은 동일 conversation snapshot의 다음
정보만 전달한다.

```text
current effective utterance
최근 user/assistant message
```

- assistant content가 비어 있으면 제외
- 최신 chronological suffix 최대 16개
- 메시지당 최대 300자
- 전체 최대 4000자
- summary, memory, user ID, RobotState, ToolSpec, credential, confirmation과 Action ID 제외

SWM25-149의 clarification resolver가 문장을 canonical utterance로 바꿨다면 그 결과가
Router와 선택 Provider 모두에 전달된다.

### 5.5 factory 기본 OFF

`build_orchestrator(..., front_router=None)`의 기본값은 기존 Provider를 그대로 사용한다.
명시적인 `FrontRouterPort`를 주입한 경우에만 wrapper를 만든다.

따라서 이번 변경은 다음을 하지 않는다.

- OpenAI candidate Inspector의 production 승격
- 새 환경변수 또는 기본 모델 변경
- 실제 status/current-action 조회
- HTTP schema 또는 SQLite migration
- confirmation, RobotAction, dispatch와 ROS/Nav2 변경

## 6. 실패 의미와 호출 상한

| 상황 | Router | 선택 Provider | 추가 Provider | 결과 |
|---|---:|---:|---:|---|
| cached replay | 0 | 0 | 0 | 저장 응답 재생 |
| server clarification | 0 | 0 | 0 | 기존 서버 질문 |
| route local handler | 1 | 0 | 0 | local result |
| route general/action | 1 | 1 logical | 0 | 선택 Provider 결과 |
| explicit `None` | 1 | fallback 1 logical | 0 | 기존 범용 동작 |
| Router error/invalid | 1 | 0 | 0 | local refusal |
| selected Provider failure | 1 | 1 logical attempt | 0 | 기존 fail-turn/error |

여기서 상한은 **한 uncached handle attempt의 logical Provider 호출** 기준이다. 현재
`ReliableProvider.complete()` 내부 retry와 model fallback의 실제 HTTP 횟수 측정은
SWM25-152 범위다.

또한 selected Provider 실패 시 기존 Orchestrator는 pending turn을 `fail_turn()`으로
정리한다. 클라이언트가 같은 request ID를 나중에 다시 제출하면 새로운 uncached attempt가
되어 Provider가 다시 호출될 수 있다. 실패 결과까지 영구 캐시하는 원장은 이번 범위에
포함하지 않는다.

## 7. 검증

추가한 핵심 검증은 다음과 같다.

- 다섯 route와 `None`의 정확한 처리기 선택 및 비선택 호출 0회
- Router exception·wrong type의 local refusal와 fallback 0회
- 선택 Provider 실패 후 두 번째 Provider 호출 0회
- 일반 대화의 request/tool 양쪽 capability 제거
- 일반 Provider의 악성 `navigate` 제안이 confirmation을 만들지 않음
- 빈 assistant 제외, newest suffix와 history 문자 예산
- process restart 뒤 cached replay의 새 Router·Provider 호출 0회
- pending confirmation의 모호한 답변·승인·replay 동안 Router 증가 0회
- SWM25-149의 `여기로 가줘` 서버 clarification이 Router보다 먼저 처리됨
- factory 기본 Provider 유지 및 explicit injection에서만 wrapper 생성

실행 결과:

```text
focused routing/integration suite: 96 passed
full malbut_agent_server suite:    566 passed
flake8:                            passed
pydocstyle production changes:     passed
isolated colcon build:              passed
isolated colcon test:               566 tests, 0 failures
installed import/factory/CLI smoke: passed
```

## 8. 후속 Story

### SWM25-152

- deterministic/local classifier와 server-owned threshold 구현
- 고정 한국어 corpus의 precision, recall과 action false-positive 측정
- Chat 모델과 Robot Planner 모델 분리
- route별 p50/p95 latency, token, 비용과 abstain 비율 측정
- logical 호출뿐 아니라 실제 HTTP retry/fallback 횟수 검증
- 기준을 통과한 경우에만 factory 기본 Router 활성화 검토

### SWM25-142~144

- read-only Tool 실행 경계
- trusted `get_robot_status` handler
- 사용자·대화에 결속된 `get_current_action` read model

## 9. Jira 결론용 요약

> SWM25-150의 다섯 가지 FrontRoute 계약을 기존 AgentProvider 위치에 연결했다. cached
> replay·pending confirmation·서버 clarification은 Router를 우회하고, uncached
> attempt만 Router를 최대 한 번 호출한 뒤 일반 대화, 로컬 clarification, 미구현 조회
> 안내, Robot Planner 또는 기존 범용 Provider 중 하나만 선택한다. 일반 대화에서는
> request와 ToolSpec 양쪽의 Tool을 제거하고 잘못 반환된 navigate 제안도 confirmation
> 전에 refusal로 차단했다. 기존 API·DB·Safety·confirmation·ROS 동작은 변경하지
> 않았으며, 검증되지 않은 OpenAI Inspector는 production에 연결하지 않아 factory
> 기본 Router는 OFF로 유지했다.
